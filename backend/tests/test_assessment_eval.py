"""Tests for the calibration harness logic, with ``litellm`` mocked.

No real LLM call, no DB. Covers the scenario matrix, the LLM author parse + the
no-LLM near-empty path, corpus freeze/load round-trip, ``build_inputs`` via the real
renderers, the programmatic + LLM judge, the scorecard aggregate, and ``run_eval``
end-to-end (generate + judge mocked).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from vibony_backend.assessment.inputs import Pitfall, TaskSpec

from scripts.assessment_eval import corpus, run_eval
from scripts.assessment_eval.author import author_scenario, build_author_prompt
from scripts.assessment_eval.judge import (
    JudgeVerdict,
    PitfallJudgment,
    ProgrammaticReport,
    Verdict,
    llm_judge,
    programmatic_checks,
)
from scripts.assessment_eval.personas import ScenarioLabel, ScenarioSpec, derive_scenarios
from scripts.assessment_eval.scorecard import aggregate, render_markdown

_session_loop = pytest.mark.asyncio(loop_scope="session")


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _spec(*, blocking: int = 1, degrading: int = 1) -> TaskSpec:
    pitfalls = [Pitfall(description=f"blocking trap {i}", severity="blocking") for i in range(blocking)]
    pitfalls += [Pitfall(description=f"degrading trap {i}", severity="degrading") for i in range(degrading)]
    return TaskSpec(task_id="t", context="c", expected_ai_pitfalls=pitfalls)


_GOOD_SUMMARY = (
    "## What went well\nx\n"
    "## What's missing for an ideal solution\ny\n"
    "## Observed reasoning patterns\nz"
)


# ---------------------------------------------------------------------------
# personas / derive_scenarios
# ---------------------------------------------------------------------------


def test_derive_scenarios_matrix_one_blocking():
    scns = derive_scenarios(_spec(blocking=1, degrading=1))
    keys = [s.key for s in scns]
    assert keys[0] == "near_empty"
    assert keys[1] == "clean"
    # one single per pitfall (2), no multi (only 1 blocking), plus fought_ai
    assert sum(1 for s in scns if s.kind == "single") == 2
    assert "multi__blocking" not in keys
    assert "fought_ai" in keys


def test_derive_scenarios_adds_multi_when_two_blocking():
    scns = derive_scenarios(_spec(blocking=2, degrading=0))
    multi = [s for s in scns if s.kind == "multi"]
    assert len(multi) == 1
    assert multi[0].label.triggered_pitfalls == [0, 1]


def test_derive_scenarios_single_label_indices():
    scns = derive_scenarios(_spec(blocking=1, degrading=1))
    singles = [s for s in scns if s.kind == "single"]
    assert [s.label.triggered_pitfalls for s in singles] == [[0], [1]]


def test_derive_scenarios_no_pitfalls_still_has_probes():
    scns = derive_scenarios(TaskSpec(task_id="t"))
    assert [s.key for s in scns] == ["near_empty", "clean"]


# ---------------------------------------------------------------------------
# author (LLM mocked; near-empty needs no LLM)
# ---------------------------------------------------------------------------


@_session_loop
async def test_author_near_empty_no_llm():
    scn = ScenarioSpec("near_empty", "near_empty", "idle", ScenarioLabel(near_empty=True))
    # No patch — must not call litellm at all.
    rec = await author_scenario(_spec(), {}, scn)
    record = corpus.make_record("t", scn, [tuple(e) for e in rec["events"]], rec["final_files"])
    inputs = corpus.build_inputs(record)
    assert inputs.is_near_empty is True


@_session_loop
async def test_author_parses_valid_reply():
    scn = ScenarioSpec("single__x", "single", "fall into trap", ScenarioLabel(triggered_pitfalls=[0]))
    reply = json.dumps(
        {
            "events": [
                [1, "session_start", {"task_id": "t"}],
                [2, "user_message", {"text": "add checkpoint"}],
                [3, "file_changed", {"op": "modified", "file_path": "train.py", "source": "candidate"}],
                [4, "session_end", {"reason": "submitted"}],
            ],
            "final_files": {"train.py": "print('x')"},
        }
    )
    with patch("scripts.assessment_eval.author.litellm.acompletion", AsyncMock(return_value=_resp(reply))):
        rec = await author_scenario(_spec(), {"train.py": "stub"}, scn)
    assert len(rec["events"]) == 4
    assert rec["final_files"]["train.py"] == "print('x')"


@_session_loop
async def test_author_rejects_unrenderable_reply():
    scn = ScenarioSpec("single__x", "single", "p", ScenarioLabel(triggered_pitfalls=[0]))
    reply = json.dumps({"events": [], "final_files": {}})
    with patch("scripts.assessment_eval.author.litellm.acompletion", AsyncMock(return_value=_resp(reply))):
        with pytest.raises(ValueError):
            await author_scenario(_spec(), {}, scn)


def test_build_author_prompt_includes_reference_only_for_clean():
    spec = _spec()
    clean = ScenarioSpec("clean", "clean", "be clean", ScenarioLabel())
    single = ScenarioSpec("s", "single", "fall", ScenarioLabel(triggered_pitfalls=[0]))
    assert "REFERENCE SOLUTION" in build_author_prompt(spec, {}, clean, reference="ref code")
    assert "REFERENCE SOLUTION" not in build_author_prompt(spec, {}, single, reference="ref code")


# ---------------------------------------------------------------------------
# corpus freeze/load + build_inputs (real renderers)
# ---------------------------------------------------------------------------


def test_corpus_round_trip(tmp_path):
    scn = ScenarioSpec("clean", "clean", "p", ScenarioLabel(triggered_pitfalls=[1], notes="n"))
    events = [(1, "session_start", {"task_id": "t"}), (2, "user_message", {"text": "hi"})]
    rec = corpus.make_record("task-x", scn, events, {"a.py": "code"})
    corpus.save_record(rec, base=tmp_path)
    loaded = corpus.load_corpus("task-x", base=tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["label"]["triggered_pitfalls"] == [1]
    assert loaded[0]["final_files"]["a.py"] == "code"
    assert corpus.corpus_task_ids(base=tmp_path) == ["task-x"]


def test_build_inputs_uses_real_renderers_and_spec():
    # task-01 has a real spec.yaml -> spec_text must be injected.
    scn = ScenarioSpec("s", "single", "p", ScenarioLabel(triggered_pitfalls=[0]))
    events = [
        (1, "session_start", {"task_id": "task-01-training-loop"}),
        (2, "user_message", {"text": "implement the training loop"}),
        (3, "file_changed", {"op": "modified", "file_path": "train.py", "source": "candidate"}),
        (4, "session_end", {"reason": "submitted"}),
    ]
    rec = corpus.make_record("task-01-training-loop", scn, events, {"train.py": "def train(): ..."})
    inputs = corpus.build_inputs(rec)
    assert inputs.is_near_empty is False
    assert "implement the training loop" in inputs.transcript
    assert "def train()" in inputs.final_code
    assert "expected_ai_pitfalls" in inputs.spec_text.lower() or inputs.spec is not None
    assert inputs.spec is not None and inputs.spec.expected_ai_pitfalls


# ---------------------------------------------------------------------------
# judge — programmatic
# ---------------------------------------------------------------------------


def _record(label: dict, events: list) -> dict:
    return {"task_id": "t", "key": "k", "kind": "single", "persona": "p", "label": label, "events": events, "final_files": {}}


def test_programmatic_flags_bad_seq():
    rec = _record({}, [[1, "user_message", {"text": "x"}], [2, "session_end", {"reason": "submitted"}]])
    findings = [{"category": "gap", "title": "t", "detail": "d", "severity": "info", "event_seq": 99}]
    rep = programmatic_checks(rec, _GOOD_SUMMARY, findings)
    assert rep.seqs_cited == 1 and rep.seqs_valid == 0
    assert any("non-existent" in i for i in rep.issues)


def test_programmatic_sections_ok_and_missing():
    rec = _record({}, [[1, "user_message", {"text": "x"}]])
    assert programmatic_checks(rec, _GOOD_SUMMARY, []).section_ok is True
    assert programmatic_checks(rec, "## What went well\nonly one", []).section_ok is False


def test_programmatic_near_empty_honesty():
    rec = _record({"near_empty": True}, [[1, "session_start", {"task_id": "t"}]])
    honest = programmatic_checks(rec, _GOOD_SUMMARY + "\ninsufficient activity to assess", [])
    assert honest.near_empty_honest is True
    dishonest = programmatic_checks(
        rec,
        _GOOD_SUMMARY,
        [{"category": "gap", "title": "t", "detail": "d", "severity": "info", "event_seq": None}],
    )
    assert dishonest.near_empty_honest is False


# ---------------------------------------------------------------------------
# judge — LLM (mocked)
# ---------------------------------------------------------------------------


@_session_loop
async def test_llm_judge_parses_verdict():
    rec = _record({"triggered_pitfalls": [0]}, [[1, "user_message", {"text": "x"}]])
    verdict_json = json.dumps(
        {
            "pitfalls": [{"index": 0, "triggered": True, "caught": True}],
            "hallucinated_pitfalls": [],
            "faithfulness": 4,
            "actionability": 5,
            "rationale": "ok",
        }
    )
    with patch("scripts.assessment_eval.judge.litellm.acompletion", AsyncMock(return_value=_resp(verdict_json))):
        v = await llm_judge(rec, _GOOD_SUMMARY, [], spec=_spec())
    assert v.faithfulness == 4 and v.actionability == 5
    assert v.pitfalls[0].caught is True


@_session_loop
async def test_llm_judge_bad_json_returns_neutral():
    rec = _record({}, [[1, "user_message", {"text": "x"}]])
    with patch("scripts.assessment_eval.judge.litellm.acompletion", AsyncMock(return_value=_resp("not json"))):
        v = await llm_judge(rec, _GOOD_SUMMARY, [], spec=_spec())
    assert v.faithfulness == 1 and "parse error" in v.rationale


def test_verdict_recall_anchored_to_label_not_judge_echo():
    # Judge drifted: it returned only one pitfall row, but the label says two were
    # triggered. Recall must use the label denominator (2), not the judge's echo (1).
    jv = JudgeVerdict(
        pitfalls=[PitfallJudgment(index=0, triggered=True, caught=True)],
        faithfulness=4,
        actionability=3,
    )
    v = Verdict(
        task_id="t", key="k", programmatic=ProgrammaticReport(), judge=jv, triggered_pitfalls=[0, 1]
    )
    assert v.triggered_total == 2
    assert v.caught_triggered == 1


# ---------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------


def _verdict(*, triggered: list[int], caught: list[int], halluc: list[int], faith: int, near_empty_honest=None) -> Verdict:
    pitfalls = [PitfallJudgment(index=i, triggered=True, caught=(i in caught)) for i in triggered]
    prog = ProgrammaticReport(seqs_cited=1, seqs_valid=1, section_ok=True, near_empty_honest=near_empty_honest)
    jv = JudgeVerdict(pitfalls=pitfalls, hallucinated_pitfalls=halluc, faithfulness=faith, actionability=3)
    return Verdict(task_id="t", key="k", programmatic=prog, judge=jv, triggered_pitfalls=triggered)


def test_scorecard_aggregate_and_render():
    verdicts = [
        _verdict(triggered=[0, 1], caught=[0], halluc=[], faith=4),
        _verdict(triggered=[], caught=[], halluc=[2], faith=2, near_empty_honest=True),
    ]
    card = aggregate(verdicts)
    assert card.runs == 2
    assert card.trap_recall == pytest.approx(0.5)  # caught 1 of 2 triggered
    assert card.hallucinated_total == 1
    assert card.near_empty_honesty == pytest.approx(1.0)
    md = render_markdown(verdicts, card, model="gen", judge_model="judge")
    assert "trap recall" in md and "Per scenario" in md
    assert "Flagged" in md  # the faith=2 / halluc verdict is surfaced


# ---------------------------------------------------------------------------
# run_eval end-to-end (generate + judge mocked)
# ---------------------------------------------------------------------------


@_session_loop
async def test_run_eval_end_to_end():
    rec = corpus.make_record(
        "task-01-training-loop",
        ScenarioSpec("clean", "clean", "p", ScenarioLabel()),
        [(1, "session_start", {"task_id": "task-01-training-loop"}), (2, "user_message", {"text": "go"}), (3, "session_end", {"reason": "submitted"})],
        {"train.py": "code"},
    )
    gen = AsyncMock(return_value=(_GOOD_SUMMARY, [], "model-x"))
    jv = Verdict(
        task_id="task-01-training-loop",
        key="clean",
        programmatic=ProgrammaticReport(section_ok=True),
        judge=JudgeVerdict(faithfulness=5, actionability=4),
    )
    judge_mock = AsyncMock(return_value=jv)
    with patch("scripts.assessment_eval.run_eval.generate_assessment", gen), patch(
        "scripts.assessment_eval.run_eval.judge_scenario", judge_mock
    ):
        verdicts, card = await run_eval.run_eval([rec], reps=2)
    assert len(verdicts) == 2  # 1 scenario x 2 reps
    assert gen.await_count == 2
    assert card.mean_faithfulness == pytest.approx(5.0)
    assert card.runs == 2
