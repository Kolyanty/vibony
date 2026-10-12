#!/usr/bin/env python3
"""Smoke-check that VIBONY_DEFAULT_MODEL + provider key in backend/.env actually work.

Goes through the exact same layer the backend uses (litellm), so a green run here
means proxy.py will work too. Two probes:

  1. plain chat completion      — connectivity, key, model slug are valid
  2. tool / function calling    — the critical path for the IDE agent (agent_host).
                                  Cheap/fast models often pass (1) but fail (2).

Run via the backend poetry env (litellm lives there):

  poetry --directory backend run python scripts/llm-check.py

Override the model without touching .env:

  poetry --directory backend run python scripts/llm-check.py --model openrouter/deepseek/deepseek-v4-flash

litellm reads the provider key from the matching env var itself (OPENROUTER_API_KEY,
DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, ...). We only load .env and
sanity-check that *some* relevant key is present before burning a request.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / "backend" / ".env"

# Map litellm provider prefix -> env var litellm expects the key in.
PROVIDER_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _expected_key_env(model: str) -> str | None:
    provider = model.split("/", 1)[0]
    return PROVIDER_KEY_ENV.get(provider)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="override VIBONY_DEFAULT_MODEL for this run")
    parser.add_argument("--skip-tools", action="store_true", help="run only the plain chat probe")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
    except ImportError:
        print("ERROR: python-dotenv not found. Run inside the backend env:", file=sys.stderr)
        print("  poetry --directory backend run python scripts/llm-check.py", file=sys.stderr)
        return 2

    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
        print(f"loaded env: {ENV_PATH}")
    else:
        print(f"WARN: {ENV_PATH} not found — relying on ambient env vars")

    model = args.model or os.environ.get("VIBONY_DEFAULT_MODEL", "")
    if not model:
        print("ERROR: model not set. Set VIBONY_DEFAULT_MODEL in backend/.env or pass --model.", file=sys.stderr)
        return 2

    key_env = _expected_key_env(model)
    print(f"model:    {model}")
    if key_env:
        key_val = os.environ.get(key_env, "")
        print(f"key env:  {key_env} = {_mask(key_val)}")
        if not key_val:
            print(
                f"ERROR: {key_env} is empty but model '{model}' needs it. Fill it in backend/.env.",
                file=sys.stderr,
            )
            return 2
    else:
        provider = model.split("/", 1)[0]
        print(f"key env:  (unknown provider '{provider}' — letting litellm resolve the key itself)")

    import litellm

    # --- Probe 1: plain chat completion ---------------------------------------
    print("\n[1/2] plain chat completion ...")
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: VIBONY_OK"}],
            max_tokens=20,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 — surface any provider/auth error verbatim
        print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    content = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    print(f"  OK    reply={content!r}")
    if usage:
        print(f"        usage={usage.model_dump() if hasattr(usage, 'model_dump') else dict(usage)}")

    if args.skip_tools:
        print("\nDONE (tool probe skipped).")
        return 0

    # --- Probe 2: tool / function calling -------------------------------------
    print("\n[2/2] tool calling (agent critical path) ...")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string", "description": "City name"}},
                    "required": ["city"],
                },
            },
        }
    ]
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "What's the weather in Tbilisi? Use the tool."}],
            tools=tools,
            tool_choice="auto",
            max_tokens=200,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  NOTE: model reachable but tool calling is broken — bad fit for the IDE agent.", file=sys.stderr)
        return 1

    tool_calls = getattr(resp.choices[0].message, "tool_calls", None)
    if not tool_calls:
        print("  WARN: model did NOT emit a tool call (returned plain text instead).", file=sys.stderr)
        print(f"        content={resp.choices[0].message.content!r}", file=sys.stderr)
        print("        Tool calling may be unreliable on this model — verify before using as default.", file=sys.stderr)
        return 1

    tc = tool_calls[0]
    fn = tc.function
    print(f"  OK    tool_call name={fn.name!r} args={fn.arguments!r}")
    print("\nDONE — model works for both plain chat and tool calling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
