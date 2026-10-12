"""Smoke tests for the backend.

No live LLM calls: litellm.acompletion is mocked. A live run against
DeepSeek/Anthropic is done by hand via curl with a real API key.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vibony_backend.main import app
from vibony_backend.weakness import render_weakness


def test_render_weakness_training_loop():
    prompt = render_weakness("task-01-training-loop")
    assert prompt, "empty prompt"
    assert "AI assistant" in prompt, "no preamble from global.yaml"
    assert "checkpoint" in prompt.lower(), "task-01 contains checkpoint in blindspots"


def test_render_weakness_unknown_task():
    with pytest.raises(FileNotFoundError):
        render_weakness("task-does-not-exist")


def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chat_with_mocked_litellm(monkeypatch):
    """Checks the backend injects the system prompt from weakness and returns ChatResponse.

    litellm.acompletion is mocked — the real model is not called.
    """
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="mock-response", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        )

    monkeypatch.setattr("vibony_backend.proxy.litellm.acompletion", fake_acompletion)

    client = TestClient(app)
    response = client.post(
        "/v1/chat",
        json={
            "task_id": "task-01-training-loop",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"] == "mock-response"
    assert body["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 15

    # Check the system-prompt injection
    messages = captured["kwargs"]["messages"]
    assert messages[0]["role"] == "system"
    assert "AI assistant" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Hello"}


def test_chat_with_unknown_task():
    client = TestClient(app)
    response = client.post(
        "/v1/chat",
        json={
            "task_id": "task-does-not-exist",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 400
    assert "weakness.yaml not found" in response.json()["detail"]
