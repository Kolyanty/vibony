"""Assessment input assembly.

Gathers the three inputs the assessor LLM reasons over:

* **Task spec** (ground truth) — the assessment-relevant fields of ``spec.yaml``:
  context, contract, acceptance_criteria, expected_ai_pitfalls (with
  detection/severity), failure_signal. This is *employer-facing* material — the
  report is never shown to the candidate, so feeding the traps to the assessor is
  correct. ``None`` when the task has no ``spec.yaml`` (e.g. task-01) — the report
  then leans on the transcript + code alone.
* **Final code** — the candidate-touched files projected from the event log
  (:func:`replay_file_state`), baseline scaffold already excluded there. Capped
  per-file and in total.
* **Transcript** — the event log rendered into seq-annotated plain text: chat
  turns, tool calls, commands run + their (truncated) output, file-change
  summaries. The ``[seq]`` prefix is what lets a finding cite ``event_seq``.
  Per-event truncation + a total budget; **what was dropped is logged** (no
  silent cap — CLAUDE.md working principle).

Everything here is pure-ish: the renderers (:func:`render_task_spec`,
:func:`render_transcript`, :func:`render_final_code`) take plain data and are
unit-testable with no DB and no LLM. Only :func:`assemble_assessment_inputs` (and
the thin ``build_*`` wrappers) touch the DB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vibony_backend.sessions.event_store import replay_file_state

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TASKS_DIR = REPO_ROOT / "tasks"

# Budgets, in characters (a ~4 char/token heuristic keeps these well within a
# cheap model's context once the spec + system prompt are added). Internal knobs,
# not operational — tune here, not via env.
PER_EVENT_CHAR_CAP = 2000
TRANSCRIPT_CHAR_BUDGET = 48_000
PER_FILE_CHAR_CAP = 8_000
FINAL_CODE_CHAR_BUDGET = 24_000

# Row cap on the transcript event SELECT. The renderer already
# bounds *output* size (TRANSCRIPT_CHAR_BUDGET), but it must first load every row
# into memory — a session with millions of tiny events is a backend OOM vector.
# Cap the rows read; a real session is hundreds–low-thousands of events, so this
# only ever bites a pathological/abusive session. We take the earliest events
# (chronological), matching the renderer's existing "drop trailing over budget"
# behaviour, and log when the cap is hit (no silent truncation).
MAX_TRANSCRIPT_EVENTS = 20_000

# Event types the transcript renders. command_output is folded into the
# command_finished line (grouped by command_id), so it is not in this set.
# Service/baseline events carry no candidate-reasoning signal and are skipped.
_RENDERED_TYPES = {
    "session_start",
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "command_run",
    "command_finished",
    "file_changed",
    "diff_rejected",
    "session_end",
}


# ---------------------------------------------------------------------------
# Task spec
# ---------------------------------------------------------------------------


@dataclass
class Pitfall:
    """One expected AI pitfall from spec.yaml ``expected_ai_pitfalls``.

    Extends a pitfall into a *controlled-trap contract* (format.md §4.5/§5):
    ``trigger`` / ``flawed_behavior`` / ``correct_behavior`` / ``defense`` /
    ``check`` formalize the conditional behavioral contract the trap-control
    mechanism + benchmark drive. All five are optional and ``None`` for legacy
    pitfalls that predate the contract — existing tasks stay valid (additive).
    The assessment still reads only ``description`` / ``detection`` /
    ``severity``; the contract fields are consumed by the benchmark.
    """

    description: str
    detection: str | None = None
    severity: str | None = None
    # Controlled-trap contract. Additive; None = legacy pitfall.
    trigger: str | None = None           # when the trap applies
    flawed_behavior: str | None = None   # default (passive/vague candidate) → planted error
    correct_behavior: str | None = None  # correct diagnosis → AI concedes + fixes
    defense: str | None = None           # vague/incorrect pushback → AI holds the flaw plausibly
    check: str | None = None             # deterministic signature where one exists (run/AST/grep)
    # Defect-mechanism classification (format.md §6: state-loss /
    # concurrency / missing-check / boundary / silent-coercion / perf-N+1). Free-form
    # (a generator may coin a new mechanism); additive; None for tasks predating it.
    # Metadata, not a control: the controller + judge stay task-general and never
    # branch on it — it is the dimension validation evidence is grouped by.
    trap_type: str | None = None


@dataclass
class TaskSpec:
    """Assessment-relevant projection of a task's ``spec.yaml``."""

    task_id: str
    context: str = ""
    contract: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    expected_ai_pitfalls: list[Pitfall] = field(default_factory=list)
    failure_signal: list[str] = field(default_factory=list)


def _str_list(value: Any) -> list[str]:
    """Coerce a YAML field into a list of non-empty strings (None-tolerant)."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _opt_str(value: Any) -> str | None:
    """A stripped string, or ``None`` when absent/blank/non-string."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_task_spec(task_id: str, parsed: Any) -> TaskSpec | None:
    """Build a :class:`TaskSpec` from already-loaded YAML (pure).

    Returns ``None`` when ``parsed`` is not a mapping. Missing fields become
    empty — a partial spec still yields a usable (if thin) TaskSpec.
    """
    if not isinstance(parsed, dict):
        return None

    pitfalls: list[Pitfall] = []
    raw_pitfalls = parsed.get("expected_ai_pitfalls")
    if isinstance(raw_pitfalls, list):
        for p in raw_pitfalls:
            if not isinstance(p, dict):
                continue
            desc = p.get("description")
            if not isinstance(desc, str) or not desc.strip():
                continue
            pitfalls.append(
                Pitfall(
                    description=desc.strip(),
                    detection=_opt_str(p.get("detection")),
                    severity=_opt_str(p.get("severity")),
                    # Controlled-trap contract (additive; None when absent).
                    trigger=_opt_str(p.get("trigger")),
                    flawed_behavior=_opt_str(p.get("flawed_behavior")),
                    correct_behavior=_opt_str(p.get("correct_behavior")),
                    defense=_opt_str(p.get("defense")),
                    check=_opt_str(p.get("check")),
                    # Defect-mechanism tag (additive; None when absent).
                    trap_type=_opt_str(p.get("trap_type")),
                )
            )

    context = parsed.get("context")
    return TaskSpec(
        task_id=task_id,
        context=context.strip() if isinstance(context, str) else "",
        contract=_str_list(parsed.get("contract")),
        acceptance_criteria=_str_list(parsed.get("acceptance_criteria")),
        expected_ai_pitfalls=pitfalls,
        failure_signal=_str_list(parsed.get("failure_signal")),
    )


def read_task_spec_from_path(spec_path: Path, task_id: str) -> TaskSpec | None:
    """Load + parse a ``spec.yaml`` at an explicit path into a :class:`TaskSpec`.

    Dev/harness helper: lets the trap-bench read the spec straight from a
    portable artifact directory, not only the repo ``tasks/<id>/`` tree. Returns
    ``None`` when the file is absent, unparseable, or not a mapping — never raises
    (a malformed spec must not break the caller). Missing fields become empty.
    """
    if not spec_path.is_file():
        return None
    try:
        parsed = yaml.safe_load(spec_path.read_text())
    except Exception as exc:
        logger.warning("read_task_spec_from_path: failed to parse %s: %s", spec_path, exc)
        return None
    return parse_task_spec(task_id, parsed)


def read_task_spec_from_disk(task_id: str) -> TaskSpec | None:
    """Load + parse the repo's ``tasks/<task_id>/spec.yaml`` into a :class:`TaskSpec`.

    **Dev/test only — NOT the runtime path.** The runtime
    resolves the spec from the **DB** task row: ``assessment/job.py`` calls
    ``parse_task_spec(session.task_id, loaded.spec)`` off ``tasks/store.py``. The
    filesystem ``tasks/`` tree is just the template *seed* source, so it
    diverges from a workspace's edited DB task. This disk reader exists for the
    eval harness (``scripts/assessment_eval/``) and unit tests, which operate on
    the repo's ``spec.yaml`` files by design. Do **not** wire it into a request
    path — use the injected ``spec`` of :func:`assemble_assessment_inputs` instead.

    Returns ``None`` when the spec is absent, unparseable, or not a mapping —
    never raises. Thin wrapper over :func:`read_task_spec_from_path`.
    """
    return read_task_spec_from_path(TASKS_DIR / task_id / "spec.yaml", task_id)


def render_task_spec(spec: TaskSpec | None) -> str:
    """Render a :class:`TaskSpec` into prompt text (pure). ``""`` for ``None``."""
    if spec is None:
        return ""
    lines: list[str] = [f"Task: {spec.task_id}"]
    if spec.context:
        lines.append(f"\nContext:\n{spec.context}")
    if spec.contract:
        lines.append("\nContract (explicitly specified to the candidate):")
        lines += [f"- {c}" for c in spec.contract]
    if spec.acceptance_criteria:
        lines.append("\nAcceptance criteria:")
        lines += [f"- {c}" for c in spec.acceptance_criteria]
    if spec.expected_ai_pitfalls:
        lines.append(
            "\nExpected AI pitfalls (employer-only ground truth — traps a weak "
            "candidate or an over-confident AI tends to fall into):"
        )
        for p in spec.expected_ai_pitfalls:
            sev = f" [severity: {p.severity}]" if p.severity else ""
            lines.append(f"- {p.description}{sev}")
            if p.detection:
                lines.append(f"  detection: {p.detection}")
    if spec.failure_signal:
        lines.append("\nFailure signals:")
        lines += [f"- {s}" for s in spec.failure_signal]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


@dataclass
class TranscriptStats:
    """What the transcript renderer produced/dropped (for truncation logging)."""

    rendered: int = 0
    skipped: int = 0
    truncated: int = 0
    dropped_over_budget: int = 0
    # Of `rendered`, how many are pure lifecycle (session start/end) lines — they
    # don't count as candidate activity for the near-empty check.
    lifecycle_rendered: int = 0
    # Of `rendered`, assistant_message lines emitted before the candidate's first
    # user_message — the auto-greeting AgentHost posts on session start. The AI
    # never speaks unprompted otherwise, so this isn't candidate activity either
    # (otherwise the welcome alone masks every no-op session).
    welcome_rendered: int = 0


def _clip(text: str, cap: int) -> tuple[str, bool]:
    """Truncate ``text`` to ``cap`` chars; return ``(text, was_truncated)``."""
    if len(text) <= cap:
        return text, False
    return text[:cap] + "… [truncated]", True


def _summarise_tool_input(payload: dict[str, Any]) -> str:
    """One-line summary of a tool_call input (file path or a short arg blob)."""
    inp = payload.get("input")
    if isinstance(inp, dict):
        path = inp.get("file_path") or inp.get("path")
        if isinstance(path, str) and path:
            return path
        cmd = inp.get("command")
        if isinstance(cmd, str) and cmd:
            return cmd
    return ""


def render_transcript(
    events: list[tuple[int, str, dict[str, Any]]],
    *,
    per_event_cap: int = PER_EVENT_CHAR_CAP,
    total_budget: int = TRANSCRIPT_CHAR_BUDGET,
) -> tuple[str, TranscriptStats]:
    """Render seq-ordered ``(seq, type, payload)`` events into prompt text (pure).

    Each rendered line is prefixed ``[seq]`` so a finding can cite ``event_seq``
    ``command_output`` chunks are folded into their ``command_finished``
    line (grouped by ``command_id``). Per-event content is clipped to
    ``per_event_cap``; once the accumulated text exceeds ``total_budget`` the
    remaining events are dropped and counted in the returned stats.
    """
    stats = TranscriptStats()

    # Pre-pass: gather command_output text per command_id (folded into finished).
    cmd_output: dict[str, list[str]] = {}
    for _seq, etype, payload in events:
        if etype == "command_output" and isinstance(payload, dict):
            cid = payload.get("command_id")
            data = payload.get("data")
            if isinstance(cid, str) and isinstance(data, str):
                cmd_output.setdefault(cid, []).append(data)

    lines: list[str] = []
    used = 0
    budget_hit = False
    seen_user_message = False

    for seq, etype, payload in events:
        if not isinstance(payload, dict):
            payload = {}
        if etype == "command_output":
            continue  # folded into command_finished
        if etype == "file_changed" and payload.get("source") == "baseline":
            stats.skipped += 1
            continue
        if etype not in _RENDERED_TYPES:
            stats.skipped += 1
            continue

        result = _render_event(seq, etype, payload, cmd_output, per_event_cap)
        if result is None:
            stats.skipped += 1
            continue
        line, truncated = result

        if budget_hit or used + len(line) > total_budget:
            budget_hit = True
            stats.dropped_over_budget += 1
            continue

        # Account only for events that actually made it into the output, so the
        # truncation log reflects what was rendered (no phantom truncations for
        # events dropped over budget).
        lines.append(line)
        used += len(line) + 1
        stats.rendered += 1
        if truncated:
            stats.truncated += 1
        if etype in ("session_start", "session_end"):
            stats.lifecycle_rendered += 1
        elif etype == "assistant_message" and not seen_user_message:
            stats.welcome_rendered += 1
        if etype == "user_message":
            seen_user_message = True

    if stats.dropped_over_budget:
        logger.info(
            "render_transcript: total budget %d chars hit — dropped %d trailing "
            "event(s); rendered %d, truncated %d",
            total_budget,
            stats.dropped_over_budget,
            stats.rendered,
            stats.truncated,
        )

    return "\n".join(lines), stats


def _render_event(
    seq: int,
    etype: str,
    payload: dict[str, Any],
    cmd_output: dict[str, list[str]],
    cap: int,
) -> tuple[str, bool] | None:
    """Render one event to ``(line, was_truncated)``, or ``None`` to skip it.

    Pure — does not touch stats; the caller accounts for truncation only when the
    line actually survives the budget (see :func:`render_transcript`).
    """
    prefix = f"[{seq}]"

    if etype == "session_start":
        return f"{prefix} session started (task={payload.get('task_id', '?')})", False

    if etype == "user_message":
        text, trunc = _clip(str(payload.get("text", "")), cap)
        return f"{prefix} candidate: {text}", trunc

    if etype == "assistant_message":
        text, trunc = _clip(str(payload.get("text", "")), cap)
        return f"{prefix} ai: {text}", trunc

    if etype == "tool_call":
        name = payload.get("tool_name", "?")
        arg_clip, trunc = _clip(_summarise_tool_input(payload), cap)
        return f"{prefix} ai called tool {name}({arg_clip})", trunc

    if etype == "tool_result":
        out, trunc = _clip(str(payload.get("output", "")), cap)
        err = " (error)" if payload.get("is_error") else ""
        return f"{prefix} tool result{err}: {out}", trunc

    if etype == "command_run":
        cmd, trunc = _clip(str(payload.get("command", "")), cap)
        return f"{prefix} candidate ran: {cmd}", trunc

    if etype == "command_finished":
        cid = payload.get("command_id")
        exit_code = payload.get("exit_code")
        out = "".join(cmd_output.get(cid, [])) if isinstance(cid, str) else ""
        out_clip, trunc = _clip(out.strip(), cap)
        tail = f"\n  output: {out_clip}" if out_clip else ""
        return f"{prefix} command finished (exit {exit_code}){tail}", trunc

    if etype == "file_changed":
        op = payload.get("op", "modified")
        path = payload.get("file_path", "?")
        source = payload.get("source", "?")
        return f"{prefix} file {op}: {path} (by {source})", False

    if etype == "diff_rejected":
        reason, trunc = _clip(str(payload.get("reason", "")), cap)
        line = f"{prefix} candidate rejected an AI diff"
        if reason:
            line += f": {reason}"
        return line, trunc

    if etype == "session_end":
        return f"{prefix} session ended (reason={payload.get('reason', '?')})", False

    return None


async def build_assessment_transcript(
    session_code: str,
) -> tuple[str, TranscriptStats]:
    """DB wrapper: read the session's events (seq-ordered) and render them."""
    from sqlalchemy import select

    from vibony_backend.db.engine import get_sessionmaker
    from vibony_backend.db.models import Event
    from vibony_backend.db.models import Session as DbSession

    sm = get_sessionmaker()
    async with sm() as db:
        rows = (
            await db.execute(
                select(Event.seq, Event.type, Event.payload)
                .join(DbSession, Event.session_id == DbSession.id)
                .where(DbSession.code == session_code)
                .order_by(Event.seq)
                .limit(MAX_TRANSCRIPT_EVENTS + 1)  # +1 to detect the cap being hit
            )
        ).all()
    if len(rows) > MAX_TRANSCRIPT_EVENTS:
        rows = rows[:MAX_TRANSCRIPT_EVENTS]
        logger.warning(
            "build_assessment_transcript: session %s exceeds %d events; "
            "assessing the first %d only",
            session_code,
            MAX_TRANSCRIPT_EVENTS,
            MAX_TRANSCRIPT_EVENTS,
        )
    events = [(seq, etype, payload) for seq, etype, payload in rows]
    return render_transcript(events)


# ---------------------------------------------------------------------------
# Final code
# ---------------------------------------------------------------------------


@dataclass
class CodeStats:
    """What the final-code renderer produced/dropped (for truncation logging)."""

    files: int = 0
    truncated: int = 0
    dropped_over_budget: int = 0


def render_final_code(
    files: dict[str, str | None],
    *,
    per_file_cap: int = PER_FILE_CHAR_CAP,
    total_budget: int = FINAL_CODE_CHAR_BUDGET,
) -> tuple[str, CodeStats]:
    """Render the ``{path: content}`` projection into prompt text (pure).

    Deleted paths (``content is None``) are skipped — only the surviving final
    files are shown. Each file is clipped to ``per_file_cap``; once the total
    exceeds ``total_budget`` the remaining files are dropped (counted in stats).
    Paths are sorted for determinism.
    """
    stats = CodeStats()
    blocks: list[str] = []
    used = 0

    for path in sorted(files):
        content = files[path]
        if content is None:
            continue
        clipped, trunc = _clip(content, per_file_cap)
        stats.truncated += int(trunc)
        block = f"# {path}\n{clipped}"
        if used + len(block) > total_budget:
            stats.dropped_over_budget += 1
            continue
        blocks.append(block)
        used += len(block) + 1
        stats.files += 1

    if stats.dropped_over_budget:
        logger.info(
            "render_final_code: total budget %d chars hit — dropped %d file(s); "
            "rendered %d, truncated %d",
            total_budget,
            stats.dropped_over_budget,
            stats.files,
            stats.truncated,
        )

    return "\n\n".join(blocks), stats


async def build_final_code(session_code: str) -> tuple[str, CodeStats]:
    """DB wrapper: project final file state from the event log and render it."""
    files = await replay_file_state(session_code)
    return render_final_code(files)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass
class AssessmentInputs:
    """The bundled inputs the generation step folds into its prompt."""

    task_id: str
    spec: TaskSpec | None
    spec_text: str
    transcript: str
    transcript_stats: TranscriptStats
    final_code: str
    code_stats: CodeStats
    is_near_empty: bool


async def assemble_assessment_inputs(
    session_code: str, task_id: str, *, spec: TaskSpec | None
) -> AssessmentInputs:
    """Gather spec + transcript + final code for a session.

    ``spec`` is injected by the caller: the job resolves the DB task and
    parses it via :func:`parse_task_spec`. ``None`` when the task can't be resolved
    (deleted, near-empty assessment still works). Flags ``is_near_empty``
    when the candidate produced no conversation/command activity *and* no surviving
    code — the generation prompt then tells the model to say "insufficient activity
    to assess" rather than invent.
    """
    spec_text = render_task_spec(spec)
    transcript, t_stats = await build_assessment_transcript(session_code)
    final_code, c_stats = await build_final_code(session_code)

    # "Near-empty" = nothing but lifecycle/baseline events + the auto-greeting
    # rendered and no code. Counts come straight off the typed event stream — no
    # string round-trip. The welcome assistant_message is subtracted
    # so a genuine no-op session trips the sentinel instead of being masked by it.
    meaningful = (
        t_stats.rendered - t_stats.lifecycle_rendered - t_stats.welcome_rendered
    )
    is_near_empty = c_stats.files == 0 and meaningful <= 0
    if is_near_empty:
        logger.info(
            "assemble_assessment_inputs: near-empty session code=%s "
            "(no surviving code, no candidate activity)",
            session_code,
        )

    return AssessmentInputs(
        task_id=task_id,
        spec=spec,
        spec_text=spec_text,
        transcript=transcript,
        transcript_stats=t_stats,
        final_code=final_code,
        code_stats=c_stats,
        is_near_empty=is_near_empty,
    )


__all__ = [
    "AssessmentInputs",
    "CodeStats",
    "Pitfall",
    "TaskSpec",
    "TranscriptStats",
    "assemble_assessment_inputs",
    "build_assessment_transcript",
    "build_final_code",
    "parse_task_spec",
    "read_task_spec_from_disk",
    "render_final_code",
    "render_task_spec",
    "render_transcript",
]
