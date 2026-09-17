"""Claude Code sessions, under `~/.claude/projects`.

Claude Code writes the common shape already, so this source carries no
translation: its files are read where they lie, and their line numbers are the
ones a drill-down `sed` command names.

The two primitives here — where the tree sits, and the directory a session ran
in — are read by `core` as well, so they live at this layer rather than in a
second copy.
"""
from __future__ import annotations

import json
import os
import pathlib

from . import IN_PLACE, Source, register

PROJECTS_ROOT = pathlib.Path(
    os.environ.get("CLAUDE_CONFIG_DIR", pathlib.Path.home() / ".claude")
) / "projects"


def transcript_cwd(path: pathlib.Path) -> str:
    """The directory a transcript's session ran in, from the first record
    carrying `cwd`. The scan stops at 50 records so a transcript carrying none
    costs a head, not a whole read."""
    try:
        with path.open(errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 50:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return ""


def discover() -> list[pathlib.Path]:
    """Addressable conversations only: two levels, no agent-* sidecars
    (mirrors claude-history's discover_agent_keys, service.rs:477-486)."""
    if not PROJECTS_ROOT.is_dir():
        return []
    return sorted(p for p in PROJECTS_ROOT.glob("*/*.jsonl")
                  if not p.name.startswith("agent-"))


register(Source(name=IN_PLACE, label="claude", discover=discover,
                cwd_of=transcript_cwd, agent="Claude"))
