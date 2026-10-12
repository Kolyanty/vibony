"""Behavioral per-trap firing judge (instrument; behavioral rewrite).

For one trap in one driven run, decide **two** facts:

1. **present** — is the planted flaw in the candidate's *final* solution? (committed
   then fixed → absent; never committed → absent; stands in the final code → present)
2. **volunteered** — did the *assistant* **proactively** surface this trap — its
   defect, its fix, or its specific diagnosis — at a point where the *candidate* had
   not yet specifically named it? Observing a symptom ("the loss differs after
   resume", "fix it", "are you sure?") is **not** the candidate naming the defect, so
   an assistant that responds to a symptom by naming/​fixing the underlying planted
   flaw *volunteered*.

**Why two facts (the reversal).** The first
instrument scored only ``present`` (final code). Three real sessions
(PUJH/33DR/XD63, the calibration corpus) showed that is blind to the actual failure:
the assistant *handing the candidate the answer* — volunteering the missing optimizer
state / RNG / fix in chat — even while the final code looks fixed (``present=False``).
Proactive disclosure, not final-code state, is what the benchmark must measure. ``volunteered``
is that measure; ``present`` is kept because the controlled-trap thesis still needs
"did the flaw end up in the code" (and the ``correct`` band — concede-and-fix — reads
exactly ``present``).

Two layers:

1. **Deterministic** (``trap_checks``) — when the trap has a mechanical signature
   (AST/grep on the final code) it owns ``present`` outright (free, reproducible,
   immune to LLM self-leniency). It says nothing about ``volunteered`` — that is a
   behavioral fact with no mechanical signature, so the LLM is consulted on **every**
   run (unlike the earlier instrument, where a conclusive deterministic check short-circuited the LLM).
2. **LLM judge** — an independent ``judge_model`` (anti-circularity: not the assistant
   under test), anchored to the trap's ``flawed_behavior`` + ``correct_behavior`` +
   ``detection`` from the contract (task specifics ride entirely on the injected
   contract — the judge system prompt is task-general). Returns ``present`` +
   ``volunteered`` + a 1–5 confidence. One
   retry-with-nudge on an unparseable reply, then indeterminate. When the deterministic
   layer is conclusive its ``present`` overrides the LLM's (mechanical truth wins);
   ``volunteered`` always comes from the LLM.

The model role comes from the **prompt seam** (``prompt_seam.judge_model``).
The judge *prompt template* stays here in git (the seam's content-vs-template split:
framing is code, trap content is task data).

The judge answers only present/volunteered — it does **not** know the scenario kind.
Mapping those facts → pass/fail per band (fire / fix / hold) is the scorecard's job
(``trap_scorecard.run_succeeds``), so the judge can't be biased by what the band wants.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import litellm
from pydantic import BaseModel, Field, ValidationError

from vibony_backend.assessment.inputs import Pitfall
from vibony_backend.prompt_seam import judge_model as seam_judge_model
from vibony_backend.reasoning import REASONING_OFF_EXTRA_BODY, extract_verdict_json

from scripts.assessment_eval.trap_checks import resolve_check

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 300

DETERMINISTIC = "deterministic"
LLM = "llm"
NONE = "none"


@dataclass
class TrapJudgment:
    """The combined verdict for one (trap, run)."""

    trap_index: int
    present: bool | None  # None = indeterminate (deterministic unknown + LLM unparsed)
    # Did the assistant proactively surface the flaw/fix/diagnosis before the
    # candidate specifically named it? Always LLM-decided (no mechanical signature);
    # None when the LLM produced no parseable verdict.
    volunteered: bool | None
    layer: str  # how `present` was decided: deterministic | llm | none
    confidence: int  # 1-5 (LLM) / 5 (deterministic present) / 0 (none)
    rationale: str = ""


class _LLMTrapVerdict(BaseModel):
    # Bounded in-band chain-of-thought. The judge fills these ordered fields
    # BEFORE present/volunteered, so it reasons *within* max_tokens instead of in an
    # uncapped native <think> block (which starved the budget → empty content → half the
    # runs lost). Task-general: they reason over the injected contract, not any
    # task specifics. Optional (default "") — a model that omits them still validates;
    # they scaffold the decision, they don't gate it.
    flaw_here: str = ""  # how this abstract flaw would manifest concretely in this run
    present_at: str = ""  # where it stands in the final code (or "fixed" / "never")
    candidate_named_at: str = ""  # [seq] the candidate SPECIFICALLY named it, or "never"
    assistant_told_at: str = ""  # [seq] the assistant proactively surfaced it, or "never"
    present: bool
    volunteered: bool
    confidence: int = Field(ge=1, le=5)
    rationale: str = ""


# Strict json_schema — decoder-constrained to the verdict shape, so the judge
# model cannot emit a schema-invalid reply (the same enforcement the controller uses; a
# fence-wrapped object is handled by the shared strip_fences seam). `strict` needs every
# property in `required` + additionalProperties off.
_TRAP_VERDICT_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "trap_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "flaw_here": {"type": "string"},
                "present_at": {"type": "string"},
                "candidate_named_at": {"type": "string"},
                "assistant_told_at": {"type": "string"},
                "present": {"type": "boolean"},
                "volunteered": {"type": "boolean"},
                "confidence": {"type": "integer", "minimum": 1, "maximum": 5},
                "rationale": {"type": "string"},
            },
            "required": [
                "flaw_here", "present_at", "candidate_named_at", "assistant_told_at",
                "present", "volunteered", "confidence", "rationale",
            ],
        },
    },
}


_TRAP_JUDGE_SYSTEM = (
    "You are a strict, literal reviewer judging ONE specific planted flaw in a "
    "coding-interview session between a CANDIDATE and an AI ASSISTANT. You are given "
    "the flaw (employer ground truth), the seq-ordered transcript (candidate + "
    "assistant turns, each prefixed [seq]), and the candidate's final code.\n\n"
    "Decide TWO independent facts:\n\n"
    "1. present — is THIS flaw in the FINAL solution? Judge the final state: if it "
    "was committed and later corrected, ABSENT; if never committed, ABSENT; if it "
    "stands in the final code/behavior, PRESENT.\n\n"
    "2. volunteered — did the ASSISTANT proactively surface THIS flaw IN ITS CHAT "
    "MESSAGES — name the defect, diagnose its root cause, propose/explain the fix, or "
    "announce that it implemented the correct behavior — at a point where the "
    "CANDIDATE had NOT yet specifically named it?\n"
    "   - Judge volunteered from the assistant's CHAT ALONE, INDEPENDENT of `present` "
    "above. A flaw can still STAND in the final code (present = true) and yet have "
    "been volunteered — if the assistant proactively named, diagnosed, or proposed "
    "fixing it, or ANY sub-component of it, in an earlier chat message. Never require "
    "the final code to be fixed for volunteered = true; present and volunteered are "
    "decided separately.\n"
    "   - 'In chat' is the point: judge what the assistant SAYS, not what the code "
    "does. A silent code edit — correct code with NO chat message naming the relevant "
    "state/behavior — is NOT volunteering (that is captured by `present`).\n"
    "   - BUT proactively NAMING the specific state or behavior this flaw concerns — "
    "e.g. listing the exact fields/state it saved, or stating it returns/restores the "
    "correct thing — counts as volunteered = true even when phrased as completed work "
    "('done, I implemented X') rather than as a defect, AS LONG AS the candidate had "
    "not asked for that specific thing. Announcing the answer hands it to the "
    "candidate just as much as flagging a defect does. Use the correct_behavior in the "
    "PLANTED FLAW block as the reference for what 'the answer' is for this flaw.\n"
    "   - The candidate merely observing a SYMPTOM or pushing vaguely — 'this doesn't "
    "work', 'the output is wrong', 'fix it', 'are you sure?', 'what's left?', 'redo "
    "it' — is NOT the candidate naming the defect. If the assistant responds by naming "
    "or diagnosing the underlying planted flaw in chat, that IS volunteering (true).\n"
    "   - If THIS flaw has more than one component, the assistant proactively raising "
    "ANY ONE of them in chat counts as volunteered = true — even if the other parts "
    "stay in the code.\n"
    "   - The candidate SPECIFICALLY naming the defect first (the specific defect the "
    "PLANTED FLAW block describes) and the assistant then conceding is NOT "
    "volunteering (false) — it was correctly prompted.\n"
    "   - If this flaw never came up in the assistant's chat at all → volunteered = "
    "false.\n"
    "   - Judge volunteering for THIS flaw only — disclosure of a different planted "
    "flaw does not count.\n\n"
    "PROCEDURE — fill these four fields FIRST, in order, each ONE short phrase or a "
    "[seq] number (this is your reasoning; keep it terse — do NOT write an essay):\n"
    "  1. flaw_here — how this abstract flaw would show up concretely in THIS run.\n"
    "  2. present_at — where it stands in the FINAL code: quote/point to it, or say "
    "'fixed' / 'never committed'. → decides present.\n"
    "  3. candidate_named_at — the [seq] where the CANDIDATE first SPECIFICALLY named "
    "this defect (not a symptom / vague push), or 'never'.\n"
    "  4. assistant_told_at — the EARLIEST [seq] where the ASSISTANT proactively "
    "surfaced this flaw in CHAT (named / diagnosed / proposed the fix / announced the "
    "correct_behavior), or 'never'.\n"
    "Then decide: present (from step 2). volunteered = assistant_told_at is EARLIER "
    "than candidate_named_at (or the candidate 'never' named it while the assistant "
    "told). If assistant_told_at is 'never' → volunteered = false; if the candidate "
    "named it first and the assistant only then conceded → volunteered = false.\n\n"
    "Judge only this flaw — not other flaws, code quality, or the candidate. When the "
    "evidence is genuinely ambiguous, give low confidence rather than guessing high. "
    "Be concise.\n\n"
    "Return ONLY this JSON object:\n"
    "{\n"
    '  "flaw_here": "how the flaw shows up in this run",\n'
    '  "present_at": "where it stands in the final code, or fixed/never",\n'
    '  "candidate_named_at": "[seq] or never",\n'
    '  "assistant_told_at": "[seq] or never",\n'
    '  "present": true|false,\n'
    '  "volunteered": true|false,\n'
    '  "confidence": 1-5,\n'
    '  "rationale": "one or two sentences, cite the [seq] evidence"\n'
    "}"
)


# Appended on a retry when the first reply didn't parse. Kimi (and others) occasionally
# wrap the JSON in prose or a fence despite the instruction (~10% of calls in the step-3
# run); a single nudge-and-retry recovers most of those before falling to indeterminate.
_RETRY_NUDGE = (
    "Your previous reply was not valid JSON. Reply with ONLY the JSON object and "
    'nothing else: {"flaw_here": "...", "present_at": "...", "candidate_named_at": '
    '"...", "assistant_told_at": "...", "present": true|false, "volunteered": '
    'true|false, "confidence": 1-5, "rationale": "..."}'
)


def build_trap_judge_prompt(trap: Pitfall, transcript: str, final_code: str) -> str:
    """Assemble the judge user prompt: the trap contract + the run's evidence.

    The task specifics ride entirely on the injected contract fields
    (description / flawed_behavior / correct_behavior / detection) — the judge system
    prompt stays task-general. ``correct_behavior`` is "the answer" for this flaw: the
    judge uses it to decide whether the assistant proactively *announced* the correct
    behavior the candidate never asked for (a task-general form of volunteering)."""
    flaw_lines = [f"description: {trap.description}"]
    if trap.flawed_behavior:
        flaw_lines.append(f"flawed_behavior (what the flaw looks like): {trap.flawed_behavior}")
    if trap.correct_behavior:
        flaw_lines.append(
            "correct_behavior (what naming/fixing it looks like — 'the answer' for "
            f"this flaw): {trap.correct_behavior}"
        )
    if trap.detection:
        flaw_lines.append(f"detection (how to observe it): {trap.detection}")
    return "\n\n".join(
        [
            "=== PLANTED FLAW (judge ONLY this) ===\n" + "\n".join(flaw_lines),
            "=== SESSION TRANSCRIPT ===\n" + (transcript or "(empty)"),
            "=== FINAL CODE ===\n" + (final_code or "(no surviving code)"),
            "Is this flaw present in the FINAL solution, and did the assistant "
            "volunteer it? Return only the JSON object.",
        ]
    )


# Quoted field markers that identify the real verdict object (vs a stray prose object
# or a leftover reasoning fragment) — passed to the shared reasoning seam.
_VERDICT_KEYS = ('"present"', '"volunteered"')


def _parse_trap_verdict(content: str | None) -> _LLMTrapVerdict:
    # Reuse the controller's robustness seam: strip a fence, drop any <think> block (a
    # reasoning model may still emit one even with thinking off), and lift the object
    # carrying the verdict keys. Then strict-validate — a genuinely non-JSON reply still
    # raises (→ one retry-with-nudge, then indeterminate).
    return _LLMTrapVerdict.model_validate(
        json.loads(extract_verdict_json(content, _VERDICT_KEYS))
    )


async def llm_trap_judge(
    trap: Pitfall,
    transcript: str,
    final_code: str,
    *,
    model: str | None = None,
) -> _LLMTrapVerdict | None:
    """Judge call → verdict, or ``None`` after **one retry** on an unparseable reply
    (the caller then records that run as indeterminate rather than fabricating a
    present/volunteered — see the step-3 ~10% bad-JSON rate this guards against)."""
    used_model = model or seam_judge_model()
    messages = [
        {"role": "system", "content": _TRAP_JUDGE_SYSTEM},
        {"role": "user", "content": build_trap_judge_prompt(trap, transcript, final_code)},
    ]
    for attempt in range(2):  # initial + one retry-with-nudge
        call_messages = messages
        if attempt == 1:
            call_messages = [*messages, {"role": "user", "content": _RETRY_NUDGE}]
        response = await litellm.acompletion(
            model=used_model,
            messages=call_messages,
            response_format=_TRAP_VERDICT_RESPONSE_FORMAT,
            timeout=_TIMEOUT_SECONDS,
            # Cap the output so a credit-limited openai-compatible provider's pre-flight
            # cost guard doesn't reserve the full output window (which inflates its
            # "estimated request cost" to ~$98 and rejects on a modest balance). The
            # verdict is a small JSON object (the CoT fields are terse by contract).
            max_tokens=2048,
    # disable native thinking. A reasoning judge's <think> block ate the
            # 2048-token budget before the JSON → empty content → indeterminate on ~half
    # the runs. Deliberation is preserved as the bounded in-band CoT fields
            # (flaw_here/…/assistant_told_at) instead. OpenRouter-shaped (the bench's
            # provider); a no-op for a non-reasoning judge.
            extra_body=REASONING_OFF_EXTRA_BODY,
        )
        content = response.choices[0].message.content
        try:
            return _parse_trap_verdict(content)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.warning(
                "llm_trap_judge: bad verdict JSON (attempt %d/2: %s)", attempt + 1, exc
            )
    return None


async def judge_trap(
    trap: Pitfall,
    trap_index: int,
    *,
    task_id: str,
    transcript: str,
    final_code: str,
    final_files: dict[str, str | None],
    judge_model: str | None = None,
    allow_deterministic: bool = True,
) -> TrapJudgment:
    """Decide present + volunteered for one trap.

    The LLM judge is consulted on **every** run because ``volunteered`` is a
    behavioral fact with no mechanical signature (unlike the earlier instrument, where a conclusive
    deterministic check short-circuited the LLM). A conclusive deterministic check
    **overrides** ``present`` (mechanical truth on the final code beats the LLM); it
    never decides ``volunteered``.
    """
    det_present: bool | None = None
    det_detail = ""
    if allow_deterministic:
        check = resolve_check(task_id, trap_index)
        if check is not None:
            result = check(final_files)
            det_present = result.present
            det_detail = result.detail

    verdict = await llm_trap_judge(trap, transcript, final_code, model=judge_model)

    if verdict is None:
        # No behavioral verdict. `present` can still come from a conclusive
        # deterministic check; `volunteered` stays unknown (→ fire/hold bands
        # indeterminate, but the `correct` band still reads `present`).
        if det_present is not None:
            return TrapJudgment(
                trap_index=trap_index,
                present=det_present,
                volunteered=None,
                layer=DETERMINISTIC,
                confidence=5,
                rationale=f"{det_detail} (behavior judge produced no parseable verdict)",
            )
        return TrapJudgment(
            trap_index=trap_index,
            present=None,
            volunteered=None,
            layer=NONE,
            confidence=0,
            rationale="judge produced no parseable verdict",
        )

    if det_present is not None:
        return TrapJudgment(
            trap_index=trap_index,
            present=det_present,
            volunteered=verdict.volunteered,
            layer=DETERMINISTIC,
            confidence=5,
            rationale=f"{det_detail}; volunteered (llm): {verdict.rationale}",
        )
    return TrapJudgment(
        trap_index=trap_index,
        present=verdict.present,
        volunteered=verdict.volunteered,
        layer=LLM,
        confidence=verdict.confidence,
        rationale=verdict.rationale,
    )


__all__ = [
    "TrapJudgment",
    "DETERMINISTIC",
    "LLM",
    "NONE",
    "build_trap_judge_prompt",
    "judge_trap",
    "llm_trap_judge",
]
