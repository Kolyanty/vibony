"""Tests for the prompt-resolution seam + model-role config.

Pure-sync (no DB, no LLM, no asyncio) — the seam is a registry + env reads.
"""

from __future__ import annotations

import pytest

from vibony_backend import prompt_seam
from vibony_backend.prompt_seam import (
    ASSISTANT,
    CONTROLLER,
    DEFAULT_VARIANT,
    JUDGE,
    register_prompt,
    resolve_prompt,
)


@pytest.fixture
def clean_registry():
    """Snapshot + restore the module-global registry around a test that mutates it."""
    snapshot = dict(prompt_seam._REGISTRY)
    yield
    prompt_seam._REGISTRY.clear()
    prompt_seam._REGISTRY.update(snapshot)


# --- default resolution -----------------------------------------------------


def test_resolve_assistant_default_is_live():
    """Assistant default delegates to the real weakness renderer (configs/global.yaml)."""
    variant = resolve_prompt("any/model", ASSISTANT)
    assert variant.kind == ASSISTANT
    assert variant.requested_model == "any/model"
    assert variant.resolved_for == DEFAULT_VARIANT  # no per-model variant registered
    rendered = variant.build(None)  # None weakness → global-only prompt
    assert "assistant" in rendered.lower()


def test_resolve_judge_default_is_live():
    """Judge default returns the assessment SYSTEM_PROMPT verbatim."""
    from vibony_backend.assessment.prompts import SYSTEM_PROMPT

    variant = resolve_prompt("any/model", JUDGE)
    assert variant.resolved_for == DEFAULT_VARIANT
    assert variant.build() == SYSTEM_PROMPT


# --- override / fallback semantics ------------------------------------------


def test_model_specific_variant_beats_default(clean_registry):
    register_prompt(ASSISTANT, lambda weakness=None: "TUNED", model="special/model")

    tuned = resolve_prompt("special/model", ASSISTANT)
    assert tuned.resolved_for == "special/model"
    assert tuned.build() == "TUNED"

    # A different model still falls back to the default variant.
    other = resolve_prompt("other/model", ASSISTANT)
    assert other.resolved_for == DEFAULT_VARIANT
    assert other.build(None) != "TUNED"


# --- error / reserved-kind semantics ----------------------------------------


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        resolve_prompt("any/model", "bogus")
    with pytest.raises(ValueError):
        register_prompt("bogus", lambda: "x")


def test_controller_default_is_live():
    # The controller mechanism exists → a live default delegates to the
    # controller's task-agnostic system prompt.
    assert CONTROLLER in prompt_seam.KNOWN_KINDS
    variant = resolve_prompt("any/model", CONTROLLER)
    assert variant.resolved_for == prompt_seam.DEFAULT_VARIANT
    prompt = variant.build()
    assert "trap controller" in prompt.lower()


# --- model roles (independent config dimensions) ----------------------------


def test_assistant_model_env_then_proxy_default(monkeypatch):
    from vibony_backend import proxy

    monkeypatch.setenv("VIBONY_ASSISTANT_MODEL", "x/assist")
    assert prompt_seam.assistant_model() == "x/assist"

    monkeypatch.delenv("VIBONY_ASSISTANT_MODEL", raising=False)
    assert prompt_seam.assistant_model() == proxy.DEFAULT_MODEL


def test_judge_model_is_independent(monkeypatch):
    monkeypatch.setenv("VIBONY_ASSISTANT_MODEL", "a/assist")
    monkeypatch.delenv("VIBONY_JUDGE_MODEL", raising=False)
    # Unset → falls back to the assistant model (out-of-the-box working default).
    assert prompt_seam.judge_model() == "a/assist"
    # Set → independent of the assistant model.
    monkeypatch.setenv("VIBONY_JUDGE_MODEL", "j/judge")
    assert prompt_seam.judge_model() == "j/judge"


def test_controller_model_none_until_configured(monkeypatch):
    monkeypatch.delenv("VIBONY_CONTROLLER_MODEL", raising=False)
    assert prompt_seam.controller_model() is None
    monkeypatch.setenv("VIBONY_CONTROLLER_MODEL", "c/ctrl")
    assert prompt_seam.controller_model() == "c/ctrl"
