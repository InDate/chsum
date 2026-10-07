#!/usr/bin/env python3
"""PreToolUse and PostToolUse for `chsum observe`, run out of the plugin's own directory.

The first argument names the event: `pre` reviews a pending write to a file an
observe base covers, `post` appends a landed one to the base. With no observe
base started, both exit 0 at once.

One JSON payload arrives on stdin. Exit 2 is the code that blocks a turn, so
every path here exits 0; a deny travels in the JSON on stdout.
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

hook = observe.hook_post if sys.argv[1:2] == ["post"] else observe.hook_pre
raise SystemExit(hook(chsum._read_payload()))
