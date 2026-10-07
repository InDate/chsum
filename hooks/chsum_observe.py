#!/usr/bin/env python3
"""PostToolUse for `chsum observe`, run out of the plugin's own directory: a
landed write to a file an observe base covers moves the base forward. With no
observe base started, it exits 0 at once.

One JSON payload arrives on stdin. Exit 2 is the code that blocks a turn, so
every path here exits 0.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import chsum
    from chsum import observe
except Exception as e:  # noqa: BLE001 — an unimportable module stops the hook, not the turn
    print(f"chsum observe hook: {e}", file=sys.stderr)
    raise SystemExit(0)

raise SystemExit(observe.hook_post(chsum._read_payload()))
