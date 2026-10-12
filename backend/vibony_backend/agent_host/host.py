"""AgentHost: per-session web runtime agent.

One instance per active web session. Keyed by session_code in AgentHostRegistry.

Lifecycle:
  start()               — provision sandbox (docker) or tempdir (local), extract
                          task bundle, emit session_start and welcome message.
  submit_user_message() — run one chat_turn (LLM + tool dispatches).
  terminate()           — tear down sandbox / tempdir; called on grace expiry.

VIBONY_SANDBOX_PROVIDER env selects the backend:
  "local"  (default) — BackendLocalToolset over a tempdir (safe for tests)
  "docker"           — DockerSandboxProvider + SandboxToolset
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
import logging
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from io import BytesIO
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from vibony_backend.llm_usage import session_usage, UsageLimitExceeded
from vibony_backend.sessions.runtime_store import PersistenceError, load_runtime, save_runtime
from vibony_backend.tasks.store import LoadedTask, loaded_from_snapshot
from .controller import ControllerHook
from vibony_backend.sessions import PROTOCOL_VERSION
from vibony_backend.sessions.bundle import build_bundle_from_files
from vibony_backend.sessions.event_store import (
    has_assistant_message,
    load_agent_messages,
    replay_file_state,
    replay_messages_for_agent,
    save_agent_messages,
    store_event,
)

from .bash_gate import classify_bash, extract_prefix, is_prefix_allowed
from .compaction import HISTORY_TOKEN_BUDGET, compact_messages
from .local_toolset import BackendLocalToolset
from .loop import (
    SESSION_MAX_LLM_CALLS,
    SESSION_MAX_TOKENS,
    SessionBudget,
    chat_turn,
)

logger = logging.getLogger(__name__)

# How long the terminal transition waits for an in-flight turn to finish cleanly
# before cancelling it. Bounds the wait so a wedged LLM call can't hang
# session_submit / async-timeout / interviewer-end indefinitely.
QUIESCE_TIMEOUT_SECONDS = 30.0
MAX_PENDING_FILE_WRITES = 8

# Floor for the per-call LLM timeout derived from the remaining async
# budget — a call near the deadline still gets a brief chance to return rather than
# an instant-timeout (it may overrun the deadline by up to this much, which quiesce
# then absorbs). Below this we let the deadline + quiesce do the cancelling.
LLM_TIMEOUT_ASYNC_FLOOR_SECONDS = 15.0


def _empty_tgz() -> bytes:
    import io as _io
    buf = _io.BytesIO()
    import tarfile as _tf
    with _tf.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


async def _load_loaded_task(session_id: uuid.UUID) -> LoadedTask | None:
    """Resolve the DB task a session runs. Module-level so it's a single
    patch point for tests (which run without a DB). Returns a
    ``tasks.store.LoadedTask`` or None."""
    from vibony_backend.db.engine import get_sessionmaker
    from vibony_backend.db.models import Session as DbSession
    from vibony_backend.tasks.store import resolve_for_session

    sm = get_sessionmaker()
    async with sm() as db:
        session = await db.get(DbSession, session_id)
        if session is None:
            return None
        return await resolve_for_session(db, session)


class BackendEventSink:
    """EventSink that writes events to DB and publishes to broker."""

    def __init__(self, session_id: uuid.UUID, session_code: str) -> None:
        self._session_id = session_id
        self._session_code = session_code

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            await store_event(
                self._session_id,
                self._session_code,
                event_type,
                payload,
            )
        except Exception as exc:
            raise PersistenceError(f"Could not persist {event_type}") from exc


class AgentHost:
    """Backend agent loop bound to one web session."""

    def __init__(
        self,
        session_id: uuid.UUID,
        session_code: str,
        task_id: str,
        ai_config: dict[str, Any],
    ) -> None:
        self._session_id = session_id
        self._session_code = session_code
        self._task_id = task_id
        self._ai_config = ai_config
        self._model: str | None = ai_config.get("primary_model")
        # The DB task this session runs, resolved once in start(). The
        # weakness system prompt is rendered from it once and passed into each
        # chat_turn — no DB hit per LLM iteration. None when the task can't be
        # resolved (then the host uses the global-only prompt).
        self._loaded_task: LoadedTask | None = None
        self._system_prompt: str | None = None
        # Per-session trap-control gate. None = controller off
        # (VIBONY_CONTROLLER_MODEL unset or the task has no policeable contracts) →
        # chat_turn behaves exactly as before. Built in _resolve_task.
        self._controller: ControllerHook | None = None
        self._runtime_state: dict[str, Any] = {}

        self._sink = BackendEventSink(session_id, session_code)
        self._toolset: Any | None = None  # BackendLocalToolset | SandboxToolset
        self._workdir: Path | None = None  # local mode only
        self._sandbox_provider: Any | None = None  # DockerSandboxProvider | None
        self._sandbox_handle: Any | None = None  # SandboxHandle | None
        # Raw task bundle laid down at provision; consumed once by
        # _emit_task_baseline() to seed File History diff baselines.
        self._bundle_tar: bytes | None = None
        self._messages: list[dict[str, Any]] = []
        self._is_resumed: bool = False
        self._started = False
        self._terminated = False
        # Cleared by quiesce() so the terminal transition
        # (session_submit / async timeout / interviewer end) can refuse new turns
        # while it drains the in-flight one, before claiming the session completed.
        self._accepting = True
        # The WebFsProxy this host registered. _teardown_sandbox
        # unregisters by identity so a stale teardown can't evict a fresh host's
        # proxy registered under the same session_code after a grace+reconnect.
        self._fs_proxy: Any | None = None  # WebFsProxy | None
        # Serialise agent turns — one LLM round-trip at a time. Also held by
        # reset_sandbox, terminate() and candidate inline-bash so every
        # sandbox-mutating / teardown op is mutually exclusive.
        self._turn_lock = asyncio.Lock()
        # Assistant soft-stop counters are checkpointed. The authoritative quota
        # in llm_usage.py reserves calls for all roles across host recreation.
        self._budget = SessionBudget(SESSION_MAX_LLM_CALLS, SESSION_MAX_TOKENS)
        # The single in-flight turn / inline-bash task. The recv
        # loop spawns via start_user_message / start_inline_bash (single-flight —
        # a second concurrent request is rejected, not piled up); terminate()
        # cancels these so a turn/exec stops once the session is terminal.
        self._turn_task: asyncio.Task[None] | None = None
        self._bash_task: asyncio.Task[None] | None = None
        self._file_tasks: set[asyncio.Task[None]] = set()
        # Inline bash gate state
        self._bash_prefixes: set[str] = set()
        self._pending_confirms: dict[str, asyncio.Future[str]] = {}
        # Direct WS send callback for bash_confirm_request (set by ws.py on connect).
        self._ws_send: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        # Set by registry when the previous grace timer expired before reconnect.
        self._sandbox_was_expired: bool = False

    async def _restore_messages(self) -> None:
        """Populate _messages from storage when resuming an existing session.

        Canonical path: the persisted message blob
        (``sessions.agent_messages``) is restored verbatim — lossless, including
        tool-call rounds. A populated blob means the session already had a turn,
        so _is_resumed=True (suppresses the welcome in _emit_start_events).

        Legacy fallback (older sessions with no blob): reconstruct the lossy
        user/assistant history from the event log via replay_messages_for_agent
        — tool rounds are dropped, but it keeps old sessions resumable.

        No-op if the session has no history. Storage failures stop startup.
        """
        try:
            blob = await load_agent_messages(self._session_code)
            if blob is not None:
                # A persisted blob (even an empty one) means the session already
                # ran a turn — restore it verbatim and treat as resumed. NULL (no
                # column value) is the only signal to fall back to legacy replay.
                self._messages = blob
                self._is_resumed = True
                logger.info(
                    "AgentHost._restore_messages: restored %d messages from blob for code=%s",
                    len(blob),
                    self._session_code,
                )
                return

            restored = await replay_messages_for_agent(self._session_code)
            if restored:
                self._messages = restored
                logger.info(
                    "AgentHost._restore_messages: restored %d messages (legacy replay) for code=%s",
                    len(restored),
                    self._session_code,
                )
            # Short-circuit: if conversation history was restored, there's
            # definitely an existing session; skip the extra DB query.
            self._is_resumed = bool(restored) or await has_assistant_message(self._session_code)
        except Exception as exc:
            raise PersistenceError("Could not restore conversation") from exc

    async def _persist_messages(self) -> None:
        """Persist the live LLM message blob after a completed turn.

        Canonical artifact for LLM continuation — restored verbatim on cold-start
        by _restore_messages so tool-call rounds survive a backend restart.
        Storage failures stop the host; continuing would lose session evidence.
        """
        try:
            await save_agent_messages(self._session_code, self._messages)
        except Exception as exc:
            raise PersistenceError("Could not save conversation") from exc

    async def _checkpoint_runtime(self) -> None:
        """Persist private control state before exposing actions to the candidate."""
        state = self._runtime_state
        state["version"] = 1
        state["assistant_model"] = self._model
        state["system_prompt"] = self._system_prompt
        state["controller_model"] = self._controller.model if self._controller else None
        state["assistant_budget"] = {"calls": self._budget.llm_calls, "tokens": self._budget.total_tokens}
        if self._loaded_task:
            state["task"] = json.loads(json.dumps(asdict(self._loaded_task), default=str))
        if self._controller:
            saved = asdict(self._controller.state)
            saved["all_indices"] = sorted(saved["all_indices"])
            saved["unlocked"] = sorted(saved["unlocked"])
            state["controller"] = saved
        try:
            await save_runtime(self._session_code, state)
        except Exception as exc:
            raise PersistenceError("Could not save agent state") from exc

    async def _maybe_compact_messages(self) -> None:
        """Summarise the old part of the conversation when over budget.

        Called after a clean turn, under ``_turn_lock``, before persisting. Caps
        unbounded growth of ``_messages`` on long async sessions by folding the
        oldest turns into a running summary (kept at the head of the blob). The
        weakness system prompt and the task traps are not in ``_messages`` — they
        are re-injected by ``call_llm`` every turn — so compaction can never make
        the agent forget its role. Errors are swallowed: a failed summary call
        must never break the candidate's turn (we just stay over budget).
        """
        try:
            # compact_messages is a no-op (returns None) when under budget, so no
            # separate pre-check is needed here.
            compacted = await compact_messages(self._messages, HISTORY_TOKEN_BUDGET)
            if compacted is not None:
                before = len(self._messages)
                self._messages = compacted
                logger.info(
                    "AgentHost._maybe_compact_messages: code=%s compacted %d → %d messages",
                    self._session_code,
                    before,
                    len(compacted),
                )
        except UsageLimitExceeded:
            return  # Keep the existing history when the session quota is spent.
        except PersistenceError:
            raise
        except Exception as exc:
            logger.warning(
                "AgentHost._maybe_compact_messages: failed for code=%s: %s",
                self._session_code,
                exc,
            )

    async def start(self) -> None:
        try:
            await self._start_session()
        except Exception:
            self._accepting = False
            self._runtime_state.setdefault("integrity_errors", []).append("agent_start_failed")
            try:
                await self._checkpoint_runtime()
            except PersistenceError:
                pass
            if self._ws_send:
                await self._ws_send({"kind": "error", "code": "AGENT_FAILED",
                    "message": "Session could not be started reliably. Start a new session."})
            raise

    async def _start_session(self) -> None:
        """Provision sandbox (docker mode) or tempdir (local mode), extract
        bundle, emit session_start + welcome message."""
        if self._started:
            return
        self._started = True

        self._runtime_state = await load_runtime(self._session_code) or {}
        if self._runtime_state.get("turn_in_progress") or self._runtime_state.get("fs_write_in_progress"):
            self._runtime_state.setdefault("integrity_errors", []).append("interrupted_turn")
            await save_runtime(self._session_code, self._runtime_state)
            raise PersistenceError("Previous agent turn was interrupted; start a new session")
        if self._runtime_state.get("integrity_errors"):
            raise PersistenceError("Session evidence is incomplete; start a new session")
        await self._restore_messages()
        await self._resolve_task()
        saved_controller = self._runtime_state.get("controller", {})
        if self._controller and saved_controller:
            state = self._controller.state
            state.apply_unlock(saved_controller.get("unlocked", []))
            for name in ("fail_opens", "review_retries", "verdict_repairs", "unresolved_rounds"):
                setattr(state, name, int(saved_controller.get(name, 0)))
        from vibony_backend.proxy import DEFAULT_MODEL
        self._model = self._runtime_state.get("assistant_model") or self._model or DEFAULT_MODEL
        budget = self._runtime_state.get("assistant_budget", {})
        self._budget.llm_calls = int(budget.get("calls", 0))
        self._budget.total_tokens = int(budget.get("tokens", 0))
        await self._checkpoint_runtime()

        sandbox_mode = os.getenv("VIBONY_SANDBOX_PROVIDER", "local")

        if sandbox_mode == "docker":
            await self._start_docker()
        else:
            # Fail closed: local mode runs candidate bash unsandboxed
            # on the host. Refuse unless explicitly allowed (dev/test only).
            from vibony_backend.sandbox import config as _sandbox_config

            _sandbox_config.ensure_local_sandbox_allowed(sandbox_mode)
            await self._start_local()

    async def _resolve_task(self) -> None:
        """Resolve the DB task this session runs + render its weakness prompt.

        Always leaves ``_system_prompt`` set to a valid prompt: the task's weakness
        when resolved, else the **global-only** prompt (no traps) so a chat turn
        never crashes on a missing/deleted task — a user-authored task has no
        filesystem dir, so the legacy ``render_weakness(task_id)`` fallback in
        ``call_llm`` would raise FileNotFoundError. ``_loaded_task`` stays None on
        miss (empty bundle + default image). Load/render failures stop startup.
        """
        from vibony_backend.weakness import render_weakness_dict

        loaded = None
        try:
            snapshot = self._runtime_state.get("task")
            if snapshot:
                loaded = loaded_from_snapshot(snapshot)
            else:
                loaded = await _load_loaded_task(self._session_id)
        except Exception as exc:
            logger.warning(
                "AgentHost._resolve_task: task load failed for code=%s: %s",
                self._session_code,
                exc,
            )
            raise PersistenceError("Could not load session task") from exc
        if loaded is not None:
            self._loaded_task = loaded
        else:
            logger.warning(
                "AgentHost._resolve_task: no DB task for code=%s task_id=%s "
                "(global-only prompt + empty bundle)",
                self._session_code,
                self._task_id,
            )
        try:
            self._system_prompt = self._runtime_state.get("system_prompt") or render_weakness_dict(
                loaded.weakness if loaded is not None else {}
            )
        except Exception as exc:
            # global.yaml unreadable — last resort: chat_turn falls back to the
            # filesystem render_weakness (deploy is broken if this fires).
            logger.warning(
                "AgentHost._resolve_task: weakness render failed for code=%s: %s",
                self._session_code,
                exc,
            )
            raise PersistenceError("Could not render session prompt") from exc

        # Build the trap-control gate from the task's spec contracts. No-op
        # (None) when no controller model is configured or the task has no policeable
        # contracts. Invalid configured control must not silently become disabled.
        try:
            self._controller = self._build_controller(loaded)
        except Exception as exc:
            logger.warning(
                "AgentHost._resolve_task: controller build failed for code=%s: %s",
                self._session_code,
                exc,
            )
            raise PersistenceError("Could not configure session controller") from exc

    def _build_controller(self, loaded: LoadedTask | None) -> ControllerHook | None:
        """Build the per-session controller hook from the DB task's spec.

        ``None`` when the controller model is unset (VIBONY_CONTROLLER_MODEL) or the
        task has no policeable trap contracts — the loop is then unchanged.
        """
        from vibony_backend.agent_host.controller import build_hook
        from vibony_backend.assessment.inputs import parse_task_spec
        from vibony_backend.prompt_seam import controller_model

        model = self._runtime_state.get("controller_model", controller_model())
        if not model or loaded is None:
            return None
        spec = parse_task_spec(self._task_id, loaded.spec)
        pitfalls = spec.expected_ai_pitfalls if spec is not None else []
        return build_hook(pitfalls, model)

    def controller_fail_opens(self) -> int:
        """How many times the trap controller failed open this session (a bad-JSON /
        transport error let a round through ungated). 0 when the controller is off.

        A benchmark reads this to EXCLUDE a run — a controller fail-open is a technical
        error that would otherwise silently become measured data."""
        ctrl = self._controller
        return ctrl.state.fail_opens if ctrl is not None else 0

    def controller_review_retries(self) -> int:
        """How many controller review calls needed a retry-with-nudge this session (a
        malformed / empty verdict on the first attempt). 0 when the controller is off.
        Reliability observability; surfaced in the sweep scorecard."""
        ctrl = self._controller
        return ctrl.state.review_retries if ctrl is not None else 0

    def controller_verdict_repairs(self) -> int:
        """How many controller verdicts the repair-validator salvaged in place this
        session (a partly-malformed reply that would otherwise have cost a retry /
        fail-open). 0 when the controller is off. Reliability observability."""
        ctrl = self._controller
        return ctrl.state.verdict_repairs if ctrl is not None else 0

    def _build_task_bundle(self) -> bytes | None:
        """Build the candidate bundle from the resolved DB task.

        None when no task resolved or the build fails — callers fall back to an
        empty archive (provisioning must not break on a bad task).
        """
        if self._loaded_task is None:
            return None
        try:
            tar_bytes, _ = build_bundle_from_files(
                self._loaded_task.starter_files,
                description_md=self._loaded_task.description_md,
                label=self._task_id,
            )
            return tar_bytes
        except Exception as exc:
            logger.warning(
                "AgentHost._build_task_bundle: bundle build failed for %s: %s",
                self._task_id,
                exc,
            )
            return None

    async def _start_local(self) -> None:
        """BackendLocalToolset over a tempdir."""
        await self._provision_local()
        await self._emit_start_events()
        logger.info(
            "AgentHost started (local): code=%s task=%s workdir=%s",
            self._session_code,
            self._task_id,
            self._workdir,
        )

    async def _provision_local(self) -> None:
        """Provision tempdir + BackendLocalToolset + WebFsProxy. No events emitted."""
        self._workdir = Path(tempfile.mkdtemp(prefix=f"vibony-{self._session_code}-"))
        self._toolset = BackendLocalToolset(self._sink, self._workdir)

        tar_bytes = self._build_task_bundle()
        if tar_bytes:
            self._bundle_tar = tar_bytes
            try:
                with tarfile.open(fileobj=BytesIO(tar_bytes), mode="r:gz") as tf:
                    tf.extractall(self._workdir, filter="data")
            except Exception as exc:
                logger.warning(
                    "AgentHost._provision_local: bundle extraction failed for %s: %s",
                    self._task_id,
                    exc,
                )

        from vibony_backend.sessions.fs_proxy import WebFsProxy, web_fs_proxy_registry
        self._fs_proxy = WebFsProxy(workdir=self._workdir)
        web_fs_proxy_registry.register(self._session_code, self._fs_proxy)

    async def _start_docker(self) -> None:
        """DockerSandboxProvider + SandboxToolset."""
        await self._provision_docker()
        logger.info(
            "AgentHost started (docker): code=%s task=%s container=%s",
            self._session_code,
            self._task_id,
            self._sandbox_handle.sandbox_id[:12],  # type: ignore[union-attr]
        )
        await self._emit_start_events()

    async def _provision_docker(self, *, restore: bool = True) -> None:
        """Provision DockerSandboxProvider + SandboxToolset + WebFsProxy. No events emitted.

        ``restore=True`` (cold start after a backend restart) replays the
        file_changed event log onto the fresh sandbox. ``reset_sandbox`` passes
        ``restore=False`` — a manual reset must land on clean task baseline, not
        re-apply the candidate's previous edits.
        """
        from vibony_backend.sandbox.config import (
            SANDBOX_CPU_LIMIT,
            SANDBOX_IMAGE,
            SANDBOX_MEM_LIMIT,
            SANDBOX_NOFILE,
            SANDBOX_PIDS_LIMIT,
        )
        from vibony_backend.sandbox.docker_provider import DockerSandboxProvider
        from vibony_backend.sandbox.toolset import SandboxToolset

        provider = DockerSandboxProvider()
        self._sandbox_provider = provider

        # Per-task image (deps baked) overrides the global default; absent → fallback.
        image = (
            self._loaded_task.runtime_image if self._loaded_task is not None else None
        ) or SANDBOX_IMAGE
        tar_bytes = self._build_task_bundle() or _empty_tgz()
        self._bundle_tar = tar_bytes

        handle = await provider.create(
            tar_bytes,
            "work",
            session_code=self._session_code,
            image=image,
            cpu_limit=SANDBOX_CPU_LIMIT,
            mem_limit=SANDBOX_MEM_LIMIT,
            pids_limit=SANDBOX_PIDS_LIMIT,
            nofile=SANDBOX_NOFILE,
        )
        self._sandbox_handle = handle
        self._toolset = SandboxToolset(provider, handle, self._sink, bash_gate_fn=self._gate_ai_bash)

        from vibony_backend.sessions.fs_proxy import WebFsProxy, web_fs_proxy_registry
        self._fs_proxy = WebFsProxy(provider=provider, handle=handle)
        web_fs_proxy_registry.register(self._session_code, self._fs_proxy)

        if restore:
            await self._restore_fs_from_events()

    async def _restore_fs_from_events(self) -> None:
        """Replay file_changed events onto a freshly provisioned sandbox.

        After a backend restart, ``provider.create()`` lays down only the task
        baseline. This method applies the event-log projection on top so the
        candidate's edits, bash-created files, and deletions are restored.

        Idempotent. No-op when:
          * the projection is empty (fresh session, no file_changed yet);
          * the sandbox is local-mode (handled by tempdir lifecycle elsewhere).

        Emits ``sandbox_restored`` once with the number of paths written, so the
        UI can surface a "Sandbox restored from history" banner.
        """
        if self._sandbox_provider is None or self._sandbox_handle is None:
            return
        try:
            projection = await replay_file_state(self._session_code)
        except Exception as exc:
            logger.warning(
                "AgentHost._restore_fs_from_events: replay_file_state failed for %s: %s",
                self._session_code,
                exc,
            )
            return
        if not projection:
            return

        from vibony_backend.sandbox.docker_provider import _safe_path

        written = 0
        deleted = 0
        for path, content in projection.items():
            try:
                # Defence-in-depth: event payloads are candidate-influenced, so
                # reject paths that escape the workdir before they reach the
                # delete argv (write_file applies the same guard internally).
                safe_path = _safe_path(path)
                if content is None:
                    await self._sandbox_provider.exec(
                        self._sandbox_handle, ["rm", "-rf", "--", safe_path]
                    )
                    deleted += 1
                else:
                    await self._sandbox_provider.write_file(
                        self._sandbox_handle, safe_path, content
                    )
                    written += 1
            except Exception as exc:
                logger.warning(
                    "AgentHost._restore_fs_from_events: failed to apply %s for %s: %s",
                    path,
                    self._session_code,
                    exc,
                )

        if written == 0 and deleted == 0:
            return

        await self._sink.emit(
            "sandbox_restored",
            {"file_count": written, "deleted_count": deleted},
        )
        logger.info(
            "AgentHost._restore_fs_from_events: code=%s written=%d deleted=%d",
            self._session_code,
            written,
            deleted,
        )

    async def _emit_start_events(self) -> None:
        await self._sink.emit(
            "session_start",
            {
                "task_id": self._task_id,
                "ai_config": {
                    "model": self._model or "",
                },
                "protocol_version": PROTOCOL_VERSION,
                "cli_version": "web",
            },
        )
        if not self._is_resumed:
            await self._emit_task_baseline()
        if self._sandbox_was_expired:
            await self._sink.emit("sandbox_reset", {"reason": "expired"})
        if not self._is_resumed:
            await self._sink.emit(
                "assistant_message",
                {
                    "text": (
                        "Hello! I'm your AI assistant. "
                        "I have access to the task files in the working directory. "
                        "How can I help you get started?"
                    ),
                    "usage": None,
                },
            )

    async def _emit_task_baseline(self) -> None:
        """Emit one ``file_changed`` per text file in the task bundle so File
        History has a diff baseline for the candidate's (and AI's) first edit.

        Source ``baseline`` marks these as the pre-edit scaffold: the dashboard
        diffs the first real change against this content instead of an empty
        string. Emitted once, on fresh-session start only (guarded by the caller
        via ``not self._is_resumed``); cold-start replays them from the log.

        Binary scaffold files (non-UTF-8) are skipped — a text diff baseline
        does not apply, matching ``replay_file_state``'s content-only semantics.
        """
        if not self._bundle_tar:
            return
        try:
            with tarfile.open(fileobj=BytesIO(self._bundle_tar), mode="r:gz") as tf:
                baselines: list[tuple[str, str]] = []
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    fobj = tf.extractfile(member)
                    if fobj is None:
                        continue
                    try:
                        baselines.append((member.name, fobj.read().decode("utf-8")))
                    except UnicodeDecodeError:
                        continue
        except Exception as exc:
            logger.warning(
                "AgentHost._emit_task_baseline: bundle read failed for %s: %s",
                self._task_id,
                exc,
            )
            return

        for path, content in baselines:
            await self._sink.emit(
                "file_changed",
                {
                    "file_path": path,
                    "diff": None,
                    "source": "baseline",
                    "content": content,
                    "op": "created",
                },
            )

        # Consumed once at start — drop the reference so the full tar isn't
        # stranded on the heap for the lifetime of a (possibly long) session.
        self._bundle_tar = None

    def _has_incomplete_evidence(self) -> bool:
        """Under the turn lock, markers can only belong to a previous failure.

        Never let a queued operation overwrite a cancelled operation's marker.
        It remains durable so assessment and host recreation both fail closed.
        """
        return bool(self._runtime_state.get("turn_in_progress")
                    or self._runtime_state.get("fs_write_in_progress")
                    or self._runtime_state.get("integrity_errors"))

    def start_user_message(self, text: str) -> bool:
        """Spawn a tracked agent-turn task (single-flight + cancel).

        Returns False (without spawning) when a turn is already in flight, the
        host is terminated, or a terminal transition is draining it (quiesce) —
        the recv loop then surfaces a BUSY error instead of piling up unbounded
        coroutines + per-frame DB writes. The spawned task is tracked in
        ``_turn_task`` so terminate()/quiesce() can stop an in-flight turn.
        """
        if self._terminated or not self._accepting:
            return False
        if self._turn_task is not None and not self._turn_task.done():
            return False
        self._turn_task = asyncio.create_task(self.submit_user_message(text))
        return True

    def is_turn_in_flight(self) -> bool:
        """Whether an agent turn is currently running — used to send the
        'working' status to a candidate that reconnects mid-turn."""
        return self._turn_task is not None and not self._turn_task.done()

    def _resolve_llm_timeout(self) -> float | None:
        """Per-LLM-call timeout for the current turn.

        None for a live/unbudgeted session → `call_llm` uses the absolute ceiling
        (`VIBONY_LLM_CALL_TIMEOUT_SECONDS`). For an async session, cap at the
        remaining budget (clamped to the absolute ceiling and floored) so a single
        hung call can't run materially past the candidate's deadline."""
        from vibony_backend.proxy import LLM_CALL_TIMEOUT_SECONDS
        from vibony_backend.sessions.async_timer import async_timer_registry

        remaining = async_timer_registry.remaining_seconds(self._session_code)
        if remaining is None:
            return None
        return max(
            LLM_TIMEOUT_ASYNC_FLOOR_SECONDS, min(LLM_CALL_TIMEOUT_SECONDS, remaining)
        )

    async def _publish_agent_status(self, state: str) -> None:
        """Advisory, non-persisted hint that an agent turn is/ isn't in flight.
        Broadcast like timer_tick (drop-if-full, never stored, never
        replayed); best-effort — a failure must never break a turn."""
        try:
            from vibony_backend.sessions.broker import broker

            await broker.publish_advisory(
                self._session_code, {"kind": "agent_status", "state": state}
            )
        except Exception:
            logger.debug("agent_status publish failed: code=%s", self._session_code)

    async def submit_user_message(self, text: str) -> None:
        """Run one agent turn for the given user message."""
        if self._terminated:
            logger.warning("AgentHost.submit_user_message called after terminate: %s", self._session_code)
            return
        if not self._started or self._toolset is None:
            logger.warning("AgentHost.submit_user_message called before start: %s", self._session_code)
            return

        async def _noop_emit_user(t: str) -> None:
            pass

        async def _emit_assistant(t: str | None, usage: dict | None) -> None:
            # Defense-in-depth: never persist a blank assistant
            # bubble. chat_turn already guards its emit sites; this guards the sink
            # itself so no future caller can resurrect the empty-bubble defect.
            if not (t or "").strip():
                return
            await self._sink.emit(
                "assistant_message",
                {"text": t, "usage": usage},
            )

        # Emit user_message *behind* the turn lock. Single-flight in
        # start_user_message already rejects a concurrent turn, so the lock is
        # uncontended here except against an in-progress reset — emitting after we
        # hold it avoids persisting a user_message that then waits on the reset.
        async with self._turn_lock:
            with session_usage(self._session_code):
                # The session may have terminated while we waited for the lock.
                if self._terminated or self._has_incomplete_evidence():
                    self._accepting = False
                    return
                try:
                    self._runtime_state["turn_in_progress"] = True
                    await self._checkpoint_runtime()
                    await self._sink.emit("user_message", {"text": text})
                    # Tell attendees a turn is in flight so the candidate sees
                    # "AI is working…" for the *whole* turn — including the silent gap
                    # between an intermediate message and the next LLM call — instead of a
                    # tool-heavy turn that goes quiet looking idle.
                    await self._publish_agent_status("working")
                    await chat_turn(
                        toolset=self._toolset,
                        task_id=self._task_id,
                        messages=self._messages,
                        user_text=text,
                        emit_user_message=_noop_emit_user,
                        emit_assistant_message=_emit_assistant,
                        model=self._model,
                        session_code=self._session_code,
                        system_prompt=self._system_prompt,
                        budget=self._budget,
                        is_terminated=lambda: self._terminated,
                        llm_timeout=self._resolve_llm_timeout,
                        controller=self._controller,
                        checkpoint=self._checkpoint_runtime,
                    )
                    # Compact the history (summarise old turns) before
                    # persisting, so the blob that lands in storage is the bounded
                    # one and the next turn sends fewer tokens.
                    await self._maybe_compact_messages()
                    # Persist the live blob only after a clean turn — a turn
                    # that raised mid-loop can leave _messages with an assistant
                    # tool_call but no tool result (an invalid history to restore).
                    # The last-good blob from the prior turn stays canonical instead.
                    await self._persist_messages()
                    self._runtime_state["turn_in_progress"] = False
                    await self._checkpoint_runtime()
                except UsageLimitExceeded:
                    from .loop import BUDGET_EXHAUSTED_MESSAGE
                    await _emit_assistant(BUDGET_EXHAUSTED_MESSAGE, None)
                    # A mid-tool round may be incomplete. Preserve the last good blob.
                    self._accepting = False
                    self._runtime_state.setdefault("integrity_errors", []).append("budget_interrupted_turn")
                    await self._checkpoint_runtime()
                except Exception as exc:
                    logger.exception(
                        "AgentHost.submit_user_message error: code=%s", self._session_code
                    )
                    self._accepting = False
                    self._runtime_state.setdefault("integrity_errors", []).append("agent_turn_failed")
                    try:
                        await self._checkpoint_runtime()
                    except PersistenceError:
                        pass  # Existing turn_in_progress marker remains durable.
                    if self._ws_send:
                        await self._ws_send({"kind": "error", "code": "AGENT_FAILED",
                            "message": "Session could not be saved reliably. Start a new session."})
                    if isinstance(exc, PersistenceError):
                        return
                    # Avoid surfacing raw exception strings (may contain API keys/responses).
                    await self._sink.emit(
                        "assistant_message",
                        {"text": f"Request failed ({type(exc).__name__}). See server logs for details.", "usage": None},
                    )
                finally:
                    # Always clear the working hint — clean finish, error, or the
                    # CancelledError a terminal transition raises mid-turn.
                    await self._publish_agent_status("idle")

    def start_candidate_file_write(
        self, proxy: Any, path: Any, content: Any,
        reply: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> bool:
        """Queue a bounded, tracked save without blocking WS confirmations.

        Accepted writes survive a disconnect and drain before session_end. The
        callback belongs to the requesting connection and acknowledges durable
        evidence only; a disconnected client can recover the file on reconnect.
        """
        if (self._terminated or not self._accepting
                or len(self._file_tasks) >= MAX_PENDING_FILE_WRITES):
            return False

        async def save_and_reply() -> None:
            result = await self.write_candidate_file(proxy, path, content, accepted=True)
            try:
                await reply(result)
            except Exception:
                logger.debug("File acknowledgement failed: code=%s", self._session_code)

        task = asyncio.create_task(save_and_reply())
        self._file_tasks.add(task)
        task.add_done_callback(self._file_tasks.discard)
        return True

    async def write_candidate_file(
        self, proxy: Any, path: Any, content: Any, *, accepted: bool = False,
    ) -> dict[str, Any]:
        """Acknowledge editor saves only after their replay evidence is durable."""
        async with self._turn_lock:
            # quiesce closes admission but must let previously accepted writes
            # finish. An interrupted/failed turn still makes queued writes unsafe.
            if (self._terminated or (not accepted and not self._accepting)
                    or self._has_incomplete_evidence()):
                return {"error": "Session is not accepting changes"}
            try:
                self._runtime_state["fs_write_in_progress"] = True
                await self._checkpoint_runtime()
                result = await proxy.request("write", path=path, content=content)
                if "error" not in result:
                    await self._sink.emit("file_changed", {
                        "file_path": path, "diff": None, "source": "manual",
                        "content": content, "op": "modified",
                    })
                self._runtime_state["fs_write_in_progress"] = False
                await self._checkpoint_runtime()
                return result
            except Exception:
                self._accepting = False
                self._runtime_state.setdefault("integrity_errors", []).append("candidate_save_failed")
                try:
                    await self._checkpoint_runtime()
                except PersistenceError:
                    pass
                logger.exception("Candidate file save failed: code=%s", self._session_code)
                return {"error": "File could not be saved reliably. Start a new session."}

    def start_inline_bash(self, cmd: str) -> bool:
        """Spawn a tracked inline-bash task (single-flight + cancel).

        Returns False (without spawning) when a candidate inline-bash is already
        in flight, the host is terminated, or a terminal transition is draining it
        (quiesce) — serialises candidate bash per session so a frame flood can't
        launch unbounded concurrent container execs. Tracked in ``_bash_task`` so
        terminate()/quiesce() can stop an in-flight exec.
        """
        if self._terminated or not self._accepting:
            return False
        if self._bash_task is not None and not self._bash_task.done():
            return False
        self._bash_task = asyncio.create_task(self.submit_inline_bash(cmd))
        return True

    async def submit_inline_bash(self, cmd: str) -> None:
        """Execute !cmd from browser: classify → gate → run → emit events.

        The classify/confirm round-trip runs lock-free (it may wait up to 300 s
        for the candidate to answer a confirm dialog); the actual execution in
        _run_bash acquires _turn_lock so the FS snapshot that
        attributes file_changed provenance never overlaps an agent turn's writes.
        """
        if self._terminated:
            return
        if not self._started or self._toolset is None:
            return

        command_id = uuid.uuid4().hex

        # Already allowed by a previous prefix-allow choice.
        if is_prefix_allowed(cmd, self._bash_prefixes):
            await self._run_bash(cmd, command_id, "auto_allow")
            return

        verdict = classify_bash(cmd)

        if verdict == "deny":
            await self._sink.emit("command_run", {
                "command_id": command_id,
                "command": cmd,
                "cwd": ".",
                "permission_decision": "policy_deny",
            })
            await self._sink.emit("command_finished", {
                "command_id": command_id,
                "exit_code": 1,
                "duration_ms": 0,
                "timed_out": False,
            })
            return

        if verdict == "allow":
            await self._run_bash(cmd, command_id, "auto_allow")
            return

        # confirm: browser round-trip
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_confirms[command_id] = fut

        prefix = extract_prefix(cmd)
        if self._ws_send is None:
            # No browser connected — auto-deny immediately instead of waiting 300 s.
            self._pending_confirms.pop(command_id, None)
            choice = "deny"
        else:
            await self._ws_send({
                "kind": "bash_confirm_request",
                "cmd_id": command_id,
                "cmd": cmd,
                "prefix": prefix,
                "choices": ["allow_once", "allow_prefix", "deny"],
            })
            try:
                choice = await asyncio.wait_for(fut, timeout=300.0)
            except asyncio.TimeoutError:
                choice = "deny"
            finally:
                self._pending_confirms.pop(command_id, None)

        if choice == "deny":
            await self._sink.emit("command_run", {
                "command_id": command_id,
                "command": cmd,
                "cwd": ".",
                "permission_decision": "user_deny",
            })
            await self._sink.emit("command_finished", {
                "command_id": command_id,
                "exit_code": 1,
                "duration_ms": 0,
                "timed_out": False,
            })
            return

        if choice == "allow_prefix" and prefix:
            self._bash_prefixes.add(prefix)

        await self._run_bash(cmd, command_id, "user_allow")

    async def _gate_ai_bash(self, cmd: str, cmd_id: str) -> tuple[str, bool]:
        """Permission gate for AI-initiated Bash tool calls.

        Returns (decision, prompted) where:
          decision ∈ {auto_allow, user_allow, user_deny, policy_deny}
          prompted — True when the browser was shown a confirm dialog

        Shares _bash_prefixes and _pending_confirms state with submit_inline_bash
        so prefix-allows granted by the candidate apply to AI commands too.
        """
        if is_prefix_allowed(cmd, self._bash_prefixes):
            return "auto_allow", False

        verdict = classify_bash(cmd)
        if verdict == "deny":
            return "policy_deny", False
        if verdict == "allow":
            return "auto_allow", False

        # confirm path — requires a live browser connection
        if self._ws_send is None:
            return "policy_deny", False

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_confirms[cmd_id] = fut

        prefix = extract_prefix(cmd)
        await self._ws_send({
            "kind": "bash_confirm_request",
            "cmd_id": cmd_id,
            "cmd": cmd,
            "prefix": prefix,
            "choices": ["allow_once", "allow_prefix", "deny"],
        })

        try:
            choice = await asyncio.wait_for(fut, timeout=300.0)
        except asyncio.TimeoutError:
            choice = "deny"
        finally:
            self._pending_confirms.pop(cmd_id, None)

        if choice in ("allow_once", "allow_prefix"):
            if choice == "allow_prefix" and prefix:
                self._bash_prefixes.add(prefix)
            return "user_allow", True
        return "user_deny", True

    def set_ws_send(
        self, cb: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        """Register (or clear) the direct WS send callback for the candidate browser."""
        self._ws_send = cb

    def resolve_bash_confirm(self, cmd_id: str, choice: str) -> None:
        """Resolve a pending bash_confirm_request (called from _recv_loop_web)."""
        fut = self._pending_confirms.get(cmd_id)
        if fut is not None and not fut.done():
            fut.set_result(choice)

    async def _run_bash(self, cmd: str, command_id: str, permission_decision: str) -> None:
        """Run a candidate inline-bash command, serialised against the agent turn.

        Acquires _turn_lock: the docker-mode FS snapshot below
        attributes file_changed provenance by diffing the sandbox before/after
        exec, so it must not overlap an agent turn's tool writes (which would be
        mis-attributed to the candidate). submit_user_message holds the same lock
        for the whole turn, so the two are mutually exclusive. The lock is taken
        here, after the (possibly long) confirm round-trip in the caller.
        """
        async with self._turn_lock:
            if self._terminated or self._has_incomplete_evidence():
                self._accepting = False
                return
            try:
                self._runtime_state["turn_in_progress"] = True
                await self._checkpoint_runtime()
                await self._run_bash_locked(cmd, command_id, permission_decision)
                self._runtime_state["turn_in_progress"] = False
                await self._checkpoint_runtime()
            except PersistenceError:
                self._accepting = False
                self._runtime_state.setdefault("integrity_errors", []).append("command_evidence_failed")
                try:
                    await self._checkpoint_runtime()
                except PersistenceError:
                    pass
                if self._ws_send:
                    await self._ws_send({"kind": "error", "code": "AGENT_FAILED",
                        "message": "Command evidence could not be saved. Start a new session."})
                raise

    async def _run_bash_locked(
        self, cmd: str, command_id: str, permission_decision: str
    ) -> None:
        """Emit command_run, execute cmd, stream command_output chunks, emit command_finished."""
        await self._sink.emit("command_run", {
            "command_id": command_id,
            "command": cmd,
            "cwd": ".",
            "permission_decision": permission_decision,
        })
        t0 = time.monotonic()

        if self._workdir is not None:
            # Local mode — stream stdout and stderr concurrently via asyncio.subprocess.
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self._workdir,
                )

                async def _stream(reader: asyncio.StreamReader, stream_name: str) -> None:
                    # Label each stream correctly. Local mode reads stdout
                    # and stderr as separate pipes, so stderr must not be mislabeled
                    # as stdout (docker mode merges them into one stream → "stdout").
                    while True:
                        chunk = await reader.read(4096)
                        if not chunk:
                            break
                        await self._sink.emit("command_output", {
                            "command_id": command_id,
                            "stream": stream_name,
                            "data": chunk.decode("utf-8", errors="replace"),
                        })

                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            _stream(proc.stdout, "stdout"),  # type: ignore[arg-type]
                            _stream(proc.stderr, "stderr"),  # type: ignore[arg-type]
                            proc.wait(),
                        ),
                        timeout=120.0,
                    )
                    timed_out = False
                    exit_code = proc.returncode or 0
                except asyncio.TimeoutError:
                    timed_out = True
                    proc.kill()
                    # proc.wait() — don't read pipes here; _stream coroutines were
                    # cancelled mid-read and proc.communicate() would race with them.
                    await proc.wait()
                    exit_code = -1
            except PersistenceError:
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                await self._sink.emit("command_output", {
                    "command_id": command_id,
                    "stream": "stdout",
                    "data": f"Error: {exc}\n",
                })
                await self._sink.emit("command_finished", {
                    "command_id": command_id,
                    "exit_code": 1,
                    "duration_ms": duration_ms,
                    "timed_out": False,
                })
                return

            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._sink.emit("command_finished", {
                "command_id": command_id,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
            })

        elif self._sandbox_provider is not None and self._sandbox_handle is not None:
            # Docker mode — stream via exec_stream so chunks arrive as they are produced.
            from vibony_backend.sandbox.config import SANDBOX_BASH_TIMEOUT_MS

            t_wall = time.time()
            # Take pre-exec snapshot to detect deletions and new dirs.
            pre_files: set[str] = set()
            pre_dirs: set[str] = set()
            try:
                pre_files = await self._sandbox_provider.snapshot_paths(self._sandbox_handle)
                pre_dirs = await self._sandbox_provider.snapshot_dirs(self._sandbox_handle)
            except PersistenceError:
                raise
            except Exception as exc:
                logger.debug("AgentHost._run_bash: pre-snapshot failed: %s", exc)

            async def _on_chunk(chunk: bytes) -> None:
                await self._sink.emit("command_output", {
                    "command_id": command_id,
                    "stream": "stdout",
                    "data": chunk.decode("utf-8", errors="replace"),
                })

            try:
                exec_result = await self._sandbox_provider.exec_stream(
                    self._sandbox_handle,
                    cmd,
                    _on_chunk,
                    cwd=None,
                    timeout_ms=SANDBOX_BASH_TIMEOUT_MS,
                )
                await self._sink.emit("command_finished", {
                    "command_id": command_id,
                    "exit_code": exec_result.exit_code,
                    "duration_ms": exec_result.duration_ms,
                    "timed_out": exec_result.timed_out,
                })
                # Emit file_changed for any files the bash command wrote/deleted/created dirs.
                if not exec_result.timed_out:
                    try:
                        # list_changed_since scans the filesystem *after* exec, so
                        # files deleted inside the same command are absent from its
                        # os.walk and will not appear here — only in deleted_files below.
                        # The post-snapshot is taken after this call; ordering must not
                        # be swapped without re-checking this invariant.
                        # Created/modified files
                        changed = await self._sandbox_provider.list_changed_since(
                            self._sandbox_handle, t_wall
                        )
                        read_results = await asyncio.gather(
                            *(
                                self._sandbox_provider.read_file(
                                    self._sandbox_handle, rel_path
                                )
                                for rel_path in changed
                            ),
                            return_exceptions=True,
                        )
                        for rel_path, read_result in zip(changed, read_results):
                            if isinstance(read_result, Exception):
                                logger.debug(
                                    "AgentHost._run_bash: read_file failed for %s: %s",
                                    rel_path,
                                    read_result,
                                )
                                continue
                            await self._sink.emit("file_changed", {
                                "file_path": rel_path,
                                "diff": None,
                                "source": "manual",
                                "content": read_result,
                                "op": "modified",
                            })

                        # Deleted files and new/deleted directories
                        post_files = await self._sandbox_provider.snapshot_paths(
                            self._sandbox_handle
                        )
                        post_dirs = await self._sandbox_provider.snapshot_dirs(
                            self._sandbox_handle
                        )

                        deleted_files = pre_files - post_files
                        for rel_path in sorted(deleted_files):
                            await self._sink.emit("file_changed", {
                                "file_path": rel_path,
                                "diff": None,
                                "source": "manual",
                                "content": None,
                                "op": "deleted",
                            })

                        new_dirs = post_dirs - pre_dirs
                        for rel_path in sorted(new_dirs):
                            await self._sink.emit("file_changed", {
                                "file_path": rel_path,
                                "diff": None,
                                "source": "manual",
                                "content": None,
                                "op": "created",
                                "is_dir": True,
                            })

                        deleted_dirs = pre_dirs - post_dirs
                        for rel_path in sorted(deleted_dirs):
                            await self._sink.emit("file_changed", {
                                "file_path": rel_path,
                                "diff": None,
                                "source": "manual",
                                "content": None,
                                "op": "deleted",
                                "is_dir": True,
                            })

                    except PersistenceError:
                        raise
                    except Exception as exc:
                        logger.debug(
                            "AgentHost._run_bash: file change detection failed: %s", exc
                        )
            except PersistenceError:
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                await self._sink.emit("command_output", {
                    "command_id": command_id,
                    "stream": "stdout",
                    "data": f"Error: {exc}\n",
                })
                await self._sink.emit("command_finished", {
                    "command_id": command_id,
                    "exit_code": 1,
                    "duration_ms": duration_ms,
                    "timed_out": False,
                })

        else:
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._sink.emit("command_output", {
                "command_id": command_id,
                "stream": "stdout",
                "data": "Error: sandbox not ready\n",
            })
            await self._sink.emit("command_finished", {
                "command_id": command_id,
                "exit_code": 1,
                "duration_ms": duration_ms,
                "timed_out": False,
            })

    async def _teardown_sandbox(self) -> None:
        """Destroy current sandbox/workdir and unregister WebFsProxy without terminating the host.

        Safe to call whether the sandbox is in local or docker mode. Used by both
        terminate() and reset_sandbox().
        """
        from vibony_backend.sessions.fs_proxy import web_fs_proxy_registry
        # Unregister by identity so a stale teardown racing a
        # reconnect can't evict the fresh host's proxy under the same code.
        web_fs_proxy_registry.unregister(self._session_code, self._fs_proxy)
        self._fs_proxy = None

        if self._sandbox_provider is not None and self._sandbox_handle is not None:
            try:
                await self._sandbox_provider.teardown(self._sandbox_handle)
            except Exception as exc:
                logger.warning(
                    "AgentHost._teardown_sandbox: sandbox teardown failed for %s: %s",
                    self._session_code,
                    exc,
                )
            self._sandbox_provider = None
            self._sandbox_handle = None

        if self._workdir is not None and self._workdir.exists():
            try:
                shutil.rmtree(self._workdir)
            except Exception as exc:
                logger.warning(
                    "AgentHost._teardown_sandbox: failed to remove workdir %s: %s",
                    self._workdir,
                    exc,
                )
        self._workdir = None
        self._toolset = None

    async def quiesce(self, timeout: float = QUIESCE_TIMEOUT_SECONDS) -> None:
        """Stop accepting new turns/bash/saves and drain accepted operations.

        Called by a terminal transition (session_submit / async timeout /
        interviewer end) *before* it stores ``session_end``. The turn finishes
        cleanly so (a) its trailing events aren't dropped by store_event's
        ``active → completed`` guard once the session flips to completed,
        and (b) the transcript handed to the assessment isn't left
        with a dangling final ``user_message`` whose turn never completed.
        Waits up to ``timeout`` total for a clean finish; an operation that
        overruns (e.g. a wedged LLM call) is cancelled so the transition can't
        hang. Idempotent and safe when no operation is in flight.

        Honours a cancellation aimed at *quiesce itself* (the timeout path runs
        inside the countdown task, which a racing submit/REST end may cancel via
        ``async_timer_registry.cancel``): a CancelledError requested on the
        current task is re-raised, while a CancelledError that merely settled the
        awaited turn/bash (cancelled by another path) is swallowed so the rest of
        the drain proceeds.
        """
        self._accepting = False
        deadline = time.monotonic() + timeout
        for task in (self._turn_task, self._bash_task, *tuple(self._file_tasks)):
            if task is None or task.done():
                continue
            try:
                await asyncio.wait_for(task, max(0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                logger.warning(
                    "AgentHost.quiesce: in-flight operation overran %.0fs; "
                    "cancelled (code=%s)",
                    timeout,
                    self._session_code,
                )
                # wait_for already requested cancellation and awaited the task.
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # our own coroutine is being cancelled — propagate
                # Otherwise the awaited turn/bash was cancelled by another path.
            except Exception:
                # Turn/exec errors are already handled inside
                # submit_user_message / submit_inline_bash; nothing to do here.
                pass

    async def terminate(self) -> None:
        """Tear down sandbox (docker) or tempdir (local) and mark as terminated."""
        if self._terminated:
            return
        self._terminated = True
        self._accepting = False

        # Cancel any in-flight turn / inline-bash task so an orphaned
        # chat_turn stops calling the LLM (and an exec stops touching the sandbox)
        # after the session is terminal. We don't await them — the cancellation
        # raises at the next await point; the _terminated flag (checked at each
        # chat_turn iteration) is the belt-and-suspenders if cancellation is missed.
        for task in (self._turn_task, self._bash_task, *tuple(self._file_tasks)):
            if task is not None and not task.done():
                task.cancel()

        # Cancel any pending bash_confirm_request futures so they don't leak.
        for fut in self._pending_confirms.values():
            if not fut.done():
                fut.cancel()
        self._pending_confirms.clear()

        # Hold _turn_lock across teardown so it can't run
        # concurrently with reset_sandbox (which also holds it) — otherwise a
        # reset could rebuild a sandbox this terminate is tearing down. We cancel
        # the in-flight turn/bash above first, so a turn holding the lock unwinds
        # on its CancelledError and releases it; this acquire then waits only for
        # that unwind, not for a full LLM round-trip.
        async with self._turn_lock:
            await self._teardown_sandbox()
        logger.info("AgentHost terminated: code=%s", self._session_code)

    async def reset_sandbox(self) -> None:
        """Destroy current sandbox, recreate with task baseline, emit sandbox_reset.

        Conversation history (_messages) is preserved. Only the FS is reset.
        Non-ops if the host is already terminated.
        """
        if self._terminated:
            return

        # Cancel any pending bash_confirm_request futures so they don't block
        # on a destroyed sandbox.  Same pattern as terminate().
        for fut in self._pending_confirms.values():
            if not fut.done():
                fut.cancel()
        self._pending_confirms.clear()

        # Hold _turn_lock across the whole teardown → re-provision → emit
        # sequence (submit_user_message holds the same lock). Releasing it after
        # teardown alone would let a concurrent chat turn observe a half-built
        # sandbox between provision and the sandbox_reset emit.
        async with self._turn_lock:
            # A terminate() may have set _terminated and run
            # its own teardown while we waited for the lock. Re-check under the
            # lock so we don't rebuild a sandbox on an already-torn-down host.
            if self._terminated:
                return
            await self._teardown_sandbox()

            # Reset-vs-terminal-end race: a terminal end (interviewer /
            # async timeout / submit) commits status=completed then
            # force_terminate → terminate(), which sets _terminated *before*
            # blocking on _turn_lock (held by this reset). quiesce() does not
            # drain reset_sandbox, so without this re-check the rebuild below
            # runs to completion only to be torn straight back down by the
            # waiting terminate(), and the post-completion baseline/sandbox_reset
            # emits hit store_event's active→completed guard as a burst of
            # swallowed warnings. Bail here (after the awaited teardown, which can
            # itself span the flip) — the session is terminal, the FS stays down.
            if self._terminated:
                return

            sandbox_mode = os.getenv("VIBONY_SANDBOX_PROVIDER", "local")
            if sandbox_mode == "docker":
                await self._provision_docker(restore=False)
            else:
                await self._provision_local()

            # _terminated can also flip during the (awaited) rebuild — re-check
            # once more before the emits so they never land on a completed row.
            if self._terminated:
                return

            # Re-seed File History diff baselines: the FS is back to clean
            # scaffold, so post-reset edits must diff against the scaffold, not
            # the candidate's pre-reset content. The dashboard treats a
            # non-first baseline as a reset boundary (segments the diff chain +
            # draws a "Sandbox reset" divider).
            await self._emit_task_baseline()
            await self._sink.emit("sandbox_reset", {})
        logger.info("AgentHost sandbox reset: code=%s", self._session_code)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DISCONNECT_GRACE_SECONDS = 3600  # 1 hour — sandbox survives short disconnects


class AgentHostRegistry:
    """In-process map of session_code → AgentHost. Mirror of FsProxyRegistry."""

    def __init__(self) -> None:
        self._hosts: dict[str, AgentHost] = {}
        self._grace_tasks: dict[str, asyncio.Task[None]] = {}
        self._expired_codes: set[str] = set()
        # Monotonic per-code connection counter. Each connect
        # bumps it; the WS captures the value and passes it back to
        # connection_closed, which only schedules a grace timer if no newer
        # connection has since bumped it. Bounded by distinct session codes per
        # process lifetime (a few bytes each) — never reset, so a stale close can
        # never collide with a fresh lifecycle's epoch.
        self._connect_epochs: dict[str, int] = {}
        # Live candidate-connection count per code. The epoch
        # alone tracks the *newest* connection, not how many are open — so a
        # duplicate connection (same token, rejected at the presence join but it
        # still bumped the epoch) that disconnects first would arm the grace timer
        # on the now-current epoch and kill the still-live first connection after
        # the grace window. Arming is gated on this reaching 0 instead, so grace
        # fires only when the *last* connection for a code closes.
        self._active_conns: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def get(self, code: str) -> AgentHost | None:
        return self._hosts.get(code)

    async def get_or_create(
        self,
        code: str,
        session_id: uuid.UUID,
        task_id: str,
        ai_config: dict[str, Any],
    ) -> tuple[AgentHost, bool, int]:
        """Return (host, was_just_created, connect_epoch).

        Cancels any pending grace timer and bumps the connection epoch — the
        caller (the candidate WS) passes ``connect_epoch`` back to
        ``connection_closed`` on disconnect so an overlapping reconnect can't be
        killed by the old connection's stale grace schedule.
        """
        async with self._lock:
            self._cancel_grace(code)
            epoch = self._connect_epochs.get(code, 0) + 1
            self._connect_epochs[code] = epoch
            self._active_conns[code] = self._active_conns.get(code, 0) + 1
            if code in self._hosts:
                return self._hosts[code], False, epoch
            host = AgentHost(session_id, code, task_id, ai_config)
            if code in self._expired_codes:
                self._expired_codes.discard(code)
                host._sandbox_was_expired = True
            self._hosts[code] = host
            return host, True, epoch

    async def connection_closed(self, code: str, epoch: int) -> None:
        """A candidate WS connection (established at ``epoch``) has ended.

        Schedules the DISCONNECT_GRACE_SECONDS grace timer — but only when the
        *last* live connection for ``code`` closes (``_active_conns`` reaches 0)
        and the host is still present. The connection count is authoritative for
        "is anyone still connected"; the epoch is retained only for the
        reconnect fast-path (``get_or_create`` cancels a pending grace), and the
        ``epoch`` arg is kept for signature stability. This closes the
        overlapping-connection races:
          * a reconnect overlapping the old close keeps the count ≥ 1 (or its
            cancel_grace undoes a momentarily-armed timer) → no grace-kill;
          * a duplicate connection closing first leaves the count ≥ 1
            → the live first connection is not grace-killed on the bumped epoch;
          * a close after force_terminate finds no host → no leaked 1 h timer;
          * the normal last-connection close still arms the timer.
        """
        async with self._lock:
            remaining = max(0, self._active_conns.get(code, 0) - 1)
            if remaining > 0:
                self._active_conns[code] = remaining
                return  # another live connection still holds this host
            # Last connection for this code closed. Pop the counter (re-created on
            # the next get_or_create) instead of leaving a {code: 0} entry that no
            # path clears — otherwise one stale key leaks per terminated session.
            self._active_conns.pop(code, None)
            if code not in self._hosts:
                return  # already force-terminated — nothing to grace
            self._cancel_grace(code)
            task = asyncio.create_task(self._grace_terminate(code))
            self._grace_tasks[code] = task

    def cancel_grace(self, code: str) -> None:
        """Cancel pending grace timer (called on reconnect)."""
        self._cancel_grace(code)

    async def force_terminate(self, code: str) -> None:
        """Immediately terminate and remove the host."""
        async with self._lock:
            self._cancel_grace(code)
            self._active_conns.pop(code, None)
            host = self._hosts.pop(code, None)
        if host is not None:
            await host.terminate()

    # ---------- internals ----------

    def _cancel_grace(self, code: str) -> None:
        task = self._grace_tasks.pop(code, None)
        if task is not None and not task.done():
            task.cancel()

    async def _grace_terminate(self, code: str) -> None:
        try:
            await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        async with self._lock:
            host = self._hosts.pop(code, None)
            self._active_conns.pop(code, None)
            if host is not None:
                self._expired_codes.add(code)
        self._grace_tasks.pop(code, None)
        if host is not None:
            await host.terminate()
            logger.info("AgentHost grace-terminated: code=%s", code)


# Global singleton — mirror of web_fs_proxy_registry pattern.
agent_host_registry = AgentHostRegistry()

__all__ = [
    "AgentHost",
    "AgentHostRegistry",
    "BackendEventSink",
    "agent_host_registry",
    "DISCONNECT_GRACE_SECONDS",
]
