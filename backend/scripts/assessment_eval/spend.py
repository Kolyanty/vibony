"""LLM spend accounting for a trap-bench sweep.

A trap-bench run spends real money at three places — the assistant under test (via
``call_llm``), the candidate driver, and the judge — all through ``litellm.acompletion``.
This registers ONE process-global ``litellm`` success callback so every completion in the
process is tallied with no per-call-site plumbing: token counts (always exact) and cost.

**Cost is OpenRouter's own charged amount.** OpenRouter returns ``usage.cost`` (USD) in
every response, and litellm surfaces it as ``response_cost`` — so the figure is what was
actually billed for OpenRouter-routed models, NOT a guess from litellm's price map (which
does not even know these models). A provider that reports no cost is counted under
``unknown_cost_calls`` and excluded from the dollar total (never silently zeroed into it).
The OpenRouter dashboard remains the authoritative billed total; this is the per-run view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import litellm
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger(__name__)


@dataclass
class _ModelSpend:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


def _extract_cost(kwargs: dict, response_obj, usage: dict) -> float | None:
    """OpenRouter's reported cost for one call, or ``None`` if the provider gave none.

    Order: response ``usage.cost`` (OpenRouter native) → litellm ``response_cost`` in the
    callback kwargs → ``_hidden_params.response_cost`` on the response. All three carry the
    same OpenRouter value when present; the fallbacks just cover litellm-version variance.
    """
    cost = usage.get("cost")
    if cost is None:
        cost = kwargs.get("response_cost")
    if cost is None:
        hidden = getattr(response_obj, "_hidden_params", None) or {}
        cost = hidden.get("response_cost")
    return None if cost is None else float(cost)


class SpendTracker(CustomLogger):
    """Process-global accumulator of per-model LLM spend over one sweep.

    Append it to ``litellm.callbacks`` via :meth:`register` before the sweep; it then
    fires for every ``litellm.acompletion`` in the process. Accounting never raises —
    a bad/odd response is logged at debug and skipped, never breaking a run.
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_model: dict[str, _ModelSpend] = {}
        self.unknown_cost_calls = 0

    # -- registration -----------------------------------------------------
    def register(self) -> None:
        if self not in litellm.callbacks:
            litellm.callbacks.append(self)

    def unregister(self) -> None:
        if self in litellm.callbacks:
            litellm.callbacks.remove(self)

    # -- capture ----------------------------------------------------------
    def _record(self, kwargs: dict, response_obj) -> None:
        try:
            model = kwargs.get("model") or getattr(response_obj, "model", None) or "?"
            usage_obj = getattr(response_obj, "usage", None)
            usage: dict = {}
            if usage_obj is not None:
                usage = (
                    usage_obj.model_dump()
                    if hasattr(usage_obj, "model_dump")
                    else dict(usage_obj)
                )
            ms = self.by_model.setdefault(model, _ModelSpend())
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            # Some non-OpenRouter providers omit total_tokens — fall back to the sum.
            tt = int(usage.get("total_tokens") or 0) or (pt + ct)
            ms.calls += 1
            ms.prompt_tokens += pt
            ms.completion_tokens += ct
            ms.total_tokens += tt
            cost = _extract_cost(kwargs, response_obj, usage)
            if cost is None:
                self.unknown_cost_calls += 1
            else:
                ms.cost_usd += cost
        except Exception:  # accounting must never break a sweep
            logger.debug("spend tracker: failed to record a call", exc_info=True)

    # Only the async hook is implemented — the bench is all ``acompletion`` (async), and
    # implementing both would double-count if litellm fired sync + async for one call.
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs, response_obj)

    # -- totals + render --------------------------------------------------
    @property
    def total_calls(self) -> int:
        return sum(m.calls for m in self.by_model.values())

    @property
    def total_cost(self) -> float:
        return sum(m.cost_usd for m in self.by_model.values())

    def render_markdown(self) -> str:
        lines = [
            "## Spend (this run)",
            "",
            "Cost is OpenRouter's own reported `usage.cost` (USD billed), not a price-map "
            "estimate — exact for OpenRouter-routed models; tokens are exact. The "
            "OpenRouter dashboard is the authoritative billed total.",
            "",
            "| model | calls | prompt tok | completion tok | total tok | cost (USD) |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for model in sorted(self.by_model):
            m = self.by_model[model]
            lines.append(
                f"| `{model}` | {m.calls} | {m.prompt_tokens:,} | "
                f"{m.completion_tokens:,} | {m.total_tokens:,} | ${m.cost_usd:.4f} |"
            )
        tot_p = sum(m.prompt_tokens for m in self.by_model.values())
        tot_c = sum(m.completion_tokens for m in self.by_model.values())
        tot_t = sum(m.total_tokens for m in self.by_model.values())
        lines.append(
            f"| **total** | {self.total_calls} | {tot_p:,} | {tot_c:,} | {tot_t:,} | "
            f"**${self.total_cost:.4f}** |"
        )
        if self.unknown_cost_calls:
            lines += [
                "",
                f"_{self.unknown_cost_calls} call(s) reported no cost (excluded from the "
                "dollar total) — tokens still counted. Cost is only returned by "
                "OpenRouter-routed models; a model pointed at another provider (e.g. a "
                "direct/openai-compatible route) shows here._",
            ]
        return "\n".join(lines) + "\n"


__all__ = ["SpendTracker"]
