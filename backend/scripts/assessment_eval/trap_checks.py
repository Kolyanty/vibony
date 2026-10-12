"""Deterministic trap checks — the mechanical layer of the trap judge.

Where a trap has a **mechanical signature** (an AST / grep fact about the final
code, or — later — running the acceptance/equivalence test in the sandbox), a
deterministic check beats an LLM judge: it is free, reproducible, and immune to the
self-leniency the LLM layer guards against. Where it does not, the trap falls
through to the LLM judge (``trap_judge.llm_trap_judge``), anchored to ``detection``.

This module is the registry + concrete checkers for the task-01 traps that have a
clean mechanical signature: trap 0 (checkpoint omits optimizer state), trap 4 (omits
RNG state), trap 5 (omits the epoch counter) — three facets of the same bare
``torch.save`` dict, each an AST scan resolving the common
``checkpoint = {...}; torch.save(checkpoint, path)`` variable form, not just inline
dicts — and trap 3 (``torch.load`` without ``weights_only=False``). Traps 1 (returns last not best
weights) and 2 (best_state reference vs clone) are semantic — no registered check,
they fall through to the LLM judge.

A check takes the run's **final files** (``{path: content|None}``) and returns
:class:`CheckResult` with ``present``:

* ``True``  — the planted flaw is present in the final code.
* ``False`` — the flaw is absent (avoided or corrected).
* ``None``  — the check could not decide (file missing / unparseable / no signal);
  the caller then defers to the LLM judge.

Checks operate on final files only (not the sandbox) so they stay pure + unit-
testable with no Docker. A future sandbox-backed check (run the equivalence test,
) would be a separate registered checker with its own seam.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    present: bool | None
    detail: str


# (task_id, trap_index) -> checker. A checker takes the final-files projection.
CheckFn = Callable[[dict[str, str | None]], CheckResult]
_REGISTRY: dict[tuple[str, int], CheckFn] = {}


def register_check(task_id: str, trap_index: int, fn: CheckFn) -> None:
    """Register a deterministic checker for one (task, trap). Overwrites on re-reg."""
    _REGISTRY[(task_id, trap_index)] = fn


def resolve_check(task_id: str, trap_index: int) -> CheckFn | None:
    """The deterministic checker for (task, trap), or ``None`` (→ LLM judge)."""
    return _REGISTRY.get((task_id, trap_index))


# ---------------------------------------------------------------------------
# Worked example: task-01 trap 0 — checkpoint omits optimizer state
# ---------------------------------------------------------------------------
#
# Contract (tasks/task-01-training-loop/spec.yaml, expected_ai_pitfalls[0]):
#   flawed_behavior — saves only model.state_dict(); no optimizer/RNG/epoch state.
#   check — "the saved checkpoint dict lacks optimizer_state_dict / rng-state keys".
#
# Signature: parse train.py, find torch.save(...) calls, inspect what is saved.
# A save of a dict carrying an optimizer-state key → flaw ABSENT (fixed). A save of
# a bare model.state_dict() (or a dict with no optimizer key) → flaw PRESENT. No
# torch.save at all → indeterminate (let the LLM judge read the transcript).

# Substrings that mark a checkpoint dict key (or saved expression) as carrying
# optimizer state. Matched case-insensitively against dict-literal string keys.
_OPTIMIZER_KEY_MARKERS = ("optim",)


def _torch_save_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``*.save(...)`` call in the tree (covers ``torch.save``).

    Only the attribute form — a bare ``save(...)`` Name is NOT matched: a
    ``from x import dump as save`` alias would be a false positive, and a wrong
    ``saves optimizer/RNG`` verdict silently suppresses a real trap fire (worse than
    deferring to the LLM). The ``from torch import save`` alias is rare; it falls
    through to the LLM judge. The attribute owner stays unpinned (``torch.save`` vs a
    re-import) because a non-checkpoint ``.save`` yields no dict/state_dict signal
    anyway → None."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "save"
    ]


def _dict_string_keys(node: ast.Dict) -> list[str]:
    keys: list[str] = []
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.append(k.value)
    return keys


def _is_state_dict_call(node: ast.expr) -> bool:
    """True for ``<x>.state_dict()`` — a bare-weights save with no optimizer state."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "state_dict"
    )


# RNG-state markers (task-01 trap 4) — dict keys, and value calls that *produce*
# RNG state (torch.get_rng_state(), random.getstate(), np.random.get_state()).
# Key markers stay specific: a bare "random" would false-match `random_seed`, but
# saving a *seed* (not the state) is exactly the trap-4 mistake — matching it would
# wrongly clear the flaw. The real-state case is caught by the value attrs anyway.
_RNG_KEY_MARKERS = ("rng", "random_state")
_RNG_VALUE_ATTRS = ("get_rng_state", "getstate", "get_state")

# Epoch-counter markers (task-01 trap 5 — checkpoint omits the epoch number, so resume
# restarts from 0). Key-only: the counter is a plain int, no distinctive value call.
# The anti-markers exclude the early-stopping patience counter ("epochs_no_improve"),
# which also contains "epoch" but is NOT the resume counter — matching it would wrongly
# clear the flaw when the actual epoch counter is absent.
_EPOCH_KEY_MARKERS = ("epoch",)
_EPOCH_KEY_ANTIMARKERS = ("improve", "patience", "no_improve")


# name -> source-ordered [(lineno, dict-literal)] for resolving the common
# ``checkpoint = {...}; torch.save(checkpoint, path)`` form, where the save arg is a
# bare Name rather than an inline Dict. Without this, real AI-written code (which
# almost always builds the dict in a variable first) falls through to the LLM judge
# on traps 0/3/4 — the deterministic layer never bites.
DictAssignments = dict[str, list[tuple[int, ast.Dict]]]


def _dict_assignments(tree: ast.AST) -> DictAssignments:
    """Map every ``name = {<dict literal>}`` assignment, source-ordered per name.

    Only single-target assignments of a **dict literal** are tracked. Dicts built
    via ``dict(...)``, ``cp.update(...)`` or per-key ``cp['k'] = ...`` stay
    unresolved (→ ``None`` → LLM judge) — the honest fallback, never a wrong verdict.
    Scope is not modelled: a name assigned a dict in two functions resolves by line
    number across the whole module, which is fine for the single-``train.py`` bench.
    """
    out: DictAssignments = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            out.setdefault(node.targets[0].id, []).append((node.lineno, node.value))
    for entries in out.values():
        entries.sort(key=lambda e: e[0])
    return out


def _resolve_saved_dict(obj: ast.expr, assignments: DictAssignments) -> ast.Dict | None:
    """The dict literal a ``torch.save`` first-arg denotes.

    An inline ``ast.Dict`` is itself; a bare ``Name`` resolves to the nearest
    **preceding** ``name = {...}`` assignment (last one at or above the use's line —
    so a later reassignment of the same name doesn't clobber an earlier save).
    ``None`` if unresolvable, including a save that precedes every assignment of the
    name (a forward reference — never guess the later dict) → defer to the LLM judge.
    """
    if isinstance(obj, ast.Dict):
        return obj
    if isinstance(obj, ast.Name):
        entries = assignments.get(obj.id)
        if entries:
            ref_line = getattr(obj, "lineno", None)
            preceding = [d for ln, d in entries if ref_line is None or ln <= ref_line]
            if preceding:
                return preceding[-1]
    return None


def _saves_optimizer_state(
    call: ast.Call, assignments: DictAssignments | None = None
) -> bool | None:
    """Does this ``torch.save(obj, path)`` save optimizer state? (trap 0)

    ``True`` — dict with an optimizer-ish key (or an ``optimizer.state_dict()``
    value); ``False`` — dict without one, or a bare ``model.state_dict()``;
    ``None`` — ``obj`` not statically resolvable.
    """
    if not call.args:
        return None
    obj = call.args[0]
    saved = _resolve_saved_dict(obj, assignments or {})
    if saved is not None:
        keys = [k.lower() for k in _dict_string_keys(saved)]
        if any(m in key for key in keys for m in _OPTIMIZER_KEY_MARKERS):
            return True
        # A dict whose *value* is optimizer.state_dict() under a non-"optim" key.
        for val in saved.values:
            if _is_state_dict_call(val) and isinstance(val.func, ast.Attribute):
                owner = val.func.value
                if isinstance(owner, ast.Name) and "optim" in owner.id.lower():
                    return True
        return False
    if _is_state_dict_call(obj):
        return False  # torch.save(model.state_dict(), ...) — bare weights
    return None


def _saves_rng_state(
    call: ast.Call, assignments: DictAssignments | None = None
) -> bool | None:
    """Does this ``torch.save(obj, path)`` save RNG state? (trap 4)

    ``True`` — dict with an ``rng``/``random`` key, or a value that is a
    ``*.get_rng_state()`` / ``*.getstate()`` / ``*.get_state()`` call; ``False`` —
    dict without any, or a bare ``model.state_dict()``; ``None`` — unresolvable.
    """
    if not call.args:
        return None
    obj = call.args[0]
    saved = _resolve_saved_dict(obj, assignments or {})
    if saved is not None:
        keys = [k.lower() for k in _dict_string_keys(saved)]
        if any(m in key for key in keys for m in _RNG_KEY_MARKERS):
            return True
        for val in saved.values:
            if (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr in _RNG_VALUE_ATTRS
            ):
                return True
        return False
    if _is_state_dict_call(obj):
        return False  # bare model weights — no RNG state
    return None


def _saves_epoch_state(
    call: ast.Call, assignments: DictAssignments | None = None
) -> bool | None:
    """Does this ``torch.save(obj, path)`` save the epoch counter? (trap 5)

    ``True`` — dict with an epoch-counter key (``epoch`` / ``start_epoch`` / ... but
    not the ``epochs_no_improve`` patience counter); ``False`` — dict without one, or
    a bare ``model.state_dict()``; ``None`` — ``obj`` not statically resolvable.
    """
    if not call.args:
        return None
    obj = call.args[0]
    saved = _resolve_saved_dict(obj, assignments or {})
    if saved is not None:
        keys = [k.lower() for k in _dict_string_keys(saved)]
        for key in keys:
            if any(m in key for m in _EPOCH_KEY_MARKERS) and not any(
                am in key for am in _EPOCH_KEY_ANTIMARKERS
            ):
                return True
        return False
    if _is_state_dict_call(obj):
        return False  # bare model weights — no epoch counter
    return None


def _checkpoint_carries_check(
    final_files: dict[str, str | None],
    predicate,
    carries_detail: str,
    missing_detail: str,
) -> CheckResult:
    """Shared scan for the checkpoint-omission traps (0 = optimizer, 4 = RNG, 5 = epoch).

    Parses ``train.py``, runs ``predicate`` over every ``torch.save`` call: any
    ``True`` (the state is saved somewhere) → flaw **absent**; else any ``False``
    (a save that demonstrably omits it) → flaw **present**; otherwise
    indeterminate (no save / unresolvable args → LLM).
    """
    code = final_files.get("train.py")
    if not code:
        return CheckResult(None, "train.py absent from final files")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return CheckResult(None, f"train.py unparseable: {exc}")

    calls = _torch_save_calls(tree)
    if not calls:
        return CheckResult(None, "no torch.save call found")

    assignments = _dict_assignments(tree)
    signals = [predicate(c, assignments) for c in calls]
    if any(s is True for s in signals):
        return CheckResult(False, carries_detail)
    if any(s is False for s in signals):
        return CheckResult(True, missing_detail)
    return CheckResult(None, "torch.save arg(s) not statically resolvable")


def check_training_loop_checkpoint_state(final_files: dict[str, str | None]) -> CheckResult:
    """Deterministic check for task-01 trap 0 (checkpoint omits optimizer state)."""
    return _checkpoint_carries_check(
        final_files,
        _saves_optimizer_state,
        "checkpoint saves optimizer state (flaw fixed)",
        "checkpoint saves only model weights — optimizer state omitted",
    )


def check_training_loop_rng_state(final_files: dict[str, str | None]) -> CheckResult:
    """Deterministic check for task-01 trap 4 (RNG state not saved in checkpoint)."""
    return _checkpoint_carries_check(
        final_files,
        _saves_rng_state,
        "checkpoint saves RNG state (flaw fixed)",
        "checkpoint omits RNG state (torch / python random)",
    )


def check_training_loop_epoch_state(final_files: dict[str, str | None]) -> CheckResult:
    """Deterministic check for task-01 trap 5 (checkpoint omits the epoch counter)."""
    return _checkpoint_carries_check(
        final_files,
        _saves_epoch_state,
        "checkpoint saves epoch counter (flaw fixed)",
        "checkpoint omits epoch counter — resume restarts from epoch 0",
    )


# ---------------------------------------------------------------------------
# trap 3 — torch.load without weights_only=False (crashes on torch>=2.6 when the
# checkpoint carries non-tensor objects, e.g. RNG state)
# ---------------------------------------------------------------------------


def _torch_load_calls(tree: ast.AST) -> list[ast.Call]:
    """``torch.load(...)`` calls. Owner pinned to ``torch`` so ``json.load`` /
    ``pickle.load`` don't false-match; the bare-Name ``load(...)`` form is NOT
    matched (a ``from json import load`` alias passing ``weights_only=False`` would
    wrongly clear the flaw — a wrong ``absent`` suppresses a real fire). A rare
    ``from torch import load`` falls through to the LLM judge instead."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "load"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "torch"
    ]


def _load_has_weights_only_false(call: ast.Call) -> bool:
    """True iff the call passes ``weights_only=False`` explicitly."""
    for kw in call.keywords:
        if (
            kw.arg == "weights_only"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
        ):
            return True
    return False


def check_training_loop_torch_load_safety(final_files: dict[str, str | None]) -> CheckResult:
    """Deterministic check for task-01 trap 3 (torch.load missing weights_only=False).

    The flaw only *bites* when the checkpoint carries non-tensor state (RNG), so the
    check stays honest: a safe load (``weights_only=False``) → **absent**; an unsafe
    load while RNG state is saved → **present**; an unsafe load with no non-tensor
    state detected → indeterminate (runtime/torch-version dependent → LLM).
    """
    code = final_files.get("train.py")
    if not code:
        return CheckResult(None, "train.py absent from final files")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return CheckResult(None, f"train.py unparseable: {exc}")

    loads = _torch_load_calls(tree)
    if not loads:
        return CheckResult(None, "no torch.load call found")
    if any(_load_has_weights_only_false(c) for c in loads):
        return CheckResult(False, "torch.load uses weights_only=False (flaw fixed)")

    assignments = _dict_assignments(tree)
    saves_nontensor = any(
        _saves_rng_state(c, assignments) is True for c in _torch_save_calls(tree)
    )
    if saves_nontensor:
        return CheckResult(
            True,
            "torch.load lacks weights_only=False while the checkpoint carries "
            "non-tensor (RNG) state → load crashes on torch>=2.6",
        )
    return CheckResult(
        None,
        "torch.load lacks weights_only=False but no non-tensor state detected "
        "(runtime/torch-version dependent)",
    )


register_check("task-01-training-loop", 0, check_training_loop_checkpoint_state)
register_check("task-01-training-loop", 3, check_training_loop_torch_load_safety)
register_check("task-01-training-loop", 4, check_training_loop_rng_state)
register_check("task-01-training-loop", 5, check_training_loop_epoch_state)


__all__ = [
    "CheckResult",
    "CheckFn",
    "register_check",
    "resolve_check",
    "check_training_loop_checkpoint_state",
    "check_training_loop_rng_state",
    "check_training_loop_epoch_state",
    "check_training_loop_torch_load_safety",
]
