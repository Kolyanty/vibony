# Vibony — contributor instructions

## Purpose and current priority

Vibony is a self-hostable, Apache-2.0 platform for technical interviews with an AI
assistant: browser IDE, live/async sessions, isolated execution, replay and assessment.
Its distinguishing mechanism is task-defined AI mistakes and an optional controller
that tries to preserve them until the candidate diagnoses them.

The current priority is **open-source release preparation**, not a commercial SaaS
launch. Keep the name Vibony. Do not add billing, an AI task generator, or new product
milestones as part of release preparation.

Read [README.md](README.md) for the product, startup instructions and known
limitations, and [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow.
Use [docs/README.md](docs/README.md) to find architecture and protocol documentation.

## Repository map

- `backend/`: Python 3.12–3.13, Poetry, FastAPI, async SQLAlchemy/PostgreSQL, Alembic,
  LiteLLM, session agent and sandbox management. See [backend/AGENTS.md](backend/AGENTS.md).
- `dashboard/`: React, TypeScript, Vite, npm; employer UI, candidate IDE and replay.
  See [dashboard/AGENTS.md](dashboard/AGENTS.md).
- `tasks/`: two reference tasks: `task-01-training-loop` (ML checkpoints) and
  `task-02-seat-reservation` (concurrent reservations).
- `sandbox/`: Python and Node Docker images. See [sandbox/README.md](sandbox/README.md).
- `configs/global.yaml`: shared assistant prompt configuration.
- `backend/scripts/assessment_eval/`: assessment and trap-control evaluation tools.
- `scripts/`: standalone development helpers. See [scripts/AGENTS.md](scripts/AGENTS.md).
- `docs/protocol/`: shared REST/WebSocket/event/task/tool/sandbox contracts.
- `docs/task-design/format.md` and `docs/weakness-format.md`: task and prompt formats.

## Working rules

- Respond to the owner in concise Russian. Keep code, comments and public docs in
  English; preserve the dashboard's intentional English/Russian localization.
- Make focused, reviewable changes. Preserve unrelated work. Record durable decisions
  in the relevant documentation rather than only in chat.
- Keep backend schemas, dashboard types and protocol docs consistent in the same change.
- Treat task flaws as intentional measurement fixtures. Do not “fix” planted mistakes
  or reference solutions without understanding the task contract and mechanics tests.
- `spec.yaml` is the task contract; `task.md` is its candidate-facing projection.
  Specs, weakness instructions, interviewer guides and solutions may be public in the
  source repository, but must stay out of candidate bundles at runtime.
- Never commit credentials, local `.env` files, candidate data or generated transcripts.
  Read templates rather than printing local secrets. Check tracked files when preparing
  exports: a gitignore rule does not remove already tracked data.
- Use available tools directly. The old Claude Code `/commit` skill, named subagents
  and `.claude/` hooks are not required dependencies. Delegate only when requested by
  the user or an applicable instruction; no mandatory test-runner agent.
- Review the diff before finishing. Commit or push only when requested. Rewrite
  history or change repository visibility only with explicit owner authorization.

## Verification

Run checks appropriate to the change; document exactly what ran and any limitations.
Do not present historical test counts as a fresh result. Documentation-only edits need
link/path and diff checks, not a full application test run.

```bash
# Backend (from backend/; local PostgreSQL binaries required)
poetry install
poetry run pytest -q

# Dashboard (from dashboard/; use the version in .nvmrc)
npm ci
npm run test
npm run lint
npm run build
```

Do not hide failures or add unconditional skips. Infrastructure-dependent skips must
encode the actual missing prerequisite. See component instructions for fixture and
runtime details. Changes to startup commands/env/ports also need matching README and
Compose updates.

## Paid evaluations and publication

Real `run_trapbench`/`run_eval` evaluations and LLM/image smoke scripts can spend money.
By default the owner runs them: prepare an exact, small smoke command first, then a
larger run only on request. Do not start paid evaluations, background sweeps or their
infrastructure as routine verification. Mocked evaluation unit tests are ordinary tests.

Keep internal planning, review notes, local backups and real session transcripts
outside the repository. Preserve the curated public history; do not reintroduce
archived development history. Confirm GitHub CI before a release. Repository
visibility changes remain an owner action.
