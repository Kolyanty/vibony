"""Scenario matrix derived from a task's ``spec.yaml``.

``derive_scenarios(spec)`` turns a parsed :class:`TaskSpec` into the same shape of
scenario set for *any* task — there is no per-task code here. Each scenario carries:

* ``persona`` — the instruction handed to the LLM author (``author.py``); also the
  human-readable intent for a hand-authored seed fixture.
* ``label`` — the ground truth (which ``expected_ai_pitfalls`` indices the scenario
  triggers, near-empty / fought-the-AI flags). The judge grades against this.

The matrix (scales with the spec's pitfall count ``N``):

* ``near_empty`` — no candidate activity. Probes D12 honesty. No LLM needed.
* ``clean`` — a solid solution that triggers **no** pitfall. The anti-hallucination
  probe: the report must not invent traps that did not happen.
* ``single[i]`` — one per pitfall: the candidate falls into pitfall ``i`` (and ideally
  no other). Probes recall of ``i`` + precision (the others must not be flagged).
* ``multi`` — falls into every ``blocking`` pitfall at once (only when ≥2 exist).
  Probes recall under load + severity mirroring.
* ``fought_ai`` — the AI pushes a pitfall, the candidate rejects it (``diff_rejected``).
  Probes that rejecting bad advice reads as a *strength/pattern*, not a gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from vibony_backend.assessment.inputs import TaskSpec


@dataclass
class ScenarioLabel:
    """Ground truth for one scenario — what the judge grades the report against."""

    near_empty: bool = False
    # Indices into ``spec.expected_ai_pitfalls`` that this scenario triggers.
    triggered_pitfalls: list[int] = field(default_factory=list)
    # The candidate pushed back on a bad AI suggestion (expect a critical-thinking
    # strength/pattern, not a gap).
    fought_ai: bool = False
    notes: str = ""


@dataclass
class ScenarioSpec:
    """One scenario to author: a stable ``key`` (filename-safe), its generation
    ``kind``, the author ``persona`` instruction, and the ground-truth ``label``."""

    key: str
    kind: str  # near_empty | clean | single | multi | fought_ai
    persona: str
    label: ScenarioLabel


def _slug(text: str, *, maxlen: int = 40) -> str:
    """Filename-safe slug from a pitfall description (for the scenario key)."""
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:maxlen].rstrip("_") or "pitfall"


def derive_scenarios(spec: TaskSpec) -> list[ScenarioSpec]:
    """Build the task-agnostic scenario matrix off ``spec.expected_ai_pitfalls``.

    A task with no pitfalls still yields ``near_empty`` + ``clean`` (the
    spec-independent probes); the per-pitfall / multi / fought-AI scenarios appear
    only when the spec actually enumerates traps.
    """
    pitfalls = spec.expected_ai_pitfalls
    scenarios: list[ScenarioSpec] = [
        ScenarioSpec(
            key="near_empty",
            kind="near_empty",
            persona=(
                "The candidate opened the session and did essentially nothing — no "
                "meaningful chat, no commands, no surviving code changes."
            ),
            label=ScenarioLabel(near_empty=True, notes="no candidate activity"),
        ),
        ScenarioSpec(
            key="clean",
            kind="clean",
            persona=(
                "The candidate produces a solid, correct solution that satisfies the "
                "contract and acceptance criteria, and VERIFIES it by running code "
                "(e.g. the resume/equivalence check). They fall into NONE of the "
                "expected AI pitfalls. The AI assists correctly. This is the "
                "anti-hallucination probe: a faithful report must not invent traps."
            ),
            label=ScenarioLabel(triggered_pitfalls=[], notes="clean, verified"),
        ),
    ]

    for i, p in enumerate(pitfalls):
        scenarios.append(
            ScenarioSpec(
                key=f"single__{_slug(p.description)}",
                kind="single",
                persona=(
                    f"The candidate falls into exactly this pitfall and no other:\n"
                    f"  - {p.description}\n"
                    f"  - how it shows up: {p.detection or 'n/a'}\n"
                    "Everything else about the solution is reasonable. The candidate "
                    "does NOT run the verification that would expose this trap."
                ),
                label=ScenarioLabel(
                    triggered_pitfalls=[i],
                    notes=f"single pitfall {i}: {p.description[:80]}",
                ),
            )
        )

    blocking = [i for i, p in enumerate(pitfalls) if (p.severity or "") == "blocking"]
    if len(blocking) >= 2:
        bullet = "\n".join(f"  - {pitfalls[i].description}" for i in blocking)
        scenarios.append(
            ScenarioSpec(
                key="multi__blocking",
                kind="multi",
                persona=(
                    "The candidate falls into ALL of these blocking pitfalls at "
                    f"once:\n{bullet}\n"
                    "They never run the verification that would expose them."
                ),
                label=ScenarioLabel(
                    triggered_pitfalls=blocking,
                    notes="all blocking pitfalls at once",
                ),
            )
        )

    if pitfalls:
        p0 = pitfalls[0]
        scenarios.append(
            ScenarioSpec(
                key="fought_ai",
                kind="fought_ai",
                persona=(
                    "The AI assistant confidently pushes a change that would cause "
                    f"this pitfall:\n  - {p0.description}\n"
                    "The candidate is skeptical, REJECTS the AI's diff (a diff_rejected "
                    "event), reasons about why it is wrong, and lands a correct fix "
                    "instead. The final code is correct — no pitfall survives."
                ),
                label=ScenarioLabel(
                    triggered_pitfalls=[],
                    fought_ai=True,
                    notes="candidate rejected bad AI advice; expect a strength/pattern",
                ),
            )
        )

    return scenarios


__all__ = ["ScenarioLabel", "ScenarioSpec", "derive_scenarios"]
