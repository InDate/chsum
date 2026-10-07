"""`chsum observe`: a base conversation holding the watched files under a root
directory, which every write to one of them passes through before it lands.

The base is a `claude -p` session run with no setting sources, so no hook of
any plugin runs inside it. Its system prompt is a skill directory's files; its
first turn carries every watched file, the files under the root the `--match`
regexes match. A write to a watched file is held, not written: the writing
session receives a deny naming an edit number, and a background session forked
from the base, one per edit, reviews the held write and corresponds with the
writing session by SendMessage. It writes the agreed version to the file
itself and ends with `observe close`, which reads the outcome from the file
and moves the base forward. The fork's exchange
never enters the base, so the base holds the watched files and no correction.
A change made out of band moves the base forward the same way, as a diff.

The state the base holds is a commit on `refs/chsum/observe/<name>` in a git
directory of the observer's own under the chsum data directory, with the root
as its work tree. A root inside a repository, or in none, is held the same way,
and a repository's own `.git` is never read or written. The commits are built
the way checkpoints are: a throwaway index, `commit-tree`, `update-ref`. The
watched files are added with `-f`, so a file a `.gitignore` under the root
names is held as any other is. Each advance diffs the held commit against the
files as they stand and moves the ref forward; the ref's chain is every state
the base took in.

A plain `--resume` in print mode appends to the same session id. An unsaved
request writes its cache entry at its own last message, which no later request
carries, so a review reads only the entry the base's last saved turn left.
That entry holds only where the base's first reply carries a thinking block
(see `_SAVED_ASK`).

chsum ships the mechanism only: the skill, the regexes and the model are
arguments to `start`, recorded in a state file under the chsum data directory.
With no state file covering a path, every hook here exits 0 at once.
"""
from __future__ import annotations

import dataclasses
import difflib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import textwrap
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from . import core, sessions


def _state_dir() -> pathlib.Path:
    return core.CHSUM_DIR / "observe"


# A request over a 30K-token Opus prefix returns in well under this; the hook's
# own timeout in hooks.json sits above it, so a stalled call returns an error
# here rather than the hook being killed mid-write.
_CALL_TIMEOUT = 280

# No settings file loads, so no plugin, hook, skill listing or CLAUDE.md
# reaches the base; no MCP server loads, and the env var shuts out claude.ai
# connectors, whose tools would otherwise open the request. The tool list is
# the forks' list, since it opens every request and a fork reads the base's
# cache only where its list matches; under `dontAsk` the base's own requests
# run none of them. Project auto-memory loads with no setting sources unless
# the inline settings switch it off: measured 2026-10-06, a base built in this
# repo counted the lines of its MEMORY.md. Over three Haiku probes the same
# day, a fork with these flags read the base's whole prefix on its first
# request, SendMessage reached the writing session and its reply reached the
# fork, an approved Bash command wrote in the shared checkout while an
# unapproved `touch` was refused, and an inline SessionEnd hook fired on
# `claude stop`.
_SETTINGS = {"autoMemoryEnabled": False}
_BASE_FLAGS = ["--tools", "SendMessage,Bash,Read,Write,Edit", "--setting-sources", "", "--strict-mcp-config",
               "--permission-mode", "dontAsk", "--settings", json.dumps(_SETTINGS)]
_BASE_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}

# Set by Claude Code in a session's processes, so a base call made from a
# fork's Bash or its end hook inherits them and runs inside that fork's job;
# measured 2026-10-06, `observe close` run by a fork left the base behind
# where the same advance run from a shell moved it.
_SESSION_ENV = ("CLAUDE_JOB_DIR", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SSE_PORT")


def _call_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}
    env.pop("FORCE_PROMPT_CACHING_5M", None)
    env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = "1h"
    env.update(_BASE_ENV)
    return env

# A fork still open this long after its edit was held is stopped by the next
# chsum hook, and the edit closed unapplied.
_EDIT_LIFE = 3600

# `observe close` stops the fork this long after it runs, so the fork's last
# message leaves before its session ends.
_CLOSE_DELAY = 10

# The create turn ends on this ask, and the base's cache holds on it. Measured
# 2026-10-06 on Claude Code 2.1.291 (anthropics/claude-code #99833): over 30
# create-then-resume pairs on Sonnet and Opus, a resume read the created prefix
# in each of the 11 whose created reply carried a thinking block and in none of
# the 19 whose reply carried none, which re-wrote the whole history. A one-word
# reply ("loaded", "ok", "the second word above") carried none at the default
# effort, nor with "ultrathink" or "Think, then reply ok." appended (0 of 9). A
# count over the material above carried one in 12 of 12 (Sonnet and Opus) at
# 150 to 270 output tokens, and each resume read. `--effort max` carried one
# too, at max effort on every review.
_SAVED_ASK = "Reply with how many lines the files above hold in total."

# A saved turn on this ask follows the create turn. A request caches up to its
# own last message, so the reply to `_SAVED_ASK`, thinking included, stands
# uncached until a later saved request carries it; an unsaved review or ping
# carries it and re-writes it each time. Measured 2026-10-06: a ping on a base
# whose last saved reply was a 2,683-token count wrote 2,720.
_TAIL_ASK = "Reply with one word: ok."

# An advance ends on this ask. Only the base's first reply needs a thinking
# block: measured 2026-10-06 on Opus, a review after an advance whose reply
# carried none read the whole prefix (10,597). A one-word reply left uncached
# costs each later request a few tokens to re-write, so no tail turn follows.
_ADVANCE_ASK = "Reply with one word: noted."

# A base with less than this left on its cache takes a ping from the next
# chsum hook in any session. A ping restarts the clock of every entry it
# reads, so a base is held warm while any session fires hooks within its hour.
_WARM_BELOW = 900

# A base takes no second ping within this many seconds of one starting: a
# session firing a hook per tool call would otherwise start one per call
# while the first is in flight.
_PING_GAP = 60

# The session hooks compare the watched files against the held commit at most
# this often per base; each comparison walks the watched directories, stages
# the matches into a throwaway index and writes a tree.
_DRIFT_GAP = 10


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- state ------------------------------------------------------------------

def _state_path(root: str, name: str) -> pathlib.Path:
    key = f"{root}\0{name}".encode()
    return _state_dir() / f"{_sha(key)[:16]}.json"


def _path(state: dict, suffix: str) -> pathlib.Path:
    return _state_path(state["root"], state["name"]).with_suffix(suffix)


def _states() -> list[dict]:
    out = []
    for f in sorted(_state_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("root") and d.get("name") and d.get("sid"):
            out.append(d)
    return out


def _save(state: dict) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    path = _state_path(state["root"], state["name"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _ref(state: dict) -> str:
    return f"refs/chsum/observe/{state['name']}"


# --- matching ---------------------------------------------------------------

def _matches(patterns: list[str], rel: str) -> bool:
    """A full match of any pattern against the slash-separated relative path."""
    return any(re.fullmatch(p, rel) for p in patterns)


def _walk_root(pattern: str) -> str:
    """The directory a pattern's literal opening names, '' for the root: the
    characters up to its first regex operator, cut at the last slash, with
    escapes such as `\\.` read as the character they escape. A walk from that
    directory lists every path the pattern can match, and a pattern opening on
    an operator walks the whole tree."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern) and not pattern[i + 1].isalnum():
            out.append(pattern[i + 1])
            i += 2
            continue
        if c in ".^$*+?{}[]|()\\":
            break
        out.append(c)
        i += 1
    literal = "".join(out)
    return literal[:literal.rfind("/")] if "/" in literal else ""


def _skipped(skips: list[str], rel: str) -> bool:
    """A path is skipped where a skip pattern fully matches it or any
    directory above it, so a skipped directory takes everything under it."""
    parts = rel.split("/")
    return any(_matches(skips, "/".join(parts[:i])) for i in range(1, len(parts) + 1))


def _listed(root: pathlib.Path, patterns: list[str], skips: list[str], follow: bool) -> list[str]:
    """Relative paths of the files under `root` a match pattern takes in and
    no skip pattern takes out, sorted. Each match pattern's walk opens at its
    literal directory; a directory a skip pattern matches is pruned before
    the walk enters it, and `.git` always is."""
    out = set()
    for start in sorted({_walk_root(p) for p in patterns}):
        if start and _skipped(skips, start):
            continue
        for dirpath, dirs, names in os.walk(root / start, followlinks=follow):
            here = pathlib.Path(dirpath).relative_to(root).as_posix()
            prefix = "" if here == "." else here + "/"
            dirs[:] = sorted(d for d in dirs
                             if d != ".git" and not _matches(skips, prefix + d))
            for n in names:
                rel = prefix + n
                if _matches(patterns, rel) and not _matches(skips, rel):
                    out.add(rel)
    return sorted(out)


def _covers(state: dict, path: pathlib.Path) -> str:
    """The root-relative path when a match pattern takes it in and no skip
    pattern takes it out, or ''."""
    try:
        rel = path.resolve().relative_to(state["root"]).as_posix()
    except ValueError:
        return ""
    if not _matches(state["match"], rel) or _skipped(state.get("skip") or [], rel):
        return ""
    return rel


def _state_for(path: pathlib.Path) -> tuple[dict | None, str]:
    for s in _states():
        rel = _covers(s, path)
        if rel:
            return s, rel
    return None, ""


# --- skill ------------------------------------------------------------------

def _skill_files(state: dict) -> list[str]:
    """Skill-relative paths of the files `--skill-match` matches, in sorted
    order, symlinked directories followed. Bases built from the same skill
    and patterns send the same system prompt byte for byte, and read it from
    one cache entry."""
    return _listed(pathlib.Path(state["skill"]), state["skill_match"],
                   state.get("skill_skip") or [], follow=True)


def _skill_text(state: dict) -> str:
    root = pathlib.Path(state["skill"])
    return "\n\n".join(f"=== {rel}\n{(root / rel).read_text(errors='replace')}"
                       for rel in _skill_files(state))


# --- git --------------------------------------------------------------------

class _GitError(Exception):
    pass


# Commits on an observer's ref carry this identity, so a machine with no git
# user configured builds them as one with a user does.
_IDENTITY = {"GIT_AUTHOR_NAME": "chsum observe", "GIT_AUTHOR_EMAIL": "observe@chsum",
             "GIT_COMMITTER_NAME": "chsum observe", "GIT_COMMITTER_EMAIL": "observe@chsum"}


def _git(state: dict, args: list[str], data: bytes | None = None,
         index: bool = False) -> bytes:
    """git against the observer's own git directory, the root as its work
    tree. The directory is created bare on first use."""
    gitdir = _path(state, ".git")
    if not gitdir.is_dir():
        _state_dir().mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "init", "-q", "--bare", str(gitdir)],
                           capture_output=True, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise _GitError(f"git init: {e}") from e
    env = {**os.environ, **_IDENTITY}
    if index:
        env["GIT_INDEX_FILE"] = str(_path(state, ".index"))
        env["GIT_LITERAL_PATHSPECS"] = "1"
    try:
        proc = subprocess.run(["git", f"--git-dir={gitdir}", f"--work-tree={state['root']}", *args],
                              cwd=state["root"], input=data, env=env,
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise _GitError(f"git {args[0]}: {e}") from e
    if proc.returncode:
        raise _GitError(f"git {args[0]}: {proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc.stdout


def _watched(state: dict) -> list[str]:
    return _listed(pathlib.Path(state["root"]), state["match"], state.get("skip") or [], follow=False)


def _tree(state: dict) -> str:
    """The watched files as they stand, written as a tree object. The index is
    a throwaway file of this base's own, so the user's staging area is never
    in the sequence; `-f` adds an ignored file as a tracked one."""
    _state_dir().mkdir(parents=True, exist_ok=True)
    _path(state, ".index").unlink(missing_ok=True)
    files = _watched(state)
    if files:
        _git(state, ["add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
             data="\0".join(files).encode(), index=True)
    return _git(state, ["write-tree"], index=True).decode().strip()


def _held_tree(state: dict) -> str:
    return _git(state, ["rev-parse", f"{state['held']}^{{tree}}"]).decode().strip() \
        if state.get("held") else ""


def _commit(state: dict, tree: str, edit: dict | None = None, outcome: str = "", kind: str = "") -> str:
    """A commit of `tree` on the held one. An edit's commit carries its record
    as trailers, so the observer's history holds every edit's writer, fork and
    outcome after `stop` has removed the edit files."""
    parent = ["-p", state["held"]] if state.get("held") else []
    if edit:
        subject = f"chsum observe {state['name']}: {edit.get('title') or _subject(edit)} {outcome}"
        body = (f"Edit: {edit['number']}\nRoot: {state['root']}\nFile: {edit['rel']}\nWriter: {edit['writer']}\n"
                f"Fork: {edit.get('fork') or ''}\nTitle: {edit.get('title') or ''}\nOutcome: {outcome}")
        message = f"{subject}\n\n{body}"
    else:
        message = f"chsum observe {state['name']}: " + (kind or ("start" if not parent else "change made out of band"))
    return _git(state, ["commit-tree", tree, *parent, "-m", message]).decode().strip()


def _show(state: dict, rev: str, rel: str) -> bytes | None:
    try:
        return _git(state, ["show", f"{rev}:{rel}"])
    except _GitError:
        return None


def _changes(state: dict, old: str, new: str) -> list[tuple[str, str]]:
    """(status letter, path) per watched file that differs from `old` to
    `new`, each a commit or tree; renames read as a deletion and an addition."""
    out = _git(state, ["diff", "--no-renames", "--name-status", "-z", old, new]).decode()
    parts = [p for p in out.split("\0") if p]
    return list(zip(parts[0::2], parts[1::2]))


def _block(rel: str, data: bytes) -> str:
    return f"=== {rel}  sha256 {_sha(data)[:12]}\n{data.decode(errors='replace')}\n"


def _fits(diff: str, new: bytes) -> bool:
    """A diff over half the new file's size reads worse than the file itself
    and saves too little to pay for that, so the file goes instead."""
    return bool(diff) and len(diff) <= len(new) // 2


# --- calls ------------------------------------------------------------------

def _note(state: dict, **fields) -> None:
    """Merges `fields` into the base's activity file, which `chsum observe
    line` reads on every status line refresh. A failed write costs the status
    line one refresh and never the call it describes."""
    path = _path(state, ".activity.json")
    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError):
        current = {}
    current.update(fields)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current))
        tmp.replace(path)
    except OSError:
        pass


def _activity(state: dict) -> dict:
    try:
        return json.loads(_path(state, ".activity.json").read_text())
    except (OSError, ValueError):
        return {}


def _base_version(state: dict) -> str:
    """The Claude Code version of the base's last request, which built the
    prefix its cache holds; '' before the base has a transcript."""
    hits = list(core.PROJECTS_ROOT.glob(f"*/{state['sid']}.jsonl"))
    return core._request_version(list(core._records(hits[0]))) if hits else ""


def _base_exe(state: dict) -> tuple[str, str]:
    """(binary, error) for a resume of the base: the binary of the version
    that built its prefix, since a request on another version can miss the
    whole base."""
    version = _base_version(state)
    exe = core._claude_at(version)
    if exe:
        return exe, ""
    return "", (f"Claude Code {version} built the base and is no longer installed; "
                f"`{_reload_hint(state)}` rebuilds it on the installed version"
                if version else "claude is not on PATH")


def _call(state: dict, message: str, persist: bool, create: str = "", doing: str = "") -> dict:
    """One request on the base. `create` carries the skill text on the first
    request, which records it as the system prompt every resume replays. The
    flags past the first request match it exactly: the tool list opens the
    request, and a different one misses every cached entry. Returns the reply
    JSON, or an `error` key. A resume runs on the version that built the
    base (`_base_exe`); the create runs on PATH's."""
    exe, error = (shutil.which("claude") or "", "claude is not on PATH") if create else _base_exe(state)
    if not exe:
        return {"error": error}
    cmd = [exe, "-p", "--model", state["model"], *_BASE_FLAGS, "--output-format", "json"]
    cmd += ["--session-id", state["sid"], "--system-prompt", create] if create else ["--resume", state["sid"]]
    if not persist:
        cmd.append("--no-session-persistence")
    env = _call_env()
    started = time.time()
    _note(state, now={"doing": doing, "since": started})
    reply = _run(cmd, state, message, env, persist, started)
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    _note(state, now=None, last={
        "doing": doing, "at": time.time(), "seconds": round(time.time() - started, 1),
        "read": int(usage.get("cache_read_input_tokens") or 0),
        "wrote": int(usage.get("cache_creation_input_tokens") or 0),
        "error": reply.get("error", "")})
    return reply


def _run(cmd: list[str], state: dict, message: str, env: dict, persist: bool,
         started: float) -> dict:
    try:
        proc = subprocess.run(cmd, cwd=state["root"], input=message, env=env,
                              capture_output=True, text=True, timeout=_CALL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        reply = json.loads(proc.stdout)
    except ValueError:
        reply = {}
    if not isinstance(reply, dict) or reply.get("is_error") or proc.returncode:
        detail = (str((reply or {}).get("result") or "").strip()
                  or proc.stderr.strip()[-500:] or "no reply")
        return {"error": f"exit {proc.returncode}, {detail}"}
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    if not persist and usage:
        # An unsaved request restarts the cache clock and leaves no record in
        # the transcript; the ping log carries it to `chsum cache` and `warm`.
        try:
            core._log_ping(state["sid"], started, usage)
        except OSError:
            pass
    return reply


def _usage_line(reply: dict) -> str:
    u = reply.get("usage") or {}
    return (f"read {int(u.get('cache_read_input_tokens') or 0):,}, "
            f"wrote {int(u.get('cache_creation_input_tokens') or 0):,}")


class _Lock:
    """One base takes one request at a time: an advance landing between a
    review's read of the held commit and its request would leave the review
    checking against a state the base no longer holds."""

    def __init__(self, state: dict):
        _state_dir().mkdir(parents=True, exist_ok=True)
        self.path = _path(state, ".lock")

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _cache_tail(state: dict) -> str:
    """One saved turn on `_TAIL_ASK`. Returns '' or the error text."""
    reply = _call(state, _TAIL_ASK, persist=True, doing="caching the last reply")
    return reply.get("error", "")


# --- moving the base --------------------------------------------------------

def _advance(state: dict, edit: dict | None = None) -> str:
    """Carries to the base every watched file that differs from the held
    commit: a diff where one fits, the whole file where it does not or the
    file is new, a line where it is gone. On success the ref moves to a
    commit of the files as they stand; on failure it stays, so the next
    advance carries the same change. Returns '' or the error text. The caller
    holds the lock. The held commit is read from the state file, not the
    caller's copy, since another process may have moved it since that copy
    was loaded."""
    fresh = _load(state["root"], state["name"])
    state.update(held=fresh.get("held", ""), diffs=fresh.get("diffs") or {})
    tree = _tree(state)
    if tree == _held_tree(state):
        return ""
    new = _commit(state, tree, edit, "applied" if edit else "")
    parts, chain, touched = [], dict(state.get("diffs") or {}), []
    for status, rel in _changes(state, state["held"], new):
        touched.append(rel)
        if status == "D":
            parts.append(f"{rel} is removed.")
            chain.pop(rel, None)
            continue
        data = _show(state, new, rel) or b""
        diff = "" if status == "A" else _git(
            state, ["diff", "-U3", "--no-color", state["held"], new, "--", rel]).decode(errors="replace")
        if _fits(diff, data):
            parts.append(f"{rel} changed. The diff below turns the copy of it above "
                         f"into the file as it now stands, sha256 {_sha(data)[:12]}:"
                         f"\n\n```diff\n{diff}```")
            chain[rel] = chain.get(rel, 0) + 1
        else:
            parts.append(f"{rel} now reads:\n\n{_block(rel, data)}")
            chain[rel] = 0
    reply = _call(state, "\n\n".join(parts) + f"\n\n{_ADVANCE_ASK}", persist=True,
                  doing="advancing " + ", ".join(touched))
    if "error" in reply:
        return reply["error"]
    _git(state, ["update-ref", _ref(state), new, state["held"]])
    state.update(held=new, diffs=chain, advanced=time.time())
    _save(state)
    return ""


def _proposed(tool: str, inp: dict, path: pathlib.Path) -> str | None:
    """The file's full text after the call, or None where the call fails on
    its own (an old_string absent from the file), which leaves nothing to
    review."""
    if tool == "Write":
        return str(inp.get("content") or "")
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    edits = inp.get("edits") if tool == "MultiEdit" else [inp]
    for e in edits or []:
        old, new = str(e.get("old_string") or ""), str(e.get("new_string") or "")
        if old not in text:
            return None
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


# --- held edits -------------------------------------------------------------

def _event(edit: dict, what: str) -> None:
    """Appends a timed observer event to the edit record, for `show` to set
    between the fork's transcript rows. The caller saves the record."""
    edit.setdefault("events", []).append({"at": time.time(), "what": what})


def _edit_path(state: dict, number: int) -> pathlib.Path:
    return _path(state, ".edits") / f"{number}.json"


def _edit(state: dict, number: int) -> dict:
    try:
        return json.loads(_edit_path(state, number).read_text())
    except (OSError, ValueError):
        raise SystemExit(f"observer {state['name']} holds no edit #{number}") from None


def _save_edit(state: dict, edit: dict) -> None:
    path = _edit_path(state, edit["number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(edit, indent=2))
    tmp.replace(path)


def _open_edits(state: dict) -> list[dict]:
    out = []
    for f in sorted(_path(state, ".edits").glob("*.json"), key=lambda f: int(f.stem)):
        try:
            edit = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if edit.get("status") in ("held", "forked"):
            out.append(edit)
    return out


def _cleared(state: dict, session_id: str, tool: str, inp: dict) -> dict | None:
    """The passed edit holding this exact call from this session, closed
    within `_EDIT_LIFE`: the clearance its rerun runs on, once."""
    now = time.time()
    for f in _path(state, ".edits").glob("*.json"):
        try:
            edit = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if (edit.get("status") == "passed" and edit.get("writer") == session_id
                and edit.get("tool") == tool and edit.get("input") == inp
                and now - float(edit.get("ended") or 0) <= _EDIT_LIFE):
            return edit
    return None


def _fork_name(state: dict, number: int) -> str:
    return f"observe-{state['name']}-{number}"


def _launcher() -> str:
    return f"{sys.executable} {pathlib.Path(__file__).resolve().parent.parent / 'chsum.py'}"


def _edit_cmd(state: dict, action: str, number: int) -> str:
    return (f"{_launcher()} observe {action} {number} --root {shlex.quote(state['root'])} "
            f"--name {shlex.quote(state['name'])}")




def _unified(rel: str, old: str | None, new: str, context: int = 3) -> str:
    return "".join(difflib.unified_diff((old or "").splitlines(keepends=True),
                                        new.splitlines(keepends=True),
                                        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=context))


def _changed_lines(old: str | None, new: str) -> str:
    """The `-` and `+` lines alone of a diff from `old` to `new`: no file
    headers, hunk headers or context lines."""
    return "".join(line for line in difflib.unified_diff((old or "").splitlines(keepends=True),
                                                         new.splitlines(keepends=True), n=0)
                   if line[:1] in "+-" and not line.startswith(("+++", "---")))


def _job(fork: str) -> dict:
    """Claude Code's record of a background session: its `state` (running,
    failed, done…) and `detail`, from ~/.claude/jobs/<short id>/state.json;
    {} where none exists."""
    try:
        d = json.loads((pathlib.Path.home() / ".claude" / "jobs" / fork / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _fork_status(fork: str) -> str:
    """'busy' or 'idle' from Claude Code's session registry for the fork
    whose session id opens with `fork`, '' where none is running."""
    if not fork:
        return ""
    for f in (pathlib.Path.home() / ".claude" / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and str(d.get("sessionId") or "").startswith(fork):
            return str(d.get("status") or "")
    return ""


def _messages(fork: str) -> int:
    """Messages between a fork and the writing session: the fork's
    SendMessage calls and the cross-session messages its transcript
    received."""
    hits = list(core.PROJECTS_ROOT.glob(f"*/{fork}*.jsonl")) if fork else []
    if not hits:
        return 0
    n = 0
    for rec in core._records(hits[0]):
        content = (rec.get("message") or {}).get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        for part in parts:
            if not isinstance(part, dict):
                continue
            if rec.get("type") == "assistant" and part.get("type") == "tool_use" and part.get("name") == "SendMessage":
                n += 1
            elif rec.get("type") == "user" and part.get("type") == "text" \
                    and "<cross-session-message" in str(part.get("text") or ""):
                n += 1
    return n


def _phase(edit: dict) -> str:
    """Where an open edit's exchange stands: its fork starting, working, or
    idle with a message out to the writing session."""
    if edit["status"] == "error":
        return "fork failed"
    if edit["status"] == "held":
        return "starting its fork"
    if _job(edit["fork"]).get("state") == "failed":
        return "fork failed"
    if _fork_status(edit["fork"]) == "busy":
        return "thinking" if _messages(edit["fork"]) else "reviewing"
    return "waiting"


def _hold(state: dict, rel: str, text: str | None, session_id: str, path: pathlib.Path | None,
          tool: str = "", inp: dict | None = None) -> dict:
    """Records a call as a held edit and starts its fork. A write to a
    watched file carries `rel`, `text` and `path`: the file stays as it
    stands, and `before` is its text now, which `close` checks again. Any
    other call carries `tool` and `inp` with `rel` empty."""
    for edit in _open_edits(state):
        if edit["writer"] == session_id and (
                (rel and edit["rel"] == rel) or
                (not rel and edit.get("tool") == tool and edit.get("input") == inp)):
            return edit
    with _Lock(state):
        fresh = _load(state["root"], state["name"])
        number = int(fresh.get("next_edit") or 1)
        fresh["next_edit"] = number + 1
        _save(fresh)
    try:
        before = path.read_text(errors="replace") if path else None
    except OSError:
        before = None
    edit = {"number": number, "rel": rel, "tool": tool, "input": inp, "writer": session_id,
            "to": sessions.address(session_id), "before": before, "pending": text, "agreed": None,
            "status": "held", "fork": "", "created": time.time()}
    _event(edit, f"held · `{_subject(edit)}`")
    _save_edit(state, edit)
    _note(state, outcome={"file": _subject(edit), "result": "held", "at": time.time()})
    _detach(state, "fork", number)
    return edit


def _subject(edit: dict) -> str:
    return edit["rel"] or edit.get("tool") or ""


# The skill directory's file of the tool calls a fork carries out an agreed
# result with, one permission rule per line, `{file}` standing for the held
# file's absolute path. A rule naming `{file}` drops out for a call that
# holds no file. The calls stay within the base's `--tools` list: a fork
# sending another tool list misses the base's cached prefix.
_ALLOWED_FILE = "allowed-tools.txt"
_ALLOWED_DEFAULT = ["Read(/{file})", "Write(/{file})", "Edit(/{file})"]


def _allowed(state: dict, held: pathlib.Path | None) -> list[str]:
    try:
        lines = (pathlib.Path(state["skill"]) / _ALLOWED_FILE).read_text().splitlines()
    except OSError:
        lines = _ALLOWED_DEFAULT
    out = []
    for rule in (x.strip() for x in lines):
        if not rule or rule.startswith("#") or ("{file}" in rule and not held):
            continue
        out.append(rule.replace("{file}", str(held)))
    return out


def _fork_prompt(state: dict, edit: dict, shown: str, allowed: list[str]) -> str:
    n, rel, to = edit["number"], edit["rel"], edit["to"]
    close = _edit_cmd(state, "close", n)
    if rel:
        path = pathlib.Path(state["root"]) / rel
        head = (f"Edit #{n}: the session at {to} generated a write to {rel}; chsum held it, left "
                f"{path} as it stands, and told that session this session ({_fork_name(state, n)}) "
                f"would message it.\n\n{shown}\n\n"
                f"Where the held write holds to the rules, write it to {path} as it is. Otherwise "
                f"message the session the change it needs and why, and on its accept write that "
                f"version; on a stated purpose, message a new change; on a decline, write nothing. "
                f"Message the session what was written, or that nothing was.")
    else:
        head = (f"Edit #{n}: the session at {to} generated a {edit['tool']} call; chsum held it "
                f"unrun, and told that session this session ({_fork_name(state, n)}) would "
                f"message it.\n\n{shown}\n\n"
                f"Where the held call holds to the rules, carry it out as it is. Otherwise "
                f"message the session the change it needs and why, and on its accept carry out "
                f"that version; on a stated purpose, message a new change; on a decline, carry "
                f"out nothing. Message the session what was carried out, or that nothing was.")
    rules = "; ".join(allowed) or "none"
    return (f"{head} A held call that holds to the rules and that the approved calls below "
            f"cannot carry out closes with `--pass`: the session's rerun of that same call, "
            f"unchanged, then runs once, and the message to the session says to run it again "
            f"unchanged. Then run `{close} --title '<name>'`, with `--pass` where it applies, as "
            f"the last call, <name> two to four lowercase hyphenated words for what the edit "
            f"does. The calls approved here are "
            f"SendMessage, that command, and these: {rules}. Any other call is refused, and a "
            f"refusal leaves the approved ones approved.")


def _fork(state: dict, number: int) -> int:
    """Moves the base to the watched files as they stand, then starts the
    edit's fork as a background session resumed from the base. Its SessionEnd
    hook runs `observe ended`, detached, so the base moves forward whatever
    ends it."""
    edit = _edit(state, number)
    with _Lock(state):
        err = _advance(state)
        state = _load(state["root"], state["name"])
    if err:
        edit.update(status="error", error=f"the base did not advance: {err}")
        _save_edit(state, edit)
        return 1
    if edit["rel"]:
        new = edit["pending"].encode()
        old = _show(state, state["held"], edit["rel"])
        diff = "" if old is None else _unified(edit["rel"], old.decode(errors="replace"), edit["pending"], context=1)
        shown = (f"The diff below turns the file as it stands in this conversation into the "
                 f"held write.\n\n```diff\n{diff}```" if _fits(diff, new) else
                 f"The held write's full content is below.\n\n{_block(edit['rel'], new)}")
        held = pathlib.Path(state["root"]) / edit["rel"]
    else:
        shown = (f"The held call's input is below.\n\n```json\n"
                 f"{json.dumps(edit['input'], indent=2)}\n```")
        held = None
    ended = (f"nohup {_edit_cmd(state, 'ended', number)} >/dev/null 2>&1 &")
    # `bgIsolation: none` lets the background fork edit the shared checkout:
    # measured 2026-10-06, a fork without it had Write refused with "Call
    # EnterWorktree first", one with it wrote the file. A worktree holds only
    # what git tracks, so a watched file git ignores is absent from one.
    settings = {**_SETTINGS, "worktree": {"bgIsolation": "none"},
                "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": ended}]}]}}
    rules = _allowed(state, held)
    allowed = ",".join(["SendMessage", *rules, f"Bash({_launcher()} observe close:*)"])
    flags = _BASE_FLAGS[:_BASE_FLAGS.index("--settings")]
    exe, error = _base_exe(state)
    if not exe:
        edit.update(status="error", error=error)
        _save_edit(state, edit)
        return 1
    cmd = [exe, "--bg", "--resume", state["sid"], "--fork-session", "--name", _fork_name(state, number),
           "--model", state["model"], *flags, "--allowedTools", allowed,
           "--settings", json.dumps(settings), _fork_prompt(state, edit, shown, rules)]
    env = _call_env()
    reason = ""
    for _ in range(2):
        short, reason = _launch(cmd, state["root"], env)
        if short:
            edit.update(status="forked", fork=short)
            _event(edit, f"fork `{short}` started")
            _save_edit(state, edit)
            return 0
    edit.update(status="error", error=f"the fork did not start: {reason}")
    _event(edit, "fork failed")
    _save_edit(state, edit)
    _note(state, outcome={"file": _subject(edit), "title": edit.get("title") or "", "fork": edit.get("fork") or "", "result": "error",
                          "at": time.time()})
    return 1


# A background session's job leaves its start-up states within this many
# seconds, or the launch counts as failed.
_START_WAIT = 30


def _launch(cmd: list[str], cwd: str, env: dict) -> tuple[str, str]:
    """Starts one background session and waits for its job to run. Returns
    (short id, '') once it runs, or ('', the reason) where `claude --bg`
    printed no id or the job failed: measured 2026-10-06, a launch that
    printed an id ended `failed` with "exit 1 before init" ten seconds later,
    so a printed id alone does not show the session started."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"{type(e).__name__}: {e}"
    m = re.search(r"backgrounded\s*·\s*(\w+)", out)
    if not m:
        return "", out.strip()[-300:] or "claude --bg printed no session id"
    short = m.group(1)
    deadline = time.time() + _START_WAIT
    while time.time() < deadline:
        job = _job(short)
        if job.get("state") == "failed":
            return "", f"{job.get('detail') or 'failed'} (job {short})"
        if _fork_status(short) or list(core.PROJECTS_ROOT.glob(f"*/{short}*.jsonl")):
            return short, ""
        time.sleep(1)
    return "", f"job {short} did not start within {_START_WAIT} s"


def _record_drop(state: dict, edit: dict, outcome: str = "dropped") -> None:
    """An edit closed unapplied leaves the files as they stand, so its commit
    carries the held tree unchanged, with the edit's trailers."""
    try:
        with _Lock(state):
            fresh = _load(state["root"], state["name"])
            state.update(held=fresh.get("held", ""))
            new = _commit(state, _held_tree(state), edit, outcome)
            _git(state, ["update-ref", _ref(state), new, state["held"]])
            fresh["held"] = new
            _save(fresh)
    except (_GitError, SystemExit):
        pass


def _close(state: dict, number: int, title: str = "", passed: bool = False) -> int:
    """The fork's last call. With `passed`, the edit closes as passed, and
    the writing session's rerun of the same call runs once. Otherwise a held
    write's file sets the outcome: changed since the write was held means the
    fork wrote it, applied, and the base moves forward to it; unchanged means
    dropped. A held call with no file reads as applied where any watched file
    differs from the held commit, and as dropped where none does. Then the
    fork is stopped."""
    edit = _edit(state, number)
    if title:
        edit["title"] = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
        _event(edit, f"named `{edit['title']}`")
    if edit["status"] in ("held", "forked") and passed:
        edit.update(status="passed", ended=time.time())
        _event(edit, "passed")
        _record_drop(state, edit, "passed")
    elif edit["status"] in ("held", "forked"):
        if edit["rel"]:
            try:
                now = (pathlib.Path(state["root"]) / edit["rel"]).read_text(errors="replace")
            except OSError:
                now = None
            changed = now is not None and now != edit["before"]
        else:
            now = None
            try:
                changed = _tree(state) != _held_tree(state)
            except _GitError:
                changed = False
        if changed:
            edit.update(status="applied", agreed=now, applied=time.time(), ended=time.time())
            _event(edit, "applied" if now == edit["pending"] else "applied, changed")
            _save_edit(state, edit)
            try:
                with _Lock(state):
                    err = _advance(state, edit)
            except _GitError as e:
                err = str(e)
            if err:
                print(f"the base did not take the write: {err}")
        else:
            edit.update(status="closed", ended=time.time())
            _event(edit, "dropped")
            _record_drop(state, edit)
    _save_edit(state, edit)
    result = edit["status"] if edit["status"] in ("applied", "passed") else "dropped"
    _note(state, outcome={"file": _subject(edit), "title": edit.get("title") or "", "fork": edit.get("fork") or "",
                          "result": result, "at": time.time()})
    _detach(state, "end", number)
    print(f"edit #{number} {result}; this session stops in {_CLOSE_DELAY} s")
    return 0


def _end(state: dict, number: int, delay: float) -> int:
    """Stops and removes the edit's fork after `delay` seconds."""
    time.sleep(delay)
    fork = _edit(state, number).get("fork")
    if fork:
        for verb in ("stop", "rm"):
            subprocess.run(["claude", verb, fork], capture_output=True, timeout=60)
    return 0


def _ended(state: dict, number: int) -> int:
    """The fork's SessionEnd: an edit still open closes unapplied, and the
    base moves to the watched files as they stand."""
    edit = _edit(state, number)
    if edit["status"] in ("held", "forked"):
        edit.update(status="closed", ended=time.time())
        _event(edit, "dropped")
        _save_edit(state, edit)
        _record_drop(state, edit)
    with _Lock(state):
        _advance(state)
    return 0


# --- history ----------------------------------------------------------------

def _git_dirs() -> list[pathlib.Path]:
    return sorted(_state_dir().glob("*.git"))


def _history(gitdir: pathlib.Path) -> list[dict]:
    """Every edit commit on the observer refs one git directory holds: the
    trailers, the observer's name, the commit and its time. Read with git
    alone, so it serves an observer whose state file `stop` removed."""
    try:
        out = subprocess.run(["git", f"--git-dir={gitdir}", "log", "--format=%x1e%H%x1f%cI%x1f%s%x1f%b",
                              "--glob=refs/chsum/observe/*"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for chunk in out.stdout.split("\x1e"):
        parts = chunk.strip("\n").split("\x1f")
        if len(parts) != 4:
            continue
        sha, when, subject, body = parts
        trailers = dict(line.split(": ", 1) for line in body.splitlines() if ": " in line)
        if "Edit" not in trailers:
            continue
        rows.append({"commit": sha, "when": when, "observer": subject.split(":")[0].removeprefix("chsum observe "),
                     "gitdir": str(gitdir), "number": int(trailers["Edit"]),
                     "root": trailers.get("Root", ""), "file": trailers.get("File", ""),
                     "writer": trailers.get("Writer", ""), "title": trailers.get("Title", ""),
                     "fork": trailers.get("Fork", ""), "outcome": trailers.get("Outcome", "")})
    return rows


def edits_for_session(session_id: str) -> list[dict]:
    """Every edit a session's writes became, across every observer: the
    closed ones from the observers' histories, the open ones from their edit
    files, oldest first. Each carries the agreed diff where it was applied."""
    out, seen = [], set()
    for gitdir in _git_dirs():
        for row in _history(gitdir):
            if row["writer"] != session_id:
                continue
            seen.add((row["observer"], row["number"]))
            if row["outcome"] == "applied":
                row["diff"] = subprocess.run(
                    ["git", f"--git-dir={gitdir}", "show", "--format=", row["commit"], "--", row["file"]],
                    capture_output=True, text=True, timeout=60).stdout
            out.append(row)
    for state in _states():
        for edit in _open_edits(state):
            if edit["writer"] == session_id and (state["name"], edit["number"]) not in seen:
                out.append({"observer": state["name"], "number": edit["number"], "file": _subject(edit),
                            "root": state["root"], "title": edit.get("title") or "",
                            "writer": session_id, "fork": edit.get("fork") or "", "outcome": "open",
                            "when": datetime.fromtimestamp(edit["created"], timezone.utc).isoformat()})
    return sorted(out, key=lambda r: r["when"])


def fork_session(first_text: str) -> tuple[str, int] | None:
    """(observer, edit number) where a session's first message is an edit
    fork's prompt, None otherwise."""
    m = re.match(r"Edit #(\d+): the session at \S+ wrote to .*?this session \(observe-(.+)-\1\)",
                 first_text, re.S)
    return (m.group(2), int(m.group(1))) if m else None


def base_session(first_text: str) -> bool:
    return first_text.startswith("The files below are the watched files of ")


def session_ref(session_id: str) -> str:
    """The `ch_` ref `chsum digest` takes for a session, checked against the
    transcripts on disk; one with no transcript reads as never started."""
    if not session_id:
        return "-"
    hits = list(core.PROJECTS_ROOT.glob(f"*/{session_id}*.jsonl"))
    return core.ch_ref_for_path(hits[0]) if hits else f"{session_id} (never started)"


def _fork_rows(edit: dict) -> tuple[list, str]:
    """The fork's digest rows after the edit's prompt, as `chsum digest
    --messages` reads them, and the fork's session id. The rows before the
    prompt are the base's copied turns, which belong to no edit; the prompt
    is chsum's own instructions, the same for every edit."""
    hits = list(core.PROJECTS_ROOT.glob(f"*/{edit.get('fork')}*.jsonl")) if edit.get("fork") else []
    if not hits:
        return [], ""
    rows = core.collect_rows(hits[0], core._ROW_KINDS["messages"])
    start = next((k for k, r in enumerate(rows)
                  if r.kind == "message" and r.text.startswith(f"Edit #{edit['number']}:")), None)
    return (rows[start + 1:] if start is not None else []), hits[0].stem


def _event_md(at: float, what: str, edit: str = "") -> str:
    label = f"{edit} · " if edit else ""
    return f"*{datetime.fromtimestamp(at):%H:%M:%S} · {label}{what}*"


_CALL_FLAGS = re.compile(r" --root \S+| --name \S+")
# Any interpreter running any copy of chsum.py: the fork ran its own, and the
# process rendering it may run another.
_CHSUM_CALL = re.compile(r"\S*python[\d.]*\s+\S*/chsum\.py\b")


def _row_md(row, session: str) -> str:
    """One fork row as the digest's messages view prints it: a message whole,
    a tool call as one clipped line. A fork's chsum call is shown as `chsum
    observe <action> <edit>`: the interpreter path and the root and name its
    command carries are the same on every call and push the action out of
    the clipped line."""
    if row.kind in ("tool", "command") and _CHSUM_CALL.search(row.text):
        row = dataclasses.replace(row, text=_CALL_FLAGS.sub("", _CHSUM_CALL.sub("chsum", row.text)))
    return "\n".join(core._row_block(row, session, row.kind == "message")).strip("\n")


def _timeline(edit: dict, rows: list, session: str, label: str = "") -> list[tuple[float, str]]:
    """The fork's rows and the edit's observer events, as (time, markdown)
    in time order."""
    out = [(core._parse_ts(r.when).timestamp(), _row_md(r, session)) for r in rows
           if core._parse_ts(r.when)]
    out += [(e["at"], _event_md(e["at"], e["what"], label)) for e in edit.get("events") or []]
    return sorted(out, key=lambda item: item[0])


def _find_edit(state: dict, key: str) -> dict:
    """An edit by its number, or by the name its fork gave it, the latest
    such where several share one."""
    key = key.lstrip("#")
    if key.isdigit():
        return _edit(state, int(key))
    named = []
    for f in _path(state, ".edits").glob("*.json"):
        try:
            edit = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if edit.get("title") == key:
            named.append(edit)
    if not named:
        raise SystemExit(f"observer {state['name']} holds no edit named {key!r}")
    return max(named, key=lambda e: e["created"])


# An edit record's status as the status line and `observe log` name it.
_OUTCOME = {"closed": "dropped", "error": "fork failed"}


def _follow_all(state: dict) -> int:
    """Every action of one observer as it lands, until interrupted: the
    base's requests, and for each edit its observer events, its fork's rows
    and its live phase, each labelled with the edit's name."""
    _out(f"# {state['name']}\n\n*following `{state['root']}` · ctrl-c stops*\n")
    printed: set = set()
    phases: dict[int, str] = {}
    for f in _path(state, ".edits").glob("*.json"):
        try:
            e = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        rows, session = _fork_rows(e)
        printed.update(_timeline(e, rows, session, e.get("title") or f"edit #{e['number']}"))
    running, last_at = None, float((_activity(state).get("last") or {}).get("at") or 0)
    try:
        while True:
            act = _activity(state)
            now = act.get("now")
            if now and now != running:
                subject, phase = _running_parts(now.get("doing") or "", act.get("outcome"), time.time())
                _out(_event_md(time.time(), f"{subject} · {phase}"))
            running = now
            last = act.get("last") or {}
            if float(last.get("at") or 0) > last_at:
                last_at = float(last["at"])
                subject, phase = _running_parts(last.get("doing") or "", act.get("outcome"), time.time())
                _out(_event_md(last_at, f"{subject} · {phase} done in {last.get('seconds', 0)}s"))
            for f in sorted(_path(state, ".edits").glob("*.json"), key=lambda f: int(f.stem)):
                try:
                    e = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                label = e.get("title") or f"edit #{e['number']}"
                rows, session = _fork_rows(e)
                fresh = [md for item in _timeline(e, rows, session, label) if item not in printed
                         and not printed.add(item) for md in [item[1]]]
                if fresh:
                    _out("\n\n".join(fresh))
                if e["status"] in ("held", "forked"):
                    live = _phase(e)
                    if live in ("reviewing", "thinking", "waiting") and live != phases.get(e["number"]):
                        phases[e["number"]] = live
                        _out(_event_md(time.time(), live, label))
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


def _list_edits(state: dict) -> int:
    """Every edit the observer holds a record of, newest first: the name and
    number `show` takes, its outcome or phase, its age and file."""
    edits = []
    for f in _path(state, ".edits").glob("*.json"):
        try:
            edits.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    if not edits:
        print(f"observer {state['name']} has held no edit since it started")
        return 0
    for e in sorted(edits, key=lambda e: e["created"], reverse=True):
        where = _phase(e) if e["status"] in ("held", "forked") else _OUTCOME.get(e["status"], e["status"])
        print(f"#{e['number']:<3} {e.get('title') or '(unnamed)':<28} {where:<10} "
              f"{_ago(time.time() - e['created']):>4} ago  {e['rel']}")
    print("\nOne edit's exchange: chsum observe show <name or #> [--follow]")
    return 0


def _out(markdown: str) -> None:
    """Markdown to the terminal through chsum's renderer, as the other
    documents print; piped or captured, the markdown itself."""
    tty = sys.stdout.isatty() and core._colour_ok()
    print(core._md_ansi(markdown) if tty else markdown, flush=True)


# `--follow` holds on this long after an edit closes for the fork's last
# message, which lands after `apply` has closed the edit.
_FOLLOW_TAIL = 60


def _show_edit(state: dict, key: str, follow: bool) -> int:
    """An edit as a markdown document: the fork's transcript rows from the
    edit's prompt, rendered as the digest renders them, with the observer's
    events (held, fork started, named, proposed, applied, dropped) between
    them by time. With `follow`, each new row and event as it lands, and the
    fork's live phase (reviewing, thinking, waiting) as it changes, until the
    fork has ended or `_FOLLOW_TAIL` seconds after the edit closed."""
    edit = _find_edit(state, key)
    name = edit.get("title") or f"edit #{edit['number']}"
    _out(f"# {name}\n\n*{state['name']} · edit #{edit['number']} · `{edit['rel']}`*\n")
    printed, phase, closed_at = set(), "", 0.0
    while True:
        edit = _edit(state, edit["number"])
        rows, session = _fork_rows(edit)
        fresh = [md for at, md in _timeline(edit, rows, session) if (at, md) not in printed
                 and not printed.add((at, md))]
        if fresh:
            _out("\n\n".join(fresh))
        is_open = edit["status"] in ("held", "forked")
        if follow and is_open:
            live = _phase(edit)
            if live != phase and live in ("reviewing", "thinking", "waiting"):
                phase = live
                _out(_event_md(time.time(), live))
        if not follow:
            break
        if not is_open:
            closed_at = closed_at or time.time()
            if not _fork_status(edit.get("fork") or "") or time.time() - closed_at > _FOLLOW_TAIL:
                break
        time.sleep(2)
    if edit["status"] == "applied":
        _out(f"\n## Applied\n\n```diff\n{_changed_lines(edit['before'], edit['agreed'])}```\n")
    elif edit["status"] in ("held", "forked") and edit.get("agreed") is not None:
        lines = _changed_lines(edit["pending"], edit["agreed"])
        if lines:
            _out(f"\n## Proposed\n\n```diff\n{lines}```\n")
    return 0


def _log(state: dict) -> int:
    for row in _history(_path(state, ".git")):
        print(f"{row['when'][:19]}  {row['title'] or '(unnamed)':<24} {row['outcome']:<8} {row['file']}")
        print(f"    writer {session_ref(row['writer'])}  fork {session_ref(row['fork'])}  "
              f"commit {row['commit'][:12]}")
    return 0


# --- cache ------------------------------------------------------------------

def _cache_left(state: dict, now: float) -> float | None:
    """Seconds left on the base's cache, counted from its last request or
    logged ping; None where its transcript records no cache write."""
    hits = list(core.PROJECTS_ROOT.glob(f"*/{state['sid']}.jsonl"))
    row = core._cache_row(list(core._records(hits[0])), now, state["sid"]) if hits else None
    if not row or row["lifetime"] is None or row["age"] is None:
        return None
    return row["lifetime"] - row["age"]


def _expired(state: dict) -> bool:
    """An expired base is relaunched only by `chsum observe reload`: a review,
    an advance or a ping on it would re-write its whole prefix."""
    left = _cache_left(state, time.time())
    return left is not None and left <= 0


def _detach(state: dict, action: str, number: int | None = None) -> None:
    launcher = pathlib.Path(__file__).resolve().parent.parent / "chsum.py"
    edit = [str(number)] if number is not None else []
    subprocess.Popen([sys.executable, str(launcher), "observe", action, *edit,
                      "--root", state["root"], "--name", state["name"]],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def on_hook(payload: dict) -> None:
    """Called from every chsum hook in every session. For each base that is
    not expired: one whose cache has under `_WARM_BELOW` seconds left takes an
    unsaved ping, and one whose watched files differ from the held commit
    takes an advance. Each runs in a detached process, so the hook returns at
    once."""
    now = time.time()
    for state in _states():
        left = _cache_left(state, now)
        if left is not None and left <= 0:
            continue
        for edit in _open_edits(state):
            if now - float(edit.get("created") or now) > _EDIT_LIFE:
                edit.update(status="closed", ended=time.time())
                _event(edit, "dropped")
                _save_edit(state, edit)
                _detach(state, "end", edit["number"])
        act = _activity(state)
        if left is not None and left < _WARM_BELOW and now - float(act.get("ping_started") or 0) >= _PING_GAP:
            _note(state, ping_started=now)
            _detach(state, "ping")
        if act.get("now") or now - float(act.get("drift_checked") or 0) < _DRIFT_GAP:
            continue
        _note(state, drift_checked=now)
        try:
            drifted = _tree(state) != _held_tree(state)
        except _GitError:
            continue
        if drifted:
            _detach(state, "advance")


def _ping(state: dict) -> int:
    """One unsaved request over the base, through `chsum warm`'s ping with the
    base's flags and environment, logged so the cache clock counts it."""
    hits = list(core.PROJECTS_ROOT.glob(f"*/{state['sid']}.jsonl"))
    if not hits:
        return 1
    _, lifetime = core._cache_basis(list(core._records(hits[0])))
    started = time.time()
    _note(state, now={"doing": "warming the cache", "since": started})
    usage = core._warm_ping(state["sid"], state["root"], state["model"],
                            lifetime or 3600, tuple(_BASE_FLAGS), dict(_BASE_ENV),
                            _base_version(state))
    _note(state, now=None, last={"doing": "warming the cache", "at": time.time(),
                                 "seconds": round(time.time() - started, 1),
                                 "read": int(usage.get("cache_read_input_tokens") or 0),
                                 "wrote": int(usage.get("cache_creation_input_tokens") or 0),
                                 "error": usage.get("error", "")})
    if "error" in usage:
        return 1
    core._log_ping(state["sid"], started, usage)
    return 0


def base_call(sid: str) -> tuple[list[str], dict[str, str]]:
    """The flags and environment a request on this session needs to send the
    prefix its base was built with, or ([], {}) for a session no observe
    state names. `chsum warm` sends them: a ping with the default tool list
    and settings misses the base's whole prefix."""
    if any(s["sid"] == sid for s in _states()):
        return list(_BASE_FLAGS), dict(_BASE_ENV)
    return [], {}


# --- hooks ------------------------------------------------------------------

def _reload_hint(state: dict) -> str:
    return f"chsum observe reload --root {state['root']} --name {state['name']}"


def hold_call(payload: dict, root: str, name: str) -> int:
    """`observe hold`: one PreToolUse payload, printed back as a decision in
    JSON for the calling hook to return. A call matching a passed edit from
    the same session spends that clearance and prints `allow`. Any other
    call becomes a held edit with its fork started, and prints `deny` with
    the reason. A Write, Edit or MultiEdit to a watched file holds the
    file's text after the call. Any other call holds on the observer --root
    and --name select, --root falling back to the payload's cwd. Exit 1,
    with the cause on stderr, where the payload names no tool, no observer
    holds the call, its cache has expired, or the write fails on its own."""
    tool = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input") or {}
    if not tool:
        print("the payload names no tool_name; hold takes a PreToolUse payload", file=sys.stderr)
        return 1
    cwd = pathlib.Path(str(payload.get("cwd") or "."))
    state, rel, path, text = None, "", None, None
    if tool in ("Write", "Edit", "MultiEdit") and inp.get("file_path"):
        target = pathlib.Path(str(inp["file_path"]))
        target = target if target.is_absolute() else cwd / target
        state, rel = _state_for(target)
        if state:
            path, text = target, _proposed(tool, inp, target)
            if text is None:
                print(f"the {tool} call fails on its own: its old_string is absent from {rel}",
                      file=sys.stderr)
                return 1
    if not state:
        state = _load(_resolve_root(root or str(cwd)), name)
    session = str(payload.get("session_id") or "")
    cleared = _cleared(state, session, tool, inp)
    if cleared:
        cleared["status"] = "released"
        _event(cleared, "rerun released")
        _save_edit(state, cleared)
        print(json.dumps({"observer": state["name"], "edit": cleared["number"], "decision": "allow"}))
        return 0
    if _expired(state):
        print(f"observer {state['name']} has expired; `{_reload_hint(state)}` relaunches it",
              file=sys.stderr)
        return 1
    edit = _hold(state, rel, text, session, path, tool, inp)
    print(json.dumps({"observer": state["name"], "edit": edit["number"], "decision": "deny",
                      "reason": f"held for review by {state['name']}; its reviewer will message you."}))
    return 0


def hook_post(payload: dict) -> int:
    """PostToolUse on Write, Edit and MultiEdit: a landed write to a watched
    file moves the base forward. Every path exits 0."""
    try:
        inp = payload.get("tool_input") or {}
        cwd = pathlib.Path(str(payload.get("cwd") or "."))
        path = pathlib.Path(str(inp.get("file_path") or ""))
        state, _ = _state_for(path if path.is_absolute() else cwd / path)
        if not state or _expired(state):
            return 0
        with _Lock(state):
            err = _advance(state)
        if err:
            print(f"chsum observe: the base did not advance ({err}); the next "
                  f"review retries it", file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001 — a fault here never fails the turn
        print(f"chsum observe post-tool-use hook: {e}", file=sys.stderr)
        return 0


# --- start, reload, stop ----------------------------------------------------

def _first_message(state: dict, files: list[tuple[str, bytes]]) -> str:
    """The base's first turn: every watched file whole, then the ask whose
    reply carries the thinking block the cache holds on."""
    return (f"The files below are the watched files of {state['root']}, the "
            f"files whose root-relative path fully matches "
            f"{' or '.join(state['match'])}. Each opens with its path and "
            f"sha256.\n\n"
            + "\n".join(_block(rel, data) for rel, data in files)
            + f"\n{_SAVED_ASK}")


def _preview(state: dict, full: bool) -> int:
    """What `start` sends for `state`, read from disk with no model call and
    nothing written: each skill file and watched file with its size, and the
    token estimate of each part at four characters a token. `full` prints the
    system prompt and the first message as sent."""
    root, skill = pathlib.Path(state["root"]), pathlib.Path(state["skill"])
    skill_files = [(rel, (skill / rel).read_bytes()) for rel in _skill_files(state)]
    watched = [(rel, (root / rel).read_bytes()) for rel in _watched(state)]
    system, first = _skill_text(state), _first_message(state, watched)
    if full:
        print(f"===== system prompt ({len(system):,} characters)\n{system}\n")
        print(f"===== first message ({len(first):,} characters)\n{first}")
        return 0
    print(f"system prompt  {skill}  ~{len(system) // 4:,} tokens")
    print(f"  matching {' | '.join(state['skill_match'])}"
          + (f", skipping {' | '.join(state['skill_skip'])}" if state.get("skill_skip") else ""))
    for rel, data in skill_files:
        print(f"  {len(data):>9,}  {rel}")
    print(f"first message  {root}  ~{len(first) // 4:,} tokens")
    print(f"  matching {' | '.join(state['match'])}"
          + (f", skipping {' | '.join(state['skip'])}" if state.get("skip") else ""))
    for rel, data in watched:
        print(f"  {len(data):>9,}  {rel}")
    print(f"total          ~{(len(system) + len(first)) // 4:,} tokens written once at start, "
          f"then read from cache by each review")
    return 0


def _build(state: dict) -> int:
    """Creates the base: the skill as its system prompt, every watched file in
    its first turn, then the tail turn. The ref is set to the commit the base
    took in, a child of the held one where `state` carries the history of an
    earlier base."""
    skill_text = _skill_text(state)
    state.update(sid=str(uuid.uuid4()), held=state.get("held") or "", diffs={}, started=time.time(),
                 skill_sha=_sha(skill_text.encode()))
    commit = _commit(state, _tree(state), kind="reload" if state["held"] else "start")
    files = [f for f in _git(state, ["ls-tree", "-r", "--name-only", "-z", commit]).decode().split("\0") if f]
    if not files:
        raise SystemExit(f"no file in {state['root']} matches {state['match']}")
    message = _first_message(state, [(f, _show(state, commit, f) or b"") for f in files])
    with _Lock(state):
        reply = _call(state, message, persist=True, create=skill_text, doing="building the base")
        if "error" in reply:
            raise SystemExit(f"the base was not created: {reply['error']}")
        print(f"base {state['sid']}: {len(files)} files, {_usage_line(reply)}")
        _git(state, ["update-ref", _ref(state), commit])
        state["held"] = commit
        _save(state)
        err = _cache_tail(state)
        tail = _activity(state).get("last") or {}
        state["base_tokens"] = int(tail.get("read") or 0) + int(tail.get("wrote") or 0)
        _save(state)
    if err:
        print(f"the last reply was not cached ({err}); each review re-writes it "
              f"until the next advance", file=sys.stderr)
    print(f"held at {_ref(state)}; chsum hooks in any session keep it warm and "
          f"carry out-of-band changes forward; once it expires, "
          f"`{_reload_hint(state)}` relaunches it")
    return 0


def _resolve_root(arg: str) -> str:
    root = pathlib.Path(arg or ".").expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"no such directory: {root}")
    if not shutil.which("git"):
        raise SystemExit("git is not on PATH; observe holds its state in a git directory")
    return str(root)


def _load(root: str, name: str) -> dict:
    found = [s for s in _states() if s["root"] == root and (not name or s["name"] == name)]
    if len(found) == 1:
        return found[0]
    if not found:
        here = sorted(s["name"] for s in _states() if s["root"] == root)
        if name and here:
            raise SystemExit(f"no observer named {name!r} in {root}; it holds {', '.join(here)}")
        raise SystemExit(f"no observer in {root}; `chsum observe start` builds one")
    raise SystemExit(f"{len(found)} observers in {root}; --name picks one of "
                     f"{', '.join(s['name'] for s in found)}")


def _drop(state: dict, keep_history: bool) -> None:
    _state_path(state["root"], state["name"]).unlink(missing_ok=True)
    for suffix in (".index", ".activity.json"):
        _path(state, suffix).unlink(missing_ok=True)
    for f in _path(state, ".edits").glob("*.json"):
        try:
            fork = json.loads(f.read_text()).get("fork")
        except (OSError, ValueError):
            fork = ""
        for verb in ("stop", "rm") if fork else ():
            subprocess.run(["claude", verb, fork], capture_output=True, timeout=60)
    shutil.rmtree(_path(state, ".edits"), ignore_errors=True)
    if not keep_history:
        shutil.rmtree(_path(state, ".git"), ignore_errors=True)


def _compile_all(patterns: list[str]) -> None:
    for p in patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise SystemExit(f"not a regex: {p!r} ({e})") from None


# --- status line ------------------------------------------------------------

def _ago(secs: float) -> str:
    secs = int(secs)
    return f"{secs}s" if secs < 60 else f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h"


# Claude Code shows COLUMNS - 4 visible columns of a status line and cuts a
# longer one to an ellipsis: at 126 columns a 124-column line showed as 121
# columns and "…". A line of COLUMNS - 4 ends on the right edge.
_INSET = 4

_PAINT = {"dim": "2", "name": "1;36", "review": "33", "advance": "34", "build": "35",
          "pass": "32", "edit": "38;5;208", "error": "31", "held": "33", "proposed": "33",
          "applied": "32", "closed": "2", "dropped": "2", "warm": "32", "low": "33", "cold": "31"}

# After a base request ends, the status line holds that base's line this long
# before it returns to the summary line.
_HOLD = 60


def _paint(text: str, key: str) -> str:
    return f"\033[{_PAINT[key]}m{text}\033[0m"


def _spread(left: tuple[str, int], mid: tuple[str, int], right: tuple[str, int], width: int) -> str:
    """Places `right` against the end of `width` and `mid` on its centre,
    shifted left by the columns a centred `mid` would take from `right` and
    the two-column gap before it. Each part is (painted text, visible
    length): the escapes take no columns, so the padding is computed on the
    visible lengths. A width too narrow for the three parts with a gap
    between each returns them joined by spaces."""
    (lt, ll), (mt, ml), (rt, rl) = left, mid, right
    if width <= 0:
        return f"{lt}  {mt}  {rt}"
    mid_at = max(min((width - ml) // 2, width - rl - 2 - ml), ll + 2)
    gap = width - rl - (mid_at + ml)
    if gap < 2:
        return f"{lt}  {mt}  {rt}"
    return lt + " " * (mid_at - ll) + mt + " " * gap + rt


def _tokens(n: int) -> str:
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def _cache_part(left_secs: float | None, size: int, wrote: int | None = None) -> tuple[str, int]:
    """The cache's size in tokens, and with `wrote` the tokens the base's
    last request wrote to it. An expired cache shows the reload instead: no
    hook pings, reviews or advances it again until the person relaunches it."""
    if left_secs is not None and left_secs <= 0:
        text = "cache expired"
        return _paint(text, "cold"), len(text)
    head = f"cache {_tokens(size)}" if size else "cache unknown"
    text = head if wrote is None else f"{head} +{_tokens(wrote)}"
    return _paint(text, "dim"), len(text)


def _base_view(state: dict, now: float) -> dict:
    """What the status line needs from one base: its activity file, the
    moment of its latest request start or end, and its cache's time left."""
    act = _activity(state)
    running = act.get("now")
    moved = max([float((running or {}).get("since") or 0),
                 float((act.get("last") or {}).get("at") or 0)])
    hits = list(core.PROJECTS_ROOT.glob(f"*/{state['sid']}.jsonl"))
    row = core._cache_row(list(core._records(hits[0])), now, state["sid"]) if hits else None
    left = (row["lifetime"] - row["age"]
            if row and row["lifetime"] is not None and row["age"] is not None else None)
    return {"state": state, "act": act, "running": running, "moved": moved, "left": left,
            "prefix": row["prefix"] if row else 0}


def _growth(views: list[dict]) -> str:
    """The bases' size at their last build, what later turns added, and the
    longest run of diffs any one file carries: the reload measures. A reload
    pays for itself after about 20 × base / growth requests."""
    base = sum(int(v["state"].get("base_tokens") or 0) for v in views)
    if not base:
        return ""
    grown = sum(max(v["prefix"] - int(v["state"].get("base_tokens") or 0), 0) for v in views)
    chain = max([n for v in views for n in (v["state"].get("diffs") or {}).values()] or [0])
    text = f"base {base / 1000:.0f}K +{grown / 1000:.1f}K"
    return text + (f" · {chain} diffs on one file" if chain > 1 else "")


# What each base request is, as (subject, phase) for the status line.
_RUNNING = {"building the base": ("base", "building"), "caching the last reply": ("base", "caching"),
            "warming the cache": ("cache", "warming")}


def _running_parts(doing: str, outcome: dict | None, now: float) -> tuple[str, str]:
    """A running request as (subject, phase). An advance names its files,
    or the edit it carries where an edit was applied under `_HOLD` seconds
    ago to the file it advances."""
    if doing.startswith("advancing "):
        files = doing.removeprefix("advancing ").split(", ")
        if outcome and outcome.get("title") and outcome.get("file") in files \
                and now - float(outcome.get("at") or 0) < _HOLD:
            return outcome["title"], "advancing"
        return ", ".join(pathlib.Path(f).name for f in files), "advancing"
    if doing.startswith("reviewing "):
        return pathlib.Path(doing.removeprefix("reviewing ")).name, "reviewing"
    return _RUNNING.get(doing, (doing or "base", "running"))


def _base_parts(view: dict, now: float) -> tuple[tuple[str, int], tuple[str, int]]:
    """One live base as (activity, gauge): the open edit, the running
    request, or the request that ended under `_HOLD` seconds ago, then its
    cache with the fork's message count where a fork holds the edit."""
    state, act, running = view["state"], view["act"], view["running"]
    edits = _open_edits(state)
    last = act.get("last") or {}
    outcome = act.get("outcome")
    if edits and not running:
        e = edits[-1]
        more = f" (+{len(edits) - 1} more)" if len(edits) > 1 else ""
        text = (f"{e.get('title') or pathlib.Path(e['rel']).name} · {_phase(e)} · "
                f"{_ago(now - e['created'])}{more}")
        said = _messages(e.get("fork") or "")
        count = f"{said} message{'' if said == 1 else 's'} · "
        cache = _cache_part(view["left"], view["prefix"])
        return (_paint(text, "review"), len(text)), (_paint(count, "dim") + cache[0], len(count) + cache[1])
    elif running:
        name, phase = _running_parts(running.get("doing") or "", outcome, now)
        text = f"{name} · {phase} · {_ago(now - running.get('since', now))}"
        mid = (_paint(text, "review"), len(text))
    elif outcome and outcome["result"] in ("applied", "dropped", "error") and now - outcome["at"] < _HOLD:
        name = outcome.get("title") or pathlib.Path(outcome["file"]).name
        tail = f" · {_ago(now - outcome['at'])} ago"
        mid = (_paint(f"{name} · ", "dim") + _paint(outcome["result"], "error" if outcome["result"] == "error" else "dim") + _paint(tail, "dim"),
               len(name) + 3 + len(outcome["result"]) + len(tail))
    elif str(last.get("doing") or "").startswith("advancing "):
        changed = ", ".join(pathlib.Path(f).name for f in last["doing"].removeprefix("advancing ").split(", "))
        text = f"{changed} changed outside · advanced · {_ago(now - float(last.get('at') or now))} ago"
        mid = (_paint(text, "dim"), len(text))
    else:
        text = f"{last.get('doing') or 'idle'} · {_ago(now - float(last.get('at') or now))} ago"
        mid = (_paint(text, "dim"), len(text))
    wrote = None if running else int(last.get("wrote") or 0)
    cache = _cache_part(view["left"], view["prefix"], wrote)
    held = outcome and outcome.get("fork") and now - outcome["at"] < _HOLD and not running
    if held:
        said = _messages(outcome["fork"])
        count = f"{said} message{'' if said == 1 else 's'} · "
        cache = (_paint(count, "dim") + cache[0], len(count) + cache[1])
    return mid, cache


def _observer_parts(payload: dict, now: float) -> dict | None:
    """The observer's share of the status line: the base it names, its
    activity where a base is live, and its cache gauge. A live base is one
    with a request running, an edit open, or a request that ended under
    `_HOLD` seconds ago; with none live, the newest expired base and its
    reload warning, and otherwise the newest base and its cache. `others`
    counts the bases on the root the line leaves out. None where no base
    covers the session's root."""
    ws = payload.get("workspace") or {}
    root = str(ws.get("project_dir") or ws.get("current_dir") or payload.get("cwd") or "").rstrip("/")
    views = [_base_view(s, now) for s in _states()
             if not root or (s["root"] + "/").startswith(root + "/")
             or (root + "/").startswith(s["root"] + "/")]
    if not views:
        return None
    live = [v for v in views if v["running"] or now - v["moved"] < _HOLD or _open_edits(v["state"])]
    if live:
        view = max(live, key=lambda v: (bool(v["running"]), v["moved"]))
        mid, gauge = _base_parts(view, now)
        return {"state": view["state"], "mid": mid, "gauge": gauge, "others": len(views) - 1}
    expired = [v for v in views if v["left"] is not None and v["left"] <= 0]
    view = max(expired or views, key=lambda v: v["moved"])
    return {"state": view["state"], "mid": None, "gauge": _cache_part(view["left"], view["prefix"]),
            "others": len(views) - 1}


# The handoff's phase as (text, paint) for the status line's activity.
_HANDOFF_PHASE = {"writing": ("handoff writing {}", "review"),
                  "ready": ("handoff ready", "review"),
                  "sent": ("handoff sent", "dim"),
                  "delivered": ("handoff delivered", "dim"),
                  "skipped": ("handoff failed: {}", "error")}


# The handoff's request runs about 20 seconds after the rewind; a cache with
# under this many seconds left expires before that request reads it.
_REWIND_MARGIN = 60


def _rewind_parts(payload: dict, now: float) -> dict:
    """The rewind's share of the status line. `event` holds a handoff phase,
    which outranks every other activity; `mid` the rewind to the cursor as
    one phrase, its words, the context it frees and the share of its cost
    the turns past the cursor have paid, which fills the activity where
    nothing else holds it, in the attention paint once that share reaches
    1, and stays off once the session's cache has under `_REWIND_MARGIN`
    seconds left: a cold cache rewrites the cut at the write rate, and the
    handoff fails on an expired branch; `cursor` the same phrase as (words, tail, paint), so a narrow line
    clips the words and keeps the tail; `saved` what this session's rewinds
    have saved."""
    out: dict = {"event": None, "mid": None, "cursor": None, "saved": None}
    path = pathlib.Path(str(payload.get("transcript_path") or ""))
    if not path.name:
        return out
    phase = core._handoff_phase(path.stem, now)
    if phase:
        name, detail = phase
        form, key = _HANDOFF_PHASE[name]
        text = form.format(detail.split(":")[0].strip()[:40])
        out["event"] = (_paint(text, key), len(text))
    status = core._rewind_view(path)
    if not status:
        return out
    tokens = lambda n: f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}K"
    active = max(float(status.get("started") or 0), core._last_ping(path.stem))
    lifetime = status.get("lifetime")
    warm = bool(active and lifetime) and now - active < lifetime - _REWIND_MARGIN
    if status["cursor"] and warm:
        share = status["paid"] / status["cost"] if status["cost"] else 0
        words = f"rewind to “{status['cursor']}”"
        tail = f" frees {tokens(status['cut'])} of context · " + (
            "worth it" if share >= 1 else f"break-even {share:.0%}")
        key = "review" if share >= 1 else "dim"
        out["cursor"] = (words, tail, key)
        out["mid"] = (_paint(words + tail, key), len(words) + len(tail))
    if status["saved"] > 0:
        text = f"rewinds saved {tokens(status['saved'])}"
        out["saved"] = (_paint(text, "dim"), len(text))
    return out


_ESCAPE = re.compile(r"\033\[[0-9;]*m")


def _clip(part: tuple[str, int], limit: int) -> tuple[str, int]:
    """`part` cut to `limit` visible columns, the last one an ellipsis. The
    escapes take no columns and are carried whole, so the paint before the
    cut holds and a reset closes it."""
    text, length = part
    if length <= limit:
        return part
    if limit <= 0:
        return "", 0
    out, seen, i = [], 0, 0
    while i < len(text) and seen < limit - 1:
        m = _ESCAPE.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(text[i])
        seen += 1
        i += 1
    return "".join(out) + "…\033[0m", limit


def _join(parts: list[tuple[str, int]]) -> tuple[str, int]:
    sep = _paint(" · ", "dim")
    return sep.join(t for t, _ in parts), sum(n for _, n in parts) + 3 * max(len(parts) - 1, 0)


def _line(payload: dict) -> str:
    """The chsum status line: the name on the left, the rewind in the
    centre, the observer on the right. The rewind side holds the handoff's
    phase, or else the rewind to the cursor, then what the session's
    rewinds saved. The observer side holds the base's name, its activity
    where a base is live, and its cache. A line wider than COLUMNS - 4
    drops the saved total, then the base's name, then clips the cursor's
    words, then the observer's activity, then the rewind. '' where
    neither side has anything to show."""
    try:
        width = int(os.environ.get("COLUMNS") or 0) - _INSET
    except ValueError:
        width = 0
    now = time.time()
    observer = _observer_parts(payload, now) or {}
    try:
        rewind = _rewind_parts(payload, now)
    except Exception:  # noqa: BLE001 — a failed rewind part leaves the observer's part standing
        rewind = {"event": None, "mid": None, "cursor": None, "saved": None}
    shown = rewind["event"] or rewind["mid"]
    cursor = rewind["cursor"] if not rewind["event"] else None
    name = observer.get("state", {}).get("name")
    if name and observer.get("others"):
        name += f" +{observer['others']}"
    named = (_paint(name, "dim"), len(name)) if name else None
    activity, gauge = observer.get("mid"), observer.get("gauge")
    if not (shown or rewind["saved"] or gauge):
        return ""
    brand = "chsum"
    left = (_paint(brand, "name"), len(brand))
    mid = _join([x for x in (shown, rewind["saved"]) if x])
    right = _join([x for x in (named, activity, gauge) if x])
    over = lambda: width > 0 and left[1] + mid[1] + right[1] + 4 > width
    if over() and rewind["saved"]:
        mid = _join([shown] if shown else [])
    if over() and named:
        right = _join([x for x in (activity, gauge) if x])
    if over() and cursor:
        words, tail, key = cursor
        room = width - left[1] - right[1] - 4 - len(tail)
        if room >= 14:
            head = _clip((words, len(words)), room)
            mid = (_paint(head[0] + tail, key), head[1] + len(tail))
    if over() and activity:
        room = width - left[1] - mid[1] - 4 - (gauge[1] + 3 if gauge else 0)
        right = _join([x for x in (_clip(activity, max(room, 0)), gauge) if x and x[1]])
    if over():
        mid = _clip(mid, width - left[1] - right[1] - 4)
    return _spread(left, mid, right, width)


# --- command ----------------------------------------------------------------

def _status(state: dict) -> int:
    print(f"observer {state['name']}  ({state['root']})")
    skip = f", skipping {' | '.join(state['skip'])}" if state.get("skip") else ""
    print(f"watched  {' | '.join(state['match'])}{skip}  ({len(_watched(state))} files)")
    print(f"model    {state['model']}")
    try:
        skill_now = _sha(_skill_text(state).encode())
    except OSError:
        skill_now = ""
    note = "" if skill_now == state.get("skill_sha") else \
        "  — changed since the base was built; `reload` carries the new text"
    sskip = f", skipping {' | '.join(state['skill_skip'])}" if state.get("skill_skip") else ""
    print(f"skill    {state['skill']}  ({len(_skill_files(state))} files matching "
          f"{' | '.join(state['skill_match'])}{sskip}){note}")
    print(f"base     {state['sid']}")
    print(f"held     {_ref(state)} at {state['held'][:12]}")
    hits = list(core.PROJECTS_ROOT.glob(f"*/{state['sid']}.jsonl"))
    if hits:
        records = list(core._records(hits[0]))
        row = core._cache_row(records, time.time(), state["sid"])
        turns = sum(1 for r in records if r.get("type") == "user")
        if row:
            print(f"cache    {core._cache_state(row)}, prefix {row['prefix']:,}, {turns} turns")
    behind = _changes(state, state["held"], _tree(state))
    print("behind   " + (", ".join(f"{rel} ({s})" for s, rel in behind) if behind else
                         "none: the base holds the watched files as they stand"))
    growth = _growth([_base_view(state, time.time())])
    if growth:
        print(f"growth   {growth}")
    for edit in _open_edits(state):
        print(f"edit #{edit['number']}  {edit.get('title') or edit['rel']}  {_phase(edit)}  fork "
              f"{session_ref(edit['fork']) if edit['fork'] else 'starting'}, "
              f"{_ago(time.time() - edit['created'])} old")
    return 0


def cmd_observe(args) -> int:
    if args.action == "line":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        print(_line(payload))
        return 0
    if args.action == "hold":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            raise SystemExit("hold reads one hook payload as JSON on stdin") from None
        if not isinstance(payload, dict):
            raise SystemExit("hold reads one hook payload as JSON on stdin")
        return hold_call(payload, "" if args.root == "." else args.root, args.name)
    root = _resolve_root(args.root)
    _compile_all(args.match + args.skip + args.skill_match + args.skill_skip)
    try:
        if args.action == "preview" and not (args.skill or args.match):
            state = dict(_load(root, args.name))
            for key in ("skill_match", "skill_skip", "match", "skip"):
                if getattr(args, key):
                    state[key] = getattr(args, key)
            return _preview(state, args.full)
        if args.action in ("start", "preview"):
            if not (args.skill and args.match and (args.model or args.action == "preview")):
                raise SystemExit(f"{args.action} takes --skill, --match and --model")
            skill = str(pathlib.Path(args.skill).expanduser().resolve())
            if not pathlib.Path(skill).is_dir():
                raise SystemExit(f"no such skill directory: {skill}")
            name = args.name or pathlib.Path(skill).name
            if _state_path(root, name).exists():
                raise SystemExit(f"{root} already has an observer named {name}; "
                                 f"`chsum observe reload` rebuilds it, `chsum observe stop` drops it")
            state = {"root": root, "name": name, "skill": skill,
                     "skill_match": args.skill_match or [".*"], "skill_skip": args.skill_skip,
                     "match": args.match, "skip": args.skip, "model": args.model}
            if not _skill_files(state):
                raise SystemExit(f"no file in {skill} matches {state['skill_match']}")
            return _preview(state, args.full) if args.action == "preview" else _build(state)
        state = _load(root, args.name)
        if args.action == "show":
            if args.edit is None:
                return _list_edits(state)
            return _show_edit(state, args.edit, args.follow)
        if args.action in ("fork", "close", "end", "ended"):
            if args.edit is None or not str(args.edit).isdigit():
                raise SystemExit(f"{args.action} takes the edit number")
            args.edit = int(args.edit)
            if args.action == "fork":
                return _fork(state, args.edit)
            if args.action == "close":
                return _close(state, args.edit, args.title, args.passed)
            if args.action == "end":
                return _end(state, args.edit, _CLOSE_DELAY)
            return _ended(state, args.edit)
        if args.action == "ping":
            return _ping(state)
        if args.action == "log":
            return _log(state)
        if args.action == "advance":
            with _Lock(state):
                err = _advance(state)
            if err:
                print(f"the base did not advance: {err}", file=sys.stderr)
                return 1
            return 0
        if args.action == "stop":
            _drop(state, args.keep_history)
            print(f"dropped observer {state['name']} (base {state['sid']}); its history at "
                  f"{_path(state, '.git')} " + ("kept" if args.keep_history else "deleted"))
            return 0
        if args.action == "reload":
            for edit in _open_edits(state):
                for verb in ("stop", "rm") if edit.get("fork") else ():
                    subprocess.run(["claude", verb, edit["fork"]], capture_output=True, timeout=60)
                edit.update(status="closed", ended=time.time())
                _event(edit, "dropped")
                _save_edit(state, edit)
                _record_drop(state, edit)
            state = _load(root, args.name)
            print(f"dropped base {state['sid']}; its history and edit count carry on")
            fresh = {k: state.get(k) or [] for k in ("skill_skip", "skip")}
            fresh.update({k: state[k] for k in ("root", "name", "skill", "skill_match", "match", "model")})
            fresh.update(held=state.get("held") or "", next_edit=state.get("next_edit") or 1)
            for key in ("skill_match", "skill_skip", "match", "skip"):
                if getattr(args, key):
                    fresh[key] = getattr(args, key)
            if args.model:
                fresh["model"] = args.model
            return _build(fresh)
        if args.follow:
            return _follow_all(state)
        return _status(state)
    except _GitError as e:
        raise SystemExit(f"observe: {e}") from None


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "observe", parents=parents,
        help="hold the watched files under a directory in a base conversation that reviews "
             "every write to them before it lands",
        description="`start` builds a base conversation with a skill directory's "
                    "files as its system prompt and every file under --root a --match regex "
                    "fully matches, and no --skip regex removes, as its first turn, "
                    "and holds that state on refs/chsum/observe/<name> in a git "
                    "directory of its own under the chsum data directory. A call a "
                    "hook passes to `hold` is reviewed by a background session forked "
                    "from the base, which messages the calling session and on an accept "
                    "carries out the agreed version. A change made out of band is "
                    "carried to the "
                    "base as a diff. "
                    "`hold` reads one PreToolUse hook payload on stdin, holds that call "
                    "for review, and prints the edit and a deny reason as JSON for the "
                    "calling hook to return; the fork's tools come from allowed-tools.txt "
                    "in the skill directory. "
                    "`preview` prints what `start` would send, with no model call. "
                    "`reload` rebuilds the base from the files as they stand; `stop` "
                    "drops it and deletes its git directory unless --keep-history.")
    p.add_argument("action", choices=["start", "preview", "status", "reload", "stop", "hold", "line", "ping",
                                      "advance", "fork", "close", "end", "ended", "log",
                                      "show"],
                   nargs="?", default="status")
    p.add_argument("edit", nargs="?", default=None,
                   help="show: the edit's name or number; propose, apply, close: its number")
    p.add_argument("--follow", action="store_true",
                   help="show: print each new message and phase until the edit closes; "
                        "with no action: every action of the observer, until interrupted")
    p.add_argument("--root", default=".",
                   help="the directory the --match paths are relative to; any directory, "
                        "in a git repository or not (default: .)")
    p.add_argument("--name", default="",
                   help="the observer's name (default at start: the skill directory's name)")
    p.add_argument("--skill", default="",
                   help="the directory whose files form the base's system prompt")
    p.add_argument("--skill-match", action="append", default=[], metavar="REGEX",
                   help="which skill files, by full match on the skill-relative path, "
                        r"e.g. '.*\.md'; repeatable (default: every file)")
    p.add_argument("--match", action="append", default=[], metavar="REGEX",
                   help="which root files to watch, by full match on the root-relative "
                        r"path, e.g. 'docs/landscapes/.*\.landscape'; repeatable")
    p.add_argument("--skill-skip", action="append", default=[], metavar="REGEX",
                   help="skill files or directories to leave out, by full match; repeatable")
    p.add_argument("--skip", action="append", default=[], metavar="REGEX",
                   help="root files or directories to leave out, by full match on the "
                        "root-relative path; a matched directory is never walked, "
                        "e.g. '(.*/)?node_modules'; repeatable")
    p.add_argument("--model", default="", help="the base's model, e.g. opus")
    p.add_argument("--title", default="", help="close: the edit's name, two to four words")
    p.add_argument("--pass", dest="passed", action="store_true",
                   help="close: the held call passes, and the writing session's rerun of "
                        "it, unchanged, runs once")
    p.add_argument("--full", action="store_true",
                   help="preview: print the system prompt and first message as sent")
    p.add_argument("--keep-history", action="store_true",
                   help="stop: keep the observer's git directory and its ref's history")
    p.set_defaults(func=cmd_observe)
