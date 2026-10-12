# Vibe-coding interview task format

Describes criteria for "what makes a good task" in vibe-coding format, regardless of discipline (DS, backend, frontend, ML-infra, tools — anything). Narrows the surface where task authors (and, in perspective, an AI generator) can go wrong.

**Context:** [architecture.md](../architecture.md). Reference examples: [training loop](../../tasks/task-01-training-loop/) and [seat reservation](../../tasks/task-02-seat-reservation/). Their contracts illustrate the format; they do not establish interview validity or model reliability.

**Language-agnostic.** The platform is not tied to any language or stack. Each task declares its own sandbox via `runtime_image` in `spec.yaml` (e.g. `vibony/sandbox-py:3.12`, `vibony/sandbox-node:20`); the candidate solves it in whatever language that image provides. The two reference tasks happen to be Python to demonstrate checkpoint persistence and concurrent state updates — Python, `poetry`, and `pyproject.toml` appearing below are concrete instances, **not** a product limitation. Read every Python-specific mention as "the equivalent in the task's `runtime_image`".

---

## 1. Thesis

A good vibe-coding task tests whether **the candidate can bring a non-trivial requirement written by another person to working code, using AI as an executor**. The interview artifact — code whose behavior can be evaluated binarily ("does what was asked / doesn't do"). The candidate must understand the requirement, break it into chunks, give AI the right instructions, read the result, notice where AI erred, and correct it.

This is **not**:
- A research task ("try different approaches, choose the best"). The artifact there is insight, not code. AI errors hide behind "well I tried, didn't work" and aren't observable.
- A design interview ("design system X"). Almost no code, AI barely needed.
- An algorithmic competition for speed. Vibe-coding isn't about how fast you write BFS yourself — it's about how you direct AI on a piece of work you wouldn't finish alone in the allotted time.

## 2. Task evaluation axes

Six axes. A good task hits the right column in most.

| Axis | Bad for vibe-coding | Good for vibe-coding |
|------|--------------------|--------------------|
| **Artifact** | Insight, report, approach choice | Working code with specified behavior |
| **Verifiability** | Continuous metric "better/worse" | Binary: behavior matches spec or not |
| **Engineering depth** | Gluing ready-made calls in 20 lines | Non-trivial invariants, state, edge cases |
| **AI trap surface** | AI errors in approach choice (not catchable as a bug) | AI predictably errors in code, and the error manifests on run |
| **Solution space** | Open: dozens of valid directions | Constrained: many ways, but narrow behavioral spec |
| **Data dependency** | Solution quality determined by data properties | Data is background; solution determined by code logic |

No axis is absolute. For example, "implement parser X" can be either strong (spec is clear, traps in edge cases) or weak (if format is trivial). Axes — criteria for checking, not a formula.

## 3. Anti-patterns

### 3.1. Open-ended goal ("make it better")

Task is formulated as improving a metric, without defining "good enough". Candidate and AI can tweak the solution in any direction, and both "success" and "failure" are indistinguishable from noise.

**Hypothetical example:** "Improve forecast quality. What to count as 'better' — your call". This is a valid DS task (this is what real work looks like), but in interview format it:
- Gives no "failed / completed" signal — any MAPE change can be explained.
- Doesn't create moments where AI structurally misses the mark (task mismatch, API hallucinations, contextual literacy).
- Blurs evaluation: interviewer is forced to compare "research quality", not "AI steering quality".

### 3.2. Artifact is analysis, not code

If the outcome is a comparison table, textual conclusion, or decision "I'll take model A", then AI is working as a consultant, not executor. The point of application for vibe-coding skill is absent.

### 3.3. Spec that AI solves one-shot

If the task fits in one prompt and AI produces correct code on the first try — we don't observe what we're running the interview for: ability to direct, verify, correct. The task must be either large enough, or contain traps where AI predictably errs.

### 3.4. Spec impossible to verify compliance with

"Make a beautiful UI", "write understandable code", "optimize" without metric definition. Interviewer has nothing to rely on.

### 3.5. task.md that narrates the traps

If `task.md` explicitly lists in requirements or acceptance the exact invariants where AI should get caught, AI diligently implements them on the first try — the trap is neutralized. The candidate doesn't need to "notice" anything, they just read the requirements.

**Example:** in a training-loop task, explicitly requiring equivalence between resumed and continuous training reveals the checkpoint invariant the candidate is meant to identify. Keep the candidate-facing goal at "resume works without losing progress", and put the precise invariant in the author-facing contract.

**Rule:** invariants where AI should get caught live in `spec.yaml` (author-facing contract) and `weakness.yaml`, **not** in `task.md`. In `task.md` stays the high-level goal ("resume works, progress isn't lost"), while formulating the equivalence invariant — is the candidate's side. That's the competency test.

**How to check when designing:** for each trap from `expected_ai_pitfalls` go through `task.md` and ask: "is there a phrase here that directly requires this trap to be closed?" If yes — reformat as a more general goal or remove. Same check for `weakness.yaml`: after rendering into system prompt, AI gets an explicit signal "write a simplified version", and this signal must not contradict what AI reads in `task.md`.

### 3.6. A ready-made verification harness shipped with the task

Sibling of §3.5. Even if `task.md` never names the traps, shipping a **ready-made check script** in the candidate bundle (a `loadtest.py`, a test file, a driver that prints `ok`/`FAIL` per invariant) neutralizes the exercise a different way: it does the *detection* work for the candidate. Noticing that "list the orders" needs a two-user test, that a per-order loop is an N+1, that pagination needs a boundary check — that is the competency. A shipped harness hands it over. It also inverts the priority: the task ends up shaped for the validation bench (which wants a runnable check) rather than for a real interview.

**Example:** a ready-made concurrency driver that prints an oversell verdict for the [seat-reservation task](../../tasks/task-02-seat-reservation/) would perform the diagnosis for the candidate. The shipped task instead describes the expected behavior and asks the candidate to write a check.

**Rule:** the candidate bundle ships **no** ready-made verification harness. `task.md` describes verification as a **procedure / expected behavior** per requirement ("seed several users and list as one — you get back only that user's orders"; "delete the checkpoint, run n_epochs=10 then 20 — it should continue from 10"), and the candidate + AI write the check in place. This mirrors the canonical [task-01](../../tasks/task-01-training-loop/): it ships no test script — `task.md` states the resume/early-stopping checks as procedures, and the trap-bench's `breaking` persona executes that procedure via the AI (it does **not** need a given script). Do not ship a scaffold file whose only purpose is to run the traps' checks, and do not add an affordance to a `data`/`utility` file that exists only to make a trap observable (for example, a query counter added solely to expose an N+1) — that is a hint. A trap that only a given harness would have surfaced (e.g. an N+1, whose output is correct) becomes an accepted **scenario gap**, documented under `known_limitations`, not a reason to ship the harness.

## 4. Pro-patterns

### 4.1. Implement a specified mechanism to an exact spec

Task describes behavior in terms of requirements, each verifiable. Candidate must understand the spec, break it into sub-requirements, and guide AI through them.

**Example:** [task-01-training-loop](../../tasks/task-01-training-loop/): training loop with explicit requirements — Adam + MSELoss, per-epoch logging, early stopping with patience, checkpoint after each epoch, resume without losing progress. Each point verifiable by running. What is **not** listed in `task.md` and the candidate formulates themselves — exactly what fields should be in the checkpoint (optimizer/RNG/epoch), what invariant defines "progress isn't lost" (equivalence to continuous run), best vs last weights. See §3.5 — precisely because these invariants aren't in `task.md`, the traps remain active.

### 4.2. Planted AI traps

Task is designed so that AI with high probability makes specific errors in code, and these errors are observable when running or reviewing. A good candidate catches them, a weak one doesn't.

**Example:** in task-01 — checkpoint without optimizer state. AI often writes `torch.save(model.state_dict(), ...)`, forgetting optimizer and random state. On resume training degrades. Visible when testing.

Traps must be:
- **Predictable** — meaning AI commits them regularly, not occasionally (otherwise nothing to calibrate against).
- **Not stylistic** — the difference between right and wrong is unambiguous, not "a matter of taste".
- **Local in code** — can point at specific lines.

### 4.3. Volume that doesn't fit in one prompt

Task requires multiple iterations — multiple files, multiple phases ("first structure, then logic"), or bugs discovered during runs. This creates the field for observing candidate behavior.

### 4.4. Scope control

Candidate's time goes toward verifiable behavior, not bootstrap (creating the project, setting up environment, writing boilerplate that isn't evaluated). This is achieved by one of three approaches — choice depends on the task:

**(a) Starter scaffold.** Candidate is given ready-made chunks they don't need to write (model, data, utilities), and the chunk to be written is explicitly marked. Used when the context is heavy and without it 20+ minutes would go to setup. Example: task-01 gives `model.py` and `data.py`, candidate only writes `train.py`.

**(b) Compact from-scratch task.** If the task naturally fits in one or two files (parser, algorithm, CLI utility, protocol implementation), scaffold isn't needed and is even harmful — the structure of the solution is itself part of what's being tested. Given: `pyproject.toml` with dependencies and an empty starter file with signature/contract. Everything else — the candidate, including how to split into modules.

**(c) "Design and implement" task.** Bootstrap is explicitly outside the timer (repository with `pyproject.toml` already created), but architectural decisions inside — are part of evaluation. Used for middle/senior when ability to structure a system is the tested skill. Requires especially clear behavioral spec and acceptance criteria, otherwise slides into anti-pattern §3.4.

What must **not** happen in any variant: candidate spends the first 15 minutes on environment bootstrap — `poetry init` and copying dependencies (or the `runtime_image` equivalent: `npm init`, `go mod init`, …), setting up tooling. This is not vibe-coding, it's unboxing.

### 4.5. Controlled traps (direction)

§4.2 describes traps as **natural** AI mistakes — the AI errs on its own, the candidate catches it. In practice frontier models are careful and commit these only intermittently, so trap-firing is unpredictable: two candidates get materially different interviews. **The platform makes traps controlled** — the prompts and optional controller try to preserve each planted trap until it is diagnosed — so sessions can target comparable mistakes. This is experimental behavior, not a deterministic guarantee.

A controlled trap is a **conditional behavioral contract**, not a dumb script:

- **flawed_behavior** — by default (candidate passive or vague), the AI commits the planted error.
- **correct_behavior** — if the candidate **correctly diagnoses** the issue, the AI concedes and fixes it. Without this the candidate cannot pass — worse than an unreliable trap.
- **defense** — under **vague / incorrect** pushback ("just redo it"), the AI holds the flaw plausibly. Without this, weak candidates pass by luck.

The competency measured = moving the AI from flawed→correct by a **correct** diagnosis. Naturalness lives in the texture (the AI is a believable assistant everywhere except the planted flaw + its defense); determinism lives in the flaw.

**Plausible-error-surface criterion.** Induce only mistakes that are *realistically plausible* — forgetting optimizer state is a common oversight, not "playing dumb". A trap requiring blatant stupidity breaks face validity: a strong candidate smells the sandbagging. "Always wrong here" is acceptable **only** when the wrong version reads as a natural first-draft. This is a selection constraint on which traps a task may use.

The control *mechanism* (system-prompt directives vs an intermediate controller that analyzes candidate turns vs model-tier selection) is a genuine research question; the shipped mechanism is an intermediate controller that reviews each candidate turn against the trap contracts.

**Content vs template (storage decision).** A trap's *contract* — the fields in §5's `expected_ai_pitfalls` — is **content**: model-agnostic task data (source of truth `spec.yaml`, runtime `tasks` DB row). A task is never rewritten per model. The *framing* that renders that contract into a system prompt, the judge prompt, and any control prompts is **template**: code/config in git, resolvable per model via the seam in [architecture.md](../architecture.md) §7. This split is why the contract lives on the task and the prompt-rendering lives in the backend.

## 5. Task specification template

Task author (or AI generator) must fill in these fields. If even one can't be filled in concretely — the task isn't ready.

```yaml
id: task-NN-short-name
format_version: 1                       # portable-artifact format version (§6); additive, None for legacy tasks
discipline: backend | ds | frontend | ml-infra | tools | ...

# Sandbox / runtime — selects language and stack for this task
runtime_image: vibony/sandbox-py:3.12   # e.g. -py:3.12 | -node:20 | -go:1.22 | ...

# What candidate receives
context: |
  Brief situation description in 2-3 sentences. Not a "business story",
  but context for why the code is needed.

scaffold:
  # Files given to candidate ready-made
  - path: ...
    role: model | data | utility | implementation-stub

# What needs to be done
contract:
  # Behavioral requirements. Each — verifiable by running or review.
  - "Function X accepts Y and returns Z given condition A"
  - "On input B the program must do C"

acceptance_criteria:
  # Binary checks. Collectively — define "task completed".
  - "Running `cmd` completes without errors"
  - "Behavior P is observable in scenario S"

# What we expect from AI and candidate.
# expected_ai_pitfalls carries the controlled-trap contract (§4.5).
# description/detection/severity is the subset the assessment reads;
# trigger/flawed_behavior/correct_behavior/defense/check are parsed onto the
# assessment Pitfall model and drive the control mechanism + benchmark.
# Existing tasks stay valid — the extra fields are additive (None when
# absent). Worked example: tasks/task-01-training-loop/spec.yaml (first pitfall).
expected_ai_pitfalls:
  - description: "AI forgets X when implementing Y"        # human-readable label
    trap_type: missing-check                               # defect-mechanism label (see §6); free-form, optional
    trigger: "candidate works on requirement Y"            # when the trap applies
    flawed_behavior: "AI produces the version missing X"   # default (passive/vague candidate)
    correct_behavior: "if the candidate names X specifically, AI adds it"  # concede on correct diagnosis
    defense: "under vague 'redo it', AI keeps the flaw, plausibly"          # hold under weak pushback
    detection: "manifests when running with scenario Z"    # signature the judge checks
    check: "run cmd / AST-grep pattern / equivalence test" # deterministic check where possible
    severity: blocking | degrading | cosmetic

failure_signal:
  # What interviewer sees if task is failed.
  - "Acceptance criterion N not met"
  - "Candidate didn't notice trap P, and it made it to final code"

# Operations
duration_minutes: 60
difficulty: junior | middle | senior
runtime_limits:        # sandbox resource caps (optional; defaults applied if omitted)
  memory: 2g
  cpus: 2
```

## 6. Portable task artifact

A task ships as a **self-contained directory** — the decoupling boundary three things
agree on: a (future) **generator** *produces* it, the vibony **importer** loads it
into a `tasks` row, and the **trap-bench validator** *consumes* it. The generator
depends only on this format, never on vibony code. This formalizes the existing
`tasks/<id>/` layout (the seed source) into an explicit, versioned contract; an
external author needs only this section, not the backend.

### Layout

```
task-NN-short-name/
├── spec.yaml          ← REQUIRED — manifest + source of truth (see below)
├── task.md            ← REQUIRED — candidate projection (the only candidate-facing brief)
├── weakness.yaml      ← REQUIRED — AI-assistant config (private)
├── <starter files>    ← optional — public scaffold shipped to the candidate (pyproject.toml, app.py, …)
├── _solution.*        ← optional — reference solution (private; `_` prefix)
└── validation/        ← reserved — validation records travel with the artifact
```

**No shipped verification harness (§3.6).** The starter files are stubs + data/utilities the candidate builds on — **not** a ready-made check script that runs the traps' invariants. Verification lives in `task.md` as a procedure the candidate + AI perform; shipping a harness does the detection work for them and shapes the task for the bench rather than the interview.

`spec.yaml` **is the manifest** — there is no separate manifest file (a second file
would duplicate `id` / `runtime_image` / `scaffold` and could drift). It carries:

- `id` — the slug (matches the directory name for repo templates).
- `format_version` — the artifact format version (current: **1**). Additive; `None` for
  older tasks. Bump only when the layout changes in a way an importer must branch on.
- `runtime_image` — the sandbox image (validated against `ALLOWED_RUNTIME_IMAGES`).
- `scaffold` — the starter files + their `role` (model / data / utility /
  implementation-stub). **Descriptive**, for the author/generator — the importer does
  not use it to decide what to ship (it ships every non-private top-level file; see
  §"Private and public files"). Keep `scaffold` in sync with the actual files by hand.
- the contract / acceptance / `expected_ai_pitfalls` (incl. the §4.5 controlled-trap
  contract + the `trap_type` tag below) / `failure_signal` / operations fields from §5.

**Private files are a naming convention, not per-file manifest flags** (defense in depth,
enforced again on the serving path): the reserved names `spec.yaml` / `weakness.yaml` /
`INTERVIEWER.md` / `persona-playbook.md`, plus any `_*` or `.*` name, never reach the
candidate bundle. See [docs/protocol/task-format.md](../protocol/task-format.md).

### `trap_type` — defect-mechanism tag

Each `expected_ai_pitfalls` entry may declare a `trap_type` classifying its **defect
mechanism**. Canonical set:
`state-loss` · `concurrency/race` · `missing-check` · `boundary/off-by-one` ·
`silent-coercion` · `perf-N+1`. Free-form (a generator may coin a new mechanism),
optional, `None` when absent — purely additive. It is **metadata, not a control**: the
controller and the trap-bench judge stay task-general and never branch on it. It
exists because validation evidence is grouped by mechanism — that is the
generalization question ("does the controller hold *this kind* of mistake across
tasks?").

### Importer

`tasks/importer.py` loads one artifact directory into a `tasks` row — `read_artifact`
(pure filesystem read) + `import_artifact` (validate + idempotent upsert). Validation
re-applies the task field rules: slug shape, the `runtime_image` whitelist, bare
non-private starter-file names + size caps, JSONB size. The repo **template seed**
(`tasks/seed.py`) is one caller (persistent, `is_template`); the **trap-bench** is
another, staging an arbitrary artifact into an ephemeral row to validate it
(`validate(artifact_dir) → verdict`), dropped when the sweep ends.

## 7. What this document doesn't cover

Intentionally out of scope for now:
- Task categorization by disciplines and subcategories.
- Difficulty calibration (junior/middle/senior) — nothing to calibrate against yet.
- AI task generation pipeline — need to stabilize criteria and collect ≥3 validated tasks.
