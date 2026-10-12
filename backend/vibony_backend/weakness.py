"""Weakness prompt for the AI agent.

Format source of truth — [docs/weakness-format.md](../../docs/weakness-format.md).
Merges configs/global.yaml + tasks/<task_id>/weakness.yaml via deep_merge
and renders it into a text system prompt.

Two usage modes:
- API: `render_weakness(task_id)` — for the backend (chat endpoint).
- CLI: `python -m vibony_backend.weakness <path-to-weakness.yaml>` — for
  ad-hoc debugging.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GLOBAL_CONFIG_PATH = REPO_ROOT / "configs" / "global.yaml"


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Not found: {path}")
    parsed = yaml.safe_load(path.read_text())
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: expected a YAML object at the top level")
    return parsed


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge: override on top of base.

    Dicts are merged recursively. Lists and scalars are replaced, not concatenated.
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def render(config: dict) -> str:
    prompt_cfg = config.get("prompt") or {}
    weakness = config.get("weakness") or {}

    preamble = (prompt_cfg.get("preamble") or "").rstrip()
    finale = (prompt_cfg.get("finale") or "").rstrip()
    confidence_hints = prompt_cfg.get("confidence_hints") or {}
    depth_hints = prompt_cfg.get("depth_hints") or {}

    if not preamble:
        raise ValueError("merged config is missing prompt.preamble")

    parts: list[str] = [preamble]

    blindspots = weakness.get("blindspots") or []
    if blindspots:
        parts.append(
            "\n\n## Blind spots\n\n"
            "These are problems you do NOT raise on your own initiative. "
            "If the candidate explicitly points out a specific problem, "
            "agree and help fix it. The value is in what the candidate "
            "noticed, not in you arguing."
        )
        for i, bs in enumerate(blindspots, 1):
            block = [f"\n### Blind spot {i}"]
            block.append(f"\n**Problem:** {(bs.get('issue') or '').strip()}")
            if bs.get("if_asked_vaguely"):
                block.append(
                    "\n**If the candidate asks vaguely** (e.g. \"what in the code "
                    f"could be improved?\", \"are there any problems?\"): {bs['if_asked_vaguely'].strip()}"
                )
            if bs.get("if_asked_directly"):
                block.append(
                    "\n**If the candidate points directly at this specific problem:** "
                    f"{bs['if_asked_directly'].strip()}"
                )
            parts.append("\n".join(block))

    patterns = weakness.get("patterns") or []
    if patterns:
        parts.append(
            "\n\n## Behavior patterns\n\n"
            "Specific triggers and the actions to take in response."
        )
        for p in patterns:
            block = [
                f"\n**When:** {(p.get('trigger') or '').strip()}",
                f"**Act like this:** {(p.get('action') or '').strip()}",
            ]
            parts.append("\n".join(block))

    code_habits = weakness.get("code_habits") or []
    if code_habits:
        parts.append(
            "\n\n## Coding habits\n\n"
            "When you write or edit code, follow these habits:"
        )
        parts.extend(f"- {h.strip()}" for h in code_habits)

    style = weakness.get("style") or {}
    if style:
        style_lines = ["\n\n## Response style"]
        conf = style.get("confidence")
        if conf:
            hint = confidence_hints.get(conf, "")
            style_lines.append(
                f"- Confidence level: **{conf}**" + (f" — {hint}" if hint else "")
            )
        depth = style.get("explanation_depth")
        if depth:
            hint = depth_hints.get(depth, "")
            style_lines.append(
                f"- Explanation depth: **{depth}**" + (f" — {hint}" if hint else "")
            )
        parts.append("\n".join(style_lines))

    if finale:
        parts.append(f"\n\n---\n\n{finale}")

    return "\n".join(parts).rstrip() + "\n"


def render_from_paths(global_path: Path, task_path: Path) -> str:
    """Render the prompt from two paths (for CLI and ad-hoc calls)."""
    base = load_yaml(global_path)
    task = load_yaml(task_path)
    return render(deep_merge(base, task))


def render_weakness(task_id: str) -> str:
    """Render the AI agent's system prompt for a specific task.

    API-friendly wrapper: resolves task_id → tasks/<task_id>/weakness.yaml.
    Legacy filesystem path: used by `/v1/chat` and tests; the runtime renders
    from the DB via `render_weakness_dict`.
    """
    # task_id is a flat slug. Reject path separators / traversal / dotfiles
    # so a caller can't escape tasks/ and read an arbitrary weakness.yaml.
    if (
        not task_id
        or "/" in task_id
        or "\\" in task_id
        or ".." in task_id
        or task_id.startswith(".")
    ):
        raise FileNotFoundError(f"weakness.yaml not found for task_id={task_id!r}: invalid task_id")
    task_path = REPO_ROOT / "tasks" / task_id / "weakness.yaml"
    if not task_path.exists():
        raise FileNotFoundError(f"weakness.yaml not found for task_id={task_id}: {task_path}")
    return render_from_paths(GLOBAL_CONFIG_PATH, task_path)


def render_weakness_dict(weakness: dict | None) -> str:
    """Render the system prompt from an already-parsed weakness dict.

    DB source: `weakness` is the task's internal `weakness:` mapping
    (blindspots/patterns/code_habits/style), merged on top of configs/global.yaml
    and rendered by the same `render()`. Empty/None weakness → prompt from global only.
    """
    base = load_yaml(GLOBAL_CONFIG_PATH)
    merged = deep_merge(base, {"weakness": weakness or {}})
    return render(merged)


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write(f"Usage: python -m vibony_backend.weakness <path-to-weakness.yaml>\n")
        return 1

    task_path = Path(sys.argv[1])
    try:
        sys.stdout.write(render_from_paths(GLOBAL_CONFIG_PATH, task_path))
    except (FileNotFoundError, yaml.YAMLError, ValueError) as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
