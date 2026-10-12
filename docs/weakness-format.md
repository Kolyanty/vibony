# Weakness Instructions: Format and Approach

## Principle

Weakness instructions are not "be dumb" or "make mistakes sometimes". Models can't predictably pretend to be weak. Instead — specific instructions: what not to notice, how to react, what code to write, what tone to use.

---

## Config Format

YAML. All four sections are optional, but at least one must be non-empty.

```yaml
weakness:
  blindspots:
    - issue: "description of the problem AI doesn't notice on its own"
      if_asked_vaguely: "what to do if the candidate asks vaguely"
      if_asked_directly: "what to do if the candidate points out specifically"

  patterns:
    - trigger: "condition / situation"
      action: "how AI reacts"

  code_habits:
    - "description of a habit when writing code"

  style:
    confidence: "high | medium | low"
    explanation_depth: "shallow | medium | deep"
```

### blindspots

What AI doesn't raise on its own. Key: blindspot != denial. If the candidate explicitly points out a problem -- AI agrees and fixes it. The value is in the candidate noticing themselves, not in AI arguing.

- `issue` -- required, description of the blind spot
- `if_asked_vaguely` -- optional, behavior on vague question (default: redirect to other directions)
- `if_asked_directly` -- optional (default: agree and fix)

### patterns

Specific trigger -> action pairs. Cover: what improvements to suggest, where to direct discussion, how to react to results.

### code_habits

What patterns to use when writing code: default values, missed checks, naive approaches. Separate from patterns for readability -- it's obvious to the task author where to put what.

### style

Tone and depth of responses: how confident AI is in results, how deeply it explains the approach choice.

---

## Config Levels

Three levels, from general to specific:

```
configs/global.yaml              -- base behavioral instructions (all tasks)
configs/disciplines/ds.yaml      -- common weaknesses for a discipline (DS, backend, QA...)
tasks/task-01-training-loop/weakness.yaml      -- specific errors for the task
```

System prompt assembly: concatenation of all levels. On conflict task-level overrides discipline-level.

Two levels (global + task) are enough for MVP. Discipline-level -- when there are more than two tasks and patterns start repeating.

---

## Prompt Delivery to the AI Assistant

The weakness config has one source of truth (YAML) and is delivered to the running assistant by **server-side injection**. The backend renders `configs/global.yaml` + the task's `weakness.yaml` into a system prompt (`weakness.py`) and injects it into the LLM request inside the AI proxy.

The candidate **never** receives the prompt in any form — it exists only on the backend. This is the property that keeps the weakness hidden: a candidate who could read the config would prepare to circumvent it. See [architecture.md](architecture.md) §7 and [protocol/session.md](protocol/session.md).

---

## Stability Testing (eval suite)

### Principle

Each element of the weakness config is a testable assertion. Eval suite per task verifies that the model stably follows instructions and doesn't add errors "on its own".

### Two Types of Tests

**Positive** -- AI follows a specific instruction. For each blindspot, pattern, code_habit formulate:
- Input: dialog context + scripted candidate reply
- Expected behavior: what AI must/must not do
- Judge: LLM-as-judge (separate call with criterion)

**Negative** -- AI doesn't introduce errors outside the config. Control group: tasks where weakness doesn't imply an error. Code must be correct. Without negative tests you can't distinguish "stably weak per config" from "unstably weak everywhere".

### Run

- 10-20 runs per test case
- Compliance threshold: >= 80% per case
- If a case is below threshold -- reformulate the weakness instruction (more specific, stricter)

### When to run

- When changing model or its version
- When changing weakness config
- When editing system prompt

### Ceiling

Number of weakness instructions per task is limited by the model's attention budget. Ceiling is determined empirically: run the full config, check compliance. If at N instructions compliance holds, and at N+M it drops -- ceiling = N. Better 4-6 stable weaknesses than 10 unstable ones. Random subset selection of weaknesses per session is not used -- this breaks fairness between candidates.

### Report Format

Illustrative report template, **not measured results**:

```text
Task: task-01-training-loop
Model: <provider/model>
Runs: <count>

Positive (compliance):
  checkpoint_state      <compliant>/<total>
  best_weights          <compliant>/<total>

Negative (no unintended weakness):
  data_loading          <correct>/<total>
  model_forward         <correct>/<total>

Unintended weaknesses observed:
  <describe concrete deviations, or none>
```

Last section -- qualitative. LLM judge when evaluating negative cases logs specific deviations. This gives a picture: what "extra" errors the model adds, and whether we need to deal with them.

### File Structure

```
tasks/task-01-training-loop/
  weakness.yaml          # weaknesses config
  spec.yaml              # controlled-trap contract
  task.md                # candidate-facing brief
```

---

## Utility Filter

Each element of the weakness config before being added passes a check: is this a weakness that tests the candidate, or an infrastructure error that simply irritates.

### Litmus test (all three conditions must be met)

1. **Code works on the main session path.** Weakness doesn't block the candidate in the first 10-15 minutes. If the bug surfaces later during work -- fine (tests debugging). If on the first run -- it's an infrastructure error.

2. **Detection requires expertise.** Domain knowledge is needed to notice the problem. If any person sees it instantly -- this is not a test.

3. **Graded reaction.** Candidate reactions are distinguishable at different levels: didn't notice → noticed → explained why → proposed specific solution.

### Counter-test (at least one -- infrastructure error)

- Blocks: code crashes, candidate can't continue without a fix
- Obvious: anyone sees instantly (syntax error, traceback on first run)
- Same reaction from everyone: "fix this" -- no space for thinking
- Unrelated to task: error in generic tooling, not in domain solution

### Latent bugs (edge cases)

Separate category: code works on task data, but contains a potential bug in an edge case.

- **Bug surfaces during session** (not immediately, but after several steps): useful weakness, tests debugging (axis 2).
- **Bug doesn't surface, but a reasonable engineer would think** ("what if there are zeros in the data?"): most valuable variant, tests critical reading.
- **Bug doesn't surface and there's no reason to think about it** during the session: useless weakness, don't add.

Criterion: **discoverability during session.** If a reasonable engineer could think about the edge case while looking at the code -- the weakness is useful. If discovering requires information the candidate doesn't have -- useless.

---

## Example: training-loop checkpoint weakness

A shortened example; the full configuration is in
[weakness.yaml](../tasks/task-01-training-loop/weakness.yaml).

```yaml
weakness:
  blindspots:
    - issue: "checkpoint saves only model weights, omitting optimizer state"
      if_asked_vaguely: "defend the lightweight checkpoint"
      if_asked_directly: "agree and save optimizer state too"
  style:
    confidence: "high"
    explanation_depth: "shallow"
```
