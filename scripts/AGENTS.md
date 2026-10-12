# Development helper instructions

Inherits [../AGENTS.md](../AGENTS.md). These rules apply to root `scripts/`, not
`backend/scripts/assessment_eval/`.

- Keep helpers small and standalone: stdlib, a `uv` PEP 723 script, or an existing
  component environment. Do not introduce a shared root dependency project.
- `docker-save.sh` / `docker-load.sh` transfer development images for offline use.
- `llm-check.py` probes completion and tool calls via the backend Poetry environment;
  it uses real credentials and may spend money. Do not run as an ordinary unit check.
- `claude-settings.template.json` is local, ignored configuration that may contain real
  credentials. Never publish it, even though its name says “template”.
- Weakness rendering belongs in `backend/vibony_backend/weakness.py`; the format is
  documented in [../docs/weakness-format.md](../docs/weakness-format.md).
