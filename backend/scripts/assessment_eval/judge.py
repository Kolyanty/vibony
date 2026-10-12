"""Grading the generated assessment.

Two layers, combined per scenario into a :class:`Verdict`:

* **Programmatic** (no LLM, deterministic, universal): every cited ``event_seq``
  exists in the session; ``summary_md`` has exactly the three required H2 sections
  in order; a near-empty scenario was answered honestly (says "insufficient
  activity", keeps findings ≤ 1).
* **LLM judge** (a *different* model than the generator — against self-leniency):
  per-pitfall recall against the scenario label, hallucinated pitfalls (asserted
  but not triggered), faithfulness (1–5), actionability (1–5).

The LLM judge is given the scenario's ground-truth label, so recall/precision are
graded against fact, not the judge's own guess.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import litellm
from pydantic import BaseModel, Field, ValidationError

from vibony_backend.assessment.inputs import TaskSpec, read_task_spec_from_disk, render_transcript
from vibony_backend.proxy import DEFAULT_MODEL

from scripts.assessment_eval.corpus import label_from_dict, record_events

logger = logging.getLogger(__name__)

# Default judge model. Override with --judge-model; prefer a *different* (ideally
# stronger) model than the generator so the judge does not rubber-stamp its own family.
JUDGE_MODEL_DEFAULT = DEFAULT_MODEL
_TIMEOUT_SECONDS = 300

REQUIRED_SECTIONS = (
    "## What went well",
    "## What's missing for an ideal solution",
    "## Observed reasoning patterns",
)


# ---------------------------------------------------------------------------
# Programmatic checks
# ---------------------------------------------------------------------------


@dataclass
class ProgrammaticReport:
    seqs_cited: int = 0
    seqs_valid: int = 0
    section_ok: bool = False
    # None when the scenario is not near-empty; else whether the report was honest.
    near_empty_honest: bool | None = None
    issues: list[str] = field(default_factory=list)


# Phrases that signal an honest near-empty report. Coupled to the wording the
# production prompt asks for ("insufficient activity to assess" — see
# vibony_backend.assessment.prompts.NEAR_EMPTY_NOTE / SYSTEM_PROMPT). If that
# prompt is reworded, update these or the honesty check silently goes blind.
_HONEST_MARKERS = (
    "insufficient activity",
    "no activity",
    "little activity",
    "no meaningful",
    "did not produce any code",
    "no solution exists",
    "no candidate activity",
    "no evidence of any",
)


def _sections_in_order(summary_md: str) -> bool:
    """True iff all three required H2 headers appear, in order."""
    idx = [summary_md.find(h) for h in REQUIRED_SECTIONS]
    if any(i < 0 for i in idx):
        return False
    return idx == sorted(idx)


def programmatic_checks(
    record: dict[str, Any], summary_md: str, findings: list[dict[str, Any]]
) -> ProgrammaticReport:
    rep = ProgrammaticReport()
    valid_seqs = {seq for seq, _t, _p in record_events(record)}

    cited = [f.get("event_seq") for f in findings if f.get("event_seq") is not None]
    rep.seqs_cited = len(cited)
    rep.seqs_valid = sum(1 for s in cited if int(s) in valid_seqs)
    if rep.seqs_valid != rep.seqs_cited:
        bad = [s for s in cited if int(s) not in valid_seqs]
        rep.issues.append(f"findings cite non-existent event_seq: {bad}")

    rep.section_ok = _sections_in_order(summary_md)
    if not rep.section_ok:
        rep.issues.append("summary_md missing the 3 required H2 sections in order")

    label = label_from_dict(record.get("label", {}))
    if label.near_empty:
        low = summary_md.lower()
        honest = any(m in low for m in _HONEST_MARKERS) and len(findings) <= 1
        rep.near_empty_honest = honest
        if not honest:
            rep.issues.append(
                "near-empty session: report did not say 'insufficient activity' "
                f"and/or invented findings ({len(findings)})"
            )

    return rep


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------


class PitfallJudgment(BaseModel):
    index: int
    triggered: bool  # echoed ground truth (the judge is told this)
    caught: bool  # did the assessment surface it as a finding/section


class JudgeVerdict(BaseModel):
    pitfalls: list[PitfallJudgment] = Field(default_factory=list)
    # Pitfall indices the report ASSERTED but that were not triggered (false positives).
    hallucinated_pitfalls: list[int] = Field(default_factory=list)
    faithfulness: int = Field(ge=1, le=5)
    actionability: int = Field(ge=1, le=5)
    rationale: str = ""


_JUDGE_SYSTEM = (
    "You are a strict meta-evaluator. You are given a task's expected AI pitfalls "
    "(employer ground truth), which pitfalls a synthetic session ACTUALLY triggered "
    "(the label), the session transcript, and an AI-written assessment of that "
    "session. Judge the assessment, not the candidate.\n\n"
    "For each pitfall: echo whether it was triggered (from the label) and decide "
    "whether the assessment CAUGHT it (named it as a gap/observation). A pitfall that "
    "was NOT triggered must NOT be reported — if the assessment asserts it anyway, "
    "list its index in hallucinated_pitfalls. Rate faithfulness (1=invents facts the "
    "transcript/code don't support, 5=every claim is grounded) and actionability "
    "(1=vague, 5=specific and fixable). Be skeptical and concise.\n\n"
    "Return ONLY this JSON object:\n"
    "{\n"
    '  "pitfalls": [{"index": int, "triggered": bool, "caught": bool}],\n'
    '  "hallucinated_pitfalls": [int],\n'
    '  "faithfulness": 1-5,\n'
    '  "actionability": 1-5,\n'
    '  "rationale": string\n'
    "}"
)


def build_judge_prompt(
    spec: TaskSpec | None,
    record: dict[str, Any],
    summary_md: str,
    findings: list[dict[str, Any]],
) -> str:
    label = label_from_dict(record.get("label", {}))
    pitfall_lines = []
    if spec:
        for i, p in enumerate(spec.expected_ai_pitfalls):
            sev = f" [{p.severity}]" if p.severity else ""
            pitfall_lines.append(f"  [{i}]{sev} {p.description}")
    pitfalls_block = "\n".join(pitfall_lines) or "  (none)"

    transcript, _ = render_transcript(record_events(record))

    return "\n\n".join(
        [
            "=== EXPECTED PITFALLS (ground truth, indexed) ===\n" + pitfalls_block,
            "=== LABEL (what THIS session triggered) ===\n"
            + json.dumps(
                {
                    "triggered_pitfalls": label.triggered_pitfalls,
                    "near_empty": label.near_empty,
                    "fought_ai": label.fought_ai,
                    "notes": label.notes,
                }
            ),
            "=== SESSION TRANSCRIPT ===\n" + (transcript or "(empty)"),
            "=== ASSESSMENT UNDER TEST ===\n"
            + "summary_md:\n"
            + summary_md
            + "\n\nfindings:\n"
            + json.dumps(findings, ensure_ascii=False, indent=2),
            "Judge it now. Return only the JSON object.",
        ]
    )


def _parse_verdict(content: str | None) -> JudgeVerdict:
    text = (content or "").strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return JudgeVerdict.model_validate(json.loads(text))


async def llm_judge(
    record: dict[str, Any],
    summary_md: str,
    findings: list[dict[str, Any]],
    *,
    spec: TaskSpec | None = None,
    model: str | None = None,
) -> JudgeVerdict:
    if spec is None:
        spec = read_task_spec_from_disk(record["task_id"])
    used_model = model or JUDGE_MODEL_DEFAULT
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": build_judge_prompt(spec, record, summary_md, findings)},
    ]
    response = await litellm.acompletion(
        model=used_model,
        messages=messages,
        response_format={"type": "json_object"},
        timeout=_TIMEOUT_SECONDS,
    )
    content = response.choices[0].message.content
    try:
        return _parse_verdict(content)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        logger.warning("llm_judge: bad verdict JSON (%s); returning neutral", exc)
        return JudgeVerdict(faithfulness=1, actionability=1, rationale=f"judge parse error: {exc}")


# ---------------------------------------------------------------------------
# Combined verdict
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    task_id: str
    key: str
    programmatic: ProgrammaticReport
    judge: JudgeVerdict
    # Ground truth from the scenario label — NOT the LLM's echoed `triggered` flag.
    # The recall denominator is the one number the whole harness exists to trust, so
    # it must be fact: if the judge drifts (omits/adds a pitfall row) the metric stays
    # anchored to the label. The judge is consulted only for which were *caught*.
    triggered_pitfalls: list[int] = field(default_factory=list)

    @property
    def triggered_total(self) -> int:
        return len(self.triggered_pitfalls)

    @property
    def caught_triggered(self) -> int:
        caught = {p.index for p in self.judge.pitfalls if p.caught}
        return sum(1 for i in self.triggered_pitfalls if i in caught)


async def judge_scenario(
    record: dict[str, Any],
    summary_md: str,
    findings: list[dict[str, Any]],
    *,
    spec: TaskSpec | None = None,
    model: str | None = None,
) -> Verdict:
    prog = programmatic_checks(record, summary_md, findings)
    verdict = await llm_judge(record, summary_md, findings, spec=spec, model=model)
    label = label_from_dict(record.get("label", {}))
    return Verdict(
        task_id=record["task_id"],
        key=record["key"],
        programmatic=prog,
        judge=verdict,
        triggered_pitfalls=label.triggered_pitfalls,
    )


__all__ = [
    "JUDGE_MODEL_DEFAULT",
    "JudgeVerdict",
    "PitfallJudgment",
    "ProgrammaticReport",
    "Verdict",
    "build_judge_prompt",
    "judge_scenario",
    "llm_judge",
    "programmatic_checks",
]
