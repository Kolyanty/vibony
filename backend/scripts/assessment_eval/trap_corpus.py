"""Owner-supplied calibration fixtures for the behavioral trap judge.

No real session transcripts are distributed in the public snapshot. To freeze
consented local sessions, provide a JSON mapping of session codes to verdict,
note and per-trap labels via --labels. Calibration invokes a paid judge model;
run it explicitly. Synthetic tests validate the loader without claiming quality.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CORPUS_ROOT = Path(__file__).parent / "trap_corpus"


@dataclass(frozen=True)
class TrapLabel:
    """Hand-authored ground truth for one trap in one frozen session."""

    volunteered: bool  # did the AI proactively surface this flaw/fix/diagnosis?
    present: bool | None = None  # flaw in the final code (None = not asserted)
    note: str = ""


@dataclass(frozen=True)
class FrozenSession:
    """A real session frozen for judge calibration (transcript + code + labels)."""

    code: str
    task_id: str
    opening: str  # the candidate's first message (context for a reader)
    verdict: str  # session-level ground truth: blown | partially_blown | held
    note: str
    transcript: str  # rendered (render_transcript) — what the judge reads
    final_files: dict[str, str | None]  # the {path: content} projection
    labels: dict[int, TrapLabel]  # trap_index -> label


# ---------------------------------------------------------------------------
# Loading (pure — JSON only, no DB)
# ---------------------------------------------------------------------------


def corpus_dir(task_id: str) -> Path:
    return CORPUS_ROOT / task_id


def _frozen_from_json(data: dict) -> FrozenSession:
    labels = {
        int(idx): TrapLabel(
            volunteered=bool(lbl["volunteered"]),
            present=lbl.get("present"),
            note=lbl.get("note", ""),
        )
        for idx, lbl in (data.get("labels") or {}).items()
    }
    return FrozenSession(
        code=data["code"],
        task_id=data["task_id"],
        opening=data.get("opening", ""),
        verdict=data.get("verdict", ""),
        note=data.get("note", ""),
        transcript=data.get("transcript", ""),
        final_files=data.get("final_files", {}),
        labels=labels,
    )


def load_trap_corpus(task_id: str) -> list[FrozenSession]:
    """Load every frozen session for ``task_id`` (sorted by code). Empty if none."""
    d = corpus_dir(task_id)
    if not d.is_dir():
        return []
    sessions: list[FrozenSession] = []
    for path in sorted(d.glob("*.json")):
        sessions.append(_frozen_from_json(json.loads(path.read_text())))
    return sessions


# ---------------------------------------------------------------------------
# Calibration (author-run — needs the judge LLM)
# ---------------------------------------------------------------------------


@dataclass
class TrapCalibration:
    """The judge's verdict vs the label for one (session, trap)."""

    code: str
    trap_index: int
    expected_volunteered: bool
    judged_volunteered: bool | None
    expected_present: bool | None
    judged_present: bool | None
    layer: str
    rationale: str

    @property
    def volunteered_match(self) -> bool:
        return self.judged_volunteered == self.expected_volunteered

    @property
    def present_match(self) -> bool | None:
        """``None`` when the label doesn't assert ``present`` (not scored)."""
        if self.expected_present is None:
            return None
        return self.judged_present == self.expected_present


@dataclass
class CalibrationReport:
    rows: list[TrapCalibration] = field(default_factory=list)

    @property
    def volunteered_matches(self) -> int:
        return sum(1 for r in self.rows if r.volunteered_match)

    @property
    def present_scored(self) -> list[TrapCalibration]:
        return [r for r in self.rows if r.expected_present is not None]

    @property
    def present_matches(self) -> int:
        return sum(1 for r in self.present_scored if r.present_match)


async def calibrate(
    task_id: str,
    *,
    judge_model: str | None = None,
    allow_deterministic: bool = True,
    codes: list[str] | None = None,
    traps: list[int] | None = None,
) -> CalibrationReport:
    """Run the behavioral judge over the frozen corpus, compare to the labels.

    For each frozen session × each labelled trap, calls the real ``judge_trap``
    (deterministic layer included unless disabled) and records judged vs expected
    ``volunteered`` / ``present``. Author-run: makes one judge LLM call per labelled
    (session, trap). Raises ``SystemExit`` if the corpus or the task spec is missing.

    ``codes`` / ``traps`` restrict the run to those session codes / trap indices — a
    targeted re-run after a single label or trap-contract change. **Not for verifying a
    judge-prompt change**, which affects every call: a subset would hide a regression in
    the cells that already matched, so re-run the whole corpus for that.
    """
    from vibony_backend.assessment.inputs import (
        read_task_spec_from_disk,
        render_final_code,
    )

    from scripts.assessment_eval.trap_judge import judge_trap

    sessions = load_trap_corpus(task_id)
    if not sessions:
        raise SystemExit(f"no frozen corpus for {task_id} (run --freeze first)")
    if codes:
        wanted = set(codes)
        sessions = [s for s in sessions if s.code in wanted]
        if not sessions:
            raise SystemExit(f"no frozen sessions match --codes {sorted(wanted)}")
    spec = read_task_spec_from_disk(task_id)
    if spec is None or not spec.expected_ai_pitfalls:
        raise SystemExit(f"{task_id} has no spec.yaml pitfalls")

    trap_filter = set(traps) if traps is not None else None
    report = CalibrationReport()
    for sess in sessions:
        final_code, _ = render_final_code(sess.final_files)
        for trap_index, label in sorted(sess.labels.items()):
            if trap_filter is not None and trap_index not in trap_filter:
                continue
            if trap_index >= len(spec.expected_ai_pitfalls):
                logger.warning("calibrate: %s trap %d out of range", sess.code, trap_index)
                continue
            trap = spec.expected_ai_pitfalls[trap_index]
            judgment = await judge_trap(
                trap,
                trap_index,
                task_id=task_id,
                transcript=sess.transcript,
                final_code=final_code,
                final_files=sess.final_files,
                judge_model=judge_model,
                allow_deterministic=allow_deterministic,
            )
            report.rows.append(
                TrapCalibration(
                    code=sess.code,
                    trap_index=trap_index,
                    expected_volunteered=label.volunteered,
                    judged_volunteered=judgment.volunteered,
                    expected_present=label.present,
                    judged_present=judgment.present,
                    layer=judgment.layer,
                    rationale=judgment.rationale,
                )
            )
    return report


def render_calibration_markdown(report: CalibrationReport, *, judge_model: str) -> str:
    """Render the judge-vs-ground-truth confusion as markdown."""
    lines: list[str] = [
        "# Trap-judge calibration vs ground-truth sessions",
        "",
        f"- judge: `{judge_model}`",
        f"- volunteered match: {report.volunteered_matches}/{len(report.rows)}",
        f"- present match (scored only): {report.present_matches}/{len(report.present_scored)}",
        "",
        "A `✗` in the volunteered column is a calibration FAILURE — the behavioral "
        "judge disagreed with the hand-labelled ground truth.",
        "",
        "| session | trap | exp vol | judged vol | exp present | judged present | "
        "layer | match | rationale |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def _b(x: bool | None) -> str:
        return "—" if x is None else ("yes" if x else "no")

    for r in sorted(report.rows, key=lambda r: (r.code, r.trap_index)):
        vol_mark = "✓" if r.volunteered_match else "✗"
        pm = r.present_match
        present_mark = "—" if pm is None else ("✓" if pm else "✗")
        rationale = (r.rationale or "").replace("|", "\\|").replace("\n", " ")
        if len(rationale) > 100:
            rationale = rationale[:97] + "..."
        lines.append(
            f"| {r.code} | {r.trap_index} | {_b(r.expected_volunteered)} | "
            f"{_b(r.judged_volunteered)} | {_b(r.expected_present)} | "
            f"{_b(r.judged_present)} | {r.layer} | vol {vol_mark} / pres {present_mark} "
            f"| {rationale} |"
        )
    return "\n".join(lines).rstrip() + "\n"


async def _freeze_one(code: str, task_id: str, gt: dict) -> Path:
    """Pull one session from the DB, render it, write the frozen JSON (author-run)."""
    from sqlalchemy import select

    from vibony_backend.assessment.inputs import render_transcript
    from vibony_backend.db.engine import get_sessionmaker
    from vibony_backend.db.models import Event
    from vibony_backend.db.models import Session as DbSession
    from vibony_backend.sessions.event_store import replay_file_state

    from scripts.assessment_eval.trap_driver import TRAP_TRANSCRIPT_BUDGET

    sm = get_sessionmaker()
    async with sm() as db:
        rows = (
            await db.execute(
                select(Event.seq, Event.type, Event.payload)
                .join(DbSession, Event.session_id == DbSession.id)
                .where(DbSession.code == code)
                .order_by(Event.seq)
            )
        ).all()
    if not rows:
        raise SystemExit(f"session {code} has no events (is it in this DB?)")
    events = [(seq, etype, payload or {}) for seq, etype, payload in rows]
    # Freeze the WHOLE session at the trap-judge budget (not the 48k assessment
    # default) so a late disclosure isn't truncated out of the ground truth — the
    # corpus must hold exactly what the behavioral judge will read.
    transcript, _ = render_transcript(events, total_budget=TRAP_TRANSCRIPT_BUDGET)
    final_files = await replay_file_state(code)

    opening = next(
        (str(p.get("text", "")) for _s, t, p in events if t == "user_message"), ""
    )
    payload = {
        "code": code,
        "task_id": task_id,
        "opening": opening,
        "verdict": gt["verdict"],
        "note": gt["note"],
        "transcript": transcript,
        "final_files": final_files,
        "labels": {str(k): v for k, v in gt["labels"].items()},
    }
    out_dir = corpus_dir(task_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{code}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return out_path


async def _freeze_main(task_id: str, labels_path: Path) -> None:
    """Freeze every ground-truth session for ``task_id`` from the DB."""
    for code, gt in json.loads(labels_path.read_text()).items():
        path = await _freeze_one(code, task_id, gt)
        logger.info("froze %s -> %s", code, path)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import asyncio

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    parser = argparse.ArgumentParser(description="Trap-judge ground-truth corpus")
    parser.add_argument("--task", default="task-01-training-loop", help="task slug")
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="(author, needs the DB sessions) re-pull the ground-truth sessions and "
        "rewrite their frozen JSON",
    )
    parser.add_argument("--labels", type=Path, help="private JSON ground-truth mapping required by --freeze")
    parser.add_argument("--judge-model", default=None, help="judge model for --calibrate")
    parser.add_argument(
        "--no-deterministic",
        action="store_true",
        help="skip the deterministic present check during --calibrate (LLM only)",
    )
    parser.add_argument(
        "--codes",
        nargs="*",
        default=None,
        help="restrict calibration to these session codes (e.g. --codes LOCAL-0001). "
        "For a targeted re-run after a label/contract change — NOT for a judge-prompt "
        "change, which affects every call (re-run the whole corpus for that).",
    )
    parser.add_argument(
        "--traps",
        nargs="*",
        type=int,
        default=None,
        help="restrict calibration to these trap indices (e.g. --traps 0 3)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.freeze:
        if args.labels is None:
            parser.error("--freeze requires --labels PATH to owner-reviewed ground truth")
        asyncio.run(_freeze_main(args.task, args.labels))
        return

    # Default: calibrate the judge against the frozen corpus (author-run, judge LLM).
    import os

    judge_model = (
        args.judge_model
        or os.environ.get("VIBONY_JUDGE_MODEL")
        or "openrouter/moonshotai/kimi-k2.6"
    )

    async def _run() -> None:
        report = await calibrate(
            args.task,
            judge_model=judge_model,
            allow_deterministic=not args.no_deterministic,
            codes=args.codes,
            traps=args.traps,
        )
        print(render_calibration_markdown(report, judge_model=judge_model))

    asyncio.run(_run())


__all__ = [
    "CORPUS_ROOT",
    "TrapLabel",
    "FrozenSession",
    "TrapCalibration",
    "CalibrationReport",
    "corpus_dir",
    "load_trap_corpus",
    "calibrate",
    "render_calibration_markdown",
    "main",
]


if __name__ == "__main__":
    main()
