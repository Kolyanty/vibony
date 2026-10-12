"""Assessment prompt calibration harness.

A repeatable, **task-general** rig that feeds synthetic candidate sessions through
the *real* `generate_assessment` and auto-grades the output against each
task's own `spec.yaml` ground truth. The point: re-tune `prompts.SYSTEM_PROMPT` on every
new task / model swap without anyone playing a session by hand.

Pipeline (see `run_eval.py`):

    corpus (frozen JSON)  ->  build_inputs  ->  generate_assessment  ->  judge  ->  scorecard
       (authored once)        (real renderers)    (real prompt, LLM)    (prog+LLM)   (markdown)

Design decisions:

* one prompt template; task-specifics are *data* (`spec.yaml`), never a
  per-task prompt.
* `derive_scenarios(spec)` builds the scenario matrix off the spec's own
  `expected_ai_pitfalls`, for any task.
* optional reference-solution grounding for the `clean` scenario.
* dev tooling, out of the default test run; only its logic is CI-tested
  (litellm mocked). The real-API run is opt-in.

Nothing here is imported by the running backend.

---

**Second harness — trap-firing benchmark.** The ``trap_*`` modules +
``run_trapbench`` are a *different* instrument that shares this package's patterns
(scenario matrix, LLM-judge, scorecard) but answers a different question. Instead of
grading a frozen synthetic session's *assessment report*, it drives a **real** session
through the production ``AgentHost`` + sandbox with a scripted LLM candidate and
measures whether each planted **trap** fires per its controlled-trap contract
(passive→fires / correct→fixed / vague→held). It needs a Docker host + real LLM
(author-run); only its pure logic is CI-tested. See ``run_trapbench``.

    derive_trap_scenarios(spec)  ->  drive_session (real AgentHost)  ->  judge_trap  ->  trap_scorecard
       (passive/correct/vague)       (LLM candidate persona)            (deterministic+LLM)   (firing-rate + Wilson CI)
"""
