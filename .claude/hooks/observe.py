#!/usr/bin/env python3
"""PreToolUse hook: a call that touches the observe bench's notes goes to
`chsum observe hold`. A rerun of a call the reviewer passed runs; any other
is denied with the reason hold returns.
A Write, Edit or MultiEdit whose file_path falls under NOTES, and a Bash
command naming NOTES, are held; every other call runs. A hold that fails
(no observer, an expired cache) lets the call run, with the cause on stderr.
"""
import json
import subprocess
import sys

NOTES = "bench/observe/work/notes/"


def held(payload: dict) -> bool:
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") or {}
    if tool in ("Write", "Edit", "MultiEdit"):
        return NOTES in str(inp.get("file_path") or "")
    if tool == "Bash":
        return NOTES in str(inp.get("command") or "")
    return False


def main() -> int:
    payload = json.load(sys.stdin)
    if not held(payload):
        return 0
    proc = subprocess.run(["chsum", "observe", "hold"], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print(f"observe hook: {proc.stderr.strip()}", file=sys.stderr)
        return 0
    out = json.loads(proc.stdout)
    if out["decision"] == "allow":
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": out["reason"]}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
