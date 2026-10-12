"""Reasoning-off seam + robust verdict-JSON extraction (shared).

A reasoning-family model (qwen3 / R1 / Kimi with thinking on) emits a
``<think>…</think>`` block and, even under ``response_format=json_object``, may
wrap the actual JSON in prose. Two independent problems follow, and this module
holds the two independent fixes so every LLM-judge call site shares one implementation
(controller, trap judge, candidate driver) instead of re-deriving it:

1. **The ``<think>`` block eats ``max_tokens``.** On a capped output the reasoning
   trace can consume the whole budget before the JSON is emitted → empty / truncated
   content → the caller fails open (the gate silently stops working). Fix:
   :data:`REASONING_OFF_EXTRA_BODY` — pass it as ``extra_body`` to disable thinking for
   a verdict that needs no visible chain-of-thought. It is OpenRouter-shaped (the
   provider vibony routes through) and a no-op for a non-reasoning model.

2. **The verdict is buried in prose / a fence / after a ``<think>`` block.** Fix:
   :func:`extract_verdict_json` — strip a fence, drop ``<think>`` blocks, and lift the
   first balanced ``{…}`` object that carries the caller's verdict keys.

These are the pure, task-general primitives; call-site policy (which env var overrides
the default, which keys mark the verdict) stays at the call site.
"""

from __future__ import annotations

import re

# Disable a reasoning model's thinking. OpenRouter-shaped (vibony's provider); a no-op
# for a non-reasoning model. A non-OpenRouter deployment can send a different body — the
# constant is the default, not a hard requirement.
REASONING_OFF_EXTRA_BODY: dict = {"reasoning": {"enabled": False}}


def strip_fences(content: str) -> str:
    """Strip a leading ```/```json code fence and a trailing ``` if present.

    Handles the usual multi-line form (```json\\n{...}\\n```), a fence missing its
    language tag, and the single-line form with no newline after the opening fence
    (```{...}``` or ```json {...}```). The single-line case previously left the opening
    ``` in place — the JSON then failed to parse and burned the model's one retry.
    JSON never starts with an identifier char, so stripping an optional
    leading language token can't eat into the payload. The canonical implementation for
    the whole backend (``assessment.generate`` imports it from here).
    """
    text = (content or "").strip()
    if not text.startswith("```"):
        return text
    if text.endswith("```"):
        text = text[:-3]
    rest = text[3:]
    nl = rest.find("\n")
    if nl != -1:
        rest = rest[nl + 1 :]
    else:
        rest = re.sub(r"^[ \t]*[A-Za-z0-9_+-]*[ \t]*", "", rest, count=1)
    return rest.strip()


def strip_think(text: str) -> str:
    """Remove ``<think>…</think>`` reasoning blocks — linear scan, no regex.

    A regex ``<think>.*?</think>`` backtracks quadratically on a reply with many
    unclosed ``<think`` tokens (a malfunctioning/adversarial model output), stalling
    the event loop. This is O(n): a malformed or unclosed ``<think`` drops the rest to
    end-of-string (a truncated reasoning reply then yields no object → the caller fails
    open).
    """
    lower = text.lower()
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        start = lower.find("<think", i)
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        gt = text.find(">", start)
        if gt == -1:
            break  # malformed open tag → drop the rest
        close = lower.find("</think>", gt)
        if close == -1:
            break  # unclosed reasoning → drop to EOF
        i = close + len("</think>")
    return "".join(out)


def json_objects(text: str):
    """Yield each top-level balanced ``{…}`` object substring, in order.

    String/escape-aware (a brace inside a JSON string is not a nesting level). Stops
    at the first unbalanced (truncated) object — earlier complete objects still yield.
    """
    i, n = 0, len(text)
    while i < n:
        start = text.find("{", i)
        if start == -1:
            return
        depth = 0
        in_str = esc = False
        j = start
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : j + 1]
                    break
            j += 1
        else:
            return  # unbalanced tail — no further complete objects
        i = j + 1


def first_json_object(text: str) -> str | None:
    """The first balanced ``{…}`` object, or ``None`` (no ``{`` / truncated)."""
    return next(json_objects(text), None)


def extract_verdict_json(content: str | None, verdict_keys: tuple[str, ...] = ()) -> str:
    """Best-effort extraction of a verdict JSON string from a model reply.

    Fence strip → drop ``<think>`` blocks → pick the first balanced object that carries
    a verdict key (so a stray prose object like ``{"epoch": 5}`` before the real verdict
    is skipped, not silently parsed as an empty verdict). Falls back to the first object,
    then to the stripped text, so a genuinely non-JSON reply still raises in the caller's
    ``json.loads`` (and is logged) rather than being masked.

    ``verdict_keys`` are the quoted field markers that identify the real verdict object
    (e.g. ``('"present"', '"volunteered"')``); empty → take the first balanced object.
    """
    text = strip_think(strip_fences(content or "")).strip()
    objects = list(json_objects(text))
    if not objects:
        return text
    for obj in objects:
        if any(key in obj for key in verdict_keys):
            return obj
    return objects[0]


__all__ = [
    "REASONING_OFF_EXTRA_BODY",
    "strip_fences",
    "strip_think",
    "json_objects",
    "first_json_object",
    "extract_verdict_json",
]
