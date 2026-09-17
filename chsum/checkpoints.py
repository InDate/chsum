"""The git checkpoints: one commit per tool call that changed the tree.

Each call that writes commits under `refs/chsum/<session>`, chained onto the
last. `HEAD`, the index and the working tree are never written — `commit-tree`
builds the object and `update-ref` publishes it — so nothing here can leave a
checkpoint as the branch tip and the user's commit hooks never fire.

A ref is a gc root, so a chain outlives the reflog expiring, a `git gc`, and the
removal of the worktree it was written in. It is also never pruned, which is why
retention is asked for through `chsum checkpoints --prune`.

Checkpoints written before chains exist sit in `HEAD`'s reflog instead; `shas`
reads both and merges them, so a session spanning the change keeps its whole
history.

Nothing here reads a transcript or renders anything: `core` holds the views that
read these, and the commands that call them.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

GATE_NAME = "chsum-checkpoint"  # "enabled" / "declined" / absent (never asked)
# The throwaway index the staging steps write, named per worktree by
# `--git-path`. The user's own index is never in the sequence.
_INDEX_NAME = "chsum-index"
# `update-ref`'s spelling for "this ref must not exist yet".
_EMPTY_SHA = "0" * 40
STATE_NAME = "chsum-checkpoint-state"
LOCK_NAME = "chsum-checkpoint.lock"
_LOCK_WAIT = 20.0
_LOCK_POLL = 0.05
_CHECKPOINT_PREFIX = "chsum-checkpoint: "
_HOOK_GIT_TIMEOUT = 30  # seconds per git call — this must never be what hangs a turn
_CHECKPOINT_RE = re.compile("^" + re.escape(_CHECKPOINT_PREFIX)
                            + r"(\S+) @ (\S+)(?: (\S+))?$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# `core` installs its tracer here at import. Left None, every call below runs
# untraced rather than reaching back into a module that imports this one.
TRACER = None


def set_tracer(tracer) -> None:
    """The debug tracer `--debug` prints. One direction only: `core` imports
    this module, and this module never imports `core`."""
    global TRACER
    TRACER = tracer


def _git(args: list[str], cwd: pathlib.Path,
         env: dict | None = None) -> subprocess.CompletedProcess:
    """One git call. `env` carries `GIT_INDEX_FILE` for the staging steps, which
    is what keeps the user's index out of the checkpoint sequence."""
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=_HOOK_GIT_TIMEOUT, env=env)


def _git_dir(cwd: pathlib.Path) -> pathlib.Path | None:
    """The real `.git` directory for `cwd` — a worktree's `.git` is a file
    naming it elsewhere. `None` where `cwd` sits outside a repo, or where `git`
    is unreachable."""
    try:
        proc = _git(["git", "rev-parse", "--git-dir"], cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    p = pathlib.Path(proc.stdout.strip())
    return p if p.is_absolute() else (cwd / p).resolve()


def _enabled(git_dir: pathlib.Path) -> bool:
    """Per-project opt-in: true only where the gate file reads `enabled`. An
    absent gate and a `declined` one both read as not-enabled — the SessionStart
    path reads the file itself, where the two differ."""
    try:
        return (git_dir / GATE_NAME).read_text().strip() == "enabled"
    except OSError:
        return False


def _self_heal(cwd: pathlib.Path, git_dir: pathlib.Path) -> None:
    """Resets away a checkpoint commit left as the branch tip by a chsum that
    moved `HEAD` — the mechanism before checkpoints chained under a ref, which
    committed and then reset. Nothing written now can leave that state, since
    `commit-tree` never touches `HEAD`; this stays as the repair path for a
    repo that ran the old sequence and was killed inside it, and for the state
    file such a run leaves behind."""
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
        head = _git(["git", "log", "-1", "--format=%B"], cwd)
        if head.returncode == 0 and head.stdout.startswith(_CHECKPOINT_PREFIX):
            _git(["git", "reset", "--soft", before], cwd)
            _git(["git", "read-tree", index_tree], cwd)
        # Any other HEAD carries a real commit, or a reset that already landed:
        # git's current state stands.
    except (OSError, subprocess.SubprocessError):
        pass
    state.unlink(missing_ok=True)


def _hold_checkpoint_lock(git_dir: pathlib.Path) -> tuple[bool, int | None]:
    """(proceed, fd) for an exclusive advisory lock on the checkpoint sequence.

    Parallel subagents share one working tree and one `HEAD`, and each hook is
    its own process. Two sequences overlapping means the second reads `HEAD` as
    the first's checkpoint, stores it as its baseline, and resets to it, leaving
    that commit as the branch tip. One holder at a time removes that read.

    The kernel releases the lock when the process exits, including a kill, so
    nothing on disk goes stale. `os.close` on the returned fd releases it.

    (True, fd) where it is held here. (True, None) where `fcntl` is absent,
    which runs the sequence unserialised. (False, None) where another process
    holds it past `_LOCK_WAIT`, which writes no checkpoint: the files land in
    the next checkpoint's diff, under a later row."""
    if fcntl is None:
        return True, None
    try:
        fd = os.open(str(git_dir / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return True, None
    deadline = time.monotonic() + _LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True, fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return False, None
            time.sleep(_LOCK_POLL)


def checkpoint_ref(session_id: str) -> str:
    """The ref a session's checkpoints chain under. Shared across worktrees,
    since `refs/` lives in the common git dir — a checkpoint written in a linked
    worktree outlives that worktree's removal, where `HEAD`'s reflog does not."""
    return f"refs/chsum/{session_id}"


def _ref_tip(cwd: pathlib.Path, ref: str) -> str:
    """The commit a ref points at, or "" where it does not exist."""
    try:
        proc = _git(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _tree_of(cwd: pathlib.Path, commit: str) -> str:
    """The tree a commit holds, or "" where the commit is unreadable."""
    try:
        proc = _git(["git", "rev-parse", f"{commit}^{{tree}}"], cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _write_checkpoint(cwd: pathlib.Path, git_dir: pathlib.Path, session_id: str,
                      ref: str = "") -> None:
    """One checkpoint per tool call that changed the tree, chained under this
    session's ref. `ref` is the `tool_use` id of the call that caused it, "" where
    the payload carries none.

    `HEAD` is never written. The commit object is built by `commit-tree` from a
    tree and a parent, and only `update-ref` publishes it, so no step of this
    leaves the branch tip on a checkpoint and none of it fires the user's commit
    hooks. The index is a throwaway file named by `--git-path`, per worktree, so
    the user's staging area is never in the sequence.

    Each checkpoint is parented on the last one, which makes `git show` the
    call's own diff and puts the order in the commit graph. `_enabled` is checked
    here rather than in the hook, so a project that has since declined still
    takes every early return cleanly."""
    if not _enabled(git_dir):
        return
    try:
        status = _git(["git", "status", "--porcelain"], cwd)
    except (OSError, subprocess.SubprocessError):
        return
    if status.returncode != 0 or not status.stdout.strip():
        return  # the tree matches HEAD — nothing to checkpoint

    try:
        index = _git(["git", "rev-parse", "--git-path", _INDEX_NAME], cwd)
    except (OSError, subprocess.SubprocessError):
        return
    if index.returncode != 0:
        return
    # Absolute, so the environment holds one path whatever `cwd` git resolves to.
    index_path = pathlib.Path(index.stdout.strip())
    if not index_path.is_absolute():
        index_path = (cwd / index_path).resolve()
    env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}

    try:
        staged = _git(["git", "add", "-A"], cwd, env=env)
        if staged.returncode != 0:
            return
        tree = _git(["git", "write-tree"], cwd, env=env)
    except (OSError, subprocess.SubprocessError):
        return
    finally:
        # The throwaway index is rebuilt from scratch on the next call, so a
        # stale one costs nothing; leaving it does not touch the user's.
        pass
    if tree.returncode != 0:
        return
    tree_sha = tree.stdout.strip()

    session_ref = checkpoint_ref(session_id)
    tip = _ref_tip(cwd, session_ref)
    # A call that wrote nothing leaves the tree equal to the last checkpoint's:
    # the working tree differs from `HEAD` for the whole session, so the status
    # check above passes on every call and a `Read` would otherwise chain a copy.
    if tip and _tree_of(cwd, tip) == tree_sha:
        return
    parent = tip or _head_sha(cwd)
    if not parent:
        return  # no commits yet — nothing to parent the first checkpoint on

    now = datetime.now(timezone.utc)
    # Millisecond precision, matching Claude Code's transcript timestamps: every
    # timestamp comparison here is a string compare, which sorts a whole-second
    # stamp after a same-second one carrying ".mmm".
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    message = f"{_CHECKPOINT_PREFIX}{session_id} @ {ts}"
    if ref:
        # The `tool_use` id of the call that wrote this. chsum addresses every
        # action as `<session>:<line>`, and `_transcript_row` turns this id into
        # that address at read time — the transcript record holding the call is
        # flushed after the hook fires, so the row does not exist yet here.
        message += f" {ref}"

    try:
        made = _git(["git", "commit-tree", tree_sha, "-p", parent, "-m", message], cwd)
    except (OSError, subprocess.SubprocessError):
        return
    if made.returncode != 0:
        return
    commit = made.stdout.strip()

    # Compare-and-swap on the tip: parallel subagents share one worktree, and two
    # hooks reading the same tip would otherwise fork the chain. The loser sees
    # its expected old value fail and takes the retry, which re-reads the tip and
    # chains onto the winner.
    for _ in range(2):
        try:
            done = _git(["git", "update-ref", session_ref, commit,
                         tip or _EMPTY_SHA], cwd)
        except (OSError, subprocess.SubprocessError):
            return
        if done.returncode == 0:
            return
        tip = _ref_tip(cwd, session_ref)
        if not tip or _tree_of(cwd, tip) == tree_sha:
            return  # the winner wrote this same tree; nothing left to record
        try:
            again = _git(["git", "commit-tree", tree_sha, "-p", tip, "-m", message], cwd)
        except (OSError, subprocess.SubprocessError):
            return
        if again.returncode != 0:
            return
        commit = again.stdout.strip()


def _head_sha(cwd: pathlib.Path) -> str:
    """`HEAD`'s commit, or "" in a repo with no commits yet."""
    try:
        proc = _git(["git", "rev-parse", "HEAD"], cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def reflog_sessions(project_dir: pathlib.Path) -> dict[str, int]:
    """`session uuid -> checkpoints` for every session recorded in `HEAD`'s
    reflog. These are the checkpoints written before chains existed: still read,
    still unreachable, and still lost to a `gc` or to the removal of the
    worktree they were written in."""
    try:
        proc = subprocess.run(
            ["git", "reflog", "show", "HEAD", "--format=%gs"],
            cwd=project_dir, capture_output=True, text=True,
            timeout=_HOOK_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    found: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        _, sep, subject = line.partition(": ")
        if not sep:
            continue
        m = _CHECKPOINT_RE.match(subject)
        if m:
            found[m.group(1)] = found.get(m.group(1), 0) + 1
    return found


def _checkpoint_refs(project_dir: pathlib.Path) -> list[tuple[str, str, int]]:
    """`(session uuid, tip sha, checkpoints)` per chained session in a repo,
    newest activity first. `[]` outside a repo or where none were written."""
    try:
        proc = subprocess.run(
            ["git", "for-each-ref", "refs/chsum/", "--format=%(refname) %(objectname)"],
            cwd=project_dir, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        ref, _, sha = line.partition(" ")
        uuid = ref.removeprefix("refs/chsum/")
        if uuid and sha:
            out.append((uuid, sha, len(_chain_shas(project_dir, uuid))))
    return out


def _commit_subject(project_dir: pathlib.Path, sha: str) -> str:
    """A commit's subject line, or "" where the commit is gone."""
    try:
        proc = _git(["git", "log", "-1", "--format=%s", sha], project_dir)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _base_of(project_dir: pathlib.Path, sha: str) -> str:
    """The commit a checkpoint was parented on, which is the session's base."""
    try:
        proc = _git(["git", "log", "-1", "--format=%P", sha], project_dir)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.split()[0] if proc.returncode == 0 and proc.stdout.split() else ""


def _migrate_chain(project_dir: pathlib.Path, uuid: str,
                   marks: list[tuple[str, str, str]],
                   on_progress=None) -> tuple[int, int, str]:
    """Rebuild a session's checkpoints as a chain under its ref. Returns
    `(written, skipped, note)`.

    A commit cannot be re-parented — the parent is part of what its sha hashes —
    so each checkpoint is rebuilt from the tree and message it already carries.
    The trees are the originals, so the content is identical and only the shas
    change; chsum resolves a checkpoint by the stamp in its message, which the
    rebuild copies verbatim.

    The ref is moved with its expected old value, so a hook writing to the same
    session while this runs fails the swap rather than losing its checkpoint.

    `on_progress(done, total)` is called per checkpoint. A session of several
    hundred is one `commit-tree` each, and the caller is what narrates it — this
    module writes to no stream of its own."""
    tip_before = _ref_tip(project_dir, checkpoint_ref(uuid))
    parent = ""
    written = skipped = 0
    for _when, sha, _call in marks:
        tree = _tree_of(project_dir, sha)
        subject = _commit_subject(project_dir, sha)
        if not tree or not subject:
            skipped += 1  # the commit was pruned before this ran
            continue
        if not parent:
            parent = _base_of(project_dir, sha) or _head_sha(project_dir)
        if not parent:
            return 0, len(marks), "the repo has no commit to parent the chain on"
        try:
            made = _git(["git", "commit-tree", tree, "-p", parent, "-m", subject],
                        project_dir)
        except (OSError, subprocess.SubprocessError):
            return written, skipped + 1, "git could not write a commit"
        if made.returncode != 0:
            return written, skipped + 1, made.stderr.strip()[:80]
        parent = made.stdout.strip()
        written += 1
        if on_progress is not None:
            on_progress(written, len(marks))
    if not written:
        return 0, skipped, "nothing readable left to rebuild"
    try:
        done = _git(["git", "update-ref", checkpoint_ref(uuid), parent,
                     tip_before or _EMPTY_SHA], project_dir)
    except (OSError, subprocess.SubprocessError):
        return 0, skipped, "git could not move the ref"
    if done.returncode != 0:
        return 0, skipped, "the session wrote a checkpoint while this ran; run it again"
    return written, skipped, ""


def _chain_shas(project_dir: pathlib.Path,
                session_uuid: str) -> list[tuple[str, str, str]]:
    """The session's checkpoints off its own ref, oldest first, in the shape
    `_checkpoint_shas` returns. `[]` where the session wrote no chain, which is
    every session recorded before the ref existed — those are read from the
    reflog instead.

    The stamp is parsed from the commit message rather than taken from `%cI`:
    chsum's own millisecond timestamp is what sorts against transcript rows, and
    a commit date would change that ordering underneath the comparisons."""
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%H %s", checkpoint_ref(session_uuid)],
            cwd=project_dir, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        m = _CHECKPOINT_RE.match(subject)
        if m and m.group(1) == session_uuid:
            out.append((m.group(2), sha, m.group(3) or ""))
    out.reverse()
    TRACER and TRACER.step("_chain_shas", repo=str(project_dir), session=session_uuid[:8],
               checkpoints=len(out))
    return out


def _checkpoint_shas(project_dir: pathlib.Path | None,
                     session_uuid: str) -> list[tuple[str, str, str]]:
    """(timestamp, sha, ref) per checkpoint the hooks committed-then-reset-away
    for this session, oldest first (the raw reflog is newest-first). `ref` is the
    `tool_use` id of the call that wrote it, which `_transcript_row` resolves to
    a `<session>:<line>`, and "" where the payload carried no id. Never raises: no repo, no `git`, no hook installed, or
    a reflog that's already aged the entries out all degrade
    to `[]`, same as a session that never had checkpoints at all — this must
    be exactly as forgiving as the rest of this file's summariser-failure
    handling, even though no model call is involved."""
    if not project_dir:
        return []
    chained = _chain_shas(project_dir, session_uuid)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["git", "reflog", "show", "HEAD", "--format=%H %gs"],
            cwd=project_dir, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        TRACER and TRACER.step("_checkpoint_shas", repo=str(project_dir), git=type(e).__name__,
                   checkpoints=0, fallback="_files_touched")
        return []
    TRACER and TRACER.proc(["git", "-C", str(project_dir), "reflog", "show", "HEAD"],
               proc.returncode, time.monotonic() - started,
               f"{len(proc.stdout.splitlines())} reflog entries")
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        sha, _, rest = line.partition(" ")
        if not sha:
            continue
        # `%gs` is the reflog *action* plus the commit subject — "commit: …",
        # "commit (initial): …" — not the subject alone; only the part after
        # the first ": " is ever the message the hook actually wrote.
        _, sep, subject = rest.partition(": ")
        if not sep:
            continue
        m = _CHECKPOINT_RE.match(subject)
        if m and m.group(1) == session_uuid:
            out.append((m.group(2), sha, m.group(3) or ""))
    out.reverse()
    # Both sources, merged: a session running across the change from reflog to
    # chain holds some of each, and preferring one would drop the other half of
    # its own history.
    #
    # Identity is the stamp, not the sha. A migrated checkpoint carries the same
    # tree and message under a new sha — a parent is part of what a sha hashes —
    # so a sha-keyed merge counts it twice and every count downstream doubles.
    # The chain's copy wins, being the durable one. Ordered by chsum's own
    # stamp, which is what every comparison downstream sorts on.
    if chained:
        held = {(when, call) for when, _sha, call in chained}
        out = chained + [m for m in out if (m[0], m[2]) not in held]
        out.sort(key=lambda m: m[0])
    TRACER and TRACER.step("_checkpoint_shas", repo=str(project_dir), session=session_uuid[:8],
               checkpoints=len(out), chained=len(chained),
               span=f"{out[0][0]}→{out[-1][0]}" if out else "—")
    return out


def _checkpoint_numstat(project_dir: pathlib.Path | None, prev_ref: str,
                        cur_ref: str) -> list[tuple[int, int, str]]:
    """`(added, removed, path)` per file changed between two checkpoints, from
    `git diff --numstat`. A binary file reports `-` for both counts and comes
    back as `(0, 0, path)`. Never raises: no repo or bad refs give `[]`."""
    if not project_dir:
        return []
    try:
        proc = subprocess.run(["git", "diff", "--numstat", prev_ref, cur_ref],
                              cwd=project_dir, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        out.append((int(added) if added.isdigit() else 0,
                    int(removed) if removed.isdigit() else 0, path))
    return out


def _checkpoint_diff_files(project_dir: pathlib.Path | None, prev_ref: str, cur_ref: str) -> list[str]:
    """Same bullet shape `_files_touched` produces — `- \\`path\\`:ranges` — but
    sourced from a real `git diff` between two checkpoint commits instead of
    the transcript, so it sees every change regardless of how it was made (a
    raw `sed -i`, not just Edit/Write/MultiEdit) and is never stale (no line
    numbers frozen at the moment of an earlier edit in the same turn). Parses
    `--- a/`/`+++ b/` for the path and `@@ -a,b +c,d @@` hunks for the new-side
    range (`c` to `c+d-1`); `d == 0` is a pure deletion at the new side, which
    would invent a range that doesn't exist, so a file left with no real range
    prints `(deleted)` instead. Never raises: not a repo, bad refs, or an
    unparseable diff all degrade to `[]`."""
    if not project_dir:
        return []
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["git", "diff", "-U0", prev_ref, cur_ref],
            cwd=project_dir, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    TRACER and TRACER.proc(["git", "-C", str(project_dir), "diff", "-U0", prev_ref, cur_ref],
               proc.returncode, time.monotonic() - started, f"{len(proc.stdout)} chars")
    if proc.returncode != 0:
        return []
    by_path: dict[str, list[str]] = {}
    order: list[str] = []
    pure_deletion: set[str] = set()
    path = None
    pending_a = None
    for line in proc.stdout.splitlines():
        if line.startswith("--- "):
            a = line[4:]
            pending_a = None if a == "/dev/null" else a[2:]  # strip "a/"
        elif line.startswith("+++ "):
            b = line[4:]
            path = pending_a if b == "/dev/null" else b[2:]  # strip "b/"
            if path and path not in by_path:
                by_path[path] = []
                order.append(path)
        elif line.startswith("@@ ") and path:
            m = _HUNK_RE.match(line)
            if not m:
                continue
            new_start = int(m.group(1))
            new_lines = int(m.group(2)) if m.group(2) is not None else 1
            if new_lines == 0:
                pure_deletion.add(path)
                continue
            rng = f"{new_start}-{new_start + new_lines - 1}"
            if rng not in by_path[path]:
                by_path[path].append(rng)
    out = []
    for p in order:
        ranges = by_path[p]
        if ranges:
            out.append(f"- `{p}`:{', '.join(ranges)}")
        elif p in pure_deletion:
            out.append(f"- `{p}` (deleted)")
        else:
            out.append(f"- `{p}`")
    return out
