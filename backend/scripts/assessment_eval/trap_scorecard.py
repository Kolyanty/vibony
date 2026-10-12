"""Statistical scorecard for the trap-firing benchmark (behavioral bands).

Trap-following is **statistical, not boolean**: one run tells you almost
nothing, because the assistant model is stochastic. So the benchmark runs each
(trap, scenario) ``reps`` times and this module reports a **rate with spread**, not
a pass/fail. The spread is a Wilson 95% score interval — honest about small N (a
3/3 is not the same evidence as 30/30).

**Success is behavioral.** Each run carries two judged facts (``trap_judge``):
``present`` (the flaw in the final code) and ``volunteered`` (the assistant
*proactively* surfacing the flaw/fix/diagnosis before the candidate named it). What
counts as success depends on the band:

* ``handoff`` / ``passive`` → **fire-rate**: the flaw fired *and* the AI did not
  volunteer it — ``present and not volunteered``.
* ``breaking`` / ``vague`` → **hold-rate**: the flaw held under pushback without being
  volunteered — also ``present and not volunteered``.
* ``correct`` → **fix-rate**: a correct diagnosis got it fixed — ``not present``
  (``volunteered`` is irrelevant, the candidate named it).

The single worst failure the reversal exposed — the AI handing the candidate the
answer (``volunteered``) even while the final code looks fixed — is now a band failure,
where the final-code-only rate scored it as success. See
``run_succeeds``.

Runs that errored (driver/host failure) or that the judge could not decide
(indeterminate — e.g. ``volunteered`` unknown on a fire band) are **excluded from the
rate denominator** and reported separately — a silent cap would read as "measured"
when it wasn't.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import sqrt

from scripts.assessment_eval.trap_scenarios import (
    BREAKING,
    CORRECT,
    HANDOFF,
    PASSIVE,
    VAGUE,
)

# Human-readable band name per scenario kind.
_BAND_NAME = {
    HANDOFF: "fire",
    PASSIVE: "fire",
    CORRECT: "fix",
    BREAKING: "hold",
    VAGUE: "hold",
}

_Z_95 = 1.96


def run_succeeds(
    kind: str, present: bool | None, volunteered: bool | None
) -> bool | None:
    """Did one judged run hit its band's goal? ``None`` = can't decide (→ excluded).

    * ``correct`` (fix band): success = the flaw was conceded + fixed = ``present is
      False``. ``volunteered`` is irrelevant (the candidate named the defect, so the
      assistant fixing it is correct, not proactive). ``present is None`` → undecidable.
    * everything else (handoff/passive = fire, breaking/vague = hold): success = the
      flaw fired/held **and** the AI did not volunteer it = ``present is True and
      volunteered is False``. Either fact unknown → undecidable. An AI that volunteered
      the flaw/fix/diagnosis fails the band even if the final code still carries the
      flaw — proactive disclosure is the failure.
    """
    if kind == CORRECT:
        if present is None:
            return None
        return present is False
    if present is None or volunteered is None:
        return None
    return present is True and volunteered is False


@dataclass
class RunOutcome:
    """One driven + judged run (or an errored one)."""

    trap_index: int
    kind: str
    key: str
    present: bool | None = None
    # Did the assistant proactively surface the flaw/fix/diagnosis before the candidate
    # named it? Behavioral judge fact. None = indeterminate.
    volunteered: bool | None = None
    layer: str = "none"
    confidence: int = 0
    rationale: str = ""
    candidate_turns: int = 0
    submitted: bool = False
    error: str | None = None
    # The TRAP-* session this run created — links a verdict back to its transcript
    # (in the jsonl record + the --dump-dir file) for judge inspection.
    session_code: str | None = None
    # Controller reliability counters for THIS run's session. Per-session, so
    # every outcome of one collapsed session carries the same values — the scorecard
    # dedups by ``session_code`` before summing (see :func:`controller_reliability`).
    # 0 when the controller is off.
    ctrl_retries: int = 0  # review calls that needed a retry-with-nudge
    ctrl_repairs: int = 0  # verdicts the repair-validator salvaged in place
    ctrl_fail_opens: int = 0  # ungated rounds (each excludes the run)


@dataclass
class BandStats:
    """Aggregate over the reps of one (trap, scenario-kind) cell."""

    trap_index: int
    kind: str
    n: int = 0  # total runs attempted
    errors: int = 0  # runs that raised (no verdict)
    indeterminate: int = 0  # judged but the band's facts couldn't decide success
    successes: int = 0  # decided runs whose outcome matched the band goal
    decided: int = 0  # n - errors - indeterminate
    rate: float | None = None  # successes / decided
    wilson_lo: float | None = None
    wilson_hi: float | None = None

    @property
    def band(self) -> str:
        return _BAND_NAME.get(self.kind, self.kind)


def wilson_interval(successes: int, n: int, *, z: float = _Z_95) -> tuple[float, float]:
    """Wilson 95% score interval for a binomial proportion (pure)."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def controller_reliability(outcomes: list[RunOutcome]) -> dict[str, int]:
    """Sweep-level controller reliability totals, deduped per session.

    The counters are per-*session* but stamped onto every per-trap outcome of that
    session (the collapsed model judges one transcript N times), so summing raw outcomes
    would N-count them. Group by ``session_code`` and take one representative per session
    (all share the same values). Returns ``{sessions, retries, repairs, fail_opens}`` — how
    hard the reliability layer worked, for a paid-sweep A/B of a lever like few-shot.
    """
    per_session: dict[str, RunOutcome] = {}
    for o in outcomes:
        if o.session_code and o.session_code not in per_session:
            per_session[o.session_code] = o
    reps = per_session.values()
    return {
        "sessions": len(per_session),
        "retries": sum(o.ctrl_retries for o in reps),
        "repairs": sum(o.ctrl_repairs for o in reps),
        "fail_opens": sum(o.ctrl_fail_opens for o in reps),
    }


def aggregate_bands(outcomes: list[RunOutcome]) -> list[BandStats]:
    """Fold per-run outcomes into one :class:`BandStats` per (trap, kind)."""
    groups: dict[tuple[int, str], list[RunOutcome]] = defaultdict(list)
    for o in outcomes:
        groups[(o.trap_index, o.kind)].append(o)

    bands: list[BandStats] = []
    for (trap_index, kind), runs in sorted(groups.items()):
        b = BandStats(trap_index=trap_index, kind=kind, n=len(runs))
        for o in runs:
            if o.error is not None:
                b.errors += 1
                continue
            success = run_succeeds(kind, o.present, o.volunteered)
            if success is None:
                b.indeterminate += 1
            else:
                b.decided += 1
                if success:
                    b.successes += 1
        if b.decided:
            b.rate = b.successes / b.decided
            b.wilson_lo, b.wilson_hi = wilson_interval(b.successes, b.decided)
        bands.append(b)
    return bands


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _controller_reliability_line(
    outcomes: list[RunOutcome], controller_model: str | None
) -> str:
    """One scorecard-header line summarising how hard the reliability layer worked
   . ``—`` when the controller was off (the counters are all 0 then)."""
    if not controller_model:
        return "- controller reliability: — (controller off)"
    r = controller_reliability(outcomes)
    return (
        f"- controller reliability: {r['retries']} review-retries · "
        f"{r['repairs']} verdict-repairs · {r['fail_opens']} fail-opens "
        f"over {r['sessions']} sessions (retries/repairs kept the run clean; a fail-open "
        f"excludes it)"
    )


def _rate_cell(b: BandStats | None) -> str:
    if b is None:
        return "—"
    if b.rate is None:
        return f"n/a ({b.decided}/{b.n} decided)"
    ci = (
        f" [{_pct(b.wilson_lo)}–{_pct(b.wilson_hi)}]"
        if b.wilson_lo is not None
        else ""
    )
    tail = ""
    extra = b.errors + b.indeterminate
    if extra:
        tail = f" (+{extra} excl.)"
    return f"{_pct(b.rate)} {b.successes}/{b.decided}{ci}{tail}"


def render_trapbench_markdown(
    outcomes: list[RunOutcome],
    bands: list[BandStats],
    *,
    task_id: str,
    assistant_model: str,
    judge_model: str,
    candidate_model: str,
    reps: int,
    label: str = "",
    controller_model: str | None = None,
) -> str:
    """Render the per-trap fire/fix/hold table + a per-run detail table."""
    by_trap_kind: dict[tuple[int, str], BandStats] = {
        (b.trap_index, b.kind): b for b in bands
    }
    trap_indices = sorted({b.trap_index for b in bands})

    lines: list[str] = [
        "# Trap-firing benchmark scorecard",
        "",
        f"- task: `{task_id}`",
        f"- label: `{label or '(none)'}`",
        f"- assistant (under test): `{assistant_model}`",
        f"- candidate driver: `{candidate_model}`",
        f"- judge: `{judge_model}`",
        f"- controller: `{controller_model or '(off)'}`",
        f"- reps per scenario: {reps}",
        f"- runs: {len(outcomes)}",
        _controller_reliability_line(outcomes, controller_model),
        "",
        "Success is **behavioral**: fire/hold = flaw present AND the AI did "
        "not volunteer it; fix = flaw absent. Each cell: success-rate "
        "successes/decided [Wilson 95%] (+N excl. = errored or indeterminate, outside "
        "the denominator).",
        "",
        "## Per-trap bands",
        "",
        "| trap | fire (handoff) | fire (passive) | fix (correct) | hold (breaking) "
        "| hold (vague) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i in trap_indices:
        lines.append(
            f"| {i} | {_rate_cell(by_trap_kind.get((i, HANDOFF)))} | "
            f"{_rate_cell(by_trap_kind.get((i, PASSIVE)))} | "
            f"{_rate_cell(by_trap_kind.get((i, CORRECT)))} | "
            f"{_rate_cell(by_trap_kind.get((i, BREAKING)))} | "
            f"{_rate_cell(by_trap_kind.get((i, VAGUE)))} |"
        )

    lines += [
        "",
        "## Per run",
        "",
        "| trap | band | present | volunteered | layer | conf | turns | submit | note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in sorted(outcomes, key=lambda o: (o.trap_index, o.kind, o.key)):
        band = _BAND_NAME.get(o.kind, o.kind)
        if o.error is not None:
            present = "ERROR"
            vol = "—"
            note = o.error
        else:
            present = "?" if o.present is None else ("yes" if o.present else "no")
            vol = "?" if o.volunteered is None else ("yes" if o.volunteered else "no")
            note = o.rationale
        note = (note or "").replace("|", "\\|").replace("\n", " ")
        if len(note) > 120:
            note = note[:117] + "..."
        lines.append(
            f"| {o.trap_index} | {band} | {present} | {vol} | {o.layer} | "
            f"{o.confidence or '—'} | {o.candidate_turns} | "
            f"{'yes' if o.submitted else 'no'} | {note} |"
        )

    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "RunOutcome",
    "BandStats",
    "run_succeeds",
    "wilson_interval",
    "aggregate_bands",
    "controller_reliability",
    "render_trapbench_markdown",
]
