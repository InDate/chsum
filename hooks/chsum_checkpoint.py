#!/usr/bin/env python3
"""Claude Code Stop hook: commit the working tree for real, then reset the
commit away, so a per-turn snapshot lands only in `HEAD`'s own reflog —
`chsum recap` reads it back via `_checkpoint_shas`/
`_checkpoint_diff_files` in chsum.py. See chsum's CLAUDE.md ("Checkpoints")
for why commit-then-reset was chosen over `commit --amend` or a dedicated ref.

Never raises out to Claude Code and never exits non-zero: exit 2 is the one
Stop-hook code that *blocks* the turn, so any unexpected condition here is
swallowed (optionally noted on stderr) rather than surfaced as a failure.
stdin is the hook payload Claude Code sends on every Stop event — see
`_read_payload` for the fields this script actually uses.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _common import _enabled, _git_dir, _read_payload, _run

STATE_NAME = "chsum-checkpoint-state"
PREFIX = "chsum-checkpoint: "


def _self_heal(cwd: Path, git_dir: Path) -> None:
    """Recovers from a crash between `git commit` and the reset that hides it
    — on *this* invocation, possibly a different session than the one that
    crashed. A killed process or machine sleep is not something a `trap`
    catches, so this is the only place recovery can happen: the next time
    anything runs the hook in this repo."""
    state = git_dir / STATE_NAME
    if not state.exists():
        return
    try:
        raw = state.read_text().split()
        index_tree, before = raw[0], raw[1]
    except (OSError, ValueError, IndexError):
        state.unlink(missing_ok=True)
        return
    try:
        head = _run(["git", "log", "-1", "--format=%B"], cwd)
        if head.returncode == 0 and head.stdout.startswith(PREFIX):
            _run(["git", "reset", "--soft", before], cwd)
            _run(["git", "read-tree", index_tree], cwd)
        # else: something else happened since (a real commit, or the reset
        # half of a prior crash already landed but cleanup didn't) — trust
        # whatever git's real state is now, don't touch it.
    except (OSError, subprocess.SubprocessError):
        pass
    state.unlink(missing_ok=True)


def _checkpoint(cwd: Path, git_dir: Path, session_id: str) -> None:
    """The per-turn sequence itself. Every early return leaves git exactly as
    it already was — the state file is only ever written right before the
    one step (`git commit`) that changes `HEAD`, and only removed once the
    reset that undoes it has actually run. `_enabled` is checked here, not in
    `main`, so `_self_heal` still runs (and cleans up) even in a project that
    has since been declined or never asked; only *starting* new checkpoint
    work is gated."""
    if not _enabled(git_dir):
        return
    try:
        status = _run(["git", "status", "--porcelain"], cwd)
    except (OSError, subprocess.SubprocessError):
        return
    if status.returncode != 0 or not status.stdout.strip():
        return  # nothing changed this turn — nothing to checkpoint

    try:
        before = _run(["git", "rev-parse", "HEAD"], cwd)
    except (OSError, subprocess.SubprocessError):
        return
    if before.returncode != 0:
        return  # no commits yet — nothing to parent a `reset --soft` against
    before_sha = before.stdout.strip()

    try:
        tree = _run(["git", "write-tree"], cwd)
    except (OSError, subprocess.SubprocessError):
        return
    if tree.returncode != 0:
        return
    index_tree = tree.stdout.strip()

    state = git_dir / STATE_NAME
    # Written *before* the risky step: if the process dies between here and
    # the reset below, the next invocation's `_self_heal` (this session's or
    # any other's) is what cleans it up.
    state.write_text(f"{index_tree} {before_sha}")

    # Millisecond precision, matching Claude Code's own transcript timestamps
    # exactly (e.g. "2026-08-16T01:36:09.044Z") — chsum.py compares timestamps
    # as plain strings throughout, which only sorts correctly when every
    # timestamp being compared shares the same precision. A whole-second
    # stamp here (no ".mmm") would sort *after* a same-second transcript
    # timestamp that has one, since "44Z" > "44.500Z" lexicographically even
    # though 44.5s is later — backwards from chronological order.
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    message = f"{PREFIX}{session_id} @ {ts}"
    try:
        _run(["git", "add", "-A"], cwd)
        # --no-verify: this commit is never a real one — it exists only to be
        # reset away a few lines down. Running the user's own commit hooks
        # (formatters, linters) against it risks mutating the working tree
        # for a commit nobody will ever see, and buys nothing.
        commit = _run(["git", "commit", "-q", "--no-verify", "-m", message], cwd)
    except (OSError, subprocess.SubprocessError):
        state.unlink(missing_ok=True)
        return
    if commit.returncode != 0:
        # Nothing landed on HEAD (hook rejected it, race, etc.) — nothing to
        # reset away.
        state.unlink(missing_ok=True)
        return

    if os.environ.get("CHSUM_CHECKPOINT_TEST_CRASH_AFTER_COMMIT"):
        # Test-only: simulate a kill between commit and reset, so the *next*
        # invocation's self-heal can be verified against a real stray commit.
        return

    try:
        _run(["git", "reset", "--soft", before_sha], cwd)
        _run(["git", "read-tree", index_tree], cwd)
    except (OSError, subprocess.SubprocessError):
        # State file stays — the next invocation's self-heal will finish this.
        return
    state.unlink(missing_ok=True)


def main() -> int:
    payload = _read_payload()
    session_id = payload.get("session_id")
    cwd_str = payload.get("cwd")
    if not isinstance(session_id, str) or not session_id:
        return 0
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
        _self_heal(cwd, git_dir)
        _checkpoint(cwd, git_dir, session_id)
    except Exception as e:  # noqa: BLE001 — never let this hook fail the turn
        print(f"chsum checkpoint hook: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
