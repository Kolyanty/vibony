"""Calibration runner CLI (c).

Two modes:

* default — replay the **frozen** corpus through the real ``generate_assessment``
  (N reps for variance), grade each run, write a markdown scorecard. Cheap; this is
  the loop you run after every ``prompts.SYSTEM_PROMPT`` edit.
* ``--regen-corpus`` — (re)author the scenario matrix for spec-bearing tasks with
  the LLM author and freeze it. Run once per task / when scenarios change.

Examples (from ``backend/``, with OPENROUTER creds in .env):

    poetry run python -m scripts.assessment_eval.run_eval
    poetry run python -m scripts.assessment_eval.run_eval --reps 3 --judge-model anthropic/claude-opus-4-8
    poetry run python -m scripts.assessment_eval.run_eval --regen-corpus --tasks task-01-training-loop

``generate_assessment`` and ``judge_scenario`` are imported into this module's
namespace so the CI tests can patch them here (no real LLM in the default test run).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

# Load backend/.env before importing proxy/generate so OPENROUTER_API_KEY and the
# import-time model defaults (proxy.DEFAULT_MODEL / generate.ASSESSMENT_MODEL) resolve
# from it — same .env the running backend uses. Must precede the vibony imports.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from vibony_backend.assessment.generate import generate_assessment  # noqa: E402
from vibony_backend.assessment.inputs import read_task_spec_from_disk  # noqa: E402

from scripts.assessment_eval.author import (  # noqa: E402
    author_scenario,
    read_reference,
    read_scaffold,
)
from scripts.assessment_eval.corpus import (  # noqa: E402
    build_inputs,
    corpus_task_ids,
    load_corpus,
    make_record,
    save_record,
)
from scripts.assessment_eval.judge import Verdict, judge_scenario  # noqa: E402
from scripts.assessment_eval.personas import derive_scenarios  # noqa: E402
from scripts.assessment_eval.scorecard import Scorecard, aggregate, render_markdown  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path(__file__).parent / "scorecard.md"


async def run_eval(
    records: list[dict[str, Any]],
    *,
    model: str | None = None,
    judge_model: str | None = None,
    reps: int = 1,
) -> tuple[list[Verdict], Scorecard]:
    """Generate + judge every record ``reps`` times; return verdicts + aggregate."""
    verdicts: list[Verdict] = []
    for record in records:
        inputs = build_inputs(record)
        spec = read_task_spec_from_disk(record["task_id"])
        for rep in range(reps):
            summary, findings, _used = await generate_assessment(inputs, model=model)
            verdict = await judge_scenario(
                record, summary, findings, spec=spec, model=judge_model
            )
            verdicts.append(verdict)
            logger.info(
                "run_eval: %s/%s rep %d/%d — recall %d/%d, faith %d",
                record["task_id"],
                record["key"],
                rep + 1,
                reps,
                verdict.caught_triggered,
                verdict.triggered_total,
                verdict.judge.faithfulness,
            )
    return verdicts, aggregate(verdicts)


async def regen_corpus(task_ids: list[str], *, model: str | None = None) -> int:
    """Author + freeze the scenario matrix for each task that has a ``spec.yaml``."""
    written = 0
    for task_id in task_ids:
        spec = read_task_spec_from_disk(task_id)
        if spec is None:
            logger.warning("regen_corpus: %s has no spec.yaml — skipping", task_id)
            continue
        scaffold = read_scaffold(task_id)
        reference = read_reference(task_id)
        for scenario in derive_scenarios(spec):
            authored = await author_scenario(
                spec, scaffold, scenario, reference=reference, model=model
            )
            record = make_record(
                task_id, scenario, authored["events"], authored["final_files"]
            )
            save_record(record)
            written += 1
    return written


def _resolve_tasks(arg_tasks: list[str] | None) -> list[str]:
    if arg_tasks:
        return arg_tasks
    found = corpus_task_ids()
    if not found:
        raise SystemExit(
            "no frozen corpus found — run with --regen-corpus --tasks <id> first"
        )
    return found


async def _amain(args: argparse.Namespace) -> None:
    if args.regen_corpus:
        if not args.tasks:
            raise SystemExit("--regen-corpus requires --tasks <id> [<id> ...]")
        count = await regen_corpus(args.tasks, model=args.model)
        logger.info("regen_corpus: wrote %d scenario(s)", count)
        return

    records: list[dict[str, Any]] = []
    for task_id in _resolve_tasks(args.tasks):
        records.extend(load_corpus(task_id))
    if not records:
        raise SystemExit("corpus is empty")

    verdicts, card = await run_eval(
        records, model=args.model, judge_model=args.judge_model, reps=args.reps
    )
    out = Path(args.out)
    out.write_text(
        render_markdown(
            verdicts,
            card,
            model=args.model or "(default)",
            judge_model=args.judge_model or "(default)",
        )
    )
    logger.info(
        "scorecard: recall %s, hallucinated %d, faith %.2f -> %s",
        f"{card.caught_total}/{card.triggered_total}",
        card.hallucinated_total,
        card.mean_faithfulness,
        out,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Assessment prompt calibration harness")
    parser.add_argument("--tasks", nargs="*", help="task ids (default: all frozen corpora)")
    parser.add_argument("--reps", type=int, default=1, help="generations per scenario")
    parser.add_argument("--model", default=None, help="generator model (default: env)")
    parser.add_argument("--judge-model", default=None, help="judge model (default: env)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="scorecard markdown path")
    parser.add_argument(
        "--regen-corpus",
        action="store_true",
        help="(re)author + freeze scenarios for --tasks (LLM calls)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
