"""Controller core tests — pure logic + review call (litellm mocked).

No DB, no real API. Async tests carry the session-loop mark per backend/CLAUDE.md
trap #1 (mixed sync+async file → per-test decorator, not a module mark)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from vibony_backend.agent_host import controller as ctrl
from vibony_backend.assessment.inputs import Pitfall

_session_loop = pytest.mark.asyncio(loop_scope="session")


def _pitfall(desc: str, *, flaw: str | None = None, correct: str | None = None) -> Pitfall:
    return Pitfall(description=desc, flawed_behavior=flaw, correct_behavior=correct)


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


# --------------------------------------------------------------------------- #
# policeable_contracts                                                         #
# --------------------------------------------------------------------------- #


def test_policeable_filters_legacy_and_preserves_index():
    pitfalls = [
        _pitfall("legacy, no contract"),                 # idx 0 — skipped
        _pitfall("optimizer", flaw="saves no optimizer"),  # idx 1 — kept
        _pitfall("also legacy"),                          # idx 2 — skipped
        _pitfall("epoch", flaw="saves no epoch"),          # idx 3 — kept
    ]
    contracts = ctrl.policeable_contracts(pitfalls)
    assert [c.index for c in contracts] == [1, 3]
    assert contracts[0].pitfall.description == "optimizer"


def test_policeable_empty_when_no_contracts():
    assert ctrl.policeable_contracts([_pitfall("a"), _pitfall("b")]) == []


# --------------------------------------------------------------------------- #
# extract_proposed_edits                                                       #
# --------------------------------------------------------------------------- #


def _tc(name: str, args: dict) -> dict:
    return {"function": {"name": name, "arguments": json.dumps(args)}}


def test_extract_write_and_edit():
    calls = [
        _tc("Write", {"path": "train.py", "content": "torch.save(...)"}),
        _tc("Edit", {"path": "m.py", "old_string": "a", "new_string": "b"}),
    ]
    edits = ctrl.extract_proposed_edits(calls)
    assert [e.kind for e in edits] == ["write", "edit"]
    assert edits[0].path == "train.py" and "torch.save" in edits[0].body
    assert "a" in edits[1].body and "b" in edits[1].body


def test_extract_ignores_non_mutating_tools():
    calls = [
        _tc("Read", {"path": "x"}),
        _tc("Grep", {"pattern": "y"}),
        _tc("Bash", {"command": "ls"}),
    ]
    assert ctrl.extract_proposed_edits(calls) == []


def test_extract_skips_malformed():
    calls = [
        {"function": {"name": "Write", "arguments": "not json"}},
        {"function": {"name": "Write", "arguments": json.dumps({"path": ""})}},  # blank path
        {"function": {"name": "Write", "arguments": json.dumps({"content": "x"})}},  # no path
        _tc("Write", {"path": "ok.py", "content": "good"}),
    ]
    edits = ctrl.extract_proposed_edits(calls)
    assert len(edits) == 1 and edits[0].path == "ok.py"


def test_extract_none_tool_calls():
    assert ctrl.extract_proposed_edits(None) == []


# --------------------------------------------------------------------------- #
# round_is_reviewable                                                          #
# --------------------------------------------------------------------------- #


def test_round_reviewable():
    edit = ctrl.ProposedEdit(path="x", kind="write", body="b")
    assert ctrl.round_is_reviewable([edit], None) is True
    assert ctrl.round_is_reviewable([], "let me explain") is True
    assert ctrl.round_is_reviewable([], "   ") is False
    assert ctrl.round_is_reviewable([], None) is False


# --------------------------------------------------------------------------- #
# TrapControlState                                                             #
# --------------------------------------------------------------------------- #


def test_state_apply_unlock_filters_range():
    st = ctrl.TrapControlState(all_indices=frozenset({0, 1, 3}))
    st.apply_unlock([1, 2, 99])  # 2 and 99 are not policeable
    assert st.unlocked == {1}


def test_state_fully_unlocked():
    st = ctrl.TrapControlState(all_indices=frozenset({0, 1}))
    assert st.fully_unlocked() is False
    st.apply_unlock([0, 1])
    assert st.fully_unlocked() is True


def test_state_fully_unlocked_false_when_no_traps():
    st = ctrl.TrapControlState(all_indices=frozenset())
    assert st.fully_unlocked() is False


# --------------------------------------------------------------------------- #
# verdict schema + correction                                                  #
# --------------------------------------------------------------------------- #


def test_verdict_defaults_empty():
    v = ctrl.ControllerVerdict.model_validate({})
    assert v.newly_unlocked == [] and v.violations == []


def test_build_correction_lists_violations():
    vs = [
        ctrl.TrapViolation(trap=0, channel="code", evidence="added optimizer_state_dict", remove="that key"),
        ctrl.TrapViolation(trap=4, channel="chat", evidence="mentioned RNG", remove="that sentence"),
    ]
    text = ctrl.build_correction(vs)
    assert "added optimizer_state_dict" in text
    assert "that key" in text
    assert "STOP" in text


def test_build_correction_adds_contract_anchor():
    # With the contracts, each violation carries the positive anchor (flawed_behavior +
    # defense) so the assistant knows what to KEEP, not only what to remove.
    contracts = ctrl.policeable_contracts(
        [
            Pitfall(
                description="oversell",
                flawed_behavior="non-atomic read-modify-write, no lock",
                correct_behavior="name the race",
                defense="the load test may be flaky",
            )
        ]
    )
    vs = [ctrl.TrapViolation(trap=0, channel="code", evidence="added a lock", remove="the lock")]
    text = ctrl.build_correction(vs, contracts)
    assert "keep this behavior intact instead: non-atomic read-modify-write" in text
    assert "hold like this: the load test may be flaky" in text
    # Absent contracts → the negative-only form (back-compat), no anchor lines.
    assert "keep this behavior intact" not in ctrl.build_correction(vs)


def test_build_review_prompt_fences_and_lists_unlocked():
    contracts = ctrl.policeable_contracts([_pitfall("opt", flaw="no optimizer", correct="name optimizer")])
    prompt = ctrl.build_review_prompt(
        contracts, {0}, "is the optimizer saved?", "sure, done", []
    )
    assert "Trap 0" in prompt
    assert "0" in prompt  # unlocked listing
    assert "is the optimizer saved?" in prompt  # fenced candidate text present


# --------------------------------------------------------------------------- #
# review_turn (litellm mocked, validate_model patched)                         #
# --------------------------------------------------------------------------- #


_CONTRACTS = None


def _contracts():
    return ctrl.policeable_contracts(
        [
            _pitfall("optimizer", flaw="no optimizer", correct="names optimizer"),
            _pitfall("epoch", flaw="no epoch", correct="names epoch"),
        ]
    )


@_session_loop
async def test_review_happy_path_parses_verdict():
    payload = {
        "newly_unlocked": [0],
        "violations": [
            {"trap": 1, "channel": "code", "evidence": "added epoch", "remove": "the epoch key"}
        ],
    }
    mock = AsyncMock(return_value=_resp(json.dumps(payload)))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "do it", "added epoch", [], model="x/y")
    assert v.newly_unlocked == [0]
    assert len(v.violations) == 1 and v.violations[0].trap == 1


@_session_loop
async def test_review_drops_out_of_range_indices():
    payload = {
        "newly_unlocked": [0, 5],  # 5 is not policeable
        "violations": [
            {"trap": 9, "channel": "chat", "evidence": "x", "remove": "y"}  # 9 invalid → dropped
        ],
    }
    mock = AsyncMock(return_value=_resp(json.dumps(payload)))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    assert v.newly_unlocked == [0]
    assert v.violations == []


@_session_loop
async def test_review_fails_open_on_bad_json():
    mock = AsyncMock(return_value=_resp("not json at all"))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    assert v.newly_unlocked == [] and v.violations == []
    assert mock.call_count == 2  # initial + one retry-with-nudge before failing open


@_session_loop
async def test_review_retries_bad_json_then_parses():
    # A capable controller intermittently emits an unparseable verdict (truncated
    # string / missing `remove`); one retry-with-nudge recovers it instead of an immediate
    # fail-open (an ungated round).
    good = {"newly_unlocked": [], "violations": [
        {"trap": 0, "channel": "code", "evidence": "e", "remove": "r"}]}
    mock = AsyncMock(side_effect=[_resp('{"violations": [{"trap": 0, "evidence": "trunc'),
                                  _resp(json.dumps(good))])
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", "added a lock", [], model="x/y")
    assert mock.call_count == 2
    assert len(v.violations) == 1 and v.violations[0].trap == 0


@_session_loop
async def test_review_forwards_strict_json_schema():
    # The controller call is decoder-constrained via a strict json_schema (not the
    # looser {"type":"json_object"} that let the model emit schema-invalid JSON).
    good = AsyncMock(return_value=_resp(json.dumps({"newly_unlocked": [], "violations": []})))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", good
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    rf = good.call_args.kwargs["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True


@_session_loop
async def test_review_fail_open_increments_state():
    # A fail-open is an ungated round — a technical error that must be VISIBLE so a
    # benchmark can exclude the run. review_turn counts it on state.fail_opens.
    state = ctrl.TrapControlState(all_indices=frozenset({0, 1}))
    assert state.fail_opens == 0
    bad = AsyncMock(return_value=_resp("not json at all"))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", bad
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y", state=state)
    assert state.fail_opens == 1  # both attempts bad → one recorded fail-open
    # A clean verdict does not increment.
    good = AsyncMock(return_value=_resp(json.dumps({"newly_unlocked": [], "violations": []})))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", good
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y", state=state)
    assert state.fail_opens == 1  # unchanged


def test_controller_verdict_parses_cot_fields():
    # The in-band CoT fields parse and are retained; a lenient reply omitting them
    # still validates (they default to "").
    v = ctrl.ControllerVerdict.model_validate(
        {"candidate_named": "named the race", "round_summary": "adds a lock",
         "newly_unlocked": [0], "violations": []}
    )
    assert v.candidate_named == "named the race" and v.newly_unlocked == [0]
    v2 = ctrl.ControllerVerdict.model_validate({"newly_unlocked": [], "violations": []})
    assert v2.candidate_named == "" and v2.round_summary == ""


# --------------------------------------------------------------------------- #
# verdict repair (before-validator: salvage kimi's best-effort JSON)            #
# --------------------------------------------------------------------------- #


def test_verdict_repair_drops_blank_and_missing_remove():
    # A blank/missing `remove` is kimi's false-positive "examined and cleared" marker —
    # dropped, not defaulted (a spurious violation would trigger a needless retry). This is
    # the tolerant-`remove` parse, subsumed by the repair validator.
    v = ctrl.ControllerVerdict.model_validate(
        {
            "violations": [
                {"trap": 0, "channel": "code", "evidence": "real", "remove": "the lock"},
                {"trap": 1, "channel": "chat", "evidence": "cleared", "remove": ""},
                {"trap": 2, "channel": "chat", "evidence": "cleared"},  # remove absent
            ]
        }
    )
    assert [x.trap for x in v.violations] == [0]


def test_verdict_repair_salvages_partial_list():
    # One malformed entry no longer sinks the whole verdict: a bare string and a blank-remove
    # false positive are dropped; the one good violation survives. (A trap-less entry WITH a
    # real `remove` is a different case — it raises, see the un-coercible-trap test below.)
    v = ctrl.ControllerVerdict.model_validate(
        {
            "newly_unlocked": [],
            "violations": [
                "No violation.",  # a bare string, not an object → dropped
                {"trap": 1, "channel": "chat", "evidence": "cleared", "remove": ""},  # blank → dropped
                {"trap": 3, "channel": "code", "evidence": "e", "remove": "r"},  # good
            ],
        }
    )
    assert len(v.violations) == 1 and v.violations[0].trap == 3


def test_verdict_repair_coerces_string_ids():
    # kimi occasionally types ids as strings; coerce them instead of failing the verdict.
    v = ctrl.ControllerVerdict.model_validate(
        {
            "newly_unlocked": ["0", "2", "nope"],  # last is un-coercible → dropped
            "violations": [{"trap": "1", "channel": "code", "evidence": "e", "remove": "r"}],
        }
    )
    assert v.newly_unlocked == [0, 2]
    assert len(v.violations) == 1 and v.violations[0].trap == 1


def test_verdict_repair_non_dict_still_raises():
    # Scope is narrow: a non-dict (a totally-broken shape) is NOT salvaged — normal
    # validation raises so the caller retries / fails open (the run-exclusion guarantee).
    with pytest.raises(ValidationError):
        ctrl.ControllerVerdict.model_validate(["not", "a", "verdict"])


def test_verdict_repair_uncoercible_trap_with_real_remove_still_raises():
    # Data-integrity: a real disclosure (non-blank `remove`) whose `trap` isn't an index
    # (e.g. kimi names it by a slug) must NOT be silently dropped to a clean-empty verdict —
    # that would pass an ungated round and count it as data. It falls through to raise →
    # retry → fail-open → the run is excluded. (A blank-`remove` non-int entry is the
    # "cleared" false positive and IS dropped — covered above.)
    for bad_trap in ({"trap": "oversell"}, {}):  # a slug id, and a missing trap key
        with pytest.raises(ValidationError):
            ctrl.ControllerVerdict.model_validate(
                {"violations": [{**bad_trap, "channel": "code",
                                 "evidence": "added a lock", "remove": "the lock"}]}
            )


def test_verdict_repair_nonstring_remove_still_raises():
    # Symmetric to the un-coercible-trap case: a valid int trap but a malformed non-string
    # `remove` (the strict schema says string; the model deviated) is a real signal we can't
    # render — it must raise, not drop to a clean verdict (else the round is silently ungated).
    with pytest.raises(ValidationError):
        ctrl.ControllerVerdict.model_validate(
            {"violations": [{"trap": 2, "channel": "code", "evidence": "e",
                             "remove": ["undo the lock", "restore the check"]}]}
        )


@_session_loop
async def test_review_salvages_partial_verdict_without_retry():
    # End-to-end: a partly-malformed verdict is repaired in-place, so review_turn returns
    # the usable violation on the FIRST call — no retry, no fail-open.
    state = ctrl.TrapControlState(all_indices=frozenset({0, 1}))
    payload = {
        "newly_unlocked": [],
        "violations": [
            {"trap": 0, "channel": "chat", "evidence": "cleared", "remove": ""},  # dropped
            {"trap": 1, "channel": "code", "evidence": "added epoch", "remove": "the epoch key"},
        ],
    }
    mock = AsyncMock(return_value=_resp(json.dumps(payload)))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(
            _contracts(), set(), "msg", "added epoch", [], model="x/y", state=state
        )
    assert mock.call_count == 1  # salvaged in-place, no retry
    assert state.fail_opens == 0  # not an excluded run
    assert state.review_retries == 0  # no retry needed
    assert state.verdict_repairs == 1  # the in-place salvage is counted
    assert [x.trap for x in v.violations] == [1]


# --------------------------------------------------------------------------- #
# reliability counters + APIError retry + env lever                             #
# --------------------------------------------------------------------------- #


@_session_loop
async def test_review_retries_counter_increments():
    # Observability: a review that needed a retry-with-nudge counts on review_retries;
    # a clean verdict on the retry means nothing was salvaged (verdict_repairs stays 0).
    state = ctrl.TrapControlState(all_indices=frozenset({0, 1}))
    good = {"newly_unlocked": [], "violations": []}
    mock = AsyncMock(side_effect=[_resp("not json"), _resp(json.dumps(good))])
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y", state=state)
    assert state.review_retries == 1
    assert state.verdict_repairs == 0
    assert state.fail_opens == 0  # recovered → not excluded


@_session_loop
async def test_review_retries_empty_response_apierror():
    # An empty-response APIError ("Unable to get json response") is a malformed reply,
    # not a hang → retried like a bad-JSON parse; a good verdict on the retry recovers it.
    import litellm

    err = litellm.APIError(500, "OpenrouterException - Unable to get json response", "openrouter", "x/y")
    good = {"newly_unlocked": [], "violations": []}
    state = ctrl.TrapControlState(all_indices=frozenset({0}))
    mock = AsyncMock(side_effect=[err, _resp(json.dumps(good))])
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y", state=state)
    assert mock.call_count == 2  # retried, not an immediate fail-open
    assert state.review_retries == 1
    assert state.fail_opens == 0
    assert v.violations == []


@_session_loop
async def test_review_other_apierror_fails_open_immediately():
    # A non-empty-response APIError (a real provider error) is NOT retried — it fails open at
    # once so a hung/erroring provider can't stall the candidate's turn.
    import litellm

    err = litellm.APIError(500, "OpenrouterException - rate limit exceeded", "openrouter", "x/y")
    state = ctrl.TrapControlState(all_indices=frozenset({0}))
    mock = AsyncMock(
        side_effect=[err, _resp(json.dumps({"newly_unlocked": [], "violations": []}))]
    )
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y", state=state)
    assert mock.call_count == 1  # not retried
    assert state.fail_opens == 1
    assert v.violations == []


def test_max_controller_retries_env(monkeypatch):
    # The residual-3 lever: VIBONY_CONTROLLER_MAX_RETRIES overrides the default; a
    # non-int / negative value falls back to it.
    monkeypatch.delenv("VIBONY_CONTROLLER_MAX_RETRIES", raising=False)
    assert ctrl.max_controller_retries() == ctrl.MAX_CONTROLLER_RETRIES
    monkeypatch.setenv("VIBONY_CONTROLLER_MAX_RETRIES", "5")
    assert ctrl.max_controller_retries() == 5
    monkeypatch.setenv("VIBONY_CONTROLLER_MAX_RETRIES", "0")
    assert ctrl.max_controller_retries() == 0
    monkeypatch.setenv("VIBONY_CONTROLLER_MAX_RETRIES", "-3")
    assert ctrl.max_controller_retries() == ctrl.MAX_CONTROLLER_RETRIES
    monkeypatch.setenv("VIBONY_CONTROLLER_MAX_RETRIES", "notanint")
    assert ctrl.max_controller_retries() == ctrl.MAX_CONTROLLER_RETRIES


@_session_loop
async def test_review_parses_verdict_wrapped_in_reasoning():
    """A reasoning-family controller (qwen3 / R1) prefixes a <think> block and may
    wrap the object in prose — the parser must still lift the JSON, not fail open."""
    payload = {
        "newly_unlocked": [0],
        "violations": [{"trap": 1, "channel": "code", "evidence": "e", "remove": "r"}],
    }
    wrapped = (
        "<think>The candidate named the optimizer, so trap 0 unlocks. The edit adds an "
        "epoch key which discloses trap 1.</think>\n\nHere is the verdict:\n"
        f"```json\n{json.dumps(payload)}\n```"
    )
    mock = AsyncMock(return_value=_resp(wrapped))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "do it", "added epoch", [], model="x/y")
    assert v.newly_unlocked == [0]
    assert len(v.violations) == 1 and v.violations[0].trap == 1


def test_extract_verdict_json_keys_on_controller_fields():
    # The controller wrapper keys on ITS verdict fields (generic parsing lives in
    # test_reasoning.py). A stray prose object before the real verdict is skipped for
    # the one carrying a controller key (else it parses as an empty verdict → silent pass).
    picked = ctrl._extract_verdict_json(
        'Note: {"epoch": 5} then {"newly_unlocked": [1], "violations": []}'
    )
    assert picked == '{"newly_unlocked": [1], "violations": []}'


def test_controller_extra_body_defaults_reasoning_off(monkeypatch):
    # Unset → the reasoning-off default.
    monkeypatch.delenv("VIBONY_CONTROLLER_EXTRA_BODY", raising=False)
    assert ctrl._controller_extra_body() == {"reasoning": {"enabled": False}}
    # Explicit empty → send nothing (thinking back on).
    monkeypatch.setenv("VIBONY_CONTROLLER_EXTRA_BODY", "")
    assert ctrl._controller_extra_body() is None
    # A custom object is forwarded verbatim.
    monkeypatch.setenv("VIBONY_CONTROLLER_EXTRA_BODY", '{"provider": {"order": ["x"]}}')
    assert ctrl._controller_extra_body() == {"provider": {"order": ["x"]}}
    # Malformed / non-object → back to the reasoning-off default.
    monkeypatch.setenv("VIBONY_CONTROLLER_EXTRA_BODY", "not json")
    assert ctrl._controller_extra_body() == {"reasoning": {"enabled": False}}
    monkeypatch.setenv("VIBONY_CONTROLLER_EXTRA_BODY", "[1, 2]")
    assert ctrl._controller_extra_body() == {"reasoning": {"enabled": False}}


@_session_loop
async def test_review_forwards_reasoning_off_by_default(monkeypatch):
    payload = {"newly_unlocked": [], "violations": []}

    # Default (env unset): the reasoning-off body reaches the call.
    monkeypatch.delenv("VIBONY_CONTROLLER_EXTRA_BODY", raising=False)
    on = AsyncMock(return_value=_resp(json.dumps(payload)))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", on
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    assert on.call_args.kwargs["extra_body"] == {"reasoning": {"enabled": False}}

    # Explicit empty: no extra_body forwarded (thinking on).
    monkeypatch.setenv("VIBONY_CONTROLLER_EXTRA_BODY", "")
    off = AsyncMock(return_value=_resp(json.dumps(payload)))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", off
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    assert "extra_body" not in off.call_args.kwargs


def test_controller_fewshot_toggle(monkeypatch):
    # Off by default → the system prompt is byte-identical to the baseline.
    monkeypatch.delenv("VIBONY_CONTROLLER_FEWSHOT", raising=False)
    assert ctrl._controller_system_prompt() == ctrl._SYSTEM_PROMPT
    # A truthy value → the worked example is appended (baseline still a prefix).
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("VIBONY_CONTROLLER_FEWSHOT", val)
        prompt = ctrl._controller_system_prompt()
        assert prompt.startswith(ctrl._SYSTEM_PROMPT)
        assert "WORKED EXAMPLE" in prompt
    # A falsy/garbage value → still baseline (no accidental enable).
    monkeypatch.setenv("VIBONY_CONTROLLER_FEWSHOT", "0")
    assert ctrl._controller_system_prompt() == ctrl._SYSTEM_PROMPT


@_session_loop
async def test_review_forwards_fewshot_when_enabled(monkeypatch):
    payload = {"newly_unlocked": [], "violations": []}

    # Toggle ON: the system message the controller sends carries the few-shot example.
    monkeypatch.setenv("VIBONY_CONTROLLER_FEWSHOT", "1")
    on = AsyncMock(return_value=_resp(json.dumps(payload)))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", on
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    system_msg = on.call_args.kwargs["messages"][0]
    assert system_msg["role"] == "system" and "WORKED EXAMPLE" in system_msg["content"]

    # Toggle OFF: the baseline system prompt, no example.
    monkeypatch.delenv("VIBONY_CONTROLLER_FEWSHOT", raising=False)
    off = AsyncMock(return_value=_resp(json.dumps(payload)))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", off
    ):
        await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    assert "WORKED EXAMPLE" not in off.call_args.kwargs["messages"][0]["content"]


@_session_loop
async def test_review_fails_open_on_transport_error():
    mock = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    assert v.violations == []


@_session_loop
async def test_review_fails_open_on_disallowed_model():
    from vibony_backend.proxy import ModelNotAllowed

    def _raise(_m):
        raise ModelNotAllowed("nope")

    mock = AsyncMock(return_value=_resp("{}"))
    with patch("vibony_backend.proxy.validate_model", _raise), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="bad/model")
    assert v.violations == [] and v.newly_unlocked == []
    mock.assert_not_called()  # never reached the LLM


@_session_loop
async def test_review_strips_json_fence():
    payload = {"newly_unlocked": [], "violations": []}
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    mock = AsyncMock(return_value=_resp(fenced))
    with patch("vibony_backend.proxy.validate_model", lambda m: m), patch(
        "vibony_backend.agent_host.controller.litellm.acompletion", mock
    ):
        v = await ctrl.review_turn(_contracts(), set(), "msg", None, [], model="x/y")
    assert v.violations == []
