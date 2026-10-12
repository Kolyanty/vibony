# Task format

**Context:** [README.md](README.md), [session.md](session.md), [docs/task-design/format.md](../task-design/format.md), [docs/weakness-format.md](../weakness-format.md).

This document describes the **technical layout** of a task: the DB storage model, the filesystem layout the repo's reference tasks use (and the seed importer reads), what goes into the candidate bundle, and what stays private.

Task content (what should be in it, how to formulate it) is a separate document, [docs/task-design/format.md](../task-design/format.md). Here we cover only the format and privacy rules.

> **Note.** Tasks live in the `tasks` DB table — see §Storage. The filesystem
> sections below describe the **reference-task seed format** and remain the authoring
> reference: authoring is the dashboard / `/tasks` REST API, the candidate is the
> browser IDE, and the bundle is built per-request from DB content.
>
> The filesystem layout is also the **portable task artifact** — a versioned,
> self-contained directory that is the decoupling boundary between a (future)
> generator, the importer, and the trap-bench validator. Full format:
> [docs/task-design/format.md §6](../task-design/format.md). `spec.yaml` carries
> `format_version` (current `1`, additive/None for older tasks) and each
> `expected_ai_pitfalls` entry may carry a free-form `trap_type` mechanism tag
> (metadata only). Import goes through `tasks/importer.py` (`read_artifact` +
> validated, idempotent `import_artifact`); the seed is one caller.

---

## Storage: tasks live in the DB

A task is a row in the `tasks` table, not a folder on disk. The dashboard authoring
UI + the `/tasks` REST API create and edit tasks; nothing reads `tasks/<id>/` at
request time.

The repo's `tasks/<id>/` folders remain as the **reference seed source**: at backend
startup `tasks/seed.py` imports each folder (an idempotent upsert) into a row owned by
the fixed system **"Vibony Templates"** workspace (`is_template=true`,
`visibility='public'`), surfaced read-only to every workspace. The folder layout
below is therefore both the seed format and the canonical reference for what a task
contains.

When a reference folder is removed or renamed, seeding first backfills existing
sessions, then marks the obsolete system template private and non-template. Its
record and content remain available to those sessions, but it is no longer offered
for new interviews. Workspace-owned tasks are not changed.

### Column mapping

| Folder artifact | DB column |
|---|---|
| — (derived: first `# ` heading of `task.md`, else title-cased slug) | `title` (required) |
| public scaffold files (`model.py`, `train.py`, …) | `starter_files` JSONB `[{path, content}]` |
| `task.md` (candidate brief) | `description_md` (synthesized back into `task.md` in the bundle) |
| `spec.yaml` (minus `id` / `runtime_image`) | `spec` JSONB |
| `spec.yaml.runtime_image` | `runtime_image` (validated vs `ALLOWED_RUNTIME_IMAGES`) |
| `weakness.yaml`'s `weakness:` mapping | `weakness` JSONB |
| `INTERVIEWER.md`, `persona-playbook.md`, `_solution.py` | not imported (interviewer / calibration only) |

`slug` is the human id (= folder name for reference tasks); `(company_id, slug)` is unique.
Visibility: `private` (author only) / `workspace` (members) / `public` (reference tasks).
A `Session` carries `task_ref_id` (FK → `tasks`, `ON DELETE SET NULL`) + a `task_id`
slug snapshot for display / workdir-name stability. The protocol `task_id` field stays
a string.

### REST (`/tasks`, cookie auth)

| Method | Path | Notes |
|---|---|---|
| GET | `/tasks` | reference tasks + caller's visible workspace tasks (own private + workspace) |
| GET | `/tasks/{id}` | 404 if not visible (existence-hiding) |
| POST | `/tasks` | create in a workspace; validates slug shape, image whitelist, private-name starter files; a dup slug → 409 |
| PUT | `/tasks/{id}` | partial update; reference tasks read-only → 403; slug/company immutable |
| DELETE | `/tasks/{id}` | reference tasks read-only → 403; sessions' `task_ref_id` → NULL |

Each `TaskListItem`/`TaskResponse` carries `can_edit` (false for reference tasks and for a
private task the caller didn't author). Session create resolves `task_id` (slug) to
an accessible task (a workspace task preferred over a same-slug reference task) and binds
`task_ref_id`.

---

## Folder layout

All reference tasks live in `tasks/<task-id>/` in the repository. `<task-id>` is a slug in `task-NN-short-name` format (e.g., `task-01-training-loop`).

```
tasks/task-01-training-loop/
├── spec.yaml              ← private, source of truth
├── task.md                ← public, candidate's task description
├── INTERVIEWER.md         ← private, interviewer guide
├── persona-playbook.md    ← private, persona scenarios
├── weakness.yaml          ← private, weakness instructions
├── _solution.py           ← private (prefix `_`), reference solution
├── pyproject.toml         ← public, scaffold
├── model.py               ← public, scaffold
├── data.py                ← public, scaffold
└── train.py               ← public, scaffold
```

Real example: [tasks/task-01-training-loop/](../../tasks/task-01-training-loop/).

---

## Private and public files

The backend splits the folder contents into two groups when building the task bundle.

### Private (NOT included in the bundle)

A fixed list of file names + a rule by prefix:

| Name | Purpose |
|------|---------|
| `spec.yaml` | Source of truth about the task (contract, acceptance, AI traps) |
| `INTERVIEWER.md` | Interviewer guide |
| `persona-playbook.md` | Persona scenarios (for calibration) |
| `weakness.yaml` | Per-task weakness instructions for the AI proxy |

**`_` prefix rule.** Any file or directory whose name starts with `_` is considered private and not included in the bundle. This is an escape hatch for reference solutions (`_solution.py`), additional internal files (`_notes.md`), private directories (`_calibration/`).

**Dotfile rule.** Any file/directory whose name starts with `.` (`.git`, `.venv`, `.DS_Store`) is not included in the bundle. These are technical artifacts, not part of the task.

### Public (included in the bundle)

Everything else. The backend doesn't maintain a positive list — what's not private is public. This reduces the risk of "forgot to add a new file to the whitelist" when designing new tasks.

**Required public file — `task.md`.** If it's missing, the task is invalid (see §"Validation").

---

## What the backend reads

### From `spec.yaml`

**Fields for rendering the interviewer UI and validation** — not for AI logic.

The backend reads:
- `id` — comparison with the directory name
- `discipline` — for grouping in the session-creation UI
- `expected_ai_pitfalls` — for rendering in the dashboard's right panel as a list of "what to watch for"

Fields the backend **does not use** (read by the author/interviewer, not the system): `context`, `scaffold`, `contract`, `acceptance`, `interviewer_notes`. They're part of the document, but not the data plane.

Full `spec.yaml` schema — in [docs/task-design/format.md §5](../task-design/format.md).

### From `weakness.yaml`

Fully. This is the AI proxy config: merged on top of `configs/global.yaml` (see [docs/weakness-format.md](../weakness-format.md)) and injected into the system prompt of LLM requests. The candidate **never** receives this file — otherwise the candidate could view the instructions and prepare to circumvent them.

### From `task.md`

The backend puts the file in the bundle without modification. It doesn't parse it. The candidate UI renders it as the "task description" at session start.

---

## Bundle

A tarball `bundle.tar.gz` built server-side from the DB task content and provisioned into the candidate's sandbox. The browser candidate never downloads or unpacks it; `GET /sessions/{code}/bundle.tar.gz` still exposes it and `bundle_sha256` is its checksum.

### Contents

All public files from the task (see the rules above), packed with relative paths from the task root. That is, in the tarball — `task.md`, `pyproject.toml`, `model.py`, `data.py`, `train.py`, not `tasks/task-01-training-loop/task.md`.

### Properties

| Property | Value |
|----------|-------|
| Format | gzip-compressed tar |
| Contents | UTF-8 text and binary files (for rare tasks with fixtures) |
| Stable ordering | Files inside the tar are sorted by name for a reproducible SHA-256 |
| SHA-256 | Computed by the backend when building the bundle, returned in `session_config.bundle_sha256` |
| Caching | The backend caches the built bundle until the task content changes |
| Maximum size | 10 MB (protection against accidentally committed model weights, etc.) |

### Building

1. The backend collects the task's public files (from the DB task's `starter_files` + a synthesized `task.md`).
2. Filters private files (fixed list + `_*` + `.*`).
3. Sorts the remaining files by name.
4. Packs them into `tar.gz` with fixed timestamps (0) — so the SHA-256 is reproducible.
5. Computes the SHA-256 and caches the result.

---

## Validation on task import

The seed importer reads all folders in `tasks/` and validates each one. An invalid folder is not imported; the backend writes a warning to the log.

| Check | What a failure means |
|-------|----------------|
| Folder contains `spec.yaml` | Otherwise the folder is not considered a task (there may be utility folders in `tasks/`) |
| `spec.yaml` parses as YAML | Syntax OK |
| `spec.yaml.id` matches the directory name | Protection against desync on rename |
| Folder contains `task.md` | The candidate needs something to work from |
| Folder contains `weakness.yaml` | An AI proxy without weakness isn't the product; an empty weakness is OK, absence is not |
| Bundle ≤ 10 MB after building | Protection against accidentally committed large artifacts |
| `INTERVIEWER.md` and `persona-playbook.md` — recommended, not required | Warning, not error |

An invalid task doesn't block the backend — other tasks remain available.

---

## Task versioning

A task is identified by its slug. Edits go through the dashboard / `/tasks` REST API; for the reference seed tasks, change history is via git. `session_config.bundle_sha256` lets a client detect if the bundle changed between the bootstrap call and a retry download (e.g., after a rolling deploy mid-session).

---

## Not in scope

- Parameterized tasks (random seed → different data/bugs).
- Multi-language scaffold (one task supports Python + Go) — one discipline = one stack.
- Dynamic task generation (an LLM creates a variation) — a separate feature, not a contract change.
- Bundle signatures (detecting file substitution) — trusted in the current model; HTTPS provides integrity.
