"""Bash permission gate for AgentHost inline-bash.

classify_bash(cmd) — pure function that returns 'allow' | 'confirm' | 'deny'.
extract_prefix(cmd) — extracts canonical executable prefix for prefix-allow.
is_prefix_allowed(cmd, prefixes) — checks all segments against prefix set.

Duplication from CLI is intentional until both runtimes stabilise.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

Verdict = Literal["allow", "confirm", "deny"]

_SAFE_ROOTS: frozenset[str] = frozenset({
    "ls", "cat", "head", "tail", "wc", "tree", "pwd", "which", "file",
    "stat", "du", "df", "readlink", "realpath", "basename", "dirname",
    "grep", "egrep", "fgrep", "find", "rg", "fd", "ag",
    "sort", "uniq", "awk", "sed", "cut", "tr", "diff", "cmp", "column",
    "python", "python3", "pytest", "mypy", "ruff", "black", "isort",
    "flake8", "pylint",
    "echo", "printf", "true", "false", ":", "exit", "test", "[",
    "sleep", "nproc", "uname", "whoami", "date", "hostname", "id",
    "tty", "type",
})

_SAFE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({
        "status", "log", "diff", "show", "blame", "branch", "ls-files",
        "ls-tree", "config", "remote", "stash", "describe", "rev-parse",
        "rev-list", "tag", "shortlog", "reflog",
    }),
    "pip": frozenset({"list", "show", "freeze", "check"}),
    "poetry": frozenset({"run", "show", "env", "lock", "check", "version"}),
    "npm": frozenset({"list", "ls", "outdated", "view"}),
    "make": frozenset({"-n", "--dry-run"}),
}

_DENY_ROOTS: frozenset[str] = frozenset({
    "sudo", "su", "doas",
    "mkfs", "mkfs.ext4", "mkfs.ext3", "mkfs.ext2",
    "mkfs.btrfs", "mkfs.xfs", "mkfs.vfat", "mkfs.fat",
    "shutdown", "reboot", "halt", "poweroff",
})

_SHELL_TARGETS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

_DANGEROUS_RM_TARGETS: frozenset[str] = frozenset({
    "/", "/*", "~", "~/", "$HOME", "${HOME}", "*",
    ".", "..", "../..", "../../..",
})

_FORK_BOMB_RE = re.compile(r":\s*\(\s*\)")

_DANGEROUS_CHMOD_MODES: frozenset[str] = frozenset({
    "777", "666", "755", "0777", "0666", "0755",
})

_CHAIN_OPERATORS: frozenset[str] = frozenset({"&&", "||", ";", "|"})

_LONE_AMPERSAND_RE = re.compile(r"(?<![&>])&(?![&>])")

_WRAPPERS_PASSTHROUGH: frozenset[str] = frozenset({"time", "nohup"})


def classify_bash(cmd: str) -> Verdict:
    if _FORK_BOMB_RE.search(cmd):
        return "deny"
    try:
        segments, operators = _split_chain(cmd)
    except ValueError:
        return "confirm"
    if not segments:
        return "allow"
    for i, op in enumerate(operators):
        if op == "|" and i + 1 < len(segments):
            target = segments[i + 1]
            if target and target[0] in _SHELL_TARGETS:
                return "deny"
    verdicts = [_classify_segment(seg) for seg in segments]
    if "deny" in verdicts:
        return "deny"
    if "confirm" in verdicts:
        return "confirm"
    return "allow"


def _split_chain(cmd: str) -> tuple[list[list[str]], list[str]]:
    cmd = _LONE_AMPERSAND_RE.sub(";", cmd)
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="&|;")
    lex.whitespace_split = True
    tokens = list(lex)
    segments: list[list[str]] = []
    operators: list[str] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _CHAIN_OPERATORS:
            segments.append(current)
            current = []
            operators.append(tok)
        else:
            current.append(tok)
    segments.append(current)
    return segments, operators


def _classify_segment(tokens: list[str]) -> Verdict:
    if not tokens:
        return "allow"
    root = tokens[0]
    args = tokens[1:]
    if root in _DENY_ROOTS:
        return "deny"
    if root == "rm":
        return _classify_rm(args)
    if root == "chmod":
        return _classify_chmod(args)
    if root == "dd":
        return _classify_dd(args)
    if root == "tee":
        return _classify_tee(args)
    if root == "env":
        i = 0
        while i < len(args) and "=" in args[i] and not args[i].startswith("="):
            i += 1
        if i == len(args):
            return "allow"
        return _classify_segment(args[i:])
    if root == "poetry" and args and args[0] == "run" and len(args) >= 2:
        return _classify_segment(args[1:])
    if root == "xargs" and args:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 1
        if i < len(args):
            return _classify_segment(args[i:])
        return "confirm"
    if root in _SAFE_SUBCOMMANDS:
        if args and args[0] in _SAFE_SUBCOMMANDS[root]:
            if root == "git" and args[0] == "branch":
                if any(a in {"-d", "-D", "--delete"} for a in args[1:]):
                    return "confirm"
            return "allow"
        return "confirm"
    if root in _SAFE_ROOTS:
        return "allow"
    return "confirm"


def _classify_rm(args: list[str]) -> Verdict:
    if not args:
        return "confirm"
    recursive = False
    force = False
    positional: list[str] = []
    for a in args:
        if a in {"-rf", "-fr", "-Rf", "-fR"}:
            recursive = True
            force = True
        elif a == "--recursive":
            recursive = True
        elif a == "--force":
            force = True
        elif a.startswith("--"):
            pass
        elif a.startswith("-"):
            for c in a[1:]:
                if c in {"r", "R"}:
                    recursive = True
                if c == "f":
                    force = True
        else:
            positional.append(a)
    if recursive and force:
        if not positional:
            return "deny"
        for target in positional:
            if target in _DANGEROUS_RM_TARGETS:
                return "deny"
            if target.startswith("/") and not target.startswith("/tmp/"):
                return "deny"
    return "confirm"


def _classify_chmod(args: list[str]) -> Verdict:
    recursive = False
    mode: str | None = None
    positional: list[str] = []
    for a in args:
        if a == "-R" or a == "--recursive":
            recursive = True
        elif a.startswith("-"):
            pass
        elif mode is None and a in _DANGEROUS_CHMOD_MODES:
            mode = a
        elif mode is None:
            mode = a
        else:
            positional.append(a)
    if recursive and mode in _DANGEROUS_CHMOD_MODES:
        for target in positional:
            if target == "/" or target.startswith("/"):
                return "deny"
    return "confirm"


def _classify_dd(args: list[str]) -> Verdict:
    for a in args:
        if a.startswith("of=/dev/"):
            return "deny"
    return "confirm"


def _classify_tee(args: list[str]) -> Verdict:
    for a in args:
        if a.startswith("/dev/") and a not in {"/dev/null", "/dev/stdout",
                                                "/dev/stderr", "/dev/tty"}:
            return "deny"
    return "allow"


def extract_prefix(cmd: str) -> str | None:
    try:
        segments, _operators = _split_chain(cmd)
    except ValueError:
        return None
    if len(segments) != 1:
        return None
    return _extract_segment_prefix(segments[0])


def _extract_segment_prefix(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    root = tokens[0]
    args = tokens[1:]
    if root == "env":
        i = 0
        while i < len(args) and "=" in args[i] and not args[i].startswith("="):
            i += 1
        if i == len(args):
            return None
        return _extract_segment_prefix(args[i:])
    if root == "poetry" and args and args[0] == "run":
        if len(args) < 2:
            return None
        tail = _extract_segment_prefix(args[1:])
        if tail is None:
            return None
        return f"poetry run {tail}"
    if root == "xargs":
        if not args:
            return None
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 1
        if i == len(args):
            return None
        tail = _extract_segment_prefix(args[i:])
        if tail is None:
            return None
        return f"xargs {tail}"
    if root == "timeout":
        if len(args) < 2:
            return None
        tail = _extract_segment_prefix(args[1:])
        if tail is None:
            return None
        return f"timeout {tail}"
    if root in _WRAPPERS_PASSTHROUGH:
        if not args:
            return None
        tail = _extract_segment_prefix(args)
        if tail is None:
            return None
        return f"{root} {tail}"
    if root in _SAFE_SUBCOMMANDS:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 1
        if i < len(args):
            return f"{root} {args[i]}"
        return root
    return root


def is_prefix_allowed(cmd: str, prefixes: set[str]) -> bool:
    try:
        segments, _operators = _split_chain(cmd)
    except ValueError:
        return False
    if not segments or not any(seg for seg in segments):
        return False
    for seg in segments:
        if not seg:
            continue
        if _classify_segment(seg) == "allow":
            continue
        prefix = _extract_segment_prefix(seg)
        if prefix is None or prefix not in prefixes:
            return False
    return True


__all__ = ["Verdict", "classify_bash", "extract_prefix", "is_prefix_allowed"]
