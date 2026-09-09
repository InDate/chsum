#!/usr/bin/env python3
"""SessionStart: the checkpoint opt-in, run out of the plugin's own directory.

Same import path as `chsum_post_tool_use.py`: the plugin's own `chsum.py`,
with nothing required on PATH.

One JSON payload arrives on stdin, and the reply Claude Code parses goes to
stdout, so every message here goes to stderr.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import chsum
except Exception as e:  # noqa: BLE001 — an unimportable module stops the hook, not the session
    print(f"chsum session-start hook: {e}", file=sys.stderr)
    raise SystemExit(0)

raise SystemExit(chsum._hook_session_start(
    chsum._read_payload(), entrypoint=str(pathlib.Path(chsum.__file__).resolve())))
