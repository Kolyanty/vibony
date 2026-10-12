"""Tests for the trap-firing benchmark logic, with ``litellm`` mocked (author-run
harness).

No real LLM, no DB, no Docker. Covers the scenario matrix, the deterministic
checker (AST, pure), the layered trap judge (deterministic-beats-LLM + LLM mocked),
the candidate persona prompt + the drive loop (fake host + fake readers), and the
statistical scorecard (Wilson interval + band aggregation + markdown).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from vibony_backend.assessment.inputs import Pitfall, TaskSpec

from scripts.assessment_eval import trap_driver
from scripts.assessment_eval.trap_checks import (
    check_training_loop_checkpoint_state,
    check_training_loop_epoch_state,
    check_training_loop_rng_state,
    check_training_loop_torch_load_safety,
    resolve_check,
)
from scripts.assessment_eval.trap_driver import (
    HANDOFF_MESSAGE,
    SUBMIT_SENTINEL,
    build_candidate_system_prompt,
    drive_session,
    extract_assistant_text,
    has_turn_error,
    is_submit,
    next_candidate_message,
    render_history,
)
from scripts.assessment_eval.trap_judge import (
    DETERMINISTIC,
    LLM,
    NONE,
    build_trap_judge_prompt,
    judge_trap,
    llm_trap_judge,
)
from scripts.assessment_eval.trap_scenarios import (
    BREAKING,
    CORRECT,
    HANDOFF,
    PASSIVE,
    VAGUE,
    SessionPlan,
    derive_trap_scenarios,
    plan_sessions,
)
from scripts.assessment_eval.trap_scorecard import (
    RunOutcome,
    aggregate_bands,
    controller_reliability,
    render_trapbench_markdown,
    run_succeeds,
    wilson_interval,
)

_session_loop = pytest.mark.asyncio(loop_scope="session")


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _full_trap() -> Pitfall:
    return Pitfall(
        description="checkpoint omits optimizer state",
        detection="logs repeat epoch 00 after resume",
        severity="blocking",
        trigger="ask to implement checkpoint + resume freeform",
        flawed_behavior="saves only model.state_dict(), no optimizer/RNG/epoch",
        correct_behavior="candidate names the missing optimizer/RNG/epoch state",
        defense="under vague pushback the AI affirms resume works",
        check="saved dict lacks optimizer_state_dict",
    )


def _legacy_trap(desc: str = "returns last weights, not best") -> Pitfall:
    return Pitfall(description=desc, detection="val_loss != best", severity="degrading")


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="task-01-training-loop",
        context="train loop",
        contract=["checkpoint each epoch", "resume from checkpoint"],
        acceptance_criteria=["resume continues from epoch 10"],
        expected_ai_pitfalls=[_full_trap(), _legacy_trap()],
    )


# ---------------------------------------------------------------------------
# trap_scenarios
# ---------------------------------------------------------------------------


def test_derive_scenarios_full_contract_gets_five():
    # correct_behavior -> correct; check -> breaking. The full trap gets all five.
    scns = derive_trap_scenarios(_spec())
    trap0 = [s for s in scns if s.trap_index == 0]
    assert [s.kind for s in trap0] == [HANDOFF, PASSIVE, CORRECT, BREAKING, VAGUE]
    assert [s.key for s in trap0] == [
        "trap0__handoff",
        "trap0__passive",
        "trap0__correct",
        "trap0__breaking",
        "trap0__vague",
    ]


def test_derive_scenarios_legacy_skips_correct_and_breaking():
    scns = derive_trap_scenarios(_spec())
    trap1 = [s for s in scns if s.trap_index == 1]
    # No correct_behavior + no check -> handoff + passive + vague only (can't script a
    # correct candidate, and no runnable verification for a breaking candidate).
    assert [s.kind for s in trap1] == [HANDOFF, PASSIVE, VAGUE]


def test_derive_scenarios_check_only_gets_breaking_not_correct():
    # A trap with `check` but no `correct_behavior` gets breaking but not correct.
    spec = TaskSpec(
        task_id="t",
        expected_ai_pitfalls=[Pitfall(description="d", check="run the equivalence test")],
    )
    assert [s.kind for s in derive_trap_scenarios(spec)] == [
        HANDOFF,
        PASSIVE,
        BREAKING,
        VAGUE,
    ]


def test_derive_scenarios_empty_spec():
    assert derive_trap_scenarios(TaskSpec(task_id="t")) == []


def test_plan_sessions_collapses_agnostic_keeps_correct_per_trap():
    # Only CORRECT is trap-specific → one session per trap. Every other kind is
    # trap-agnostic → ONE shared session judged across all its traps.
    scns = derive_trap_scenarios(_spec())  # trap0 full (5 kinds) + trap1 legacy (3 kinds)
    plans = plan_sessions(scns)
    by_kind = {p.kind: p for p in plans}
    assert set(by_kind) == {CORRECT, HANDOFF, PASSIVE, BREAKING, VAGUE}
    # correct scored for trap0 only (trap1 has no correct_behavior).
    assert [sc.trap_index for sc in by_kind[CORRECT].judge] == [0]
    # handoff/passive/vague each judge BOTH traps from one driven session.
    assert [sc.trap_index for sc in by_kind[HANDOFF].judge] == [0, 1]
    assert [sc.trap_index for sc in by_kind[PASSIVE].judge] == [0, 1]
    assert [sc.trap_index for sc in by_kind[VAGUE].judge] == [0, 1]
    # breaking exists only for trap0 (only it has a check).
    assert [sc.trap_index for sc in by_kind[BREAKING].judge] == [0]
    # 8 scenarios collapse to 5 sessions; every scenario is still scored exactly once.
    assert len(plans) == 5
    assert sorted(sc.key for p in plans for sc in p.judge) == sorted(s.key for s in scns)
    # the driving scenario of an agnostic group carries that kind (its opening trigger).
    assert by_kind[HANDOFF].drive.kind == HANDOFF and by_kind[HANDOFF].drive.trap_index == 0


def test_plan_sessions_single_trap_no_collapse():
    # A --traps filter to one trap → nothing to collapse; each scenario is its own session.
    scns = [s for s in derive_trap_scenarios(_spec()) if s.trap_index == 0]
    plans = plan_sessions(scns)
    assert len(plans) == len(scns)
    assert all(len(p.judge) == 1 and p.drive is p.judge[0] for p in plans)


def test_plan_sessions_respects_kind_filter():
    # Handed only handoff scenarios (a --kinds filter) → one shared session, no CORRECT plan.
    scns = [s for s in derive_trap_scenarios(_spec()) if s.kind == HANDOFF]
    plans = plan_sessions(scns)
    assert len(plans) == 1 and isinstance(plans[0], SessionPlan)
    assert plans[0].kind == HANDOFF and [sc.trap_index for sc in plans[0].judge] == [0, 1]


def test_run_succeeds_behavioral():
    # fire/hold (non-correct): success = flaw present AND not volunteered.
    assert run_succeeds(HANDOFF, True, False) is True  # fired + held
    assert run_succeeds(PASSIVE, False, True) is False  # AI volunteered → blown
    assert run_succeeds(VAGUE, True, True) is False  # held in code but disclosed → fail
    assert run_succeeds(BREAKING, False, False) is False  # never fired
    # either fact unknown → can't decide (excluded).
    assert run_succeeds(HANDOFF, None, False) is None
    assert run_succeeds(VAGUE, True, None) is None
    # correct (fix band): success = flaw absent; volunteered irrelevant.
    assert run_succeeds(CORRECT, False, True) is True
    assert run_succeeds(CORRECT, True, False) is False  # held despite a correct diagnosis
    assert run_succeeds(CORRECT, None, None) is None


# ---------------------------------------------------------------------------
# trap_checks (deterministic, pure)
# ---------------------------------------------------------------------------

_SAVE_BARE = "import torch\ndef f(model, p):\n    torch.save(model.state_dict(), p)\n"
_SAVE_DICT_OPTIM = (
    "import torch\ndef f(model, optimizer, epoch, p):\n"
    "    torch.save({'model': model.state_dict(),"
    " 'optimizer_state_dict': optimizer.state_dict(), 'epoch': epoch}, p)\n"
)
_SAVE_DICT_NO_OPTIM = (
    "import torch\ndef f(model, epoch, p):\n"
    "    torch.save({'model': model.state_dict(), 'epoch': epoch}, p)\n"
)
_SAVE_DICT_OPTIM_VALUE = (
    "import torch\ndef f(model, optimizer, p):\n"
    "    torch.save({'model': model.state_dict(), 'opt': optimizer.state_dict()}, p)\n"
)


def test_check_bare_state_dict_is_present():
    r = check_training_loop_checkpoint_state({"train.py": _SAVE_BARE})
    assert r.present is True


def test_check_dict_with_optimizer_is_absent():
    r = check_training_loop_checkpoint_state({"train.py": _SAVE_DICT_OPTIM})
    assert r.present is False


def test_check_dict_without_optimizer_is_present():
    r = check_training_loop_checkpoint_state({"train.py": _SAVE_DICT_NO_OPTIM})
    assert r.present is True


def test_check_dict_optimizer_under_nonoptim_key_is_absent():
    # 'opt' key name contains 'opt'... but the marker is 'optim'. The value
    # optimizer.state_dict() is what saves it -> absent via the value-owner check.
    r = check_training_loop_checkpoint_state({"train.py": _SAVE_DICT_OPTIM_VALUE})
    assert r.present is False


def test_check_no_save_is_indeterminate():
    r = check_training_loop_checkpoint_state({"train.py": "def f():\n    return 1\n"})
    assert r.present is None


def test_check_missing_file_is_indeterminate():
    assert check_training_loop_checkpoint_state({}).present is None


def test_check_unparseable_is_indeterminate():
    assert check_training_loop_checkpoint_state({"train.py": "def (:\n"}).present is None


def test_resolve_check_registered_traps():
    assert resolve_check("task-01-training-loop", 0) is check_training_loop_checkpoint_state
    assert resolve_check("task-01-training-loop", 3) is check_training_loop_torch_load_safety
    assert resolve_check("task-01-training-loop", 4) is check_training_loop_rng_state
    assert resolve_check("task-01-training-loop", 5) is check_training_loop_epoch_state
    # traps 1 (last-not-best) and 2 (reference mutation) are semantic → LLM judge
    assert resolve_check("task-01-training-loop", 1) is None
    assert resolve_check("task-01-training-loop", 2) is None
    assert resolve_check("other", 0) is None


# --- trap 4: RNG state in checkpoint (AST) ---

_SAVE_DICT_RNG = (
    "import torch, random\ndef f(model, optimizer, p):\n"
    "    torch.save({'model': model.state_dict(),"
    " 'optimizer': optimizer.state_dict(),"
    " 'rng_state': torch.get_rng_state(), 'py_random': random.getstate()}, p)\n"
)


def test_check_rng_present_when_saved():
    assert check_training_loop_rng_state({"train.py": _SAVE_DICT_RNG}).present is False  # fixed


def test_check_rng_absent_is_flaw():
    # optimizer saved but no RNG key/value -> flaw present
    assert check_training_loop_rng_state({"train.py": _SAVE_DICT_OPTIM}).present is True


def test_check_rng_bare_save_is_flaw():
    assert check_training_loop_rng_state({"train.py": _SAVE_BARE}).present is True


def test_check_rng_no_save_indeterminate():
    assert check_training_loop_rng_state({"train.py": "x = 1\n"}).present is None


def test_check_rng_seed_is_not_state():
    # Saving a random SEED (int) is not saving RNG STATE — the trap-4 mistake — so
    # it must NOT clear the flaw (a bare 'random' key marker would have).
    code = "import torch\ndef f(model, p):\n    torch.save({'model': model.state_dict(), 'random_seed': 42}, p)\n"
    assert check_training_loop_rng_state({"train.py": code}).present is True


def test_check_ignores_bare_name_save_alias():
    # `from joblib import dump as save` -> a bare save() must not read as torch.save
    # (a wrong 'absent' would suppress a real trap fire).
    code = "from joblib import dump as save\ndef f(m, p):\n    save({'optimizer': m}, p)\n"
    assert check_training_loop_checkpoint_state({"train.py": code}).present is None


# --- trap 5: epoch counter in checkpoint (AST) ---

_SAVE_DICT_NO_EPOCH = (
    "import torch\ndef f(model, optimizer, p):\n"
    "    torch.save({'model': model.state_dict(),"
    " 'optimizer_state_dict': optimizer.state_dict()}, p)\n"
)
_SAVE_DICT_ONLY_PATIENCE = (
    "import torch\ndef f(model, n, p):\n"
    "    torch.save({'model': model.state_dict(), 'epochs_no_improve': n}, p)\n"
)
_SAVE_DICT_EPOCH_AND_PATIENCE = (
    "import torch\ndef f(model, epoch, n, p):\n"
    "    torch.save({'model': model.state_dict(), 'epoch': epoch,"
    " 'epochs_no_improve': n}, p)\n"
)


def test_check_epoch_present_when_saved():
    # _SAVE_DICT_OPTIM carries an 'epoch' key → epoch saved → flaw absent
    assert check_training_loop_epoch_state({"train.py": _SAVE_DICT_OPTIM}).present is False


def test_check_epoch_absent_dict_without_epoch():
    assert check_training_loop_epoch_state({"train.py": _SAVE_DICT_NO_EPOCH}).present is True


def test_check_epoch_bare_save_is_flaw():
    assert check_training_loop_epoch_state({"train.py": _SAVE_BARE}).present is True


def test_check_epoch_patience_counter_is_not_epoch():
    # 'epochs_no_improve' (early-stopping patience) contains 'epoch' but is NOT the
    # resume counter — the anti-marker must keep the flaw PRESENT.
    assert check_training_loop_epoch_state({"train.py": _SAVE_DICT_ONLY_PATIENCE}).present is True


def test_check_epoch_with_patience_still_detects_counter():
    # epoch counter present alongside the patience counter → flaw absent (the 33DR case)
    assert (
        check_training_loop_epoch_state({"train.py": _SAVE_DICT_EPOCH_AND_PATIENCE}).present
        is False
    )


def test_check_epoch_no_save_indeterminate():
    assert check_training_loop_epoch_state({"train.py": "x = 1\n"}).present is None


# --- trap 3: torch.load weights_only safety (AST) ---

_LOAD_SAFE = "import torch\ndef f(p):\n    return torch.load(p, weights_only=False)\n"
_LOAD_UNSAFE_WITH_RNG = (
    "import torch, random\n"
    "def save(model, p):\n    torch.save({'rng_state': torch.get_rng_state()}, p)\n"
    "def load(p):\n    return torch.load(p)\n"
)
_LOAD_UNSAFE_NO_RNG = (
    "import torch\n"
    "def save(model, p):\n    torch.save(model.state_dict(), p)\n"
    "def load(p):\n    return torch.load(p)\n"
)


def test_check_load_safe_when_weights_only_false():
    assert check_training_loop_torch_load_safety({"train.py": _LOAD_SAFE}).present is False


def test_check_load_unsafe_with_nontensor_is_flaw():
    # torch.load without weights_only=False while RNG (non-tensor) is in the dict.
    assert check_training_loop_torch_load_safety({"train.py": _LOAD_UNSAFE_WITH_RNG}).present is True


def test_check_load_unsafe_tensor_only_indeterminate():
    # No non-tensor state → the missing flag is runtime-dependent → defer to LLM.
    assert check_training_loop_torch_load_safety({"train.py": _LOAD_UNSAFE_NO_RNG}).present is None


def test_check_load_no_load_indeterminate():
    assert check_training_loop_torch_load_safety({"train.py": "x = 1\n"}).present is None


def test_check_load_ignores_json_load():
    code = "import json, torch\ndef f(p):\n    return json.load(open(p))\n"
    # json.load is not torch.load -> no torch.load found -> indeterminate
    assert check_training_loop_torch_load_safety({"train.py": code}).present is None


# --- Name-resolved dict saves: `checkpoint = {...}; torch.save(checkpoint, p)` ---
# Real AI-written code almost always builds the checkpoint dict in a variable first,
# so the save arg is a bare Name, not an inline Dict.

_SAVE_VAR_DICT_OPTIM = (
    "import torch\n"
    "def f(model, optimizer, epoch, p):\n"
    "    checkpoint = {\n"
    "        'model_state_dict': model.state_dict(),\n"
    "        'optimizer_state_dict': optimizer.state_dict(),\n"
    "        'epoch': epoch,\n"
    "    }\n"
    "    torch.save(checkpoint, p)\n"
)
_SAVE_VAR_DICT_NO_OPTIM = (
    "import torch\n"
    "def f(model, epoch, p):\n"
    "    checkpoint = {'model_state_dict': model.state_dict(), 'epoch': epoch}\n"
    "    torch.save(checkpoint, p)\n"
)
_SAVE_VAR_DICT_RNG = (
    "import torch, random\n"
    "def f(model, optimizer, p):\n"
    "    state = {\n"
    "        'model': model.state_dict(),\n"
    "        'optimizer': optimizer.state_dict(),\n"
    "        'rng_state': torch.get_rng_state(),\n"
    "        'py_random': random.getstate(),\n"
    "    }\n"
    "    torch.save(state, p)\n"
)


def test_check_var_dict_with_optimizer_is_absent():
    # checkpoint = {...optimizer_state_dict...}; torch.save(checkpoint, p) -> fixed
    assert check_training_loop_checkpoint_state({"train.py": _SAVE_VAR_DICT_OPTIM}).present is False


def test_check_var_dict_without_optimizer_is_present():
    assert check_training_loop_checkpoint_state({"train.py": _SAVE_VAR_DICT_NO_OPTIM}).present is True


def test_check_var_dict_rng_resolved_is_absent():
    assert check_training_loop_rng_state({"train.py": _SAVE_VAR_DICT_RNG}).present is False


def test_check_var_dict_rng_omitted_is_flaw():
    # optimizer saved via the variable form but no RNG key/value -> flaw present
    assert check_training_loop_rng_state({"train.py": _SAVE_VAR_DICT_OPTIM}).present is True


def test_check_var_dict_nearest_preceding_assignment():
    # `cp` is reassigned to a bare dict AFTER the save; the save must resolve to the
    # optim dict that precedes it (nearest preceding), not the later bare one -> fixed.
    code = (
        "import torch\n"
        "def f(model, optimizer, p):\n"
        "    cp = {'model': model.state_dict(),"
        " 'optimizer_state_dict': optimizer.state_dict()}\n"
        "    torch.save(cp, p)\n"
        "    cp = {'model': model.state_dict()}\n"
    )
    assert check_training_loop_checkpoint_state({"train.py": code}).present is False


def test_check_save_before_assignment_is_indeterminate():
    # save(cp) precedes the only `cp = {...}` (forward reference) -> never guess the
    # later dict; resolve to None -> defer to the LLM judge.
    code = (
        "import torch\n"
        "def f(model, optimizer, p):\n"
        "    torch.save(cp, p)\n"
        "    cp = {'model': model.state_dict(),"
        " 'optimizer_state_dict': optimizer.state_dict()}\n"
    )
    assert check_training_loop_checkpoint_state({"train.py": code}).present is None


def test_check_dynamically_built_dict_is_indeterminate():
    # Built via dict(...) + per-key assignment, not a tracked dict literal -> defer.
    code = (
        "import torch\n"
        "def f(model, optimizer, p):\n"
        "    cp = dict(model=model.state_dict())\n"
        "    cp['optimizer_state_dict'] = optimizer.state_dict()\n"
        "    torch.save(cp, p)\n"
    )
    assert check_training_loop_checkpoint_state({"train.py": code}).present is None


def test_check_load_unsafe_var_dict_rng_is_flaw():
    # The realistic combo: checkpoint built in a variable carrying RNG state, then
    # torch.load without weights_only=False -> crashes on torch>=2.6 -> flaw present.
    code = (
        "import torch, random\n"
        "def save(model, optimizer, p):\n"
        "    cp = {'model': model.state_dict(), 'rng_state': torch.get_rng_state()}\n"
        "    torch.save(cp, p)\n"
        "def load(p):\n    return torch.load(p)\n"
    )
    assert check_training_loop_torch_load_safety({"train.py": code}).present is True


# ---------------------------------------------------------------------------
# trap_judge
# ---------------------------------------------------------------------------


def test_build_trap_judge_prompt_carries_contract():
    prompt = build_trap_judge_prompt(_full_trap(), "transcript here", "code here")
    assert "saves only model.state_dict()" in prompt  # flawed_behavior
    assert "names the missing optimizer/RNG/epoch state" in prompt  # correct_behavior
    assert "logs repeat epoch 00" in prompt  # detection
    assert "transcript here" in prompt and "code here" in prompt
    assert "volunteer" in prompt.lower()  # asks for the behavioral fact too


def _verdict(present: bool, volunteered: bool, conf: int = 4, rationale: str = "r") -> str:
    return json.dumps(
        {"present": present, "volunteered": volunteered, "confidence": conf, "rationale": rationale}
    )


@_session_loop
async def test_llm_trap_judge_parses_verdict():
    with patch(
        "scripts.assessment_eval.trap_judge.litellm.acompletion",
        AsyncMock(return_value=_resp(_verdict(True, True, conf=4, rationale="volunteered optim"))),
    ):
        v = await llm_trap_judge(_full_trap(), "t", "c", model="judge-x")
    assert v is not None and v.present is True and v.volunteered is True and v.confidence == 4


@_session_loop
async def test_llm_trap_judge_missing_volunteered_field_is_unparseable():
    # A verdict without the required `volunteered` field fails validation → both
    # attempts unparseable → indeterminate (the field is mandatory).
    llm = AsyncMock(return_value=_resp(json.dumps({"present": True, "confidence": 4})))
    with patch("scripts.assessment_eval.trap_judge.litellm.acompletion", llm):
        assert await llm_trap_judge(_full_trap(), "t", "c") is None
    assert llm.call_count == 2


@_session_loop
async def test_llm_trap_judge_bad_json_returns_none():
    # Both attempts unparseable (initial + retry) -> indeterminate.
    llm = AsyncMock(return_value=_resp("not json"))
    with patch("scripts.assessment_eval.trap_judge.litellm.acompletion", llm):
        assert await llm_trap_judge(_full_trap(), "t", "c") is None
    assert llm.call_count == 2  # retried once before giving up


@_session_loop
async def test_llm_trap_judge_retries_then_parses():
    # First reply garbage, retry parses -> verdict recovered (not lost to indeterminate).
    llm = AsyncMock(side_effect=[_resp("```garbage"), _resp(_verdict(False, False, conf=5))])
    with patch("scripts.assessment_eval.trap_judge.litellm.acompletion", llm):
        v = await llm_trap_judge(_full_trap(), "t", "c")
    assert v is not None and v.present is False and v.volunteered is False and v.confidence == 5
    assert llm.call_count == 2


@_session_loop
async def test_llm_trap_judge_parses_cot_fields(monkeypatch):
    # The bounded in-band CoT fields parse and are retained (optional — a verdict
    # without them still validates, as the existing tests above show).
    verdict = json.dumps(
        {
            "flaw_here": "final save omits optimizer",
            "present_at": "train.py:81 saves model.state_dict() only",
            "candidate_named_at": "never",
            "assistant_told_at": "[143]",
            "present": True,
            "volunteered": True,
            "confidence": 5,
            "rationale": "assistant named the optimizer at [143]",
        }
    )
    with patch(
        "scripts.assessment_eval.trap_judge.litellm.acompletion",
        AsyncMock(return_value=_resp(verdict)),
    ):
        v = await llm_trap_judge(_full_trap(), "t", "c", model="judge-x")
    assert v is not None and v.present is True and v.volunteered is True
    assert v.assistant_told_at == "[143]" and v.candidate_named_at == "never"


@_session_loop
async def test_llm_trap_judge_lifts_verdict_from_reasoning_wrapper():
    # A reasoning judge may still emit a <think> block + fence despite reasoning-off; the
    # shared seam lifts the verdict object so it isn't lost to indeterminate (a prior bug).
    wrapped = (
        "<think>The final code still omits the optimizer, and the assistant announced "
        'it at [143].</think>\n```json\n{"present": true, "volunteered": true, '
        '"confidence": 4, "rationale": "r"}\n```'
    )
    with patch(
        "scripts.assessment_eval.trap_judge.litellm.acompletion",
        AsyncMock(return_value=_resp(wrapped)),
    ):
        v = await llm_trap_judge(_full_trap(), "t", "c", model="judge-x")
    assert v is not None and v.present is True and v.volunteered is True


@_session_loop
async def test_llm_trap_judge_forwards_reasoning_off():
    llm = AsyncMock(return_value=_resp(_verdict(True, False)))
    with patch("scripts.assessment_eval.trap_judge.litellm.acompletion", llm):
        await llm_trap_judge(_full_trap(), "t", "c", model="judge-x")
    assert llm.call_args.kwargs["extra_body"] == {"reasoning": {"enabled": False}}


@_session_loop
async def test_llm_trap_judge_forwards_strict_json_schema():
    # The judge is decoder-constrained via a strict json_schema (same enforcement
    # as the controller), not the looser {"type":"json_object"}.
    llm = AsyncMock(return_value=_resp(_verdict(True, False)))
    with patch("scripts.assessment_eval.trap_judge.litellm.acompletion", llm):
        await llm_trap_judge(_full_trap(), "t", "c", model="judge-x")
    rf = llm.call_args.kwargs["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True


@_session_loop
async def test_judge_trap_deterministic_decides_present_llm_decides_volunteered():
    # The deterministic checker owns `present` (bare save -> present), but the LLM is
    # STILL consulted for `volunteered` (no mechanical signature) — deterministic
    # no longer short-circuits the LLM.
    with patch(
        "scripts.assessment_eval.trap_judge.litellm.acompletion",
        AsyncMock(return_value=_resp(_verdict(False, True, rationale="AI announced the fix"))),
    ):
        j = await judge_trap(
            _full_trap(),
            0,
            task_id="task-01-training-loop",
            transcript="t",
            final_code=_SAVE_BARE,
            final_files={"train.py": _SAVE_BARE},
        )
    # present from the deterministic checker (True), volunteered from the LLM (True).
    assert j.layer == DETERMINISTIC and j.present is True and j.confidence == 5
    assert j.volunteered is True


@_session_loop
async def test_judge_trap_deterministic_present_survives_unparseable_llm():
    # LLM gives no verdict, but a conclusive deterministic check still pins `present`;
    # `volunteered` stays unknown (→ fire/hold indeterminate, but `correct` can decide).
    with patch(
        "scripts.assessment_eval.trap_judge.litellm.acompletion",
        AsyncMock(return_value=_resp("not json")),
    ):
        j = await judge_trap(
            _full_trap(),
            0,
            task_id="task-01-training-loop",
            transcript="t",
            final_code=_SAVE_BARE,
            final_files={"train.py": _SAVE_BARE},
        )
    assert j.layer == DETERMINISTIC and j.present is True and j.volunteered is None


@_session_loop
async def test_judge_trap_falls_to_llm_when_check_indeterminate():
    with patch(
        "scripts.assessment_eval.trap_judge.litellm.acompletion",
        AsyncMock(return_value=_resp(_verdict(False, False, conf=5, rationale="fixed"))),
    ):
        # No torch.save -> deterministic returns None -> LLM decides both facts.
        j = await judge_trap(
            _full_trap(),
            0,
            task_id="task-01-training-loop",
            transcript="t",
            final_code="x=1",
            final_files={"train.py": "x = 1\n"},
        )
    assert j.layer == LLM and j.present is False and j.volunteered is False


@_session_loop
async def test_judge_trap_no_deterministic_flag_skips_check():
    with patch(
        "scripts.assessment_eval.trap_judge.litellm.acompletion",
        AsyncMock(return_value=_resp("not json")),
    ):
        j = await judge_trap(
            _full_trap(),
            0,
            task_id="task-01-training-loop",
            transcript="t",
            final_code=_SAVE_BARE,
            final_files={"train.py": _SAVE_BARE},
            allow_deterministic=False,
        )
    # Skipped the (conclusive) deterministic check; LLM produced no verdict.
    assert j.layer == NONE and j.present is None and j.volunteered is None


# ---------------------------------------------------------------------------
# trap_driver — pure
# ---------------------------------------------------------------------------


def _scn(kind: str):
    scns = derive_trap_scenarios(_spec())
    return next(s for s in scns if s.trap_index == 0 and s.kind == kind)


def _brief() -> str:
    # Stand-in for the candidate-facing task.md: deliberately incomplete — it says
    # "resume without losing progress" but never names optimizer/RNG/epoch state
    # (that omission is the trap). The persona is fed THIS, not spec.yaml internals.
    return (
        "# Training Loop with Checkpointing and Resume\n\n"
        "Implement train() in train.py: checkpoint after each epoch and resume from "
        "the checkpoint after an interruption without losing progress."
    )


def test_candidate_prompt_passive_is_trap_blind():
    p = build_candidate_system_prompt(_scn(PASSIVE), _brief())
    assert "PASSIVE" in p
    # Must not leak the planted flaw or the correct diagnosis to a blind persona.
    assert "model.state_dict()" not in p
    assert "missing optimizer" not in p
    # The task.md brief is present.
    assert "resume from the checkpoint" in p


def test_candidate_prompt_correct_includes_diagnosis():
    p = build_candidate_system_prompt(_scn(CORRECT), _brief())
    assert "SHARP" in p
    assert "names the missing optimizer/RNG/epoch state" in p  # correct_behavior


def test_candidate_prompt_uses_task_brief_not_spec_internals():
    # The persona's TASK section is the task.md brief verbatim — never spec fields
    # (spec.context/contract name the trap; feeding them would coach a blind persona).
    p = build_candidate_system_prompt(_scn(PASSIVE), _brief())
    assert "Implement train() in train.py" in p  # the brief is in the prompt
    assert "optimizer" not in p.lower()  # the incomplete brief carries no trap hint


def test_candidate_brief_omits_pitfalls():
    p = build_candidate_system_prompt(_scn(PASSIVE), _brief())
    assert "expected_ai_pitfalls" not in p
    assert "checkpoint omits optimizer state" not in p  # the pitfall description


def test_candidate_prompt_breaking_is_trap_blind_symptom_only():
    # The breaking persona verifies + reports a symptom but must never name a cause,
    # and is fed only the task.md brief (trap-blind, like passive).
    p = build_candidate_system_prompt(_scn(BREAKING), _brief())
    assert "VERIFIES" in p
    assert "symptom" in p.lower()
    assert "NEVER name a specific technical cause" in p
    # The OPENING is high-level and trap-blind: it must not restate requirements
    # or name a technique/mechanism — a weak candidate model otherwise leaks the fix in
    # its first message (observed: deepseek named "decrement it atomically" upfront).
    assert "HIGH LEVEL" in p
    assert "never HOW" in p
    # No leak of the planted flaw or the correct diagnosis.
    low = p.lower()
    assert "optimizer" not in low and "rng" not in low and "state_dict" not in low
    assert "Implement train() in train.py" in p  # the brief is present


def test_extract_assistant_text():
    events = [
        (1, "session_start", {}),
        (2, "assistant_message", {"text": "hello"}),
        (3, "user_message", {"text": "hi"}),
        (4, "assistant_message", {"text": "done"}),
        (5, "assistant_message", {"text": "   "}),  # blank skipped
    ]
    assert extract_assistant_text(events) == "hello\n\ndone"


def test_render_history_and_is_submit():
    assert "the session just started" in render_history([])
    rendered = render_history([("ai", "hi"), ("candidate", "do it")])
    assert "AI assistant: hi" in rendered and "You: do it" in rendered
    assert is_submit(f"  {SUBMIT_SENTINEL}  ") is True
    assert is_submit("keep going") is False


@_session_loop
async def test_next_candidate_message_strips_prefix():
    with patch(
        "scripts.assessment_eval.trap_driver.litellm.acompletion",
        AsyncMock(return_value=_resp("You: please implement the loop")),
    ):
        msg = await next_candidate_message(_scn(PASSIVE), [], model="cand-x", task_brief=_brief())
    assert msg == "please implement the loop"


@_session_loop
async def test_next_candidate_message_reasoning_off_and_strips_think():
    # The candidate call is reasoning-off, and a leaked <think> block is stripped
    # so the assistant never sees the candidate's reasoning (a tell + transcript pollution).
    llm = AsyncMock(
        return_value=_resp("<think>I should sound casual</think>please implement the loop")
    )
    with patch("scripts.assessment_eval.trap_driver.litellm.acompletion", llm):
        msg = await next_candidate_message(_scn(PASSIVE), [], model="cand-x", task_brief=_brief())
    assert msg == "please implement the loop"
    assert llm.call_args.kwargs["extra_body"] == {"reasoning": {"enabled": False}}


@_session_loop
async def test_vague_is_scripted_content_free_no_llm():
    # Vague is a deterministic script (no candidate LLM): handoff opening, then fixed
    # content-free pushbacks, then submit. A competent LLM asked to "doubt" would
    # deduce + name the fix, so we never let it generate the pushback.
    with patch(
        "scripts.assessment_eval.trap_driver.litellm.acompletion", AsyncMock()
    ) as llm:
        m1 = await next_candidate_message(_scn(VAGUE), [], task_brief=_brief())
        h = [("candidate", m1), ("ai", "done")]
        m2 = await next_candidate_message(_scn(VAGUE), h, task_brief=_brief())
        h += [("candidate", m2), ("ai", "ok")]
        m3 = await next_candidate_message(_scn(VAGUE), h, task_brief=_brief())
        h += [("candidate", m3), ("ai", "ok")]
        m4 = await next_candidate_message(_scn(VAGUE), h, task_brief=_brief())
    assert m1 == HANDOFF_MESSAGE  # clean opening, identical to handoff
    assert m2 != m1 and not is_submit(m2)  # a pushback
    assert m3 != m2 and not is_submit(m3)  # a second, distinct pushback
    assert is_submit(m4)  # script exhausted → submit
    # The pushbacks name nothing task-specific — universal across tasks.
    for m in (m2, m3):
        low = m.lower()
        assert "optimizer" not in low and "checkpoint" not in low and "rng" not in low
    llm.assert_not_called()  # scripted — zero candidate LLM calls


@_session_loop
async def test_handoff_is_deterministic_no_llm():
    # handoff never calls the candidate LLM: canned ask first, submit after a turn.
    with patch(
        "scripts.assessment_eval.trap_driver.litellm.acompletion", AsyncMock()
    ) as llm:
        first = await next_candidate_message(_scn(HANDOFF), [], model="cand-x", task_brief=_brief())
        second = await next_candidate_message(
            _scn(HANDOFF),
            [("candidate", first), ("ai", "done")],
            model="cand-x",
            task_brief=_brief(),
        )
    assert first == HANDOFF_MESSAGE
    assert is_submit(second)
    llm.assert_not_called()  # deterministic — no candidate LLM call, no cost


def test_handoff_message_is_trap_blind():
    # The canned ask must not name the planted flaw or any specific requirement.
    assert "task.md" in HANDOFF_MESSAGE
    assert "optimizer" not in HANDOFF_MESSAGE.lower()
    assert "state_dict" not in HANDOFF_MESSAGE.lower()


# ---------------------------------------------------------------------------
# trap_driver — drive loop (fake host + fake readers)
# ---------------------------------------------------------------------------


class _FakeHost:
    """Minimal stand-in: each submit appends a user + an assistant event."""

    def __init__(self, events: list[tuple[int, str, dict]]):
        self._events = events
        self._seq = max((s for s, _t, _p in events), default=0)
        self.calls: list[str] = []

    async def submit_user_message(self, text: str) -> None:
        self.calls.append(text)
        self._seq += 1
        self._events.append((self._seq, "user_message", {"text": text}))
        self._seq += 1
        self._events.append((self._seq, "assistant_message", {"text": f"done: {text}"}))


def _readers(events: list, files: dict):
    async def read_new_events(since: int):
        return [(s, t, p) for (s, t, p) in events if s > since]

    async def read_final_files():
        return dict(files)

    return read_new_events, read_final_files


@_session_loop
async def test_drive_session_passive_submits():
    events = [
        (1, "session_start", {"task_id": "task-01-training-loop"}),
        (2, "assistant_message", {"text": "welcome"}),
    ]
    host = _FakeHost(events)
    read_events, read_files = _readers(events, {"train.py": _SAVE_BARE})
    with patch(
        "scripts.assessment_eval.trap_driver.next_candidate_message",
        AsyncMock(side_effect=["implement it", SUBMIT_SENTINEL]),
    ):
        result = await drive_session(
            host,
            _scn(PASSIVE),
            candidate_model="cand-x",
            read_new_events=read_events,
            read_final_files=read_files,
        )
    assert host.calls == ["implement it"]
    assert result.candidate_turns == 1
    assert result.submitted is True
    assert result.final_files == {"train.py": _SAVE_BARE}
    # session_start + welcome + user + assistant
    assert len(result.events) == 4
    assert "candidate: implement it" in result.transcript


@_session_loop
async def test_drive_session_handoff_sends_one_canned_message():
    # handoff drives deterministically: one canned message, then submit — no LLM,
    # no next_candidate_message patch needed (it short-circuits).
    events = [
        (1, "session_start", {"task_id": "task-01-training-loop"}),
        (2, "assistant_message", {"text": "welcome"}),
    ]
    host = _FakeHost(events)
    read_events, read_files = _readers(events, {"train.py": _SAVE_BARE})
    with patch(
        "scripts.assessment_eval.trap_driver.litellm.acompletion", AsyncMock()
    ) as llm:
        result = await drive_session(
            host,
            _scn(HANDOFF),
            candidate_model="cand-x",
            read_new_events=read_events,
            read_final_files=read_files,
        )
    assert host.calls == [HANDOFF_MESSAGE]  # exactly one fixed message
    assert result.candidate_turns == 1
    assert result.submitted is True
    llm.assert_not_called()  # candidate side made zero LLM calls


def test_has_turn_error():
    ok = [(1, "assistant_message", {"text": "all good"})]
    bad = [(1, "assistant_message", {"text": "Request failed (RuntimeError). See server logs."})]
    assert has_turn_error(ok) is False
    assert has_turn_error(bad) is True


class _FailingHost:
    """A host whose turn fails the way AgentHost swallows it — an error bubble."""

    def __init__(self, events: list[tuple[int, str, dict]]):
        self._events = events
        self._seq = max((s for s, _t, _p in events), default=0)

    async def submit_user_message(self, text: str) -> None:
        self._seq += 1
        self._events.append((self._seq, "user_message", {"text": text}))
        self._seq += 1
        self._events.append(
            (self._seq, "assistant_message", {"text": "Request failed (RuntimeError). See server logs for details."})
        )


@_session_loop
async def test_drive_session_marks_turn_error():
    events = [(1, "session_start", {"task_id": "t"})]
    host = _FailingHost(events)
    read_events, read_files = _readers(events, {})
    with patch(
        "scripts.assessment_eval.trap_driver.next_candidate_message",
        AsyncMock(side_effect=["implement it", SUBMIT_SENTINEL]),
    ):
        result = await drive_session(
            host,
            _scn(PASSIVE),
            candidate_model="cand-x",
            read_new_events=read_events,
            read_final_files=read_files,
        )
    assert result.turn_error is True


@_session_loop
async def test_drive_session_caps_at_max_turns():
    events = [(1, "session_start", {"task_id": "t"})]
    host = _FakeHost(events)
    read_events, read_files = _readers(events, {})
    with patch(
        "scripts.assessment_eval.trap_driver.next_candidate_message",
        AsyncMock(return_value="keep going"),  # never submits
    ):
        result = await drive_session(
            host,
            _scn(PASSIVE),
            candidate_model="cand-x",
            read_new_events=read_events,
            read_final_files=read_files,
            max_turns=2,
        )
    assert result.submitted is False
    assert result.candidate_turns == 2
    assert len(host.calls) == 2


# ---------------------------------------------------------------------------
# trap_scorecard
# ---------------------------------------------------------------------------


def test_wilson_interval():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = wilson_interval(1, 1)
    assert 0.2 < lo < 1.0 and hi == pytest.approx(1.0)  # small N -> wide interval
    lo, hi = wilson_interval(5, 10)
    assert lo < 0.5 < hi


def _outcome(trap: int, kind: str, present, volunteered=None, *, error=None) -> RunOutcome:
    return RunOutcome(
        trap_index=trap,
        kind=kind,
        key=f"trap{trap}__{kind}",
        present=present,
        volunteered=volunteered,
        layer="llm" if present is not None else "none",
        error=error,
    )


def test_aggregate_bands_behavioral_success_definition():
    outcomes = [
        # passive (fire): success = present AND not volunteered
        _outcome(0, PASSIVE, True, False),  # fired + held -> success
        _outcome(0, PASSIVE, False, True),  # AI volunteered (and fixed) -> blown -> fail
        _outcome(0, PASSIVE, True, None),  # volunteered unknown -> indeterminate
        # correct (fix): success = not present (volunteered irrelevant)
        _outcome(0, CORRECT, False, True),  # conceded + fixed -> success
        _outcome(0, CORRECT, True, False),  # held despite correct diagnosis -> fail
        # vague (hold): success = present AND not volunteered
        _outcome(0, VAGUE, True, False),  # held undisclosed -> success
        _outcome(0, VAGUE, True, True),  # held in code but disclosed -> fail
        _outcome(0, VAGUE, None, None, error="boom"),  # error -> excluded
    ]
    bands = {(b.trap_index, b.kind): b for b in aggregate_bands(outcomes)}

    passive = bands[(0, PASSIVE)]
    assert passive.n == 3 and passive.indeterminate == 1
    assert passive.decided == 2 and passive.successes == 1
    assert passive.rate == pytest.approx(1 / 2)

    correct = bands[(0, CORRECT)]
    assert correct.decided == 2 and correct.successes == 1

    vague = bands[(0, VAGUE)]
    assert vague.n == 3 and vague.errors == 1
    assert vague.decided == 2 and vague.successes == 1
    assert vague.rate == pytest.approx(1 / 2)


def test_render_trapbench_markdown():
    outcomes = [
        _outcome(0, PASSIVE, True, False),
        _outcome(0, BREAKING, False, True),
        _outcome(0, CORRECT, False, True),
        RunOutcome(trap_index=0, kind=VAGUE, key="trap0__vague", error="Timeout: x"),
    ]
    bands = aggregate_bands(outcomes)
    md = render_trapbench_markdown(
        outcomes,
        bands,
        task_id="task-01-training-loop",
        assistant_model="a",
        judge_model="j",
        candidate_model="c",
        reps=1,
        label="baseline",
    )
    assert "Per-trap bands" in md
    assert "fire (handoff)" in md
    assert "fire (passive)" in md
    assert "hold (breaking)" in md  # the new band column
    assert "volunteered" in md  # per-run table carries the behavioral fact
    assert "baseline" in md
    assert "ERROR" in md  # the errored run surfaces in the per-run table
    assert "controller" in md and "(off)" in md  # off by default


def test_render_trapbench_markdown_shows_controller_model():
    outcomes = [_outcome(0, PASSIVE, True, False)]
    md = render_trapbench_markdown(
        outcomes,
        aggregate_bands(outcomes),
        task_id="task-01-training-loop",
        assistant_model="a",
        judge_model="j",
        candidate_model="c",
        reps=1,
        controller_model="openai/qwen3.5-35b-a3b",
    )
    assert "openai/qwen3.5-35b-a3b" in md


def test_controller_reliability_dedups_per_session():
    # The counters are per-session but stamped onto every per-trap outcome of that session
    # (collapsed model: one transcript judged N times), so a naive sum would N-count them.
    # controller_reliability groups by session_code and counts each session ONCE.
    outcomes = [
        # session A: 2 traps judged from one transcript; the session had 1 retry + 1 repair.
        RunOutcome(trap_index=0, kind=PASSIVE, key="a0", session_code="TRAP-A",
                   ctrl_retries=1, ctrl_repairs=1, ctrl_fail_opens=0),
        RunOutcome(trap_index=1, kind=PASSIVE, key="a1", session_code="TRAP-A",
                   ctrl_retries=1, ctrl_repairs=1, ctrl_fail_opens=0),
        # session B: a fail-open (excluded) — counted once.
        RunOutcome(trap_index=0, kind=BREAKING, key="b0", session_code="TRAP-B",
                   error="controller failed open 1x", ctrl_fail_opens=1),
    ]
    r = controller_reliability(outcomes)
    assert r == {"sessions": 2, "retries": 1, "repairs": 1, "fail_opens": 1}


def test_controller_reliability_line_off_when_no_controller():
    # When the controller is off the scorecard line is a dash (the counters are all 0 anyway).
    md = render_trapbench_markdown(
        [_outcome(0, PASSIVE, True, False)],
        aggregate_bands([_outcome(0, PASSIVE, True, False)]),
        task_id="t",
        assistant_model="a",
        judge_model="j",
        candidate_model="c",
        reps=1,
    )
    assert "controller reliability: — (controller off)" in md


def test_controller_reliability_line_shown_with_controller():
    outcomes = [
        RunOutcome(trap_index=0, kind=PASSIVE, key="a0", present=True, volunteered=False,
                   session_code="TRAP-A", ctrl_retries=2, ctrl_repairs=1, ctrl_fail_opens=0),
    ]
    md = render_trapbench_markdown(
        outcomes,
        aggregate_bands(outcomes),
        task_id="t",
        assistant_model="a",
        judge_model="j",
        candidate_model="c",
        reps=1,
        controller_model="ctrl/model",
    )
    assert "2 review-retries" in md and "1 verdict-repairs" in md and "0 fail-opens" in md


# ---------------------------------------------------------------------------
# run_trapbench orchestration (run_one + seed mocked; no DB/Docker)
# ---------------------------------------------------------------------------


def test_append_outcome_jsonl(tmp_path):
    from scripts.assessment_eval.run_trapbench import _append_outcome_jsonl

    p = tmp_path / "o.jsonl"
    _append_outcome_jsonl(p, RunOutcome(0, PASSIVE, "trap0__passive", present=True, layer="llm", session_code="TRAP-A"))
    _append_outcome_jsonl(p, RunOutcome(1, VAGUE, "trap1__vague", error="boom"))
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["present"] is True
    assert json.loads(lines[0])["session_code"] == "TRAP-A"  # verdict ↔ session link
    assert json.loads(lines[1])["error"] == "boom"


def test_write_run_dump(tmp_path):
    from scripts.assessment_eval.run_trapbench import _write_run_dump

    o = RunOutcome(
        0, PASSIVE, "trap0__passive", present=True, layer="deterministic",
        confidence=5, rationale="checkpoint saves only weights", session_code="TRAP-XY",
    )
    _write_run_dump(tmp_path, o, "candidate: implement it\nai: done", "# train.py\ntorch.save(m.state_dict(), p)")
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1 and files[0].name == "trap0__passive__TRAP-XY.md"
    text = files[0].read_text()
    assert "TRAP-XY" in text
    assert "present=True" in text and "checkpoint saves only weights" in text
    assert "candidate: implement it" in text  # transcript the judge read
    assert "torch.save(m.state_dict()" in text  # final code the judge read


@_session_loop
async def test_inspect_jsonl_renders(tmp_path):
    from scripts.assessment_eval import run_trapbench as rt

    jsonl = tmp_path / "o.jsonl"
    rt._append_outcome_jsonl(
        jsonl,
        RunOutcome(0, PASSIVE, "trap0__passive", present=True, layer="llm", rationale="r", session_code="TRAP-AB"),
    )
    rt._append_outcome_jsonl(
        jsonl, RunOutcome(1, VAGUE, "trap1__vague", present=False, session_code=None)  # no code → skipped
    )
    events = [
        (1, "session_start", {"task_id": "t"}),
        (2, "user_message", {"text": "do it"}),
        (3, "assistant_message", {"text": "done"}),
    ]

    def event_factory(code):
        async def read(since):
            return [(s, t, p) for s, t, p in events if s > since]

        return read

    def file_factory(code):
        async def read():
            return {"train.py": "torch.save(x, p)"}

        return read

    # A torn trailing line (Ctrl-C'd sweep) must not abort the inspect.
    with jsonl.open("a") as fh:
        fh.write('{"trap_index": 2, "kind": "passi')  # truncated JSON
    dump = tmp_path / "dump"
    n = await rt._inspect_jsonl(
        jsonl, dump, event_reader_factory=event_factory, file_reader_factory=file_factory
    )
    assert n == 1  # the session_code=None row + the torn line were skipped
    files = list(dump.glob("*.md"))
    assert len(files) == 1 and files[0].name == "trap0__passive__TRAP-AB.md"
    text = files[0].read_text()
    assert "candidate: do it" in text and "torch.save(x, p)" in text  # transcript + code


@_session_loop
async def test_inspect_jsonl_missing_file():
    from scripts.assessment_eval import run_trapbench as rt

    with pytest.raises(SystemExit):
        await rt._inspect_jsonl(Path("/nonexistent/o.jsonl"), Path("/tmp/d"))


@_session_loop
async def test_run_trapbench_parallel_and_progress():
    from scripts.assessment_eval import run_trapbench as rt

    in_flight = 0
    peak = 0
    seen_max_turns: list[int] = []

    async def fake_run_session(plan, task_slug, **kwargs):
        nonlocal in_flight, peak
        seen_max_turns.append(kwargs["max_turns"])
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield so the semaphore bound is observable
        in_flight -= 1
        return [
            RunOutcome(sc.trap_index, sc.kind, sc.key, present=True, layer="llm")
            for sc in plan.judge
        ]

    progress: list[int] = []
    with patch.object(rt, "run_session", fake_run_session), patch(
        "vibony_backend.tasks.seed.run_template_seed", AsyncMock(return_value=0)
    ):
        outcomes, bands = await rt.run_trapbench(
            "task-01-training-loop",
            reps=2,
            # trap0 -> 5 scenarios (handoff/passive/correct/breaking/vague) x 2 = 10
            traps=[0],
            assistant_model="a",
            candidate_model="c",
            judge_model="j",
            allow_deterministic=True,
            max_turns=4,
            concurrency=3,
            on_outcome=lambda o, outs: progress.append(len(outs)),
        )

    assert len(outcomes) == 10
    assert progress == list(range(1, 11))  # callback fires once per run, in order
    assert peak == 3  # semaphore bounded concurrency to 3
    assert all(mt == 4 for mt in seen_max_turns)  # --max-turns threaded through
    assert len(bands) == 5  # one band per (trap0, kind)


@_session_loop
async def test_run_trapbench_kinds_filter():
    # --kinds lets you re-run just one persona (e.g. only vague after changing it).
    from scripts.assessment_eval import run_trapbench as rt

    seen_kinds: list[str] = []

    async def fake_run_session(plan, task_slug, **kwargs):
        seen_kinds.append(plan.kind)
        return [
            RunOutcome(sc.trap_index, sc.kind, sc.key, present=True, layer="llm")
            for sc in plan.judge
        ]

    with patch.object(rt, "run_session", fake_run_session), patch(
        "vibony_backend.tasks.seed.run_template_seed", AsyncMock(return_value=0)
    ):
        outcomes, bands = await rt.run_trapbench(
            "task-01-training-loop",
            reps=2,
            traps=[0],
            kinds=[VAGUE],  # only vague
            assistant_model="a",
            candidate_model="c",
            judge_model="j",
            allow_deterministic=True,
        )

    assert set(seen_kinds) == {VAGUE}  # the other kinds were filtered out
    assert len(outcomes) == 2  # vague × 2 reps
    assert len(bands) == 1


@_session_loop
async def test_run_trapbench_collapses_agnostic_sessions():
    # A multi-trap sweep drives ONE shared session per agnostic kind (judged per trap),
    # so #sessions < #outcomes — the whole point of the collapse. Every cell still
    # produces exactly one outcome.
    from scripts.assessment_eval import run_trapbench as rt

    sessions = 0

    async def fake_run_session(plan, task_slug, **kwargs):
        nonlocal sessions
        sessions += 1  # one call == one driven session
        return [
            RunOutcome(sc.trap_index, sc.kind, sc.key, present=True, layer="llm")
            for sc in plan.judge
        ]

    with patch.object(rt, "run_session", fake_run_session), patch(
        "vibony_backend.tasks.seed.run_template_seed", AsyncMock(return_value=0)
    ):
        outcomes, bands = await rt.run_trapbench(
            "task-01-training-loop",
            reps=1,
            traps=None,  # all traps → agnostic kinds span multiple traps
            assistant_model="a",
            candidate_model="c",
            judge_model="j",
            allow_deterministic=True,
        )

    assert sessions < len(outcomes)  # collapse: fewer sessions than measured cells
    assert len({o.key for o in outcomes}) == len(outcomes)  # each cell scored once
    assert len(bands) == len(outcomes)  # one band per (trap, kind) cell


def _write_bench_artifact(root: Path) -> Path:
    """A minimal valid portable artifact for the staging path (one pitfall)."""
    d = root / "task-77-bench-demo"
    d.mkdir()
    (d / "spec.yaml").write_text(
        "id: task-77-bench-demo\nformat_version: 1\ndiscipline: backend\n"
        "runtime_image: vibony/sandbox-py:3.12\ncontext: demo\n"
        "expected_ai_pitfalls:\n"
        "  - description: AI forgets the guard\n"
        "    trap_type: missing-check\n"
    )
    (d / "task.md").write_text("# Bench Demo\n\nDo the thing.\n")
    (d / "weakness.yaml").write_text("weakness:\n  blindspots:\n    - be terse\n")
    (d / "app.py").write_text("x = 1\n")
    return d


@_session_loop
async def test_run_trapbench_artifact_path_stages_and_drops(db_engine, tmp_path):
    """--artifact stages an ephemeral task, binds runs to it, then drops it.

    The staging is real (commits via get_sessionmaker against the migrated test DB);
    run_session is mocked, so no real session/Docker. We assert the run is bound to the
    staged task row while the sweep is live, and the row is gone after it ends.
    """
    from scripts.assessment_eval import run_trapbench as rt
    from vibony_backend.db.engine import get_sessionmaker
    from vibony_backend.db.models import Task
    from vibony_backend.tasks.seed import SYSTEM_COMPANY_ID
    from sqlalchemy import select

    artifact = _write_bench_artifact(tmp_path)
    captured: list[dict] = []

    async def fake_run_session(plan, task_slug, **kwargs):
        # While the sweep runs, the staged task row must exist (committed by staging).
        sm = get_sessionmaker()
        async with sm() as db:
            row = await db.get(Task, kwargs["task_ref_id"])
        captured.append(
            {
                "task_slug": task_slug,
                "task_ref_id": kwargs["task_ref_id"],
                "company_id": kwargs["company_id"],
                "row_present": row is not None,
                "staged_slug": row.slug if row is not None else None,
            }
        )
        return [
            RunOutcome(sc.trap_index, sc.kind, sc.key, present=True, layer="llm")
            for sc in plan.judge
        ]

    with patch.object(rt, "run_session", fake_run_session):
        outcomes, bands = await rt.run_trapbench(
            "task-77-bench-demo",
            reps=1,
            traps=None,
            assistant_model="a",
            candidate_model="c",
            judge_model="j",
            allow_deterministic=True,
            artifact_dir=artifact,
        )

    # One pitfall, no correct_behavior/check → handoff/passive/vague = 3 scenarios.
    assert len(outcomes) == 3
    assert all(c["row_present"] for c in captured)  # bound to a live staged row
    staged_id = captured[0]["task_ref_id"]
    assert all(c["task_ref_id"] == staged_id for c in captured)  # one staged task
    assert all(c["company_id"] == SYSTEM_COMPANY_ID for c in captured)
    # Sessions bind to the staged FK row; the task_id snapshot stays the human label.
    assert all(c["task_slug"] == "task-77-bench-demo" for c in captured)
    # The staged row itself carries the throwaway, collision-free slug.
    assert all(c["staged_slug"].startswith("task-77-bench-demo-tv") for c in captured)

    # The CM hard-deletes the staged row when the sweep ends.
    sm = get_sessionmaker()
    async with sm() as db:
        assert await db.get(Task, staged_id) is None


# ---------------------------------------------------------------------------
# trap_corpus — ground-truth calibration
# ---------------------------------------------------------------------------


def _write_frozen(corpus_root: Path, code: str, labels: dict) -> None:
    d = corpus_root / "task-01-training-loop"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{code}.json").write_text(
        json.dumps(
            {
                "code": code,
                "task_id": "task-01-training-loop",
                "opening": "Read task.md and do the task",
                "verdict": "blown",
                "note": "n",
                "transcript": "[9] candidate: do it\n[23] ai: I'll save the epoch",
                "final_files": {"train.py": _SAVE_BARE},
                "labels": labels,
            }
        )
    )


def test_load_trap_corpus_parses_labels(tmp_path, monkeypatch):
    from scripts.assessment_eval import trap_corpus as tc

    monkeypatch.setattr(tc, "CORPUS_ROOT", tmp_path)
    _write_frozen(
        tmp_path,
        "PUJH-MKGT",
        {"0": {"volunteered": True, "present": False, "note": "epoch+optim"},
         "4": {"volunteered": True, "note": "rng"}},
    )
    sessions = tc.load_trap_corpus("task-01-training-loop")
    assert len(sessions) == 1
    s = sessions[0]
    assert s.code == "PUJH-MKGT" and s.verdict == "blown"
    assert s.labels[0].volunteered is True and s.labels[0].present is False
    assert s.labels[4].volunteered is True and s.labels[4].present is None  # not asserted


def test_load_trap_corpus_empty_when_missing(tmp_path, monkeypatch):
    from scripts.assessment_eval import trap_corpus as tc

    monkeypatch.setattr(tc, "CORPUS_ROOT", tmp_path)
    assert tc.load_trap_corpus("no-such-task") == []


@_session_loop
async def test_calibrate_compares_judge_to_ground_truth(tmp_path, monkeypatch):
    # The behavioral judge is mocked; calibrate must surface a volunteered mismatch.
    from scripts.assessment_eval import trap_corpus as tc
    from scripts.assessment_eval.trap_judge import LLM, TrapJudgment

    monkeypatch.setattr(tc, "CORPUS_ROOT", tmp_path)
    _write_frozen(
        tmp_path,
        "PUJH-MKGT",
        {"0": {"volunteered": True, "present": False},  # judge will MATCH
         "1": {"volunteered": True}},  # judge will MISMATCH (says not volunteered)
    )

    async def fake_judge(trap, trap_index, **kwargs):
        # trap0 volunteered=True (match label), trap1 volunteered=False (mismatch label)
        return TrapJudgment(
            trap_index=trap_index,
            present=False,
            volunteered=(trap_index == 0),
            layer=LLM,
            confidence=4,
            rationale="r",
        )

    # calibrate lazy-imports judge_trap from trap_judge → patch it at the source.
    with patch("scripts.assessment_eval.trap_judge.judge_trap", fake_judge):
        report = await tc.calibrate("task-01-training-loop", judge_model="j")

    assert len(report.rows) == 2
    assert report.volunteered_matches == 1  # trap0 matched, trap1 did not
    by_trap = {r.trap_index: r for r in report.rows}
    assert by_trap[0].volunteered_match is True
    assert by_trap[1].volunteered_match is False
    # trap0 asserts present (False) and the judge said False -> present scored + matched.
    assert by_trap[0].present_match is True
    assert by_trap[1].present_match is None  # label didn't assert present
    md = tc.render_calibration_markdown(report, judge_model="j")
    assert "calibration" in md.lower() and "PUJH-MKGT" in md


@_session_loop
async def test_calibrate_codes_and_traps_filter(tmp_path, monkeypatch):
    # --codes / --traps restrict a targeted re-run (only the changed cells).
    from scripts.assessment_eval import trap_corpus as tc
    from scripts.assessment_eval.trap_judge import LLM, TrapJudgment

    monkeypatch.setattr(tc, "CORPUS_ROOT", tmp_path)
    _write_frozen(tmp_path, "AAAA-1111", {"0": {"volunteered": True}, "4": {"volunteered": True}})
    _write_frozen(tmp_path, "BBBB-2222", {"0": {"volunteered": True}, "4": {"volunteered": True}})

    judged: list[tuple[str, int]] = []

    async def fake_judge(trap, trap_index, **kwargs):
        # session code isn't passed to judge_trap, but the transcript is per-session;
        # track (rationale-free) which trap_index ran — codes filter cuts whole sessions.
        judged.append(("?", trap_index))
        return TrapJudgment(trap_index=trap_index, present=False, volunteered=True, layer=LLM, confidence=4)

    with patch("scripts.assessment_eval.trap_judge.judge_trap", fake_judge):
        report = await tc.calibrate(
            "task-01-training-loop", judge_model="j", codes=["AAAA-1111"], traps=[0]
        )

    # only AAAA-1111 (codes filter) × trap 0 (traps filter) → exactly one row.
    assert len(report.rows) == 1
    assert report.rows[0].code == "AAAA-1111" and report.rows[0].trap_index == 0
    assert judged == [("?", 0)]


@_session_loop
async def test_calibrate_unknown_code_raises(tmp_path, monkeypatch):
    from scripts.assessment_eval import trap_corpus as tc

    monkeypatch.setattr(tc, "CORPUS_ROOT", tmp_path)
    _write_frozen(tmp_path, "AAAA-1111", {"0": {"volunteered": True}})
    with pytest.raises(SystemExit):
        await tc.calibrate("task-01-training-loop", codes=["NOPE-9999"])


@_session_loop
async def test_calibrate_no_corpus_raises(tmp_path, monkeypatch):
    # MUST be an async test on the session loop — calling asyncio.run() from a sync
    # test closes the process event loop and breaks every session-scoped DB test in
    # the files that run after this one (backend/CLAUDE.md "Test suite traps" #1).
    from scripts.assessment_eval import trap_corpus as tc

    monkeypatch.setattr(tc, "CORPUS_ROOT", tmp_path)
    with pytest.raises(SystemExit):
        await tc.calibrate("task-01-training-loop")


def test_synthetic_corpus_preserves_independent_labels(tmp_path, monkeypatch):
    """A disclosed epoch flaw must not erase a held optimizer flaw."""
    import json
    from scripts.assessment_eval import trap_corpus as tc

    monkeypatch.setattr(tc, "CORPUS_ROOT", tmp_path)
    folder = tmp_path / "synthetic-task"
    folder.mkdir()
    (folder / "SYNTH-0001.json").write_text(json.dumps({
        "code": "SYNTH-0001", "task_id": "synthetic-task",
        "transcript": "[1] assistant: The epoch counter should also be restored.",
        "verdict": "partially_blown", "labels": {
            "0": {"volunteered": False, "present": True},
            "5": {"volunteered": True, "present": None},
        },
    }))
    session, = tc.load_trap_corpus("synthetic-task")
    assert session.labels[0].volunteered is False
    assert session.labels[5].volunteered is True
    assert session.labels[5].present is None
    assert "[1]" in session.transcript


# ---------------------------------------------------------------------------
# spend tracking (SpendTracker) — pure, litellm callback not invoked for real
# ---------------------------------------------------------------------------


class _FakeUsage:
    """Stands in for litellm's pydantic usage object (has model_dump)."""

    def __init__(self, **kw):
        self._kw = kw

    def model_dump(self):
        return dict(self._kw)


def _spend_resp(model, pt, ct, *, cost=None, hidden=None):
    kw = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
    if cost is not None:
        kw["cost"] = cost
    return SimpleNamespace(model=model, usage=_FakeUsage(**kw), _hidden_params=hidden or {})


@_session_loop
async def test_spend_tracker_accumulates_per_model():
    from scripts.assessment_eval.spend import SpendTracker

    t = SpendTracker()
    await t.async_log_success_event({"model": "m1"}, _spend_resp("m1", 10, 5, cost=0.001), 0, 0)
    await t.async_log_success_event({"model": "m1"}, _spend_resp("m1", 20, 5, cost=0.002), 0, 0)
    await t.async_log_success_event({"model": "m2"}, _spend_resp("m2", 4, 4, cost=0.0005), 0, 0)

    assert t.total_calls == 3
    assert abs(t.total_cost - 0.0035) < 1e-9
    assert t.by_model["m1"].total_tokens == 40
    md = t.render_markdown()
    assert "`m1`" in md and "`m2`" in md
    assert "$0.0035" in md  # grand total


@_session_loop
async def test_spend_tracker_cost_from_hidden_params_fallback():
    # No usage.cost, but litellm surfaced response_cost on _hidden_params.
    from scripts.assessment_eval.spend import SpendTracker

    t = SpendTracker()
    await t.async_log_success_event(
        {"model": "m"}, _spend_resp("m", 3, 2, hidden={"response_cost": 0.004}), 0, 0
    )
    assert t.unknown_cost_calls == 0
    assert abs(t.total_cost - 0.004) < 1e-9


@_session_loop
async def test_spend_tracker_unknown_cost_excluded_not_zeroed():
    # Provider reported no cost anywhere → counted as unknown, kept out of the $ total,
    # tokens still tallied.
    from scripts.assessment_eval.spend import SpendTracker

    t = SpendTracker()
    await t.async_log_success_event({"model": "m"}, _spend_resp("m", 3, 2), 0, 0)
    assert t.total_calls == 1
    assert t.total_cost == 0.0
    assert t.unknown_cost_calls == 1
    assert t.by_model["m"].total_tokens == 5
    assert "reported no cost" in t.render_markdown()
