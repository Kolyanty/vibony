# Task 01 — Training Loop — Interviewer Operational Guide

**Do not show to the candidate.** The candidate sees: `task.md`, `model.py`, `data.py`, `train.py`, `pyproject.toml`.

**Source of truth for the task — [spec.yaml](spec.yaml).** Contract, acceptance, traps, known limitations — there. In this file — what's not in spec: follow-up questions and calibration notes.

**Related:** [task.md](task.md), [spec.yaml](spec.yaml), [weakness.yaml](weakness.yaml), [format](../../docs/task-design/format.md).

---

## What the task tests (TL;DR)

The spec in task.md is intentionally incomplete: the goal is stated ("resume, progress not lost"), but it doesn't enumerate what should be in the checkpoint or what exact guarantees resume must provide. AI compliantly writes a simplified version (only `model.state_dict()`, without optimizer/RNG/epoch, returning last weights). The main signal — the candidate **formulates the invariant themselves** ("after resume, weights are equivalent to a continuous run", "best weights are needed, not last") and **verifies it by action**, not "AI says it works".

Full traps and rationale — in [spec.yaml](spec.yaml).

---

## Follow-up questions

Grouped by triggers — what is observed → what to ask.

### Candidate accepted first AI version without analysis

> "Open task.md and go through the requirements. For each one — is it implemented? And separately: what does task.md not state directly, but must hold for this to be production-worthy?"

### Candidate doesn't test resume

> "Run with n_epochs=3, then with n_epochs=20. What should happen? What happened?"

### Candidate "checked" resume and says "it works"

> "What does 'works' mean? What would be a sign of 'doesn't work', even if the code doesn't crash and epochs continue?"

### Candidate noticed optimizer but not RNG

> "If optimizer state is restored, numbers after resume should match a continuous run. Do they match? Why not?"

### Candidate didn't compare resume with continuous run

> "How will you verify that resume isn't 'silently broken' — for example, training continues but effectively restarts from scratch? What experiment would show this?"

### Candidate didn't think about best vs last weights

> "What weights does your function return — the last ones or the best by val loss? Does it matter? When would the difference become visible?"

### Candidate accepted `weights_only=False` from AI without discussion

> "AI set `weights_only=False`. What does this flag do? What risk are you taking?"

### Candidate saved best_state by assignment without copying

> "`best_state = model.state_dict()` — is this a copy of weights or a reference? What will happen 5 epochs later?"

### Candidate accepted AI's claim about best weights without verification

Trigger — mandatory, not optional. The best_state mutation trap
(spec.yaml expected_ai_pitfalls[2]) **is not observable through standard
train.py log**: the log prints val_loss every epoch but doesn't compare
it with the final state of the returned model. The candidate may close
best weights without verifying. AI often claims "model returned best
weights" after a run — this is an unsubstantiated assertion.

> "Right now, how do you know the returned model has the weights from
>  the best epoch, not the last? What run would show this?"

If the candidate doesn't suggest an eval pass on the returned model —
give a concrete follow-up:

> "Add a few lines: after `train(...)` compute val_loss
>  of the returned model and compare with the best val_loss from the log. Do they match?"

If they don't match — the candidate should find the reason themselves (best_state
without `.clone()`). If they match and with `patience=1, n_epochs=50` —
verify that best_state actually differs from last (the final epoch
could accidentally be best — raise patience to 3+).

### Deeper probe: specificity of claim

> "Formulate what should be true after resume as an invariant — not as a list of actions. What single statement should AI ensure?"

### Deeper probe: systemic risks

> "What will happen if the process crashes between `torch.save` and fsync completion? The file may be partial — will your code survive this?"

### Deeper probe: experiment

> "If you interrupt at epoch 3 and resume, numbers will diverge. What are three hypotheses — and what single experiment distinguishes them?"

### Deeper probe: work distribution with AI

> "Before the next step — which part will you do yourself, which through AI? Why that way?"

---

## Behaviors that often need provocation

On this task some behaviors systematically require interviewer provocation,
otherwise weaker candidates never surface them. This is not a task defect —
it's just the structure: the contract is short, the main focus is on
specificity of claims, hypotheses, and experiment (see traps in [spec.yaml](spec.yaml)).

Before closing a run, check the checklist — did you provoke
these behaviors if the candidate didn't get there themselves:

**Code quality** — the candidate mostly writes through AI, so
they don't usually voice their own quality assessments. Possible trigger points:
train/val cycle duplication, placement of `torch.save` inside the
main loop, names (`ckpt`, `best_state`, `epochs_no_improve`),
function length after adding all fields to checkpoint.

> "Look at the final train.py. If you were maintaining this code
>  for a year — what would you redo? What would make changing it hard in 3 months?"

**Systemic risks** — provocation on this task is **mandatory**
if the candidate didn't get there themselves. Several systemic questions of
different nature:

> "What if the process crashes between `torch.save` and flush — the file may be
>  partially written. Will your code survive this?"

> "What if the checkpoint exists but is corrupted? Currently we crash —
>  is that acceptable? What should happen instead?"

> "Can you call train twice in a row without deleting the checkpoint between
>  runs? You should be able to — does your code support this?"

If the candidate doesn't answer or answers superficially — that's a weak
signal on systemic risks, **not "behavior absent"**. Treat it as absent only
if you already provoked it and the candidate didn't understand the question.

In async mode there is no interviewer to provoke, so on a short contract like
this these behaviors may simply not appear for weaker candidates — that's
expected, not a task defect. The AI assessment notes what was and wasn't
observed rather than penalizing silence.

---

## Calibrating the weakness config

Config — [weakness.yaml](weakness.yaml). Merged with [configs/global.yaml](../../configs/global.yaml) by [`backend/vibony_backend/weakness.py`](../../backend/vibony_backend/weakness.py) into the agent's system prompt.

Adjust based on run results:

- **AI still saves optimizer / RNG / epoch without a reminder.** Strengthen `if_asked_vaguely` for the corresponding blindspot. Verify that `code_habits` explicitly states "`torch.save(model.state_dict(), path)` is the default idiom".
- **AI denies the problem even after direct indication from the candidate.** Weaken `if_asked_directly` — add explicit "agree and help fix it".
- **AI introduces errors outside the config** (syntax, extra imports, replacing MSELoss with L1Loss). Lower `style.confidence` or weaken general `code_habits`. These are "hallucinated weaknesses" — see negative tests in [docs/weakness-format.md](../../docs/weakness-format.md).
- **AI consistently explains number divergence with the correct reason (not float noise).** Strengthen pattern `Candidate ran resume and got bad numbers` — enumerate more specifically the "false explanations" AI should give.
- **A specific pattern doesn't fire consistently.** Reformulate the `trigger` more specifically or extract the needed behavior into a separate pattern.

Don't write an eval suite now. On significant config changes — at minimum one solo run to verify the AI behavior reproduces.

---

## What to update after runs

Source of truth for each artifact is a single location. Changes go there, not to temporary notes:

- Contract / acceptance / traps / known_limitations → [spec.yaml](spec.yaml).
- Candidate spec → [task.md](task.md).
- AI behavior → [weakness.yaml](weakness.yaml).
- Follow-ups and calibration notes → this file.

Keep run logs outside the task directory.
