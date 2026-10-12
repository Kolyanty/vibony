"""LLM scenario author.

Turns a :class:`ScenarioSpec` into a concrete synthetic session — an event stream
plus the candidate's final files — by asking a model to role-play the session in the
**canonical event schema** the assessment renderer consumes
(``inputs._render_event``). The author is task-general: it is handed the parsed
``spec.yaml`` text, the task's scaffold files (for language/structure), and, when a
reference solution exists, that too for the ``clean`` scenario.

The ``near_empty`` scenario needs no model — it is a pure lifecycle log.

This module makes **real** LLM calls when invoked; it is exercised in CI only with
``litellm.acompletion`` mocked. Output is validated by round-tripping through
``render_transcript`` (a record that cannot be rendered is rejected before freezing).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import litellm

from vibony_backend.assessment.inputs import TaskSpec, render_task_spec, render_transcript
from vibony_backend.proxy import DEFAULT_MODEL

from scripts.assessment_eval.personas import ScenarioSpec

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKS_DIR = REPO_ROOT / "tasks"

_AUTHOR_MODEL_DEFAULT = DEFAULT_MODEL
_TIMEOUT_SECONDS = 300

# The event types + payload fields the assessment transcript renderer reads. Kept in
# sync with vibony_backend.assessment.inputs._render_event — if that grows a type,
# add it here so the author can emit it.
_EVENT_SCHEMA_DOC = """\
Each event is a JSON array [seq, type, payload] with a strictly increasing integer seq
starting at 1. Use ONLY these types and payload fields:

- ["session_start", {"task_id": "<id>"}]            // always first
- ["user_message", {"text": "..."}]                  // the CANDIDATE talking to the AI
- ["assistant_message", {"text": "..."}]             // the AI replying
- ["tool_call", {"tool_name": "edit_file"|"bash"|..., "input": {"file_path": "..."} or {"command": "..."}}]
- ["tool_result", {"output": "...", "is_error": false}]
- ["command_run", {"command": "..."}]                // a shell command the candidate ran
- ["command_output", {"command_id": "c1", "data": "..."}]  // stdout/stderr chunk
- ["command_finished", {"command_id": "c1", "exit_code": 0}] // pair command_output+finished by command_id
- ["file_changed", {"op": "modified"|"created"|"deleted", "file_path": "...", "source": "candidate"|"ai"}]
- ["diff_rejected", {"reason": "..."}]               // candidate rejected an AI-proposed diff
- ["session_end", {"reason": "submitted"|"timeout"|"interviewer"}]  // always last

Return ONLY a JSON object: {"events": [[seq, type, payload], ...], "final_files": {"<path>": "<full file content>"}}.
final_files is the candidate's END-STATE code (what survives at session end); omit unchanged scaffold files.
Make it realistic: real chat, real commands with plausible output, file_changed events for edits."""


def read_scaffold(task_id: str) -> dict[str, str]:
    """Read the task's candidate-visible scaffold files (skip the hidden reference)."""
    task_dir = TASKS_DIR / task_id
    files: dict[str, str] = {}
    if not task_dir.is_dir():
        return files
    for path in sorted(task_dir.glob("*.py")):
        if path.name.startswith("_"):  # _solution.py etc. — handled separately
            continue
        files[path.name] = path.read_text()
    return files


def read_reference(task_id: str) -> str | None:
    """Read a reference solution (``_solution.*``) if the task ships one."""
    task_dir = TASKS_DIR / task_id
    if not task_dir.is_dir():
        return None
    for path in sorted(task_dir.glob("_solution.*")):
        return path.read_text()
    return None


def near_empty_record_events(task_id: str) -> list[tuple[int, str, dict[str, Any]]]:
    """Pure lifecycle log (no candidate activity) — drives ``is_near_empty`` true."""
    return [
        (1, "session_start", {"task_id": task_id}),
        (2, "session_end", {"reason": "timeout"}),
    ]


def build_author_prompt(
    spec: TaskSpec,
    scaffold: dict[str, str],
    scenario: ScenarioSpec,
    *,
    reference: str | None = None,
) -> str:
    """Assemble the author user prompt (spec + scaffold + persona + schema)."""
    parts: list[str] = [
        "You are simulating a candidate's vibe-coding interview session to build a "
        "TEST FIXTURE. Produce a realistic synthetic session for the scenario below.",
        "=== TASK SPEC ===\n" + (render_task_spec(spec) or "(no spec)"),
    ]
    if scaffold:
        joined = "\n\n".join(f"# {name}\n{content}" for name, content in scaffold.items())
        parts.append("=== SCAFFOLD (starting code) ===\n" + joined)
    if reference and scenario.kind == "clean":
        parts.append(
            "=== REFERENCE SOLUTION (ground the clean final code on this) ===\n"
            + reference
        )
    parts.append("=== SCENARIO ===\n" + scenario.persona)
    parts.append("=== OUTPUT FORMAT ===\n" + _EVENT_SCHEMA_DOC)
    return "\n\n".join(parts)


def _parse_author_reply(content: str | None) -> dict[str, Any]:
    """Parse the author reply into ``{events, final_files}`` and sanity-check it."""
    text = (content or "").strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    data = json.loads(text)
    events = data.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("author reply has no events")
    norm: list[tuple[int, str, dict]] = []
    for row in events:
        seq, etype, payload = row
        norm.append((int(seq), str(etype), dict(payload) if payload else {}))
    # Round-trip through the real renderer — a record we cannot render is useless.
    rendered, _stats = render_transcript(norm)
    if not rendered.strip():
        raise ValueError("author events render to an empty transcript")
    return {
        "events": [[s, t, p] for s, t, p in norm],
        "final_files": dict(data.get("final_files") or {}),
    }


async def author_scenario(
    spec: TaskSpec,
    scaffold: dict[str, str],
    scenario: ScenarioSpec,
    *,
    reference: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Author one scenario → ``{events, final_files}``.

    ``near_empty`` short-circuits to a pure lifecycle log (no LLM). Everything else
    is one ``litellm.acompletion`` JSON-mode call, parsed + validated. Raises on an
    unusable reply (no retry here — re-run ``--regen-corpus``; the seed corpus is
    hand-authored and does not depend on this path).
    """
    if scenario.kind == "near_empty":
        events = near_empty_record_events(spec.task_id)
        return {"events": [[s, t, p] for s, t, p in events], "final_files": {}}

    used_model = model or _AUTHOR_MODEL_DEFAULT
    messages = [
        {
            "role": "system",
            "content": (
                "You generate realistic synthetic interview sessions as JSON test "
                "fixtures. Output only the JSON object requested — no prose."
            ),
        },
        {"role": "user", "content": build_author_prompt(spec, scaffold, scenario, reference=reference)},
    ]
    response = await litellm.acompletion(
        model=used_model,
        messages=messages,
        response_format={"type": "json_object"},
        timeout=_TIMEOUT_SECONDS,
    )
    content = response.choices[0].message.content
    record = _parse_author_reply(content)
    logger.info(
        "author_scenario: %s/%s -> %d events, %d files (model=%s)",
        spec.task_id,
        scenario.key,
        len(record["events"]),
        len(record["final_files"]),
        used_model,
    )
    return record


__all__ = [
    "author_scenario",
    "build_author_prompt",
    "near_empty_record_events",
    "read_reference",
    "read_scaffold",
]
