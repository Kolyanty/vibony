"""Tests: assessment input assembly.

Two layers (mirrors test_agent_host_compaction):

1. Pure renderers — ``parse_task_spec`` / ``read_task_spec_from_disk`` / ``render_task_spec``
   / ``render_transcript`` / ``render_final_code``: seq annotation, command-output
   folding, baseline + service skips, per-event/per-file truncation, total-budget
   drop. No DB, no LLM.
2. DB wrappers — ``build_assessment_transcript`` / ``build_final_code`` /
   ``assemble_assessment_inputs`` against the real Postgres fixture (functions open
   their own session via the module sessionmaker, same pattern as
   test_async_timer's ``timer_db``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vibony_backend.assessment.inputs import (
    PER_EVENT_CHAR_CAP,
    Pitfall,
    TaskSpec,
    assemble_assessment_inputs,
    build_assessment_transcript,
    build_final_code,
    parse_task_spec,
    read_task_spec_from_disk,
    read_task_spec_from_path,
    render_final_code,
    render_task_spec,
    render_transcript,
)
from vibony_backend.db.models import Event
from vibony_backend.db.models import Session as DbSession

_session_loop = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Task spec (pure + real file)
# ---------------------------------------------------------------------------


def test_parse_task_spec_full():
    parsed = {
        "context": "  do the thing  ",
        "contract": ["a", "  b  ", "", 3],
        "acceptance_criteria": ["runs"],
        "expected_ai_pitfalls": [
            {"description": "only state_dict", "detection": "diverges", "severity": "blocking"},
            {"description": "  no clone  ", "severity": "degrading"},
            {"no_description": "skip me"},
            "not a dict",
        ],
        "failure_signal": ["accepted last weights"],
    }
    spec = parse_task_spec("task-x", parsed)
    assert spec is not None
    assert spec.context == "do the thing"
    assert spec.contract == ["a", "b"]  # blank + non-str dropped
    assert spec.acceptance_criteria == ["runs"]
    assert len(spec.expected_ai_pitfalls) == 2  # malformed entries dropped
    assert spec.expected_ai_pitfalls[0].severity == "blocking"
    assert spec.expected_ai_pitfalls[1].description == "no clone"
    assert spec.expected_ai_pitfalls[1].detection is None
    assert spec.failure_signal == ["accepted last weights"]


def test_parse_task_spec_trap_contract():
    """The controlled-trap contract fields are parsed (additive, blank→None)."""
    parsed = {
        "expected_ai_pitfalls": [
            {
                "description": "only state_dict",
                "trigger": "  candidate asks to implement checkpoint  ",
                "flawed_behavior": "saves model only",
                "correct_behavior": "names missing optimizer state → AI adds it",
                "defense": "  ",  # blank → None
                "check": "run resume; logs must not repeat epoch 00",
                "severity": "blocking",
            },
            {"description": "legacy pitfall, no contract"},
        ],
    }
    spec = parse_task_spec("task-x", parsed)
    assert spec is not None
    p0 = spec.expected_ai_pitfalls[0]
    assert p0.trigger == "candidate asks to implement checkpoint"  # stripped
    assert p0.flawed_behavior == "saves model only"
    assert p0.correct_behavior == "names missing optimizer state → AI adds it"
    assert p0.defense is None  # blank coerced to None
    assert p0.check == "run resume; logs must not repeat epoch 00"
    # Legacy pitfall without the contract stays valid — all contract fields None.
    p1 = spec.expected_ai_pitfalls[1]
    assert (p1.trigger, p1.flawed_behavior, p1.correct_behavior, p1.defense, p1.check) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_parse_task_spec_trap_type():
    """The defect-mechanism tag is parsed (free-form, additive, blank→None)."""
    parsed = {
        "expected_ai_pitfalls": [
            {"description": "non-atomic decrement", "trap_type": "  concurrency/race  "},
            {"description": "no trap_type"},
            {"description": "blank trap_type", "trap_type": "   "},
        ],
    }
    spec = parse_task_spec("task-x", parsed)
    assert spec is not None
    assert spec.expected_ai_pitfalls[0].trap_type == "concurrency/race"  # stripped
    assert spec.expected_ai_pitfalls[1].trap_type is None  # absent
    assert spec.expected_ai_pitfalls[2].trap_type is None  # blank → None


def test_parse_task_spec_missing_fields():
    spec = parse_task_spec("task-x", {"id": "task-x"})
    assert spec is not None
    assert spec.context == ""
    assert spec.contract == []
    assert spec.expected_ai_pitfalls == []


def test_parse_task_spec_non_mapping():
    assert parse_task_spec("task-x", ["a", "b"]) is None
    assert parse_task_spec("task-x", None) is None


def test_read_task_spec_from_disk_training_loop_real_file():
    """task-01 has a complete spec.yaml — all assessment fields populate."""
    spec = read_task_spec_from_disk("task-01-training-loop")
    assert spec is not None
    assert spec.context
    assert spec.contract
    assert spec.acceptance_criteria
    assert len(spec.expected_ai_pitfalls) >= 4
    assert any(p.severity == "blocking" for p in spec.expected_ai_pitfalls)
    assert spec.failure_signal


def test_read_task_spec_from_disk_absent_returns_none(tmp_path, monkeypatch):
    """A task directory without spec.yaml returns None, without raising."""
    from vibony_backend.assessment import inputs

    (tmp_path / "minimal-task").mkdir()
    monkeypatch.setattr(inputs, "TASKS_DIR", tmp_path)
    assert read_task_spec_from_disk("minimal-task") is None


def test_read_task_spec_from_path(tmp_path):
    """Read a spec straight from an artifact path (not the repo tree)."""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "id: task-99\ncontext: demo\n"
        "expected_ai_pitfalls:\n  - description: oops\n    trap_type: boundary/off-by-one\n"
    )
    spec = read_task_spec_from_path(spec_path, "task-99")
    assert spec is not None
    assert spec.context == "demo"
    assert spec.expected_ai_pitfalls[0].trap_type == "boundary/off-by-one"
    # Absent / unparseable → None, never raises.
    assert read_task_spec_from_path(tmp_path / "nope.yaml", "task-99") is None
    assert read_task_spec_from_disk("task-does-not-exist") is None


def test_render_task_spec_none_is_empty():
    assert render_task_spec(None) == ""


def test_render_task_spec_includes_pitfalls_and_severity():
    spec = TaskSpec(
        task_id="task-x",
        context="ctx",
        contract=["c1"],
        acceptance_criteria=["a1"],
        expected_ai_pitfalls=[Pitfall("trap", detection="how", severity="blocking")],
        failure_signal=["fs1"],
    )
    out = render_task_spec(spec)
    assert "task-x" in out
    assert "ctx" in out
    assert "- c1" in out
    assert "- a1" in out
    assert "trap" in out
    assert "severity: blocking" in out
    assert "detection: how" in out
    assert "fs1" in out


# ---------------------------------------------------------------------------
# Transcript renderer (pure)
# ---------------------------------------------------------------------------


def test_render_transcript_ordering_and_seq_annotation():
    events = [
        (1, "session_start", {"task_id": "task-01-training-loop"}),
        (2, "user_message", {"text": "add checkpoint"}),
        (3, "assistant_message", {"text": "sure, editing train.py"}),
        (4, "tool_call", {"tool_name": "Edit", "input": {"file_path": "train.py"}}),
        (5, "tool_result", {"output": "ok", "is_error": False}),
        (6, "session_end", {"reason": "submitted"}),
    ]
    out, stats = render_transcript(events)
    lines = out.splitlines()
    assert lines[0].startswith("[1] session started")
    assert lines[1] == "[2] candidate: add checkpoint"
    assert lines[2] == "[3] ai: sure, editing train.py"
    assert "[4] ai called tool Edit(train.py)" in out
    assert "[5] tool result: ok" in out
    assert lines[-1] == "[6] session ended (reason=submitted)"
    assert stats.rendered == 6


def test_render_transcript_folds_command_output_into_finished():
    events = [
        (1, "command_run", {"command_id": "c1", "command": "python train.py"}),
        (2, "command_output", {"command_id": "c1", "stream": "stdout", "data": "epoch 00\n"}),
        (3, "command_output", {"command_id": "c1", "stream": "stdout", "data": "epoch 01\n"}),
        (4, "command_finished", {"command_id": "c1", "exit_code": 0}),
    ]
    out, stats = render_transcript(events)
    assert "[1] candidate ran: python train.py" in out
    # command_output events are not standalone lines.
    assert "[2]" not in out and "[3]" not in out
    assert "[4] command finished (exit 0)" in out
    assert "epoch 00" in out and "epoch 01" in out
    # 2 command_output events were folded, not rendered.
    assert stats.rendered == 2


def test_render_transcript_skips_baseline_and_service_events():
    events = [
        (1, "file_changed", {"file_path": "train.py", "source": "baseline", "content": "x"}),
        (2, "file_changed", {"file_path": "train.py", "source": "agent", "op": "modified"}),
        (3, "assessment_pending", {"reason": "submitted"}),
        (4, "sandbox_restored", {"file_count": 3}),
    ]
    out, stats = render_transcript(events)
    assert "[1]" not in out  # baseline skipped
    assert "[2] file modified: train.py (by agent)" in out
    assert "assessment_pending" not in out
    assert "sandbox_restored" not in out
    assert stats.rendered == 1
    assert stats.skipped == 3


def test_render_transcript_per_event_truncation():
    long_text = "z" * (PER_EVENT_CHAR_CAP + 500)
    events = [(1, "user_message", {"text": long_text})]
    out, stats = render_transcript(events)
    assert "… [truncated]" in out
    assert stats.truncated == 1
    assert len(out) < len(long_text)


def test_render_transcript_total_budget_drops_trailing():
    # Each message ~120 chars; tiny budget keeps only the first, drops the rest.
    events = [(i, "user_message", {"text": "x" * 100}) for i in range(1, 11)]
    out, stats = render_transcript(events, total_budget=150)
    assert stats.rendered == 1
    assert stats.dropped_over_budget == 9
    assert "[1]" in out and "[2]" not in out


# ---------------------------------------------------------------------------
# Final code renderer (pure)
# ---------------------------------------------------------------------------


def test_render_final_code_skips_deleted_and_sorts():
    files = {
        "b.py": "print('b')",
        "a.py": "print('a')",
        "gone.py": None,
    }
    out, stats = render_final_code(files)
    assert out.index("# a.py") < out.index("# b.py")  # sorted
    assert "gone.py" not in out
    assert stats.files == 2


def test_render_final_code_per_file_truncation():
    files = {"big.py": "q" * 9000}
    out, stats = render_final_code(files, per_file_cap=100)
    assert "… [truncated]" in out
    assert stats.truncated == 1


def test_render_final_code_total_budget_drops_files():
    files = {f"f{i}.py": "y" * 200 for i in range(10)}
    out, stats = render_final_code(files, total_budget=250)
    assert stats.files == 1
    assert stats.dropped_over_budget == 9


# ---------------------------------------------------------------------------
# DB wrappers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def inputs_db(db_engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Point the module sessionmaker at the test engine; truncate around."""
    import vibony_backend.db.engine as eng

    async def _truncate() -> None:
        async with db_engine.begin() as conn:
            await conn.execute(text(
                "TRUNCATE TABLE session_assessments, events, sessions, memberships, "
                "oauth_identities, invites, users, companies RESTART IDENTITY CASCADE"
            ))

    await _truncate()
    eng._engine = db_engine
    eng._sessionmaker = async_sessionmaker(
        db_engine, expire_on_commit=False, class_=AsyncSession
    )
    yield eng._sessionmaker
    await _truncate()
    eng._engine = None
    eng._sessionmaker = None


async def _seed(
    sm: async_sessionmaker[AsyncSession],
    code: str,
    events: list[tuple[str, dict]],
) -> uuid.UUID:
    sid = uuid.uuid4()
    async with sm() as db:
        db.add(
            DbSession(
                id=sid,
                code=code,
                status="completed",
                task_id="task-01-training-loop",
                protocol_version=7,
                ai_config={"primary_model": "test"},
                candidate_token=str(uuid.uuid4()),
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )
        for i, (etype, payload) in enumerate(events, start=1):
            db.add(
                Event(
                    session_id=sid,
                    seq=i,
                    id=uuid.uuid4(),
                    type=etype,
                    client_timestamp=datetime.now(UTC),
                    payload=payload,
                )
            )
        await db.commit()
    return sid


@_session_loop
async def test_build_assessment_transcript_db(inputs_db):
    await _seed(
        inputs_db,
        "INP-TR01",
        [
            ("session_start", {"task_id": "task-01-training-loop"}),
            ("user_message", {"text": "hi"}),
            ("assistant_message", {"text": "hello"}),
            ("session_end", {"reason": "submitted"}),
        ],
    )
    transcript, stats = await build_assessment_transcript("INP-TR01")
    assert "[2] candidate: hi" in transcript
    assert stats.rendered == 4


@_session_loop
async def test_build_assessment_transcript_caps_event_rows(inputs_db, monkeypatch):
    """The transcript SELECT is row-capped so a session with a pathological
    event count can't OOM the backend. Events past the cap are dropped + logged."""
    import vibony_backend.assessment.inputs as inp

    monkeypatch.setattr(inp, "MAX_TRANSCRIPT_EVENTS", 3)
    await _seed(
        inputs_db,
        "INP-CAP01",
        [("user_message", {"text": f"msg {i}"}) for i in range(6)],
    )
    transcript, stats = await build_assessment_transcript("INP-CAP01")
    # Only the first 3 (chronological) rows are read → rendered.
    assert stats.rendered == 3
    assert "msg 0" in transcript
    assert "msg 5" not in transcript


@_session_loop
async def test_build_final_code_db(inputs_db):
    await _seed(
        inputs_db,
        "INP-FC01",
        [
            ("session_start", {"task_id": "task-01-training-loop"}),
            ("file_changed", {"file_path": "train.py", "source": "baseline", "content": "stub"}),
            ("file_changed", {"file_path": "train.py", "source": "agent", "op": "modified", "content": "final code"}),
        ],
    )
    code, stats = await build_final_code("INP-FC01")
    assert "# train.py" in code
    assert "final code" in code
    assert stats.files == 1


@_session_loop
async def test_assemble_inputs_near_empty_flag(inputs_db):
    """A session with only lifecycle events + no code → is_near_empty True."""
    await _seed(
        inputs_db,
        "INP-EMPTY",
        [
            ("session_start", {"task_id": "task-01-training-loop"}),
            ("session_end", {"reason": "timeout"}),
        ],
    )
    inputs = await assemble_assessment_inputs(
        "INP-EMPTY", "task-01-training-loop", spec=read_task_spec_from_disk("task-01-training-loop")
    )
    assert inputs.is_near_empty is True
    assert inputs.spec is not None  # task-01 spec injected
    assert inputs.final_code == ""


@_session_loop
async def test_assemble_inputs_near_empty_with_welcome(inputs_db):
    """The auto-emitted welcome assistant_message (before any candidate
    user_message) must NOT count as activity — a no-op session that only got the
    greeting is still near-empty, so the honesty sentinel actually fires."""
    await _seed(
        inputs_db,
        "INP-WELC",
        [
            ("session_start", {"task_id": "task-01-training-loop"}),
            ("assistant_message", {"text": "Hello! I'm your AI assistant. How can I help?"}),
            ("session_end", {"reason": "timeout"}),
        ],
    )
    transcript, stats = await build_assessment_transcript("INP-WELC")
    assert stats.welcome_rendered == 1
    inputs = await assemble_assessment_inputs(
        "INP-WELC", "task-01-training-loop", spec=read_task_spec_from_disk("task-01-training-loop")
    )
    assert inputs.is_near_empty is True


def test_render_transcript_welcome_only_before_first_user_message():
    """A second assistant turn AFTER a candidate message is real activity — only
    the leading greeting is counted as welcome."""
    transcript, stats = render_transcript([
        (1, "session_start", {"task_id": "t"}),
        (2, "assistant_message", {"text": "Hello! How can I help?"}),
        (3, "user_message", {"text": "add a checkpoint"}),
        (4, "assistant_message", {"text": "Sure, editing train.py"}),
        (5, "session_end", {"reason": "submitted"}),
    ])
    assert stats.welcome_rendered == 1
    assert stats.lifecycle_rendered == 2


@_session_loop
async def test_assemble_inputs_near_empty_ignores_lifecycle_wording(inputs_db):
    """A candidate message that *mentions* lifecycle wording must not be miscounted
    as a lifecycle line — the session is active, so is_near_empty stays False."""
    await _seed(
        inputs_db,
        "INP-WORD",
        [
            ("session_start", {"task_id": "task-01-training-loop"}),
            ("user_message", {"text": "when did the session started? remind me"}),
            ("session_end", {"reason": "timeout"}),
        ],
    )
    inputs = await assemble_assessment_inputs(
        "INP-WORD", "task-01-training-loop", spec=read_task_spec_from_disk("task-01-training-loop")
    )
    assert inputs.is_near_empty is False


@_session_loop
async def test_assemble_inputs_active_session_not_empty(inputs_db):
    await _seed(
        inputs_db,
        "INP-ACT",
        [
            ("session_start", {"task_id": "task-01-training-loop"}),
            ("user_message", {"text": "add checkpoint"}),
            ("file_changed", {"file_path": "train.py", "source": "agent", "op": "modified", "content": "code"}),
            ("session_end", {"reason": "submitted"}),
        ],
    )
    inputs = await assemble_assessment_inputs(
        "INP-ACT", "task-01-training-loop", spec=read_task_spec_from_disk("task-01-training-loop")
    )
    assert inputs.is_near_empty is False
    assert "add checkpoint" in inputs.transcript
    assert "code" in inputs.final_code
