"""Aggregate verdicts into a scorecard.

Turns a flat list of per-(scenario, rep) :class:`Verdict`s into the numbers the
author reads instead of the raw sessions, plus a markdown report. The aggregate
metrics are deliberately blunt — they are a triage signal for "where is the prompt
failing", not a benchmark:

* **trap recall** (micro) — caught triggered pitfalls / triggered pitfalls.
* **hallucinated traps** — total false-positive pitfall assertions (should be 0,
  watched hardest on ``clean`` / ``near_empty``).
* **seq validity** — cited event_seqs that actually exist / cited.
* **section ok rate** — reports with the 3 required H2 sections in order.
* **near-empty honesty** — honest near-empty reports / near-empty scenarios.
* **mean faithfulness / actionability** — the LLM judge's 1–5 scales.

With ``reps > 1`` the per-scenario rows are repeated, so the means and rates absorb
generation variance.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from scripts.assessment_eval.judge import Verdict


@dataclass
class Scorecard:
    runs: int
    trap_recall: float | None  # None when no scenario triggered any pitfall
    triggered_total: int
    caught_total: int
    hallucinated_total: int
    seq_validity: float | None  # None when nothing was cited
    section_ok_rate: float
    near_empty_honesty: float | None  # None when no near-empty scenario
    mean_faithfulness: float
    mean_actionability: float


def aggregate(verdicts: list[Verdict]) -> Scorecard:
    n = len(verdicts)
    triggered = sum(v.triggered_total for v in verdicts)
    caught = sum(v.caught_triggered for v in verdicts)
    hallucinated = sum(len(v.judge.hallucinated_pitfalls) for v in verdicts)

    cited = sum(v.programmatic.seqs_cited for v in verdicts)
    valid = sum(v.programmatic.seqs_valid for v in verdicts)

    section_ok = sum(1 for v in verdicts if v.programmatic.section_ok)
    near_empty = [v for v in verdicts if v.programmatic.near_empty_honest is not None]
    honest = sum(1 for v in near_empty if v.programmatic.near_empty_honest)

    return Scorecard(
        runs=n,
        trap_recall=(caught / triggered) if triggered else None,
        triggered_total=triggered,
        caught_total=caught,
        hallucinated_total=hallucinated,
        seq_validity=(valid / cited) if cited else None,
        section_ok_rate=(section_ok / n) if n else 0.0,
        near_empty_honesty=(honest / len(near_empty)) if near_empty else None,
        mean_faithfulness=mean(v.judge.faithfulness for v in verdicts) if n else 0.0,
        mean_actionability=mean(v.judge.actionability for v in verdicts) if n else 0.0,
    )


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def render_markdown(
    verdicts: list[Verdict], card: Scorecard, *, model: str, judge_model: str
) -> str:
    lines: list[str] = [
        "# Assessment prompt calibration scorecard",
        "",
        f"- generator model: `{model}`",
        f"- judge model: `{judge_model}`",
        f"- runs: {card.runs}",
        "",
        "## Aggregate",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| trap recall (micro) | {_pct(card.trap_recall)} ({card.caught_total}/{card.triggered_total}) |",
        f"| hallucinated traps | {card.hallucinated_total} |",
        f"| seq validity | {_pct(card.seq_validity)} |",
        f"| section ok rate | {_pct(card.section_ok_rate)} |",
        f"| near-empty honesty | {_pct(card.near_empty_honesty)} |",
        f"| mean faithfulness (1-5) | {card.mean_faithfulness:.2f} |",
        f"| mean actionability (1-5) | {card.mean_actionability:.2f} |",
        "",
        "## Per scenario",
        "",
        "| task | scenario | recall | halluc. | seq | sect. | honest | faith | act. |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for v in verdicts:
        recall = (
            f"{v.caught_triggered}/{v.triggered_total}" if v.triggered_total else "—"
        )
        prog = v.programmatic
        seq = f"{prog.seqs_valid}/{prog.seqs_cited}" if prog.seqs_cited else "—"
        honest = (
            "—"
            if prog.near_empty_honest is None
            else ("yes" if prog.near_empty_honest else "NO")
        )
        lines.append(
            f"| {v.task_id} | {v.key} | {recall} | "
            f"{len(v.judge.hallucinated_pitfalls)} | {seq} | "
            f"{'ok' if prog.section_ok else 'NO'} | {honest} | "
            f"{v.judge.faithfulness} | {v.judge.actionability} |"
        )

    # Surface the worst offenders (low faithfulness or any programmatic issue).
    flagged = [
        v
        for v in verdicts
        if v.judge.faithfulness <= 3
        or v.judge.hallucinated_pitfalls
        or v.programmatic.issues
    ]
    if flagged:
        lines += ["", "## Flagged (low faithfulness / hallucination / format)", ""]
        for v in flagged:
            lines.append(f"### {v.task_id} / {v.key}")
            for issue in v.programmatic.issues:
                lines.append(f"- programmatic: {issue}")
            if v.judge.hallucinated_pitfalls:
                lines.append(f"- hallucinated pitfalls: {v.judge.hallucinated_pitfalls}")
            if v.judge.rationale:
                lines.append(f"- judge: {v.judge.rationale}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["Scorecard", "aggregate", "render_markdown"]
