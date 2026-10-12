# Interviewer guide — task-02 seat reservation

**Private.** Not for the candidate. The candidate sees only `task.md`.

## What this task measures

Can the candidate take a deceptively simple requirement ("reserve from a shared pool,
never oversell") to correct code under concurrency, using the AI as an executor? The
AI writes a single-threaded-correct `reserve()` that races; the candidate must notice
the concurrency hazard, drive the AI to a real fix, and **verify the fix actually
worked** rather than trusting the AI's "it's safe now".

The AI is instructed to follow the controlled-trap contract in `spec.yaml` (`expected_ai_pitfalls`):
the intended behavior is to commit each planted flaw by default, hold under vague
pushback, and concede on a correct, specific diagnosis. This behavior is experimental
and must be checked against the recorded session.

## The three traps

0. **Oversell race (blocking).** The non-atomic `get → check → set` with an `await`
   between read and write. Concurrent reservations all read the same stock and all
   commit. Surfaces by firing many concurrent reservations at a small pool (confirms
   more than the stock). The fix is a single shared per-item `asyncio.Lock` (or an
   atomic store op).
1. **No idempotency on retry (degrading).** `reserve()` ignores `request_id`, so a
   retried request reserves twice. The candidate has to connect "clients retry" +
   the `request_id` parameter and ask for dedup. Weakly surfaced by verification (a
   natural oversell check uses unique ids) — mostly a code-review / reasoning signal.
2. **Missing qty guard (degrading, missing-check).** `qty <= 0` is unvalidated; a
   negative qty *inflates* stock. Surfaces by calling `reserve(item, -5, ...)`.

## Follow-ups

- Did the candidate think to run a concurrent test at all, see the oversell, and correctly
  attribute it to the non-atomic read-check-write (not "Python concurrency is weird")?
- Candidate added a lock — did they **re-run the concurrent test** to confirm the fix
  actually held, or take the AI's word that it's safe now?
- Did the candidate reason about retries / `request_id` at all? (trap 1)
- Did they probe edge cases — zero / negative qty? (trap 2)

## Calibration notes

- No verification harness ships with the task (format.md §6) — the candidate writes the
  concurrent driver in place. The race window is widened by `_IO_LATENCY_S` in `store.py`.
  If a flawed run shows no oversell under some scheduler, raise the latency, the stock, or
  the concurrency in the candidate's driver.
- Reference solution: `_solution.py` (excluded from the candidate bundle).
