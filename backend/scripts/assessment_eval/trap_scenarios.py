"""Trap-firing scenario matrix (adds the natural breaking scenario).

The trap-control benchmark measures one thing per planted trap: does the interview
AI follow the **controlled-trap contract** (`docs/task-design/format.md` §4.5/§5)?
That contract is a *conditional behavioral* one, so a single boolean "did it fire"
is not enough — each trap is probed in up to five scenarios:

* ``handoff`` — the candidate hands the task off with **zero guidance** ("read
  task.md and implement it"), then submits. No requirements restated, no pushback —
  the purest baseline, matching the most common real behaviour ("just do the task").
  The trap should **fire** and **hold** (success = flaw present + not volunteered).
  Not LLM-driven: the driver sends one canned message (see ``trap_driver``), so
  nothing the candidate says can coach the assistant.
* ``passive`` — the candidate is naive / hands-off ("build it, looks ok, submit").
  The trap should **fire**: the AI commits the planted ``flawed_behavior`` and does
  not volunteer it.
* ``correct`` — the candidate **correctly diagnoses** the issue (the contract's
  ``correct_behavior`` precondition). The AI should **concede and fix** it.
* ``breaking`` — the candidate **actually verifies** the result by following the
  task's own verification steps, observes a discrepancy, and pushes to fix it while
  reporting **only the symptom** (never the root cause). This is the *natural breaking
  path* the reversal exposed: the way the real
  sessions actually blew the trap was implement → run continuous-vs-resume → see the
  loss diverge → "fix it" — a path the content-free ``vague`` script never walks. The
  AI should **hold** (success = flaw present + not volunteered): fix only what the
  candidate's symptom directly implies, without volunteering the planted diagnosis.
* ``vague`` — the candidate pushes back **vaguely / incorrectly** ("just redo it",
  "are you sure?"). The AI should **hold** the flaw plausibly (the ``defense``).

This module is the task-general matrix builder, mirroring
``personas.derive_scenarios`` for the assessment harness: no per-task code, it reads
each task's parsed ``spec.yaml`` (the :class:`Pitfall` contract) and emits the
scenarios it can drive.

**Which scenarios a trap gets:**

* ``handoff`` + ``passive`` + ``vague`` for **every** pitfall — none needs a contract
  field: handoff is a fixed zero-guidance ask, the passive opening is the trap's
  natural precondition, and a vague-pushback candidate just pushes vaguely.
* ``correct`` **only when** ``correct_behavior`` is set — scripting a *correct*
  candidate requires the contract to say what a correct diagnosis is.
* ``breaking`` **only when** ``check`` is set — the persona verifies by *running* the
  task's check, so a trap with no runnable verification has no breaking path. The
  persona itself is task-general (it follows ``task.md``'s verification steps, never
  the author-facing ``check`` text — see ``trap_driver``); ``check`` being present is
  just the structural signal that the trap is observable by running something.

A legacy pitfall (: only ``description`` / ``detection`` / ``severity``) is
therefore handoff+passive+vague only. For task-01, once the trap contracts are
populated every trap with a ``check`` gets ``breaking``, and traps with a
``correct_behavior`` also get ``correct``.
"""

from __future__ import annotations

from dataclasses import dataclass

from vibony_backend.assessment.inputs import Pitfall, TaskSpec

HANDOFF = "handoff"
PASSIVE = "passive"
CORRECT = "correct"
BREAKING = "breaking"
VAGUE = "vague"


@dataclass(frozen=True)
class TrapScenario:
    """One (trap, scenario-kind) cell of the benchmark matrix.

    Carries the parsed :class:`Pitfall` (the contract the driver scripts the
    candidate from, and the judge anchors detection to) plus its index into the
    spec's ``expected_ai_pitfalls`` — the index is the trap's stable identity
    across the run, the scorecard, and any deterministic check registered for it.
    """

    trap_index: int
    kind: str  # handoff | passive | correct | breaking | vague
    trap: Pitfall

    @property
    def key(self) -> str:
        """Stable, filename-safe identifier — ``trap{i}__{kind}``."""
        return f"trap{self.trap_index}__{self.kind}"


def derive_trap_scenarios(spec: TaskSpec) -> list[TrapScenario]:
    """Build the benchmark matrix off ``spec.expected_ai_pitfalls`` (task-general).

    Order is grouped by trap (handoff, passive, then correct/breaking when scriptable,
    then vague) so the scorecard reads top-to-bottom per trap. A spec with no pitfalls
    yields an empty matrix — there is nothing to measure.
    """
    scenarios: list[TrapScenario] = []
    for i, p in enumerate(spec.expected_ai_pitfalls):
        scenarios.append(TrapScenario(i, HANDOFF, p))
        scenarios.append(TrapScenario(i, PASSIVE, p))
        if p.correct_behavior:
            scenarios.append(TrapScenario(i, CORRECT, p))
        if p.check:
            scenarios.append(TrapScenario(i, BREAKING, p))
        scenarios.append(TrapScenario(i, VAGUE, p))
    return scenarios


# Only CORRECT drives a trap-SPECIFIC transcript: the candidate names that trap's
# ``correct_behavior``, so the session is meaningful for that one trap. Every other kind
# drives a trap-AGNOSTIC transcript — the candidate names nothing specific (handoff/vague
# are scripted, passive just trusts a plausible result, breaking pushes only the symptom
# it observed). Since the assistant plants ALL traps in EVERY session, one agnostic
# transcript can be scored against every trap contract — so N same-kind agnostic scenarios
# collapse into ONE driven session judged N times (the sessions are the expensive part;
# this roughly halves them for a multi-trap task). Per-(trap, kind) cells are unchanged,
# so per-cell Wilson CIs stay valid: each rep is still its own session.
TRAP_SPECIFIC_KINDS = frozenset({CORRECT})


@dataclass(frozen=True)
class SessionPlan:
    """One driven session + the trap scenarios scored from its single transcript.

    ``drive`` is the scenario whose persona drives the session; ``judge`` are the
    scenarios scored against the resulting transcript. A trap-specific kind yields
    ``judge == (drive,)`` (one trap per session, unchanged from the per-scenario sweep).
    A trap-agnostic kind yields every selected trap of that kind — one session, judged
    once per trap.
    """

    kind: str
    drive: TrapScenario
    judge: tuple[TrapScenario, ...]


def plan_sessions(scenarios: list[TrapScenario]) -> list[SessionPlan]:
    """Group scenarios into sessions: trap-agnostic same-kind scenarios share ONE driven
    session (scored per trap); a trap-specific kind (CORRECT) stays one session per trap.

    Order-preserving and filter-respecting — it groups only the scenarios handed to it, so
    an upstream ``--traps`` / ``--kinds`` filter is honoured (a single selected trap yields
    no collapse). The driving scenario of an agnostic group is its first member: any works,
    the persona is trap-agnostic; only its opening rides ``trap.trigger``, a task-level ask.
    """
    plans: list[SessionPlan] = []
    groups: dict[str, list[TrapScenario]] = {}
    group_order: list[str] = []
    for s in scenarios:
        if s.kind in TRAP_SPECIFIC_KINDS:
            plans.append(SessionPlan(kind=s.kind, drive=s, judge=(s,)))
            continue
        if s.kind not in groups:
            groups[s.kind] = []
            group_order.append(s.kind)
        groups[s.kind].append(s)
    for kind in group_order:
        members = groups[kind]
        plans.append(SessionPlan(kind=kind, drive=members[0], judge=tuple(members)))
    return plans


__all__ = [
    "HANDOFF",
    "PASSIVE",
    "CORRECT",
    "BREAKING",
    "VAGUE",
    "TRAP_SPECIFIC_KINDS",
    "TrapScenario",
    "SessionPlan",
    "derive_trap_scenarios",
    "plan_sessions",
]
