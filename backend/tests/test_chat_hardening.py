"""Model allowlist + SSRF guard + /v1/chat session-less gate + weakness
slug containment.

Mixes sync unit tests (validate_model, render_weakness) with async endpoint
tests; async ones carry the session-loop mark (see CLAUDE.md test traps).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

_session_loop = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# validate_model — allowlist + SSRF
# ---------------------------------------------------------------------------

def test_validate_model_allows_default():
    from vibony_backend.proxy import DEFAULT_MODEL, validate_model

    assert validate_model(DEFAULT_MODEL) == DEFAULT_MODEL


def test_validate_model_rejects_api_base_ssrf():
    from vibony_backend.proxy import ModelNotAllowed, validate_model

    with pytest.raises(ModelNotAllowed):
        validate_model("openai/gpt-4@http://169.254.169.254/latest/meta-data")
    with pytest.raises(ModelNotAllowed):
        validate_model("custom://evil.example/v1")


def test_validate_model_rejects_unlisted():
    from vibony_backend.proxy import ModelNotAllowed, validate_model

    with pytest.raises(ModelNotAllowed):
        validate_model("anthropic/claude-please-bill-someone-else")


def test_validate_model_respects_env_allowlist(monkeypatch):
    monkeypatch.setenv("VIBONY_MODEL_ALLOWLIST", "openai/gpt-4o, deepseek/deepseek-chat")
    from vibony_backend.proxy import ModelNotAllowed, validate_model

    assert validate_model("openai/gpt-4o") == "openai/gpt-4o"
    with pytest.raises(ModelNotAllowed):
        validate_model("openai/o1")  # not in the configured allowlist


# ---------------------------------------------------------------------------
# render_weakness — slug containment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "evil",
    [
        "../../etc/passwd",
        "..",
        "foo/bar",
        "foo\\bar",
        ".hidden",
        "",
    ],
)
def test_render_weakness_rejects_traversal(evil):
    from vibony_backend.weakness import render_weakness

    with pytest.raises(FileNotFoundError):
        render_weakness(evil)


# ---------------------------------------------------------------------------
# /v1/chat session-less gate
# ---------------------------------------------------------------------------

def test_v1_chat_sessionless_gated_off_by_default(monkeypatch):
    monkeypatch.delenv("VIBONY_ALLOW_UNAUTH_CHAT", raising=False)
    from vibony_backend.main import app

    client = TestClient(app)
    resp = client.post(
        "/v1/chat",
        json={"task_id": "task-01-training-loop", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"reason": "not_found"}


def test_v1_chat_sessionless_allowed_with_flag(monkeypatch):
    monkeypatch.setenv("VIBONY_ALLOW_UNAUTH_CHAT", "1")

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok", tool_calls=None))],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 1}),
        )

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)
    from vibony_backend.main import app

    client = TestClient(app)
    resp = client.post(
        "/v1/chat",
        json={"task_id": "task-01-training-loop", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# call_llm per-call timeout
# ---------------------------------------------------------------------------


@_session_loop
async def test_call_llm_uses_request_timeout(monkeypatch):
    """call_llm honors ChatRequest.timeout_seconds (the agent loop's async-budget
    cap) and falls back to the absolute VIBONY_LLM_CALL_TIMEOUT_SECONDS ceiling."""
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok", tool_calls=None),
                )
            ],
            usage=None,
        )

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)
    from vibony_backend.proxy import LLM_CALL_TIMEOUT_SECONDS, call_llm
    from vibony_backend.schemas import ChatRequest

    msgs = [{"role": "user", "content": "hi"}]
    await call_llm(ChatRequest(task_id="task-01-training-loop", messages=msgs, system_prompt="x"))
    assert captured["timeout"] == LLM_CALL_TIMEOUT_SECONDS  # default ceiling

    await call_llm(
        ChatRequest(task_id="task-01-training-loop", messages=msgs, system_prompt="x", timeout_seconds=42.0)
    )
    assert captured["timeout"] == 42.0


# ---------------------------------------------------------------------------
# weakness prompt position
# ---------------------------------------------------------------------------


def _capture_messages_acompletion(captured: dict):
    async def fake_acompletion(**kwargs):
        captured["messages"] = kwargs.get("messages")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="ok", tool_calls=None),
                )
            ],
            usage=None,
        )

    return fake_acompletion


@_session_loop
async def test_call_llm_prompt_position_start_default(monkeypatch):
    """Default: the weakness system prompt is messages[0] (production behavior)."""
    captured: dict = {}
    monkeypatch.delenv("VIBONY_PROMPT_POSITION", raising=False)
    monkeypatch.setattr(
        "vibony_backend.proxy.litellm.acompletion", _capture_messages_acompletion(captured)
    )
    from vibony_backend.proxy import call_llm
    from vibony_backend.schemas import ChatRequest

    await call_llm(
        ChatRequest(
            task_id="task-01-training-loop",
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="SYS",
        )
    )
    msgs = captured["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[-1]["role"] == "user"


@_session_loop
async def test_call_llm_prompt_position_end_appends_reminder(monkeypatch):
    """End keeps the full system prompt at the head AND appends the
    non-initiative reminder after the candidate's latest (non-tool) message."""
    captured: dict = {}
    monkeypatch.setenv("VIBONY_PROMPT_POSITION", "end")
    monkeypatch.setattr(
        "vibony_backend.proxy.litellm.acompletion", _capture_messages_acompletion(captured)
    )
    from vibony_backend.proxy import _NON_INITIATIVE_REMINDER, call_llm
    from vibony_backend.schemas import ChatRequest

    await call_llm(
        ChatRequest(
            task_id="task-01-training-loop",
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="SYS",
        )
    )
    msgs = captured["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}  # context still leads
    assert msgs[-1] == {"role": "system", "content": _NON_INITIATIVE_REMINDER}
    assert sum(1 for m in msgs if m["role"] == "system") == 2


@_session_loop
async def test_call_llm_prompt_position_end_tool_safe(monkeypatch):
    """The reminder is NOT appended after a role:tool result (tool-loop safety) — that
    placement broke the agent in the first b′ probe."""
    captured: dict = {}
    monkeypatch.setenv("VIBONY_PROMPT_POSITION", "end")
    monkeypatch.setattr(
        "vibony_backend.proxy.litellm.acompletion", _capture_messages_acompletion(captured)
    )
    from vibony_backend.proxy import _NON_INITIATIVE_REMINDER, call_llm
    from vibony_backend.schemas import ChatRequest

    await call_llm(
        ChatRequest(
            task_id="task-01-training-loop",
            messages=[
                {"role": "user", "content": "do it"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
                {"role": "tool", "tool_call_id": "t1", "content": "file contents"},
            ],
            system_prompt="SYS",
        )
    )
    msgs = captured["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[-1]["role"] == "tool"  # no reminder appended after the tool result
    assert _NON_INITIATIVE_REMINDER not in [m.get("content") for m in msgs]
