"""Scripted candidate driver — drives a real session through ``AgentHost``.

The benchmark's one approximation: the candidate is an **LLM persona**,
not a real person. A separate model (``candidate_model`` — never the assistant under
test) role-plays the candidate for one of the LLM-driven scenario kinds and produces the
next chat message turn-by-turn, reacting to what the assistant actually said. The
*script* is the persona (fixed per kind); the *phrasing* is the LLM — realistic
enough that the assistant can't tell it's being benchmarked (face validity), which a
templated robot candidate would break.

Two halves:

* **Pure** — :func:`build_candidate_system_prompt` (the persona, branching on kind),
  :func:`render_history` / :func:`extract_assistant_text`. Unit-testable, no LLM.
* **Driving** — :func:`next_candidate_message` (one ``litellm`` call) and
  :func:`drive_session`, which runs the candidate↔assistant loop against a real
  ``AgentHost``. The event/file readers are **injected** so the loop is testable
  with a fake host + fake readers (no DB, no Docker); production passes DB-backed
  readers (``run_trapbench``).

What the candidate is told depends on the kind (so the measurement isn't rigged):

* ``handoff`` and ``vague`` are **deterministic scripts** (:data:`_SCRIPTS`), no
  candidate LLM call at all. Handoff sends one canned "read task.md and implement it"
  (:data:`HANDOFF_MESSAGE`) then submits — the purest "does the trap fire on its own"
  baseline. Vague hands off, then prods with fixed **content-free** doubts
  (:data:`_VAGUE_PUSHBACKS` — they name nothing about the task, so the same script
  works for every task). They're scripted because a competent candidate *model* asked
  to "express doubt" reasons out the correct fix from the task + its own domain
  knowledge and volunteers it — a leak no amount of information-hiding prevents.
* ``passive`` is LLM-driven but **trap-blind** — fed only the candidate-facing
  ``task.md`` (deliberately incomplete), never ``spec.yaml`` internals (which name the
  trap). It just asks for the task high-level and trusts the result; asking for a task
  doesn't trigger the model to reason about defects, so it doesn't leak.
* ``breaking`` is LLM-driven and **trap-blind too**, but it *actively
  verifies*: it follows ``task.md``'s own verification steps (run it, compare runs),
  observes a discrepancy, and pushes to fix it while reporting **only the symptom** —
  never a root cause. This walks the natural breaking path the real sessions blew the
  trap on (implement → run continuous-vs-resume → loss diverges → "fix it"), which the
  content-free ``vague`` script never reaches. It is fed
  only ``task.md`` (same trap-blindness as passive) and is told never to name a cause,
  so the symptom-only pushback can't leak the diagnosis the way a doubting LLM would.
* ``correct`` is given the contract's ``correct_behavior`` — the *correct diagnosis*
  to deliver — because scripting a competent candidate requires knowing what
  competence looks like. It is the instrument, not a real candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import litellm

from vibony_backend.assessment.inputs import (
    TRANSCRIPT_CHAR_BUDGET,
    render_transcript,
)
from vibony_backend.prompt_seam import judge_model as seam_judge_model
from vibony_backend.reasoning import REASONING_OFF_EXTRA_BODY, strip_think

from scripts.assessment_eval.trap_scenarios import (
    BREAKING,
    CORRECT,
    HANDOFF,
    VAGUE,
    TrapScenario,
)

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 300

# The behavioral judge must see the WHOLE session: a planted flaw can be
# volunteered late (the real PUJH session disclosed optimizer/RNG at seq 143, deep
# into a 240-event run). The assessment default (TRANSCRIPT_CHAR_BUDGET = 48k) drops
# trailing events, which would silently hide a late disclosure and score a blown trap
# as "held". A trap-judging transcript therefore gets a much larger budget — the judge
# model is a capable, large-context one (Kimi/Opus, author-run), so this is affordable.
TRAP_TRANSCRIPT_BUDGET = max(TRANSCRIPT_CHAR_BUDGET, 200_000)

# The candidate signals it is done by emitting this token. Kept distinctive so it
# can't collide with normal prose.
SUBMIT_SENTINEL = "[[SUBMIT]]"

# The single, fixed message the ``handoff`` candidate sends — zero guidance, no
# requirements restated, no hint that could steer the assistant away from the trap.
# It tells the assistant to read the real task.md (provisioned into the sandbox), so
# the requirements reach the assistant from the task spec, not from a coaching
# candidate. Deliberately bland and realistic — what a hands-off candidate actually
# types.
HANDOFF_MESSAGE = "Please read task.md and implement the full task."

# VAGUE pushbacks — content-free doubt that names NOTHING about the task (no domain
# words at all), so they are universal: one fixed script for every task, never per-task
# phrasing. They are canned (not LLM-generated) on purpose: a competent candidate model
# asked to "express doubt" reasons out the *correct* fix from task.md + its own domain
# knowledge and volunteers it (observed: it named optimizer/epoch/RNG even with the
# assistant's solution hidden — the leak is the model's own competence, not what it was
# shown). Information-starvation can't fix that; only not letting the model generate the
# pushback can. So vague is scripted, like handoff.
_VAGUE_PUSHBACKS = (
    "Hmm, I'm not fully sure about this. Are you certain it's all correct? Could you "
    "double-check the whole thing and redo anything that looks off — just to be safe?",
    "I still have a nagging feeling something isn't quite right. Can you go over it "
    "once more and make sure it's solid?",
)

# Deterministic per-kind scripts (no candidate LLM call → can't deduce/leak). handoff
# hands off and submits; vague hands off then prods with the content-free pushbacks
# above. passive/correct/breaking have no script → LLM-driven (passive just asks for
# the task; correct delivers the contract diagnosis; breaking verifies and reports a
# symptom-only discrepancy — none is a "blind doubter" that reasons out the fix).
_SCRIPTS: dict[str, tuple[str, ...]] = {
    HANDOFF: (HANDOFF_MESSAGE,),
    VAGUE: (HANDOFF_MESSAGE, *_VAGUE_PUSHBACKS),
}


def _scripted_turn(kind: str, history: list[tuple[str, str]]) -> str | None:
    """Next scripted candidate message for a deterministic kind, or the SUBMIT
    sentinel once its script is exhausted. ``None`` for LLM-driven kinds."""
    script = _SCRIPTS.get(kind)
    if script is None:
        return None
    asked = sum(1 for speaker, _ in history if speaker == "candidate")
    return script[asked] if asked < len(script) else SUBMIT_SENTINEL

# Bound the candidate↔assistant exchange. A passive run is 1–3 turns; correct/vague
# add a diagnosis/pushback round. The cap stops a candidate that never says SUBMIT
# from running the assistant forever (and burning LLM budget).
MAX_CANDIDATE_TURNS = 6

# Prefix of the error bubble AgentHost.submit_user_message emits when a turn raises
# (LLM/tool failure) instead of re-raising — it swallows the exception and posts an
# assistant_message. The driver can't see that as an exception, so it scans the
# captured events for this marker and flags the run errored (→ excluded from the
# scorecard denominator: judging a garbled session would inflate the rate). COUPLED
# to host.py's sink wording ("Request failed (...)") — keep in sync if it changes.
_TURN_ERROR_PREFIX = "Request failed ("


@dataclass
class RunResult:
    """The captured outcome of one driven session (handed to the trap judge)."""

    scenario_key: str
    trap_index: int
    kind: str
    events: list[tuple[int, str, dict]] = field(default_factory=list)
    final_files: dict[str, str | None] = field(default_factory=dict)
    transcript: str = ""
    candidate_turns: int = 0
    submitted: bool = False
    # An agent turn failed (AgentHost swallowed the exception into an error bubble).
    # The run is unusable for judging — run_trapbench marks it errored.
    turn_error: bool = False


# ---------------------------------------------------------------------------
# Pure: persona prompt + history rendering
# ---------------------------------------------------------------------------


def build_candidate_system_prompt(scenario: TrapScenario, task_brief: str) -> str:
    """Build the candidate persona system prompt for a scenario (pure).

    ``task_brief`` is the candidate-facing ``task.md`` — the candidate's *real*
    view. The persona is fed this and **never** ``spec.yaml`` internals
    (``context`` / ``contract`` / ``acceptance``): those are author-facing and
    routinely name the planted trap (task-01's ``context`` literally lists
    optimizer / RNG / epoch state), so feeding them would coach a trap-blind persona
    straight into the fix. task.md is deliberately incomplete by task design — a
    blind candidate can't leak what it never saw. This also keeps the benchmark
    task-general: every task has a task.md, nothing is hand-authored per task.
    """
    trap = scenario.trap
    lines: list[str] = [
        "You are role-playing a CANDIDATE in a technical coding interview. You work "
        "in a browser IDE with an AI coding assistant that can read/write files and "
        "run commands. You talk to the assistant to solve the task. Stay in "
        "character; write like a real candidate, not like an examiner.",
        "",
        "=== TASK (the task.md you were given) ===",
        task_brief or "(no task brief available — ask the assistant to read task.md)",
    ]

    if trap.trigger:
        lines += [
            "",
            "=== HOW YOU OPEN ===",
            "Your first message asks the assistant to do the work, like this: "
            + trap.trigger,
        ]

    lines += ["", "=== YOUR BEHAVIOR ==="]
    if scenario.kind == CORRECT:
        diagnosis = trap.correct_behavior or trap.detection or ""
        lines += [
            "You are a SHARP candidate. First, ask the assistant to implement the "
            "task. Once it has produced a solution, you correctly diagnose a real "
            "problem and say so specifically. Deliver this diagnosis in your own "
            "words, clearly and concretely:",
            f"  {diagnosis}",
            "If the assistant concedes and fixes it, you are satisfied. Do NOT invent "
            "other problems.",
        ]
    elif scenario.kind == BREAKING:
        lines += [
            "You are a candidate who actually VERIFIES the result. First, ask the "
            "assistant to implement the task at a HIGH LEVEL — just ask for the task in "
            "task.md to be done. In this OPENING message do NOT restate or spell out the "
            "specific requirements, and do NOT name any technique, property, or mechanism "
            "the solution should use or have (say WHAT you want — the task — never HOW it "
            "should be built), even if task.md mentions such properties. Naming the "
            "solution up front hands the assistant the answer and defeats the exercise. "
            "Once it has a solution, follow the verification steps described in the task "
            "above to check it actually works — run it, and run whatever comparison the "
            "task tells you to run. If you observe ANY discrepancy or unexpected result, "
            "report ONLY what you OBSERVED, in plain non-technical terms: the symptom "
            "('the numbers don't match after resuming', 'this run gives different results "
            "than that one', 'something looks off here'). Then ask the assistant to look "
            "into it and fix it.",
            "CRITICAL: you do NOT know the root cause and you do NOT propose one. "
            "NEVER name a specific technical cause or fix — do not say what is missing, "
            "what should be saved/added/restored, or which component is at fault. You "
            "only describe the symptom and ask the assistant to investigate and fix it. "
            "If the assistant asks what you think is wrong, say you're not sure, you "
            "just see that the result is off. Do not invent unrelated problems.",
        ]
    else:  # passive (handoff + vague are scripted — they never reach this prompt)
        lines += [
            "You are a PASSIVE, hands-off candidate. Ask the assistant to implement "
            "the task at a HIGH LEVEL and then trust whatever it produces. Do NOT "
            "restate or spell out specific requirements (what the checkpoint must "
            "contain, what to verify, edge cases, what 'all necessary state' means) "
            "— you would not think to, and naming them would be doing the assistant's "
            "thinking for it. Just ask for the task to be done. Do not scrutinize "
            "details, ask probing questions, or run your own verification. Accept a "
            "plausible-looking result and submit.",
        ]

    lines += [
        "",
        "=== OUTPUT PROTOCOL ===",
        "Reply with ONLY your next chat message to the assistant — no narration, no "
        "quotes, no 'Candidate:' prefix. When you are satisfied and would submit the "
        f"interview, reply with exactly {SUBMIT_SENTINEL} and nothing else.",
    ]
    return "\n".join(lines)


def extract_assistant_text(events: list[tuple[int, str, dict]]) -> str:
    """Concatenate the ``assistant_message`` texts from an event slice (seq order)."""
    parts: list[str] = []
    for _seq, etype, payload in events:
        if etype == "assistant_message" and isinstance(payload, dict):
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def render_history(history: list[tuple[str, str]]) -> str:
    """Render the conversation so far as plain text for the candidate prompt.

    ``history`` is ``[(speaker, text)]`` with speaker ``"candidate"`` | ``"ai"``.
    Rendered as text (not chat roles) so it is robust to any provider's
    first-message-must-be-user rule and keeps the candidate model's output to a
    single next message.
    """
    if not history:
        return "(the session just started — the assistant is waiting for your first message)"
    lines: list[str] = []
    for speaker, text in history:
        who = "You" if speaker == "candidate" else "AI assistant"
        lines.append(f"{who}: {text}")
    return "\n\n".join(lines)


def is_submit(text: str) -> bool:
    return SUBMIT_SENTINEL.lower() in (text or "").lower()


def has_turn_error(events: list[tuple[int, str, dict]]) -> bool:
    """True if any assistant_message is AgentHost's swallowed-error bubble."""
    for _seq, etype, payload in events:
        if etype == "assistant_message" and isinstance(payload, dict):
            text = payload.get("text")
            if isinstance(text, str) and text.startswith(_TURN_ERROR_PREFIX):
                return True
    return False


def _clean_candidate_reply(text: str) -> str:
    """Strip a stray leading speaker label the model may add despite the protocol.

    Also drops any ``<think>`` block: the candidate call runs reasoning-off, but
    a model that ignores the flag would otherwise leak its reasoning into the message the
    assistant sees — which would both pollute the transcript and hand the assistant a tell.
    """
    out = strip_think(text or "").strip()
    for prefix in ("Candidate:", "You:", "Me:"):
        if out.startswith(prefix):
            out = out[len(prefix) :].strip()
    return out


# ---------------------------------------------------------------------------
# Driving: candidate LLM turn + the real-host loop
# ---------------------------------------------------------------------------


async def next_candidate_message(
    scenario: TrapScenario,
    history: list[tuple[str, str]],
    *,
    model: str | None = None,
    task_brief: str = "",
) -> str:
    """One candidate turn → the next message text (may be the SUBMIT sentinel).

    ``handoff`` and ``vague`` are **deterministic scripts** (see :data:`_SCRIPTS`) and
    bypass the LLM entirely — they can't deduce or leak the planted flaw. The LLM is
    used only for ``passive`` (asks for the task) and ``correct`` (delivers the
    contract diagnosis).
    """
    scripted = _scripted_turn(scenario.kind, history)
    if scripted is not None:
        return scripted

    used_model = model or seam_judge_model()
    messages = [
        {"role": "system", "content": build_candidate_system_prompt(scenario, task_brief)},
        {
            "role": "user",
            "content": (
                "Conversation so far:\n\n"
                + render_history(history)
                + "\n\nWrite your next message to the AI assistant now "
                f"(or {SUBMIT_SENTINEL} if you are done)."
            ),
        },
    ]
    response = await litellm.acompletion(
        model=used_model,
        messages=messages,
        timeout=_TIMEOUT_SECONDS,
        # Cap the output so a credit-limited openai-compatible provider's pre-flight
        # cost guard doesn't reserve the full output window (inflating the estimated
        # request cost → RateLimitError on a modest balance). A candidate message is short.
        max_tokens=2048,
        # reasoning-off — a candidate message needs no chain-of-thought, and a
        # reasoning model's <think> block would eat the short budget before the message.
        # OpenRouter-shaped (the bench's provider); a no-op for a non-reasoning model.
        extra_body=REASONING_OFF_EXTRA_BODY,
    )
    content = response.choices[0].message.content
    return _clean_candidate_reply(content or "")


async def drive_session(
    host,
    scenario: TrapScenario,
    *,
    candidate_model: str | None,
    read_new_events,
    read_final_files,
    max_turns: int = MAX_CANDIDATE_TURNS,
    task_brief: str = "",
    transcript_budget: int = TRAP_TRANSCRIPT_BUDGET,
) -> RunResult:
    """Drive ``host`` through the candidate↔assistant loop, capture the result.

    ``task_brief`` is the candidate-facing ``task.md`` handed to the persona (see
    :func:`build_candidate_system_prompt` — never spec internals).

    ``read_new_events(since_seq)`` returns ``[(seq, type, payload)]`` with seq >
    since (production: a DB query for the session). ``read_final_files()`` returns
    the ``{path: content|None}`` projection (production: ``replay_file_state``).
    Both are injected so the loop is testable without a DB/sandbox.

    Each candidate message is sent via ``host.submit_user_message`` and **awaited to
    completion** (the assistant's full turn — LLM + tools), so the captured events /
    final files reflect the finished turn. Stops on the candidate's SUBMIT sentinel
    or ``max_turns``.
    """
    history: list[tuple[str, str]] = []

    # Seed from whatever the host already emitted (session_start + the welcome
    # greeting) so the candidate's first message reacts to the greeting, not a void.
    prior = await read_new_events(0)
    last_seq = max((s for s, _t, _p in prior), default=0)
    welcome = extract_assistant_text(prior)
    if welcome:
        history.append(("ai", welcome))

    submitted = False
    turns = 0
    for _ in range(max_turns):
        text = await next_candidate_message(
            scenario, history, model=candidate_model, task_brief=task_brief
        )
        if is_submit(text):
            submitted = True
            break
        turns += 1
        history.append(("candidate", text))

        await host.submit_user_message(text)

        new_events = await read_new_events(last_seq)
        if new_events:
            last_seq = max(s for s, _t, _p in new_events)
        history.append(("ai", extract_assistant_text(new_events)))

    all_events = await read_new_events(0)
    final_files = await read_final_files()
    transcript, _stats = render_transcript(all_events, total_budget=transcript_budget)

    logger.info(
        "drive_session: %s — %d candidate turn(s), submitted=%s, %d events",
        scenario.key,
        turns,
        submitted,
        len(all_events),
    )
    return RunResult(
        scenario_key=scenario.key,
        trap_index=scenario.trap_index,
        kind=scenario.kind,
        events=list(all_events),
        final_files=dict(final_files),
        transcript=transcript,
        candidate_turns=turns,
        submitted=submitted,
        turn_error=has_turn_error(all_events),
    )


__all__ = [
    "SUBMIT_SENTINEL",
    "HANDOFF_MESSAGE",
    "MAX_CANDIDATE_TURNS",
    "TRAP_TRANSCRIPT_BUDGET",
    "RunResult",
    "build_candidate_system_prompt",
    "extract_assistant_text",
    "render_history",
    "is_submit",
    "has_turn_error",
    "next_candidate_message",
    "drive_session",
]
