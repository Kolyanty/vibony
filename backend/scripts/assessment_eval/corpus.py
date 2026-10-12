"""Frozen scenario corpus: freeze / load / build inputs.

A scenario *record* is the JSON unit that gets committed under ``corpus/<task_id>/``.
It fully defines one synthetic session, decoupled from the DB and the sandbox:

    {
      "task_id": "task-01-training-loop",
      "key": "single__best_state_mutation",
      "kind": "single",
      "persona": "...",                     # author intent (human-readable)
      "label": { "near_empty": false,       # ground truth the judge grades against
                 "triggered_pitfalls": [2],
                 "fought_ai": false,
                 "notes": "..." },
      "events": [[seq, type, payload], ...], # canonical event stream
      "final_files": { "train.py": "..." }   # candidate end-state code (or {})
    }

``build_inputs(record)`` reconstructs an :class:`AssessmentInputs` using the **real**
production renderers (``render_transcript`` / ``render_final_code`` / ``read_task_spec_from_disk``)
and computes ``is_near_empty`` exactly as ``assemble_assessment_inputs`` does — so the
harness exercises the same rendering path the live job uses, never a parallel mock.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from vibony_backend.assessment.inputs import (
    AssessmentInputs,
    read_task_spec_from_disk,
    render_final_code,
    render_task_spec,
    render_transcript,
)

from scripts.assessment_eval.personas import ScenarioLabel, ScenarioSpec

logger = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).parent / "corpus"


# ---------------------------------------------------------------------------
# Record <-> dict
# ---------------------------------------------------------------------------


def label_to_dict(label: ScenarioLabel) -> dict[str, Any]:
    return {
        "near_empty": label.near_empty,
        "triggered_pitfalls": list(label.triggered_pitfalls),
        "fought_ai": label.fought_ai,
        "notes": label.notes,
    }


def label_from_dict(data: dict[str, Any]) -> ScenarioLabel:
    return ScenarioLabel(
        near_empty=bool(data.get("near_empty", False)),
        triggered_pitfalls=[int(i) for i in data.get("triggered_pitfalls", [])],
        fought_ai=bool(data.get("fought_ai", False)),
        notes=str(data.get("notes", "")),
    )


def make_record(
    task_id: str,
    scenario: ScenarioSpec,
    events: list[tuple[int, str, dict[str, Any]]],
    final_files: dict[str, str | None],
) -> dict[str, Any]:
    """Bundle a scenario + its authored session into a JSON-serialisable record."""
    return {
        "task_id": task_id,
        "key": scenario.key,
        "kind": scenario.kind,
        "persona": scenario.persona,
        "label": label_to_dict(scenario.label),
        "events": [[int(s), t, p] for s, t, p in events],
        "final_files": dict(final_files),
    }


def record_events(record: dict[str, Any]) -> list[tuple[int, str, dict[str, Any]]]:
    """Normalise a record's ``events`` into the ``(seq, type, payload)`` tuples the
    renderers expect (JSON stores them as lists)."""
    out: list[tuple[int, str, dict[str, Any]]] = []
    for row in record.get("events", []):
        seq, etype, payload = row
        out.append((int(seq), str(etype), dict(payload) if payload else {}))
    return out


# ---------------------------------------------------------------------------
# Freeze / load
# ---------------------------------------------------------------------------


def scenario_path(task_id: str, key: str, *, base: Path | None = None) -> Path:
    return (base or CORPUS_DIR) / task_id / f"{key}.json"


def save_record(record: dict[str, Any], *, base: Path | None = None) -> Path:
    path = scenario_path(record["task_id"], record["key"], base=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    logger.info("save_record: %s (%d events)", path, len(record.get("events", [])))
    return path


def load_corpus(task_id: str, *, base: Path | None = None) -> list[dict[str, Any]]:
    """Load every frozen scenario record for a task (sorted by key)."""
    task_dir = (base or CORPUS_DIR) / task_id
    if not task_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(task_dir.glob("*.json")):
        records.append(json.loads(path.read_text()))
    return records


def corpus_task_ids(*, base: Path | None = None) -> list[str]:
    """Task ids that already have a frozen corpus directory."""
    root = base or CORPUS_DIR
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Inputs reconstruction (real renderers)
# ---------------------------------------------------------------------------


def build_inputs(record: dict[str, Any]) -> AssessmentInputs:
    """Reconstruct :class:`AssessmentInputs` from a frozen record (no DB).

    Uses the same renderers + the same ``is_near_empty`` rule as
    ``assemble_assessment_inputs`` — the harness must assess exactly what the live
    job would, not an approximation.
    """
    task_id = record["task_id"]
    spec = read_task_spec_from_disk(task_id)
    spec_text = render_task_spec(spec)

    transcript, t_stats = render_transcript(record_events(record))
    final_code, c_stats = render_final_code(record.get("final_files") or {})

    meaningful = t_stats.rendered - t_stats.lifecycle_rendered
    is_near_empty = c_stats.files == 0 and meaningful <= 0

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
    "CORPUS_DIR",
    "build_inputs",
    "corpus_task_ids",
    "label_from_dict",
    "label_to_dict",
    "load_corpus",
    "make_record",
    "record_events",
    "save_record",
    "scenario_path",
]
