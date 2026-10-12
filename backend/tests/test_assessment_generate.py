"""Assessment generation tests with ``litellm.acompletion`` mocked.

No real LLM call, no DB. Covers: happy-path parse, markdown-fence stripping, the
single retry on malformed JSON / schema-validation failure, hard failure after two
bad replies, model override (param + env default), and the near-empty prompt note.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from vibony_backend.assessment.generate import (
    AssessmentGenerationError,
    _strip_fences,
    build_user_prompt,
    generate_assessment,
)
from vibony_backend.assessment.inputs import (
    AssessmentInputs,
    CodeStats,
    TranscriptStats,
)

_session_loop = pytest.mark.asyncio(loop_scope="session")


def _inputs(*, near_empty: bool = False) -> AssessmentInputs:
    return AssessmentInputs(
        task_id="task-01-training-loop",
        spec=None,
        spec_text="Task: task-01\nContract:\n- checkpoint each epoch",
        transcript="[1] candidate: add checkpoint\n[2] ai: editing train.py",
        transcript_stats=TranscriptStats(rendered=2),
        final_code="# train.py\nprint('x')",
        code_stats=CodeStats(files=1),
        is_near_empty=near_empty,
    )


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


_GOOD = json.dumps(
    {
        "summary_md": "## What went well\nx\n## What's missing for an ideal solution\ny\n## Observed reasoning patterns\nz",
        "findings": [
            {
                "category": "gap",
                "title": "No optimizer state in checkpoint",
                "detail": "Checkpoint saved only model.state_dict().",
                "severity": "blocking",
                "event_seq": 2,
            },
            {
                "category": "strength",
                "title": "Ran the training loop",
                "detail": "Verified by executing train.py.",
                "severity": None,
                "event_seq": None,
            },
        ],
    }
)


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


def test_build_user_prompt_includes_sections():
    prompt = build_user_prompt(_inputs())
    assert "TASK SPEC" in prompt
    assert "CANDIDATE FINAL CODE" in prompt
    assert "SESSION TRANSCRIPT" in prompt
    assert "checkpoint each epoch" in prompt
    assert "insufficient activity" not in prompt


def test_build_user_prompt_near_empty_note():
    prompt = build_user_prompt(_inputs(near_empty=True))
    # The near-empty note pins the exact sentinel the cheap programmatic honesty
    # check keys on — keep this assertion and the marker list
    # in scripts/assessment_eval/judge.py aligned with the prompt wording.
    assert "Insufficient activity to assess" in prompt


def test_build_user_prompt_wraps_sections_in_untrusted_fences():
    """Spec, final code and transcript are each fenced as untrusted data
    so a candidate (or template author) can't steer the judge from inside them."""
    prompt = build_user_prompt(_inputs())
    # Each of the three content blocks is wrapped.
    assert prompt.count("<<<BEGIN UNTRUSTED DATA") == 3
    assert prompt.count("<<<END UNTRUSTED DATA") == 3
    # The labels identify each block.
    assert "[task spec]" in prompt
    assert "[candidate final code]" in prompt
    assert "[session transcript]" in prompt
    # The candidate content lands inside a fence, not as bare prompt text.
    code_marker = prompt.index("<<<BEGIN UNTRUSTED DATA [candidate final code]")
    assert prompt.index("print('x')") > code_marker


def test_system_prompt_carries_untrusted_data_guidance():
    """The must-not-obey rule rides in the system prompt."""
    from vibony_backend.assessment import prompts

    assert "UNTRUSTED" in prompts.SYSTEM_PROMPT
    assert "never instructions" in prompts.SYSTEM_PROMPT


def test_build_user_prompt_handles_missing_pieces():
    inputs = AssessmentInputs(
        task_id="t",
        spec=None,
        spec_text="",
        transcript="",
        transcript_stats=TranscriptStats(),
        final_code="",
        code_stats=CodeStats(),
        is_near_empty=True,
    )
    prompt = build_user_prompt(inputs)
    assert "(no spec available for this task)" in prompt
    assert "(no candidate code was produced)" in prompt
    assert "(empty transcript)" in prompt


# ---------------------------------------------------------------------------
# generate_assessment
# ---------------------------------------------------------------------------


@_session_loop
async def test_generate_happy_path():
    mock = AsyncMock(return_value=_resp(_GOOD))
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        summary, findings, model = await generate_assessment(_inputs(), model="m1")
    assert mock.await_count == 1
    assert summary.startswith("## What went well")
    assert len(findings) == 2
    assert findings[0]["category"] == "gap"
    assert findings[0]["severity"] == "blocking"
    assert findings[0]["event_seq"] == 2
    assert findings[1]["severity"] is None
    assert model == "m1"
    # model override threaded into the call
    assert mock.await_args.kwargs["model"] == "m1"


# ---------------------------------------------------------------------------
# _strip_fences
# ---------------------------------------------------------------------------


def test_strip_fences_multiline_with_lang_tag():
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_multiline_no_lang_tag():
    assert _strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_no_fence_passthrough():
    assert _strip_fences('{"a": 1}') == '{"a": 1}'


def test_strip_fences_single_line_no_trailing_newline():
    # A single-line fence with no newline after the opening ``` used
    # to leave the opening fence in place → json.loads failed and burned the retry.
    assert _strip_fences('```{"a": 1}```') == '{"a": 1}'
    assert _strip_fences('```json {"a": 1}```') == '{"a": 1}'
    assert _strip_fences('```json{"a": 1}```') == '{"a": 1}'


def test_strip_fences_opening_fence_no_closing():
    # Model emitted the opening fence + body but forgot the closing fence.
    assert _strip_fences('```json\n{"a": 1}') == '{"a": 1}'


@_session_loop
async def test_generate_single_line_fence_parses_without_retry():
    """A single-line-fenced reply parses on the first attempt."""
    one_line = "```" + _GOOD + "```"
    mock = AsyncMock(return_value=_resp(one_line))
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        summary, findings, _ = await generate_assessment(_inputs(), model="m1")
    assert mock.await_count == 1  # no retry burned
    assert summary.startswith("## What went well")
    assert len(findings) == 2


@_session_loop
async def test_generate_strips_markdown_fences():
    fenced = f"```json\n{_GOOD}\n```"
    mock = AsyncMock(return_value=_resp(fenced))
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        summary, findings, _ = await generate_assessment(_inputs(), model="m1")
    assert summary.startswith("## What went well")
    assert len(findings) == 2


@_session_loop
async def test_generate_retries_once_on_malformed_then_succeeds():
    mock = AsyncMock(side_effect=[_resp("not json at all"), _resp(_GOOD)])
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        summary, findings, _ = await generate_assessment(_inputs(), model="m1")
    assert mock.await_count == 2
    assert len(findings) == 2


@_session_loop
async def test_generate_retries_once_on_schema_violation_then_succeeds():
    bad_schema = json.dumps({"summary_md": "x", "findings": [{"category": "nope"}]})
    mock = AsyncMock(side_effect=[_resp(bad_schema), _resp(_GOOD)])
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        summary, findings, _ = await generate_assessment(_inputs(), model="m1")
    assert mock.await_count == 2
    assert len(findings) == 2


@_session_loop
async def test_generate_raises_after_two_bad_replies():
    mock = AsyncMock(side_effect=[_resp("garbage"), _resp("still garbage")])
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        with pytest.raises(AssessmentGenerationError):
            await generate_assessment(_inputs(), model="m1")
    assert mock.await_count == 2


@_session_loop
async def test_generate_empty_choices_funnels_into_retry():
    """An empty `choices` reply must not raise IndexError — it degrades to a retry."""
    empty = SimpleNamespace(choices=[])
    mock = AsyncMock(side_effect=[empty, _resp(_GOOD)])
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        _, findings, _ = await generate_assessment(_inputs(), model="m1")
    assert mock.await_count == 2
    assert len(findings) == 2


@_session_loop
async def test_generate_uses_env_default_model(monkeypatch):
    monkeypatch.setattr(
        "vibony_backend.assessment.generate.ASSESSMENT_MODEL", "env-default-model"
    )
    mock = AsyncMock(return_value=_resp(_GOOD))
    with patch("vibony_backend.assessment.generate.litellm.acompletion", mock):
        _, _, model = await generate_assessment(_inputs())  # no model param
    assert model == "env-default-model"
    assert mock.await_args.kwargs["model"] == "env-default-model"
