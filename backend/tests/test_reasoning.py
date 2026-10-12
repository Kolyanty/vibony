"""Shared reasoning-off seam + verdict-JSON extraction (vibony_backend.reasoning).

Pure string logic — no LLM, no DB. These primitives back three call sites (controller,
trap judge, candidate driver); the call-site-specific behavior is tested there.
"""

from __future__ import annotations

from vibony_backend import reasoning as r


def test_reasoning_off_extra_body_shape():
    assert r.REASONING_OFF_EXTRA_BODY == {"reasoning": {"enabled": False}}


def test_strip_fences():
    assert r.strip_fences('{"a": 1}') == '{"a": 1}'
    assert r.strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert r.strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert r.strip_fences("") == ""


def test_strip_think_removes_blocks():
    assert r.strip_think("<think>reasoning</think>answer").strip() == "answer"
    assert r.strip_think("a<think>x</think>b<think>y</think>c") == "abc"
    # No think block → unchanged.
    assert r.strip_think("plain") == "plain"


def test_strip_think_is_linear_on_unclosed_opens():
    # Many unclosed <think opens must not hang (no quadratic regex backtrack).
    assert r.strip_think("<think>" * 20000 + "tail").strip() == ""
    # A single unclosed open drops the rest to EOF (truncated reasoning reply).
    assert r.strip_think("keep <think>dangling") == "keep "


def test_first_json_object():
    assert r.first_json_object('{"a": 1}') == '{"a": 1}'
    assert r.first_json_object('noise {"a": {"b": 2}} tail') == '{"a": {"b": 2}}'
    # A brace inside a string is not a nesting level.
    assert r.first_json_object('{"s": "a}b"}') == '{"s": "a}b"}'
    # No object / truncated (token-starved) → None.
    assert r.first_json_object("<think>reasoning with no verdict") is None
    assert r.first_json_object('{"a": 1') is None


def test_json_objects_yields_in_order_and_stops_at_truncation():
    assert list(r.json_objects('{"a": 1} {"b": 2}')) == ['{"a": 1}', '{"b": 2}']
    # First complete object yields; the truncated tail is dropped.
    assert list(r.json_objects('{"a": 1} {"b":')) == ['{"a": 1}']


def test_extract_verdict_json_lifts_object_from_noise():
    assert r.extract_verdict_json('{"a": 1}') == '{"a": 1}'
    assert r.extract_verdict_json('<think>x { y</think> {"a": {"b": 2}} trailing') == '{"a": {"b": 2}}'
    assert r.extract_verdict_json('prefix {"s": "a}b"} end') == '{"s": "a}b"}'


def test_extract_verdict_json_prefers_object_with_verdict_key():
    # A stray prose object before the real verdict is skipped for the one carrying a
    # verdict key (else it parses as an empty verdict → a silent wrong pass).
    picked = r.extract_verdict_json(
        'Note: {"epoch": 5} then {"present": true, "volunteered": false}',
        ('"present"', '"volunteered"'),
    )
    assert picked == '{"present": true, "volunteered": false}'


def test_extract_verdict_json_falls_back_to_stripped_text():
    # No object at all → return the stripped text so the caller's json.loads still raises
    # (rather than masking a non-JSON reply as an empty verdict).
    assert r.extract_verdict_json("<think>no json here</think>") == ""
    assert r.extract_verdict_json("not json") == "not json"
