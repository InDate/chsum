"""Shared by chsum's Claude Code hooks: git-dir resolution and the per-project
checkpoint gate (`.git/chsum-checkpoint`). Split out so `chsum_checkpoint.py`
(Stop) and `chsum_session_start.py` (SessionStart) don't each resolve the gate
differently — both need the exact same answer to "has this project decided?".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GATE_NAME = "chsum-checkpoint"  # "enabled" / "declined" / absent (never asked)
_TIMEOUT = 30  # seconds per git call — this must never be what hangs a turn


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=_TIMEOUT)


def _git_dir(cwd: Path) -> Path | None:
    """Resolves to the real `.git` directory (handles worktrees, where `.git`
    is a file pointing elsewhere) — None if `cwd` isn't inside a git repo or
    `git` isn't reachable at all."""
    try:
        proc = _run(["git", "rev-parse", "--git-dir"], cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    p = Path(proc.stdout.strip())
    return p if p.is_absolute() else (cwd / p).resolve()


def _enabled(git_dir: Path) -> bool:
    """Per-project opt-in: true only once the gate file says `enabled`. A
    missing or `declined` gate both read as not-enabled — callers that need
    to distinguish "never asked" from "declined" read the gate file directly."""
    gate = git_dir / GATE_NAME
    try:
        return gate.read_text().strip() == "enabled"
    except OSError:
        return False


def _read_payload() -> dict:
    try:
        data = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not data.strip():
        return {}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
