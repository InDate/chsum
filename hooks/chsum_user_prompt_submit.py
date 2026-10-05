#!/usr/bin/env python3
"""UserPromptSubmit: holds back one prompt to a cold cache, run out of the plugin's own directory.

The `chsum` package sits beside this file in the plugin, so the import reaches
the plugin's own copy and needs nothing on PATH. One installed tree runs both
the hooks and the CLI.

One JSON payload arrives on stdin. Exit 2 is the code that blocks a turn, so
every path here exits 0.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import chsum
except Exception as e:  # noqa: BLE001 — an unimportable module stops the hook, not the turn
    print(f"chsum user_prompt_submit hook: {e}", file=sys.stderr)
    raise SystemExit(0)

raise SystemExit(chsum._hook_user_prompt_submit(chsum._read_payload()))
