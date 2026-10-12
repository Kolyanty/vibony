"""Trap-firing benchmark runner CLI.

Drives **real** sessions through the production ``AgentHost`` + sandbox: for each
planted trap, runs its scenarios (handoff / passive / correct / vague) ``reps`` times
on the assistant model under test, judges whether the flaw fired, and writes a
statistical scorecard. This is the fail-fast instrument — "can the interview AI
follow a planted trap deterministically + corrigibly, measurably, on one task?"

**Requires a Docker host + a real LLM** (like an end-to-end checkpoint, the
calibration run) — it provisions containers and makes paid calls on the *production*
assistant model. macOS/CI cannot run it; it is author-run. The pure pieces (driver
loop, judge, scorecard, deterministic check) are unit-tested with ``litellm`` mocked
(``tests/test_trap_bench.py``).

Models are three **independent** roles (prompt seam):
* ``--assistant-model`` — under test; defaults to the proxy ``DEFAULT_MODEL``. Must
  be allowlisted (``VIBONY_MODEL_ALLOWLIST``) — it runs through the real ``call_llm``.
* ``--candidate-model`` — role-plays the candidate; defaults to the resolved judge
  model.
* ``--judge-model`` — the trap-firing judge; resolves ``--judge-model`` >
  ``VIBONY_JUDGE_MODEL`` > :data:`DEFAULT_JUDGE_MODEL` (Kimi K2.6). The default is
  deliberately **not** the assistant model — a same-family judge rates its own kind
  leniently (anti-circularity), and the assistant defaults to DeepSeek.
* ``--controller-model`` — the trap-control gate; overrides
  ``VIBONY_CONTROLLER_MODEL`` for the run (must be allowlisted + a different family
  than the assistant). The driven AgentHost reads the env at session start, so the
  controller is **off** unless this flag or the env is set. ``--no-controller`` forces
  it off for a clean controller-off comparison run. ``--controller-fewshot`` appends a
  worked example to the controller system prompt (``VIBONY_CONTROLLER_FEWSHOT``) — an A/B
  lever for the malformed-verdict rate, off by default.

Wall-clock is dominated by the LLM round-trips (the assistant agent-loop runs many
tool steps per turn), not Docker. So the speed levers are ``--concurrency`` (runs
are independent → parallelise), ``--reps``, and a tighter per-call LLM timeout via
``VIBONY_LLM_CALL_TIMEOUT_SECONDS`` (a hung provider call otherwise stalls a turn).
Wrap the command in ``caffeinate -dimsu`` so macOS doesn't throttle/sleep the
process mid-sweep. Each sweep writes a **self-contained, timestamped run directory**
under ``runs/`` — ``runs/<UTC-timestamp>[__label]/`` holding ``scorecard.md``,
``outcomes.jsonl`` (raw per-run records), and ``dumps/<key>__<code>.md`` (each run's
transcript + final code + judge verdict). Prior runs are never overwritten, so the
full history stays on disk and findable. Both files are rewritten/appended after
**each** run, so a Ctrl-C / crash mid-sweep keeps everything finished so far. The
dumps are the only way to inspect a benchmark dialog, since the TRAP-* sessions live
under the system templates company and the (workspace-scoped) dashboard won't show
them. ``--inspect runs/<dir>/outcomes.jsonl`` retro-renders those dumps from the kept
TRAP-* sessions (no re-run, no LLM calls) if a run's ``dumps/`` was lost.

Examples (from ``backend/``, Docker up + creds in .env):

    caffeinate -dimsu env VIBONY_SANDBOX_PROVIDER=docker VIBONY_LLM_CALL_TIMEOUT_SECONDS=150 \\
        poetry run python -m scripts.assessment_eval.run_trapbench \\
        --task task-01-training-loop --reps 3 --concurrency 4 --label baseline \\
        --assistant-model openrouter/deepseek/deepseek-v4-flash \\
        --candidate-model openrouter/deepseek/deepseek-v4-flash
        # --assistant-model is the model UNDER TEST — pin it (don't rely on the .env
        # default); it must be allowlisted or = VIBONY_DEFAULT_MODEL. --candidate-model
        # only role-plays the candidate (not measured, no anti-circularity) → any cheap
        # valid model; it defaults to the judge model if omitted. judge defaults to Kimi
        # K2.6 (non-DeepSeek, anti-circular); override with --judge-model … to compare.

    # one trap, quick smoke
    ... run_trapbench --task task-01-training-loop --traps 0 --reps 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Load backend/.env before importing the backend so model defaults + DB URL resolve
# from it (same .env the running backend uses). Must precede the vibony imports.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from sqlalchemy import select  # noqa: E402

from vibony_backend.assessment.inputs import (  # noqa: E402
    TASKS_DIR,
    read_task_spec_from_disk,
    read_task_spec_from_path,
    render_final_code,
    render_transcript,
)
from vibony_backend.prompt_seam import (  # noqa: E402
    assistant_model as seam_assistant_model,
)
from vibony_backend.sessions import PROTOCOL_VERSION  # noqa: E402

from scripts.assessment_eval.trap_driver import (  # noqa: E402
    MAX_CANDIDATE_TURNS,
    drive_session,
)
from scripts.assessment_eval.trap_judge import judge_trap  # noqa: E402
from scripts.assessment_eval.trap_scenarios import (  # noqa: E402
    BREAKING,
    CORRECT,
    HANDOFF,
    PASSIVE,
    VAGUE,
    SessionPlan,
    derive_trap_scenarios,
    plan_sessions,
)
from scripts.assessment_eval.progress import (  # noqa: E402
    SweepProgress,
    render_summary,
)
from scripts.assessment_eval.spend import SpendTracker  # noqa: E402
from scripts.assessment_eval.trap_scorecard import (  # noqa: E402
    RunOutcome,
    aggregate_bands,
    render_trapbench_markdown,
)

logger = logging.getLogger(__name__)

# Every sweep writes a self-contained, timestamped run directory under here
# (scorecard.md + outcomes.jsonl + dumps/) so history is never overwritten — runs are
# findable by time + label. gitignored.
RUNS_ROOT = Path(__file__).parent / "runs"


def _read_task_brief(task_slug: str) -> str:
    """The candidate-facing ``task.md`` for a task (the persona's only task view).

    Dev/harness path, mirroring ``read_task_spec_from_disk``: reads the repo
    ``tasks/<slug>/task.md``. Empty string if absent — the persona then just tells
    the assistant to read task.md. task-general: every task has a task.md.
    """
    path = TASKS_DIR / task_slug / "task.md"
    return path.read_text() if path.exists() else ""


def _new_run_dir(label: str) -> Path:
    """A fresh ``runs/<UTC-timestamp>[__label]`` dir, disambiguated on collision."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label).strip("-")[:40]
    base = f"{stamp}__{safe}" if safe else stamp
    run_dir = RUNS_ROOT / base
    n = 2
    while run_dir.exists():
        run_dir = RUNS_ROOT / f"{base}-{n}"
        n += 1
    return run_dir


# Default trap-firing judge. Kimi K2.6 (Moonshot): near-Opus on the intelligence
# index (~54 vs 57, June 2026), ~5x cheaper, and — crucially — NOT DeepSeek, which
# the production assistant defaults to: a same-family judge self-leniently rates its
# own kind (anti-circularity). Overridden by --judge-model or VIBONY_JUDGE_MODEL. We
# deliberately do NOT fall back to the assistant model here (that would be circular).
DEFAULT_JUDGE_MODEL = "openrouter/moonshotai/kimi-k2.6"


# ---------------------------------------------------------------------------
# DB-backed session lifecycle (real AgentHost + sandbox)
# ---------------------------------------------------------------------------


def _make_event_reader(code: str):
    async def read_new_events(since_seq: int) -> list[tuple[int, str, dict]]:
        from vibony_backend.db.engine import get_sessionmaker
        from vibony_backend.db.models import Event
        from vibony_backend.db.models import Session as DbSession

        sm = get_sessionmaker()
        async with sm() as db:
            rows = (
                await db.execute(
                    select(Event.seq, Event.type, Event.payload)
                    .join(DbSession, Event.session_id == DbSession.id)
                    .where(DbSession.code == code, Event.seq > since_seq)
                    .order_by(Event.seq)
                )
            ).all()
        return [(seq, etype, payload or {}) for seq, etype, payload in rows]

    return read_new_events


def _make_file_reader(code: str):
    async def read_final_files() -> dict[str, str | None]:
        from vibony_backend.sessions.event_store import replay_file_state

        return await replay_file_state(code)

    return read_final_files


async def _delete_session(session_id: uuid.UUID) -> None:
    """Hard-delete a benchmark session row (events cascade via the FK)."""
    from sqlalchemy import delete

    from vibony_backend.db.engine import get_sessionmaker
    from vibony_backend.db.models import Session as DbSession

    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            await db.execute(delete(DbSession).where(DbSession.id == session_id))


@dataclass(frozen=True)
class _StagedArtifact:
    """An artifact staged into an ephemeral DB task for one trap-bench sweep."""

    slug: str
    spec: object  # TaskSpec
    task_brief: str
    task_ref_id: uuid.UUID
    company_id: uuid.UUID


@asynccontextmanager
async def _stage_artifact(artifact_dir: Path):
    """Stage an arbitrary portable artifact into an ephemeral task row.

    This is the ``validate(artifact_dir) → verdict`` external contract: the caller
    hands over a directory, not a pre-seeded slug. The artifact is imported into a
    ``tasks`` row under the system-templates company with a throwaway,
    collision-free slug (so it never clobbers a seeded template of the same name),
    yielded as a :class:`_StagedArtifact`, then **hard-deleted** when the sweep
    exits. The driven sessions are torn down separately (``force_terminate`` /
    ``--cleanup``); this only owns the staged task row.
    """
    from vibony_backend.db.engine import get_sessionmaker
    from vibony_backend.db.models import Task
    from vibony_backend.tasks.importer import import_artifact, read_artifact
    from vibony_backend.tasks.seed import SYSTEM_COMPANY_ID, ensure_system_company

    data = read_artifact(artifact_dir)
    if data is None:
        raise SystemExit(f"{artifact_dir} is not a task artifact (no weakness.yaml)")
    spec = read_task_spec_from_path(artifact_dir / "spec.yaml", data.slug)
    if spec is None or not spec.expected_ai_pitfalls:
        raise SystemExit(f"{artifact_dir} has no spec.yaml pitfalls to benchmark")
    task_md = artifact_dir / "task.md"
    task_brief = task_md.read_text() if task_md.is_file() else ""

    # Throwaway slug: the real slug may already be a seeded template; a unique suffix
    # keeps the staged row isolated and droppable. Bounded to the 64-char slug limit.
    stage_slug = f"{data.slug[:48]}-tv{uuid.uuid4().hex[:8]}"

    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            await ensure_system_company(db)
            row = await import_artifact(
                db,
                artifact_dir,
                company_id=SYSTEM_COMPANY_ID,
                created_by=None,
                is_template=False,
                visibility="workspace",
                slug_override=stage_slug,
            )
            task_ref_id = row.id
    try:
        yield _StagedArtifact(
            slug=stage_slug,
            spec=spec,
            task_brief=task_brief,
            task_ref_id=task_ref_id,
            company_id=SYSTEM_COMPANY_ID,
        )
    finally:
        from sqlalchemy import delete

        async with sm() as db:
            async with db.begin():
                await db.execute(delete(Task).where(Task.id == task_ref_id))


async def _mint_session(
    task_slug: str,
    model: str,
    *,
    task_ref_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    """Create a throwaway live Session row bound to a task.

    Returns ``(session_id, code)``. When ``task_ref_id``/``company_id`` are given
    (a staged artifact) the session binds straight to that row; otherwise the
    task is resolved by slug from the seeded templates (the repo path). Raises
    ``SystemExit`` if a slug-resolved template is not seeded — start the backend
    once (its lifespan seeds) or this runner's pre-seed step failed.
    """
    from vibony_backend.db.engine import get_sessionmaker
    from vibony_backend.db.models import Session as DbSession
    from vibony_backend.tasks.store import find_task_by_slug

    code = "TRAP-" + uuid.uuid4().hex[:8].upper()
    sm = get_sessionmaker()
    async with sm() as db:
        async with db.begin():
            if task_ref_id is None:
                task = await find_task_by_slug(db, task_slug, None)
                if task is None:
                    raise SystemExit(
                        f"template task {task_slug!r} not found — start the backend "
                        "once to seed templates, or check the slug"
                    )
                task_ref_id = task.id
                company_id = task.company_id
            row = DbSession(
                code=code,
                status="pending",
                task_id=task_slug,
                task_ref_id=task_ref_id,
                protocol_version=PROTOCOL_VERSION,
                ai_config={"primary_model": model},
                company_id=company_id,
                candidate_token=str(uuid.uuid4()),
                expires_at=datetime.now(UTC) + timedelta(hours=24),
                mode="live",
            )
            db.add(row)
            await db.flush()
            session_id = row.id
    return session_id, code


async def run_session(
    plan: SessionPlan,
    task_slug: str,
    *,
    assistant_model: str,
    candidate_model: str,
    judge_model: str,
    allow_deterministic: bool,
    task_brief: str = "",
    max_turns: int = MAX_CANDIDATE_TURNS,
    cleanup: bool = False,
    active_codes: set[str] | None = None,
    dump_dir: Path | None = None,
    task_ref_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> list[RunOutcome]:
    """Drive ONE session (``plan.drive``'s persona) and judge it against every trap in
    ``plan.judge`` — one :class:`RunOutcome` per judged trap. For a trap-specific kind
    ``plan.judge`` is a singleton (the old per-scenario run). For a trap-agnostic kind it
    holds every selected trap of that kind, so N same-kind sessions collapse into one
    driven session judged N times (the assistant plants ALL traps in every session, so one
    transcript scores them all). Any failure → an errored RunOutcome per judged trap (the
    band reports it separately; one bad run never aborts the sweep) — a session-level
    failure (turn_error / controller fail-open) invalidates EVERY trap scored from it.

    ``active_codes`` (when given) tracks this run's session code while its container is
    live, so the parent sweep can force-terminate a straggler if a hard interrupt cancels
    this worker before its own ``finally`` completes. ``dump_dir`` (when given) writes the
    judged transcript + final code + verdict per trap for inspection. ``task_ref_id`` /
    ``company_id`` (when given) bind the session to a staged artifact instead of resolving
    ``task_slug`` from the seeded templates.
    """
    from vibony_backend.agent_host.host import agent_host_registry

    def _errored(reason: str, *, code: str | None, result=None) -> list[RunOutcome]:
        # One errored outcome per judged trap — a session-level failure kills them all.
        return [
            RunOutcome(
                trap_index=sc.trap_index,
                kind=sc.kind,
                key=sc.key,
                error=reason,
                candidate_turns=result.candidate_turns if result is not None else 0,
                submitted=result.submitted if result is not None else False,
                session_code=code,
            )
            for sc in plan.judge
        ]

    code: str | None = None
    session_id: uuid.UUID | None = None
    try:
        session_id, code = await _mint_session(
            task_slug,
            assistant_model,
            task_ref_id=task_ref_id,
            company_id=company_id,
        )
        if active_codes is not None:
            active_codes.add(code)
        host, _created, _epoch = await agent_host_registry.get_or_create(
            code, session_id, task_slug, {"primary_model": assistant_model}
        )
        await host.start()
        result = await drive_session(
            host,
            plan.drive,
            candidate_model=candidate_model,
            read_new_events=_make_event_reader(code),
            read_final_files=_make_file_reader(code),
            max_turns=max_turns,
            task_brief=task_brief,
        )
        final_code, _stats = render_final_code(result.final_files)
        # Controller reliability counters for this session, read once — stamped
        # onto every outcome below so the scorecard can sum them deduped-per-session.
        fail_opens = host.controller_fail_opens()
        ctrl_stats = {
            "ctrl_retries": host.controller_review_retries(),
            "ctrl_repairs": host.controller_verdict_repairs(),
            "ctrl_fail_opens": fail_opens,
        }
        # A controller fail-open let a round through UNGATED — a technical error that
        # would otherwise silently become measured data. Exclude the run, like turn_error
        # Zero fail-opens is the target; any > 0 is surfaced as an errored cell.
        if result.turn_error or fail_opens > 0:
            reason = (
                "agent turn failed (error bubble in transcript)"
                if result.turn_error
                else f"controller failed open {fail_opens}x (ungated round — unreliable)"
            )
            outcomes = _errored(reason, code=code, result=result)
        else:
            outcomes = []
            for sc in plan.judge:
                judgment = await judge_trap(
                    sc.trap,
                    sc.trap_index,
                    task_id=task_slug,
                    transcript=result.transcript,
                    final_code=final_code,
                    final_files=result.final_files,
                    judge_model=judge_model,
                    allow_deterministic=allow_deterministic,
                )
                outcomes.append(
                    RunOutcome(
                        trap_index=sc.trap_index,
                        kind=sc.kind,
                        key=sc.key,
                        present=judgment.present,
                        volunteered=judgment.volunteered,
                        layer=judgment.layer,
                        confidence=judgment.confidence,
                        rationale=judgment.rationale,
                        candidate_turns=result.candidate_turns,
                        submitted=result.submitted,
                        session_code=code,
                    )
                )
        # Stamp the per-session controller counters onto every outcome from THIS drive
        # (the fail-open-errored ones + the judged ones) so the scorecard can sum them
        # deduped-per-session. A session that raised before the drive completed
        # (the outer except below) has no counters to stamp — its outcomes keep 0.
        for o in outcomes:
            o.ctrl_retries = ctrl_stats["ctrl_retries"]
            o.ctrl_repairs = ctrl_stats["ctrl_repairs"]
            o.ctrl_fail_opens = ctrl_stats["ctrl_fail_opens"]
        if dump_dir is not None:
            # The dump is a side-channel — a write failure must not drop a valid verdict
            # (turn it into an errored, excluded run). One dump per judged trap; they
            # share the transcript + final code, differing only in the verdict.
            for outcome in outcomes:
                try:
                    _write_run_dump(dump_dir, outcome, result.transcript, final_code)
                except Exception:
                    logger.warning("run_session: dump failed for %s", outcome.key)
        return outcomes
    except Exception as exc:
        logger.exception("run_session failed: %s", plan.drive.key)
        return _errored(f"{type(exc).__name__}: {exc}", code=code)
    finally:
        if code is not None:
            await agent_host_registry.force_terminate(code)
            if active_codes is not None:
                active_codes.discard(code)
        if cleanup and session_id is not None:
            try:
                await _delete_session(session_id)
            except Exception:
                logger.warning("run_session: cleanup failed for %s", code)


def _append_outcome_jsonl(path: Path, outcome: RunOutcome) -> None:
    """Append one outcome as a JSON line (crash-safe raw record for recovery)."""
    with path.open("a") as fh:
        fh.write(json.dumps(asdict(outcome)) + "\n")


def _write_run_dump(
    dump_dir: Path, outcome: RunOutcome, transcript: str, final_code: str
) -> None:
    """Dump exactly what the judge saw (transcript + final code) next to what it
    decided (present / layer / confidence / rationale), for judge inspection.

    One markdown file per run, ``<key>__<code>.md``. This is the only path that
    surfaces a benchmark dialog — the TRAP-* sessions live under the system
    templates company, so the dashboard (workspace-membership scoped) won't show
    them.
    """
    dump_dir.mkdir(parents=True, exist_ok=True)
    code = outcome.session_code or "nocode"
    verdict = (
        "ERROR (excluded)"
        if outcome.error
        else (
            f"present={outcome.present} · volunteered={outcome.volunteered} · "
            f"layer={outcome.layer} · confidence={outcome.confidence}"
        )
    )
    body = (
        f"# {outcome.key}  (session {code})\n\n"
        f"- trap {outcome.trap_index}, band `{outcome.kind}`\n"
        f"- judge verdict: **{verdict}**\n"
        f"- rationale: {outcome.rationale or outcome.error or '—'}\n"
        f"- candidate_turns={outcome.candidate_turns}, submitted={outcome.submitted}\n\n"
        "## Transcript (what the judge read)\n\n```\n"
        f"{transcript or '(empty)'}\n```\n\n"
        "## Final code (what the judge read)\n\n```\n"
        f"{final_code or '(no surviving code)'}\n```\n"
    )
    (dump_dir / f"{outcome.key}__{code}.md").write_text(body)


async def _inspect_jsonl(
    jsonl_path: Path,
    dump_dir: Path,
    *,
    event_reader_factory=_make_event_reader,
    file_reader_factory=_make_file_reader,
) -> int:
    """Retro-render a finished sweep's ``*-outcomes.jsonl`` into readable per-run
    dumps — for inspecting a run that wasn't launched with ``--dump-dir``.

    Each jsonl line is a :class:`RunOutcome`; its ``session_code`` is resolved back
    to the live TRAP-* session (so this needs the sweep to have kept its rows, i.e.
    not ``--cleanup``). Returns how many were rendered; rows with no code or a
    vanished session are warned + skipped. Readers are injectable for testing.
    """
    if not jsonl_path.is_file():
        raise SystemExit(f"no such outcomes file: {jsonl_path}")
    known = {f.name for f in fields(RunOutcome)}
    rendered = 0
    for line in jsonl_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        # Tolerate a torn trailing line (a Ctrl-C'd sweep's last incremental write)
        # or a row missing a required field — inspect exists to recover exactly
        # those interrupted sweeps, so one bad line must not abort the rest.
        try:
            data = {k: v for k, v in json.loads(line).items() if k in known}
            outcome = RunOutcome(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("inspect: skipping unparseable line (%s)", exc)
            continue
        code = outcome.session_code
        if not code:
            logger.warning("inspect: %s has no session_code — skip", outcome.key)
            continue
        events = await event_reader_factory(code)(0)
        if not events:
            logger.warning(
                "inspect: no events for %s (session %s gone — --cleanup'd?) — skip",
                outcome.key,
                code,
            )
            continue
        transcript, _ = render_transcript(events)
        final_code, _ = render_final_code(await file_reader_factory(code)())
        _write_run_dump(dump_dir, outcome, transcript, final_code)
        rendered += 1
    return rendered


async def run_trapbench(
    task_slug: str,
    *,
    reps: int,
    traps: list[int] | None,
    kinds: list[str] | None = None,
    assistant_model: str,
    candidate_model: str,
    judge_model: str,
    allow_deterministic: bool,
    max_turns: int = MAX_CANDIDATE_TURNS,
    concurrency: int = 1,
    cleanup: bool = False,
    dump_dir: Path | None = None,
    artifact_dir: Path | None = None,
    on_outcome: Callable[[RunOutcome, list[RunOutcome]], None] | None = None,
    progress: SweepProgress | None = None,
) -> tuple[list[RunOutcome], list]:
    """Sweep every scenario × ``reps``, return outcomes + bands.

    Two task sources. Default (``artifact_dir`` is ``None``): resolve
    ``task_slug`` from the seeded repo templates (the spec read from the repo
    ``tasks/<slug>/`` tree). With ``artifact_dir``: stage an arbitrary **portable
    artifact** into an ephemeral task row (the ``validate(artifact_dir) → verdict``
    path) — ``task_slug`` is then only the scorecard label; the spec + brief + task
    binding come from the artifact, and the staged row is dropped when the sweep ends.
    """
    if artifact_dir is not None:
        async with _stage_artifact(artifact_dir) as staged:
            return await _run_sweep(
                task_slug=task_slug,
                spec=staged.spec,
                task_brief=staged.task_brief,
                reps=reps,
                traps=traps,
                kinds=kinds,
                assistant_model=assistant_model,
                candidate_model=candidate_model,
                judge_model=judge_model,
                allow_deterministic=allow_deterministic,
                max_turns=max_turns,
                concurrency=concurrency,
                cleanup=cleanup,
                dump_dir=dump_dir,
                task_ref_id=staged.task_ref_id,
                company_id=staged.company_id,
                on_outcome=on_outcome,
                progress=progress,
            )

    from vibony_backend.tasks.seed import run_template_seed

    await run_template_seed()  # idempotent — ensure the template task exists

    spec = read_task_spec_from_disk(task_slug)
    if spec is None or not spec.expected_ai_pitfalls:
        raise SystemExit(f"{task_slug} has no spec.yaml pitfalls to benchmark")

    # The persona is fed the candidate-facing task.md (not spec internals — those name
    # the trap). Loaded once; the same brief drives every scenario of this task.
    task_brief = _read_task_brief(task_slug)

    return await _run_sweep(
        task_slug=task_slug,
        spec=spec,
        task_brief=task_brief,
        reps=reps,
        traps=traps,
        kinds=kinds,
        assistant_model=assistant_model,
        candidate_model=candidate_model,
        judge_model=judge_model,
        allow_deterministic=allow_deterministic,
        max_turns=max_turns,
        concurrency=concurrency,
        cleanup=cleanup,
        dump_dir=dump_dir,
        task_ref_id=None,
        company_id=None,
        on_outcome=on_outcome,
        progress=progress,
    )


async def _run_sweep(
    *,
    task_slug: str,
    spec: object,
    task_brief: str,
    reps: int,
    traps: list[int] | None,
    kinds: list[str] | None,
    assistant_model: str,
    candidate_model: str,
    judge_model: str,
    allow_deterministic: bool,
    max_turns: int,
    concurrency: int,
    cleanup: bool,
    dump_dir: Path | None,
    task_ref_id: uuid.UUID | None,
    company_id: uuid.UUID | None,
    on_outcome: Callable[[RunOutcome, list[RunOutcome]], None] | None,
    progress: SweepProgress | None = None,
) -> tuple[list[RunOutcome], list]:
    """Run every scenario × ``reps`` (the source-agnostic sweep loop).

    Runs up to ``concurrency`` sessions at once (independent — own session, code,
    container, baseline code — so safe to parallelise; the LLM round-trips are
    I/O-bound and dominate wall-clock). ``on_outcome(outcome, outcomes_so_far)`` is
    invoked under a lock after each run finishes — used to persist partial progress
    so a Ctrl-C / crash mid-sweep doesn't lose the runs already done.
    """
    scenarios = derive_trap_scenarios(spec)
    if traps is not None:
        scenarios = [s for s in scenarios if s.trap_index in traps]
    if kinds is not None:
        scenarios = [s for s in scenarios if s.kind in kinds]
    if not scenarios:
        raise SystemExit("no scenarios selected (check --traps / --kinds)")

    # Collapse trap-agnostic same-kind scenarios into shared sessions (the session is the
    # expensive part; one transcript scores every trap). Cells stay per-(trap, kind), so
    # `total` — the progress/scorecard denominator — is still the scenario count, not the
    # session count.
    plans = plan_sessions(scenarios)
    total = len(scenarios) * reps
    if progress is not None:
        progress.set_total(total)
    sem = asyncio.Semaphore(max(1, concurrency))
    record_lock = asyncio.Lock()
    outcomes: list[RunOutcome] = []
    # Codes with a live container, so the finally below can force-terminate any
    # straggler a hard interrupt cancelled before its own run_session finally ran.
    active_codes: set[str] = set()

    async def _worker(plan: SessionPlan) -> None:
        async with sem:
            if progress is not None:
                for sc in plan.judge:  # every cell this session will score is now in-flight
                    progress.on_start(sc.key)
            results = await run_session(
                plan,
                task_slug,
                assistant_model=assistant_model,
                candidate_model=candidate_model,
                judge_model=judge_model,
                allow_deterministic=allow_deterministic,
                task_brief=task_brief,
                max_turns=max_turns,
                cleanup=cleanup,
                active_codes=active_codes,
                dump_dir=dump_dir,
                task_ref_id=task_ref_id,
                company_id=company_id,
            )
        # Serialise bookkeeping + the partial-progress write (asyncio is single-
        # threaded, but the lock keeps append + callback atomic across workers).
        async with record_lock:
            for outcome in results:
                outcomes.append(outcome)
                if progress is not None:
                    progress.on_finish(outcome)
                else:
                    logger.info(
                        "trapbench: %d/%d done — %s (%s)",
                        len(outcomes),
                        total,
                        outcome.key,
                        "ERROR" if outcome.error else f"present={outcome.present} [{outcome.layer}]",
                    )
                if on_outcome is not None:
                    on_outcome(outcome, list(outcomes))

    # One task per (session-plan, rep); the semaphore bounds how many run at once.
    try:
        await asyncio.gather(
            *(_worker(p) for p in plans for _ in range(reps))
        )
    finally:
        # Safety net: on a hard interrupt that cancelled workers mid-run, sweep
        # any container whose own run_session finally didn't get to terminate it.
        if active_codes:
            from vibony_backend.agent_host.host import agent_host_registry

            for code in list(active_codes):
                await agent_host_registry.force_terminate(code)

    return outcomes, aggregate_bands(outcomes)


async def _amain(args: argparse.Namespace) -> None:
    if args.inspect:
        src = Path(args.inspect)
        # Default: render back into the run's own dumps/ (alongside the outcomes.jsonl).
        dump_dir = Path(args.dump_dir) if args.dump_dir else src.parent / "dumps"
        n = await _inspect_jsonl(src, dump_dir)
        logger.info("inspect: rendered %d run(s) from %s -> %s", n, src, dump_dir)
        return

    # Task source: --artifact stages an arbitrary portable artifact dir
    # (validate(artifact_dir) → verdict); otherwise --task resolves a seeded template.
    artifact_dir = Path(args.artifact) if args.artifact else None
    task_label = args.task or (artifact_dir.name if artifact_dir else "task-01-training-loop")

    assistant_model = args.assistant_model or seam_assistant_model()
    # judge: explicit flag > VIBONY_JUDGE_MODEL > Kimi default (never the assistant
    # — anti-circularity). candidate inherits the judge model unless overridden.
    judge_model = (
        args.judge_model or os.environ.get("VIBONY_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL
    )
    candidate_model = args.candidate_model or judge_model

    # controller: the driven AgentHost reads VIBONY_CONTROLLER_MODEL at session
    # start (controller off when unset). --controller-model overrides it for this run;
    # --no-controller forces it off for a clean comparison. Resolved here so the
    # scorecard header records exactly what gated the runs.
    if args.no_controller:
        os.environ.pop("VIBONY_CONTROLLER_MODEL", None)
    elif args.controller_model:
        os.environ["VIBONY_CONTROLLER_MODEL"] = args.controller_model
    controller_model = None if args.no_controller else os.environ.get("VIBONY_CONTROLLER_MODEL")

    # The controller defaults to reasoning-off (agent_host/controller.py) — a reasoning
    # verdict needs no chain-of-thought and risks failing open. --controller-thinking
    # forces it back on for this run (empty extra_body → the model's own default).
    if args.controller_thinking:
        os.environ["VIBONY_CONTROLLER_EXTRA_BODY"] = ""

    # --controller-max-retries raises the assistant-round correction cap for this run
    # (VIBONY_CONTROLLER_MAX_RETRIES, default 2) — the residual-3 lever: A/B whether
    # more corrective rounds recover the hold on a maximally blatant defect a free-working
    # passive persona keeps volunteering.
    if args.controller_max_retries is not None:
        os.environ["VIBONY_CONTROLLER_MAX_RETRIES"] = str(args.controller_max_retries)

    # --controller-fewshot appends the worked example to the controller system prompt
    # (VIBONY_CONTROLLER_FEWSHOT, read per-call in controller.py). An A/B lever for the
    # malformed-verdict rate; off by default so the baseline prompt is byte-identical.
    if args.controller_fewshot:
        os.environ["VIBONY_CONTROLLER_FEWSHOT"] = "1"

    # Each sweep gets its own timestamped run dir — never overwrites prior runs, so
    # the full history stays on disk and findable (resolves the old "clear the default
    # scorecard or it mixes runs" footgun).
    run_dir = Path(args.run_dir) if args.run_dir else _new_run_dir(args.label)
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / "scorecard.md"
    jsonl = run_dir / "outcomes.jsonl"
    dump_dir = run_dir / "dumps"
    jsonl.write_text("")  # this run's raw record (its own dir — no cross-run mixing)

    # litellm prints a "Provider List / Give Feedback" banner straight to the console
    # (a bare print, not via logging) on its internal retries/exceptions — it bypasses
    # the WARNING-level quieting below and clobbers the sticky progress bar. Silence it
    # at the source; genuine per-run failures still surface as errored (⚠) cells.
    import litellm

    litellm.suppress_debug_info = True

    # Process-global LLM spend tally (assistant + candidate + judge, every
    # litellm.acompletion in this process) — folded into the scorecard, updated live.
    spend = SpendTracker()
    spend.register()

    # Live sticky progress bar (author-run UX). On a TTY it replaces the per-cell INFO
    # firehose — the INFO logs (litellm/httpx/agent-host) would clobber the sticky line,
    # so raise the console to WARNING while the bar owns stderr. --no-progress or a
    # non-TTY stream keeps the old plain INFO line-per-cell output.
    use_progress = (not args.no_progress) and sys.stderr.isatty()
    progress = SweepProgress(spend=spend) if use_progress else None
    started = time.monotonic()
    if progress is not None:
        logging.getLogger().setLevel(logging.WARNING)
        sel = f"traps={args.traps or 'all'} kinds={args.kinds or 'all'}"
        progress.start(
            [
                f"\x1b[1m━━━ trap-bench · {task_label} ━━━\x1b[0m",
                f"  assistant   {assistant_model}",
                f"  candidate   {candidate_model}",
                f"  judge       {judge_model}",
                f"  controller  {controller_model or '(off)'}"
                + (" +fewshot" if controller_model and args.controller_fewshot else ""),
                f"  {sel} reps={args.reps} concurrency={args.concurrency}",
            ]
        )
    else:
        logger.info("trapbench: controller = %s", controller_model or "(off)")

    def _render(outcomes: list[RunOutcome]) -> str:
        scorecard = render_trapbench_markdown(
            outcomes,
            aggregate_bands(outcomes),
            task_id=task_label,
            assistant_model=assistant_model,
            judge_model=judge_model,
            candidate_model=candidate_model,
            reps=args.reps,
            label=args.label,
            controller_model=controller_model,
        )
        return scorecard + "\n" + spend.render_markdown()

    def _persist(outcome: RunOutcome, outcomes_so_far: list[RunOutcome]) -> None:
        # Partial progress: append the raw outcome + rewrite the scorecard so a
        # Ctrl-C / crash mid-sweep leaves both on disk, current.
        _append_outcome_jsonl(jsonl, outcome)
        out.write_text(_render(outcomes_so_far))

    try:
        outcomes, _bands = await run_trapbench(
            task_label,
            reps=args.reps,
            traps=args.traps,
            kinds=args.kinds,
            assistant_model=assistant_model,
            candidate_model=candidate_model,
            judge_model=judge_model,
            allow_deterministic=not args.no_deterministic,
            max_turns=args.max_turns,
            concurrency=args.concurrency,
            cleanup=args.cleanup,
            dump_dir=dump_dir,
            artifact_dir=artifact_dir,
            on_outcome=_persist,
            progress=progress,
        )
    finally:
        # Always retire the sticky bar — a Ctrl-C / crash mid-sweep (the long-run case
        # this UX targets) must still leave the cursor on a clean line, not a stale bar.
        # Partial progress is already on disk via _persist.
        if progress is not None:
            progress.close()

    out.write_text(_render(outcomes))  # final authoritative scorecard (incl. spend)
    spend.unregister()
    summary = render_summary(
        outcomes,
        aggregate_bands(outcomes),
        total_calls=spend.total_calls,
        total_cost=spend.total_cost,
        elapsed_s=time.monotonic() - started,
        run_dir=str(run_dir),
        color=use_progress,
    )
    print("\n".join(summary), file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Trap-firing benchmark")
    parser.add_argument(
        "--task",
        default=None,
        help="seeded template task slug to benchmark (default: task-01-training-loop; "
        "with --artifact, only the scorecard label, defaulting to the artifact dir name)",
    )
    parser.add_argument(
        "--artifact",
        default=None,
        metavar="DIR",
        help="benchmark an arbitrary portable task-artifact directory: stage it "
        "into an ephemeral task, run the sweep, drop it — instead of resolving --task "
        "from the seeded templates. The validate(artifact_dir) → verdict path.",
    )
    parser.add_argument("--reps", type=int, default=3, help="runs per scenario")
    parser.add_argument(
        "--traps",
        type=int,
        nargs="*",
        default=None,
        help="restrict to these pitfall indices (default: all)",
    )
    parser.add_argument(
        "--kinds",
        nargs="*",
        default=None,
        choices=[HANDOFF, PASSIVE, CORRECT, BREAKING, VAGUE],
        help="restrict to these scenario kinds (default: all) — e.g. re-run only the "
        "one persona you changed: --kinds breaking",
    )
    parser.add_argument("--assistant-model", default=None, help="model under test")
    parser.add_argument("--candidate-model", default=None, help="candidate driver model")
    parser.add_argument(
        "--judge-model",
        default=None,
        help=f"trap-firing judge (default: {DEFAULT_JUDGE_MODEL} — near-Opus, "
        "non-DeepSeek for anti-circularity, ~5x cheaper; or VIBONY_JUDGE_MODEL)",
    )
    parser.add_argument(
        "--controller-model",
        default=None,
        help="trap-control gate model (overrides VIBONY_CONTROLLER_MODEL for "
        "this run; must be allowlisted + a different family than the assistant). "
        "Unset → whatever the env says (off by default).",
    )
    parser.add_argument(
        "--no-controller",
        action="store_true",
        help="force the controller OFF for this run even if VIBONY_CONTROLLER_MODEL "
        "is set in the env (for a clean controller-off comparison run)",
    )
    parser.add_argument(
        "--controller-thinking",
        action="store_true",
        help="re-enable the controller model's chain-of-thought (OFF by default — the "
        "verdict needs none and a reasoning model risks failing open); use to A/B "
        "whether thinking changes gating quality",
    )
    parser.add_argument(
        "--controller-fewshot",
        action="store_true",
        help="append a worked few-shot example to the controller system prompt (OFF by "
        "default); use to A/B whether it lowers the malformed-verdict rate (kimi reasoning "
        "into `violations` / listing a clean trap) — measurable only in a real paid run",
    )
    parser.add_argument(
        "--controller-max-retries",
        type=int,
        default=None,
        metavar="N",
        help="corrective-regeneration cap per assistant round (VIBONY_CONTROLLER_MAX_RETRIES; "
        "default 2). The residual-3 lever: raise it to A/B whether more rounds recover "
        "the hold on a maximally blatant defect a passive persona keeps volunteering",
    )
    parser.add_argument(
        "--label",
        default="",
        help="run label (e.g. baseline) — folded into the run-dir name + scorecard",
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="where to write this run (scorecard.md + outcomes.jsonl + dumps/); "
        "default: a fresh timestamped dir under runs/ — prior runs are never "
        "overwritten, so history stays findable",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="how many sessions to run at once (default 1 = sequential; 4-6 is a "
        "good fit on a laptop — runs are independent, the LLM calls dominate)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=MAX_CANDIDATE_TURNS,
        help=f"cap on candidate↔assistant turns per run (default {MAX_CANDIDATE_TURNS}; "
        "lower = faster but the candidate has fewer chances — changes the measurement)",
    )
    parser.add_argument(
        "--no-deterministic",
        action="store_true",
        help="skip the deterministic check layer (LLM judge only)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="disable the live progress bar; keep the plain INFO line-per-cell log "
        "(auto-off on a non-TTY stream, e.g. a redirected log)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete each benchmark session row after judging (default: keep; "
        "TRAP-* codes can be bulk-deleted later)",
    )
    parser.add_argument(
        "--dump-dir",
        default=None,
        help="only with --inspect: where to write the rendered <key>__<code>.md "
        "dumps (default: a dumps/ next to the outcomes.jsonl). A normal sweep always "
        "writes dumps into its own run-dir.",
    )
    parser.add_argument(
        "--inspect",
        default=None,
        metavar="OUTCOMES_JSONL",
        help="don't run a sweep — retro-render a finished run's outcomes.jsonl "
        "into readable <key>__<code>.md dumps (needs the TRAP-* sessions kept, i.e. "
        "the sweep ran without --cleanup); writes to --dump-dir or a dumps/ beside it",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()


__all__ = ["run_session", "run_trapbench", "main"]
