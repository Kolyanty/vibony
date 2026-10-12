# Contributing to Vibony

Vibony is an experimental, self-hostable interview platform. Contributions that make
sessions more reliable, tasks clearer, or local setup easier are welcome. Read the
[README](README.md) for setup and current limitations.

## Development workflow

1. Use Python 3.12 or 3.13, Poetry 1.6.1, PostgreSQL 16 binaries and the Node version
   in `dashboard/.nvmrc`. Copy `backend/.env.example` to an untracked `.env` for local
   development. Keep API keys and interview data out of commits.
2. Install with `cd backend && poetry install --with dev` and
   `cd dashboard && npm ci` from the repository root in separate shells.
3. Make a focused change. Include regression coverage for behavior changes and update
   the relevant [protocol documentation](docs/protocol/) when contracts change.
4. Run the checks below and include their results in your pull request. Explain the
   problem, the resulting behavior, and any remaining limitations.

```bash
# backend/
poetry check --lock
poetry run pytest -q -ra
poetry build

# dashboard/
npm run lint -- --max-warnings=0
npm run test
npm run build
```

Backend tests spawn an isolated PostgreSQL cluster via `pg_ctl`; they do not use your
application database. Set `PGCTL` if the binary is outside PATH. Docker tests need a
running daemon and `vibony/sandbox-py:3.12` built using [sandbox/README.md](sandbox/README.md).
Keep async backend tests on the shared session loop. Mock model calls in tests; normal
CI must not require provider keys or paid evaluations.

## Tasks and evaluation

`spec.yaml` is the task contract; `task.md` is its candidate-facing projection. Follow
[the task format](docs/task-design/format.md) and [weakness format](docs/weakness-format.md).
Preserve intentional flaws. Specs, weakness instructions, interviewer notes and
reference solutions may be public source, but must never enter candidate bundles.

The evaluation harness lives in `backend/scripts/assessment_eval`. Its assessment
seed corpus is authored synthetic data. Real session transcripts are not distributed
as trap-calibration fixtures. Supply only consented, reviewed local data to `trap_corpus`;
`--freeze` requires an explicit `--labels` file. Running evaluation commands invokes
paid models. Agree on a small budget before any live run and report technical failures
separately from model behavior. Software tests and demo media are not quality benchmarks.

## Dependency checks

Run `npm audit` in `dashboard/`. For the backend, export the locked production graph
with `poetry export --only main -f requirements.txt --without-hashes -o /tmp/vibony-requirements.txt`
and run `pip-audit -r /tmp/vibony-requirements.txt` using a separate audit environment.
Dependency audits and Docker integration tests are manual checks; the minimal CI
runs non-Docker backend tests, package build, dashboard lint, tests and build. Review
updates and run the application checks after changing either lockfile.

Monaco 0.57.0 pins DOMPurify 3.4.15. The scoped npm override selects 3.4.16 to address
[the IN_PLACE advisory](https://github.com/advisories/GHSA-p98j-92pf-mc4p). Remove it when
Monaco's own dependency is fixed, after checking the editor and build.
