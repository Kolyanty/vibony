"""Seat-reservation artifact and deterministic mechanics checks.

The paid behavior evaluation is owner-run. These tests check the artifact,
controlled-trap contracts and scenario matrix, plus the concurrent oversell,
retry deduplication and quantity-validation mechanics without model calls.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from scripts.assessment_eval.trap_scenarios import derive_trap_scenarios
from vibony_backend.assessment.inputs import parse_task_spec
from vibony_backend.tasks.importer import read_artifact
from vibony_backend.tasks.schemas import validate_task_artifact_fields

# Pure-asyncio tests still MUST run on the session loop (backend/CLAUDE.md trap #1):
# a function-scoped loop tears down and breaks the session-bound asyncpg pool for the
# next DB test. This file mixes sync + async, so decorate per-test, not module-level.
_session_loop = pytest.mark.asyncio(loop_scope="session")

TASK_DIR = Path(__file__).resolve().parents[2] / "tasks" / "task-02-seat-reservation"


# --- artifact / contract shape (pure) -------------------------------------------------


def test_read_artifact_projection():
    data = read_artifact(TASK_DIR)
    assert data is not None
    assert data.slug == "task-02-seat-reservation"
    assert data.title == "Seat Reservation Service"
    assert data.format_version == 1
    assert data.runtime_image == "vibony/sandbox-py:3.12"
    assert sorted(f["path"] for f in data.starter_files) == [
        "app.py",
        "pyproject.toml",
        "store.py",
    ]
    # private + projection files excluded from the candidate bundle.
    names = {f["path"] for f in data.starter_files}
    assert not ({"spec.yaml", "weakness.yaml", "INTERVIEWER.md", "_solution.py", "task.md"} & names)
    assert data.weakness.get("blindspots")


def test_artifact_passes_field_validation():
    data = read_artifact(TASK_DIR)
    assert data is not None
    # Must not raise — the importer applies these same rules.
    validate_task_artifact_fields(
        slug=data.slug,
        runtime_image=data.runtime_image,
        starter_files=data.starter_files,
        weakness=data.weakness,
        spec=data.spec,
    )


def test_spec_parses_three_full_trap_contracts():
    data = read_artifact(TASK_DIR)
    assert data is not None
    spec = parse_task_spec(data.slug, {**data.spec, "id": data.slug})
    assert spec is not None
    pitfalls = spec.expected_ai_pitfalls
    # Independent traps: race, idempotency, quantity validation.
    assert len(pitfalls) == 3
    assert [p.trap_type for p in pitfalls] == [
        "concurrency/race",
        "concurrency/race",
        "missing-check",
    ]
    assert [p.severity for p in pitfalls] == [
        "blocking",
        "degrading",
        "degrading",
    ]
    # Every trap carries the full controlled-trap contract (drives the bench scenarios).
    for p in pitfalls:
        assert p.trigger and p.flawed_behavior and p.correct_behavior and p.defense and p.check


def test_trap_scenario_matrix_is_fifteen_cells():
    data = read_artifact(TASK_DIR)
    assert data is not None
    spec = parse_task_spec(data.slug, {**data.spec, "id": data.slug})
    assert spec is not None
    scenarios = derive_trap_scenarios(spec)
    # 3 traps × {handoff, passive, correct, breaking, vague} — correct+breaking only
    # fire because every trap sets correct_behavior + check.
    assert len(scenarios) == 15
    kinds = sorted({s.kind for s in scenarios})
    assert kinds == ["breaking", "correct", "handoff", "passive", "vague"]


# --- trap mechanics (the race is real; the reference fixes it) ------------------------


def _load_task_module(name: str, filename: str):
    """Import a task source file under a unique module name, with the task dir on
    sys.path so its sibling imports (`from store import Store`) resolve. Caller cleans up."""
    spec = importlib.util.spec_from_file_location(name, TASK_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def task_modules():
    """`(app, store, solution)` modules from the task dir, isolated + cleaned up."""
    added = str(TASK_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(TASK_DIR))
    loaded = ["store", "app", "solution"]
    try:
        store = _load_task_module("store", "store.py")
        app = _load_task_module("app", "app.py")
        solution = _load_task_module("solution", "_solution.py")
        yield app, store, solution
    finally:
        for name in loaded:
            sys.modules.pop(name, None)
        if added:
            sys.path.remove(str(TASK_DIR))


async def _flawed_reserve(app, store, item_id, qty, request_id):
    """The straightforward non-atomic version the assistant writes by default."""
    current = await store.get(item_id)
    if current >= qty:
        await store.set(item_id, current - qty)
        return app.ReserveResult(ok=True, remaining=current - qty)
    return app.ReserveResult(ok=False, remaining=current)


async def _sweep(reserve, Store, *, stock=10, concurrency=50):
    store = Store({"seat-A": stock})
    results = await asyncio.gather(
        *(reserve(store, "seat-A", 1, f"req-{i}") for i in range(concurrency))
    )
    confirmed = sum(1 for r in results if r.ok)
    return confirmed, store.snapshot()["seat-A"]


@_session_loop
async def test_flawed_reserve_oversells(task_modules):
    app, store_mod, _ = task_modules

    async def reserve(store, item_id, qty, request_id):
        return await _flawed_reserve(app, store, item_id, qty, request_id)

    confirmed, _ = await _sweep(reserve, store_mod.Store, stock=10, concurrency=50)
    # The trap fires: far more confirmations than units existed (the race is real).
    assert confirmed > 10


@_session_loop
async def test_reference_solution_no_oversell(task_modules):
    _, store_mod, solution = task_modules
    confirmed, remaining = await _sweep(
        solution.reserve, store_mod.Store, stock=10, concurrency=50
    )
    assert confirmed == 10 and remaining == 0


@_session_loop
async def test_reference_solution_idempotent_retry(task_modules):
    _, store_mod, solution = task_modules
    store = store_mod.Store({"seat-A": 10})
    r1 = await solution.reserve(store, "seat-A", 3, "RID")
    r2 = await solution.reserve(store, "seat-A", 3, "RID")
    assert r1.ok and r2 == r1  # retry returns the original result
    assert store.snapshot()["seat-A"] == 7  # reserved once, not twice


@_session_loop
async def test_reference_solution_rejects_non_positive_qty(task_modules):
    _, store_mod, solution = task_modules
    store = store_mod.Store({"seat-A": 10})
    for bad in (0, -5):
        with pytest.raises(ValueError):
            await solution.reserve(store, "seat-A", bad, f"q{bad}")
    assert store.snapshot()["seat-A"] == 10  # stock untouched
