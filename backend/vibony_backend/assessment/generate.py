"""AI assessment generation.

One ``litellm.acompletion`` structured-output call that turns the assembled
inputs into the report: a 3-section ``summary_md`` blob + a structured
``findings`` list. Model-agnostic (DeepSeek default, Anthropic via litellm) —
the JSON schema is both requested via ``response_format`` *and* spelled out in the
prompt, so a provider that ignores ``response_format`` still gets valid JSON.

Robustness: the reply is parsed (markdown fences stripped) and validated
against :class:`AssessmentPayload`. On a parse/validation failure we retry **once**
with a "return valid JSON only" nudge; a second failure raises
:class:`AssessmentGenerationError` (the job marks the row ``failed``).

The prompt never reaches the candidate — it is fed the employer-only spec traps
on purpose. For a near-empty session the model is told to say so
plainly rather than invent activity.
"""

from __future__ import annotations

import json
import logging
import os

import litellm
from vibony_backend.llm_usage import completion
from pydantic import ValidationError

from vibony_backend.assessment import prompts
from vibony_backend.assessment.inputs import AssessmentInputs
from vibony_backend.assessment.schemas import AssessmentPayload
from vibony_backend.prompt_safety import wrap_untrusted
from vibony_backend.proxy import DEFAULT_MODEL
from vibony_backend.reasoning import strip_fences

logger = logging.getLogger(__name__)

# Default assessment model. Falls back to the shared proxy default (DeepSeek)
# — same one-place-for-the-default pattern as compaction.SUMMARY_MODEL. A future
# per-workspace Opus plan flag overrides per call, not here.
ASSESSMENT_MODEL = os.environ.get("VIBONY_ASSESSMENT_MODEL") or DEFAULT_MODEL

# A generous timeout — the assessment is a background job, not on a request path.
_TIMEOUT_SECONDS = 300


class AssessmentGenerationError(Exception):
    """Raised when the model fails to return valid JSON after one retry."""


def build_user_prompt(inputs: AssessmentInputs) -> str:
    """Fold spec + final code + transcript into the user message.

    Each of the three content blocks is candidate- or
    task-author-authored and therefore untrusted, so each is wrapped in an
    unforgeable untrusted-data fence (:func:`wrap_untrusted`). The section
    headers, the near-empty note and the write instruction are *our* framing and
    stay outside the fences. The system prompt's quarantine clause tells the model
    everything inside a fence is data, never an instruction.
    """
    parts: list[str] = []

    if inputs.is_near_empty:
        parts.append(prompts.NEAR_EMPTY_NOTE)

    parts.append(
        f"{prompts.SPEC_SECTION_HEADER}\n"
        + wrap_untrusted(inputs.spec_text or prompts.SPEC_FALLBACK, label="task spec")
    )
    parts.append(
        f"{prompts.FINAL_CODE_SECTION_HEADER}\n"
        + wrap_untrusted(
            inputs.final_code or prompts.FINAL_CODE_FALLBACK,
            label="candidate final code",
        )
    )
    parts.append(
        f"{prompts.TRANSCRIPT_SECTION_HEADER}\n"
        + wrap_untrusted(
            inputs.transcript or prompts.TRANSCRIPT_FALLBACK,
            label="session transcript",
        )
    )
    parts.append(prompts.WRITE_INSTRUCTION)
    return "\n\n".join(parts)


# The canonical fence-stripper lives in vibony_backend.reasoning (shared with the
# trap-bench judge + controller seam); re-exported here under the private name the
# assessment path and its regression tests already use.
_strip_fences = strip_fences


def _parse_payload(content: str | None) -> AssessmentPayload:
    """Parse + validate the model reply into :class:`AssessmentPayload`.

    Raises ``json.JSONDecodeError`` or ``pydantic.ValidationError`` on a bad reply.
    """
    raw = _strip_fences(content or "")
    data = json.loads(raw)
    return AssessmentPayload.model_validate(data)


def _content_of(response: object) -> str | None:
    """Best-effort extract of the reply text. Returns ``None`` on an empty/odd
    ``choices`` shape (e.g. a content-filter refusal) so it funnels into the same
    parse-failure → retry path rather than raising an opaque IndexError."""
    try:
        return response.choices[0].message.content  # type: ignore[attr-defined]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


async def generate_assessment(
    inputs: AssessmentInputs, *, model: str | None = None
) -> tuple[str, list[dict], str]:
    """Generate the assessment for one session's assembled inputs.

    Returns ``(summary_md, findings, model_used)`` where ``findings`` is a list of
    plain dicts (ready for the JSONB column). Raises
    :class:`AssessmentGenerationError` if the model can't produce valid JSON after
    one retry; any litellm/transport error propagates unchanged (the job marks the
    row ``failed`` either way).
    """
    used_model = model or ASSESSMENT_MODEL
    messages = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(inputs)},
    ]

    response = await completion("assessment",
        model=used_model,
        messages=messages,
        response_format={"type": "json_object"},
        timeout=_TIMEOUT_SECONDS,
    )
    content = _content_of(response)

    try:
        payload = _parse_payload(content)
    except (json.JSONDecodeError, ValidationError) as first_err:
        logger.warning(
            "generate_assessment: invalid JSON on first try (%s); retrying once",
            first_err,
        )
        messages.append({"role": "assistant", "content": content or ""})
        messages.append({"role": "user", "content": prompts.RETRY_NUDGE})
        retry = await completion("assessment",
            model=used_model,
            messages=messages,
            response_format={"type": "json_object"},
            timeout=_TIMEOUT_SECONDS,
        )
        retry_content = _content_of(retry)
        try:
            payload = _parse_payload(retry_content)
        except (json.JSONDecodeError, ValidationError) as second_err:
            raise AssessmentGenerationError(
                f"model did not return valid JSON after one retry: {second_err}"
            ) from second_err

    findings = [f.model_dump() for f in payload.findings]
    logger.info(
        "generate_assessment: ok model=%s findings=%d summary_chars=%d",
        used_model,
        len(findings),
        len(payload.summary_md),
    )
    return payload.summary_md, findings, used_model


__all__ = [
    "ASSESSMENT_MODEL",
    "AssessmentGenerationError",
    "build_user_prompt",
    "generate_assessment",
]
