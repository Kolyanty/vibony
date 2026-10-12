# Frozen scenario corpus

Committed test fixtures for the assessment prompt-calibration harness.
Each `<task_id>/<scenario>.json` is one synthetic session — see
`scripts/assessment_eval/corpus.py` for the record shape. Loaded by `run_eval.py`,
graded by `judge.py`.

## The seed is a *curated subset*, not the full mechanical matrix

`personas.derive_scenarios(spec)` produces a matrix of `4 + N` scenarios off the
spec's `expected_ai_pitfalls`. The committed seed is hand-authored (for fidelity and
zero-API availability) and intentionally covers only the high-signal axes:

- `near_empty` — D12 honesty probe.
- `clean` — anti-hallucination probe (verified solution, no trap).
- `single__no_optimizer_state` — pitfall 0 (naive checkpoint = only `model.state_dict()`).
- `single__best_state_mutation` — pitfall 2 (the *silent* trap; AI claim accepted without eval).
- `fought_ai` — candidate rejects bad AI advice → expect a strength/pattern, not a gap.

### Why `multi__blocking` is omitted for task-01

`derive_scenarios` emits `multi__blocking` whenever a task has ≥2 `blocking`
pitfalls. task-01 has two (pitfall 0 = naive checkpoint saving only weights;
pitfall 3 = `torch.load` without `weights_only=False` crashing on non-tensor
objects), but they are **mutually exclusive in manifestation**: pitfall 3 can only
fire if the candidate *does* save non-tensor state (RNG) and then loads it naively,
which contradicts pitfall 0's "only `model.state_dict()`". A coherent "all blocking
at once" session can't exist here, so it is not frozen.

General lesson: the mechanical matrix can propose incoherent combos. **`--regen-corpus`
output is a draft — review it before committing**, prune combos that can't co-occur,
and keep the human-authored seed as the trusted baseline.
