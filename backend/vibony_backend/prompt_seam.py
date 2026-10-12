"""Prompt-resolution seam + model-role config.

The trap-control work separates two concerns the current ``weakness.py``
mechanism conflates:

* **Content (data, per-task, model-agnostic).** A task's traps — *what* error to
  plant, when, how to concede / defend — are data on the task (``spec.yaml``'s
  ``expected_ai_pitfalls`` contract, the DB ``weakness`` mapping). A task is never
  rewritten per model.
* **Template (code/config, in git).** The *framing* that renders trap content into
  an LLM system prompt, the judge prompt — these are code/config, versioned in
  git, and may legitimately need a model-specific variant (a frontier model reads
  a directive differently than a small one).

This module is the seam for the second concern. :func:`resolve_prompt` returns the
prompt variant for ``(model_id, kind)``, falling back to the ``default`` variant
when no model-specific one is registered. The system runs **one** assistant model, so the
registry holds only ``default`` slots today — the per-model dimension is built but
**not populated**. Populating it later (a fallback model, a model swap, per-model
tuning) is a registration, not a refactor, and never touches task data.

**Model roles are independent config dimensions.** ``assistant_model`` (under
test, pinned to one for now), ``judge_model`` (independent — may be a stronger
model; anti-circularity for the benchmark), ``controller_model`` (only if a
controller is chosen). Each is its own env knob; resolving a role's prompt is
``resolve_prompt(<role model>, kind)``.

Nothing in production consumes this seam yet — ``weakness.py`` / ``proxy.py`` /
``assessment`` still call their prompt sources directly. The benchmark and the
chosen mechanism route through it. Built minimal + tested now so that
later work is wiring, not design. See architecture.md §7.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_VARIANT = "default"


# ---------------------------------------------------------------------------
# Prompt kinds — the distinct roles a prompt template plays. Stable strings:
# they are the registry keys, and the benchmark / mechanism refer to them by
# name.
# ---------------------------------------------------------------------------

ASSISTANT = "assistant"    # candidate-facing AI framing + trap-control directives
JUDGE = "judge"            # trap-firing / assessment judge (independent model)
CONTROLLER = "controller"  # per-turn trap-control gate (agent_host/controller.py)

KNOWN_KINDS = frozenset({ASSISTANT, JUDGE, CONTROLLER})


# ---------------------------------------------------------------------------
# Model roles (independent config dimensions)
# ---------------------------------------------------------------------------


def assistant_model() -> str:
    """The model under test (``VIBONY_ASSISTANT_MODEL`` → proxy ``DEFAULT_MODEL``).

    The system pins this to one model; the benchmark measures *its* trap-following.
    """
    from vibony_backend.proxy import DEFAULT_MODEL  # lazy: proxy imports weakness

    return os.environ.get("VIBONY_ASSISTANT_MODEL") or DEFAULT_MODEL


def judge_model() -> str:
    """The benchmark / assessment judge model (``VIBONY_JUDGE_MODEL``).

    Independent dimension: defaults to the assistant model for a working
    out-of-the-box setup, but for a real benchmark it **should** differ — a
    stronger model, and anti-circularity (the judge must not be the system it
    grades). Set ``VIBONY_JUDGE_MODEL`` explicitly when measuring.
    """
    return os.environ.get("VIBONY_JUDGE_MODEL") or assistant_model()


def controller_model() -> str | None:
    """The intermediate-controller model (``VIBONY_CONTROLLER_MODEL``). ``None``
    when unset → the controller is off and the agent loop behaves exactly as before.
    Set it (and add it to ``VIBONY_MODEL_ALLOWLIST``) to enable trap control. Should be
    a *different* family from the assistant — anti-circularity, same as the judge."""
    return os.environ.get("VIBONY_CONTROLLER_MODEL") or None


# ---------------------------------------------------------------------------
# Prompt-variant registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptVariant:
    """A resolved prompt template for one ``(kind, model)`` slot.

    ``build`` produces the prompt text; its arguments depend on the kind (the
    assistant builder takes a task weakness mapping, the judge builder takes
    none). ``resolved_for`` is the registry key that actually matched — the
    requested model when a model-specific variant exists, else
    :data:`DEFAULT_VARIANT` — so a caller can log / assert which variant fired.
    """

    kind: str
    requested_model: str
    resolved_for: str  # DEFAULT_VARIANT | a model id
    build: Callable[..., str]
    source: str = ""  # human note: where the canonical prompt lives


# (kind, model-or-DEFAULT_VARIANT) -> (builder, source)
_REGISTRY: dict[tuple[str, str], tuple[Callable[..., str], str]] = {}


def register_prompt(
    kind: str,
    build: Callable[..., str],
    *,
    model: str = DEFAULT_VARIANT,
    source: str = "",
) -> None:
    """Register a prompt builder for ``kind`` at ``model`` (``default`` if omitted).

    Re-registering the same slot overwrites — call once per slot at import time.
    """
    if kind not in KNOWN_KINDS:
        raise ValueError(f"unknown prompt kind {kind!r} (known: {sorted(KNOWN_KINDS)})")
    _REGISTRY[(kind, model)] = (build, source)


def resolve_prompt(model_id: str, kind: str) -> PromptVariant:
    """Resolve the prompt variant for ``(model_id, kind)``.

    A model-specific variant wins; otherwise the ``default`` slot. Raises
    ``ValueError`` for an unknown kind, ``KeyError`` when a known kind has no
    variant registered at all (a programming error — every *consumed* kind must
    register a ``default``; ``controller`` is reserved and intentionally empty
    until a controller model is configured).
    """
    if kind not in KNOWN_KINDS:
        raise ValueError(f"unknown prompt kind {kind!r} (known: {sorted(KNOWN_KINDS)})")
    slot = (kind, model_id)
    resolved_for = model_id
    if slot not in _REGISTRY:
        slot = (kind, DEFAULT_VARIANT)
        resolved_for = DEFAULT_VARIANT
    if slot not in _REGISTRY:
        raise KeyError(
            f"no prompt registered for kind {kind!r} (model {model_id!r}); "
            "register a default via register_prompt(...)"
        )
    build, source = _REGISTRY[slot]
    return PromptVariant(
        kind=kind,
        requested_model=model_id,
        resolved_for=resolved_for,
        build=build,
        source=source,
    )


# ---------------------------------------------------------------------------
# Default registrations
#
# Defaults delegate to the existing prompt sources, so the seam is *live*, not a
# placeholder. The per-model dimension is intentionally empty (one model).
# Later work flips the call sites (weakness.py / assessment) to consume
# resolve_prompt; until then these defaults are exercised only by the benchmark
# + tests. Lazy imports keep this module's import light and cycle-free.
# ---------------------------------------------------------------------------


def _build_assistant_prompt(weakness: dict | None = None) -> str:
    from vibony_backend.weakness import render_weakness_dict

    return render_weakness_dict(weakness)


def _build_judge_prompt() -> str:
    from vibony_backend.assessment.prompts import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def _build_controller_prompt() -> str:
    # The controller's system prompt is task-agnostic (a constant) — trap
    # contracts are injected per review into the user message, not here.
    from vibony_backend.agent_host.controller import _SYSTEM_PROMPT

    return _SYSTEM_PROMPT


register_prompt(
    ASSISTANT,
    _build_assistant_prompt,
    source="configs/global.yaml + task weakness (weakness.render_weakness_dict)",
)
register_prompt(
    JUDGE,
    _build_judge_prompt,
    source="assessment/prompts.py:SYSTEM_PROMPT",
)
register_prompt(
    CONTROLLER,
    _build_controller_prompt,
    source="agent_host/controller.py:_SYSTEM_PROMPT",
)


__all__ = [
    "ASSISTANT",
    "JUDGE",
    "CONTROLLER",
    "KNOWN_KINDS",
    "DEFAULT_VARIANT",
    "PromptVariant",
    "assistant_model",
    "judge_model",
    "controller_model",
    "register_prompt",
    "resolve_prompt",
]
