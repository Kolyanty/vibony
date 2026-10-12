"""Tests for AgentHost lifecycle and AgentHostRegistry.

Uses asyncio directly (no DB) — store_event is mocked so we test
host internals without a real Postgres.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Task seam: AgentHost resolves its task from the DB at start() via the
# module-level _load_loaded_task. These tests run without a DB, so we patch that
# seam to inject a fake LoadedTask (or None) — provisioning then builds the bundle
# from its starter_files and renders the weakness prompt from its weakness mapping.
# ---------------------------------------------------------------------------

from vibony_backend.tasks.store import LoadedTask  # noqa: E402


def _loaded_task(
    *,
    starter_files: list[dict] | None = None,
    image: str = "vibony/sandbox-py:3.12",
    weakness: dict | None = None,
) -> LoadedTask:
    return LoadedTask(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        slug="task-01-training-loop",
        title="Training loop",
        description_md="",
        starter_files=starter_files or [],
        runtime_image=image,
        weakness=weakness or {},
        spec={},
        visibility="public",
        is_template=True,
    )


_NO_ARG = object()


def _patch_task(loaded=_NO_ARG):
    """Patch the DB task seam. No arg → an empty-scaffold task; ``None`` → no task
    resolved (provisioning falls back to empty bundle + default image)."""
    if loaded is _NO_ARG:
        loaded = _loaded_task()
    return patch(
        "vibony_backend.agent_host.host._load_loaded_task",
        new_callable=AsyncMock,
        return_value=loaded,
    )


def _make_host(**kwargs):
    from vibony_backend.agent_host.host import AgentHost

    return AgentHost(
        session_id=kwargs.get("session_id", uuid.uuid4()),
        session_code=kwargs.get("session_code", "TEST-AAAA"),
        task_id=kwargs.get("task_id", "task-01-training-loop"),
        ai_config=kwargs.get(
            "ai_config",
            {"primary_model": "test-model"},
        ),
    )


# ---------------------------------------------------------------------------
# AgentHost.start()
# ---------------------------------------------------------------------------


async def test_start_emits_session_start_and_welcome():
    """start() must emit session_start followed by assistant_message (fresh session)."""
    emitted: list[tuple[str, dict]] = []

    async def fake_store_event(
        session_id, session_code, event_type, payload, **kwargs
    ):
        emitted.append((event_type, payload))
        return len(emitted)

    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            side_effect=fake_store_event,
        ),
        _patch_task(),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await host.start()

    assert len(emitted) == 2
    assert emitted[0][0] == "session_start"
    assert emitted[0][1]["task_id"] == "task-01-training-loop"
    assert emitted[1][0] == "assistant_message"
    assert isinstance(emitted[1][1]["text"], str)
    assert emitted[1][1]["text"]  # non-empty welcome


async def test_emit_assistant_sink_drops_blank_text():
    """Defense-in-depth: AgentHost._emit_assistant suppresses an
    empty/whitespace assistant_message even if chat_turn ever hands it one. The
    user_message is still emitted; only the blank bubbles are dropped."""
    emitted: list[tuple[str, dict]] = []

    async def fake_store_event(session_id, session_code, event_type, payload, **kwargs):
        emitted.append((event_type, payload))
        return len(emitted)

    async def fake_chat_turn(*, emit_assistant_message, **_kwargs):
        await emit_assistant_message("", None)
        await emit_assistant_message("   \n", None)
        await emit_assistant_message("real answer", None)

    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            side_effect=fake_store_event,
        ),
        _patch_task(),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await host.start()
        emitted.clear()  # drop session_start + welcome
        with patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn):
            await host.submit_user_message("hi")

    types = [e[0] for e in emitted]
    assert types == ["user_message", "assistant_message"]
    assert emitted[1][1]["text"] == "real answer"


async def test_cold_start_of_existing_session_skips_welcome():
    """Cold-start of an existing session (has_assistant_message=True) must not
    emit a second assistant_message, only session_start."""
    emitted: list[tuple[str, dict]] = []

    async def fake_store_event(
        session_id, session_code, event_type, payload, **kwargs
    ):
        emitted.append((event_type, payload))
        return len(emitted)

    host = _make_host()

    prior_messages = [
        {"role": "user", "content": "Let's go"},
        {"role": "assistant", "content": "Sure, starting now."},
    ]

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            side_effect=fake_store_event,
        ),
        _patch_task(),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=prior_messages,
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await host.start()

    event_types = [e[0] for e in emitted]
    assert "session_start" in event_types
    assert "assistant_message" not in event_types, (
        "Cold-start must not emit a duplicate welcome message"
    )
    assert host._messages == prior_messages


async def test_start_is_idempotent():
    """Calling start() twice must not duplicate events."""
    emitted: list[str] = []

    async def fake_store_event(session_id, session_code, event_type, payload, **kwargs):
        emitted.append(event_type)
        return len(emitted)

    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            side_effect=fake_store_event,
        ),
        _patch_task(),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await host.start()
        await host.start()  # second call is no-op

    # Only 2 events from the first call, not 4.
    assert len(emitted) == 2


async def test_start_emits_baseline_file_changed_per_scaffold_file():
    """Fresh session: each starter file gets a baseline file_changed
    (source=baseline, op=created) after session_start, before the welcome message.

    starter_files are stored as text (JSONB), so there are no binary
    scaffold files to skip at this layer; the bundle is built from the DB task."""
    emitted: list[tuple[str, dict]] = []

    async def fake_store_event(session_id, session_code, event_type, payload, **kwargs):
        emitted.append((event_type, payload))
        return len(emitted)

    task = _loaded_task(
        starter_files=[
            {"path": "train.py", "content": "import torch\n"},
            {"path": "data.py", "content": "x = 1\n"},
        ]
    )

    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store_event),
        _patch_task(task),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await host.start()

    types = [t for t, _ in emitted]
    assert types[0] == "session_start"
    assert types[-1] == "assistant_message"

    baselines = [p for t, p in emitted if t == "file_changed"]
    assert {p["file_path"] for p in baselines} == {"train.py", "data.py"}
    for p in baselines:
        assert p["source"] == "baseline"
        assert p["op"] == "created"
        assert p["diff"] is None
        assert isinstance(p["content"], str)
    by_path = {p["file_path"]: p["content"] for p in baselines}
    assert by_path["train.py"] == "import torch\n"

    # Ordering: baselines sit between session_start and the welcome message.
    first_baseline = types.index("file_changed")
    assert first_baseline > 0
    assert first_baseline < types.index("assistant_message")


async def test_resumed_session_does_not_re_emit_baseline():
    """Cold-start of an existing session must not re-emit baseline events —
    they already live in the log and would clobber edits on replay."""
    emitted: list[tuple[str, dict]] = []

    async def fake_store_event(session_id, session_code, event_type, payload, **kwargs):
        emitted.append((event_type, payload))
        return len(emitted)

    task = _loaded_task(
        starter_files=[{"path": "train.py", "content": "import torch\n"}]
    )

    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store_event),
        _patch_task(task),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[{"role": "user", "content": "hi"}],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await host.start()

    assert not any(t == "file_changed" for t, _ in emitted)


async def test_resolve_task_missing_falls_back_to_global_prompt():
    """When no DB task resolves, _resolve_task must still set a valid global-only
    weakness prompt (no traps) so a chat turn never crashes on a missing task."""
    host = _make_host()
    with _patch_task(None):
        await host._resolve_task()
    assert host._loaded_task is None
    assert host._system_prompt  # non-empty: the global preamble rendered
    assert "## Blind spots" not in host._system_prompt  # empty weakness → no traps section


async def test_resolve_task_renders_weakness_from_db_task():
    """A resolved DB task's weakness mapping is rendered into the system prompt."""
    host = _make_host()
    task = _loaded_task(weakness={"blindspots": [{"issue": "MARKER-checkpoint-bug"}]})
    with _patch_task(task):
        await host._resolve_task()
    assert host._loaded_task is task
    assert "MARKER-checkpoint-bug" in (host._system_prompt or "")


# ---------------------------------------------------------------------------
# AgentHost.terminate()
# ---------------------------------------------------------------------------


async def test_terminate_removes_workdir():
    """terminate() must delete the tempdir created by start()."""
    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            new_callable=AsyncMock,
            return_value=1,
        ),
        _patch_task(),
    ):
        await host.start()

    workdir = host._workdir
    assert workdir is not None
    assert workdir.exists()

    await host.terminate()

    assert not workdir.exists()
    assert host._workdir is None


async def test_terminate_is_idempotent():
    """Double terminate() must not raise."""
    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            new_callable=AsyncMock,
            return_value=1,
        ),
        _patch_task(),
    ):
        await host.start()

    await host.terminate()
    await host.terminate()  # should not raise


async def test_terminate_without_start_is_safe():
    """terminate() before start() must be a no-op."""
    host = _make_host()
    await host.terminate()  # no exception


# ---------------------------------------------------------------------------
# AgentHostRegistry
# ---------------------------------------------------------------------------


async def test_registry_get_or_create_returns_same_host():
    """Two calls with the same code return the same AgentHost instance."""
    from vibony_backend.agent_host.host import AgentHostRegistry

    reg = AgentHostRegistry()
    session_id = uuid.uuid4()
    ai_config = {"primary_model": "test-model"}

    host_a, created_a, _ = await reg.get_or_create("CODE-0001", session_id, "task-01-training-loop", ai_config)
    host_b, created_b, _ = await reg.get_or_create("CODE-0001", session_id, "task-01-training-loop", ai_config)

    assert created_a is True
    assert created_b is False
    assert host_a is host_b


async def test_registry_grace_timer_terminates_host():
    """Grace timer fires after DISCONNECT_GRACE_SECONDS and calls terminate()."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original_grace = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 0  # fire immediately

    reg = AgentHostRegistry()
    session_id = uuid.uuid4()
    ai_config = {"primary_model": "test-model"}

    fake_host = MagicMock()
    fake_host.terminate = AsyncMock()

    reg._hosts["CODE-GRAC"] = fake_host
    reg._connect_epochs["CODE-GRAC"] = 1

    await reg.connection_closed("CODE-GRAC", 1)
    # Yield control so the grace task can execute.
    await asyncio.sleep(0.05)

    fake_host.terminate.assert_awaited_once()
    assert reg.get("CODE-GRAC") is None

    host_module.DISCONNECT_GRACE_SECONDS = original_grace


# ---------------------------------------------------------------------------
# AgentHost — sandbox provider selection
# ---------------------------------------------------------------------------


async def test_start_uses_local_toolset_by_default():
    """With VIBONY_SANDBOX_PROVIDER unset (default 'local'), BackendLocalToolset is used."""
    from vibony_backend.agent_host.local_toolset import BackendLocalToolset

    host = _make_host()

    with (
        patch.dict("os.environ", {}, clear=False),  # don't set VIBONY_SANDBOX_PROVIDER
        patch(
            "vibony_backend.agent_host.host.store_event",
            new_callable=AsyncMock,
            return_value=1,
        ),
        _patch_task(),
    ):
        # Ensure env var is absent (it may have been set globally by another test).
        import os as _os
        _os.environ.pop("VIBONY_SANDBOX_PROVIDER", None)
        await host.start()

    assert isinstance(host._toolset, BackendLocalToolset)
    assert host._sandbox_handle is None


async def test_start_with_docker_provider_calls_create():
    """With VIBONY_SANDBOX_PROVIDER=docker, DockerSandboxProvider.create is called
    and the resulting handle is stored on the host."""
    from unittest.mock import patch as _patch
    from vibony_backend.sandbox.docker_provider import SandboxHandle

    fake_handle = SandboxHandle(sandbox_id="fake-abc123")
    fake_provider = AsyncMock()
    fake_provider.create = AsyncMock(return_value=fake_handle)

    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "docker"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            new_callable=AsyncMock,
            return_value=1,
        ),
        _patch_task(),
        # Patch the class in its source module so the lazy import inside start() picks it up.
        _patch(
            "vibony_backend.sandbox.docker_provider.DockerSandboxProvider",
            return_value=fake_provider,
        ),
    ):
        await host.start()

    fake_provider.create.assert_awaited_once()
    assert host._sandbox_handle is fake_handle


async def test_provision_docker_threads_runtime_image_and_limits():
    """_provision_docker uses the DB task's runtime_image and passes
    pids/nofile resource limits into provider.create."""
    from vibony_backend.sandbox.docker_provider import SandboxHandle
    from vibony_backend.sandbox.config import SANDBOX_PIDS_LIMIT, SANDBOX_NOFILE

    fake_handle = SandboxHandle(sandbox_id="fake-xyz")
    fake_provider = AsyncMock()
    fake_provider.create = AsyncMock(return_value=fake_handle)

    # _patch_task() default LoadedTask pins runtime_image=vibony/sandbox-py:3.12.
    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "docker"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            new_callable=AsyncMock,
            return_value=1,
        ),
        _patch_task(),
        patch(
            "vibony_backend.sandbox.docker_provider.DockerSandboxProvider",
            return_value=fake_provider,
        ),
    ):
        await host.start()

    kwargs = fake_provider.create.call_args.kwargs
    assert kwargs["image"] == "vibony/sandbox-py:3.12"
    assert kwargs["pids_limit"] == SANDBOX_PIDS_LIMIT
    assert kwargs["nofile"] == SANDBOX_NOFILE


async def test_provision_docker_falls_back_to_default_image(monkeypatch):
    """When no DB task resolves (task gone), provisioning falls back to SANDBOX_IMAGE."""
    from vibony_backend.sandbox.docker_provider import SandboxHandle
    from vibony_backend.sandbox.config import SANDBOX_IMAGE

    fake_handle = SandboxHandle(sandbox_id="fake-fallback")
    fake_provider = AsyncMock()
    fake_provider.create = AsyncMock(return_value=fake_handle)

    host = _make_host()

    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "docker"}),
        patch(
            "vibony_backend.agent_host.host.store_event",
            new_callable=AsyncMock,
            return_value=1,
        ),
        _patch_task(None),  # task unresolved → empty bundle + default image
        patch(
            "vibony_backend.sandbox.docker_provider.DockerSandboxProvider",
            return_value=fake_provider,
        ),
    ):
        await host.start()

    assert fake_provider.create.call_args.kwargs["image"] == SANDBOX_IMAGE


async def test_registry_cancel_grace_prevents_terminate():
    """cancel_grace() within the grace window must keep the host alive."""
    from vibony_backend.agent_host import host as host_module
    from vibony_backend.agent_host.host import AgentHostRegistry

    original_grace = host_module.DISCONNECT_GRACE_SECONDS
    host_module.DISCONNECT_GRACE_SECONDS = 0.2  # 200 ms — enough to cancel

    reg = AgentHostRegistry()

    fake_host = MagicMock()
    fake_host.terminate = AsyncMock()

    reg._hosts["CODE-CNCL"] = fake_host
    reg._connect_epochs["CODE-CNCL"] = 1

    await reg.connection_closed("CODE-CNCL", 1)
    reg.cancel_grace("CODE-CNCL")   # cancel before grace fires
    await asyncio.sleep(0.3)        # wait past original grace period

    fake_host.terminate.assert_not_awaited()
    assert reg.get("CODE-CNCL") is fake_host

    host_module.DISCONNECT_GRACE_SECONDS = original_grace


# ---------------------------------------------------------------------------
# agent_status indicator + budget-aware LLM timeout
# ---------------------------------------------------------------------------


async def test_agent_status_published_around_turn():
    """A turn publishes agent_status `working` before chat_turn and `idle` after,
    so the candidate's "AI is working" indicator spans the whole turn."""
    statuses: list[str] = []

    async def fake_publish_advisory(code, msg):
        if isinstance(msg, dict) and msg.get("kind") == "agent_status":
            statuses.append(msg["state"])

    async def fake_store_event(*a, **k):
        return 1

    async def fake_chat_turn(**_kwargs):
        # the working hint must already be set while the turn runs
        assert statuses == ["working"]

    host = _make_host()
    with (
        patch.dict("os.environ", {"VIBONY_SANDBOX_PROVIDER": "local"}),
        patch("vibony_backend.agent_host.host.store_event", side_effect=fake_store_event),
        _patch_task(),
        patch(
            "vibony_backend.agent_host.host.load_agent_messages",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "vibony_backend.agent_host.host.replay_messages_for_agent",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "vibony_backend.agent_host.host.has_assistant_message",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "vibony_backend.sessions.broker.broker.publish_advisory",
            new=fake_publish_advisory,
        ),
    ):
        await host.start()
        assert host.is_turn_in_flight() is False
        with patch("vibony_backend.agent_host.host.chat_turn", side_effect=fake_chat_turn):
            await host.submit_user_message("hi")

    assert statuses == ["working", "idle"]


async def test_resolve_llm_timeout_caps_at_async_remaining():
    """live/unarmed → None (proxy uses its ceiling); async → min(ceiling, remaining)
    floored at LLM_TIMEOUT_ASYNC_FLOOR_SECONDS."""
    from datetime import UTC, datetime, timedelta

    from vibony_backend.agent_host.host import LLM_TIMEOUT_ASYNC_FLOOR_SECONDS
    from vibony_backend.proxy import LLM_CALL_TIMEOUT_SECONDS
    from vibony_backend.sessions.async_timer import async_timer_registry

    host = _make_host(session_code="TMO-0001")
    try:
        assert host._resolve_llm_timeout() is None  # not an async session → no cap
        async_timer_registry._deadlines["TMO-0001"] = datetime.now(UTC) + timedelta(seconds=10_000)
        assert host._resolve_llm_timeout() == LLM_CALL_TIMEOUT_SECONDS  # capped at the ceiling
        async_timer_registry._deadlines["TMO-0001"] = datetime.now(UTC) + timedelta(seconds=50)
        t = host._resolve_llm_timeout()
        assert t is not None and 40 < t <= 50  # capped at remaining
        async_timer_registry._deadlines["TMO-0001"] = datetime.now(UTC) + timedelta(seconds=2)
        assert host._resolve_llm_timeout() == LLM_TIMEOUT_ASYNC_FLOOR_SECONDS  # floored
    finally:
        async_timer_registry._deadlines.pop("TMO-0001", None)

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.usefixtures("isolated_agent_storage")]
