#!/usr/bin/env python3
"""Claude Code SessionStart hook: nudges Claude, via `additionalContext`, to
ask the user about per-turn checkpointing — once per project, the moment a
session opens rather than waiting for `chsum recap` to be used. See
chsum's CLAUDE.md ("Checkpoints") for the gate file this reads and why the
ask moved here from prose in the chsum skill.

Fires once per session (`SessionStart` — on startup, resume, clear, compact,
or fork), before the first user turn. Never raises out to Claude Code and
never exits non-zero: nothing here should ever block a session starting.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from _common import GATE_NAME, _git_dir, _read_payload

_NUDGE = (
    "chsum: per-turn git checkpointing is undecided for this project "
    "({gate} absent). Ask the user once, plainly, whether to enable it — "
    "each turn gets committed then reset away into the reflog, invisible in "
    "normal git commands and reversible, so `chsum recap` can "
    "read real git diffs for file/line tracking instead of reconstructing "
    "them from the transcript. Write `enabled` or `declined` to {gate} based "
    "on their answer, then never ask again in this checkout."
)


def _context(git_dir: Path) -> str | None:
    gate = git_dir / GATE_NAME
    if gate.exists():
        return None
    return _NUDGE.format(gate=gate)


def main() -> int:
    payload = _read_payload()
    cwd_str = payload.get("cwd")
    if not isinstance(cwd_str, str) or not cwd_str:
        return 0
    cwd = Path(cwd_str)
    if not cwd.is_dir():
        return 0
    if shutil.which("git") is None:
        return 0

    try:
        git_dir = _git_dir(cwd)
        if git_dir is None:
            return 0
        context = _context(git_dir)
        if context is None:
            return 0
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }))
    except Exception as e:  # noqa: BLE001 — never let this hook fail a session
        print(f"chsum session-start hook: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
