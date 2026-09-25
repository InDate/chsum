#!/usr/bin/env python3
"""chsum — coding-agent conversations as work logs and reload-ready context.

Every line of output is copied verbatim or computed, never invented. Prose
generation lives behind the `Summariser` seam and prints beneath the verbatim
record, labelled model-written.

Which formats are read, and how each reaches the record shape every function
below expects, is `chsum.sources`. Nothing here names a format.

Commands: sessions (default), last, find, digest, mark.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import concurrent.futures
import dataclasses
import functools
import hashlib
import importlib.metadata
import html
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
try:
    import fcntl
except ImportError:  # Windows: the checkpoint runs unserialised, as it did before
    fcntl = None
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import checkpoints, sources
from .checkpoints import (  # the git layer: one commit per call that wrote
    GATE_NAME, LOCK_NAME, STATE_NAME, _base_of, _chain_shas, _checkpoint_diff_files,
    _checkpoint_numstat, _checkpoint_refs, _checkpoint_shas, _commit_subject,
    _enabled, _git, _git_dir, _head_sha, _hold_checkpoint_lock, _migrate_chain,
    _ref_tip, _self_heal, _tree_of, _write_checkpoint, checkpoint_ref)
from .sources.claude import PROJECTS_ROOT, transcript_cwd as _transcript_cwd


def _data_home() -> pathlib.Path:
    """chsum's own state, in a root of its own: `~/.claude` holds what Claude
    Code wrote, and nothing here is read by Claude Code. Digests, names and turn
    breakdowns are all data, so the XDG data home is the one directory they sit
    in. macOS takes `~/.local/share` as well: a drill-down line is a `sed`
    command naming a path, and the space in `Application Support` splits one."""
    explicit = os.environ.get("CHSUM_DIR")
    if explicit:
        return pathlib.Path(explicit)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return pathlib.Path(xdg) / "chsum"
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local:
        return pathlib.Path(local) / "chsum"
    return pathlib.Path.home() / ".local" / "share" / "chsum"


CHSUM_DIR = _data_home()
DIGEST_DIR = CHSUM_DIR / "digests"
TURNS_DIR = CHSUM_DIR / "turns"


def _migrate_store() -> None:
    """The store moved from `~/.chsum` to the data home. The move runs where the
    new directory is absent, and no read falls back to the old path afterwards —
    the files move once, so a run reads one store rather than two."""
    if os.environ.get("CHSUM_DIR"):
        return  # an explicit path is the caller's own, and this moves nothing
    old = pathlib.Path.home() / ".chsum"
    if CHSUM_DIR.exists() or not old.is_dir():
        return
    CHSUM_DIR.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(old, CHSUM_DIR)
    except OSError:
        # A rename crosses no device boundary; a copy-then-delete does.
        try:
            shutil.move(str(old), str(CHSUM_DIR))
        except (OSError, shutil.Error) as e:
            print(f"chsum: {old} did not move to {CHSUM_DIR}: {e}", file=sys.stderr)
            raise SystemExit(1)
    print(f"chsum: store moved from {old} to {CHSUM_DIR}", file=sys.stderr)


# ---------------------------------------------------------------------------
# --debug trace
# ---------------------------------------------------------------------------
# What a run read, ran, and resolved — enough to reach the record behind a line
# that looked wrong, without carrying any transcript text. Off unless `--debug`
# is passed: every method returns on `self.on`, so the normal path costs one
# attribute read per call site.

_TRACE_KEEP = 3    # instances held at each end of a step name before the middle collapses
_TRACE_ROWS = 24   # files (and subprocesses) listed before the rest become a count


@dataclass
class _Trace:
    on: bool = False
    argv: list[str] = field(default_factory=list)
    files: dict[str, dict] = field(default_factory=dict)
    procs: list[tuple] = field(default_factory=list)
    steps: list[tuple[str, dict]] = field(default_factory=list)
    repro: list[str] = field(default_factory=list)

    def reset(self, on: bool, argv: list[str]) -> None:
        self.on, self.argv = on, list(argv)
        self.files, self.procs, self.steps, self.repro = {}, [], [], []

    def file(self, path, role: str, records: int | None = None) -> None:
        """One transcript or sidecar the run opened. Deduped by path and roles
        accumulate: the same file read twice for different reasons is one line."""
        if not self.on or path is None:
            return
        key = str(path)
        entry = self.files.setdefault(key, {"path": pathlib.Path(path), "roles": [], "records": None})
        if role not in entry["roles"]:
            entry["roles"].append(role)
        if records is not None:
            entry["records"] = records

    def proc(self, argv, code, seconds: float, extra: str = "") -> None:
        if not self.on:
            return
        self.procs.append((list(argv), code, seconds, extra))

    def step(self, name: str, **fields) -> None:
        if not self.on:
            return
        self.steps.append((name, fields))

    def reproduce(self, line: str) -> None:
        if not self.on or line in self.repro:
            return
        self.repro.append(line)

    # -- rendering ---------------------------------------------------------

    @staticmethod
    def _short(path: pathlib.Path) -> str:
        """Relative to the projects root where it sits under one — every row would
        otherwise repeat the same 40-character prefix, at the title's expense."""
        text = str(path)
        for root, prefix in ((str(PROJECTS_ROOT), "…/"), (str(pathlib.Path.home()), "~/")):
            if text.startswith(root + "/"):
                return prefix + text[len(root) + 1:]
        return text

    @staticmethod
    def _val(v) -> str:
        """`key=value` splits on whitespace, so only a value carrying whitespace
        is quoted. Not `_yaml`, whose `ensure_ascii` would escape the arrows and
        the ⚑ these lines copy out of the transcript."""
        text = str(v)
        return f'"{text}"' if (not text or any(c.isspace() for c in text)) else text

    @staticmethod
    def _size(n: int) -> str:
        for unit, div in (("M", 1 << 20), ("K", 1 << 10)):
            if n >= div:
                return f"{n / div:.1f}{unit}"
        return f"{n}B"

    def _file_rows(self) -> list[tuple[str, ...]]:
        rows = []
        for entry in self.files.values():
            path = entry["path"]
            try:
                size = self._size(path.stat().st_size)
            except OSError:
                size = "gone"
            parent = path.parent.name != "subagents" and not path.name.startswith("agent-")
            ref = ch_ref_for_path(path) if parent else "—"
            recs = "" if entry["records"] is None else _plural(entry["records"], "rec")
            rows.append((ref, ",".join(entry["roles"]), size, recs, self._short(path)))
        return rows

    def _step_lines(self) -> list[str]:
        """Repeated steps collapse in the middle: a recap makes hundreds of
        `_run_chunk` calls and all of them would bury the six that differ."""
        seen: dict[str, int] = defaultdict(int)
        for name, _ in self.steps:
            seen[name] += 1
        kept: dict[str, int] = defaultdict(int)
        out, held = [], []
        for name, fields in self.steps:
            kept[name] += 1
            nth, total = kept[name], seen[name]
            if total > _TRACE_KEEP * 2 and _TRACE_KEEP < nth <= total - _TRACE_KEEP:
                if nth == _TRACE_KEEP + 1:
                    held.append((len(out), name, total - _TRACE_KEEP * 2))
                    out.append("")
                continue
            body = " ".join(f"{k}={self._val(v)}" for k, v in fields.items())
            out.append(f"  {name}{'  ' + body if body else ''}")
        for idx, name, n in held:
            out[idx] = f"  … {n} further {name}"
        return out

    def render(self, exit_code: int) -> str:
        rows = self._file_rows()
        shown, dropped = rows[:_TRACE_ROWS], max(0, len(rows) - _TRACE_ROWS)
        widths = [max((len(r[i]) for r in shown), default=0) for i in range(4)]
        home = session_home()
        lines = ["--- chsum debug ---",
                 f"invocation: {shlex.join(self.argv)}",
                 f"cwd: {self._short(pathlib.Path.cwd())}",
                 f"projects: {self._short(PROJECTS_ROOT)}/  (…/ below)",
                 f"{_build_line()} · exit {exit_code}",
                 f"files ({len(rows)})"]
        # Only when they differ: the divergence is the bug this run might be
        # hitting, and naming it when there is none is noise.
        if home and home != str(pathlib.Path.cwd()):
            lines.insert(3, f"session started in: {self._short(pathlib.Path(home))}")
        for r in shown:
            cols = "  ".join(c.ljust(w) for c, w in zip(r[:4], widths))
            lines.append(f"  {cols}  {r[4]}")
        if dropped:
            lines.append(f"  …and {dropped} more")
        lines.append(f"procs ({len(self.procs)})")
        for argv, code, seconds, extra in self.procs[:_TRACE_ROWS]:
            tail = f"  ({extra})" if extra else ""
            lines.append(f"  {code}  {seconds:5.2f}s  {_clip_line(shlex.join(argv), 140)}{tail}")
        if len(self.procs) > _TRACE_ROWS:
            lines.append(f"  …and {len(self.procs) - _TRACE_ROWS} more")
        lines.append(f"steps ({len(self.steps)})")
        lines.extend(self._step_lines())
        if self.repro:
            lines.append("reproduce")
            lines.extend(f"  {line}" for line in self.repro)
        lines.append("--- end chsum debug ---")
        return "\n".join(lines)


TRACE = _Trace()
checkpoints.set_tracer(TRACE)


def _build_line() -> str:
    """Which chsum ran. The commit resolves because the install is editable —
    the package in the checkout is what executes."""
    try:
        dist = importlib.metadata.version("chsum")
    except importlib.metadata.PackageNotFoundError:
        dist = ""
    # The checkout root, one level above this package. `pyproject.toml` and the
    # git directory both sit there, and a lookup starting inside the package
    # reaches neither — the version then falls back to the installed metadata,
    # which an editable install froze, and the commit goes missing.
    here = pathlib.Path(__file__).resolve().parent.parent
    # The checkout's own pyproject outranks the installed metadata, which an
    # editable install froze at install time: `chsum.py` here is what ran.
    # Regex rather than tomllib, which is 3.11+ and this file targets lower.
    src = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read_pyproject(here))
    version = src.group(1) if src else (dist or "unknown")
    # Names the remedy, not just the mismatch. An editable install freezes the
    # packaged metadata at install time, so it stops describing what runs and
    # nothing refreshes it on its own — a reader who is told only that the two
    # disagree has to work out that a reinstall is the fix.
    stale = (f" · packaged metadata says {dist} and no longer describes what runs;"
             f" `pipx install --editable . --force` from the checkout refreshes it"
             if dist and src and dist != src.group(1) else "")
    build = ""
    try:
        rev = subprocess.run(["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(here), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        if rev.returncode == 0:
            build = f" ({rev.stdout.strip()}{' dirty' if dirty.stdout.strip() else ''})"
    except (OSError, subprocess.SubprocessError):
        build = ""
    py = ".".join(str(n) for n in sys.version_info[:3])
    # The commit belongs beside the version it built, not beside the stale note.
    return f"chsum {version}{build}{stale} · python {py} · {sys.platform}"


def _read_pyproject(here: pathlib.Path) -> str:
    try:
        return (here / "pyproject.toml").read_text(errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# ch_ ref derivation
# ---------------------------------------------------------------------------
# Reimplements claude-history's AgentConversationRef::from_parts (refs.rs:31-54):
# length-prefixed 128-bit FNV-1a over ["agent-v1", project_dir_name, session_filename].

_FNV_OFFSET = 0x6C62272E07BB014262B821756295C58D
_FNV_PRIME = 0x0000000001000000000000000000013B
_MASK = (1 << 128) - 1


def _digest_parts(parts) -> int:
    h = _FNV_OFFSET
    for part in parts:
        b = part.encode()
        for byte in len(b).to_bytes(8, "little"):
            h = ((h ^ byte) * _FNV_PRIME) & _MASK
        for byte in b:
            h = ((h ^ byte) * _FNV_PRIME) & _MASK
    return h


def ch_ref_for_path(path: pathlib.Path) -> str:
    """Full 32-hex ref. Always the full digest: the 12-hex form claude-history
    emits is corpus-dependent and gets extended to stay unambiguous."""
    return f"ch_{_digest_parts(['agent-v1', path.parent.name, path.name]):032x}"


def project_dir_name(cwd: pathlib.Path) -> str:
    """claude-history's convert_path_to_project_dir_name (src/history/path.rs:10-21)."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(cwd))


# `_transcript_cwd` is imported from `sources.claude`, where it sits beside the
# tree it walks.


@functools.cache
def _session_transcript() -> pathlib.Path | None:
    """The conversation this run sits inside, found by its id rather than by the
    directory the run happens to start in. A session keeps its transcript where
    it began: Claude Code files it under the launch directory's slug and never
    re-files it when the session cd's, so the cwd of the moment is not where the
    conversation lives — assuming otherwise is what made `chsum note` die a
    folder in, and every listing report the wrong project as empty.

    None outside a session, where there is no conversation to find. Cached for
    the process: chsum runs on demand and exits, and the answer cannot change
    under it."""
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not live:
        return None
    # cwd first: the ordinary case, and it settles the rare id filed under more
    # than one project (a copied store), where mtime alone would be a guess.
    local = PROJECTS_ROOT / project_dir_name(pathlib.Path.cwd()) / f"{live}.jsonl"
    if local.exists():
        return local
    hits = sorted(PROJECTS_ROOT.glob(f"*/{live}.jsonl"),
                  key=lambda q: q.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def session_project_dir() -> str:
    """The store key and transcript directory a run reads: the session's own
    project where a session is running, the cwd's slug where none is — a plain
    shell browsing past work has nothing else to go on, and there scoping by
    directory is the right answer rather than a fallback."""
    path = _session_transcript()
    return path.parent.name if path else project_dir_name(pathlib.Path.cwd())


def session_home() -> str:
    """The directory the running session started in, empty outside a session.
    Read from the `cwd` a record carries, since the parent directory's slug
    joins path segments on `-` and cannot be split back where a segment holds
    one."""
    path = _session_transcript()
    return _transcript_cwd(path) if path else ""


def _no_conversation() -> str:
    """Why a run found nothing, and what to do about it — the bare sentence sent
    every reader hunting the wrong cause."""
    cwd = pathlib.Path.cwd()
    out = ["no conversation found for this project",
           f"  ran in:   {cwd}",
           f"  searched: {PROJECTS_ROOT / project_dir_name(cwd)}", ""]
    if os.environ.get("CLAUDE_CODE_SESSION_ID", ""):
        out.append("this session is running, but its transcript is not filed "
                   "under any project — nothing to annotate.")
    else:
        out.append("no session is running here, so chsum scopes to the working "
                   "directory. cd to one whose sessions you want, or pass --file.")
    return "\n".join(out)


def _project_dirs(all_projects: bool) -> list[str]:
    """The store directories a listing reads: this project's alone, or every
    project the store holds. One name per directory, in the same slug the
    transcript tree uses, so a directory here keys a transcript there. The
    default scope is the session's project, not the cwd's: a listing run a
    folder in belongs to the same conversation as one run at the root."""
    if not all_projects:
        return [session_project_dir()]
    if not TURNS_DIR.is_dir():
        return []
    return sorted(d.name for d in TURNS_DIR.iterdir() if d.is_dir())


def _scope_label(all_projects: bool) -> str:
    """What an empty listing searched, named the way the flag that widens it is.
    An empty list under the default scope reads as "nothing exists" without it.
    Named for the session's own directory, so an empty listing cannot blame a
    directory the conversation was never filed under."""
    if all_projects:
        return "any project"
    home = session_home()
    return pathlib.Path(home).name if home else pathlib.Path.cwd().name


# Where the store has lived: `~/.chsum`, then `~/.claude/chsum`, now the XDG
# data home. Resolved at import, so `CHSUM_DIR` set in the environment is covered.
_STORE_PROJECT_DIRS = {project_dir_name(d) for d in (
    CHSUM_DIR, pathlib.Path.home() / ".chsum", pathlib.Path.home() / ".claude" / "chsum")}


def transcripts(local: bool = False, source: str = "") -> list[pathlib.Path]:
    """Addressable conversations only: two levels, no agent-* sidecars
    (mirrors claude-history's discover_agent_keys, service.rs:477-486).

    Every registered format is listed, each one filed under the slug of the
    directory its session ran in, so a project holds its sessions whichever
    tool wrote them. `source` narrows the walk to one format by name."""
    out: list[pathlib.Path] = []
    if source in ("", sources.IN_PLACE) and PROJECTS_ROOT.is_dir():
        out = [p for p in PROJECTS_ROOT.glob("*/*.jsonl")
               if not p.name.startswith("agent-")]
    if source != sources.IN_PLACE:
        # Translated once per change and read from disk after that, so the line
        # numbers a drill-down prints name a file that exists.
        out += sources.translated_transcripts(CHSUM_DIR, project_dir_name,
                                              only=source)
    if local:
        want = session_project_dir()
        return sorted(p for p in out if p.parent.name == want)
    # `HaikuSummariser` runs `claude -p` from the store, and Claude Code files
    # each of those under that directory's own project: 1,606 of 1,973
    # transcripts here against 48 for the checkout, all of them chsum's chunk
    # calls rather than conversations you had. Every location the store has
    # used is listed, since the sessions a past one collected stay where they
    # were written. A run from inside one takes the `local` branch above and
    # still reaches them.
    return sorted(p for p in out if p.parent.name not in _STORE_PROJECT_DIRS)


# ---------------------------------------------------------------------------
# claude-history
# ---------------------------------------------------------------------------


class HistoryError(RuntimeError):
    pass


def _history(*args: str, timeout: int = 600, corpus: str = "") -> str:
    """One `claude-history` run. `corpus` names a source whose translated tree
    the binary reads instead of Claude Code's own: the tree is laid out as a
    config directory, and `CLAUDE_CONFIG_DIR` is what selects it, so one binary
    covers every format without knowing a format exists."""
    exe = shutil.which("claude-history")
    if not exe:
        TRACE.step("claude-history", found="no")
        raise HistoryError("claude-history not found on PATH")
    env = None
    if corpus and corpus != sources.IN_PLACE:
        env = {**os.environ, **sources.corpus_env(CHSUM_DIR, corpus)}
    started = time.monotonic()
    proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                          env=env)
    TRACE.proc(["claude-history", *args], proc.returncode, time.monotonic() - started,
               f"{len(proc.stdout)} chars out")
    # Anchored to the start of a line, not searched across the body: a hit's
    # text carries whatever the conversation said, and a conversation that
    # quoted this protocol reads as a rejection under a substring test.
    failed = next((ln for ln in proc.stdout.splitlines()
                   if ln.startswith("protocol agent-error")), "")
    if failed:
        raise HistoryError(f"claude-history rejected {' '.join(args)}: {failed}")
    if proc.returncode != 0:
        raise HistoryError(proc.stderr.strip() or proc.stdout.strip() or "unknown failure")
    return proc.stdout


def _fields(line: str) -> dict:
    """Parse `key=value` tokens from a protocol line, ignoring any trailing `| text`."""
    head = line.split(" | ", 1)[0]
    return dict(t.split("=", 1) for t in head.split() if "=" in t)


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                      re.IGNORECASE)


@dataclass
class Hit:
    ref: str
    uuid: str
    title: str
    score: float = 0.0
    # The first `hit` line under the conversation: where the match sits and the
    # text around it. Without them a row states a conversation and not a reason.
    focus: str = ""
    excerpt: str = ""
    shows_term: bool = False  # the excerpt holds a query term, so it reads as evidence
    source: str = sources.IN_PLACE  # which tool wrote the conversation this hit sits in


@dataclass
class Search:
    """What one `agent search` returned: the conversations, and the warnings the
    run emitted. A warning reports a rank the search could not compute, which
    changes what the scores beside the hits mean."""
    hits: list = field(default_factory=list)
    warnings: list = field(default_factory=list)  # (kind, detail), detail decoded
    # A semantic score reached the ranking. Absent it, every score is a
    # reciprocal rank and the spread measures position, not match strength.
    semantic: bool = False


def _warning_text(kind: str, fields: dict) -> str:
    """A warning's own words. `detail` carries them where a kind raised one
    warning; where it raised several they are folded to a bare `count=`, which
    drops the refs and the reason, so the reason is restored from the kind."""
    said = kind.replace("-", " ")
    detail = urllib.parse.unquote(fields.pop("detail", ""))
    if detail:
        return f"{said} — {detail}"
    count = fields.pop("count", "")
    if kind == "skipped" and count:
        # Raised per transcript that loads but holds no visible messages and no
        # searchable metadata: those conversations were never looked inside.
        return (f"{count} transcripts skipped — empty, or carrying no searchable "
                f"conversation metadata")
    rest = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{said} — {count} {rest}".strip() if count or rest else said


def _warning_text_line(text: str) -> str:
    """A warning as one line: newlines would break a blockquote and a wrap."""
    return " ".join(text.split())


def _text_after_bar(line: str) -> str:
    return line.split(" | ", 1)[1].strip() if " | " in line else ""


def search(query: str, *, local: bool, mode: str, top: int,
           terms: list | None = None) -> Search:
    """Every corpus, one pass each, merged and re-ranked. A score is comparable
    across passes: the same binary computes it the same way over each tree, and
    a pass covers one tool's sessions because that is where those sessions sit.

    A corpus that fails is reported as a warning and the remaining passes stand,
    so one unreadable tree costs its own sessions rather than the search."""
    out = Search()
    hits: list[Hit] = []
    corpora = (sources.IN_PLACE, *sources.translating_names())
    for corpus in corpora:
        try:
            found = _search_one(query, local=local, mode=mode, top=top,
                                terms=terms, corpus=corpus)
        except HistoryError as e:
            out.warnings.append(("corpus", f"{sources.label_of(corpus)} sessions "
                                           f"were not searched: {e}"))
            continue
        hits.extend(found.hits)
        # Named by corpus where more than one was walked: each reports its own
        # passage count, and two unnamed lines read as one fault stated twice.
        tag = f"{sources.label_of(corpus)}: " if len(corpora) > 1 else ""
        for kind, text in found.warnings:
            entry = (kind, f"{tag}{text}")
            if entry not in out.warnings:
                out.warnings.append(entry)
        out.semantic = out.semantic or found.semantic
    # Highest first, then the cut, so the row count the caller asked for holds
    # across the merge rather than being multiplied by the number of corpora.
    hits.sort(key=lambda h: h.score, reverse=True)
    out.hits = hits[:top]
    return out


def _search_one(query: str, *, local: bool, mode: str, top: int,
                terms: list | None, corpus: str) -> Search:
    # `--no-budget`: the default trims the protocol to 6000 characters by
    # dropping records from the tail, and warnings sit last, so a wide search
    # arrived with its warnings already gone. This output is parsed, not read.
    args = ["agent", "search", query, "--top", str(top), f"--{mode}",
            "--no-budget", "--local" if local else "--all"]
    out = Search()
    hit_sources: set = set()
    by_ref: dict = {}
    for line in _history(*args, corpus=corpus).splitlines():
        if line.startswith("conversation "):
            f = _fields(line)
            hit = Hit(ref=f.get("ref", ""), uuid=f.get("uuid", ""),
                      title=_text_after_bar(line) or "(untitled)",
                      score=float(f.get("score") or 0.0), source=corpus)
            by_ref[hit.ref] = hit
            out.hits.append(hit)
        elif line.startswith("hit "):
            f = _fields(line)
            hit_sources.add(f.get("source", ""))
            hit = by_ref.get(f.get("ref", ""))
            if hit is None:
                continue
            text = _text_after_bar(line)
            shows = bool(terms) and any(t.lower() in text.lower() for t in terms)
            # The best-ranked hit comes first and is taken first. A later one
            # replaces it only where it shows a term and the one held does not,
            # so a row states the reason it is in the listing.
            if not hit.excerpt or (shows and not hit.shows_term):
                hit.focus = (f.get("focus") or "").split("..")[0]
                hit.excerpt, hit.shows_term = text, shows
        elif line.startswith("protocol agent-warning"):
            f = _fields(line)
            kind = f.pop("kind", "warning")
            out.warnings.append((kind, _warning_text(kind, f)))
    out.semantic = "semantic" in hit_sources
    return out


@dataclass
class Message:
    n: int
    role: str
    text: str
    # The harness's own verdict that this record is an API error rather than
    # something Claude wrote — `isApiErrorMessage`, the same standing `is_error`
    # has on a tool result. Taken as given; never inferred from the text.
    api_error: bool = False
    line: int = 0  # 1-based line of its first record in the JSONL; 0 when unknown
    # `toolUseResult` of an answer to the question tool: the questions asked,
    # the options offered and the answers given. None on every other message.
    asked: dict | None = None


# ---------------------------------------------------------------------------
# Noise filtering
# ---------------------------------------------------------------------------
# Harness scaffolding in the user role that isn't something the user typed.

_NOISE_MARKERS = (
    "[Request interrupted",
    "<system-reminder",
    "<task-notification",
    "<command-name",
    "<command-message",
    "<local-command-stdout",
    "<local-command-caveat",
    "Caveat: The messages below were generated",
    "[SYSTEM NOTIFICATION",
)

# Matched at the start, unlike `_NOISE_MARKERS`: these read as ordinary prose,
# so a substring test would drop a message that merely discusses one. They carry
# no structural marker at all — `/context`'s output is a `user` record with
# `isMeta` and no `sourceToolUseID`, byte-for-byte the shape of an image paste,
# which is your own text and stays. Text is the only thing that separates them.
_NOISE_PREFIXES = (
    "## Context Usage",  # `/context` writes its report in as a user record
    "[Your previous response had no visible output",
    "The previous response failed to produce a valid tool call",
    "Another Claude session sent a message",
    "The coordinator sent a message",
)


def is_real_prompt(text: str) -> bool:
    t = text.strip()
    if len(t) < 2:
        return False
    if t.startswith(_NOISE_PREFIXES):
        return False
    return not any(m in t for m in _NOISE_MARKERS)


def _origin(rec: dict) -> str:
    """Who put a user record into the conversation: `origin.kind`, or
    `turnOrigin`, the later spelling of the same field. "" on a tool result and
    on records older than both."""
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return str(origin["kind"])
    return str(rec.get("turnOrigin") or "")


def _harness_sent(rec: dict) -> bool:
    """A user record another sender wrote: a peer session, a coordinator, a task
    notification. Records without the field fall through to the text rules."""
    kind = _origin(rec)
    return bool(kind) and kind != "human"


_HANDBACK_LEAD = "The report follows:\n"


def _hand_back(rec: dict) -> tuple[str, str] | None:
    """(agent id, report) for a subagent's report delivered as a peer message.
    The harness indents each report line by two spaces, which is removed here."""
    origin = rec.get("origin")
    if not (isinstance(origin, dict) and origin.get("kind") == "peer"
            and origin.get("handback") and origin.get("from")):
        return None
    body = str(origin.get("body") or "")
    _, lead, report = body.partition(_HANDBACK_LEAD)
    report = report if lead else body
    report = "\n".join(ln.removeprefix("  ") for ln in report.splitlines()).strip()
    return str(origin["from"]), report


def is_typed_prompt(text: str) -> bool:
    """`is_real_prompt`, minus `!` runs — something you did, not something you
    said. Note paths stay on `is_real_prompt`, since notes are typed via `!`."""
    return is_real_prompt(text) and not text.lstrip().startswith("<bash-")


_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _parsed(lines):
    """Every line that parses, skipping the rest — the line-numbered readers hold
    their lines already and would otherwise re-read the file to scan them."""
    for raw in lines:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def _command_prompt_ids(recs) -> frozenset[str]:
    """`promptId` of every record carrying a `<command-name>` marker. A slash
    command lands as two records sharing one id — the command you typed, then
    the body it loaded — and only the id joins them."""
    return frozenset(
        rec["promptId"] for rec in recs
        if isinstance(rec, dict) and rec.get("type") == "user" and rec.get("promptId")
        and "<command-name" in _record_text(rec))


def _tool_injected(rec: dict, command_ids: frozenset[str] = frozenset()) -> bool:
    """A user record loaded on your behalf, not typed by you. Two shapes, both
    structural so no prose is matched to reject them: the Skill tool stamps
    `sourceToolUseID`, and a slash command's body shares its `promptId` with the
    `<command-name>` record. Neither is a turn — the first is Claude's tool call,
    the second is the body of a command you ran."""
    if rec.get("sourceToolUseID"):
        return True
    return bool(rec.get("isMeta")) and rec.get("promptId") in command_ids


# Harness-generated last words — matched (fixed strings), not guessed, so
# "where I left off" reports state, not how the session ended.
_NOTICE_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"^you'?ve hit your (session|usage) limit",
    r"^(claude )?(usage|session) limit reached",
    r"^\[request interrupted",
    r"^api error",
    r"^\[the user (has )?(interrupted|stopped)",
    r"^no response requested",
)]


def notice_kind(text: str) -> str:
    """The notice text itself if this message is a harness notice, else ""."""
    t = text.strip()
    # Length-bounded: a real message that merely quotes a notice isn't one.
    return t if len(t) <= 200 and any(r.match(t) for r in _NOTICE_RES) else ""


def last_said(msgs: list[Message]) -> tuple[Message | None, str]:
    """Last assistant message with content, plus any trailing notice — the
    notice says how the session ended, the message says where the work was."""
    notice = ""
    for m in reversed(msgs):
        if m.role != "assistant" or not m.text.strip():
            continue
        # An API error is how the session stopped, not what Claude last said:
        # "Prompt is too long" quoted as a closing remark reads as a reply.
        if m.api_error:
            notice = notice or m.text.strip()
            continue
        kind = notice_kind(m.text)
        if kind:
            notice = notice or kind
            continue
        return m, notice
    return None, notice


# ---------------------------------------------------------------------------
# Deterministic metadata, straight from the transcript
# ---------------------------------------------------------------------------

_FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
_READ_TOOLS = {"Read"}
_AGENT_TOOLS = {"Agent", "Task"}  # Task is the older name for the same thing


@dataclass
class AgentRun:
    """One subagent, from its sidecar transcript. Kept per-agent as well as merged
    into the parent, so a digest can name each in one line and address the rest."""
    id: str = ""  # sidecar stem minus the agent- prefix; the address
    agent_type: str = ""
    model: str = ""
    description: str = ""  # the task line from the Agent call
    spawn_depth: int = 1
    duration: str = ""
    edited: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    command_total: int = 0  # every Bash call, before the notable filter and dedupe
    path: pathlib.Path | None = None


@dataclass
class Meta:
    uuid: str = ""
    title: str = "(untitled)"  # what to call it: your name if there is one
    ai_title: str = ""  # what Claude Code called it, kept so `--clear` can say
    renamed: bool = False
    project: str = ""
    branch: str = ""
    started: str = ""
    ended: str = ""
    duration: str = ""
    prompts: int = 0  # things you actually typed
    turns: int = 0  # prompts plus answers to the question tool, `_your_turns`' unit
    recapped: int = 0  # turns the store holds a breakdown for
    recapped_at: str = ""  # the newest breakdown's `written`, ISO
    records: int = 0  # raw user/assistant records, mostly tool traffic
    active: int = 0  # seconds of work, excluding idle gaps
    resumed: bool = False  # spans a long break, so `date` alone understates it
    agents: list[AgentRun] = field(default_factory=list)
    spawned: int = 0  # Agent tool calls seen in the parent, sidecar or not
    edited: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    command_total: int = 0  # every Bash call the session and its agents made
    agent_only: set[str] = field(default_factory=set)  # files no parent turn touched
    notes: list[Annotation] = field(default_factory=list)  # kind `note` only, agents' folded in
    path: pathlib.Path | None = None
    source: str = sources.IN_PLACE  # which tool wrote the session, `chsum.sources`

    @property
    def agent_count(self) -> int:
        """Sidecars can be missing or outnumber visible Agent calls, so take the larger."""
        return max(self.spawned, len(self.agents))

    @property
    def date(self) -> str:
        return self.started[:10]

    @property
    def project_name(self) -> str:
        """The last segment of the conversation's `cwd`. An ingested document
        carries a uuid there, which names nothing a reader can search for, so the
        transcript's own directory stands in — that is where the ingest is
        named."""
        name = pathlib.Path(self.project).name if self.project else ""
        if name and not _UUID_RE.fullmatch(name):
            return name
        if self.path is not None:
            return self.path.parent.name
        return name or "?"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
# `chsum name` both records the name in chsum's own store (authoritative, since
# Claude Code's own `ai-title` would otherwise overwrite it) and appends one more
# `ai-title` record so /resume shows it too — the one exception to writing
# nothing to a transcript.

NAMES_PATH = CHSUM_DIR / "names.json"

_names_cache: tuple[float, dict[str, str]] | None = None


def _load_names() -> dict[str, dict]:
    """Raw store: uuid → {"title", "was"}. `was` is Claude Code's title at
    rename time, so `--clear` can restore it."""
    global _names_cache
    try:
        stamp = NAMES_PATH.stat().st_mtime
    except OSError:
        return {}
    if _names_cache and _names_cache[0] == stamp:
        return _names_cache[1]
    try:
        data = json.loads(NAMES_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    store = {}
    for uuid, val in data.items():
        entry = val if isinstance(val, dict) else {"title": val}
        if isinstance(entry.get("title"), str) and entry["title"]:
            store[uuid] = entry
    _names_cache = (stamp, store)
    return store


def load_names() -> dict[str, str]:
    """uuid → your name for it. Cached on mtime — the listing path asks once per session."""
    return {uuid: entry["title"] for uuid, entry in _load_names().items()}


def save_name(uuid: str, title: str | None, was: str = "") -> None:
    """Write-then-rename, so two sessions renaming at once can't leave a torn
    file — the store is the only place a name survives Claude Code's next title."""
    global _names_cache
    store = dict(_load_names())
    if title is None:
        store.pop(uuid, None)
    else:
        # `was` is only the *first* rename's title: renaming twice must still
        # revert to Claude Code's, not to your previous attempt.
        was = store.get(uuid, {}).get("was", was)
        store[uuid] = {"title": title, "was": was}
    NAMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = NAMES_PATH.with_name(NAMES_PATH.name + ".tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(NAMES_PATH)
    _names_cache = None


def title_reaches_resume(path: pathlib.Path) -> bool:
    """True where an appended title reaches the tool that wrote the session.
    Claude Code reads its own `ai-title` records, so a rename there shows up in
    `/resume`. A translated transcript is rebuilt from its original whenever
    that original changes, which drops anything appended to the copy, and the
    tool that wrote the original never reads the copy. The name still holds in
    chsum's own store, which is where every listing reads it."""
    return sources.source_of(path, CHSUM_DIR) == sources.IN_PLACE


def append_ai_title(path: pathlib.Path, title: str) -> None:
    """One more `ai-title` record, byte-identical in shape to Claude Code's own,
    written as a single append. Guards a missing trailing newline: a live
    transcript read mid-flush can lack one, and appending onto it would fuse
    two records into one unparseable line."""
    rec = json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": path.stem},
                     ensure_ascii=False)
    lead = ""
    try:
        if path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                lead = "" if fh.read(1) == b"\n" else "\n"
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{lead}{rec}\n")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
# `chsum note <text>` files into the turn store and prints nothing. The
# transcript is never written to, and nothing reads it for notes.


@dataclass
class _MarkTarget:
    """Where a note points, resolved while you type it: record uuid, row, and
    the sidecar the row is in."""
    uuid: str
    line: int = 0
    agent: str = ""


@dataclass
class Annotation:
    """One annotation as the store holds it and claude-history is served it: a
    `note` a person typed, or a `recap` bullet a model wrote, filed under one
    turn or session file. `id` is the file's uuid and a number issued once."""
    id: str
    n: int
    kind: str  # "note" | "recap" | as received on the wire
    text: str
    targets: list = field(default_factory=list)  # parent rows, int or "a..b"; [] is session-level
    # Stamped by whoever wrote the annotation, RFC 3339. A write that carries
    # neither takes chsum's clock for both; both travel back out on the read.
    created: str = ""
    modified: str = ""
    turn: str = ""  # the turn file's uuid; "" under a session file
    session: str = ""
    agent: str = ""  # sidecar the target sits in — its rows don't order against the parent's
    row: int = 0  # row inside that sidecar
    quote: str = ""  # first line of the targeted record, verbatim
    record: str = ""  # the sentinel record an imported mark came from
    # The file and rows the text was written from, where that is not the
    # conversation itself: {"path": …, "lines": "a..b"}. A sidecar's rows do not
    # order against the parent's, so they travel as a path and not as `targets`.
    origin: dict | None = None


def _record_text(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _mark_file(path: pathlib.Path, agent: str) -> pathlib.Path | None:
    """The file an `agent=` stamp names. Resolved through the parent transcript,
    since a mark read out of a sidecar names its siblings by the parent's ids."""
    root = (path.parent.parent.with_suffix(".jsonl")
            if path.parent.name == "subagents" else path)
    if not agent:
        return root
    return next((s for s, a in mark_sources(root) if a == agent), None)


def _text_at_line(lines: list[str], lineno: int) -> str:
    """Whole text of the record on that line — what `--list --full` shows."""
    if not lineno or lineno > len(lines):
        return ""
    try:
        rec = json.loads(lines[lineno - 1])
    except json.JSONDecodeError:
        return ""
    return _record_text(rec).strip() if isinstance(rec, dict) else ""


def _said_records(path: pathlib.Path | None,
                  data: str = "") -> list[tuple[str, int, pathlib.Path | None, str, str, str]]:
    """Everything either side really said, across the transcript and its sidecars,
    ordered by timestamp: (when, line, file, agent, first line, uuid). `chsum mark`'s
    own plumbing is skipped, since a mark is never a target. Timestamp order because
    two files' line numbers don't order against each other."""
    said = []
    for src, agent in (mark_sources(path) if path else [(None, "")]):
        raw_text = data if src is None or (src == path and data) else src.read_text(errors="replace")
        TRACE.file(src, "said")
        machinery = _Machinery()
        for lineno, raw in enumerate(raw_text.splitlines(), start=1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
                continue
            text = _record_text(rec).strip()
            if not text or not is_real_prompt(text):
                continue
            if machinery.sees(text):
                continue
            said.append((str(rec.get("timestamp") or ""), lineno, src, agent,
                         next((ln for ln in text.splitlines() if ln.strip()), ""),
                         str(rec.get("uuid") or "")))
    said.sort(key=lambda s: (s[0], s[1]))
    return said


def _here_target(path: pathlib.Path) -> _MarkTarget | None:
    """What a bare `chsum mark` points at, fixed while you type it rather than at
    read time: the last real thing said in the files as they stand. Read-time
    resolution can land on a record written after the command ran — one that was
    never on screen when the mark was made."""
    said = _said_records(path)
    if not said:
        TRACE.step("_here_target", said=0, target="none")
        return None
    _ts, lineno, _src, agent, _first, uid = said[-1]
    TRACE.step("_here_target", said=len(said), at=uid[:8], line=lineno,
               agent=agent or "—", when=_ts)
    return _MarkTarget(uid, lineno, agent) if uid else None


IDLE_GAP_SECONDS = 30 * 60


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def active_seconds(stamps: list[str]) -> int:
    """Time worked, not wall-clock — sessions resume days later, so first→last
    overstates badly. Gaps over IDLE_GAP_SECONDS are excluded."""
    times = sorted(t for t in (_parse_ts(s) for s in stamps) if t)
    total = 0
    for a, b in zip(times, times[1:]):
        gap = (b - a).total_seconds()
        if 0 <= gap <= IDLE_GAP_SECONDS:
            total += gap
    return int(total)


def _fmt_secs(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    h, m = divmod(secs // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _records(path: pathlib.Path):
    for line in path.open(errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def subagent_transcripts(path: pathlib.Path) -> list[pathlib.Path]:
    """Sidecars for a session: <project>/<session-uuid>/subagents/agent-*.jsonl."""
    d = path.parent / path.stem / "subagents"
    return sorted(d.glob("agent-*.jsonl")) if d.is_dir() else []


def extract_agent(side: pathlib.Path) -> AgentRun:
    """One sidecar's own totals, plus the task line from its .meta.json sibling."""
    run = AgentRun(id=side.stem.removeprefix("agent-"), path=side)
    sidemeta = side.with_suffix(".meta.json")
    if sidemeta.exists():
        try:
            d = json.loads(sidemeta.read_text(errors="replace"))
        except json.JSONDecodeError:
            d = {}
        if isinstance(d, dict):
            run.agent_type = str(d.get("agentType") or "")
            run.model = str(d.get("model") or "")
            run.description = str(d.get("description") or "")
            run.spawn_depth = int(d.get("spawnDepth") or 1)
    stamps, edited, read, cmds, all_cmds = [], [], [], [], []
    for rec in _records(side):
        if rec.get("timestamp"):
            stamps.append(rec["timestamp"])
        if rec.get("type") in ("user", "assistant"):
            _collect_tools(rec, edited, read, cmds, all_cmds)
    if stamps:
        stamps.sort()
        run.duration = _fmt_secs(active_seconds(stamps))
    run.edited, run.commands = edited, _dedupe(cmds)
    run.command_total = len(all_cmds)
    return run


# ---------------------------------------------------------------------------
# The meta cache
# ---------------------------------------------------------------------------
# `_meta_from_transcript` parses a whole JSONL — 17 ms per session measured over
# 367, so a listing of 15 costs a quarter-second and one of the whole corpus
# costs six. The parse is a pure function of the file's bytes, so its result is
# cached against `(mtime, size)` and re-read instead. `notes`, `recapped` and
# `recapped_at` are excluded, being the store's and not the transcript's.

META_CACHE_PATH = CHSUM_DIR / "meta.json"
_META_UNCACHED = ("notes", "recapped", "recapped_at")
_meta_cache: dict[str, dict] | None = None
_meta_cache_dirty = False


@functools.lru_cache(maxsize=1)
def _meta_schema() -> str:
    """Twelve hex over the three dataclasses' field names *and the package's own
    bytes*, as `_fingerprint` does for a chunk. The field names catch a changed
    shape; the bytes catch a changed rule — the notable-command filter and the
    edit-path filter both feed cached values, and a cache keyed on shape alone
    served their old results after the rule changed. An edit costs one cold
    rebuild, 6s over 367 sessions; a released file never changes.

    Every module, not this file alone: `source` is a cached field and the rule
    computing it sits under `sources/`."""
    names = ",".join(sorted(f.name for f in dataclasses.fields(Meta))
                     + sorted(f.name for f in dataclasses.fields(AgentRun))
                     + sorted(f.name for f in dataclasses.fields(Annotation)))
    here = pathlib.Path(__file__).resolve().parent
    for mod in sorted(here.glob("**/*.py")):
        try:
            names += mod.read_text(errors="replace")
        except OSError:
            pass  # no readable source: the field names still catch a changed shape
    return hashlib.sha256(names.encode()).hexdigest()[:12]


def _meta_key(path: pathlib.Path) -> str:
    """Project directory and stem. A fork copies rows under a new stem, so two
    branches of one conversation hold separate entries."""
    return f"{path.parent.name}/{path.stem}"


def _meta_cache_load() -> dict[str, dict]:
    global _meta_cache
    if _meta_cache is None:
        _meta_cache = {}
        try:
            doc = json.loads(META_CACHE_PATH.read_text())
            if doc.get("schema") == _meta_schema():
                _meta_cache = doc.get("entries") or {}
        except (OSError, json.JSONDecodeError):
            pass  # absent, half-written or from another shape: read the files
        TRACE.step("_meta_cache", entries=len(_meta_cache))
    return _meta_cache


def _meta_encode(meta: Meta) -> dict:
    d = dataclasses.asdict(meta)
    for k in _META_UNCACHED:
        d.pop(k, None)
    d["agent_only"] = sorted(d["agent_only"])
    d["path"] = None  # the key names the file; a stored path would go stale on a move
    for run in d["agents"]:
        run["path"] = str(run["path"]) if run["path"] else ""
    return d


def _meta_decode(d: dict, path: pathlib.Path) -> Meta:
    d = dict(d)
    runs = [AgentRun(**{**r, "path": pathlib.Path(r["path"]) if r.get("path") else None})
            for r in d.pop("agents", [])]
    d["agent_only"] = set(d.get("agent_only") or ())
    return Meta(**{**d, "agents": runs, "path": path})


def _meta_cache_get(path: pathlib.Path) -> Meta | None:
    try:
        st = path.stat()
    except OSError:
        return None
    hit = _meta_cache_load().get(_meta_key(path))
    if not hit or hit.get("mtime") != st.st_mtime or hit.get("size") != st.st_size:
        return None
    try:
        return _meta_decode(hit["meta"], path)
    except (TypeError, KeyError):
        return None  # an entry this build cannot read is a miss, never an error


def _meta_cache_put(path: pathlib.Path, meta: Meta) -> Meta:
    global _meta_cache_dirty
    try:
        st = path.stat()
    except OSError:
        return meta
    _meta_cache_load()[_meta_key(path)] = {
        "mtime": st.st_mtime, "size": st.st_size, "meta": _meta_encode(meta)}
    _meta_cache_dirty = True
    return meta


def _meta_cache_flush() -> None:
    """Written whole to a `.tmp` and moved, so a concurrent read never opens half
    of one. Entries whose transcript is gone are dropped here, which is the one
    pass that already holds every key."""
    if not _meta_cache_dirty or _meta_cache is None:
        return
    live = {k: v for k, v in _meta_cache.items()
            if (PROJECTS_ROOT / k).with_suffix(".jsonl").exists()}
    tmp = META_CACHE_PATH.with_suffix(".tmp")
    try:
        META_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"schema": _meta_schema(), "entries": live}))
        os.replace(tmp, META_CACHE_PATH)
        TRACE.step("_meta_cache_flush", entries=len(live),
                   dropped=len(_meta_cache) - len(live))
    except OSError:
        pass  # a cache that cannot be written costs time, never a run


def _meta_from_transcript(path: pathlib.Path) -> Meta:
    meta = Meta(uuid=path.stem, path=path,
                source=sources.source_of(path, CHSUM_DIR))
    stamps, edited, read, cmds, all_cmds = [], [], [], [], []
    # One read, held: the command ids a body is matched against are only complete
    # once the whole file is seen, and this path stays off a second pass.
    recs = list(_records(path))
    TRACE.file(path, "meta", records=len(recs))
    command_ids = _command_prompt_ids(recs)
    for rec in recs:
        if rec.get("timestamp"):
            stamps.append(rec["timestamp"])
        if rec.get("type") == "ai-title" and rec.get("aiTitle"):
            meta.ai_title = rec["aiTitle"]  # refined over the session; last wins
        if not meta.project and rec.get("cwd"):
            meta.project = rec["cwd"]
        if rec.get("gitBranch"):
            meta.branch = rec["gitBranch"]
        if rec.get("type") in ("user", "assistant"):
            meta.records += 1
            meta.spawned += _collect_tools(rec, edited, read, cmds, all_cmds)
        if rec.get("type") == "user":
            if _is_typed_prompt(rec, command_ids):
                meta.prompts += 1
                meta.turns += 1
            elif not rec.get("isCompactSummary") and _answered(rec):
                meta.turns += 1  # a menu choice is a turn, as in `_your_turns`
    # Yours wins over Claude Code's own later `ai-title` appends.
    named = load_names().get(meta.uuid, "")
    meta.renamed = bool(named)
    meta.title = named or meta.ai_title or "(untitled)"
    own_edits = set(edited)

    # Fold subagent tool use into the parent; prompts stay parent-only.
    meta.agents = [extract_agent(s) for s in subagent_transcripts(path)]
    # Delegated work is the session's work, the same rule the edits and the
    # notable commands already follow.
    meta.command_total = len(all_cmds) + sum(a.command_total for a in meta.agents)
    for run in meta.agents:
        edited.extend(run.edited)
        cmds.extend(run.commands)

    if stamps:
        stamps.sort()
        meta.started, meta.ended = stamps[0], stamps[-1]
        meta.active = active_seconds(stamps)
        meta.duration = _fmt_secs(meta.active)
        meta.resumed = (
            _parse_ts(stamps[-1]) - _parse_ts(stamps[0])
        ).total_seconds() > meta.active + IDLE_GAP_SECONDS if _parse_ts(stamps[0]) else False
    keep = lambda fs: _dedupe(_relpath(f, meta.project) for f in fs if _is_project_file(f))
    meta.edited = keep(edited)
    meta.read = keep(read)
    meta.commands = _dedupe(cmds)
    # Project-relative only once cwd is known, hence here not in extract_agent.
    for run in meta.agents:
        run.edited = keep(run.edited)
    meta.agent_only = set(meta.edited) - set(keep(own_edits))

    return meta


def extract_meta(path: pathlib.Path) -> Meta:
    """The transcript's own figures, then the store's on top. The walk is cached
    against the file's mtime and size; the store's three fields never are —
    `chsum note` changes them without touching the transcript, and a cached note
    count would report a note that was just filed as absent."""
    meta = _meta_cache_get(path) or _meta_cache_put(path, _meta_from_transcript(path))
    # Hand-typed only: a digest is deterministic, and a `recap` bullet is a
    # model's. Agents' notes are in the same files, by the `agent` they carry.
    docs = _load_store(path.parent.name).get(path.stem, [])
    meta.notes = sorted((a for doc in docs for a in _annotations_of(doc) if a.kind == "note"),
                        key=lambda a: a.created)
    # Summaries, not files, as `_recap_start`: a file holding notes alone recaps nothing.
    latest = [e for e in (_latest_summary(doc) for doc in docs) if e]
    meta.recapped = len(latest)
    meta.recapped_at = max((str(e.get("written") or "") for e in latest), default="")
    return meta


# Real work, but not project changes — they crowd out the files that matter.
_NON_PROJECT_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/")
_NON_PROJECT_PARTS = ("/scratchpad/", "/.claude/plans/", "/.claude/projects/")


# Look-only commands — they bury the few that did something.
_INSPECTION_CMDS = {
    "ls", "cat", "head", "tail", "grep", "rg", "find", "echo", "wc", "which",
    "pwd", "cd", "file", "stat", "du", "df", "tree", "sed", "awk", "jq", "sort",
    "uniq", "diff", "less", "more", "printf", "env", "date", "man", "type",
}


# `cd somewhere` before the real work is not a look at anything: 3,936 of 19,751
# Bash calls in the corpus lead with `cd` and exactly one of them is a bare `cd`,
# so judging by that first word dropped 19.9% of every digest's commands.
_CMD_SEP_RE = re.compile(r"&&|\|\||;|\n")


# `NAME=value` with nothing after it — a shell assignment, not a command.
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _command_work(cmd: str) -> str:
    """The first thing a command does, past every leading `cd somewhere` and
    `NAME=value` — those set up what follows and name no work of their own,
    whether they sit on their own line or ahead of the work behind `&&`. A digest
    line clips at 120 characters, and a scratchpad path left on the front spends
    that width on the path instead of the command. "" where every line is setup.

    The first word carries the assignment test unsplit: the basename of
    `F=/a/b.jsonl` is `b.jsonl`, which stops looking like an assignment."""
    for line in cmd.strip().splitlines():
        head = line.strip()
        while head:
            word = head.split()[0]
            if not (word.rsplit("/", 1)[-1] == "cd" or _ASSIGN_RE.match(word)):
                return head
            rest = _CMD_SEP_RE.split(head, maxsplit=1)
            head = rest[1].strip() if len(rest) > 1 else ""
    return ""


def _first_command(cmd: str) -> str:
    """The part of a command worth showing: `cd repo && python3 build.py` is the
    build, and a reader wants to see it. A command that is setup throughout falls
    back to its own first line, so `--commands` lists it rather than a blank."""
    return _command_work(cmd) or next(
        (l.strip() for l in cmd.strip().splitlines() if l.strip()), "")


_HEREDOC_OPEN = re.compile(
    r"""<<(-?)\s*(?:'([^']*)'|"([^"]*)"|(\\?)([A-Za-z0-9_]+))""")


def _heredoc_bodies(cmd: str) -> list[tuple[int, int, str]]:
    """(start, end, tag) for every heredoc body in `cmd`, in source order.

    Each `<<TAG` queues a tag; the queued bodies follow at the next newline,
    one after another, each running to a line equal to its own tag. Quoting is
    carried through the scan because `echo "a << b"` is text and the shell
    opens no body there. A `<<-` tag matches with leading tabs stripped, and
    `<<<` is a here-string with no body at all.

    A body left unterminated runs to the end of the command, which is what the
    shell would report and what a truncated transcript leaves behind."""
    i, n = 0, len(cmd)
    pending: list[tuple[str, bool]] = []
    bodies: list[tuple[int, int, str]] = []
    single = double = False
    while i < n:
        ch = cmd[i]
        if single:
            single = ch != "'"
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if double:
            # `$(` inside a double-quoted run opens a context where quoting
            # starts over. Leaving the run here is coarse and it keeps the
            # scan from reading the substitution's own quotes as closers.
            double = not (ch == '"' or cmd.startswith("$(", i))
            i += 1
            continue
        if ch == "'":
            single = True
        elif ch == '"':
            double = True
        elif ch == "#" and (i == 0 or cmd[i - 1] in " \t\n;|&("):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        elif cmd.startswith("<<<", i):
            i += 3
            continue
        elif cmd.startswith("<<", i):
            m = _HEREDOC_OPEN.match(cmd, i)
            if m:
                pending.append((m.group(2) or m.group(3) or m.group(5), m.group(1) == "-"))
                i = m.end()
                continue
        elif ch == "\n" and pending:
            pos = i + 1
            for tag, dash in pending:
                start = pos
                while True:
                    j = cmd.find("\n", pos)
                    line = cmd[pos:] if j < 0 else cmd[pos:j]
                    if (line.lstrip("\t") if dash else line) == tag:
                        bodies.append((start, pos, tag))
                        pos = n if j < 0 else j + 1
                        break
                    if j < 0:
                        bodies.append((start, n, tag))
                        pos = n
                        break
                    pos = j + 1
            pending = []
            i = pos
            continue
        i += 1
    return bodies


def _write_target(body: str) -> str:
    """The file a Python body writes, where the body's own grammar names it as
    a constant. `open("x", "w")` and `Path("x").write_text(...)` name it
    directly; a name bound once to a string constant carries it too.

    "" where the path is built at runtime, which leaves the mask carrying a
    line count and no file. Anything but Python parses to nothing here."""
    try:
        tree = ast.parse(textwrap.dedent(body))
    except (SyntaxError, ValueError):
        return ""
    const: dict[str, str] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            const[node.targets[0].id] = node.value.value

    def named(arg) -> str:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return const.get(arg.id, "")
        if isinstance(arg, ast.Call):  # pathlib.Path("x") wrapping the name
            return named(arg.args[0]) if arg.args else ""
        return ""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "open" and len(node.args) > 1:
            mode = node.args[1]
            if isinstance(mode, ast.Constant) and set(str(mode.value)) & set("wax"):
                if path := named(node.args[0]):
                    return path
        if isinstance(fn, ast.Attribute) and fn.attr in ("write_text", "write_bytes"):
            if path := named(fn.value):
                return path
    return ""


def _mask_bodies(cmd: str) -> str:
    """`cmd` with every heredoc body replaced by its line count and, for a
    Python body whose grammar names one, the file it writes.

    A body of 300 lines is what forced commands to be clipped at 200
    characters, and that clip cut them mid-word: a command ending `ls -l
    src/config.` carries no fragment a reader can match against the
    transcript, and the file a `python3 - <<'PY'` wrote is named nowhere else
    in the extract. `<<'PY' [38 lines, writes chsum.py]` costs 30 characters
    where the body costs 1,900."""
    bodies = _heredoc_bodies(cmd)
    if not bodies:
        return cmd
    out, at = [], 0
    for start, end, _ in bodies:
        body = cmd[start:end]
        note = f"{body.count(chr(10))} lines"
        if target := _write_target(body):
            note += f", writes {target}"
        out.append(cmd[at:start])
        out.append(f"[{note}]\n")
        at = end
    out.append(cmd[at:])
    return "".join(out)


def _is_notable_command(cmd: str) -> bool:
    """Did this command change something, build, or test?"""
    work = _command_work(cmd)
    if not work:
        # Every line set a variable or changed directory and did no work of its own.
        return False
    first = work.split()[0].rsplit("/", 1)[-1]
    if first in ("sudo", "time", "nohup"):
        parts = work.split()
        first = parts[1].rsplit("/", 1)[-1] if len(parts) > 1 else first
    return first not in _INSPECTION_CMDS


def _is_project_file(path: str) -> bool:
    return not (path.startswith(_NON_PROJECT_PREFIXES)
                or any(p in path for p in _NON_PROJECT_PARTS))


def _relpath(path: str, project: str) -> str:
    if project and path.startswith(project + "/"):
        return path[len(project) + 1:]
    home = str(pathlib.Path.home())
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def _is_typed_prompt(rec: dict, command_ids: frozenset[str] = frozenset()) -> bool:
    """A user record carrying text the human actually wrote. Most user-role records
    are tool_results; the rest is harness scaffolding (interrupts, notifications)
    and `!` runs (see `is_typed_prompt`). `command_ids` comes from the same file
    (`_command_prompt_ids`) — without it a slash command's body still reads as typed."""
    if rec.get("isCompactSummary") or _tool_injected(rec, command_ids):
        return False  # Claude Code's own auto-summary, or a loaded body — not typed
    if _harness_sent(rec):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
    else:
        return False
    return any(is_typed_prompt(t) for t in texts)


def _dedupe(items) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _collect_tools(rec: dict, edited: list, read: list, cmds: list,
                   all_cmds: list | None = None) -> int:
    """Append this record's tool use to the accumulators; return agents spawned.
    `all_cmds` takes every Bash call before the notable filter and the dedupe, so
    the digest can state what its own section left out without a second read."""
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return 0
    spawned = 0
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        name, inp = part.get("name"), part.get("input") or {}
        if name in _AGENT_TOOLS:
            spawned += 1
        elif name in _FILE_TOOLS and isinstance(inp.get("file_path"), str):
            edited.append(inp["file_path"])
        elif name in _READ_TOOLS and isinstance(inp.get("file_path"), str):
            read.append(inp["file_path"])
        elif name == "Bash" and isinstance(inp.get("command"), str):
            whole = inp["command"].strip()
            cmd = _first_command(whole)
            if all_cmds is not None:
                all_cmds.append(cmd)
            if _is_notable_command(whole):
                cmds.append(cmd[:120])
    return spawned


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _yaml(v: str) -> str:
    s = str(v)
    return json.dumps(s) if (":" in s or s.startswith(("[", "{", "#", "*", "&"))) else s


def _plural(n: int, word: str) -> str:
    # A word ending in a consonant plus `y` takes `-ies`, or "reply" prints as
    # "replys" — the only irregular shape any caller here passes.
    if n != 1 and word.endswith("y") and word[-2:-1] not in "aeiou":
        return f"{n} {word[:-1]}ies"
    return f"{n} {word}" + ("" if n == 1 else "s")


def _bullets(items: list[str], limit: int) -> list[str]:
    out = [f"- {_span(i)}" for i in items[:limit]]
    if len(items) > limit:
        out.append(f"- …and {len(items) - limit} more")
    return out


def _timed_bullets(pairs: list[tuple[str, str, str, int]], limit: int) -> list[str]:
    """Same shape as `_bullets`, stamped, located and counted: `pairs` is (when,
    locator, text, runs), the overflow line hand-built because `_bullets` itself
    has no room for the extra columns. An empty locator prints nothing in its
    place. `runs` above 1 is stated rather than collapsed silently: a command is
    shown by its first line, so several different scripts share one bullet."""
    out = [f"- {_hhmm(when)}  " + (f"`{loc}`  " if loc else "") + _span(text)
           + (f"  ×{runs}" if runs > 1 else "")
           for when, loc, text, runs in pairs[:limit]]
    if len(pairs) > limit:
        out.append(f"- …and {len(pairs) - limit} more")
    return out


def _located_bullets(items: list[str], where: dict[str, str], limit: int) -> list[str]:
    """`_bullets` with the row each item was first seen on. Deduplicated lists lose
    the event behind them, so the row is carried alongside rather than recovered."""
    out = [f"- {_span(i)}" + (f"  `{where[i]}`" if where.get(i) else "")
           for i in items[:limit]]
    if len(items) > limit:
        out.append(f"- …and {len(items) - limit} more")
    return out


def _clip(text: str, limit: int, hint: str = "sed the row above") -> str:
    # The hint is a parameter because not every quote sits under a locator —
    # pointing at one that isn't there invites the reader to invent it.
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [+{len(text) - limit} chars, {hint}]"


def _colour_ok(stream=None) -> bool:
    """Off unless the given stream is a terminal — a captured `!` run would store
    escape sequences in the transcript forever. FORCE_COLOR overrides; NO_COLOR always wins."""
    if os.environ.get("NO_COLOR"):
        return False
    stream = sys.stdout if stream is None else stream
    return bool(stream.isatty() or os.environ.get("FORCE_COLOR"))


# Mid-luminance 256-colour codes, legible on a light and on a dark terminal.
# Twelve of them: past twelve projects a colour repeats, which costs a glance,
# where a code near the terminal's own background costs the whole cell.
_PROJECT_COLOURS = (31, 66, 71, 97, 101, 108, 131, 136, 168, 173, 175, 180)


def _project_palette(names) -> dict[str, str]:
    """One escape per project name across a listing. A digest of the name picks
    its place in the palette, not the row's position, so reordering the listing
    leaves the colours where they were. A place already held moves the later
    name to the next free one, so two projects in one listing never share a
    colour; that probe reads the set of names present, so a project whose first
    choice collides here takes a different colour in a listing without the name
    that beat it. Past twelve projects every place is held and a colour repeats.

    Empty where the terminal takes no colour, which leaves the cells unpainted."""
    if not _colour_ok():
        return {}
    taken: set[int] = set()
    out: dict[str, str] = {}
    for name in sorted({n for n in names if n}):
        i = int(hashlib.blake2s(name.encode(), digest_size=4).hexdigest(), 16)
        slot = i % len(_PROJECT_COLOURS)
        for step in range(len(_PROJECT_COLOURS)):
            slot = (i + step) % len(_PROJECT_COLOURS)
            if slot not in taken:
                break
        taken.add(slot)
        out[name] = f"\033[38;5;{_PROJECT_COLOURS[slot]}m"
    return out


def _paint_project(cell: str, name: str, palette: dict[str, str]) -> str:
    """The already-padded cell in its project's colour. Padded first and coloured
    second: an escape sequence counted into a column width shifts every column
    after it by the length of the sequence."""
    colour = palette.get(name, "")
    return f"{colour}{cell}\033[0m" if colour else cell


def _dim(text: str) -> str:
    """Grey for quoted transcript text, so a reason and the message it marks don't
    read as one voice."""
    if not _colour_ok():
        return text
    return f"\033[2m{text}\033[0m"


# Blue against the muted project palette, which holds no blue, so a term never
# reads as a project name.
_TERM_COLOUR = "\033[38;5;39m"
# Red for the `warning:` label alone, so the prose beside it stays readable.
_WARN_COLOUR = "\033[38;5;203m"


def _lit_terms(text: str, terms) -> str:
    """A dimmed line with the query's words picked out. A highlight closes with a
    reset, which closes the dim run with it, so every match reopens the dim
    behind itself and the rest of the line stays grey."""
    if not terms or not _colour_ok():
        return _dim(text)
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    lit = pattern.sub(lambda m: f"\033[0m{_TERM_COLOUR}{m.group(0)}\033[0m\033[2m", text)
    return f"\033[2m{lit}\033[0m"


def _term_window(text: str, terms, limit: int) -> str:
    """The excerpt clipped around its first matching term rather than from the
    start. A hit's preview opens at the message's first character and the term
    that matched often sits past `limit`, which printed a row whose reason for
    being there was cut off. No term in the preview leaves the head, which is
    all the text there is to show."""
    text = " ".join(text.split())
    if not terms:
        return _clip_line(text, limit)
    found = re.search("|".join(re.escape(t) for t in terms), text, re.IGNORECASE)
    if not found or found.start() < limit:
        return _clip_line(text, limit)
    # Opened a little before the match so the term is not the first word, and
    # marked as opening mid-message.
    start = max(0, found.start() - 12)
    return "… " + _clip_line(text[start:], limit - 2)


def _span(text: str) -> str:
    """Transcript text as a code span. The backticks come out of it first: one of
    its own closes the span early, and the rest of the line then reads as markup
    — the forgery `_quote` guards whole text from, which a span has to guard
    too. A command's backtick is shell substitution and survives in the record
    the row points at; here it only breaks the line it sits on."""
    return "`" + text.replace("`", "") + "`"


def _clip_line(text: str, limit: int) -> str:
    """Clip inside one line. `_clip`'s hint is its own line, which would break out
    of a bold run or a blockquote — but the truncation still has to be visible."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _quote(text: str) -> str:
    """Blockquote transcript text. Functional, not decorative: a quoted message
    containing "## Summary" would otherwise forge a section of this document."""
    return "\n".join(f"> {line}" if line.strip() else ">"
                     for line in text.strip().splitlines())


_MODEL_WRITTEN_HEADING = "What happened, in order — model-written"

_MD_QUOTE_RE = re.compile(r"^( *)> ?(.*)$")
_MD_HEADING_RE = re.compile(r"^(#{1,3}) (.*)$")
# Checked before the italic line, which is a prefix of it: `**x**` matched
# `^\*(.+)\*$` with one asterisk left at each end of the group.
_MD_BOLD_LINE_RE = re.compile(r"^( *)\*\*(.+)\*\*$")
_MD_ITALIC_LINE_RE = re.compile(r"^\*(.+)\*$")
_MD_BULLET_RE = re.compile(r"^- (.*)$")
# `4 writes` inside a turn line. Only chsum's own metadata lines are italic —
# transcript text is quoted — so this cannot be forged by a message.
_MD_WRITES_RE = re.compile(r"\b(\d+ writes?)\b")
# `+7515 −0` at the end of a file bullet in the writes view. Anchored to the end
# and requiring both halves, so a bullet whose text merely holds a `+12` is left
# alone. The minus is U+2212, which is what the view prints.
_MD_DIFFSTAT_RE = re.compile(r"(\+\d+) (−\d+)$")
# A row view's head line — `- `id`  `locator`  HH:MM:SS  label`. The label is
# the role on a message row, which is what colours the quote hanging under it;
# the row views carry no heading for `_md_ansi` to read it from.
_MD_ROW_HEAD_RE = re.compile(
    r"^- (?:`[^`]+`  )?`[^`]+`  \d\d:\d\d:\d\d  (\S+)$")
# A bullet holding nothing but one code span — a command or a path.
_CODE_ONLY_RE = re.compile(r"^`[^`]+`$")
# A bullet whose entire content is one code span, optionally led by an HH:MM
# time — the `_bullets`/`_timed_bullets` shape — gets coloured whole instead of
# fighting a wrap boundary that might land inside the backticks.
_MD_BULLET_CODE_RE = re.compile(r"^(?:(\d\d:\d\d)  )?`([^`]+)`$")
# A line ending in a code span, with a head of its own before it — a row view's
# bullet (`id  locator  time  label  `text``) or a label naming the command it
# runs. The span is left unwrapped for the same reason `_CODE_ONLY_RE` is: see
# the bullet branch.
_MD_TAIL_CODE_RE = re.compile(r"`[^`]+`$")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_INLINE_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def _diffstat_lit(text: str, reopen: str) -> str:
    """`+N` green and `−N` red where a line ends in a diffstat pair, the rest
    left as it was. `reopen` restores the run the line was drawn in."""
    return _MD_DIFFSTAT_RE.sub(
        lambda m: f"{_ANSI_ADDED}{m.group(1)}{_ANSI_OFF}{reopen} "
                  f"{_ANSI_REMOVED}{m.group(2)}{_ANSI_OFF}{reopen}", text)


def _writes_lit(text: str, reopen: str) -> str:
    """The `N writes` run in its own colour, the rest of the line left as it
    was. `reopen` restores the run the line was being drawn in, so the colour
    ends at the segment rather than running to the end of the line."""
    # `_ANSI_OFF` first: a turn line is drawn dim, and dim stays on under a
    # colour set over it, which is what made the orange read as burnt.
    return _MD_WRITES_RE.sub(
        lambda m: f"{_ANSI_OFF}{_ANSI_WROTE}{m.group(1)}{_ANSI_OFF}{reopen}", text)


# The runs a whole line can be styled with, named so a branch can hand its own
# run to `_md_inline` to reopen — a backslash cannot appear inside an f-string
# expression before 3.12, and chsum runs on 3.10.
# Bright orange for what changed the tree: 256-colour, since the sixteen basic
# ones are already spent on projects, prompts, replies and code spans.
_ANSI_WROTE = "\033[1;38;5;214m"
_ANSI_ADDED = "\033[32m"    # lines a write added
_ANSI_REMOVED = "\033[31m"  # lines it removed
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_OFF = "\033[0m"


def _md_inline(segment: str, reopen: str = "") -> str:
    """`code`/`**bold**` styling for one already-wrapped segment. The regexes
    only see this segment, so a pair split across a wrap boundary just leaves
    its marker literal on both sides — total, never raises (see `_md_ansi`).

    `reopen` is the run this segment sits inside — a heading's bold, an italic
    line's dim. An inline style closes with a reset, and that reset closes the
    surrounding run with it, so each match reopens the run behind itself and the
    rest of the line keeps the style it was given. Same guard as `_lit_terms`.
    Without it a line styled as a whole could not carry a code span at all: the
    backticks printed literally, since nothing rendered them."""
    segment = _MD_INLINE_CODE_RE.sub(
        lambda m: f"\033[36m{m.group(1)}\033[0m{reopen}", segment)
    segment = _MD_INLINE_BOLD_RE.sub(
        lambda m: f"\033[1m{m.group(1)}\033[0m{reopen}", segment)
    return segment


# Stands in for a space inside a code span while a line is wrapped. Not
# whitespace, so `textwrap` cannot break on it; one character wide, so the
# column math is the same as the space it replaces.
_SPAN_SPACE = "\x00"


def _wrap(text: str, cols: int, **kw) -> list[str]:
    """`textwrap.wrap` that never splits a word, nor a code span. A rendered
    document is mostly paths, refs and commands; broken across a line one stops
    being copyable, and an over-long line costs a soft wrap the terminal does
    anyway.

    A span is held together because `_md_inline` colours one line at a time: a
    span split by the wrap matches on neither side, so its backticks print
    literally and the run is not coloured at all. Held whole it overflows the
    line instead, which is the trade every other long token here takes."""
    held = _MD_INLINE_CODE_RE.sub(
        lambda m: m.group(0).replace(" ", _SPAN_SPACE), text)
    return [line.replace(_SPAN_SPACE, " ")
            for line in textwrap.wrap(held, cols, break_long_words=False,
                                      break_on_hyphens=False, **kw)]


def _md_ansi(text: str) -> str:
    """Presentation-only markdown→ANSI for catch-up on a tty; the piped/captured
    document stays exact markdown. Line-based: an unmatched line passes through
    unchanged, so it can never raise on transcript text. Quote lines are matched
    first and exclusively, the same forgery guard as `_quote`. Lines are wrapped
    as plain text before colouring, since an ANSI escape would throw off
    `textwrap`'s width math."""
    width = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
    out = []
    # Whose words the current blockquote is, tracked from the section heading
    # above it (quoting is identical markup either way). Green for yours, dim for Claude's.
    mine = False
    # Markdown has no indentation, so on screen a heading and its body sat at the
    # same column and nothing showed which belonged to which. The body of a `##`
    # is indented two, a `###` four; the heading itself stays at its parent's.
    pad = ""
    cols = width

    def emit(text: str) -> None:
        out.append(pad + text if text.strip() else text)

    def emit_all(texts) -> None:
        for t in texts:
            emit(t)

    for line in text.split("\n"):
        if not line.strip():
            emit(line)
            continue
        m = _MD_QUOTE_RE.match(line)
        if m:
            # The bar rides every continuation, initial and subsequent alike —
            # a folded quote that lost it on line two would read as prose.
            colour = "\033[32m" if mine else "\033[2m"
            # An indent in the source belongs to the list item above, and the
            # bar rides inside it so the block reads as that item's content.
            nest, content = m.group(1), m.group(2)
            if not content.strip():
                emit(f"{nest}{colour}│\033[0m")
                continue
            # A quoted bullet hangs its continuation under its text: aligned
            # under the dash instead, a wrapped option reads as a new one.
            hang = "│   " if content.lstrip().startswith("- ") else "│ "
            wrapped = _wrap(content, max(20, cols - len(nest)),
                            initial_indent="│ ", subsequent_indent=hang) or ["│ "]
            emit_all(f"{nest}{colour}{wl}\033[0m" for wl in wrapped)
            continue
        m = _MD_ROW_HEAD_RE.match(line)
        if m:
            # Sets the colour and falls through: the line still renders as the
            # bullet it is.
            mine = m.group(1) == "user"
        m = _MD_HEADING_RE.match(line)
        if m:
            rest = m.group(2)
            # A quoted heading never reaches here (`_MD_QUOTE_RE` is checked
            # first and exclusively), so this can't be forged by transcript text.
            mine = rest.startswith(("You said", "Then you said", "You answered",
                                    "What I asked for"))
            colour = "\033[1;33m" if rest == _MODEL_WRITTEN_HEADING else "\033[1m"
            depth = len(m.group(1))
            pad = "  " * max(0, depth - 1)
            cols = max(20, width - len(pad))
            out.append("  " * max(0, depth - 2)
                       + f"{colour}{_md_inline(rest, colour)}\033[0m")
            continue
        m = _MD_BOLD_LINE_RE.match(line)
        if m:
            nest, label = m.group(1), m.group(2)
            # `_asked_blocks`' labels say whose the block beneath them is: the
            # question and the options were put to the user, the answer is the
            # user's own. A row head colours the whole row by its role, which
            # over these three blocks would paint all of them as typed.
            if label.startswith(("Question asked", "Options given")):
                mine = False
            elif label.startswith("Answered"):
                mine = True
            wrapped = _wrap(label, max(20, cols - len(nest))) or [""]
            emit_all(f"{nest}{_ANSI_BOLD}{_md_inline(wl, _ANSI_BOLD)}{_ANSI_OFF}"
                     for wl in wrapped)
            continue
        m = _MD_ITALIC_LINE_RE.match(line)
        if m:
            wrapped = _wrap(m.group(1), cols) or [""]
            emit_all(f"{_ANSI_DIM}{_writes_lit(_md_inline(wl, _ANSI_DIM), _ANSI_DIM)}"
                     f"{_ANSI_OFF}" for wl in wrapped)
            continue
        m = _MD_BULLET_RE.match(line)
        if m:
            content = m.group(1)
            cm = _MD_BULLET_CODE_RE.match(content)
            if cm:
                prefix = "- " + (cm.group(1) + "  " if cm.group(1) else "")
                indent = " " * len(prefix)
                # A bullet that is one code span is a command or a path: wrapped
                # at a space it can no longer be copied in one selection, so it
                # runs long and the terminal soft-wraps it instead.
                wrapped = ([prefix + cm.group(2)] if _CODE_ONLY_RE.match(content)
                           else _wrap(cm.group(2), cols, initial_indent=prefix,
                                      subsequent_indent=indent) or [prefix])
                emit_all(f"{wl[:len(prefix)]}\033[36m{wl[len(prefix):]}\033[0m"
                           for wl in wrapped)
                continue
            # A bullet ending in a code span goes out unwrapped. `_wrap` would
            # split the span across lines, and `_md_inline` colours a line at a
            # time, so each boundary left a bare backtick on screen and the
            # command or message could no longer be copied in one selection.
            # The terminal soft-wraps it instead — the same trade a bullet that
            # is nothing but a code span already takes.
            if _MD_TAIL_CODE_RE.search(content):
                emit("- " + _md_inline(content))
                continue
            # Any other bullet: two-space hanging indent so continuations align
            # under the text, not under the "- " marker.
            wrapped = _wrap(content, cols, initial_indent="- ",
                                     subsequent_indent="  ") or ["- "]
            emit_all(wl[:2] + _diffstat_lit(_md_inline(wl[2:]), "")
                     for wl in wrapped)
            continue
        # The same rule off a bullet: a label and the command or path it names,
        # `One call whole, with its output: `chsum digest … --call <id>``. Held
        # to a head that fits, so a paragraph that merely ends in a code span
        # still wraps as the prose it is.
        tail = _MD_TAIL_CODE_RE.search(line)
        if tail and len(line[:tail.start()]) <= cols:
            emit(_md_inline(line))
            continue
        wrapped = _wrap(line, cols) or [line]
        emit_all(_md_inline(wl) for wl in wrapped)
    return "\n".join(out)


def frontmatter(meta: Meta, ref: str) -> str:
    lines = ["---", f"ref: {ref}", f"uuid: {meta.uuid}", f"title: {_yaml(meta.title)}"]
    if meta.project:
        lines.append(f"project: {_yaml(meta.project)}")
    if meta.branch:
        lines.append(f"branch: {_yaml(meta.branch)}")
    if meta.started:
        lines.append(f"started: {_yaml(meta.started)}")
    if meta.duration:
        lines.append(f"duration: {meta.duration}")
    lines += [f"prompts: {meta.prompts}", f"files_edited: {len(meta.edited)}"]
    if meta.notes:
        lines.append(f"notes: {len(meta.notes)}")
    if meta.agent_count:
        lines.append(f"subagents: {meta.agent_count}  # their edits are counted above")
    lines += ["generated_by: chsum (deterministic extraction, no model)", "---"]
    return "\n".join(lines)


def messages_from_jsonl(path: pathlib.Path) -> list[Message]:
    """Text messages straight from a transcript file — every digest's reader. The
    line is recorded as it is read, so a quote can name the row it came from and a
    reader opens it with `sed` rather than a second tool."""
    msgs: list[Message] = []
    numbered: list[tuple[int, dict]] = []
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            numbered.append((lineno, rec))
    TRACE.file(path, "jsonl-messages", records=len(numbered))
    command_ids = _command_prompt_ids([r for _, r in numbered])
    for lineno, rec in numbered:
        role = rec.get("type")
        if role not in ("user", "assistant"):
            continue
        if role == "user" and _tool_injected(rec, command_ids):
            continue  # a body the agent loaded, not a prompt it was given
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            # Thinking blocks are not a fallback for a thin transcript: the JSONL
            # keeps their signature and drops the text, so they are always empty.
        else:
            continue
        text = "\n".join(t for t in texts if t.strip()).strip()
        # An answer to the question tool holds its text in `toolUseResult`, not
        # in the body. `collect_rows` reads that field and counts the record as a
        # turn, so without the same fallback the digest numbers a turn it prints
        # nothing for.
        asked = None
        if not text and role == "user":
            text = _answered(rec)
            tur = rec.get("toolUseResult")
            asked = tur if text and isinstance(tur, dict) else None
        if text:
            msgs.append(Message(n=len(msgs) + 1, role=role, text=text, line=lineno,
                                api_error=bool(rec.get("isApiErrorMessage")),
                                asked=asked))
    return msgs


_SIDECAR_HINT = "sed the sidecar row named under Drill down"

# Below this an assistant turn is almost always tool narration, not a finding.
_MEATY_CHARS = 400


def _last_meaty(msgs: list[Message], exclude=None) -> "Message | None":
    """Last turn long enough to carry a result. None if `exclude` already is one."""
    if exclude is not None and len(exclude.text.strip()) >= _MEATY_CHARS:
        return None
    for m in reversed(msgs):
        if exclude is not None and m.n >= exclude.n:
            continue
        if (m.role == "assistant" and not notice_kind(m.text)
                and len(m.text.strip()) >= _MEATY_CHARS):
            return m
    return None


def last_command_output(path: pathlib.Path, tail: int = 700) -> tuple[str, str]:
    """Last Bash command in a transcript and the tail of what it printed, verbatim
    — an agent that dies mid-run leaves its state in test output, not in speech."""
    pending: dict[str, str] = {}
    cmd = out = ""
    for rec in _records(path):
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                pending[block.get("id", "")] = (block.get("input") or {}).get("command", "")
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                body = block.get("content")
                if isinstance(body, list):
                    body = "\n".join(b.get("text", "") for b in body
                                     if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(body, str) and body.strip():
                    cmd, out = pending[block["tool_use_id"]], body.strip()
    return cmd, out[-tail:] if out else ""


def render_agent_digest(meta: Meta, parent_ref: str, run: AgentRun) -> str:
    """One subagent's work. Same shape as a session digest, minus the intent trail:
    an agent gets one instruction, so 'what I asked for' is a single block."""
    msgs = messages_from_jsonl(run.path) if run.path else []
    lines = ["---",
             f"ref: {parent_ref}/{run.id}  # chsum only — claude-history has no agent ref",
             f"parent: {parent_ref}", f"agent: {run.agent_type or 'agent'}"]
    if run.model:
        lines.append(f"model: {run.model}")
    if run.duration:
        lines.append(f"duration: {run.duration}")
    lines += [f"files_edited: {len(run.edited)}", f"commands: {len(run.commands)}",
              "generated_by: chsum (deterministic extraction, no model)", "---"]
    parts = ["\n".join(lines), ""]

    parts.append(f"# {run.description or 'subagent ' + run.id}\n")
    parts.append(f"*{run.agent_type or 'agent'}"
                 + (f"/{run.model}" if run.model else "")
                 + (f" · {run.duration}" if run.duration else "")
                 + f" · spawned by `{parent_ref}`*\n")

    notes = [a for a in meta.notes if a.agent == run.id]
    if notes:
        parts.append("## Notable\n")
        parts.append("*Noted by this agent with `chsum note`. Text is verbatim.*\n")
        # The agent id is redundant here — the whole digest is that agent.
        parts += _render_notes(notes, "", show_agent=False) + [""]

    parts.append("## Task\n")
    task = next((m.text for m in msgs if m.role == "user"), "")
    parts.append(_quote(_clip(task, 500, _SIDECAR_HINT)) + "\n" if task
                 else "*No instruction recorded.*\n")

    ref = f"{parent_ref}/{run.id}"
    if run.edited:
        parts.append("## Files changed\n")
        shown = run.edited[:20]
        parts += [f"- `{f}`" for f in shown]
        if len(run.edited) > len(shown):
            parts.append(f"- …and {len(run.edited) - len(shown)} more — "
                         f"`chsum digest {ref} --tools` lists every call in order")
        parts.append("")

    if run.commands:
        parts.append("## Commands run\n")
        shown = run.commands[:10]
        parts += [f"- `{c}`" for c in shown]
        if len(run.commands) > len(shown):
            # Both counts: these are filtered and deduplicated and the view is
            # neither, so one number cannot stand for the other.
            parts.append(f"- …and {len(run.commands) - len(shown)} more of these — "
                         f"`chsum digest {ref} --commands` lists all "
                         f"{run.command_total} in order")
        parts.append("")

    # Not "final report": an interrupted agent ends mid-thought, and the transcript
    # can't tell you which happened.
    parts.append("## Last thing it said\n")
    final, notice = last_said(msgs)
    if notice:
        parts.append(f"*Run ended on a harness notice: {notice.splitlines()[0]}*\n")
    parts.append(_quote(_clip(final.text, 900, _SIDECAR_HINT)) + "\n" if final
                 else "*Nothing recorded.*\n")

    # A killed agent often ends on tool narration; the last long turn usually says more.
    meaty = _last_meaty(msgs, exclude=final)
    if meaty:
        parts.append("## Its last substantial message\n")
        parts.append(_quote(_clip(meaty.text, 1200, _SIDECAR_HINT)) + "\n")

    if run.path:
        cmd, out = last_command_output(run.path)
        if out:
            parts.append("## Last command it ran, and what came back\n")
            # `_clip_line`, not `_clip`: this prints as one code span, and
            # `_clip`'s cut mark is a line of its own — a newline inside a span
            # is a span the renderer sees as two, and neither half is one.
            parts.append(_span(_clip_line(cmd, 200)) + "\n")
            parts.append(_quote(out) + "\n")

    parts.append("## Drill down\n")
    parts.append(f"Full sidecar: `{run.path}`\n")
    parts.append(f"Everything it said, whole, with each call between: "
                 f"`chsum digest {parent_ref}/{run.id} --messages`\n")
    parts.append(f"Its calls: `chsum digest {parent_ref}/{run.id} --tools`  ·  "
                 "`--commands`  ·  one whole with `--call <id>`\n")
    return "\n".join(parts).rstrip() + "\n"


# Tool names whose call changed a file, for the digest's per-prompt counts.
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _prompt_activity(path: pathlib.Path, only_agent: str = ""
                     ) -> dict[tuple[pathlib.Path | None, int], tuple[int, str]]:
    """Per typed prompt, its turn number and one line naming how long the turn
    ran and what happened in it, keyed by the file and row the prompt sits on.
    One `collect_rows` walk, so the digest stays deterministic and makes no model
    call. Agents' rows count with the parent's, as every other total here does.

    Keyed by file as well as row because `only_agent` moves the count into a
    sidecar, whose line numbers do not order against the parent's — keyed by row
    alone, turn 3 of an agent would answer to line 3 of the session.

    The number counts `starts_turn` rows, which is the unit `--messages` takes —
    not the digest's own position, which counts prompts and would name a window
    that lands somewhere else."""
    rows = collect_rows(path, ("message", "tool", "command"), only_agent)
    starts = [i for i, r in enumerate(rows) if r.starts_turn]
    wrote = _checkpoint_calls(path)
    out: dict[int, tuple[int, str]] = {}
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(rows)
        span = rows[i + 1:end]
        tools = sum(1 for r in span if r.kind == "tool")
        cmds = sum(1 for r in span if r.kind == "command")
        files = sum(1 for r in span if r.kind == "tool" and r.label in _EDIT_TOOLS)
        said = sum(1 for r in span if r.kind == "message" and r.label == "assistant")
        # Calls that committed a checkpoint. `files` counts Edit and Write; a
        # `sed -i` or a heredoc writes without either, and the tree's own record
        # is what catches it.
        changed = sum(1 for r in span if r.tool_id in wrote)
        # `active_seconds`, not last-minus-first: the frontmatter's own duration
        # excludes gaps over `IDLE_GAP_SECONDS`, and wall-clock here read 2h01m
        # for a turn inside a session the same document called 20m.
        worked = active_seconds([r.when for r in rows[i:end] if r.when])
        bits = [_fmt_secs(worked)] if worked else []
        for count, word in ((tools, "tool call"), (files, "file"),
                            (cmds, "command"), (said, "reply")):
            if count:
                bits.append(_plural(count, word))
        if changed:
            bits.append(_plural(changed, "write"))
        out[(rows[i].source, rows[i].line)] = (n + 1, " · ".join(bits))
    TRACE.step("_prompt_activity", turns=len(starts), rows=len(rows),
               agent=only_agent or "(all)")
    return out


def render_digest(meta: Meta, ref: str, msgs: list[Message], *,
                  path: pathlib.Path | None = None,
                  prompt_clip: int = 2000) -> str:
    # Every typed prompt, uncapped and unfiltered. The trail is what the digest
    # exists to carry, and a prompt dropped for being short or for arriving late
    # in a long session is the one a reader came for. The whole trail of the
    # longest session in the corpus costs 40 KB against 21 KB for its first 100.
    prompts = [m for m in msgs if m.role == "user" and is_typed_prompt(m.text)]
    parts = [frontmatter(meta, ref), ""]

    parts.append(f"# {meta.title}\n")
    # The ref and the counts sit here as well as in the frontmatter: a terminal
    # drops the frontmatter, and without them the document names no session.
    parts.append(f"`{ref}`\n")
    when = f"{meta.date} · {meta.duration}" if meta.duration else meta.date
    counts = [_plural(meta.prompts, "prompt"), _plural(len(meta.edited), "file")]
    if meta.agent_count:
        counts.append(_plural(meta.agent_count, "subagent"))
    parts.append(f"*{when} · {' · '.join(counts)} · {meta.project_name}"
                 + (f" · `{meta.branch}`" if meta.branch else "") + "*\n")

    if meta.notes:
        # First, because someone chose these by hand — they outrank anything
        # extraction picked out.
        parts.append("## Notable\n")
        parts.append("*Noted by hand with `chsum note`. Text is verbatim.*\n")
        parts += _render_notes(meta.notes, meta.uuid) + [""]

    # The intent trail: verbatim, in order. This is the summary, uninvented.
    parts.append("## What I asked for\n")
    if not prompts:
        parts.append("*No user prompts recorded.*\n")
    else:
        activity = _prompt_activity(path) if path else {}
        if any(_MD_WRITES_RE.search(did) for _, did in activity.values()):
            parts.append(_WROTE_LEGEND)
        for m in prompts:
            # What you said leads; the row it sits on and what followed it are
            # metadata, on one dim line beneath. The row, not an ordinal: a
            # reader opens this with `sed`.
            blocks = _asked_blocks(m.asked, prompt_clip) if m.asked else []
            parts += blocks or [
                _quote(_clip(m.text, prompt_clip, "sed the row below")) + "\n"]
            where = f"`{meta.uuid[:8]}:{m.line}`" if m.line else f"message {m.n}"
            turn, did = activity.get((path, m.line), (0, ""))
            # The turn number `--messages` takes, so a window over this prompt is
            # one command away.
            meta_bits = ([f"turn {turn}"] if turn else []) + [where] + ([did] if did else [])
            parts.append(f"*{' · '.join(meta_bits)}*\n")

    if meta.edited:
        parts.append("## Files changed\n")
        # Marked, not separated: one session's work either way, but worth knowing
        # before you hunt for the turn where you supposedly changed it.
        shown_files = meta.edited[:20]
        parts += [f"- `{f}`" + ("  (agent)" if f in meta.agent_only else "")
                  for f in shown_files]
        if len(meta.edited) > len(shown_files):
            parts.append(f"- …and {len(meta.edited) - len(shown_files)} more")
        parts.append("")

    if meta.agents:
        parts.append("## Delegated\n")
        for run in meta.agents[:8]:
            bits = [b for b in (f"{run.agent_type or 'agent'}"
                                + (f"/{run.model}" if run.model else ""),
                                run.duration,
                                f"{_plural(len(run.edited), 'file')}, "
                                f"{_plural(len(run.commands), 'command')}",
                                f"depth {run.spawn_depth}" if run.spawn_depth > 1 else "") if b]
            parts.append(f"- `{run.id}`  {' · '.join(bits)}")
            if run.description:
                parts.append(f"  {run.description}")
        if len(meta.agents) > 8:
            parts.append(f"- …and {len(meta.agents) - 8} more")
        parts.append("")
        parts.append(f"One agent's own digest: `chsum digest {ref}/<id> --stdout`\n")

    if meta.commands:
        parts.append("## Commands run\n")
        shown = meta.commands[:10]
        parts += [f"- `{c}`" for c in shown]
        if len(meta.commands) > len(shown):
            # Both counts: the bullets are filtered and deduplicated and the
            # timeline is neither, so one number cannot stand for the other.
            parts.append(f"- …and {len(meta.commands) - len(shown)} more of these — "
                         f"`chsum digest {ref} --commands` lists all "
                         f"{meta.command_total} in order")
        parts.append("")

    parts.append("## Where I left off\n")
    tail = _last_exchange(msgs)
    ended_on = last_said(msgs)[1]
    if ended_on:
        parts.append(f"*Session ended on a harness notice: {ended_on.splitlines()[0]}*\n")
    if tail:
        # Not labelled an exchange: two separate backward scans, so the reply
        # usually isn't answering the prompt above it.
        for m in tail:
            label = ("Last thing I asked" if m.role == "user"
                     else f"Last thing {sources.agent_of(meta.source)} said")
            where = f"`{meta.uuid[:8]}:{m.line}`" if m.line else f"message {m.n}"
            parts.append(f"**{label}** ({where})\n")
            parts.append(_quote(_clip(m.text, 600)) + "\n")
    else:
        parts.append("*Nothing recorded.*\n")

    parts += _drill_block(ref, path)
    return "\n".join(parts).rstrip() + "\n"


def _indent(text: str, indent: str) -> str:
    """Every line of `text` shifted right, blank lines left bare."""
    if not indent:
        return text
    return "\n".join(indent + line if line.strip() else line
                     for line in text.split("\n"))


def _asked_blocks(tur: dict, clip: int, indent: str = "") -> list[str]:
    """An answered question as three quote blocks per question: what was asked,
    what was offered, what was chosen. The options are the half a `Q → A` line
    drops, and without them the answer names one of a set nothing records.

    `indent` nests the blocks under a list item, which is where a row view puts
    them; markdown reads two spaces as that item's content."""
    answers = tur.get("answers")
    if not isinstance(answers, dict) or not answers:
        return []
    questions = tur.get("questions")
    offered = {q["question"]: q for q in questions
               if isinstance(q, dict) and isinstance(q.get("question"), str)
               } if isinstance(questions, list) else {}
    out = []
    for question, answer in answers.items():
        if not isinstance(question, str) or not isinstance(answer, str):
            continue
        asked = offered.get(question) or {}
        header = asked.get("header")
        named = f" — {header}" if isinstance(header, str) and header else ""
        # The labels are chsum's own and sit outside the quotes, which hold
        # transcript text alone: `_md_ansi` leaves a quote's markdown literal so
        # that quoted text cannot forge styling, and a label inside one printed
        # its asterisks.
        out.append(f"{indent}**Question asked{named}**\n")
        out.append(_indent(_quote(_clip_line(question, clip)), indent) + "\n")
        options = [o for o in asked.get("options") or [] if isinstance(o, dict)]
        if options:
            lines = []
            for option in options:
                label = str(option.get("label") or "").strip()
                why = str(option.get("description") or "").strip()
                lines.append(f"- {label} — {_clip_line(why, clip)}" if why else f"- {label}")
            out.append(f"{indent}**Options given**\n")
            out.append(_indent(_quote("\n".join(lines)), indent) + "\n")
        out.append(f"{indent}**Answered**\n")
        out.append(_indent(_quote(_clip_line(answer, clip)), indent) + "\n")
    return out


def _drill_block(ref: str, path: pathlib.Path | None) -> list[str]:
    """Where the conversation is, and how to open a row of it. Every locator above
    expands here, and `sed` resolves them — nothing in this document requires a
    second tool to be installed before it can be read."""
    if not path:
        return []
    # Plain words, not chsum's own: "row" is this codebase's name for a line of
    # JSONL and "drill down" names nothing a reader was looking for. One command
    # per line, each whole — joined by `·` they wrapped into each other.
    out = ["## Where this came from\n",
           "This digest was read out of one file. Every `session:line` above "
           "points into it.\n",
           "The conversation", f"- `{path}`"]
    sides = subagent_transcripts(path)
    if sides:
        out.append("\nThe subagents it ran, each in its own file")
        parent = _split_agent_ref(ref)[0]
        out += [f"- `{parent}/{sc.stem.removeprefix('agent-')}` — `{sc}`" for sc in sides]
    out += ["\nTo read one line of it, put the number in place of `<line>`",
            f"- `sed -n '<line>p' {path} | jq`",
            "\nTo read it in full, in order",
            f"- everything said, with each call between: "
            f"`chsum digest {ref} --messages`",
            f"- every tool call: `chsum digest {ref} --tools`",
            f"- every shell command: `chsum digest {ref} --commands`\n"]
    return out


# A locator as chsum prints it: `01a0acf9:31` beside a turn, `f1b9bbc6:26` on a
# row, `ch_…` in a listing, and either of the first two with `/agent-id` in
# front of the colon where the row belongs to a subagent. The line part is one
# number or a range, which is what `sed` takes either way.
_LOCATOR_RE = re.compile(
    r"^(?P<session>[0-9a-zA-Z_-]+?)"
    r"(?:/(?:agent-)?(?P<agent>[0-9a-zA-Z-]+))?"
    r"(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")

# What a tool call id starts with, per tool. A checkpoint commit in the reflog
# is stamped with one, so a stamp pasted straight out of `git reflog` resolves
# without being taken apart first.
_CALL_PREFIXES = ("toolu_", "call_", "ctc_", "fc_")


def _is_call_id(token: str) -> bool:
    return token.startswith(_CALL_PREFIXES)


def _locator_parts(locator: str) -> tuple[str, str, str, str]:
    """`(session, agent, lines, call)` from a locator, or a stop naming what
    failed. A call id stands in for the line, which is what a checkpoint stamp
    carries; the row it sits on is then looked up rather than typed."""
    token = locator.strip().strip("`")
    session, _, tail = token.partition(":")
    if _is_call_id(tail):
        return session, "", "", tail
    if _is_call_id(token):
        return "", "", "", token
    match = _LOCATOR_RE.match(token)
    if not match:
        raise SystemExit(
            f"{locator!r} is not a locator. chsum prints them as "
            f"`<session>:<line>` — for example `01a0acf9:31`, or "
            f"`01a0acf9:31-40` for a run of lines. A tool call id works too: "
            f"`chsum where toolu_01V7rDx5Le`, which is what a checkpoint "
            f"commit in the reflog is stamped with.")
    start, end = match.group("start"), match.group("end")
    lines = f"{start},{end}" if start and end else (start or "")
    return match.group("session"), match.group("agent") or "", lines, ""


def _call_row(session: str, call: str) -> tuple[pathlib.Path, str]:
    """The transcript holding a tool call, and the line its record sits on. A
    checkpoint commit names the session beside the call, and that scopes the
    search to one file; a bare id searches this project, then every project,
    because a reflog line pasted on its own carries no session."""
    if session:
        row, _ = find_row(path_for_ref(session), call)
        return (row.source or path_for_ref(session)), str(row.line)
    for scope in (transcripts(local=True), transcripts()):
        for path in scope:
            hits = [r for r in collect_rows(path, ("tool", "command"))
                    if r.tool_id.startswith(call) or _short_id(r.tool_id).startswith(call)]
            if hits:
                return (hits[0].source or path), str(hits[0].line)
    raise SystemExit(f"no tool call {call!r} in any conversation here — a "
                     f"checkpoint stamp names its session too, and "
                     f"`chsum where <session>:{call}` reads that one directly")


# A record carrying no row, in plain words. `type` is the key; a record whose
# type is absent here is named by its own type, which is already plain enough.
_RECORD_NAMES = {
    "attachment": "an attachment",
    "system": "a harness note",
    "queue-operation": "a message you queued mid-turn",
    "file-history-snapshot": "a file-history snapshot",
    "file-history-delta": "a file-history delta",
    "last-prompt": "a pointer to the last prompt",
    "mode": "the mode the session was in",
    "permission-mode": "the permission mode",
    "ai-title": "a title Claude Code wrote for the session",
}


def _record_name(rec: dict) -> str:
    """What a record with no row is, named for the line that says so. The
    subtype carries the detail where there is one — a `system` record is a
    turn duration or a notice, and the difference is what a reader is after."""
    kind = str(rec.get("type") or "record")
    if kind == "attachment":
        inner = rec.get("attachment")
        sub = inner.get("type") if isinstance(inner, dict) else ""
        return f"an attachment ({sub})" if sub else "an attachment"
    if kind == "system" and rec.get("subtype"):
        return f"a harness note ({rec['subtype']})"
    if kind == "assistant":
        return "a thinking block, which chsum skips by name"
    return _RECORD_NAMES.get(kind, f"a `{kind}` record")


def _turn_block(rows: list, idx: int, path: pathlib.Path, session: str) -> list[str]:
    """The turn a row sits in: the prompt that opened it, the last thing said
    before it, and every call the turn made either side, each clipped to a line
    by `_row_block`. The turn is the bound, not a count."""
    before, rest = rows[:idx], rows[idx + 1:]
    start = next((i for i in range(idx - 1, -1, -1) if before[i].starts_turn), -1)
    asked = before[start] if start >= 0 else None
    said = next((r for r in reversed(before[start + 1:])
                 if r.kind == "message" and r.label == "assistant"), None)
    ends = next((i for i, r in enumerate(rest) if r.starts_turn), len(rest))
    is_call = lambda r: r.kind in ("tool", "command")
    ran = [r for r in before[start + 1:] if is_call(r)]
    then = [r for r in rest[:ends] if is_call(r)]
    agent = sources.agent_of(sources.source_of(path, CHSUM_DIR))
    wrote = _checkpoint_calls(path)

    out: list[str] = []
    # Each heading carries its own locator, the same `<session>:<line>` a call
    # row prints, so the prompt and the reply are as reachable as the call is.
    if asked is not None:
        out += [f"## You asked — `{_locator(asked, session)}`\n",
                _quote(_clip(asked.text, 1200)) + "\n"]
    if said is not None:
        out += [f"## {agent} said — `{_locator(said, session)}`\n",
                _quote(_clip(said.text, 1200)) + "\n"]
    if ran:
        out += [f"## Before it — {_plural(len(ran), 'call')}\n"]
        out += [b for r in ran for b in _row_block(r, session, whole=False, wrote=wrote)] + [""]
    return out + ["\x00"] + ([f"## After it — {_plural(len(then), 'call')}\n"]
                              + [b for r in then
                                 for b in _row_block(r, session, whole=False, wrote=wrote)]
                              + [""] if then else [])


def _row_context(path: pathlib.Path, line: int, ref: str) -> str:
    """What a locator landed on, rendered by what it is.

    A quarter of a transcript's lines carry a row; the rest are tool results,
    thinking blocks, attachments and harness bookkeeping. Each is placed in its
    turn the same way, and the middle of the block differs: a call prints whole
    with its output, a message prints whole, a result prints the call it
    answers, and a record with no content is named."""
    rows = collect_rows(path, ("message", "tool", "command"))
    session = path.stem
    at = {r.line: i for i, r in enumerate(rows)}

    idx, body = at.get(line, -1), ""
    if idx >= 0:
        here = rows[idx]
        body = (render_row_detail(here, _call_output(here, path), ref)
                if here.kind in ("tool", "command")
                else "\n".join(_row_block(here, session, whole=True)) + "\n")
    else:
        rec = _record_at(path, line)
        answers = _result_call_id(rec)
        if answers:
            # The output of a call: the call is the subject, and this line is
            # the half of it `render_row_detail` prints under "Output".
            idx = next((i for i, r in enumerate(rows) if r.tool_id == answers), -1)
            if idx >= 0:
                here = rows[idx]
                body = (f"*Line {line} is the output of this call, recorded at "
                        f"line {here.line}.*\n\n"
                        + render_row_detail(here, _call_output(here, path), ref))
        if not body:
            body = f"## Line {line}\n\n*{_record_name(rec)}.*\n"
            # Placed by the nearest row above it, since it opens no turn of its own.
            idx = max((i for i, r in enumerate(rows) if r.line <= line), default=-1)

    if idx < 0:
        return body
    block = _turn_block(rows, idx, path, session)
    at_subject = block.index("\x00")
    return "\n".join(block[:at_subject] + [body] + block[at_subject + 1:])


def _record_at(path: pathlib.Path, line: int) -> dict:
    """The record on one line, or `{}` where it does not parse."""
    try:
        raw = path.read_text(errors="replace").splitlines()[line - 1]
        rec = json.loads(raw)
    except (OSError, IndexError, json.JSONDecodeError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _result_call_id(rec: dict) -> str:
    """The call a `tool_result` record answers, or ""."""
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return next((str(p.get("tool_use_id")) for p in content
                 if isinstance(p, dict) and p.get("type") == "tool_result"
                 and p.get("tool_use_id")), "")


def _call_at_line(path: pathlib.Path, line: int) -> str:
    """The `tool_use` id on a line, or "". A line naming a call is what a
    checkpoint is stamped with, so a line locator reaches the same commit."""
    rec = _record_at(path, line)
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return next((str(p.get("id")) for p in content
                 if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("id")), "")


def _checkpoint_for(path: pathlib.Path, call: str, project: str) -> tuple[str, str, str]:
    """`(sha, previous sha, when)` of the checkpoint a call wrote, or empty
    strings. The previous one is what the change is measured against; where this
    is the session's first, the commit's own parent stands in."""
    if not project or not call:
        return "", "", ""
    marks = _checkpoint_shas(pathlib.Path(project), path.stem)
    at = next((i for i, (_, _, ref) in enumerate(marks)
               if ref and (ref == call or ref.startswith(call))), -1)
    if at < 0:
        return "", "", ""
    when, sha, _ = marks[at]
    return sha, (marks[at - 1][1] if at else f"{sha}^"), when


def _call_rows(path: pathlib.Path) -> dict:
    """Every call in a transcript, by its `tool_use` id. One walk, so placing a
    call against the checkpoints and naming the calls either side cost one read
    between them."""
    return {r.tool_id: r for r in collect_rows(path, ("tool", "command")) if r.tool_id}


def _call_locator(rows: dict, call: str, session: str) -> str:
    """`<session>:<line>` for a call id, or the short id where the call is not
    in this transcript. The locator is what `chsum where` takes back."""
    row = rows.get(call) or next((r for i, r in rows.items() if i.startswith(call)), None)
    return _locator(row, session) if row else _short_id(call)


def _checkpoint_around(marks: list, when: str) -> tuple[tuple, tuple]:
    """The checkpoint before a moment and the one after it, either as `()`. The
    marks are oldest first, so the split is by timestamp."""
    before = next((m for m in reversed(marks) if m[0] <= when), ())
    after = next((m for m in marks if m[0] > when), ())
    return before, after


def _checkpoint_block(path: pathlib.Path, call: str) -> tuple[str, str]:
    """`(command, section)` for the git side of a call: a diff command and the
    files it covers, with their line ranges.

    A checkpoint is committed per call that changed the tree, so a read-only
    call wrote none. That call still sits between two of them, and the pair
    brackets it — the diff across them is what the work around it changed."""
    project = extract_meta(path).project
    sha, prev, when = _checkpoint_for(path, call, project)
    if sha:
        files = _checkpoint_diff_files(pathlib.Path(project), prev, sha)
        command = f"git -C {shlex.quote(project)} diff {prev} {sha}"
        lines = [f"## Checkpoint `{sha[:7]}`\n",
                 f"- committed: {when}",
                 f"- against: `{prev[:7] if len(prev) > 8 else prev}`",
                 f"- repo: `{project}`",
                 f"- see it: `{command}`\n"]
        lines += ["### Files it wrote\n", *files, ""] if files else ["*Wrote no file.*\n"]
        return command, "\n".join(lines)

    rows = _call_rows(path)
    here = rows.get(call) or next((r for i, r in rows.items() if i.startswith(call)), None)
    marks = _checkpoint_shas(pathlib.Path(project), path.stem) if project else []
    if here is None or not marks:
        return "", ""
    ran_at = here.when
    before, after = _checkpoint_around(marks, ran_at)
    if not (before or after):
        return "", ""
    # The span either side, which is what a call that wrote nothing sits in.
    low = before[1] if before else f"{after[1]}^"
    high = after[1] if after else before[1]
    command = f"git -C {shlex.quote(project)} diff {low} {high}"
    lines = ["## Checkpoints either side\n",
             "*This call wrote nothing to the tree, so it committed no "
             "checkpoint of its own.*\n",
             f"- before: `{before[1][:7]}`  {_hhmmss(before[0])}  "
             f"← `{_call_locator(rows, before[2], path.stem)}`"
             if before else "- before: *none — this call precedes every checkpoint*",
             f"- after: `{after[1][:7]}`  {_hhmmss(after[0])}  "
             f"← `{_call_locator(rows, after[2], path.stem)}`"
             if after else "- after: *none — no checkpoint followed it*",
             f"- repo: `{project}`",
             f"- see the span: `{command}`\n"]
    files = _checkpoint_diff_files(pathlib.Path(project), low, high)
    lines += ["### Files changed across them\n", *files, ""] if files else []
    return command, "\n".join(lines)


def cmd_where(args) -> int:
    """A locator to the command that prints the record it names.

    The command goes to stdout alone, so `chsum where 01a0acf9:31 | sh` runs it
    and `$(chsum where 01a0acf9:31)` holds it. What it resolved to goes to
    stderr, where it reads beside the command without landing in a pipe."""
    session, agent, lines, call = _locator_parts(args.locator)
    if call:
        path, lines = _call_row(session, call)
    else:
        path = path_for_ref(session)
    if agent:
        sides = [s for s in subagent_transcripts(path)
                 if s.stem.removeprefix("agent-").startswith(agent)]
        if not sides:
            named = ", ".join(s.stem.removeprefix("agent-")[:8]
                              for s in subagent_transcripts(path)) or "none"
            raise SystemExit(f"no subagent {agent!r} in {session} — it ran: {named}")
        path = sides[0]

    held = 0
    try:
        with path.open(errors="replace") as fh:
            held = sum(1 for _ in fh)
    except OSError as e:
        raise SystemExit(f"{path} cannot be read ({e})")

    if not lines:
        # No line asked for: the file is the answer, and the template beside it
        # is what a line number drops into.
        print(f"sed -n '<line>p' {shlex.quote(str(path))} | jq")
        print(f"{path}\n  {held} rows · put a line number in place of `<line>`, "
              f"or name one: `chsum where {session}:1`", file=sys.stderr)
        return 0

    first = int(lines.split(",")[0])
    last = int(lines.split(",")[-1])
    if first > held or last > held:
        # Stated against the file rather than left to `sed`, which prints
        # nothing for a line past the end and reads as an empty record.
        raise SystemExit(f"{path.stem[:8]} holds {held} rows; "
                         f"line {max(first, last)} is past the end")
    # `--git` asks for the change rather than the record, so the command on
    # stdout is the diff. The call is the one on this row where a line was named.
    subject = call or _call_at_line(path, first)
    git_command, git_block = (_checkpoint_block(path, subject)
                              if getattr(args, "git", False) else ("", ""))
    if getattr(args, "git", False) and not git_command:
        raise SystemExit(
            f"no checkpoint for {subject or f'line {first}'} — a checkpoint is "
            f"committed per call that changed the tree, so a read-only call has "
            f"none, and a project with checkpointing off has none at all")
    print(git_command or f"sed -n '{lines}p' {shlex.quote(str(path))} | jq")
    span = (f"row {first} of {held}" if first == last
            else f"rows {first}-{last} of {held}")
    print(f"{path}\n  {span}", file=sys.stderr)
    # Every locator lands in a turn, and the turn is what places it. Rendered by
    # what the line holds — a call, a message, a result, a record with none of
    # it. On stderr, since stdout carries the command `sh` runs.
    context = _row_context(path, first, ch_ref_for_path(path))
    if git_block:
        context = f"{git_block}\n{context}"
    if context:
        # Rendered where stderr is a terminal, raw markdown where it is
        # redirected — the contract every other chsum document holds, read off
        # stderr here because that is the stream it lands on.
        sys.stderr.write("\n" + (_md_ansi(context)
                                  if sys.stderr.isatty() and _colour_ok(sys.stderr)
                                  else context) + "\n")
    return 0


def _render_notes(notes: list[Annotation], session: str,
                  show_agent: bool = True) -> list[str]:
    """A note names the row it points at. The note carries that row and the
    sidecar it is in, so nothing is looked up to place it."""
    out = []
    for a in notes:
        where = []
        # The target's file decides: a sidecar row is a row of the sidecar.
        row = a.row if a.agent else _first_row(a.targets)
        if row and session:
            who = f"{session[:8]}/{a.agent[:8]}" if a.agent else session[:8]
            where.append(f"`{who}:{row}`")
        elif a.agent and show_agent:
            where.append(f"agent `{a.agent}`")
        head = f"- **{_clip_line(a.text, 300)}**"
        if where:
            head += f"  ({' · '.join(where)})"
        out.append(head)
        if a.quote:
            out.append(f"  > {_clip_line(a.quote, 160)}")
    return out


def _last_exchange(msgs: list[Message]) -> list[Message]:
    """Final real user prompt and the final assistant reply — the 'where was I' signal."""
    out = []
    for m in reversed(msgs):
        if m.role == "user" and is_typed_prompt(m.text):
            out.append(m)
            break
    final, _ = last_said(msgs)
    if final:
        out.append(final)
    return sorted(out, key=lambda m: m.n)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _path_for_uuid(uuid: str) -> pathlib.Path | None:
    return next((p for p in transcripts() if p.stem == uuid), None)


def resolve_ref(args) -> str:
    if getattr(args, "file", None):
        path = pathlib.Path(args.file).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"no such transcript: {path}")
        ref = ch_ref_for_path(path)
        # A derived value is checked against what it claims: the ref has to
        # resolve back to the file it came from.
        got = path_for_ref(ref)
        if got != path:
            raise SystemExit(
                f"derived ref resolved to {got or '(nothing)'}, expected {path}.\n"
                "the ref scheme has probably changed — use `chsum find` instead."
            )
        TRACE.step("resolve_ref", via="--file", ref=ref, uuid=path.stem, verified="yes")
        return ref
    TRACE.step("resolve_ref", via="argv", ref=args.ref or "—")
    return args.ref


def _find_notes(args) -> int:
    """Notes whose text contains the query, read off the store: plain substring
    matching — there are few notes and they're short. `find` is a search and a
    search takes a query; the whole-scope listing is `chsum note --list`."""
    q = (args.query or "").lower()
    if not q:
        raise SystemExit("find --notes searches note text and needs a query — "
                         "`chsum note --list` lists them all")
    rows = [(path, a) for _d, path, a in _pooled_annotations("note", args.all)
            if q in a.text.lower()]
    if not rows:
        print(f"no notes matching {args.query!r} in {_scope_label(args.all)}",
              file=sys.stderr)
        return 1
    for path, a in rows[:args.top]:
        ref = ch_ref_for_path(path) if path else a.session[:8]
        print(f"{ref}  {a.created[:10]}  ⚑ {a.text}")
    # Flushed first, or the hint jumps the list: stdout is block-buffered when
    # piped, stderr never is.
    sys.stdout.flush()
    print(f"\nRead one: `chsum digest <ref> --stdout`", file=sys.stderr)
    return 0


def cmd_find(args) -> int:
    if args.notes:
        return _find_notes(args)
    if not args.query:
        raise SystemExit("find: need a query")
    # Single-character words are dropped: they land inside other words and paint
    # the line rather than the term.
    terms = [t for t in re.split(r"\W+", args.query) if len(t) > 1]
    # A terminal gets the padded, coloured layout; everything else gets the
    # markdown document, which is what a grep or a chat reads.
    dressed = sys.stdout.isatty()
    # The query stands alone here, so it takes the term colour directly rather
    # than through `_lit_terms`, whose dim run is for text surrounding a match.
    said = f"{_TERM_COLOUR}{args.query}\033[0m" if dressed else args.query
    print(f"searching for {said} ({args.mode})…", file=sys.stderr)
    found = search(args.query, local=not args.all, mode=args.mode, top=args.top,
                   terms=terms)
    # Gathered before either branch prints: on a terminal they are progress on
    # stderr, and in the document they are part of the answer, since a reader
    # capturing stdout alone would otherwise not learn the search was degraded.
    said = [_warning_text_line(text) for _kind, text in found.warnings]
    if not found.hits:
        for line in said:
            print(f"warning: {line}", file=sys.stderr)
        print("no matches", file=sys.stderr)
        return 1
    scores = [h.score for h in found.hits]
    if not found.semantic and len(scores) > 1:
        # One source ranked every hit, so each score is its reciprocal rank and
        # the spread measures position rather than how well a passage matched.
        said.append(f"{_plural(len(found.hits), 'hit')} ranked by position only "
                    f"({min(scores):.4f}–{max(scores):.4f}): no semantic score "
                    f"contributed.")
    # Wrapped to the terminal and hung under its label: a warning is prose, and
    # unwrapped it ran off the line as one unread block.
    cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
    if dressed:
        for i, line in enumerate(said):
            print(file=sys.stderr)
            body = f"warning: {line}" if i < len(found.warnings) else line
            # Painted after wrapping: an escape counted into the width shortens
            # the first line by the length of the sequence.
            lines = _wrap(body, cols, subsequent_indent="  ")
            if _colour_ok(sys.stderr):
                lines[0] = lines[0].replace("warning:", f"{_WARN_COLOUR}warning:\033[0m", 1)
            print("\n".join(lines), file=sys.stderr)
    # The metas are read before the first row prints, so the palette covers every
    # project in the listing and a colour is not reassigned partway down it.
    found_meta = [(h, extract_meta(path) if (path := _path_for_uuid(h.uuid)) else Meta())
                  for h in found.hits]
    palette = _project_palette(m.project_name for _h, m in found_meta)
    # Which tool wrote a hit, stated only where the hits disagree: one corpus
    # answering alone would spend the width repeating its own name.
    _mixed = len({h.source for h, _ in found_meta}) > 1
    tag = lambda h: f" · {sources.label_of(h.source)}" if _mixed else ""
    if not dressed:
        # Piped or captured, the same contract the documents hold: markdown, one
        # bullet per hit and its excerpt beneath, so a grep reads whole records.
        out = [f"# Search — {args.query}",
               f"*{_plural(len(found.hits), 'hit')} · {args.mode}*", ""]
        for i, line in enumerate(said):
            out.append(f"> {'warning: ' if i < len(found.warnings) else ''}{line}")
        if said:
            out.append("")
        for h, meta in found_meta:
            # Clipped, generously: a book's first message runs to hundreds of
            # characters of cover description, which buries the row it names.
            out.append(f"- `{h.ref}` · {h.score:.3f} · {meta.date or '??????????'}"
                       f"{tag(h)} · {meta.project_name or '-'} — {_clip_line(h.title, 120)}")
            if h.excerpt:
                out.append(f"  - `{h.focus or '?'}` — {h.excerpt}")
        sys.stdout.write("\n".join(out) + "\n")
        return 0
    sys.stderr.flush()
    print(f"\n{_dim('results')}")
    for i, (h, meta) in enumerate(found_meta):
        if i:
            print()
        cell = _paint_project(f"{meta.project_name[:22]:<22}", meta.project_name, palette)
        print(f"  {h.ref}  {h.score:.3f}  {meta.date or '??????????'}{tag(h)}  "
              f"{cell}  {_clip_line(h.title, 40)}")
        if h.excerpt:
            print(_lit_terms(f"    ↳ {h.focus or '?':<6} "
                             f"{_term_window(h.excerpt, terms, 72)}", terms))
    return 0


def _split_agent_ref(ref: str) -> tuple[str, str]:
    """`ch_…/a38bb53…` → (parent ref, agent id). No agent part → ("", ref)."""
    parent, sep, agent = ref.partition("/")
    return (parent, agent.removeprefix("agent-")) if sep else (ref, "")


def _parent_path(ref: str) -> pathlib.Path:
    """Locally, over the transcripts on disk. The listing printed these same
    derived refs, so a paste from it resolves by construction and no subprocess
    is spent."""
    path = path_for_ref(ref)
    TRACE.step("_parent_path", ref=ref, uuid=path.stem)
    TRACE.reproduce(f"chsum digest {ref} --stdout")
    return path


@dataclass
class _Row:
    """One addressable thing in a transcript: a message, a tool call, or the Bash
    subset of those. `_Event` clips its text and carries neither an id nor a line,
    so it can address neither the record nor the result that answers it."""
    when: str  # ISO timestamp, verbatim
    agent: str  # sidecar id; "" for the parent transcript
    kind: str  # message / tool / command
    tool_id: str  # the `tool_use` id its `tool_result` names back; "" on a message
    text: str  # the message, or the command, unclipped
    label: str  # role for a message, tool name for a call
    line: int = 0  # 1-based row of the record holding it, in `source`
    source: pathlib.Path | None = None  # the file that row is in
    # A turn opens here. Yours in the parent; an agent's own only where
    # `collect_rows` was scoped to that agent, which makes the sidecar the
    # conversation being numbered.
    starts_turn: bool = False
    asked: dict | None = None  # `toolUseResult` of an answer to the question tool


# What each flag selects. A Bash call is its own kind rather than a filter applied
# over `tool`, so both flags read the same rows without a second test per row.
# The messages view carries the calls too, one clipped line each between the
# messages: a turn headed `2 tool calls · 8 commands` states how many ran and
# names none of them, which leaves the work between two replies unreadable.
_ROW_KINDS = {
    "messages": ("message", "tool", "command"),
    "tools": ("tool", "command"),
    "commands": ("command",),
}


# The tool a subagent returns its report through.
_HANDBACK_TOOL = "SubagentHandback"
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _harness_reminder(rec: dict, text: str) -> bool:
    """A user record the harness wrote whole: `isMeta`, and nothing left once its
    `<system-reminder>` blocks are removed. `isMeta` alone also marks an image
    paste, which is typed text and stays a row."""
    return bool(rec.get("isMeta")) and not _REMINDER_RE.sub("", text).strip()


def _short_id(tool_id: str) -> str:
    """Display form of a `tool_use` id. `toolu_` is on every one of them, so the
    prefix carries no information and costs six columns on every row."""
    return tool_id.removeprefix("toolu_")[:10]


def collect_rows(path: pathlib.Path, kinds: tuple[str, ...],
                 only_agent: str = "") -> list[_Row]:
    """Every row of the selected kinds, the conversation's own and its agents', in
    timestamp order. Nothing filtered, deduplicated or clipped: the digest's own
    sections do all three, and these views are what they point at. `only_agent`
    narrows to one sidecar, for an agent ref."""
    out: list[_Row] = []
    # (agent, report) the parent holds as a peer message. The same report sits
    # in the agent's own handback call, and printing both repeats it. Keyed by
    # the report too: a return delivered as an attachment leaves no peer record,
    # and its handback call is then the only copy in any row.
    handed: set[tuple[str, str]] = set()
    stops: list[tuple[str, str, str]] = []
    for src, agent in mark_sources(path):
        if only_agent and agent != only_agent:
            continue
        # Whether a turn has opened in this file yet — see `starts` below.
        started = False
        TRACE.file(src, "rows")
        parsed: list[tuple[int, dict]] = []
        for lineno, raw in enumerate(src.read_text(errors="replace").splitlines(), start=1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                parsed.append((lineno, rec))
        # A body a tool loaded is not a message, the same guard every other reader
        # here carries; the call that loaded it is a `tool` row instead.
        command_ids = _command_prompt_ids([r for _, r in parsed])
        names = _tool_names([r for _, r in parsed])
        sidecars = _sidecar_ids(path)
        for lineno, rec in parsed:
            role = rec.get("type")
            ts = str(rec.get("timestamp") or "")
            # A notification delivered as an attachment leaves its text only in
            # the queue record, which is where most handback stops are found.
            body = rec.get("content")
            if (role == "queue-operation" and rec.get("operation") == "enqueue"
                    and isinstance(body, str)
                    and body.lstrip().startswith("<task-notification>")
                    and _points_to_handback(body)
                    and (aid := _agent_return(body, names, sidecars))):
                stops.append((aid, ts, _stop_note(body)))
                continue
            if role not in ("user", "assistant"):
                continue
            content = (rec.get("message") or {}).get("content")
            texts: list[str] = []
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "tool_use":
                        name = str(part.get("name") or "?")
                        report = (part.get("input") or {}).get("message")
                        # The handback call's `message` is the agent's report and
                        # the only copy of it in any user or assistant record: the
                        # parent receives it as an attachment, which no row reads.
                        if (name == _HANDBACK_TOOL and "message" in kinds
                                and isinstance(report, str) and report.strip()):
                            out.append(_Row(ts, agent, "message", "", report.strip(),
                                            f"agent {agent} returned", lineno, src))
                            continue
                        cmd = (part.get("input") or {}).get("command")
                        bash = name == "Bash" and isinstance(cmd, str) and cmd.strip()
                        kind = "command" if bash else "tool"
                        if kind not in kinds:
                            continue
                        text = cmd.strip() if bash else _tool_event(part)[1]
                        out.append(_Row(ts, agent, kind, str(part.get("id") or ""),
                                        text, name, lineno, src))
            if "message" not in kinds:
                continue
            if role == "user" and _tool_injected(rec, command_ids):
                continue
            text = "\n".join(t for t in texts if t.strip()).strip()
            if role == "user" and _harness_reminder(rec, text):
                continue
            # An answer to the question tool carries its text in a structured
            # field and none in the body, so the view dropped it and a window
            # numbered by turns would skip the turn a menu choice settled.
            answered = _answered(rec) if role == "user" else ""
            tur = rec.get("toolUseResult") if isinstance(rec.get("toolUseResult"), dict) else None
            if not text:
                text = answered
            # Agents' rows open no turn in the parent's numbering: the unit
            # there is what you typed. Scoped to one agent, though, the sidecar
            # is the conversation being read and its own prompts are what a
            # window can name — otherwise `--messages N M` has nothing to
            # count and an agent ref can only ever print the clipped list.
            # Its opening prompt is turn 1 by construction, whatever
            # `is_typed_prompt` makes of the text: a task quoting
            # `<command-name>` or a system-reminder reads as harness noise to
            # it, and dropping it would leave the run with no turn 1 at all.
            starts = bool(role == "user" and (not agent or only_agent)
                          and not rec.get("isCompactSummary")
                          and not _harness_sent(rec)
                          and (answered or is_typed_prompt(text)
                               or (only_agent and not started)))
            returned = ""
            if role == "user" and text.lstrip().startswith("<task-notification>"):
                aid = _agent_return(text, names, sidecars)
                if aid and _points_to_handback(text):
                    stops.append((aid, ts, _stop_note(text)))
                    continue
                got = _task_fields(text)
                via = names.get(got["tool-use-id"], "")
                returned = (f"agent {aid} returned" if aid
                            else f"{via or 'task'} {got['task-id'] or '?'} finished")
                text = _report_text(text)
                starts = False
            if role == "user" and (hand := _hand_back(rec)):
                handed.add(hand)
                returned = f"agent {hand[0]} returned"
                text = hand[1]
                starts = False
            if text:
                started = started or starts
                out.append(_Row(ts, agent, "message", "", text,
                                returned or role, lineno, src, starts,
                                asked=tur if answered else None))
    out = [r for r in out if not ((r.agent, r.text) in handed and r.kind == "message"
                                  and r.label == f"agent {r.agent} returned")]
    # A parent's line numbers and a sidecar's don't order against each other;
    # only a clock does. Same reason `mark_sources`' readers sort by timestamp.
    out.sort(key=lambda r: r.when)
    out = _fold_stops(
        out, stops,
        lambda r: (r.label.removeprefix("agent ").removesuffix(" returned")
                   if r.kind == "message" and r.label.endswith(" returned") else ""),
        lambda r: r.when,
        lambda r, note: dataclasses.replace(r, label=f"{r.label} · {note}"))
    TRACE.step("collect_rows", kinds=",".join(kinds), rows=len(out),
               agent=only_agent or "(all)",
               span=f"{out[0].when}→{out[-1].when}" if out else "—")
    return out


def _locator(row: _Row, session: str) -> str:
    """`<session>:<line>`, or `<session>/<agent>:<line>` for a sidecar. Eight
    characters of the session: unique over 700 sessions measured, and the agent
    part alone is not — a fork copies a sidecar under the same name."""
    who = f"{session[:8]}/{row.agent[:8]}" if row.agent else session[:8]
    return f"{who}:{row.line}"


def _source_label(path: pathlib.Path) -> str:
    """`<project> · <session>` for the transcript a run read. The project is the
    `cwd` a record carries, not the parent directory's slug, which joins path
    segments on `-` and cannot be split back where a segment holds one."""
    cwd = _transcript_cwd(path)
    name = pathlib.Path(cwd).name if cwd else ""
    return f"{name} \u00b7 {path.stem[:8]}" if name else path.stem[:8]


# `.message.content` is a string on a typed record and a list of parts on every
# other, so reading it raw returns text for one and JSON for the other. This
# takes the text where there is text and the part's kind where there is none,
# which is what the rows and the gap notes already print.
_STRETCH_JQ = ('jq -r \'if (.message.content|type)=="string" '
               'then .message.content '
               'else [.message.content[]? | .text // .name // .type] '
               '| join("\\n") end\'')


# A report is clipped to what a typed turn gets: the whole text sits one row away.
_REPORT_CLIP = 2000
_REPORT_HINT = "sed the row named beside it"
_TASK_FIELD_RE = {
    f: re.compile(rf"<{f}>(.*?)</{f}>", re.DOTALL)
    for f in ("task-id", "tool-use-id", "status", "summary", "result")
}
# A background command notifies through the same record as a subagent. The call
# the notification names is what separates them; the summary's wording is not.
_AGENT_TOOLS = ("Agent", "Task")


def _tool_names(parsed) -> dict[str, str]:
    """`tool_use` id to the tool that made the call."""
    names: dict[str, str] = {}
    for rec in parsed:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                names[str(part.get("id") or "")] = str(part.get("name") or "")
    return names


def _task_fields(text: str) -> dict[str, str]:
    return {f: (m.group(1).strip() if (m := r.search(text)) else "")
            for f, r in _TASK_FIELD_RE.items()}


def _unescaped(text: str) -> str:
    """A report body as the agent wrote it."""
    return html.unescape(text)


def _agent_return(text: str, names: dict[str, str], sidecars: set[str]) -> str:
    """The subagent a `<task-notification>` reports for, or "". A resumed agent
    notifies again with an empty `<tool-use-id>` — 3 of 5 such records measured —
    so the sidecar carrying that task id is what identifies those, and the call
    covers an agent whose sidecar was pruned."""
    got = _task_fields(text)
    if not got["task-id"]:
        return ""
    if got["task-id"] in sidecars or names.get(got["tool-use-id"]) in _AGENT_TOOLS:
        return got["task-id"]
    return ""


def _sidecar_ids(path: pathlib.Path) -> set[str]:
    """Every subagent this conversation has a transcript for."""
    return {p.stem.removeprefix("agent-") for p in subagent_transcripts(path)}


@dataclass(frozen=True)
class _AgentReport:
    """One return from a subagent. A notification fires each time an agent stops
    and a resumed agent stops again, so one agent carries several of these."""
    agent: str
    when: str
    line: int
    status: str
    summary: str
    result: str
    # The line is in the agent's own transcript, not the parent's: the report
    # reached the parent only as an attachment, and its handback call is the
    # one record holding it.
    in_sidecar: bool = False


def _handback_reports(path: pathlib.Path) -> list[_AgentReport]:
    """Every report an agent returned through its handback call, read from its
    own transcript."""
    out: list[_AgentReport] = []
    for side in subagent_transcripts(path):
        agent = side.stem.removeprefix("agent-")
        for lineno, raw in enumerate(side.read_text(errors="replace").splitlines(), 1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            content = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
            for part in content if isinstance(content, list) else []:
                if not (isinstance(part, dict) and part.get("type") == "tool_use"
                        and part.get("name") == _HANDBACK_TOOL):
                    continue
                report = (part.get("input") or {}).get("message")
                if isinstance(report, str) and report.strip():
                    out.append(_AgentReport(agent, str(rec.get("timestamp") or ""),
                                            lineno, "", "", report.strip(),
                                            in_sidecar=True))
    return out


def _agent_reports(path: pathlib.Path) -> list[_AgentReport]:
    """Every subagent return recorded in a parent transcript, in order. The
    report lands in a `<task-notification>` record, or in a peer message where
    the agent returned it through its handback call: an async agent's
    `tool_result` carries launch metadata, not the work."""
    out: list[_AgentReport] = []
    names: dict[str, str] = {}
    sidecars = _sidecar_ids(path)
    stops: list[tuple[str, str, str]] = []
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        names.update(_tool_names([rec]))
        if rec.get("type") == "user" and (hand := _hand_back(rec)):
            out.append((True, _AgentReport(hand[0], str(rec.get("timestamp") or ""),
                                           lineno, "", "", hand[1])))
            continue
        content = (rec.get("message") or {}).get("content")
        texts = ([content] if isinstance(content, str)
                 else [p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text"]
                 if isinstance(content, list) else [])
        # A notification that was enqueued and removed before delivery leaves
        # only the queue record, and the agent then reported with no message to
        # show for it. Delivered copies win on the key below, so the queue record
        # only ever supplies a report nothing else carries.
        if rec.get("type") == "queue-operation" and rec.get("operation") == "enqueue":
            body = rec.get("content")
            texts = [body] if isinstance(body, str) else []
        elif rec.get("type") != "user":
            continue
        for text in texts:
            # Anchored at the start, and a task id required: a message *about*
            # notifications reads back as one otherwise — a 52,293-char paste
            # and an assistant message on the mechanism both matched a substring
            # test, and every one of them collapsed into a single nameless agent.
            if not text.lstrip().startswith("<task-notification>"):
                continue
            if not _agent_return(text, names, sidecars):
                continue
            got = _task_fields(text)
            if _points_to_handback(text):
                stops.append((got["task-id"], str(rec.get("timestamp") or ""),
                              _stop_note(text)))
                continue
            out.append((rec.get("type") == "user",
                        _AgentReport(got["task-id"], str(rec.get("timestamp") or ""),
                                     lineno, got["status"], got["summary"],
                                     got["result"])))
    # Undelivered, so a peer message holding the same report wins the key below.
    out += [(False, r) for r in _handback_reports(path)]
    kept: dict[tuple, _AgentReport] = {}
    for delivered, rep in out:
        key = (rep.agent, rep.status, rep.result)
        if delivered or key not in kept:
            kept[key] = rep
    # By time: a sidecar's line numbers don't order against the parent's.
    reports = sorted(kept.values(), key=lambda r: (r.when, r.line))
    # A notification enqueued and then delivered is the same stop twice.
    stops = list(dict.fromkeys(stops))
    reports = _fold_stops(
        reports, [s for s in stops if s[1]], lambda r: r.agent, lambda r: r.when,
        lambda r, note: dataclasses.replace(r, status=note) if not r.status else r)
    TRACE.step("_agent_reports", reports=len(reports), records=len(out),
               agents=len({r.agent for r in reports}))
    return reports


# The `<result>` of a notification sent after a handback call. The report sits
# in the peer message before it, so this notification adds only the stop's
# status and usage, which are moved onto that report's row.
_HANDBACK_POINTER = "This agent's report was delivered to you as a message from"
_USAGE_RE = {f: re.compile(rf"<{f}>(\d+)</{f}>")
             for f in ("subagent_tokens", "tool_uses", "duration_ms")}


def _stop_note(text: str) -> str:
    """`completed · 54k tokens · 7 tools · 57s` out of a `<task-notification>`,
    each part present only where the notification carries it."""
    got = {f: int(m.group(1)) for f, r in _USAGE_RE.items() if (m := r.search(text))}
    parts = [_task_fields(text)["status"]]
    if "subagent_tokens" in got:
        parts.append(f"{got['subagent_tokens'] / 1000:.0f}k tokens")
    if "tool_uses" in got:
        parts.append(_plural(got["tool_uses"], "tool"))
    if "duration_ms" in got:
        parts.append(_fmt_secs(got["duration_ms"] // 1000))
    return " · ".join(p for p in parts if p)


def _points_to_handback(text: str) -> bool:
    return _task_fields(text)["result"].startswith(_HANDBACK_POINTER)


def _fold_stops(rows: list, stops: list[tuple[str, str, str]], agent_of, when_of,
                note) -> list:
    """Each stop — (agent, time, note) of a notification that points at a
    handback — written onto the latest report of that agent at or before its
    time, through `note(row, text)`. A stop with no such report stays out:
    the notification carried nothing but the pointer and its usage."""
    rows = list(rows)
    for agent, when, text in stops:
        at = max((i for i, r in enumerate(rows)
                  if agent_of(r) == agent and when_of(r) <= when), default=None,
                 key=lambda i: when_of(rows[i]))
        if at is not None:
            rows[at] = note(rows[at], text)
    return rows


def _report_text(text: str) -> str:
    """The report out of a `<task-notification>`, clipped. Falls back to the
    one-line summary where the notification carries no result."""
    got = _task_fields(text)
    body = html.unescape(got["result"] or got["summary"])
    return _clip(body, _REPORT_CLIP, _REPORT_HINT) if body else text.strip()


def render_agents(meta: Meta, ref: str, reports: list[_AgentReport],
                  path: pathlib.Path) -> str:
    """Every subagent the session ran, and every report each sent back."""
    by: dict[str, list[_AgentReport]] = {}
    for r in reports:
        by.setdefault(r.agent, []).append(r)
    known = [a.id for a in meta.agents]
    ids = known + [a for a in by if a not in known]
    out = [f"# Agents — {_plural(len(ids), 'subagent')}\n",
           f"*{meta.title}*\n"]
    if not ids:
        out.append("*No subagents ran.*\n")
        return "\n".join(out)
    out.append(f"One agent's own digest: `chsum digest {ref}/<id> --stdout`  ·  "
               f"its rows: `chsum digest {ref}/<id> --messages`\n")
    for aid in ids:
        run = next((a for a in meta.agents if a.id == aid), None)
        bits = [b for b in ((f"{run.agent_type or 'agent'}"
                             + (f"/{run.model}" if run.model else "")) if run else "",
                            run.duration if run else "",
                            (f"{_plural(len(run.edited), 'file')}, "
                             f"{_plural(len(run.commands), 'command')}") if run else "",
                            f"depth {run.spawn_depth}"
                            if run and run.spawn_depth > 1 else "") if b]
        out.append(f"\n## `{aid}`\n")
        if bits:
            out.append(f"*{' · '.join(bits)}*\n")
        if run and run.description:
            out.append(f"{run.description}\n")
        runs = by.get(aid, [])
        if not runs:
            out.append("*No report recorded.*\n")
            continue
        for i, rep in enumerate(runs, 1):
            who = f"{meta.uuid[:8]}/{rep.agent[:8]}" if rep.in_sidecar else meta.uuid[:8]
            head = f"`{who}:{rep.line}`  {_hhmmss(rep.when)}"
            if len(runs) > 1:
                head = f"**Return {i} of {len(runs)}** — {head}"
            out.append(f"{head}  ·  {rep.status or 'status not recorded'}\n")
            body = _unescaped(rep.result or rep.summary)
            out.append(_quote(_clip(body, _REPORT_CLIP, _REPORT_HINT)) + "\n"
                       if body else "*Nothing recorded.*\n")
    return "\n".join(out)


def _sources_block(rows: list[_Row], session: str,
                   whole: bool = False) -> list[str]:
    """Which file the rows came from, and — where the view clipped them — the
    `sed` line that opens one whole. The locator on a row is short enough to read
    across hundreds of rows; the path it expands to has to be stated somewhere,
    or the row reaches nothing on its own.

    Paths print `~`-relative: absolute they run past the wrap width and fold
    into the prose around them, and `~` pastes into a shell unchanged.

    `whole` drops the `sed` line. It is there to recover text a row clipped, so
    a view already printing every row whole leaves nothing for it to recover —
    and an unexplained shell recipe under a document reads as an instruction to
    run something rather than as the escape hatch it is."""
    if not rows:
        return []
    seen: dict[str, str] = {}
    for r in rows:
        seen.setdefault(_locator(r, session).rsplit(":", 1)[0],
                        _relpath(str(r.source), ""))
    out = ["## Sources\n"]
    # One source needs no mapping: there is nothing for a locator to pick out,
    # and the path alone is a bullet the renderer leaves unwrapped and copyable.
    if len(seen) == 1:
        out.append(f"- `{next(iter(seen.values()))}`")
    else:
        # Two spaces rather than a dash between the pair, the separator the row
        # views already put between a locator and what sits beside it. The line
        # runs long and soft-wraps rather than folding, so the path stays one
        # selection to copy.
        out += [f"- `{k}`  `{p}`" for k, p in seen.items()]
    if whole:
        return out + [""]
    first = rows[0]
    return out + ["\nA row's whole text — `sed` the line its locator names:\n",
                  f"- `sed -n '{first.line}p' "
                  f"{_relpath(str(first.source), '')} | {_STRETCH_JQ}`\n"]


def find_row(path: pathlib.Path, spec: str) -> tuple[_Row, str]:
    """The row an id prefix names, with the captured output where it has one. An
    ambiguity lists candidates rather than picking, same rule as `_find_annotation`: the
    id is copied off a row, so a prefix matching two rows is a typo."""
    hits = [r for r in collect_rows(path, ("tool", "command"))
            if r.tool_id.startswith(spec) or _short_id(r.tool_id).startswith(spec)]
    if not hits:
        raise SystemExit(f"no tool call {spec!r} — `chsum digest <ref> --tools` lists them")
    if len(hits) > 1:
        listed = "\n".join(f"  {_short_id(r.tool_id)}  {_hhmmss(r.when)}  "
                            f"{_clip_line(r.text, 60)}" for r in hits[:8])
        more = f"\n  … and {len(hits) - 8} more" if len(hits) > 8 else ""
        raise SystemExit(f"{spec!r} matches {len(hits)} calls:\n{listed}{more}")
    row = hits[0]
    out = _call_output(row, path)
    TRACE.step("find_row", tool_id=row.tool_id, agent=row.agent or "(parent)",
               output_chars=len(out))
    return row, out


def _call_output(row: _Row, path: pathlib.Path) -> str:
    """What a call returned, from the `tool_result` naming it back. "" where the
    call was interrupted or its result is not in the transcript."""
    src = row.source or path
    out = ""
    for raw in src.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        content = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not (isinstance(part, dict) and part.get("type") == "tool_result"
                    and part.get("tool_use_id") == row.tool_id):
                continue
            body = part.get("content")
            if isinstance(body, list):
                body = "\n".join(b.get("text", "") for b in body
                                 if isinstance(b, dict) and b.get("type") == "text")
            if isinstance(body, str):
                out = body
    return out


_INDEX_RE = re.compile(r"-?\d+$")


def _split_spec(values: list[str]) -> tuple[str, list[int]]:
    """A ref and turn numbers off one positional, separated by shape — a ref
    starts `ch_`, a turn number is digits. `ref` at `nargs="?"` swallows a
    bare `-1` instead of leaving it to the window."""
    refs = [v for v in values if not _INDEX_RE.match(v)]
    nums = [int(v) for v in values if _INDEX_RE.match(v)]
    if len(refs) > 1:
        raise SystemExit(f"one conversation at a time: {' '.join(refs)}")
    if len(nums) > 2:
        raise SystemExit(f"a turn window takes one number or two, not {len(nums)}")
    return (refs[0] if refs else ""), nums


@dataclass(frozen=True)
class _Window:
    """A turn window: the turns it names and the rows it spans. The bounds are
    what a gap note measures against — a view holding only calls starts and ends
    inside the span rather than on it."""
    lo: int  # first turn of yours, 1-based
    hi: int  # last turn of yours, 1-based
    turns: int  # turns of yours in the whole conversation
    nums: list[int]  # what was asked for, verbatim
    first_line: int = 0  # row the window opens on
    last_line: int = 0  # row it closes on; 0 where the next turn sits elsewhere
    to_end: bool = False  # the window runs to the end of the conversation
    # Both bounds are rows of one file. A parent's line numbers and a sidecar's
    # do not order against each other, so each carries the file it counts in.
    first_source: pathlib.Path | None = None
    last_source: pathlib.Path | None = None


def _turn_anchors(rows: list[_Row]) -> list[int]:
    """Where each turn of yours opens, as positions in `rows`."""
    return [i for i, r in enumerate(rows) if r.starts_turn]


def _resolve_span(anchors: list[int], nums: list[int],
                  total: int) -> tuple[int, int, int, int]:
    """Signed 1-based turn numbers to a half-open row range and the turns it
    covers. Each number resolves to a turn before the pair is ordered, so `2 -2`
    and `-2 2` name one range; an index past either end clamps, and the header
    then states what printed rather than the run failing."""
    n = len(anchors)
    ords = []
    for v in nums:
        if v == 0:
            raise SystemExit("turn numbers are 1-based: 1 is your first turn, "
                             "-1 your last")
        i = v - 1 if v > 0 else n + v
        ords.append(min(max(i, 0), n - 1))
    lo, hi = min(ords), max(ords)
    # The window runs to where your next turn opens; past the last turn it runs
    # to the end of the conversation, which is that turn's own work.
    stop = anchors[hi + 1] if hi + 1 < n else total
    return anchors[lo], stop, lo + 1, hi + 1


def _turn_note(lo: int, hi: int, total: int, asked: list[int],
               owner: str = "your") -> str:
    """What the numbers resolved to, for a header: `-10 -1` alone says nothing
    about where in the conversation the window landed. One wording for the row
    views and for `recap`, which resolve the pair through `_resolve_span`.
    `owner` names whose turns were counted — an agent ref numbers the sidecar's
    own prompts, and calling those yours would misreport what the window means."""
    which = f"turn {lo}" if lo == hi else f"turns {lo}–{hi}"
    said = " ".join(str(v) for v in asked)
    return (f"{said} — {owner} {which} of {total}" if said
            else f"{owner} {which} of {total}")


def _span_rows(rows: list[_Row],
               nums: list[int]) -> tuple[list[_Row], _Window]:
    """The rows a turn window covers, and the turns it names."""
    anchors = _turn_anchors(rows)
    if not anchors:
        raise SystemExit("no turns of yours here — drop the numbers for the "
                         "whole list")
    start, stop, lo, hi = _resolve_span(anchors, nums, len(rows))
    TRACE.step("_span_rows", turns=len(anchors),
               asked=" ".join(str(v) for v in nums),
               window=f"{lo}..{hi}", rows=f"{start}..{stop}")
    # The far bound only names a row where the next turn sits in the same file;
    # a parent's line numbers and a sidecar's do not order against each other.
    last = 0
    if start < stop < len(rows) and rows[stop].source == rows[stop - 1].source:
        last = rows[stop].line - 1
    return rows[start:stop], _Window(lo, hi, len(anchors), nums,
                                     rows[start].line, last, stop >= len(rows),
                                     rows[start].source,
                                     rows[stop - 1].source if start < stop else None)


@dataclass(frozen=True)
class _Skipped:
    """One row a view stepped over. `call` and `answers` pair a tool call with
    its result, so the pair prints once rather than twice."""
    line: int
    label: str
    call: str = ""  # the `tool_use` id this row carries
    answers: str = ""  # the `tool_use` id this row answers


def _result_chars(rec: dict) -> tuple[str, int]:
    """The `tool_use` a record answers and how much it returned."""
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return "", 0
    for part in content:
        if not (isinstance(part, dict) and part.get("type") == "tool_result"):
            continue
        body = part.get("content")
        if isinstance(body, list):
            body = "".join(b.get("text", "") for b in body
                           if isinstance(b, dict) and b.get("type") == "text")
        return str(part.get("tool_use_id") or ""), len(body if isinstance(body, str) else "")
    return "", 0


def _skipped_rows(path: pathlib.Path) -> dict[int, _Skipped]:
    """Every row of a transcript that a view can step over, labelled. Results are
    sized on the same walk: a call's row is where its output is reached from, and
    the length is what says whether reaching for it is worth it."""
    parsed: list[tuple[int, dict]] = []
    sizes: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        parsed.append((lineno, rec))
        answers, chars = _result_chars(rec)
        if answers:
            sizes[answers] = chars
    out: dict[int, _Skipped] = {}
    for lineno, rec in parsed:
        content = (rec.get("message") or {}).get("content")
        parts = ([p for p in content if isinstance(p, dict)]
                 if isinstance(content, list) else [])
        calls = [p for p in parts if p.get("type") == "tool_use"]
        if calls:
            first = str(calls[0].get("id") or "")
            names = ", ".join(str(c.get("name") or "?") for c in calls)
            chars = sizes.get(first)
            label = (f"{names}, result {chars:,} chars" if chars is not None
                     else f"{names}, no result recorded")
            out[lineno] = _Skipped(lineno, label, call=first)
            continue
        answers, chars = _result_chars(rec)
        if answers:
            out[lineno] = _Skipped(lineno, f"tool result, {chars:,} chars",
                                   answers=answers)
            continue
        # `thinking` names the row and nothing else: the JSONL keeps the
        # signature and drops the text.
        if any(p.get("type") == "thinking" for p in parts):
            out[lineno] = _Skipped(lineno, "thinking")
            continue
        kind = str(rec.get("type") or "record")
        # Harness bookkeeping written beside the conversation, not part of it.
        if kind != "attachment":
            out[lineno] = _Skipped(lineno, kind)
    return out


def _gap_note(skipped: dict[int, _Skipped], session: str, agent: str,
              lo: int, hi: int) -> list[str]:
    """One line per row a view stepped over, each carrying its own locator."""
    rows = [skipped[n] for n in range(lo, hi + 1) if n in skipped]
    calls = {r.call for r in rows if r.call}
    rows = [r for r in rows if not (r.answers and r.answers in calls)]
    if not rows:
        return []
    who = f"{session[:8]}/{agent[:8]}" if agent else session[:8]
    return ([f"\n*⋯ {_plural(len(rows), 'row')} not in this view*"]
            + [f"  - `{who}:{r.line}` — {r.label}" for r in rows] + [""])


# Marks a call that changed the tree, read off the checkpoint it committed.
_WROTE_MARK = "±"
_WROTE_LEGEND = (f"*`{_WROTE_MARK}` marks a call that changed the tree, and a "
                 f"turn's `N writes` counts them. Both are read off the "
                 f"checkpoint each call committed, which catches a write made "
                 f"by any means, not only Edit and Write.*\n")


@functools.lru_cache(maxsize=32)
def _checkpoint_calls(path: pathlib.Path) -> frozenset[str]:
    """The `tool_use` ids that committed a checkpoint, so a row can say whether
    its call changed the tree. One `git reflog` per transcript, held for the
    process: a row list would otherwise ask git once per row.

    Empty where checkpointing is off for the project, which makes the mark
    absent everywhere rather than wrong anywhere."""
    project = extract_meta(path).project
    if not project:
        return frozenset()
    return frozenset(ref for _, _, ref in
                     _checkpoint_shas(pathlib.Path(project), path.stem) if ref)


def _row_block(r: _Row, session: str, whole: bool, wrote=frozenset()) -> list[str]:
    """One row, as the lines it prints. Both shapes are built here rather than at
    the two call sites they used to sit at: the head — the call id where there is
    one, the locator, the time, the role or tool name — is the same row either
    way, and spelled twice it drifted. Only what hangs off it differs, a
    blockquote of the whole text or one clipped line."""
    ident = f"`{_short_id(r.tool_id)}`  " if r.tool_id else ""
    # The mark sits before the label, where a scan down the column finds it.
    did = f"{_WROTE_MARK} " if r.tool_id in wrote else ""
    head = f"{ident}`{_locator(r, session)}`  {_hhmmss(r.when)}  {did}{r.label}"
    if not whole:
        first = next(iter(r.text.splitlines()), "")
        return [f"- {head}  {_span(_clip_line(first, 120))}"]
    # Blockquoted, not fenced: text carrying a fence would close the block and
    # forge document structure below it.
    # Nested two: the blocks are this row's content, not the next row's.
    blocks = _asked_blocks(r.asked, 600, "  ") if r.asked else []
    if blocks:
        return [f"\n- {head}\n", *blocks]
    return [f"\n- {head}\n", _quote(r.text)]


def render_rows(meta: Meta, ref: str, rows: list[_Row], what: str,
                window: _Window | None = None, whole: bool = False,
                activity: dict[tuple[pathlib.Path | None, int],
                               tuple[int, str]] | None = None) -> str:
    """One line per row, or every row whole where a turn window narrowed them —
    a reader who named a window asked for what a single clipped line cannot hold.
    `whole` says the same of the messages view's message rows, which print
    whole whatever the window: they are prose, and a conversation clipped to a
    line per message is the one thing this view cannot be used for. The calls
    that view carries between them stay one clipped line whatever the window,
    which keeps a read of the conversation from running through a heredoc's
    body; `--call <id>` opens one whole.
    `activity` heads each turn with the counts the digest already prints under a
    prompt — one wording for "what happened here", rather than a second one
    invented for this view.
    A call's first line, not a flattened clip: a heredoc
    squashed onto one line is 120 chars of its own source, where `python3 - <<'PY'`
    identifies it at a glance. The whole text sits one `sed` away, which is what
    the locator on each row is for."""
    _, agent_id = _split_agent_ref(ref)
    wrote = _checkpoint_calls(meta.path) if meta.path else frozenset()
    # The scope is stated, not inferred from whether the locators carry an agent
    # part: a session whose agents ran nothing renders identically to one scoped away.
    scope = (f"Subagent `{agent_id}` only." if agent_id
             else "This conversation and its subagents.")
    # Turns are the agent's own where the rows are scoped to it — `_turn_note`
    # would otherwise call the task it was handed something you typed.
    owner = "the agent's" if agent_id else "your"
    out = [f"# {what.capitalize()} — {_plural(len(rows), 'row')}, in order\n",
           f"*{meta.title}*\n",
           f"*{scope}*\n"]
    # Stated only where a mark appears: a project with checkpointing off marks
    # nothing, and a legend for an absent mark reads as a missing feature. The
    # turn lines carry it too, on a view whose own rows are messages.
    if any(r.tool_id in wrote for r in rows) or any(
            _MD_WRITES_RE.search(did) for _, did in (activity or {}).values()):
        out.append(_WROTE_LEGEND)
    if window:
        out.append(f"*{_turn_note(window.lo, window.hi, window.turns, window.nums, owner)}.*\n")
    if not rows:
        out.append(f"*No {what} in this window.*\n" if window
                   else f"*No {what} recorded.*\n")
        return "\n".join(out)
    out += _sources_block(rows, meta.uuid, whole=bool(window) or whole)
    # Only inside a window: unwindowed, a whole session's gaps are hundreds of
    # lines and the view is a list rather than a stretch being read.
    # Where each turn opens, per file. A view is headed by the turn a row sits
    # in rather than by the row that opens one: `--tools` keeps no message rows
    # at all, and hung off those it showed a list of calls with no boundaries in
    # it. Reading the turn off the row's line also heads a window with the turn
    # it opened inside, which is the one fact a window cannot state itself.
    anchors: dict[pathlib.Path | None, list[int]] = {}
    for src, line in sorted(activity or {}, key=lambda k: k[1]):
        anchors.setdefault(src, []).append(line)
    headed: tuple[pathlib.Path | None, int] | None = None
    kinds: dict[pathlib.Path, dict[int, _Skipped]] = {}
    seen: dict[pathlib.Path, int] = {}  # last printed row, per file
    prev: _Row | None = None
    prev_whole = False
    day = ""
    for r in rows:
        if r.when[:10] != day:
            day = r.when[:10]
            out.append(f"\n## {day}\n")
        if window and r.source is not None:
            # Per file, not per printed row: rows interleave from the parent and
            # its sidecars by timestamp, and two files' line numbers do not order
            # against each other. A file's own last printed row is what the next
            # row of that file measures back to. The window's opening bound is
            # known for one file only, so the others start at their first row.
            last = seen.get(r.source)
            since = (last + 1 if last is not None
                     else window.first_line if r.source == window.first_source
                     else 0)
            if since and r.line > since:
                if r.source not in kinds:
                    kinds[r.source] = _skipped_rows(r.source)
                out += _gap_note(kinds[r.source], meta.uuid, r.agent,
                                 since, r.line - 1)
            seen[r.source] = r.line
        # The same line the digest prints under a prompt, heading the turn it
        # belongs to: a list of rows says what was said and nothing about what
        # the turn then did, which is the thing a reader is scanning for.
        # A file with no anchors — a sidecar, where the parent holds the turns —
        # heads nothing and leaves the last head standing.
        lines = anchors.get(r.source, ())
        n = bisect.bisect_right(lines, r.line) if lines else 0
        if n and (r.source, lines[n - 1]) != headed:
            headed = (r.source, lines[n - 1])
            turn, did = activity[headed]
            out.append("\n*" + " · ".join([f"turn {turn}"] + ([did] if did else [])) + "*")
        prev = r
        row_whole = r.kind == "message" if whole else bool(window)
        # A clipped row opens flush against the line above it, and the line
        # above a call in the messages view is the closing line of a
        # blockquote, which swallows the row. One blank line closes the quote.
        if prev_whole and not row_whole:
            out.append("")
        prev_whole = row_whole
        out += _row_block(r, meta.uuid, row_whole, wrote)
    # The window runs past its last printed row to where your next turn opens.
    if window and prev is not None and prev.source is not None \
            and prev.source == window.last_source \
            and (window.last_line or window.to_end):
        if prev.source not in kinds:
            kinds[prev.source] = _skipped_rows(prev.source)
        # A window ending on the last turn is bounded by the file, not by a next
        # turn: that turn's own work is everything that follows it.
        bound = window.last_line or max(kinds[prev.source] or [0])
        if prev.line < bound:
            out += _gap_note(kinds[prev.source], meta.uuid, prev.agent,
                             prev.line + 1, bound)
    if any(r.tool_id for r in rows):
        out.append(f"\nOne call whole, with its output: `chsum digest {ref} --call <id>`\n")
    return "\n".join(out)


def render_writes(meta: Meta, ref: str, path: pathlib.Path) -> str:
    """Every turn that changed the tree, with the files it wrote and how much.

    Read from the git checkpoints, not the transcript: a checkpoint is committed
    per call that changed the tree, so a `sed -i` or a heredoc counts the same as
    an Edit. A turn spans from the checkpoint before its first write to the one
    its last write committed, and `git diff --numstat` over that span is what
    the turn changed."""
    project = meta.project
    marks = _checkpoint_shas(pathlib.Path(project), path.stem) if project else []
    turns = _turn_of_call(path)
    # Checkpoints grouped by the turn whose call committed them, in order.
    by_turn: dict[int, list] = defaultdict(list)
    for i, (when, sha, call) in enumerate(marks):
        number, opened = turns.get(call, (0, None))
        by_turn[number].append((when, sha, call, opened,
                                marks[i - 1][1] if i else f"{sha}^"))

    out = [f"# Writes — {_plural(len(by_turn), 'turn')} that changed the tree\n",
           f"*{meta.title}*\n",
           f"*Read from the git checkpoints: a call that changed the tree "
           f"committed one, so a write by any means is counted.*\n"]
    if not marks:
        return "\n".join(out + ["*No checkpoints for this session. Per-turn "
                                "checkpointing is opt-in; see `chsum hook`.*\n"])

    total_add = total_del = 0
    touched: set[str] = set()
    for number in sorted(by_turn):
        span = by_turn[number]
        low, high = span[0][4], span[-1][1]
        stat = _checkpoint_numstat(pathlib.Path(project), low, high)
        opened = span[0][3]
        head = f"## Turn {number}" if number else "## Before the first prompt"
        if opened is not None:
            head += f" — `{_locator(opened, path.stem)}`"
        out.append(head + "\n")
        if opened is not None and opened.text.strip():
            out.append(_quote(_clip_line(opened.text, 300)) + "\n")
        added = sum(a for a, _, _ in stat)
        removed = sum(d for _, d, _ in stat)
        total_add, total_del = total_add + added, total_del + removed
        for a, d, f in sorted(stat, key=lambda s: -(s[0] + s[1])):
            touched.add(f)
            out.append(f"- `{f}`  +{a} −{d}")
        out.append(f"\n*{_plural(len(span), 'write')} · "
                   f"{_plural(len(stat), 'file')} · +{added} −{removed}*\n")

    out.append(f"## In total\n")
    out.append(f"*{_plural(len(marks), 'write')} · {_plural(len(touched), 'file')} · "
               f"+{total_add} −{total_del} across {_plural(len(by_turn), 'turn')}.*\n")
    out += _drill_block(ref, path)
    return "\n".join(out)


def render_row_detail(row: _Row, out: str, ref: str) -> str:
    """One call whole and its output whole. Both whole: a reader who followed an id
    here asked for what the row's single clipped line could not hold. Blockquoted
    rather than fenced — output carrying a fence would close the block and forge
    document structure below it."""
    where = f"agent {row.agent}" if row.agent else "the parent transcript"
    parts = [f"# {row.label} `{_short_id(row.tool_id)}`\n",
             f"- id: `{row.tool_id}`",
             f"- when: {_hhmmss(row.when)}  ({row.when})",
             f"- where: {where}",
             f"- record: `{row.source}` line {row.line}",
             f"- open it: `sed -n '{row.line}p' {row.source} | jq`",
             f"- conversation: `{ref}`\n",
             "## Call\n", _quote(row.text) + "\n", "## Output\n"]
    parts.append(_quote(out) + "\n" if out.strip() else "*No output recorded.*\n")
    return "\n".join(parts)


def _digest_for(ref: str) -> tuple[Meta, str]:
    parent_ref, agent_id = _split_agent_ref(ref)
    path = _parent_path(parent_ref)
    meta = extract_meta(path)
    if agent_id:
        run = next((a for a in meta.agents if a.id == agent_id), None)
        if not run:
            known = ", ".join(a.id for a in meta.agents) or "none"
            raise HistoryError(f"no subagent {agent_id} in {parent_ref} (has: {known})")
        return meta, render_agent_digest(meta, parent_ref, run)
    return meta, render_digest(meta, ref, messages_from_jsonl(path), path=path)


def _target(args, here=None) -> tuple[pathlib.Path, list[int]]:
    """Which conversation, and which of your turns — one resolver for `recap`
    and `digest`, so a window typed for one runs on the other. A ref
    and turn numbers arrive on `spec` and on the view flag beside it, and
    `_split_spec` separates them by shape. `--last N` takes the Nth most recent
    conversation that is not this one; `--here` overrides both and takes the one
    you are in. Nothing named calls `here` — `live_transcript` for a digest,
    `_pick_transcript` for a recap — and sets `args.file` from it, so
    `resolve_ref` derives the ref from the file this returns."""
    extra = [v for k in _ROW_KINDS for v in (getattr(args, k, None) or [])]
    args.ref, span = _split_spec(list(args.spec) + extra)
    if getattr(args, "here", False):
        args.ref, args.file, args.last = "", "", None
    if args.last is not None and args.last < 1:
        raise SystemExit("--last counts from 1: 1 is the most recent "
                         "conversation that isn't this one")
    via = "--file" if args.file else "ref" if args.ref else \
        "--last" if args.last is not None else "here"
    if via == "--last":
        args.file = str(latest_transcript(local=not args.all, nth=args.last))
    elif via == "here":
        args.file = str(here() if here else live_transcript())
    if args.file:
        path = pathlib.Path(args.file).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"no such transcript: {path}")
    else:
        # An agent ref addresses a sidecar; the turns are the parent's, so the
        # parent is what resolves to a path.
        path = path_for_ref(_split_agent_ref(args.ref)[0])
    TRACE.step("_target", ref=args.ref or "(none)", uuid=path.stem,
               turns=" ".join(str(v) for v in span) or "(none)", via=via)
    return path, span


def _strip_frontmatter(text: str) -> str:
    """The leading `---` block off a rendered document. It carries the digest to
    a future context window; on screen it is ten lines before the title. Gated on
    stdout being a terminal, not on colour, so a redirect keeps it whatever
    `NO_COLOR` says."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    return text[end + 5:].lstrip("\n") if end != -1 else text


def _write_md(text: str) -> None:
    """A rendered document to stdout, coloured on a terminal and byte-identical
    markdown anywhere else — the gate `recap` and the listing use.

    One trailing newline, always: a document whose last line is coloured ends on
    the reset escape, and a shell prompt then resumes on that line."""
    if sys.stdout.isatty():
        text = _strip_frontmatter(text)
    out = _md_ansi(text) if _colour_ok() else text
    sys.stdout.write(out if out.endswith("\n") else out + "\n")


def _md_cell(text: str) -> str:
    """A cell for a markdown table. A pipe inside the text would close the cell
    and shift every column after it, so it is escaped; newlines cannot appear in
    a row at all and become spaces."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _md_table(heads: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """The same rows as a markdown table. Each row carries one field past
    `heads` — the prose the terminal prints beneath it — which becomes the last
    column under a blank heading, so one grep-able line holds the whole record."""
    n = len(heads) + 1
    out = ["| " + " | ".join(_md_cell(h) for h in (*heads, "")) + " |",
           "|" + " --- |" * n]
    for r in rows:
        cells = [_md_cell(c) for c in r[:n]]
        cells += [""] * (n - len(cells))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def _print_table(heads: tuple[str, ...], rows: list[tuple[str, ...]],
                 cols: int, project_col: int | None = None) -> None:
    """One padded column line per row, its trailing prose wrapped and dimmed
    beneath. Each row carries one field more than `heads`: the columns, then
    that prose. Wrapped here rather than left to the terminal — prose folding at
    column 0 reads as another row. `project_col` names the column carrying a
    project name, which is painted per project so rows from one project group by
    eye down a listing that crosses several."""
    # Piped or captured, the listing is markdown, the same contract `_write_md`
    # holds for a document: what a terminal pretty-prints, a grep reads as rows.
    # The terminal decides the shape; colour lives only inside that branch.
    if not sys.stdout.isatty():
        sys.stdout.write(_md_table(heads, rows))
        return
    n = len(heads)
    widths = [max(len(r[i]) for r in (*rows, heads)) for i in range(n)]
    palette = ({} if project_col is None
               else _project_palette(r[project_col] for r in rows))
    print(_dim("  ".join(f"{h:<{w}}" for h, w in zip(heads, widths)).rstrip()))
    for r in rows:
        cells = [f"{c:<{w}}" for c, w in zip(r[:n], widths)]
        if project_col is not None:
            cells[project_col] = _paint_project(cells[project_col],
                                                r[project_col], palette)
        print("  ".join(cells).rstrip())
        if r[n]:
            print(_dim("\n".join(textwrap.wrap(r[n], cols, initial_indent="  ↳ ",
                                               subsequent_indent="    "))))


# The views a digest file can hold, and what each is called in a sentence. The
# bare digest is the empty key. One table: the name a file is written under and
# the name a listing reads back off it both come from here, so a view added to
# one is never missing from the other.
_VIEWS = {"": "digest", "messages": "messages view", "tools": "tools view",
          "commands": "commands view", "agents": "agents view",
          "writes": "writes view",
          "agent": "subagent digest", "call": "single call"}

# A uuid holds hyphens of its own, so a suffix is matched at the end of the stem
# rather than split on. `agent` and `call` carry an id after their name.
_VIEW_SUFFIX_RE = re.compile(
    r"-(" + "|".join(v for v in _VIEWS if v) + r")(?:-(.+))?$")


def _view_suffix(view: str, ident: str = "") -> str:
    """What a view's file carries after the session uuid. "" for the digest,
    which is written under the uuid alone."""
    if not view:
        return ""
    return f"-{view}-{ident}" if ident else f"-{view}"


def _digest_stem(stem: str) -> tuple[str, str]:
    """`(session uuid, view)` for a digest file, the inverse of `_view_suffix`.
    The digest itself carries no suffix and returns an empty view."""
    m = _VIEW_SUFFIX_RE.search(stem)
    if not m:
        return stem, ""
    return stem[:m.start()], m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")


def _view_label(suffix: str) -> str:
    """What a written file holds, in words, for the line that confirms it."""
    view = suffix.lstrip("-").split("-")[0] if suffix else ""
    return _VIEWS.get(view, view or "digest")


def _list_digests(out: pathlib.Path, all_projects: bool) -> int:
    """The digest files `chsum digest <ref>` has written, newest first. Named by
    session uuid on disk, so each row carries the ref that reproduces it. A
    view's file carries the view after the uuid, and is listed under the same
    session as the digest it sits beside."""
    # The ref, not the stem: it is what `chsum digest <ref> --stdout` takes. The
    # session's own figures come with it, since one flat directory holds every
    # project's digests and a date alone does not say which conversation this is.
    by_stem = {p.stem: p for p in transcripts(local=not all_projects)}
    files = sorted(out.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not all_projects:
        # A digest whose transcript is gone carries no project to scope it by, so
        # project scope drops it and `--all` is where it stays reachable.
        files = [f for f in files if _digest_stem(f.stem)[0] in by_stem]
    if not files:
        print(f"no digests for {_scope_label(all_projects)} in {out}", file=sys.stderr)
        return 1
    heads = ("", "written", *(("project",) if all_projects else ()),
             "dur", "prompts", "files", "state")
    rows = []
    for f in files:
        uuid, view = _digest_stem(f.stem)
        src = by_stem.get(uuid)
        written = f.stat().st_mtime
        if src is None:
            rows.append((f"ch_? {f.stem[:8]}", _age_or_date("", written),
                         *(("-",) if all_projects else ()),
                         "-", "-", "-", "no transcript", ""))
            continue
        m = extract_meta(src)
        # A session appended to after its digest was written is covered short by
        # whatever followed; the figures beside it are the transcript's now.
        end = _parse_ts(m.ended)
        state = "stale" if end and end.timestamp() > written else "current"
        rows.append((ch_ref_for_path(src), _age_or_date("", written),
                     *((m.project_name,) if all_projects else ()),
                     m.duration or "-", str(m.prompts),
                     str(len(m.edited)), state,
                     (m.title or "(untitled)") + (f"  · {view}" if view else "")))
    _print_table(heads, rows,
                 max(40, min(shutil.get_terminal_size((100, 24)).columns, 88)),
                 project_col=2 if all_projects else None)
    sys.stdout.flush()
    print(f"\n{out}\nRead one: `chsum digest <ref> --stdout`  ·  "
          f"{_plural(len(files), 'digest')}", file=sys.stderr)
    return 0


def _deliver(args, md: str, uuid: str, suffix: str = "") -> int:
    """A document to a file, or to stdout where `--stdout` asks for that. One
    rule for every view and every way of naming a session: a digest is an
    artifact to keep and to paste into a later session, so it lands on disk and
    the path is printed.

    `suffix` keeps a view's file beside the digest rather than over it."""
    if args.stdout:
        _write_md(md)
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    dest = args.out / f"{uuid}{suffix}.md"
    dest.write_text(md)
    # The path alone on stdout, so `code $(chsum digest <ref>)` opens what was
    # written and a pipeline reads one clean line. What it holds goes to stderr,
    # the same split `chsum where` takes.
    print(dest)
    print(f"wrote the {_view_label(suffix)}", file=sys.stderr)
    return 0


def cmd_digest(args) -> int:
    if args.list:
        if args.spec or args.file or args.last:
            raise SystemExit("--list reads the digest directory and takes no "
                             "conversation — `chsum digest <ref> --stdout` prints one")
        return _list_digests(args.out, args.all)
    # One view at a time, stated: two flags together printed the first and
    # dropped the second along with any numbers attached to it.
    picked = [f"--{k}" for k in _ROW_KINDS if getattr(args, k, None) is not None]
    picked += ["--agents"] if args.agents is not None else []
    picked += ["--writes"] if getattr(args, "writes", False) else []
    picked += ["--call"] if args.call else []
    if len(picked) > 1:
        raise SystemExit(f"one view at a time: {' '.join(picked)}")
    if args.call and _split_spec(list(args.spec))[1]:
        raise SystemExit("--call names one row and takes no turn numbers")
    view = next((k for k in _ROW_KINDS if getattr(args, k, None) is not None), "")
    path, span = _target(args)
    ref = resolve_ref(args)
    # Numbers on their own name a window of what was said, the view they are
    # reached for; a flag beside them picks a different one.
    if span and not view:
        view = "messages"
    if getattr(args, "writes", False):
        meta = extract_meta(path)
        return _deliver(args, render_writes(meta, ref, path),
                        meta.uuid, _view_suffix("writes"))
    if args.agents is not None:
        if _split_spec(list(args.spec) + list(args.agents))[1]:
            raise SystemExit("--agents covers the whole conversation and takes "
                             "no turn numbers")
        parent_ref, _ = _split_agent_ref(ref)
        meta = extract_meta(path)
        return _deliver(args, render_agents(meta, parent_ref,
                                            _agent_reports(path), path),
                        meta.uuid, _view_suffix("agents"))
    if args.call or view:
        parent_ref, agent_id = _split_agent_ref(ref)
        if args.call:
            row, out = find_row(path, args.call)
            return _deliver(args, render_row_detail(row, out, ref),
                            extract_meta(path).uuid,
                            _view_suffix("call", _short_id(args.call)))
        kinds = _ROW_KINDS[view]
        # Turns are numbered off message rows, so a window over calls collects
        # them to count against and drops them once the slice is taken.
        wanted = tuple(dict.fromkeys(kinds + ("message",))) if span else kinds
        rows = collect_rows(path, wanted, agent_id)
        window = None
        if span:
            rows, window = _span_rows(rows, span)
            rows = [r for r in rows if r.kind in kinds]
        # Messages are prose and print whole, on every ref. Not a size, not a
        # flag: the turn numbers already select the part of a conversation you
        # want, so a second way to ask for less would only be a worse one — and
        # a rule that reads the ref to decide made one command print two
        # different documents. Calls stay a list — `--call <id>` opens one.
        meta = extract_meta(path)
        return _deliver(args, render_rows(meta, ref, rows, view, window,
                                          whole=view == "messages",
                                          activity=_prompt_activity(path, agent_id)),
                        meta.uuid, _view_suffix(view))
    meta, md = _digest_for(ref)
    _, agent_id = _split_agent_ref(ref)
    return _deliver(args, md, meta.uuid, _view_suffix("agent", agent_id) if agent_id else "")


def live_transcript() -> pathlib.Path:
    """The conversation running right now — the opposite of `latest_transcript`,
    which skips it. Resolved by session id wherever there is one, so it answers
    the same from any directory the session has cd'd into. Falls back to the
    newest file in this project so `chsum mark` still resolves something when
    the env var is missing."""
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    path = _session_transcript()
    if path:
        TRACE.step("live_transcript", via="CLAUDE_CODE_SESSION_ID", uuid=live,
                   dir=path.parent.name)
        return path
    cands = sorted(transcripts(local=True), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        raise SystemExit(_no_conversation())
    TRACE.step("live_transcript", via="newest mtime", uuid=cands[0].stem,
               env_session=live or "unset", candidates=len(cands))
    return cands[0]


def _mark_target(path: pathlib.Path, spec: str) -> _MarkTarget:
    """Resolve `--at` to a full record uuid, its row and its file: either a uuid
    prefix from `chsum mark --recent` (looked for in sidecars too, since `--recent`
    lists them), or a bare row number, which stays parent-only — only the parent's
    rows number against the transcript a bare mark is made in."""
    if spec.isdigit():
        line = int(spec)
    else:
        line = 0
    prefix = spec.lower()
    hits: list[_MarkTarget] = []
    for src, agent in ([(path, "")] if line else mark_sources(path)):
        TRACE.file(src, "at")
        for lineno, raw in enumerate(src.read_text(errors="replace").splitlines(), start=1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            uid = rec.get("uuid") if isinstance(rec, dict) else ""
            if not isinstance(uid, str) or not uid:
                continue
            if (line and lineno == line) or (not line and uid.startswith(prefix)):
                hits.append(_MarkTarget(uid, lineno, agent))
    if not hits:
        TRACE.step("_mark_target", spec=spec, mN_line=line, hits=0)
        raise SystemExit(f"no record matching {spec!r} in {path.name}")
    if len(hits) > 1:
        TRACE.step("_mark_target", spec=spec, mN_line=line, hits=len(hits))
        raise SystemExit(f"{spec!r} matches {len(hits)} records — use more characters")
    TRACE.step("_mark_target", spec=spec, mN_line=line, at=hits[0].uuid[:8],
               line=hits[0].line, agent=hits[0].agent or "—")
    return hits[0]


def _tool_lines(rec: dict) -> list[str]:
    """One line per tool call in a record: what was done, not that something was."""
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        name, inp = str(part.get("name") or "?"), part.get("input") or {}
        detail = ""
        for key in ("file_path", "command", "description", "pattern", "query", "path"):
            if isinstance(inp.get(key), str) and inp[key].strip():
                detail = inp[key].strip().splitlines()[0]
                break
        out.append(f"{name}: {detail}" if detail else name)
    return out


def mark_sources(path: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """Every file a mark in this conversation can point into: the transcript, then
    its sidecars, each with the agent id to label it by — a running agent's screen
    is being written to its sidecar, not the transcript."""
    return [(path, "")] + [(s, s.stem.removeprefix("agent-"))
                           for s in subagent_transcripts(path)]


def _recent_actions(path: pathlib.Path, limit: int,
                    skip_machinery: bool = False) -> list[tuple[int, dict, str, str, str, str]]:
    """(line, record, label, full text, kind, agent) for the last `limit` things
    that happened: messages either side typed, and every tool call, in order.
    `limit=0` is all, sidecars folded in. `skip_machinery` drops `chsum mark`'s
    own footprint (for `--match`; `--recent` keeps it since a `!` run happened
    too). Read straight from the JSONL, not claude-history, whose view of a live
    transcript lags behind the file."""
    out = []
    for src, agent in mark_sources(path):
        TRACE.file(src, "recent")
        machinery = _Machinery()
        for lineno, raw in enumerate(src.read_text(errors="replace").splitlines(), start=1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
                continue
            text = _record_text(rec).strip()
            if text and is_real_prompt(text):
                # Always asked, even when kept: the scan is stateful, and a record
                # it never sees is one the next record's verdict is missing.
                if machinery.sees(text) and skip_machinery:
                    continue
                if rec.get("isCompactSummary"):
                    continue  # Claude Code's own auto-summary, not a real message
                label = next((ln for ln in text.splitlines() if ln.strip()), "")
                out.append((lineno, rec, label, text, "text", agent))
            for line in _tool_lines(rec):
                if skip_machinery and machinery.sees(line, "tool"):
                    continue
                out.append((lineno, rec, line, line, "tool", agent))
    # By timestamp, not file order: an agent's records are in a different file, so
    # file order interleaves nothing. Stable, so a record's tool calls stay with it.
    out.sort(key=lambda r: str(r[1].get("timestamp") or ""))
    return out[-limit:] if limit else out


def _fold(text: str) -> str:
    """Down to words and spaces for matching a half-remembered phrase; case,
    markdown, and punctuation are noise here. Only matching is folded — marks
    still quote the original bytes."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


_NOTE_INPUT_RE = re.compile(r"\s*<bash-input>\s*chsum\s+(?:note|annotate|mark)\b")
_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)
_BASH_OUTPUT_RE = re.compile(r"<bash-stdout>(.*?)</bash-stdout>|<bash-stderr>(.*?)</bash-stderr>", re.S)
_BASH_OUT_RE = re.compile(r"\s*<bash-(?:stdout|stderr)>")


class _Machinery:
    """`chsum note`'s own footprint, tracked in file order — excluded from
    matching (a tool_use record is written before the command runs, so without
    this every `--match` finds itself). Invocations only, not talk about them,
    or a conversation about this feature becomes unnotable. One instance per
    file: adjacency is a fact about the file, and timestamp sort interleaves sidecars."""

    def __init__(self) -> None:
        self.after_note = False

    def sees(self, text: str, kind: str = "text") -> bool:
        # Tool calls hang off a record rather than being one, so they carry no
        # state: nothing is written between a tool_use and the record after it.
        if kind == "tool":
            return bool(re.search(r"\bchsum\s+(?:note|annotate|mark)\b", text))
        if _BASH_OUT_RE.match(text):
            was, self.after_note = self.after_note, False
            return was
        self.after_note = bool(_NOTE_INPUT_RE.match(text))
        return self.after_note


def _match_target(path: pathlib.Path, needle: str) -> _MarkTarget:
    """The one record whose text contains `needle`. Ambiguity is reported, never
    resolved: asking to mark a phrase puts that phrase in your own prompt too, so
    "newest wins" would routinely mark the request instead of its subject."""
    want = _fold(needle)
    hits, seen = [], set()
    for lineno, rec, label, text, kind, agent in _recent_actions(path, 0, skip_machinery=True):
        uid = str(rec.get("uuid") or "")
        if want not in _fold(text) or uid in seen:
            continue
        seen.add(uid)
        hits.append((_MarkTarget(uid, lineno, agent), rec, label))
    TRACE.step("_match_target", needle=_clip_line(needle, 60), hits=len(hits))
    if not hits:
        raise SystemExit(f"nothing in this conversation matches {needle!r}")
    if len(hits) > 1:
        print(f"{len(hits)} matches — mark one with --at, or give a longer string:",
              file=sys.stderr)
        for target, rec, label in hits:
            who = f"agent {target.agent[:8]}" if target.agent else (
                "you" if rec.get("type") == "user" else "claude")
            when = str(rec.get("timestamp") or "")[11:16]
            print(f"  {target.uuid[:8]}  {when:<5}  {who:<14}  {label[:80]}", file=sys.stderr)
        raise SystemExit(2)
    return hits[0][0]


def _context_rows(src: pathlib.Path, lineno: int, n: int) -> list[tuple[int, dict]]:
    """The n records either side of a row, in file order. Messages and one line per
    tool call — the level `--list --full` prints at, not tool inputs or result bodies."""
    try:
        lines = src.read_text(errors="replace").splitlines()
    except OSError:
        return []
    TRACE.file(src, "context", records=len(lines))
    rows = []
    for i, raw in enumerate(lines, start=1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
            continue
        # A record carrying neither text nor a tool call is a tool result: its own
        # header would print with nothing under it and read as a message that said nothing.
        if i == lineno or _record_text(rec).strip() or _tool_lines(rec):
            rows.append((i, rec))
    here = next((k for k, (i, _rec) in enumerate(rows) if i == lineno), None)
    if here is None:
        return []
    return rows[max(0, here - n):here + n + 1]


def _show_note(path: pathlib.Path, a: Annotation, context: int, cols: int) -> int:
    """Where one annotation landed and what surrounds it. The header carries
    the whole answer to "where is this" — file, row, time, agent — so nothing
    downstream has to search the transcript to place it."""
    src = _sidecar_path(path, a.agent)
    row = a.row if a.agent else _first_row(a.targets)
    where = [f"{src}:{row}" if row else str(src)]
    if a.created:
        where.append(_hhmmss(a.created))
    if a.modified and a.modified != a.created:
        where.append(f"edited {_hhmmss(a.modified)}")
    if a.agent:
        where.append(f"agent {a.agent}")
    where.append(a.kind)
    print(f"{a.id}  {a.text}")
    print(_dim("  " + "  ·  ".join(where)))
    if not row:
        print(_dim("\n  (session-level: no row)"))
        return 0
    rows = _context_rows(src, row, max(0, context))
    for lineno, rec in rows or []:
        print()
        who = f"agent {a.agent[:8]}" if a.agent else (
            "you" if rec.get("type") == "user" else "claude")
        # Local clock, matching the header's own stamp — two clocks in one view
        # read as two different moments.
        head = f"{'▸' if lineno == row else ' '} {lineno:>6}  " \
               f"{_hhmmss(str(rec.get('timestamp') or ''))}  {who}"
        print(head if lineno == row else _dim(head))
        body = _record_text(rec).strip()
        if lineno != row:
            # The targeted record prints whole; its neighbours are orientation, and
            # the row number above each one says where to read the rest.
            body = _clip(body, 400, f"line {lineno} of {src.name}")
        for para in body.splitlines() + [f"⚙ {t}" for t in _tool_lines(rec)]:
            if not para.strip():
                print()
                continue
            text = "\n".join(textwrap.wrap(para, cols, initial_indent="    ",
                                           subsequent_indent="    "))
            print(text if lineno == row else _dim(text))
    if not rows:
        print(_dim(f"\n  row {row} is not in {src.name}"))
    return 0


def cmd_note(args) -> int:
    """A note against a row of this conversation, into the store. Prints
    nothing on success: nothing is recorded by the harness any more, so there
    is nothing for output to carry."""
    # Named on every path that quotes the transcript: a result that disagrees with
    # another invocation is unreadable without knowing which file each one searched.
    cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))

    # Ahead of the transcript: the listing reads the store across the project and
    # runs where this project holds no conversation at all, which `live_transcript`
    # exits on.
    if args.list:
        pooled = _pooled_annotations("note", args.all)
        if not pooled:
            print(f"nothing noted in {_scope_label(args.all)}", file=sys.stderr)
            return 1
        if args.full:
            # Keyed by file: a note can point into a sidecar, whose line numbers mean nothing here.
            lines_of: dict[pathlib.Path, list[str]] = {}
            for i, (_d, src, a) in enumerate(pooled):
                if i:
                    print()
                print(f"{a.id[:8]}#{a.n}  {a.text}")
                row = a.row if a.agent else _first_row(a.targets)
                body = a.quote
                if src is not None:
                    src = _sidecar_path(src, a.agent)
                    if src not in lines_of:
                        lines_of[src] = src.read_text(errors="replace").splitlines()
                        TRACE.file(src, "list-full", records=len(lines_of[src]))
                    body = _text_at_line(lines_of[src], row) or a.quote
                first = True  # the ↳ opens the message, it doesn't bullet its paragraphs
                for para in (body or "(session-level)").splitlines():
                    if not para.strip():
                        print()
                        continue
                    print(_dim("\n".join(textwrap.wrap(
                        para, cols, initial_indent="  ↳ " if first else "    ",
                        subsequent_indent="    "))))
                    first = False
            sys.stdout.flush()
            print(f"\n{_plural(len(pooled), 'note')} in {_scope_label(args.all)}",
                  file=sys.stderr)
            return 0
        return _list_annotations("note", cols, args.all)

    path = pathlib.Path(args.file).expanduser().resolve() if args.file else live_transcript()
    if not path.exists():
        raise SystemExit(f"no such transcript: {path}")
    project_dir = path.parent.name

    if args.show:
        # Across the project: the store is per project and an id names a file in it.
        anns = [r[2] for r in _pooled_annotations("", args.all)]
        a = _find_annotation(anns, args.show)
        src = path if a.session == path.stem else next(
            (p for p in transcripts() if p.stem == a.session), path)
        return _show_note(src, a, args.context, cols)

    if args.delete:
        # The directory the annotation is filed under, not the cwd's: under
        # `--all` an id names a file in another project and the write goes there.
        pooled = _pooled_annotations("", args.all)
        anns = [r[2] for r in pooled]
        home = {(r[2].id, r[2].n): r[0] for r in pooled}
        for spec in args.delete:
            a = _find_annotation(anns, spec)
            _remove_annotation(home.get((a.id, a.n), project_dir), a.id)
            print(f"deleted {a.id}  {_clip_line(a.text, 80)}", file=sys.stderr)
        return 0

    if args.recent is not None:
        rows = _recent_actions(path, args.recent)
        if not rows:
            print("nothing recorded in this conversation yet", file=sys.stderr)
            return 1
        for lineno, rec, label, _, _kind, agent in rows:
            who = f"agent {agent[:8]}" if agent else (
                "you" if rec.get("type") == "user" else "claude")
            when = str(rec.get("timestamp") or "")[11:16]
            print(f"{str(rec.get('uuid') or '')[:8]}  {when:<5}  {who:<14}  {label[:88]}")
        print("\nMessages and tool calls, oldest first; tool results are not listed.\n"
              'Note one: `chsum note --at <id> "<text>"`', file=sys.stderr)
        return 0

    text = " ".join(args.text).strip()
    if not text:
        # Named separately from the bare case: `--match "<phrase>"` with no text
        # reads as complete, and the generic message sent one reader off diagnosing
        # the match instead of the missing argument.
        if args.match or args.at:
            flag = "--match" if args.match else "--at"
            raise SystemExit(
                f'{flag} says which message to note, not what — add the note:\n'
                f'  chsum note {flag} "{(args.match or args.at)}" "why this matters"')
        raise SystemExit('nothing to note — try: chsum note "why this matters"')
    if args.at and args.match:
        raise SystemExit("--at and --match name the same thing two ways; use one")
    if args.session and (args.at or args.match):
        flag = "--at" if args.at else "--match"
        raise SystemExit(f"--session files against the conversation and {flag} "
                         "names one message; use one")
    # No target at all, so `_file_note` files it under the session's own file —
    # the shape a note against a sidecar row already takes.
    target = (None if args.session
              else _mark_target(path, args.at) if args.at
              else _match_target(path, args.match) if args.match
              else _here_target(path))
    quote = ""
    if target and target.line:
        src = _sidecar_path(path, target.agent)
        quote = next((ln for ln in _text_at_line(
            src.read_text(errors="replace").splitlines(), target.line).splitlines()
            if ln.strip()), "")
    agent = target.agent if target else ""
    row = target.line if target else 0
    _file_note(project_dir, path.stem, path, [row] if row and not agent else [],
               "note", text, agent=agent, row=row if agent else 0, quote=quote)
    return 0


def cmd_annotations(args) -> int:
    """The wire claude-history speaks: one JSON object on stdin, one on stdout,
    exit zero for success. A failure exits non-zero with one line on stderr,
    which their side surfaces at the keystroke for a write and drops this
    annotator from the merge for a read."""
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        raise SystemExit("annotations: stdin is not one JSON object")
    if not isinstance(req, dict):
        raise SystemExit("annotations: stdin is not one JSON object")
    if args.op == "read":
        out = []
        for conv in req.get("conversations") or []:
            if not isinstance(conv, str) or not conv:
                continue
            p = pathlib.Path(conv)
            for a in _annotations_for(p):
                row: dict = {"conversation": conv, "id": a.id, "targets": a.targets,
                             "kind": a.kind, "text": a.text}
                # Absent where the stored annotation carries no stamp, which
                # renders without times rather than with a fabricated one.
                if a.created:
                    row["created"] = a.created
                if a.modified:
                    row["modified"] = a.modified
                if a.turn:
                    row["turn"] = a.turn
                if a.agent:
                    row["agent"], row["row"] = a.agent, a.row
                if a.origin:
                    row["origin"] = a.origin
                out.append(row)
        print(json.dumps({"annotations": out}, ensure_ascii=False))
        TRACE.step("annotations", op="read",
                   conversations=len(req.get("conversations") or []), served=len(out))
        return 0
    conv = req.get("conversation")
    if not isinstance(conv, str) or not conv:
        raise SystemExit("annotations: `conversation` is required")
    p = pathlib.Path(conv)
    if args.op == "write":
        text = " ".join(str(req.get("text") or "").split())
        if not text:
            raise SystemExit("annotations write: `text` is empty")
        targets = []
        for t in req.get("targets") or []:
            # A run "7..9" files under the turn its first row selects.
            head = t.split("..")[0] if isinstance(t, str) else t
            if isinstance(head, int) or (isinstance(head, str) and head.isdigit()):
                targets.append(int(head))
        quote = ""
        if targets and p.exists():
            quote = next((ln for ln in _text_at_line(
                p.read_text(errors="replace").splitlines(), _first_row(targets)).splitlines()
                if ln.strip()), "")
        kind = str(req.get("kind") or "note")
        created = str(req.get("created") or "")
        modified = str(req.get("modified") or "")
        # `replaces` names the note this write supersedes. Answering with the
        # same id says the record was updated and claude-history issues no
        # delete; answering with a different one has it drop the old note, which
        # is the path a move to another turn takes.
        old = str(req.get("replaces") or "")
        note_id = _rewrite_note(p.parent.name, p.stem, p, old, targets, kind, text,
                                quote=quote, modified=modified) if old else None
        if note_id is None:
            if old:
                # A note moved to another turn is a new record in the store and
                # the same note to a reader, so its first stamp travels with it
                # where the request sends none of its own.
                gone = _remove_annotation(p.parent.name, old)
                if gone is not None and not created:
                    created = gone.created
                # A move is an edit, so it stamps `modified` now. Left to
                # `_file_note` the field would copy `created` and the record
                # would read as never touched since it was written.
                modified = modified or _now_iso()
            note_id = _file_note(p.parent.name, p.stem, p, targets, kind, text,
                                 quote=quote, created=created, modified=modified)
        print(json.dumps({"id": note_id}))
        return 0
    if args.op == "delete":
        spec = str(req.get("id") or "")
        # An id the store does not hold answers false rather than failing: a
        # non-zero exit drops chsum from the merge and takes every other
        # annotation on the transcript out of the render with it.
        hit = _remove_annotation(p.parent.name, spec)
        print(json.dumps({"deleted": hit is not None}))
        return 0
    raise SystemExit(f"annotations: unknown op {args.op!r}")


def path_for_ref(ref: str) -> pathlib.Path:
    """A ref (or a uuid, or a prefix of either) to its transcript. Matched against
    locally derived refs rather than asked of claude-history, so a paste from the
    listing resolves by construction, at no subprocess cost."""
    want = ref.strip()
    hits = [p for p in transcripts()
            if ch_ref_for_path(p).startswith(want) or p.stem.startswith(want)]
    if not hits:
        raise SystemExit(f"no conversation matching {ref!r} — see `chsum sessions`")
    if len(hits) > 1:
        # Clipped, and titles read only for what's shown: `ch_` alone matches the
        # whole corpus, and 200 extract_meta calls to say "be more specific" is
        # both a wall of text and a pause.
        rows = "\n".join(f"  {ch_ref_for_path(p)}  {extract_meta(p).title}"
                         for p in hits[:8])
        more = f"\n  … and {len(hits) - 8} more" if len(hits) > 8 else ""
        raise SystemExit(f"{ref!r} matches {len(hits)} conversations:\n{rows}{more}")
    TRACE.step("path_for_ref", asked=want, ref=ch_ref_for_path(hits[0]), uuid=hits[0].stem)
    return hits[0]


def _name_target(args) -> pathlib.Path:
    if args.file:
        path = pathlib.Path(args.file).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"no such transcript: {path}")
        return path
    return path_for_ref(args.ref) if args.ref else live_transcript()


def cmd_name(args) -> int:
    """Rename a conversation. `chsum name ch_… "…"` renames that one; a bare
    `chsum name "…"` renames the session you're in — told apart by the `ch_` prefix, not a flag."""
    words = list(args.words)
    args.ref = None
    if words and words[0].startswith("ch_"):
        # A ref with no title asks what that one is called, exactly as a bare
        # `chsum name` asks about this one. It can't be read as "rename this
        # session to ch_…" — nobody titles a session with a ref.
        args.ref = words.pop(0)

    if args.list:
        # Keyed by the transcripts in scope: the name store is one flat file for
        # every project, and a uuid absent from the scope belongs to another one.
        paths = {p.stem: p for p in transcripts(local=not args.all)}
        names = {uuid: title for uuid, title in load_names().items() if uuid in paths}
        if not names:
            print(f"nothing renamed in {_scope_label(args.all)}", file=sys.stderr)
            return 1
        for uuid, title in names.items():
            print(f"{ch_ref_for_path(paths[uuid])}  {title}")
        sys.stdout.flush()  # or the hint lands above what it is a hint about
        print("\nUndo one: `chsum name <ref> --clear`", file=sys.stderr)
        return 0

    path = _name_target(args)
    meta = extract_meta(path)
    ref = ch_ref_for_path(path)

    if args.clear:
        if not meta.renamed:
            print(f"{ref} was never renamed", file=sys.stderr)
            return 1
        was = _load_names().get(path.stem, {}).get("was", "")
        save_name(path.stem, None)
        # Put Claude Code's title back the same way — appended, never deleted — or
        # /resume keeps showing the name chsum just forgot.
        if was and not args.no_resume and title_reaches_resume(path):
            try:
                append_ai_title(path, was)
            except OSError:
                pass
        print(f"{ref}\n  cleared · {was or meta.ai_title or '(untitled)'}")
        return 0

    title = " ".join(words).strip()
    if not title:
        which = f"{args.ref} " if args.ref else ""
        print(f"{ref}\n  {meta.title}"
              f"{' · renamed by you' if meta.renamed else ''}")
        sys.stdout.flush()  # or the hint lands above what it is a hint about
        print(f'\n{"Rename it again" if meta.renamed else "Rename it"}: '
              f'`chsum name {which}"what it actually was"`'
              + (f'   Back to Claude Code\'s: `chsum name {which}--clear`'
                 if meta.renamed else ""),
              file=sys.stderr)
        return 0

    save_name(path.stem, title, was=meta.ai_title)
    print(f"{ref}\n  was  {meta.ai_title or '(untitled)'}\n  now  {title}")
    if args.no_resume:
        return 0
    if not title_reaches_resume(path):
        # The name holds in chsum's store, which every listing reads. Stated so
        # the absence of a /resume change reads as the design, not a failure.
        print("renamed here; the session was written by another tool, so its "
              "own resume list keeps the title it holds", file=sys.stderr)
        return 0
    try:
        append_ai_title(path, title)
    except OSError as e:
        print(f"warning: renamed here, but the transcript is unwritable, so "
              f"/resume keeps its own title ({e})", file=sys.stderr)
        return 0
    if path == live_transcript():
        # Only the store holds for a live session: Claude Code goes on appending
        # its own ai-title as the conversation grows, and the last one is what
        # /resume reads.
        print("note: this session is still running, so Claude Code will title it "
              "again later — chsum keeps your name, /resume may not.",
              file=sys.stderr)
    return 0


def last_activity(path: pathlib.Path) -> str:
    """The conversation's own last timestamp, read from the tail of the file. Not
    `st_mtime` — anything that touches a transcript without writing to it (a
    backup, an indexer) rewrites that. Tail-read rather than `extract_meta`,
    which parses every record. Returns "" if the last records carry no
    timestamp; the caller falls back to mtime for those."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    stamps = re.findall(r'"timestamp"\s*:\s*"([^"]+)"', tail)
    return stamps[-1] if stamps else ""


def latest_transcript(local: bool = True, nth: int = 1) -> pathlib.Path:
    """Nth-most-recent conversation with activity, by last activity not filename.
    Sessions that went nowhere are skipped, as is the one currently running."""
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    cands = [p for p in transcripts(local=local) if p.stem != live]
    # Same ordering key as `sessions`, so the two agree on which one is last.
    def recency(p: pathlib.Path) -> tuple[str, float]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (last_activity(p), mtime)
    cands.sort(key=recency, reverse=True)
    seen = 0
    for p in cands:
        if not _has_activity(extract_meta(p)):
            continue
        seen += 1
        if seen == nth:
            TRACE.step("latest_transcript", nth=nth, local=local, candidates=len(cands),
                       skipped_live=live or "unset", uuid=p.stem)
            return p
    where = "this project" if local else "any project"
    raise SystemExit(f"no conversation #{nth} in {where}"
                     if seen else f"no conversations found in {where}")


# ---------------------------------------------------------------------------
# The live window: a bare `chsum recap`
# ---------------------------------------------------------------------------
# The running session, since the last thing you typed. Raw JSONL throughout —
# claude-history's read of a live transcript lags the file, and this view
# exists to be current.


@dataclass
class _Event:
    when: str  # ISO timestamp, verbatim
    agent: str  # sidecar id; "" for the parent transcript
    kind: str  # said / ran / edit / output / failed / spawn / tool
    text: str
    # Where the record sits. Carried for the verbatim sections only: the extract
    # `_event_block` renders is read by a model, and a number in it is a number
    # that can be copied out wrong.
    line: int = 0
    source: pathlib.Path | None = None


# `is_error` also flags a tool use *you* declined — not a failure of the work.
_DECLINED_RE = re.compile(r"^the user (doesn'?t|does not) want to proceed",
                          re.IGNORECASE)
# Second signal for what `is_error` misses: a traceback whose command still
# exited 0. Anchored to line start, so output *quoting* an error isn't caught.
_FAIL_RES = [re.compile(p, re.MULTILINE) for p in (
    r"^Traceback \(most recent call last\):",
    r"^\w*(Error|Exception): \S",
)]


# What a reader of the extract is told when chsum shortened a message. Not
# "clipped": that reads as something that happened to the message, and a
# summariser then reports it as an event ("the explanation was cut off").
_EXTRACT_CLIP = "not in this extract"

# What Claude said is the text a recap is about, so it is clipped at ~p99 rather
# than the 600 it used to be: over 932 transcripts and 20,971 assistant messages,
# 600 kept 55.7% of that text and 3,000 keeps 98.7%. Costs 3,000 chars more than
# a 2,000 cap across a whole session, since few messages sit in that band.
_SAID_CLIP = 3000


def _fail_excerpt(out: str) -> str:
    """400 chars from the first error line rather than the tail — a command that
    carries on after a traceback would otherwise have the tail quote whatever
    ran successfully afterwards."""
    hits = [m.start() for r in _FAIL_RES for m in [r.search(out)] if m]
    start = min(hits) if hits else max(0, len(out) - 400)
    head = f"[from {start} chars in] " if start else ""
    return head + out[start:start + 400] + ("…" if len(out) - start > 400 else "")


def _failed(body: str, is_error: bool) -> bool:
    """Whether a Bash result records something going wrong. `is_error` is taken
    as given; the text signal only counts in the last 500 chars, since a command
    that *died* ends at its error while one that merely printed error text as
    data carries on past it — unanchored, a quoted traceback forges a fresh failure."""
    if _DECLINED_RE.match(body.strip()):
        return False
    if is_error:
        return True
    tail = body[-500:]
    return any(r.search(tail) for r in _FAIL_RES)


def _typed_text(rec: dict) -> str:
    """Only the parts you typed. The harness rides `<system-reminder>` blocks in
    the same record as a prompt, and `_record_text` would quote them with it."""
    if _harness_sent(rec):
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
    else:
        return ""
    return "\n".join(t for t in texts if is_real_prompt(t)).strip()


def _last_prompt(path: pathlib.Path) -> tuple[int, dict] | None:
    """Line and record of the last thing you typed — the catch-up anchor.
    `<bash-…>` records and Skill bodies are excluded, or anchoring could catch up
    from its own footprint."""
    found = None
    lines = path.read_text(errors="replace").splitlines()
    TRACE.file(path, "anchor", records=len(lines))
    command_ids = _command_prompt_ids(_parsed(lines))
    for lineno, raw in enumerate(lines, start=1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (not isinstance(rec, dict) or rec.get("type") != "user"
                or rec.get("isCompactSummary") or _tool_injected(rec, command_ids)):
            continue
        text = _typed_text(rec)
        if text and is_typed_prompt(text):
            found = (lineno, rec)
    if found:
        TRACE.step("_last_prompt", line=found[0], uuid=str(found[1].get("uuid") or "")[:8],
                   ts=str(found[1].get("timestamp") or ""), skill_bodies=len(command_ids))
    else:
        TRACE.step("_last_prompt", line=0, found="none", records=len(lines))
    return found


def _tool_event(part: dict) -> tuple[str, str]:
    """(kind, text) for one tool_use block."""
    name, inp = str(part.get("name") or "?"), part.get("input") or {}
    if name == "Bash" and isinstance(inp.get("command"), str):
        # The description first, verbatim, then the command. It carries the
        # intent stated before the command ran — 31,148 of 32,947 Bash calls
        # carry one, mean 38 characters — and the `output:` event that follows
        # carries what came back. Without it a reader reconstructs the intent
        # from the command text and the result, which is a guess.
        said = str(inp.get("description") or "").strip()
        # The whole command, every heredoc body replaced by its line count and
        # the file it writes. The 200-character clip took the first work line
        # and dropped the rest, so `cd X && sed -i ... && npx tsc` arrived as
        # the `sed` alone and the verdict that followed it went missing. Masked,
        # the corpus costs 9.3M characters against 4.3M clipped, 25.1M raw.
        cmd = _mask_bodies(inp["command"].strip())
        return "ran", f'"{said}"\n{cmd}' if said else cmd
    if name in _FILE_TOOLS and isinstance(inp.get("file_path"), str):
        # Edited text rides along verbatim — what the summariser reads function
        # names out of, instead of chsum parsing code. Uncapped: a clip at 1500
        # cut a replacement mid-token, and a half-identifier matches nothing in
        # the transcript a reader checks it against. Across 8,441 edit and write
        # events the caps held 8.5M characters and the whole text costs 12.0M.
        bits = [inp["file_path"]]
        for key, label in (("old_string", "was"), ("new_string", "now"),
                           ("content", "now")):
            if isinstance(inp.get(key), str) and inp[key].strip():
                bits.append(f"--- {label} ---\n{inp[key]}")
        return "edit", "\n".join(bits)
    if name in _AGENT_TOOLS:
        # The spawn point, not the instruction. A spawn marks where the main
        # session handed work off, and that place is what a reader of the main
        # session follows. The prompt itself is already written to the rules a
        # bullet is written to, so a model passed it rewrites prose that needs
        # no rewriting — at 1.7M characters across 447 calls. The agent's own
        # transcript holds it verbatim, and the report comes back on its own.
        desc, prompt = str(inp.get("description") or ""), str(inp.get("prompt") or "")
        return "spawn", desc or _clip_line(prompt, 200)
    if name == "Skill" and isinstance(inp.get("skill"), str):
        # The loaded body is dropped as a turn, so this call is the only record
        # of which skill ran and what it was asked — `skill`/`args` are its keys,
        # neither of which the generic detail scan below covers. Uncapped: across
        # 106 Skill calls the args reach 306 characters, so the 400 never fired.
        args = str(inp.get("args") or "").strip()
        return "tool", f"Skill: {inp['skill']}" + (f" — {args}" if args else "")
    detail = next((inp[k].strip().splitlines()[0]
                   for k in ("file_path", "command", "description", "pattern", "query", "path")
                   if isinstance(inp.get(k), str) and inp[k].strip()), "")
    return "tool", f"{name}: {detail}" if detail else name


def _events_since(path: pathlib.Path, anchor_line: int, anchor_ts: str,
                  until_ts: str = "") -> list[_Event]:
    """Everything after your prompt, transcript and sidecars merged by timestamp
    (two files' line numbers don't order against each other). `until_ts` closes
    the window at the far end for `recap`, which picks both ends rather than
    running to now — a timestamp even for the parent, since the far end has to
    cut sidecars too."""
    events: list[_Event] = []
    shell_at: int | None = None  # index of the `!` command awaiting its output record
    sources = mark_sources(path)
    for src, agent in sources:
        # id -> the command, not just its id: a failure has to be able to name
        # what failed, and by the time the result lands the tool_use is gone.
        pending: dict[str, str] = {}
        # file-tool id -> index into `events`, so the line range from its
        # tool_result (available only after the fact) can be patched onto the
        # edit event already appended at tool_use time.
        pending_edits: dict[str, int] = {}
        TRACE.file(src, "events")
        for lineno, raw in enumerate(src.read_text(errors="replace").splitlines(), start=1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant", "system"):
                continue
            ts = str(rec.get("timestamp") or "")
            live = (lineno > anchor_line) if src == path else (ts > anchor_ts)
            if until_ts and ts and ts > until_ts:
                live = False
            if rec.get("type") == "system":
                # Only compaction is surfaced; other system records (session
                # start, etc.) carry `content` outside `message`, not an event.
                if live and rec.get("subtype") == "compact_boundary":
                    cm = rec.get("compactMetadata") or {}
                    pre, post = cm.get("preTokens"), cm.get("postTokens")
                    stats = (f"{pre:,} → {post:,} tokens"
                             if isinstance(pre, int) and isinstance(post, int)
                             else "token counts unrecorded")
                    events.append(_Event(ts, agent, "compacted",
                                         f"Conversation compacted — {stats}, "
                                         f"{cm.get('trigger', 'trigger unrecorded')}",
                                         lineno, src))
                continue
            if rec.get("type") == "user" and live:
                # The command you typed, kept as an event where its loaded body
                # is dropped as a turn — otherwise the work it caused appears
                # under the previous turn with nothing naming the cause.
                text = _record_text(rec)
                cmd = _COMMAND_NAME_RE.search(text)
                if cmd:
                    cargs = _COMMAND_ARGS_RE.search(text)
                    detail = cargs.group(1).strip() if cargs else ""
                    events.append(_Event(ts, agent, "command",
                                         _clip_line(cmd.group(1)
                                                    + (f" {detail}" if detail else ""), 200),
                                         lineno, src))
                    continue
                # A `!` run is yours, not Claude's: it lands as two records, the
                # command and then its output, and the extract names no actor on
                # its own — without this kind the model reads the reply that
                # followed and writes "Claude ran" for what you typed.
                bang = _BASH_INPUT_RE.search(text)
                if bang:
                    shell_at = len(events)
                    events.append(_Event(ts, agent, "shell",
                                         _clip_line(_first_command(bang.group(1)), 200),
                                         lineno, src))
                    continue
                if shell_at is not None and _BASH_OUTPUT_RE.search(text):
                    out = "\n".join(m.group(1) or m.group(2) or ""
                                    for m in _BASH_OUTPUT_RE.finditer(text)).strip()
                    prev = events[shell_at]
                    # chsum's own output is a view of this transcript, and a
                    # view quoted back into the extract is summarised again on
                    # the next run — each round more transcript-shaped than the
                    # last. The command stays; what it printed does not.
                    if _first_command(prev.text).startswith("chsum"):
                        out = ""
                    if out:
                        events[shell_at] = _Event(prev.when, prev.agent, prev.kind,
                                                  prev.text + "\n" + out[-400:],
                                                  prev.line, prev.source)
                    shell_at = None
                    continue
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str) and live and rec["type"] == "assistant":
                if content.strip() and not notice_kind(content):
                    events.append(_Event(ts, agent, "said", _clip(content, _SAID_CLIP, _EXTRACT_CLIP),
                                     lineno, src))
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and live and rec["type"] == "assistant":
                    t = part.get("text", "").strip()
                    if t and not notice_kind(t):
                        events.append(_Event(ts, agent, "said", _clip(t, _SAID_CLIP, _EXTRACT_CLIP),
                                             lineno, src))
                elif part.get("type") == "tool_use":
                    if part.get("name") == "Bash":
                        cmd = (part.get("input") or {}).get("command")
                        # First line, not a flattened clip: a heredoc script
                        # squashed onto one line is 200 chars of its own source,
                        # where `python3 - <<'PY'` identifies it at a glance.
                        pending[str(part.get("id") or "")] = _clip_line(
                            cmd.strip().splitlines()[0], 120) if isinstance(cmd, str) and cmd.strip() else ""
                    if live:
                        events.append(_Event(ts, agent, *_tool_event(part), lineno, src))
                        if (part.get("name") in _FILE_TOOLS
                                and isinstance((part.get("input") or {}).get("file_path"), str)):
                            pending_edits[str(part.get("id") or "")] = len(events) - 1
                elif part.get("type") == "tool_result" and live and part.get("tool_use_id") in pending_edits:
                    # The line range an edit landed on, straight off the tool's
                    # own patch — never inferred from the old/new text chsum
                    # already clips. Folded onto the edit event's first line so
                    # both the model and `_files_touched` read it from one place.
                    idx = pending_edits.pop(part["tool_use_id"])
                    tur = rec.get("toolUseResult")
                    hunks = tur.get("structuredPatch") if isinstance(tur, dict) else None
                    if isinstance(hunks, list) and hunks and idx < len(events):
                        starts = [h["newStart"] for h in hunks
                                 if isinstance(h, dict) and isinstance(h.get("newStart"), int)]
                        ends = [h["newStart"] + h.get("newLines", 0) - 1 for h in hunks
                               if isinstance(h, dict) and isinstance(h.get("newStart"), int)]
                        if starts and min(starts) > 0:
                            ev = events[idx]
                            lines = ev.text.splitlines()
                            if lines:
                                lines[0] = f"{lines[0]} (lines {min(starts)}-{max(ends)})"
                                events[idx] = _Event(ev.when, ev.agent, ev.kind,
                                                     "\n".join(lines), ev.line, ev.source)
                elif (part.get("type") == "tool_result" and live
                        and part.get("tool_use_id") in pending):
                    # Bash results only: they carry verdicts (tests, builds). Read
                    # results are file dumps. The tail, because that's where the
                    # verdict line is.
                    body = part.get("content")
                    if isinstance(body, list):
                        body = "\n".join(b.get("text", "") for b in body
                                         if isinstance(b, dict) and b.get("type") == "text")
                    if isinstance(body, str) and body.strip():
                        out = body.strip()
                        head = f"[tail of {len(out)} chars] " if len(out) > 400 else ""
                        # A failure carries its command with it. The pairing is
                        # the point: "it broke" is no use without what broke.
                        if _failed(out, bool(part.get("is_error"))):
                            cmd = pending.get(part["tool_use_id"], "")
                            events.append(_Event(ts, agent, "failed",
                                                 f"{cmd}\n{_fail_excerpt(out)}",
                                                 lineno, src))
                        else:
                            events.append(_Event(ts, agent, "output", head + out[-400:],
                                                 lineno, src))
    # Stable, so a record's own blocks stay in the order they were emitted.
    events.sort(key=lambda e: e.when)
    TRACE.step("_events_since", anchor_line=anchor_line, anchor_ts=anchor_ts,
               until=until_ts or "(none)", events=len(events),
               sidecars=len(sources) - 1,
               span=f"{events[0].when}→{events[-1].when}" if events else "—",
               failed=sum(1 for e in events if e.kind == "failed"))
    return events


def _event_block(e: _Event) -> str:
    """One event as it appears in the extract. Factored out so `--dry-run` can
    size each event by the exact text that would be sent, rather than measuring
    `e.text` and re-deriving the framing around it."""
    who = f" agent {e.agent[:8]}" if e.agent else ""
    # Local clock, not the raw UTC slice: the human-facing sections above use
    # `_hhmm` (local), and a model copying a time out of this extract has to
    # land on the same clock as the header beside it.
    head = f"[{_hhmmss(e.when)}]{who} {e.kind}:"
    if "\n" in e.text:
        return "\n".join([head] + ["  " + ln for ln in e.text.splitlines()])
    return f"{head} {e.text}"


@dataclass
class _Turn:
    """One thing you contributed: a typed prompt, or an answer to the question
    tool — both count, or a range picked from prompts alone would skip a
    decision made by menu choice."""
    line: int
    when: str
    kind: str  # "said" | "answered"
    text: str
    # The record's own uuid, which a fork copy preserves where a line number
    # does not — the name a stored breakdown is filed under. "" for a turn
    # rebuilt from an anchor rather than read off a record.
    uuid: str = ""


def _answered(rec: dict) -> str:
    """Your answers to the question tool, `Q → A` per line, or "". Read from the
    structured `toolUseResult.answers` field, never the "Your questions have
    been answered" prose in the message body."""
    tur = rec.get("toolUseResult")
    if not isinstance(tur, dict):
        return ""
    answers = tur.get("answers")
    if not isinstance(answers, dict) or not answers:
        return ""
    return "\n".join(f"{q} → {a}" for q, a in answers.items()
                     if isinstance(q, str) and isinstance(a, str))


def _your_turns(path: pathlib.Path) -> list[_Turn]:
    """Every turn of yours in a transcript, in order, for `recap`'s range picker."""
    turns = []
    lines = path.read_text(errors="replace").splitlines()
    TRACE.file(path, "turns", records=len(lines))
    command_ids = _command_prompt_ids(_parsed(lines))
    for lineno, raw in enumerate(lines, start=1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (not isinstance(rec, dict) or rec.get("type") != "user"
                or rec.get("isCompactSummary") or _tool_injected(rec, command_ids)):
            continue
        ts = str(rec.get("timestamp") or "")
        text = _typed_text(rec)
        uid = str(rec.get("uuid") or "")
        if isinstance(text, str) and is_typed_prompt(text):
            turns.append(_Turn(lineno, ts, "said", text.strip(), uid))
            continue
        answered = _answered(rec)
        if answered:
            turns.append(_Turn(lineno, ts, "answered", answered, uid))
    TRACE.step("_your_turns", turns=len(turns),
               said=sum(1 for t in turns if t.kind == "said"),
               answered=sum(1 for t in turns if t.kind == "answered"),
               skill_bodies=len(command_ids),
               first=turns[0].line if turns else 0, last=turns[-1].line if turns else 0)
    return turns


# Sized to keep a chunk call cheap against the harness floor without
# fragmenting into so many calls that their fixed overhead dominates.
_CHUNK_MAX_CHARS = 8_000


def _chunk_material(events: list[_Event]) -> str:
    """What one chunk call sees: only its own events, oldest first, nothing
    else — no other chunk's material or output, and no prompt text repeated
    across every chunk. Isolation by construction: a thread handed this string
    has no way to see what any other chunk produced."""
    return "\n".join(f"#{i} {_event_block(e)}" for i, e in enumerate(events, start=1))


def _split_to_fit(bucket: list[_Event], max_chars: int) -> list[list[_Event]]:
    """One bucket, split evenly by event count into as many pieces as it takes
    to bring each under `max_chars` (by size, not by dropping). Even splitting
    rather than repeatedly peeling off a chunk-sized head: peeling leaves one
    shrinking remainder that has to be measured and split again, where an even
    split sizes every piece in one pass."""
    size = sum(len(_event_block(e)) for e in bucket)
    if size <= max_chars or len(bucket) <= 1:
        return [bucket]
    pieces = min(len(bucket), -(-size // max_chars))  # ceil(size / max_chars)
    step = -(-len(bucket) // pieces)  # ceil(len(bucket) / pieces)
    return [bucket[i:i + step] for i in range(0, len(bucket), step)]


def _chunk_rows(chunk: list[_Event]) -> list[int]:
    """[first, last] parent row a chunk's events sit on, or [] where every
    event is a sidecar's. This is what a chunk's bullets target: the run of
    rows the call read, exact, where a row the model copied out would not be."""
    rows = [e.line for e in chunk if not e.agent and e.line]
    return [min(rows), max(rows)] if rows else []


def _launch_rows(path: pathlib.Path) -> dict[str, int]:
    """Sidecar id to the parent row holding the `Agent` call that launched it.
    Each sidecar's `.meta.json` carries the `toolUseId` of that call, and one
    read of the parent resolves every id to its row. This is where a bullet
    describing an agent's work anchors: the agent's own rows sit in another
    file and do not order against the parent's."""
    want: dict[str, str] = {}
    for side in subagent_transcripts(path):
        try:
            d = json.loads(side.with_suffix(".meta.json").read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        tid = str(d.get("toolUseId") or "") if isinstance(d, dict) else ""
        if tid:
            want[tid] = side.stem.removeprefix("agent-")
    if not want:
        return {}
    rows: dict[str, int] = {}
    TRACE.file(path, "launch")
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        hits = [t for t in want if want[t] not in rows and f'"{t}"' in raw]
        if not hits:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        calls = {p.get("id") for p in content
                 if isinstance(p, dict) and p.get("type") == "tool_use"} if isinstance(content, list) else set()
        for tid in hits:
            if tid in calls:
                rows[want[tid]] = lineno
        if len(rows) == len(want):
            break
    TRACE.step("_launch_rows", sidecars=len(want), resolved=len(rows))
    return rows


def _first_row(targets: list) -> int:
    """The row an annotation anchors at: a bare number, or the start of a run."""
    if not targets:
        return 0
    head = targets[0]
    if isinstance(head, int):
        return head
    start = str(head).split("..")[0]
    return int(start) if start.isdigit() else 0


def _chunk_activity(chunk: list[_Event]) -> bool:
    """Whether a chunk becomes a call at all — a chunk holding only "you" events
    (a turn's own words) is a quiet gap, not something to summarise. Both the
    live call path and `_cost_rows` skip on this same test, so a dry run prices
    exactly what a real run would spend."""
    return any(e.kind != "you" for e in chunk)


def _chunk_events(events: list[_Event], boundaries: list[str],
                  max_chars: int) -> list[list[_Event]]:
    """Events split into chunks for independent digest calls, in chronological
    order. Every event lands in exactly one returned chunk — never dropped to
    fit. `boundaries` (turn timestamps) are bucketed onto first, so a chunk
    never straddles a turn gap; empty `boundaries` (catchup's single gap) is
    one bucket split by size alone. Oversized buckets split further via `_split_to_fit`."""
    if not events:
        return []
    if boundaries:
        stamps = sorted(boundaries)
        buckets: list[list[_Event]] = [[] for _ in stamps]
        for e in events:
            # Shouldn't occur before the first boundary, but lands in the first bucket if it does.
            i = max(0, bisect.bisect_right(stamps, e.when) - 1)
            buckets[i].append(e)
    else:
        buckets = [events]
    chunks: list[list[_Event]] = []
    for bucket in buckets:
        if bucket:
            chunks.extend(_split_to_fit(bucket, max_chars))
    TRACE.step("_chunk_events", events=len(events), boundaries=len(boundaries),
               buckets=sum(1 for b in buckets if b), chunks=len(chunks), max_chars=max_chars)
    return chunks


# ---------------------------------------------------------------------------
# the turn store
# ---------------------------------------------------------------------------
# One file per turn, holding every breakdown written for it. Named by the turn
# record's own `uuid`: a fork copy preserves `uuid` verbatim, rewrites
# `sessionId` on every copied row, and linearizes the rows in a different order,
# so neither of the other two names the same turn across two branch files.

_CLOSING_REASONS = frozenset({"end_turn", "stop_sequence", "refusal"})


def _fingerprint(*parts: str) -> str:
    """Twelve hex of a sha256 over `parts`. Names what produced a breakdown (the
    chunk prompt and the model) and what it was produced from (one chunk's
    material), so a stored breakdown is checked against both rather than taken
    on the strength of the file existing."""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:12]


def _turn_path(project_dir: str, uuid: str) -> pathlib.Path:
    """Where one turn's breakdowns live. `project_dir` is the transcript's own
    parent directory name — the slugified cwd, which both branches of a fork
    share, so a forked session reads the breakdowns its parent wrote."""
    return TURNS_DIR / project_dir / f"{uuid}.json"


_NO_BULLETS_PREFIX = "- *no bullet list came back for this turn"

_store_cache: dict[str, tuple[int, dict[str, list[dict]]]] = {}
# One read per sidecar for a whole run: a turn's bullets name the same few agents.
_agent_desc_cache: dict[str, str] = {}


def _agent_description(side: str) -> str:
    """The task line a sidecar was launched with, from its `.meta.json`. This
    is what names the agent a bullet describes; the bullet's own text names
    only "Agent" and its clipped id."""
    hit = _agent_desc_cache.get(side)
    if hit is not None:
        return hit
    try:
        d = json.loads(pathlib.Path(side).with_suffix(".meta.json").read_text(errors="replace"))
    except (OSError, ValueError):
        d = {}
    got = str(d.get("description") or "") if isinstance(d, dict) else ""
    _agent_desc_cache[side] = got
    return got


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _load_doc(target: pathlib.Path) -> dict | None:
    try:
        doc = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _save_doc(target: pathlib.Path, doc: dict) -> None:
    """Written whole and moved into place, so a concurrent read never opens half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    tmp.replace(target)


def _number_bullets(doc: dict) -> None:
    """Every bullet in a file carries a number issued once, from the counter the
    file's notes share (`issued`). Entries are walked in the order they were
    written and a part already numbered keeps its numbers, so a re-summary
    appends under fresh numbers and an id given out earlier still names the
    same bullet."""
    count = int(doc.get("issued") or 0)
    entries = [e for e in (doc.get("summaries") or []) if isinstance(e, dict)]
    for entry in sorted(entries, key=lambda e: str(e.get("written") or "")):
        for part in entry.get("parts") or []:
            if not isinstance(part, dict) or "numbers" in part:
                continue
            bullets = part.get("bullets") if isinstance(part.get("bullets"), list) else []
            part["numbers"] = list(range(count + 1, count + 1 + len(bullets)))
            count += len(bullets)
    doc["issued"] = count


def _latest_summary(doc: dict) -> dict | None:
    entries = [e for e in (doc.get("summaries") or []) if isinstance(e, dict)]
    return max(entries, key=lambda e: str(e.get("written") or ""), default=None)


_UUID_FIELD_RE = re.compile(r'"uuid":\s*"([0-9a-f-]{36})"')


def _stamp_turns(project_dir: str, docs: dict[pathlib.Path, dict]) -> int:
    """Files written before the transcript and row were stamped get both, once:
    this project's transcripts are scanned for their uuids, oldest first, so a
    fork's parent is the one a shared uuid binds to. A uuid found in no
    transcript is stamped with an empty session, or it would be scanned for on
    every read."""
    wanted = {str(d.get("uuid") or ""): p for p, d in docs.items()
              if "session" not in d and d.get("uuid")}
    if not wanted:
        return 0
    found: dict[str, tuple[str, int]] = {}
    opened = 0
    project = PROJECTS_ROOT / project_dir
    files = sorted((p for p in project.glob("*.jsonl") if not p.name.startswith("agent-")),
                   key=lambda p: p.stat().st_mtime) if project.is_dir() else []
    for tp in files:
        opened += 1
        TRACE.file(tp, "stamp")
        for lineno, raw in enumerate(tp.read_text(errors="replace").splitlines(), start=1):
            m = _UUID_FIELD_RE.search(raw)
            uid = m.group(1) if m else ""
            if uid in wanted and uid not in found:
                found[uid] = (tp.stem, lineno)
        if len(found) == len(wanted):
            break
    for uid, target in wanted.items():
        doc = docs[target]
        doc["session"], doc["line"] = found.get(uid, ("", 0))
        _number_bullets(doc)
        _save_doc(target, doc)
    TRACE.step("_stamp_turns", dir=project_dir, unstamped=len(wanted),
               stamped=len(found), transcripts=opened)
    return len(found)


def _load_store(project_dir: str) -> dict[str, list[dict]]:
    """Every turn and session file of one project, keyed by the transcript they
    were stamped with. One directory read per run: the listing calls this once
    per session, and every write into the directory moves a file into it, which
    changes the directory's mtime, so that keys the cache."""
    d = TURNS_DIR / project_dir
    try:
        stamp = d.stat().st_mtime_ns
    except OSError:
        return {}
    hit = _store_cache.get(project_dir)
    if hit and hit[0] == stamp:
        return hit[1]
    docs: dict[pathlib.Path, dict] = {}
    for p in d.glob("*.json"):
        doc = _load_doc(p)
        if doc:
            docs[p] = doc
    if any("session" not in doc for doc in docs.values()):
        _stamp_turns(project_dir, docs)
        stamp = d.stat().st_mtime_ns
    by: dict[str, list[dict]] = {}
    for doc in docs.values():
        session = str(doc.get("session") or "")
        if session:
            by.setdefault(session, []).append(doc)
    for group in by.values():
        group.sort(key=lambda doc: (str(doc.get("when") or ""), str(doc.get("uuid") or "")))
    TRACE.step("_load_store", dir=project_dir, files=len(docs), sessions=len(by))
    _store_cache[project_dir] = (stamp, by)
    return by


def _annotations_of(doc: dict) -> list[Annotation]:
    """What one file serves: the latest entry's bullets as `recap`, then its
    notes. Earlier entries stay on disk unserved, or one turn renders once per
    prompt revision; the marked non-bullet line is prose a call replied with,
    not a summary, so it is not served either."""
    uuid = str(doc.get("uuid") or "")
    session = str(doc.get("session") or "")
    turn = uuid if uuid != session else ""
    out: list[Annotation] = []
    latest = _latest_summary(doc)
    if latest:
        line = int(doc.get("line") or 0)
        for part in latest.get("parts") or []:
            if not isinstance(part, dict):
                continue
            # The run of rows the chunk read, so the viewer spreads a turn's
            # bullets along its work; a part from before rows were stored, or
            # one whose events were all a sidecar's, anchors at the turn's row.
            def span_targets(rows: list) -> list:
                rows = [r for r in rows if isinstance(r, int)]
                if len(rows) == 2 and rows[0] < rows[1]:
                    return [f"{rows[0]}..{rows[1]}"]
                return [rows[0]] if rows else []
            run = span_targets(part.get("rows") or []) or ([line] if line else [])
            spans = part.get("spans") or []
            # Written per bullet since a turn's gap can run several agents; a
            # part from before they were stored has neither and serves as it did.
            agents = part.get("agents") or []
            origins = part.get("origins") or []
            for k, (text, n) in enumerate(zip(part.get("bullets") or [],
                                              part.get("numbers") or [])):
                if not isinstance(text, str) or text.startswith(_NO_BULLETS_PREFIX):
                    continue
                own = span_targets(spans[k]) if k < len(spans) and isinstance(spans[k], list) else []
                body = text[2:] if text.startswith("- ") else text
                origin = origins[k] if k < len(origins) and isinstance(origins[k], dict) else None
                # The description is read here rather than stored, so the served
                # text follows the sidecar's own metadata and the stored bullet
                # stays the model's words.
                desc = _agent_description(str(origin.get("path") or "")) if (
                    origin and k < len(agents) and agents[k]) else ""
                out.append(Annotation(f"{uuid}#{n}", int(n), "recap",
                                      f"{desc}: {body}" if desc else body,
                                      own or run, str(latest.get("written") or ""), "",
                                      turn, session, origin=origin))
    for note in doc.get("notes") or []:
        if not isinstance(note, dict):
            continue
        n = int(note.get("n") or 0)
        out.append(Annotation(
            f"{uuid}#{n}", n, str(note.get("kind") or "note"), str(note.get("text") or ""),
            [t for t in (note.get("targets") or []) if isinstance(t, (int, str))],
            str(note.get("created") or ""), str(note.get("modified") or ""),
            turn, session, str(note.get("agent") or ""),
            int(note.get("row") or 0), str(note.get("quote") or ""),
            str(note.get("record") or "")))
    return out


def _turn_for_targets(path: pathlib.Path | None, targets: list,
                      agent: str = "") -> "_Turn | None":
    """The turn whose gap holds the first target row — the rightmost turn at or
    before it, the bisect `_chunk_events` uses. None where there is no row to
    place, which files the note under the session."""
    if not (targets and path is not None and path.exists() and not agent):
        return None
    turns = _your_turns(path)
    i = bisect.bisect_right([t.line for t in turns], _first_row(targets)) - 1
    return turns[i] if i >= 0 and turns[i].uuid else None


def _rewrite_note(project_dir: str, session: str, path: pathlib.Path | None,
                  spec: str, targets: list[int], kind: str, text: str, *,
                  quote: str = "", modified: str = "") -> str | None:
    """An edit onto the note `spec` names, keeping its number and its `created`.
    Returns the unchanged id. None where the id names nothing, or where the new
    targets belong under a different turn — that note is filed afresh, and the
    id it comes back with is what tells claude-history to drop the old one."""
    uuid, _, num = spec.partition("#")
    if not (uuid and num.isdigit()):
        return None
    turn = _turn_for_targets(path, targets)
    if (turn.uuid if turn else session) != uuid:
        return None
    target = _turn_path(project_dir, uuid)
    doc = _load_doc(target)
    if not doc:
        return None
    for note in doc.get("notes") or []:
        if int(note.get("n") or 0) != int(num):
            continue
        note.update({"kind": kind, "text": text, "targets": targets,
                     "modified": modified or _now_iso()})
        if quote:
            note["quote"] = quote
        _save_doc(target, doc)
        TRACE.step("_rewrite_note", file=target.name, n=num, kind=kind)
        return spec
    return None


def _file_note(project_dir: str, session: str, path: pathlib.Path | None,
               targets: list[int], kind: str, text: str, *, agent: str = "",
               row: int = 0, quote: str = "", record: str = "",
               created: str = "", modified: str = "") -> str:
    """A hand-typed annotation into the store: under the turn whose gap holds
    its row — the rightmost turn at or before it, the bisect `_chunk_events`
    uses — or under the session file where there is no parent row. Returns the
    id, which names the file and the number the note took."""
    turn = _turn_for_targets(path, targets, agent)
    target = _turn_path(project_dir, turn.uuid if turn else session)
    doc = _load_doc(target) or {}
    if turn:
        doc.setdefault("uuid", turn.uuid)
        doc.setdefault("when", turn.when)
        doc.setdefault("kind", turn.kind)
        doc.setdefault("line", turn.line)
    else:
        doc.setdefault("uuid", session)
    doc["session"] = session
    _number_bullets(doc)
    n = int(doc.get("issued") or 0) + 1
    doc["issued"] = n
    stamp = created or _now_iso()
    note: dict = {"n": n, "kind": kind, "text": text, "targets": targets,
                  "created": stamp, "modified": modified or stamp}
    if agent:
        note["agent"], note["row"] = agent, row
    if quote:
        note["quote"] = quote
    if record:
        note["record"] = record
    doc.setdefault("notes", []).append(note)
    _save_doc(target, doc)
    TRACE.step("_file_note", file=target.name, n=n, kind=kind,
               under="turn" if turn else "session", targets=targets or "session-level",
               agent=agent or "—")
    return f"{doc['uuid']}#{n}"


def _remove_annotation(project_dir: str, spec: str) -> Annotation | None:
    """Delete by id: the note leaves `notes`, or the bullet and its number leave
    their part together, so the next recap reprints the turn without it. None
    where the id names nothing served."""
    uuid, _, num = spec.partition("#")
    if not num.isdigit():
        return None
    n = int(num)
    target = _turn_path(project_dir, uuid)
    doc = _load_doc(target)
    if not doc:
        return None
    hit = next((a for a in _annotations_of(doc) if a.n == n), None)
    if hit is None:
        return None
    doc["notes"] = [x for x in (doc.get("notes") or [])
                    if not (isinstance(x, dict) and x.get("n") == n)]
    for entry in doc.get("summaries") or []:
        for part in entry.get("parts") or []:
            nums = part.get("numbers") or []
            if n in nums:
                k = nums.index(n)
                # Every per-bullet list is indexed by the same k, so one that
                # kept the removed entry would place the bullets after it by
                # the row, agent and origin of the one before.
                for key in ("numbers", "bullets", "spans", "agents", "origins"):
                    have = part.get(key)
                    if isinstance(have, list) and k < len(have):
                        part[key] = have[:k] + have[k + 1:]
    _save_doc(target, doc)
    TRACE.step("_remove_annotation", file=target.name, n=n, kind=hit.kind)
    return hit


_KIND_WORD = {"note": "note", "recap": "recap bullet"}


def _list_annotations(kind: str, cols: int, all_projects: bool) -> int:
    """One kind of the store's annotations across the scope, in the shape the
    digest listing uses: a column line each carrying the text, the message it
    points at wrapped beneath. Split by kind because a `recap` bullet is a
    model's and a `note` is yours, and one listing holding both reads as one
    kind of thing.

    The id leads and the ref is absent: the id is what `--show` and `--delete`
    take, and a 35-character ref beside it folds the row on an 88-column
    terminal, where a fold reads as another row."""
    pooled = _pooled_annotations(kind, all_projects)
    if not pooled:
        print(f"no {_KIND_WORD[kind]}s in {_scope_label(all_projects)}", file=sys.stderr)
        return 1
    heads = ("id", "created", *(("project",) if all_projects else ()), "")
    rows = []
    for _d, src, a in pooled:
        # The project column costs a transcript head each, so it is read only
        # where it distinguishes rows — under `--all`.
        m = extract_meta(src) if all_projects and src else Meta()
        rows.append((f"{a.id[:8]}#{a.n}" + (f"/{a.agent[:8]}" if a.agent else ""),
                     _age_or_date(a.created),
                     *((m.project_name or "-",) if all_projects else ()),
                     _clip_line(a.text, 60),
                     _clip_line(a.quote, 160) or ("(session-level)" if not a.targets
                                                  else f"row {_first_row(a.targets)}")))
    _print_table(heads, rows, cols, project_col=2 if all_projects else None)
    # Flushed first, or the hint jumps the list: stdout is block-buffered when
    # piped, stderr never is.
    sys.stdout.flush()
    print(f"\n{_scope_label(all_projects)}\nShow one: `chsum note --show <id>`  ·  "
          f"drop one: `chsum note --delete <id>`  ·  "
          f"{_plural(len(pooled), _KIND_WORD[kind])}",
          file=sys.stderr)
    return 0


def _find_annotation(anns: list[Annotation], spec: str) -> Annotation:
    """The one annotation an id names, by a prefix of the file's uuid and the
    exact number after `#`. A prefix matching two files is a typo, listed
    rather than picked."""
    uuid, sep, num = spec.partition("#")
    if not sep or not num.isdigit():
        raise SystemExit(f"{spec!r} is not an id — they look like <uuid>#<n>; "
                         "`chsum note --list` and `chsum recap --list` show them")
    hits = [a for a in anns if a.id.split("#")[0].startswith(uuid) and a.n == int(num)]
    if not hits:
        raise SystemExit(f"no annotation {spec!r} — `chsum note --list` and "
                         "`chsum recap --list` show them")
    if len(hits) > 1:
        rows = "\n".join(f"  {a.id}  {_clip_line(a.text, 60)}" for a in hits)
        raise SystemExit(f"{spec!r} matches {len(hits)} annotations — use more characters:\n{rows}")
    return hits[0]


def _sidecar_path(path: pathlib.Path, agent: str) -> pathlib.Path:
    return _mark_file(path, agent) or path if agent else path


def _annotations_for(path: pathlib.Path) -> list[Annotation]:
    """Everything the store holds for one transcript, file order then number."""
    return [a for doc in _load_store(path.parent.name).get(path.stem, [])
            for a in _annotations_of(doc)]


def _pooled_annotations(
        kind: str, all_projects: bool
) -> list[tuple[str, pathlib.Path | None, Annotation]]:
    """Every annotation of one kind in scope, newest first, each beside the
    project directory holding it and the transcript it points at. The directory
    travels with the annotation because `--delete` writes back into the file it
    came from, which under `--all` is not the directory of the cwd. A transcript
    deleted since leaves `None`, and the row falls back to the session id. An
    empty `kind` takes every kind."""
    paths = {p.stem: p for p in transcripts(local=not all_projects)}
    out = []
    for d in _project_dirs(all_projects):
        for session, docs in _load_store(d).items():
            for a in (a for doc in docs for a in _annotations_of(doc)):
                if kind and a.kind != kind:
                    continue
                out.append((d, paths.get(session), a))
    out.sort(key=lambda r: r[2].created, reverse=True)
    return out


# A gap that closed with no assistant record in it. Distinct from "", which is
# a gap still open: you typed again before Claude replied, so the turn is
# closed and there is simply no record for a later check to resolve against.
_CLOSED_UNANSWERED = "-"


def _turn_closers(path: pathlib.Path, spine: list[_Turn], until_ts: str) -> list[str]:
    """Per turn, the uuid of the last assistant record in its gap once that gap
    is closed, `_CLOSED_UNANSWERED` where it closed holding no assistant record,
    and "" where it is still open. A gap still open takes more records after the
    ones a breakdown was built from, so nothing derived from it is storable.

    Two ways a gap closes, and either is enough. A later boundary bounds it: the
    transcript is append-only and a record timestamped inside a bounded gap has
    nowhere to arrive. Otherwise the gap's last assistant record carries a
    closing `stop_reason`, which bounds the far end of the window itself. The
    boundary test is what covers an interrupted turn, whose stretch ends on
    `tool_use` and never closes on its own — measured 38 of 634.

    The last assistant record, not the first closing value in the gap: a
    `<task-notification>` re-invokes the assistant with nothing typed, which
    appends work after a closing value in 14 of 645 measured stretches."""
    if not spine:
        return []
    stamps = [t.when for t in spine]
    last_uuid = [""] * len(spine)
    last_when = [""] * len(spine)
    last_reason = [""] * len(spine)
    for rec in _parsed(path.read_text(errors="replace").splitlines()):
        if rec.get("type") != "assistant":
            continue
        when = str(rec.get("timestamp") or "")
        if not when or when < stamps[0] or (until_ts and when > until_ts):
            continue
        i = bisect.bisect_right(stamps, when) - 1
        # A fork linearizes the same rows in a different order, so the latest
        # record in a gap is the latest by timestamp, not the last one read.
        if i < 0 or when < last_when[i]:
            continue
        msg = rec.get("message")
        last_uuid[i] = str(rec.get("uuid") or "")
        last_when[i] = when
        last_reason[i] = str(msg.get("stop_reason") or "") if isinstance(msg, dict) else ""
    bounded = lambda i: i < len(spine) - 1 or bool(until_ts)
    closed = lambda i: bounded(i) or last_reason[i] in _CLOSING_REASONS
    # `uid or _CLOSED_UNANSWERED`, not `uid`: a bounded gap holding no assistant
    # record returned "" and read downstream as open, so those turns were
    # re-called on every bare recap and never stored — 7 of 69 in one session.
    out = [(uid or _CLOSED_UNANSWERED) if closed(i) else ""
           for i, uid in enumerate(last_uuid)]
    # The final gap of the session being appended to right now stays open
    # whatever it closed on: the next record lands in it.
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if out and not until_ts and live and path.stem == live:
        out[-1] = ""
    TRACE.step("_turn_closers", turns=len(spine), closed=sum(1 for u in out if u),
               open=sum(1 for u in out if not u),
               unanswered=sum(1 for u in out if u == _CLOSED_UNANSWERED),
               by_reason=sum(1 for r in last_reason if r in _CLOSING_REASONS),
               until=until_ts or "(end of session)")
    return out


def _read_breakdown(project_dir: str, turn: _Turn, instructions: str, model: str,
                    materials: list[str]) -> list[list[str]] | None:
    """This turn's stored bullets, one list per chunk, or None. A hit needs the
    entry to name the same instructions and model, to hold one part per chunk,
    and every part to name the material of the chunk it is read for — the store
    is checked against what it claims, the same rule as `resolve_ref`."""
    try:
        doc = json.loads(_turn_path(project_dir, turn.uuid).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    for entry in doc.get("summaries") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("instructions") != instructions or entry.get("model") != model:
            continue
        parts = entry.get("parts")
        if not isinstance(parts, list) or len(parts) != len(materials):
            return None
        out = []
        for part, material in zip(parts, materials):
            if not isinstance(part, dict) or part.get("material") != material:
                return None
            bullets = part.get("bullets")
            out.append([b for b in bullets if isinstance(b, str)]
                       if isinstance(bullets, list) else [])
        return out
    return None


def _write_breakdown(project_dir: str, turn: _Turn, closed_by: str,
                     instructions: str, model: str, parts: list[dict],
                     session: str) -> None:
    """One turn's breakdown onto disk. An entry written under different
    instructions stays beside the new one rather than being dropped: the text a
    past recap printed remains findable after `_CHUNK_PROMPT` changes. The
    transcript and row are stamped here, where the recap holds both, so serving
    the file by transcript later is a filter and not a scan."""
    target = _turn_path(project_dir, turn.uuid)
    doc = _load_doc(target) or {}
    doc.update({"uuid": turn.uuid, "when": turn.when, "kind": turn.kind,
                "closed_by": closed_by, "session": session, "line": turn.line})
    kept = [e for e in (doc.get("summaries") or [])
            if isinstance(e, dict) and (e.get("instructions") != instructions
                                        or e.get("model") != model)]
    doc["summaries"] = kept + [{
        "instructions": instructions, "model": model,
        "written": _now_iso(),
        "parts": parts}]
    _number_bullets(doc)
    _save_doc(target, doc)


def _turn_chunks(chunks: list[list[_Event]], boundaries: list[str]) -> list[list[int]]:
    """Chunk indices per turn, in order — the same rightmost-turn-at-or-before
    bisect `_chunk_events` bucketed them with, so a turn owns exactly the chunks
    built from its own gap."""
    stamps = sorted(boundaries)
    per: list[list[int]] = [[] for _ in stamps]
    for i, chunk in enumerate(chunks):
        j = max(0, bisect.bisect_right(stamps, chunk[0].when) - 1)
        per[j].append(i)
    return per


def _cached_turns(project_dir: str, spine: list[_Turn], chunks: list[list[_Event]],
                  boundaries: list[str], instructions: str,
                  model: str) -> tuple[list[list[list[str]] | None], set[int]]:
    """(stored bullets per turn or None, the chunk indices already on disk).
    A turn is a hit or a miss whole: a partial hit would call for some of its
    chunks and read the rest, and the two orderings would have to be merged."""
    per_turn = _turn_chunks(chunks, boundaries)
    hits: list[list[list[str]] | None] = []
    done: set[int] = set()
    for turn, idxs in zip(spine, per_turn):
        materials = [_fingerprint(_chunk_material(chunks[i])) for i in idxs]
        got = (_read_breakdown(project_dir, turn, instructions, model, materials)
               if turn.uuid and idxs else None)
        hits.append(got)
        if got is not None:
            done.update(idxs)
    TRACE.step("_cached_turns", turns=len(spine),
               hits=sum(1 for h in hits if h is not None),
               misses=sum(1 for h in hits if h is None),
               chunks_cached=len(done), chunks=len(chunks),
               instructions=instructions, model=model, dir=str(TURNS_DIR / project_dir))
    return hits, done


def _store_turn(project_dir: str, turn: _Turn, closed_by: str, parts: list[dict],
                failed: bool, instructions: str, model: str, session: str) -> bool:
    """One turn onto disk, the moment its own chunks are in — a run killed
    part-way keeps every turn that completed. A turn holding a failed call is
    not written, or the missing part would come back as a hit.

    A turn whose gap is still open is written too, with `closed_by` empty. That
    gap takes more records after the ones these bullets were built from, and
    the next run's material carries them: `_cached_turns` fingerprints each
    chunk's material, the fingerprint moves, the turn misses, and a fresh entry
    lands beside this one under a later `written`. Holding the open turn back
    instead leaves the run that produced its bullets as the only place they
    exist, and every later run re-derives it from nothing.

    Between the two runs a reader is served bullets that describe the work up
    to the moment of the run and no further. The empty `closed_by` is what
    separates those from a settled turn's."""
    if failed or not turn.uuid:
        return False
    # A turn whose every chunk was quiet is covered: nothing happened between it
    # and your next one, so there is nothing to summarise and no call was made.
    # Without this it is stored nowhere, and every bare recap restarts on it —
    # 5 turns of one 69-turn session, re-walked on every run.
    if not any(part["bullets"] for part in parts) and not all(
            part.get("quiet") for part in parts):
        return False
    _write_breakdown(project_dir, turn, closed_by, instructions, model, parts, session)
    return True


_KIND_LABELS = {"said": "what the assistant said", "edit": "file edits",
                "ran": "commands run", "output": "command output",
                "failed": "failures (command + error)", "you": "your own turns",
                "spawn": "subagents spawned", "tool": "other tool calls",
                "command": "slash commands you ran",
                "shell": "shell commands you ran with `!`",
                "compacted": "conversation compaction"}


_EDIT_LINES_RE = re.compile(r"^(.+?) \(lines (\d+)-(\d+)\)$")


def _files_touched(events: list[_Event]) -> list[str]:
    """Full path and line range per file edited in this slice — computed from
    the line range `_events_since` folded onto each edit event's first line
    (itself off the Edit tool's own `structuredPatch`, never inferred), not
    narrated by a model. A range is missing only when the tool result never
    carried one (e.g. `Write`, a whole file, not a range)."""
    by_path: dict[str, list[str]] = {}
    order: list[str] = []
    for e in events:
        if e.kind != "edit" or not e.text:
            continue
        head = e.text.splitlines()[0]
        m = _EDIT_LINES_RE.match(head)
        path, rng = (m.group(1), f"{m.group(2)}-{m.group(3)}") if m else (head, "")
        if path not in by_path:
            by_path[path] = []
            order.append(path)
        if rng and rng not in by_path[path]:
            by_path[path].append(rng)
    return [f"- `{path}`" + (f":{', '.join(by_path[path])}" if by_path[path] else "")
           for path in order]


# ---------------------------------------------------------------------------
# The hooks Claude Code runs (`chsum hook`)
# ---------------------------------------------------------------------------
# `hooks/hooks.json` runs `chsum hook post-tool-use` and `chsum hook
# session-start`, one JSON payload per event on stdin. The PostToolUse hook
# writes the checkpoint commits `_checkpoint_shas` reads back.

# The sha of the last checkpoint this repo wrote. The tree behind it is what a
# new call is compared against.
# Polled rather than waited on: the hook is killed at 30 seconds, and a kill
# inside the commit-then-reset sequence leaves the branch tip on a checkpoint
# commit — the failure the lock prevents.








def _read_payload() -> dict:
    """The hook payload Claude Code writes to stdin. Anything unparseable
    returns an empty payload, which every hook path below exits 0 on."""
    try:
        data = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not data.strip():
        return {}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
















# No `chsum` on PATH: a plugin installs into its own directory and places no
# command, so the file the hook imported is the one a session runs.
_ENTRYPOINT_CONTEXT = (
    "chsum has no command on PATH here. It runs from this plugin: invoke it as "
    "`python3 {path}` wherever a chsum command is called for — "
    "`python3 {path} digest --last`, `python3 {path} recap`."
)

_MIGRATE_CONTEXT = (
    "chsum: {sessions} in this project hold {checkpoints} that only "
    "`HEAD`'s reflog records. Those are lost to a `git gc`, to the reflog "
    "expiring, and to the removal of the worktree they were written in. "
    "`chsum checkpoints --migrate` rebuilds them as chains under "
    "`refs/chsum/<session>`, which git keeps until dropped with "
    "`chsum checkpoints --prune`. Mention it once; it is additive and "
    "reversible, and the user decides."
)

_OPT_IN_CONTEXT = (
    "chsum: per-turn git checkpointing is undecided for this project "
    "({gate} absent). Ask the user once, plainly, whether to enable it — "
    "each call that changes the tree is committed under `refs/chsum/<session>`, "
    "leaving HEAD, the index and the working tree untouched and showing in no "
    "normal git command, so `chsum recap` and `chsum digest --writes` can read "
    "real git diffs for file and line tracking instead of reconstructing them "
    "from the transcript. The chains are kept until dropped with "
    "`chsum checkpoints --prune`. Write `enabled` or `declined` to {gate} based "
    "on their answer, then never ask again in this checkout."
)


def _opt_in_context(git_dir: pathlib.Path) -> str | None:
    """The `additionalContext` text where the gate file is absent, `None` where
    it exists — the gate is written by the `chsum` skill, never here, so a
    project that decided is not asked a second time."""
    gate = git_dir / GATE_NAME
    if gate.exists():
        return None
    return _OPT_IN_CONTEXT.format(gate=gate)


def _migrate_context(cwd: pathlib.Path, git_dir: pathlib.Path) -> str | None:
    """The text raised where this project holds checkpoints that only the reflog
    records. Those are lost to a `gc`, to the reflog expiring, and to the removal
    of the worktree they were written in, and one command makes them durable.

    `None` where checkpointing is off, where nothing is reflog-only, or where
    git is unreachable — the check is two git calls and runs once per session."""
    if not _enabled(git_dir):
        return None
    try:
        reflog = checkpoints.reflog_sessions(cwd)
        if not reflog:
            return None
        chained = {uuid for uuid, _sha, _n in _checkpoint_refs(cwd)}
    except (OSError, subprocess.SubprocessError):
        return None
    behind = [s for s in reflog if s not in chained]
    if not behind:
        return None
    return _MIGRATE_CONTEXT.format(
        sessions=_plural(len(behind), "session"),
        checkpoints=_plural(sum(reflog[s] for s in behind), "checkpoint"))


def _hook_session_start(payload: dict, entrypoint: str = "") -> int:
    """SessionStart: fires on startup, resume, clear, compact and fork, before
    the first turn. Nothing here blocks a session starting.

    `entrypoint` is the `chsum.py` the hook imported — the plugin's own copy.
    A plugin places nothing on PATH, so a session with no `chsum` command
    reaches the tool by running that file. The line emits where `chsum` is
    absent from PATH; where the command resolves, it runs and the line would
    name a second copy of the same file."""
    parts: list[str] = []
    if entrypoint and shutil.which("chsum") is None:
        parts.append(_ENTRYPOINT_CONTEXT.format(path=entrypoint))
    cwd_str = payload.get("cwd")
    if not isinstance(cwd_str, str) or not cwd_str:
        return _emit_session_context(parts)
    cwd = pathlib.Path(cwd_str)
    if not cwd.is_dir():
        return _emit_session_context(parts)
    if shutil.which("git") is None:
        return _emit_session_context(parts)

    try:
        git_dir = _git_dir(cwd)
        if git_dir is None:
            return _emit_session_context(parts)
        context = _opt_in_context(git_dir)
        if context is not None:
            parts.append(context)
        else:
            # Only where the project already decided: a project being asked to
            # opt in has no checkpoints to migrate yet.
            behind = _migrate_context(cwd, git_dir)
            if behind is not None:
                parts.append(behind)
        return _emit_session_context(parts)
    except Exception as e:  # noqa: BLE001 — never let this hook fail a session
        print(f"chsum session-start hook: {e}", file=sys.stderr)
    return 0


def _emit_session_context(parts: list[str]) -> int:
    """The SessionStart reply Claude Code parses, or nothing where no part was
    built. Stdout carries the JSON, so every diagnostic goes to stderr."""
    if not parts:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "\n\n".join(parts),
    }}))
    return 0


def _transcript_row(transcript_path: str, tool_use_id: str) -> str:
    """`<session>:<line>` for the `tool_use` a checkpoint names — the address
    every other chsum surface uses, so a commit and a digest row name the same
    place.

    Read-side, not hook-side. The transcript record holding a `tool_use` is
    flushed after PostToolUse fires: a probe in session `6c650975` found 1,055
    rows in the file and the firing call's own id in none of them, so a row
    resolved inside the hook is always "". The commit carries the id, and the
    row resolves here once the file is complete.

    A subagent's call reaches this hook carrying the parent's session and the
    parent's transcript path, and its own record is written to that session's
    `subagents/agent-<id>.jsonl`. The sidecars are searched after the parent
    and address as `<session>/<agent>:<line>`, the form a digest already uses.

    "" where the path is absent, unreadable, or holds no matching row."""
    if not transcript_path or not tool_use_id:
        return ""
    src = pathlib.Path(transcript_path)
    for at, base in [(src, src.stem[:8])] + [
            (side, f"{src.stem[:8]}/{side.stem.removeprefix('agent-')[:8]}")
            for side in sorted(src.with_suffix("").glob("subagents/agent-*.jsonl"))]:
        try:
            lines = at.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i in range(len(lines) - 1, -1, -1):
            row = lines[i]
            if '"tool_use"' in row and (f'"id": "{tool_use_id}"' in row
                                        or f'"id":"{tool_use_id}"' in row):
                return f"{base}:{i + 1}"
    return ""


def _hook_post_tool_use(payload: dict) -> int:
    """PostToolUse: one checkpoint per tool call that changed the tree, stamped
    with the transcript row of the call.

    Every tool reaches here, and `_write_checkpoint`'s `git status` decides.
    A list of the tools that write would carry a Task agent's edits, an MCP
    tool's writes and a Skill's writes to the next matching call's checkpoint,
    under a row that did not make them. The cost is one working-tree scan per
    call — 0.041s over 21 files, growing with the tree.

    Exit 2 is the code that blocks a turn, so every condition here exits 0 and
    every exception stops at this frame."""
    session_id = payload.get("session_id")
    cwd_str = payload.get("cwd")
    if not isinstance(session_id, str) or not session_id:
        return 0
    if not isinstance(cwd_str, str) or not cwd_str:
        return 0
    cwd = pathlib.Path(cwd_str)
    if not cwd.is_dir():
        return 0
    if shutil.which("git") is None:
        return 0

    try:
        git_dir = _git_dir(cwd)
        if git_dir is None:
            return 0
        proceed, lock_fd = _hold_checkpoint_lock(git_dir)
        if not proceed:
            return 0
        try:
            _self_heal(cwd, git_dir)
            _write_checkpoint(cwd, git_dir, session_id,
                              str(payload.get("tool_use_id") or ""))
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
    except Exception as e:  # noqa: BLE001 — never let this hook fail the turn
        print(f"chsum checkpoint hook: {e}", file=sys.stderr)
    return 0










def _set_gate(project: pathlib.Path, on: bool) -> int:
    """Write the per-project gate that decides whether the hook checkpoints.

    `enabled` and `declined` are both decisions, and both stop the session-start
    prompt asking again. Turning it off leaves every chain in place — what stops
    is new checkpoints — so the views keep reading what was already recorded."""
    git_dir = _git_dir(project)
    if git_dir is None:
        raise SystemExit(f"{project} is not a git repository, so there is "
                         f"nothing to checkpoint")
    gate = git_dir / GATE_NAME
    was = gate.read_text().strip() if gate.exists() else ""
    want = "enabled" if on else "declined"
    if was == want:
        print(f"checkpointing is already {want} for {project}", file=sys.stderr)
        return 0
    try:
        gate.write_text(want + "\n")
    except OSError as e:
        raise SystemExit(f"{gate} is unwritable ({e})")
    held = len(_checkpoint_refs(project))
    print(f"checkpointing {want} for {project}"
          + (f"\n{_plural(held, 'chain')} already recorded "
             f"{'stays' if held == 1 else 'stay'} readable" if held and not on else ""),
          file=sys.stderr)
    return 0


def _migrate_checkpoints(project: pathlib.Path, names: dict, dry_run: bool) -> int:
    """Rebuild reflog-only checkpoints as chains, one session at a time.

    A session whose chain already holds everything the reflog does is left
    alone; the rest are rebuilt from the trees and messages they already carry,
    which makes them reachable and so proof against a `gc`, the reflog expiring,
    and the removal of the worktree they were written in.

    The commits get new shas, since a parent is part of what a sha hashes. That
    costs nothing downstream: every reader here resolves a checkpoint by the
    session and call id in its message, never by sha."""
    reflog = checkpoints.reflog_sessions(project)
    if not reflog:
        print(f"no reflog checkpoints in {project}", file=sys.stderr)
        return 1
    rows, work = [], []
    for uuid in sorted(reflog):
        marks = _checkpoint_shas(project, uuid)
        chained = len(_chain_shas(project, uuid))
        behind = len(marks) - chained
        rows.append((uuid[:8], str(len(marks)), str(chained),
                     "rebuild" if behind > 0 else "current",
                     names.get(uuid, "")))
        if behind > 0:
            work.append((uuid, marks))
    _print_table(("session", "checkpoints", "chained", ""), rows,
                 max(40, min(shutil.get_terminal_size((100, 24)).columns, 88)))
    sys.stdout.flush()
    if not work:
        print("\nevery session is already chained", file=sys.stderr)
        return 0
    if dry_run:
        print(f"\n{_plural(len(work), 'session')} would be rebuilt. Run it "
              f"without `--dry-run` to rebuild them.", file=sys.stderr)
        return 0

    built = missed = 0
    total = sum(len(marks) for _uuid, marks in work)
    # A rebuild is one `commit-tree` per checkpoint, so a corpus of several
    # hundred runs for a while with nothing to show. `_status` narrates it on a
    # terminal and writes nothing anywhere else, leaving stdout the table.
    for n, (uuid, marks) in enumerate(work, 1):
        tell = lambda done, of, n=n, uuid=uuid: _status(
            f"… rebuilding {uuid[:8]} — session {n} of {len(work)}, "
            f"{done} of {of} · {built + done} of {total} overall")
        tell(0, len(marks))
        written, skipped, note = _migrate_chain(project, uuid, marks, tell)
        built += written
        missed += skipped
        if note:
            _clear_status()
            print(f"  {uuid[:8]}: {note}", file=sys.stderr)
    _clear_status()
    print(f"\nrebuilt {_plural(built, 'checkpoint')} across "
          f"{_plural(len(work), 'session')}"
          + (f", {missed} skipped as already pruned" if missed else "")
          + ". The reflog entries stay where they are; the reader merges both.",
          file=sys.stderr)
    return 0


def cmd_checkpoints(args) -> int:
    """The checkpoint chains this repo holds, and the command that drops one.

    A chain is reachable, so git never prunes it: retention is asked for rather
    than waited for. Dropping a ref makes its commits unreachable again — the
    state every checkpoint was in before chains — and git reclaims them on its
    next `gc`. The transcript is untouched either way; what goes is the tree
    snapshot each call wrote."""
    project = pathlib.Path(args.repo) if getattr(args, "repo", "") else pathlib.Path.cwd()
    if shutil.which("git") is None:
        raise SystemExit("git is not on PATH, so there are no checkpoints to read")
    if getattr(args, "enable", False) or getattr(args, "disable", False):
        if args.enable and args.disable:
            raise SystemExit("--enable and --disable ask for opposite things")
        return _set_gate(project, bool(args.enable))
    if getattr(args, "migrate", False):
        return _migrate_checkpoints(project, load_names(), args.dry_run)
    chains = _checkpoint_refs(project)
    if not chains:
        print(f"no checkpoint chains in {project}", file=sys.stderr)
        return 1

    names = load_names()
    cutoff = _parse_since(args.prune) if args.prune else None
    rows, doomed = [], []
    for uuid, sha, count in chains:
        marks = _chain_shas(project, uuid)
        last = marks[-1][0] if marks else ""
        stale = cutoff is not None and (_parse_ts(last) is None
                                        or _parse_ts(last).timestamp() < cutoff)
        if stale:
            doomed.append((uuid, sha, count))
        rows.append((uuid[:8], str(count), _age_or_date(last, 0) if last else "—",
                     "drop" if stale else "keep" if cutoff else "",
                     names.get(uuid, "")))
    # `_print_table` takes one field more than it has heads — the last is the
    # trailing prose, here the name. The verdict column appears only where
    # `--prune` asked a question of each row.
    heads = ("session", "checkpoints", "last", *(("",) if cutoff else ()))
    shown = [r if cutoff else (r[0], r[1], r[2], r[4]) for r in rows]
    _print_table(heads, shown,
                 max(40, min(shutil.get_terminal_size((100, 24)).columns, 88)))
    sys.stdout.flush()

    if cutoff is None:
        git_dir = _git_dir(project)
        state = ("on" if git_dir and _enabled(git_dir) else "off")
        print(f"\n{project}\ncheckpointing is {state} here · "
              f"`chsum checkpoints --{'disable' if state == 'on' else 'enable'}`"
              f"  ·  drop old chains: `chsum checkpoints --prune 30d`",
              file=sys.stderr)
        return 0
    if not doomed:
        print(f"\nnothing older than {args.prune}", file=sys.stderr)
        return 0
    if args.dry_run:
        print(f"\n{_plural(len(doomed), 'chain')} would be dropped. "
              f"Run it without `--dry-run` to drop them.", file=sys.stderr)
        return 0
    dropped = 0
    for uuid, sha, _count in doomed:
        try:
            done = _git(["git", "update-ref", "-d", checkpoint_ref(uuid), sha], project)
        except (OSError, subprocess.SubprocessError):
            continue
        dropped += 1 if done.returncode == 0 else 0
    print(f"\ndropped {_plural(dropped, 'chain')}. The commits are unreachable "
          f"now; git reclaims them on its next `gc`.", file=sys.stderr)
    return 0


def cmd_hook(args) -> int:
    payload = _read_payload()
    if args.event == "post-tool-use":
        return _hook_post_tool_use(payload)
    return _hook_session_start(payload)






_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")




def _turn_of_call(path: pathlib.Path) -> dict:
    """`call id → (turn number, the row that opened the turn)`. The turn is what
    groups a session's writes: a prompt is answered by the calls beneath it, and
    the files they wrote are that turn's."""
    out, turn, opened = {}, 0, None
    for r in collect_rows(path, ("message", "tool", "command")):
        if r.starts_turn:
            turn, opened = turn + 1, r
        elif r.tool_id:
            out[r.tool_id] = (turn, opened)
    return out




def _turn_checkpoints(spine: list[_Turn], until_ts: str, project_dir: pathlib.Path | None,
                      session_uuid: str) -> list[list[tuple[str, str]]]:
    """Per turn, every (sha, tool_use id) checkpoint covering its window,
    oldest first, or []. The reflog arithmetic alone — no diffs — so the counts
    are available before the model calls that `_turn_files` runs after, and the
    reflog is read once.

    `spine[i].when` to `spine[i+1].when` (or `until_ts` for the last turn) is
    each turn's window — same rightmost-boundary convention `_chunk_events`
    bisects on. A window holds one checkpoint per tool call that changed the
    tree, and each is kept: collapsing them to the last carries the turn's
    whole diff under one call's id, naming a row that made part of it."""
    covers: list[list[tuple[str, str]]] = [[] for _ in spine]
    if not spine:
        return covers
    checkpoints = _checkpoint_shas(project_dir, session_uuid)
    if not checkpoints:
        TRACE.step("_turn_checkpoints", turns=len(spine), checkpoint=0,
                   transcript=len(spine), reason="no checkpoint for this session")
        return covers
    ci, n = 0, len(checkpoints)
    for i in range(len(spine)):
        start = spine[i].when
        is_last = i == len(spine) - 1
        while ci < n and checkpoints[ci][0] < start:
            ci += 1  # a checkpoint stamped before this window opened isn't this turn's
        j = ci
        # An empty `until_ts` bounds the last turn by nothing, so every remaining
        # checkpoint is its own — `<= ""` would take none of them.
        while j < n and ((not until_ts or checkpoints[j][0] <= until_ts) if is_last
                          else checkpoints[j][0] < spine[i + 1].when):
            j += 1
        covers[i] = [(sha, ref) for _, sha, ref in checkpoints[ci:j]]
        ci = j
    covered = sum(1 for c in covers if c)
    TRACE.step("_turn_checkpoints", turns=len(spine), checkpoints=n,
               checkpoint=covered, transcript=len(covers) - covered,
               until=until_ts or "(none)")
    return covers


def _turn_files(turn_events: list[list[_Event]], covers: list[list[tuple[str, str]]],
                project_dir: pathlib.Path | None,
                transcript: str = "") -> list[list[str]]:
    """Per-turn files-touched, one entry per turn — a real `git diff` between
    checkpoints wherever `covers[i]` names one, since a checkpoint sees every
    change regardless of how it was made (a raw `sed -i`, not just
    Edit/Write/MultiEdit) and is never stale; `_files_touched`'s transcript
    scan for every other turn."""
    out: list[list[str]] = []
    last_sha: str | None = None
    diffs = 0
    for i, evs in enumerate(turn_events):
        marks = covers[i] if i < len(covers) else []
        if not marks:
            out.append(_files_touched(evs))
            continue
        rows: list[str] = []
        for sha, ref in marks:
            # The very first checkpoint seen has no prior checkpoint to diff
            # from, so its baseline is its own first parent — the branch tip
            # right before checkpointing started, not "nothing".
            prev = last_sha if last_sha is not None else f"{sha}^"
            # "after", not "at": the diff holds every change in the tree
            # between two checkpoints, and a checkpoint stages the whole tree.
            # Parallel subagents share that tree, so a call's checkpoint
            # carries whatever the others wrote in the same window — measured
            # in session `6c650975`, where the agent writing `scratch-note-c`
            # has checkpoints at `ab820108:24` whose diff holds
            # `scratch-note-b`. The row places the diff in time. It does not
            # name what produced it.
            at = _transcript_row(transcript, ref)
            head = f"- checkpoint `{sha[:12]}`"
            head += f" after `{at}`" if at else ""
            head += (f" — the tree's changes since the previous checkpoint"
                     f" (`git show {sha[:12]}` for the snapshot)")
            rows.append(head)
            rows += [f"  {f}" for f in _checkpoint_diff_files(project_dir, prev, sha)]
            last_sha = sha
            diffs += 1
        out.append(rows)
    TRACE.step("_turn_files", turns=len(turn_events), diffs=diffs)
    return out


def _checkpoint_source(covers: list[list[tuple[str, str]]]) -> list[str]:
    """Which source produced each turn's files, as counts chsum measured. A
    turn whose files came from the transcript scan must not read as one backed
    by a checkpoint — the two differ in what they can see and in whether their
    line ranges are current. Computed and printed above the model-written
    timeline, same placement rule as `_compaction_section`."""
    if not covers:
        return []
    covered = sum(1 for c in covers if c)
    out = [f"## Files touched — {covered} of {_plural(len(covers), 'turn')} "
           "from a checkpoint\n"]
    rest = len(covers) - covered
    if rest:
        # "the other N" only reads right when some turn was covered; at zero
        # there is no other, and the sentence would imply one.
        which = f"the other {_plural(rest, 'turn')}" if covered else f"all {_plural(rest, 'turn')}"
        out.append(f"*Files for {which} come from the transcript scan, which sees "
                   "`Edit`/`Write`/`MultiEdit` only and carries each edit's line "
                   "range as recorded at the time.*")
    return out + [""]


def _compaction_section(events: list[_Event]) -> list[str]:
    """Compaction boundaries, computed from Claude Code's own `compactMetadata`
    — printed unconditionally rather than left to the model-written timeline,
    which could omit it entirely when real work also happened in the same
    chunk (see the drop-loop/`_failures_section` precedent this follows)."""
    compactions = [e for e in events if e.kind == "compacted"]
    if not compactions:
        return []
    out = [f"## {_plural(len(compactions), 'compaction')} in this window\n"]
    for e in compactions:
        who = f" (agent {e.agent[:8]})" if e.agent else ""
        out.append(f"- {_hhmm(e.when)}{who}  {e.text}")
    return out + [""]


def _failures_section(events: list[_Event], session: str = "") -> list[str]:
    """What went wrong, computed and quoted verbatim — never the summariser's
    job, so it doesn't ride on a model's discretion."""
    failures = [e for e in events if e.kind == "failed"]
    if not failures:
        return []
    out = [f"## What went wrong — {_plural(len(failures), 'failure')}\n"]
    for e in failures[:6]:
        cmd, _, body = e.text.partition("\n")
        who = f" (agent {e.agent[:8]})" if e.agent else ""
        head = f"- {_hhmm(e.when)}{who}"
        if session and e.line:
            loc = f"{session[:8]}/{e.agent[:8]}" if e.agent else session[:8]
            head += f"  `{loc}:{e.line}`"
        out.append(f"{head}  {_span(cmd)}" if cmd else head)
        # 600, above `_fail_excerpt`'s own 400-char bound: clipping twice cuts a
        # marked excerpt a second time and prints "[+1 chars]" at the seam.
        out.append(_quote(_clip(body, 600, _CATCHUP_HINT)) + "\n")
    if len(failures) > 6:
        out.append(f"*… and {_plural(len(failures) - 6, 'more failure')}.*\n")
    return out


# Slicing the timeline onto your turns
# ---------------------------------------------------------------------------
# Each bullet is placed under the turn it followed. The boundary is known
# before any call is made: each turn's `.when` is a `_chunk_events` boundary,
# so a chunk maps 1:1 to a turn gap and needs no placement guess afterward.

# A numbered list counts too — falling back to a flat timeline over a `1.` costs
# the whole feature to save one alternation.
_BULLET_START_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+\S")
# `- #3 #5-#7 text`: the event numbers a bullet leads with, in any of the
# forms the prompt names, up to the first character that is not one. The
# trailing class takes the separator the model punctuated with: `- #12: text`
# left the colon behind and rendered as `- : text`.
_BULLET_REFS_RE = re.compile(
    r"^(?P<marker>\s*(?:[-*+]|\d{1,3}[.)])\s+)(?P<refs>(?:#\d+(?:\s*[-–]\s*#?\d+)?[\s,;]*)+)"
    r"(?P<sep>[:.–—-]*\s*)")
_REF_RE = re.compile(r"#(\d+)(?:\s*[-–]\s*#?(\d+))?")


def _bullet_refs(bullet: str) -> tuple[str, list[int]]:
    """(the bullet with its leading event numbers removed, those numbers). A
    number is what the model copied out of the extract, so nothing here trusts
    it: the caller checks each against the chunk it was written for."""
    m = _BULLET_REFS_RE.match(bullet)
    if not m:
        return bullet, []
    refs: list[int] = []
    for a, b in _REF_RE.findall(m.group("refs")):
        lo, hi = int(a), int(b) if b else int(a)
        refs.extend(range(min(lo, hi), max(lo, hi) + 1))
    rest = bullet[m.end():]
    if not rest.strip():
        return bullet, []
    return m.group("marker") + rest, refs


def _chunk_parts(text: str, chunk: list[_Event],
                 launch: dict[str, int]) -> tuple[list[str], list[list[int]],
                                                  list[str], list[dict | None]]:
    """(bullets with their event numbers stripped, [first, last] parent row per
    bullet, the sidecar each bullet describes, where its text came from). A
    bullet's numbers resolve to the rows of the events they name, which is what
    claude-history places it at; a number outside the chunk leaves [] and the
    bullet takes the chunk's own run instead — a copied value checked against
    what it claims.

    A bullet whose numbers name one sidecar's events anchors at that sidecar's
    launch row and carries the sidecar's own file and rows as its origin. Its
    events sit in another file, so without that it takes the chunk's run and
    stacks with every other bullet of the chunk. Two or more sidecars in one
    bullet name no single file, so that bullet keeps the chunk's run."""
    bullets, spans, agents, origins = [], [], [], []
    placed = anchored = 0
    for raw in _chunk_bullets(text, strip=False):
        bullet, refs = _bullet_refs(raw)
        named = [chunk[i - 1] for i in refs if 1 <= i <= len(chunk)]
        ok = bool(refs) and len(named) == len(refs)
        rows = [e.line for e in named if not e.agent and e.line]
        side = {e.agent for e in named if e.agent}
        agent = next(iter(side)) if ok and len(side) == 1 else ""
        lines = sorted(e.line for e in named if e.agent == agent and e.line) if agent else []
        src = next((e.source for e in named if e.agent == agent and e.source), None) if agent else None
        bullets.append(bullet)
        agents.append(agent)
        origins.append({"path": str(src),
                        "lines": f"{lines[0]}..{lines[-1]}" if lines[0] != lines[-1] else str(lines[0])}
                       if lines and src else None)
        if agent and agent in launch:
            spans.append([launch[agent]])
            anchored += 1
        else:
            spans.append([min(rows), max(rows)] if ok and len(rows) == len(refs) else [])
            placed += ok and len(rows) == len(refs)
    TRACE.step("_chunk_parts", bullets=len(bullets), placed=placed, anchored=anchored,
               fallback=len(bullets) - placed - anchored, events=len(chunk))
    return bullets, spans, agents, origins


def _timeline_bullets(text: str) -> tuple[list[str], list[str]]:
    """(whatever preceded the first bullet, one entry per bullet). Continuation
    lines ride with the bullet above them, or a wrapped line gets placed by a
    clock that isn't its own."""
    preamble: list[str] = []
    bullets: list[str] = []
    for line in text.splitlines():
        if _BULLET_START_RE.match(line):
            bullets.append(line.rstrip())
        elif not line.strip():
            continue
        elif bullets:
            bullets[-1] += "\n" + line.rstrip()
        else:
            preamble.append(line.rstrip())
    return preamble, bullets


def _chunk_bullets(text: str, strip: bool = True) -> list[str]:
    """One chunk digest's bullets, or a marked line when the reply carried none.

    A reply with no bullet markers is not a summary. On a chunk holding a single
    proposal-shaped message the call sometimes answers the conversation instead
    of describing it — "I approve the sentence as written" — and printed bare
    that lands in the document as a first-person line nobody said. Dropping it
    is worse (the turn loses its only output), so it is kept and labelled, which
    is a structural test the grounding problem never had: a bullet list either
    has bullets or it does not."""
    preamble, bullets = _timeline_bullets(text)
    if bullets:
        return [_bullet_refs(b)[0] for b in bullets] if strip else bullets
    if not preamble:
        return []
    return [f"{_NO_BULLETS_PREFIX}; the call replied:* "
            f"{_clip_line(' '.join(preamble), 400)}"]


def _spine(turns: list[_Turn]) -> list[str]:
    """Your turns inside the window, listed flat — where the recap goes when the
    timeline isn't being sliced onto them."""
    out = [f"### Then you said ({_plural(len(turns), 'turn')})\n"]
    for t in turns:
        tag = "answered" if t.kind == "answered" else "said"
        out.append(f"- **{_hhmm(t.when)}** ({tag}) "
                   + _clip_line(t.text.replace("\n", " · "), 150))
    return out + [""]


def _interleaved(turns: list[_Turn], buckets: list[list[str]],
                 preamble: list[str]) -> list[str]:
    """Your turns verbatim, each followed by the bullets placed under it.
    `buckets` is one entry per turn — the chunk boundary *is* the turn boundary,
    so there is nothing to place here. A bucket may instead hold a single
    failure notice rather than real bullets; either way it prints as the
    turn's activity. Headings use the `You said`/`You answered` wording
    `_md_ansi` keys its green-for-yours colour off."""
    out: list[str] = []
    if preamble:
        out += preamble + [""]
    for i, (turn, bullets) in enumerate(zip(turns, buckets)):
        verb = "You answered" if turn.kind == "answered" else "You said"
        out.append(f"### {verb} ({_hhmm(turn.when)})\n")
        # Your own words — clipped high (2,000) since a turn can be a paste.
        out.append(_quote(_clip(turn.text, 2000, _CATCHUP_HINT)) + "\n")
        # The last turn is the window's far edge, so nothing follows it by construction.
        empty = ("*The window ends here.*" if i == len(turns) - 1
                 else "*Nothing in the timeline fell in this gap.*")
        out += bullets or [empty]
        out.append("")
    return out


def _cost_rows(events: list[_Event], boundaries: list[str],
               cached: set[int] | frozenset[int] = frozenset()):
    """`events` chunked exactly as a live run would, then sized off exactly what
    each chunk's call would send. `_dry_run_report` reads it, so the priced
    figures and the sent ones come off one split.

    Returns `(rows, by_kind)`: `rows` is one `(chunk, material_chars, called)`
    per chunk, `called` from `_chunk_activity` so "chunks called" matches what
    a real run would spend. `cached` names the chunk indices the turn store
    already holds, which a live run would not call for either. `by_kind`
    aggregates only the called chunks."""
    chunks = _chunk_events(events, boundaries, _CHUNK_MAX_CHARS)
    by_kind: dict[str, list[int]] = {}
    rows: list[tuple[list[_Event], int, bool]] = []
    for i, chunk in enumerate(chunks):
        called = _chunk_activity(chunk) and i not in cached
        chars = 0
        if called:
            for e in chunk:
                row = by_kind.setdefault(e.kind, [0, 0])
                row[0] += 1
                row[1] += len(_event_block(e))
            chars = len(_chunk_material(chunk))
        rows.append((chunk, chars, called))
    return rows, by_kind


def _dry_run_report(events: list[_Event], boundaries: list[str],
                    cmd: str = "recap",
                    cached: set[int] | frozenset[int] = frozenset(),
                    agent: str = "the assistant") -> str:
    """`--dry-run`'s answer to "what is this going to cost, and why". Prices it
    the way a live run actually spends it, via `_cost_rows`, so totals reconcile
    to what a real run would send rather than approximating it. Characters are
    the computed fact; tokens carry `~` since `_est_tokens` is chars/4."""
    rows, by_kind = _cost_rows(events, boundaries, cached)
    called = [(chunk, chars) for chunk, chars, ok in rows if ok]
    n_called = len(called)

    prompt_chars = len(_chunk_prompt(agent)) * n_called
    material_chars = sum(chars for _, chars in called)
    total_chars = prompt_chars + material_chars
    pct = lambda n: f"{round(100 * n / total_chars)}%" if total_chars else "0%"
    size = lambda n: f"{n:,} chars · {_fmt_tokens(round(n / 4))} tokens · {pct(n)}"

    out = ["## Dry run — nothing was sent\n",
           "*No model call was made. Every figure below is measured off the exact "
           f"text `{cmd}` would split into chunks and pipe to "
           f"`claude -p --model {HaikuSummariser.model}`, one call per chunk.*\n"]
    # Two reasons a chunk is not called, named apart: a quiet gap was never
    # worth a call, where a stored one was already paid for.
    quiet = sum(1 for i, (_c, _n, ok) in enumerate(rows) if not ok and i not in cached)
    why = []
    if quiet:
        why.append(f"{_plural(quiet, 'chunk')} skipped — nothing in them "
                   "but your own turns")
    if cached:
        why.append(f"{_plural(len(cached), 'chunk')} already in the turn store")
    out.append(f"{_plural(n_called, 'call')} would be made"
               + (f" ({'; '.join(why)})" if why else "") + ".\n")
    if called:
        sizes = sorted(c for _, c in called)
        mid = sizes[len(sizes) // 2]
        out.append("Chunk sizes: " + (
            f"min {size(sizes[0])}, median {size(mid)}, max {size(sizes[-1])}."
            if len(sizes) > 1 else f"{size(sizes[0])}.") + "\n")

    out.append("Where the tokens come from:")
    kind_rows = [(_KIND_LABELS.get(k, k).replace("the assistant", agent), v[1], v[0])
                 for k, v in by_kind.items()]
    kind_rows.sort(key=lambda r: -r[1])
    out.append(f"- instructions (the chunk prompt) — {size(len(_chunk_prompt(agent)))} "
               f"× {n_called} = {size(prompt_chars)}")
    for label, chars, n in kind_rows:
        out.append(f"- {label} — {_plural(n, 'event')} · {size(chars)}")
    framing = material_chars - sum(v[1] for v in by_kind.values())
    out.append(f"- extract framing (headers, newlines) — {size(max(0, framing))}")

    blocks = [(e, _event_block(e)) for chunk, _, ok in rows if ok for e in chunk]
    biggest = sorted(blocks, key=lambda p: -len(p[1]))[:3]
    if biggest and len(biggest[0][1]) > 500:
        out.append("\nBiggest single events:")
        # Two lines, not one: an edit's block leads with the timestamp and puts
        # the file path underneath it, so a head-line-only row names nothing.
        out += [f"- {_fmt_tokens(_est_tokens(b))} tokens — "
                + _clip_line(" ".join(ln.strip() for ln in b.splitlines()[:2]), 70)
                for _, b in biggest]

    # Rounded per chunk, same order `_render_window` sums in — a single total
    # rounding would drift from what `_usage_note` calls "extract estimated".
    est = n_called * _est_tokens(_chunk_prompt(agent)) + sum(round(c / 4) for _, c in called)
    floor_total = _HARNESS_FLOOR * n_called
    out.append("\nTotal:")
    out.append(f"- extract across {_plural(n_called, 'call')} — {total_chars:,} chars · "
               f"{_fmt_tokens(est)} tokens")
    # Stated separately, never summed silently — a harness measurement, not a property of this extract.
    out.append(f"- `claude -p` floor — ~{_HARNESS_FLOOR:,} tokens × {n_called} "
               f"(measured {_HARNESS_FLOOR_WHEN}, not re-measured for this run) "
               f"= {_fmt_tokens(floor_total)}")
    out.append(f"- **so roughly {_fmt_tokens(est + floor_total)} input tokens across "
               f"{_plural(n_called, 'call')}**")
    return "\n".join(out)


def _hhmm(ts: str) -> str:
    t = _parse_ts(ts)
    return t.astimezone().strftime("%H:%M") if t else "??:??"


def _hhmmss(ts: str) -> str:
    """Local clock, seconds included — the extract's per-event stamps, so a time
    the model copies out of it agrees with `_hhmm`'s local-clock headers beside
    it instead of the raw UTC the JSONL stores."""
    t = _parse_ts(ts)
    return t.astimezone().strftime("%H:%M:%S") if t else "??:??:??"


def _ago(ts: str, mtime: float) -> str:
    """Coarse age for the picker (`8s ago` … `3w ago`). `ts` is the conversation's
    own clock (`last_activity`) when it has one; empty string falls back to
    `mtime`, same fallback its caller already needs for `_pick_transcript`."""
    t = _parse_ts(ts) if ts else None
    secs = max(0.0, time.time() - (t.timestamp() if t else mtime))
    for n, unit in ((60, "s"), (60, "m"), (24, "h"), (7, "d")):
        if secs < n:
            return f"{int(secs)}{unit} ago"
        secs /= n
    return f"{int(secs)}w ago"


def _age_or_date(ts: str, mtime: float = 0.0) -> str:
    """A stamp from the last seven days as its age, anything older as its date.
    Inside a week the age answers the question the column is read for; past a
    week `_ago` counts in whole weeks and stops separating rows a date separates.
    `ts` empty falls back to `mtime`, the same fallback `_ago` and `_local_when`
    take; both empty leaves no time to render and prints the listing's dash."""
    t = _parse_ts(ts) if ts else None
    stamp = t.timestamp() if t else mtime
    if not stamp:
        return "-"
    if time.time() - stamp < 7 * 86400:
        return _ago(ts, mtime)
    return datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d")


def _local_when(ts: str, mtime: float) -> str:
    """Full local datetime for the picker's `when` column, in the same form the
    day headings use — `_ago`'s coarse age reads two same-day sessions as one.
    Same `mtime` fallback as `_ago`, for a conversation with no clock of its own."""
    t = _parse_ts(ts) if ts else None
    when = t.astimezone() if t else datetime.fromtimestamp(mtime).astimezone()
    return when.strftime("%a %d %b %Y %H:%M")


def _digest_cell(m: Meta, out: pathlib.Path = DIGEST_DIR) -> str:
    """`current`, `stale` or `-`: whether a digest file covers the session as it
    stands. Stale means the transcript holds records written after the file, so
    the digest is short by whatever followed."""
    assert m.path is not None
    f = out / f"{m.path.stem}.md"
    try:
        written = f.stat().st_mtime
    except OSError:
        return "-"
    end = _parse_ts(m.ended)
    return "stale" if end and end.timestamp() > written else "current"


def _recap_cell(m: Meta) -> str:
    """`3/12 · 2h ago`: turns the store covers over turns there are, and the age
    of the newest breakdown. `-` where nothing is stored, since `0/12` beside a
    real age would read as a recap that produced nothing."""
    if not m.recapped:
        return "-"
    return f"{m.recapped}/{m.turns} · {_ago(m.recapped_at, 0.0)}"


# The columns every session list prints, in one order: `chsum` and the typed
# picker. Duration alone doesn't say which session was real
# work, and `recap` says whether a bare `recap` has anything left to cover.
SESSION_HEADS = ("when", "dur", "prompts", "files", "agents", "notes", "digest",
                 "recap", "")


def _session_row(p: pathlib.Path, when=_local_when) -> tuple[str, ...]:
    """One list row for `p`; `when` renders the first column, `_local_when` where
    the line has room for a stamp and `_ago` where it does not (the typed picker)."""
    m = extract_meta(p)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    title = ("✎ " if m.renamed else "") + (m.title or "(untitled)")
    return (when(last_activity(p), mtime), m.duration or "-", str(m.prompts),
            str(len(m.edited)), str(m.agent_count) if m.agent_count else "-",
            f"⚑{len(m.notes)}" if m.notes else "-", _digest_cell(m),
            _recap_cell(m), title)


def _pick_transcript(live_only: bool = True) -> pathlib.Path:
    """The bare-`recap` picker — used only here; `live_transcript` itself stays
    untouched since `mark`/`name` depend on its silent newest-file fallback.
    Non-interactive (no tty on stdin or stderr) falls through to
    `live_transcript()` byte-for-byte, since blocking on `input()` would hang a script."""
    # `live_only=False` (recap) always asks: the session you're in is rarely the one you want.
    if (live_only and os.environ.get("CLAUDE_CODE_SESSION_ID", "")) or not (
        sys.stdin.isatty() and sys.stderr.isatty()
    ):
        return live_transcript()
    cands = transcripts(local=True)
    if not cands:
        return live_transcript()  # let its own "no conversation" error fire

    def recency(p: pathlib.Path) -> tuple[str, float]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (last_activity(p), mtime)

    # Ascending, so the newest sits last, beside the prompt Enter picks.
    ranked = sorted(cands, key=recency)[-8:]
    # The same columns `chsum` prints; `_ago` for `when`, since
    # this one line has no room for a stamp.
    rows = [_session_row(p, _ago) for p in ranked]
    default = len(ranked)  # newest = last row = the one Enter picks
    idx_w = len(str(default))
    widths = [max(len(r[i]) for r in (*rows, SESSION_HEADS))
              for i in range(len(SESSION_HEADS) - 1)]
    # stderr here, not stdout, so `_colour_ok` needs telling which stream to ask.
    bold, dim, reset = ("\033[1m", "\033[2m", "\033[0m") if _colour_ok(sys.stderr) else ("", "", "")
    # Clipped to the terminal, not `cmd_sessions`' 88: an unclipped title wraps
    # at column 0 and reads as a second row, and a table row is not prose.
    cols = max(40, shutil.get_terminal_size((100, 24)).columns)
    title_w = max(10, cols - (idx_w + sum(widths) + 2 * len(widths) + 4))
    cells = lambda r: "  ".join(f"{c:<{w}}" for c, w in zip(r, widths))
    print(f"  {' ' * idx_w}  {dim}{cells(SESSION_HEADS)}{reset}", file=sys.stderr)
    for i, r in enumerate(rows, start=1):
        idx = f"{bold}{i:>{idx_w}}{reset}"
        print(f"  {idx}  {dim}{cells(r)}{reset}  {_clip_line(r[-1], title_w)}", file=sys.stderr)
    # Prompt written ourselves, not passed to input(), so it can't leak to stdout
    # (stdout is the document — see the module docstring).
    print(f"which session? [{bold}{default}{reset}]: ", end="", file=sys.stderr, flush=True)
    try:
        raw = input().strip()
    except EOFError:
        raw = ""
    if not raw:
        TRACE.step("_pick_transcript", offered=len(ranked), picked="default",
                   uuid=ranked[default - 1].stem)
        return ranked[default - 1]
    if raw.isdigit() and 1 <= int(raw) <= len(ranked):
        TRACE.step("_pick_transcript", offered=len(ranked), picked=raw,
                   uuid=ranked[int(raw) - 1].stem)
        return ranked[int(raw) - 1]
    raise SystemExit(f"not a session number: {raw!r}")


_CATCHUP_HINT = "the transcript has the rest"


def _status(msg: str) -> None:
    """Transient one-line stderr narration of catch-up's phase. Tty-gated,
    never stdout — stdout is the document and must stay byte-identical."""
    if sys.stderr.isatty():
        text = f"\033[2m{msg}\033[0m" if _colour_ok(sys.stderr) else msg
        sys.stderr.write(f"\r{text}\033[K")
        sys.stderr.flush()


def _usage_note(usage: dict, seconds: float, est: int) -> str:
    """What the call was actually charged, once it's over. Beside the estimate
    that preceded it, because `_est_tokens` is chars/4 and this is the only
    thing that ever says by how much it was off."""
    got = lambda k: int(usage.get(k) or 0)
    fresh, made, read = (got("input_tokens"), got("cache_creation_input_tokens"),
                         got("cache_read_input_tokens"))
    total = fresh + made + read
    # Read and creation are not the same thing and must not be added together:
    # a cache read is billed well below base rate, writing the cache above it.
    split = [f"{read:,} cache read" if read else "",
             f"{made:,} cache write" if made else ""]
    bits = [f"{total:,} in" + (f" ({', '.join(b for b in split if b)})"
                               if any(split) else "")]
    bits += [f"{got('output_tokens'):,} out", f"{seconds:.1f}s"]
    # The extract was `est`; the rest is the harness. Naming the split is the
    # point — it is what `--dry-run` predicts, and the only check on it.
    if est and total > est:
        bits.append(f"extract estimated ~{est:,}, harness ~{total - est:,}")
    return "haiku: " + " · ".join(bits)


def _note(msg: str) -> None:
    """A line of narration that stays on screen, unlike `_status`. stderr and
    tty-only for the same reason: stdout is the document."""
    if sys.stderr.isatty():
        text = f"\033[2m{msg}\033[0m" if _colour_ok(sys.stderr) else msg
        sys.stderr.write(f"\r\033[K{text}\n")
        sys.stderr.flush()


def _clear_status() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


# Constant of the *harness*, measured not computed.
_HARNESS_FLOOR = 158
_HARNESS_FLOOR_WHEN = "2026-08-16"


def _est_tokens(text: str) -> int:
    """Chars/4. An exact count needs the model's tokeniser and `dependencies`
    stays empty, so every number derived from this prints with a `~` and the
    exact character count beside it — the chars are the fact, the tokens are the
    estimate, and prose-vs-diff mix moves the real ratio either way by a quarter."""
    return round(len(text) / 4)


def _fmt_tokens(n: int) -> str:
    return f"~{n / 1000:.1f}k" if n >= 1000 else f"~{n}"


# Chunk calls in flight at once, and the cap on any one of them. Both are read
# by the ticker to state a ceiling, so they live beside it rather than inline.
_CHUNK_WORKERS = 8
_CALL_TIMEOUT = 240


class _Counter:
    """Completed-call count, incremented from worker threads and read by the
    ticker thread. `+=` on an int is not atomic under the lock-free path, and a
    dropped increment stalls the line one short of the total for the whole run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0

    def bump(self) -> None:
        with self._lock:
            self.value += 1


class _Ticker:
    """Status line while blocking `subprocess.run` calls sit in flight. `label`
    is re-read every tick, so a caller that knows how many calls have finished
    passes a callable and the line carries a denominator instead of a clock
    alone — 2,116s of rising seconds with no total says nothing about how far in
    it is."""

    def __init__(self, label):
        self._label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        start = time.monotonic()
        while not self._stop.wait(2):
            text = self._label() if callable(self._label) else self._label
            _status(f"… {text} ({int(time.monotonic() - start)}s)")

    def __enter__(self) -> "_Ticker":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        _clear_status()


def _render_window(path: pathlib.Path, meta: Meta, anchor_line: int, anchor_ts: str,
                   prompt_text: str, until_ts: str, args, turns: list[_Turn] | None = None,
                   live: bool = True, anchor_uuid: str = "", span_note: str = "") -> int:
    """The document, for a window with a start and an optional end. One body
    for a bare `recap` and a ranged one — they differ only in how the window was
    chosen. `live` says which: bare runs to now, a chosen range to its end. An empty `until_ts` bounds the window by nothing, which is "to now"
    live and "to the end of the session" in a recap — so `live` is passed in
    rather than derived from it."""
    # A chunk phase runs for minutes, and the sections printed before it scroll
    # the ticker out of view. On a terminal the document is held and printed
    # once, after the last chunk lands; a piped run has no ticker and streams as
    # it always did. The bytes are the same either way.
    doc: list[str] = []
    hold = sys.stderr.isatty()

    def out(text: str, **kw) -> None:
        if hold:
            doc.append(text)
            return
        print(_md_ansi(text) if _colour_ok() else text, **kw)

    def flush_doc() -> None:
        _clear_status()
        for text in doc:
            print(_md_ansi(text) if _colour_ok() else text)
        doc.clear()

    # `--dry-run` makes no call, so there's nothing to slice.
    interleave = bool(turns) and not live and not getattr(args, "dry_run", False)
    # Prints before the slower event walk, so the first thing on screen is what you typed.
    _clear_status()
    header = [f"# {'Catch-up' if live else 'Recap'} — {meta.title}"]
    bits = [meta.project_name, f"your prompt at {_hhmm(anchor_ts)}"]
    if span_note:
        bits.append(span_note)
    bits.append(f"snapshot at {datetime.now().astimezone().strftime('%H:%M')} — "
                "the transcript trails the live screen" if live
                else f"window ends {_hhmm(until_ts)}" if until_ts
                else "window ends with the session")
    header.append(f"*{' · '.join(bits)}*\n")
    header.append(f"## You said ({_hhmm(anchor_ts)})\n")
    header.append(_quote(_clip(prompt_text, 500, _CATCHUP_HINT)) + "\n")
    # Moves down to the interleave instead when slicing — printing both would repeat the text.
    if turns and not interleave:
        header += _spine(turns)
    out("\n".join(header), flush=True)

    _status("reading the transcript…")
    events = _events_since(path, anchor_line, anchor_ts, until_ts)
    # Your own turns are events too, or the extract shows a direction change with
    # no cause. The anchor included: without it the first turn of a window
    # fingerprints differently from the same turn mid-window, and the store
    # misses on it every time the window moves.
    if turns is not None and not live:
        events += [_Event(t.when, "", "you", t.text) for t in turns]
        events.append(_Event(anchor_ts, "", "you", prompt_text))
        events.sort(key=lambda e: e.when)
    _status(f"{_plural(len(events), 'event')} since {_hhmm(anchor_ts)}")

    if not events:
        _clear_status()
        out("*Nothing recorded in that window.*", flush=True)
        flush_doc()
        return 0

    edited, cmds, agents_seen = [], [], []
    cmd_times: dict[str, str] = {}  # first occurrence's time — a rerun keeps its earliest stamp
    cmd_runs: dict[str, int] = {}  # how many share this bullet, stated not swallowed
    # First occurrence's row too: a deduplicated list has no event left to ask.
    where: dict[str, str] = {}
    def _loc(e: _Event) -> str:
        if not e.line or not meta.uuid:
            return ""
        who = f"{meta.uuid[:8]}/{e.agent[:8]}" if e.agent else meta.uuid[:8]
        return f"{who}:{e.line}"
    for e in events:
        if e.kind == "edit":
            # This list dedupes by path (`_dedupe` below) — strip the line
            # range `_events_since` may have folded on, or the same file
            # edited twice at different lines stops deduping at all.
            head = e.text.splitlines()[0]
            m = _EDIT_LINES_RE.match(head)
            name = m.group(1) if m else head
            edited.append(name)
            # Keyed on the form `keep()` below produces, not the raw path: the
            # list is made project-relative before these rows are looked up.
            where.setdefault(_relpath(name, meta.project), _loc(e))
        elif e.kind == "ran" and _is_notable_command(e.text):
            cmds.append(e.text)
            cmd_times.setdefault(e.text, e.when)
            where.setdefault(e.text, _loc(e))
            cmd_runs[e.text] = cmd_runs.get(e.text, 0) + 1
        if e.agent and e.agent not in agents_seen:
            agents_seen.append(e.agent)
    keep = lambda fs: _dedupe(_relpath(f, meta.project) for f in fs if _is_project_file(f))
    edited, cmds = keep(edited), _dedupe(cmds)

    span = ""
    if events and anchor_ts:
        a, b = _parse_ts(anchor_ts), _parse_ts(events[-1].when)
        if a and b:
            span = _fmt_secs(max(0, int((b - a).total_seconds())))

    since = [f"## {'Since then' if live else 'In that window'} — "
             f"{_plural(len(events), 'event')}"
             + (f" over {span}" if span else "") + "\n"]
    if edited:
        since.append("Files changed:")
        since += _located_bullets(edited, where, 12) + [""]
    if cmds:
        since.append("Commands:")
        since += _timed_bullets([(cmd_times[c], where.get(c, ""), c, cmd_runs[c])
                                 for c in cmds], 8) + [""]
    if agents_seen:
        desc = {r.id: r.description for r in meta.agents}
        since.append("Agents at work:")
        since += [f"- `{a}`" + (f"  {desc[a]}" if desc.get(a) else "") for a in agents_seen]
        since.append("")

    since += _compaction_section(events)
    since += _failures_section(events, meta.uuid)

    # Built off `turns`/`live` directly, not `interleave`, so a dry run still
    # prices the chunks a real run would make. Computed here rather than after
    # the block prints, because the checkpoint counts belong in it and a dry
    # run reaches it.
    spine = ([_Turn(anchor_line, anchor_ts, "said", prompt_text, anchor_uuid)]
             + list(turns or []) if turns and not live else [])
    boundaries = [t.when for t in spine]
    project_dir = pathlib.Path(meta.project) if meta.project else None
    covers = _turn_checkpoints(spine, until_ts, project_dir, meta.uuid)
    # Last of the computed sections: it describes the per-turn bullets in the
    # timeline below, so it sits nearest them while staying above the heading.
    since += _checkpoint_source(covers)

    said = [e for e in events if e.kind == "said"]
    if said:
        last = said[-1]
        who = f" (agent {last.agent[:8]})" if last.agent else ""
        since.append(f"## Last thing Claude said{who} ({_hhmm(last.when)})\n")
        since.append(_quote(last.text) + "\n")

    _clear_status()
    out("\n".join(since), flush=True)

    # Named wherever the output attributes work, including in the prompt the
    # summarising model is given.
    agent = sources.agent_of(meta.source)
    if getattr(args, "dry_run", False):
        _clear_status()
        cached: set[int] = set()
        if (spine and not getattr(args, "no_cache", False)
                and not getattr(args, "invalidate", False)):
            _, cached = _cached_turns(
                path.parent.name, spine,
                _chunk_events(events, boundaries, _CHUNK_MAX_CHARS), boundaries,
                _fingerprint(_chunk_prompt(agent), HaikuSummariser.model),
                HaikuSummariser.model)
        out(_dry_run_report(events, boundaries, "recap", cached, agent))
        flush_doc()
        return 0

    # One call per chunk, run independently and in parallel — no chunk's call
    # ever sees another chunk's material or output.
    chunks = _chunk_events(events, boundaries, _CHUNK_MAX_CHARS)

    def _run_chunk(chunk: list[_Event]) -> tuple[str, dict, float, str]:
        """(bullets text, usage, seconds, error) — never raises, so one chunk's
        failure can't break `executor.map`'s order for the rest."""
        try:
            if not _chunk_activity(chunk):
                return "", {}, 0.0, ""
            summariser = HaikuSummariser()
            try:
                text = summariser.digest(_chunk_prompt(agent), _chunk_material(chunk))
                return text, summariser.usage, summariser.seconds, ""
            except SummariserError as e:
                return "", summariser.usage, summariser.seconds, str(e)
        finally:
            done.bump()  # counted whichever way it ended, or the line stalls short

    # The store is read only where the document slices onto turns: a bare recap
    # names its chunks by nothing durable, and its tail gap is open by construction.
    instructions = _fingerprint(_chunk_prompt(agent), HaikuSummariser.model)
    # Two gates, not one. `--invalidate` treats what is stored for this window
    # as no longer standing: the read is skipped so every chunk is called, and
    # the write still happens so the result replaces it. `--no-cache` skips
    # both and leaves the store exactly as it found it.
    write_store = interleave and not getattr(args, "no_cache", False)
    read_store = write_store and not getattr(args, "invalidate", False)
    hits: list[list[list[str]] | None] = [None] * len(spine)
    cached: set[int] = set()
    if read_store:
        hits, cached = _cached_turns(path.parent.name, spine, chunks, boundaries,
                                     instructions, HaikuSummariser.model)

    # Which turn owns which chunk, and how many of its chunks are still to come.
    per_turn = _turn_chunks(chunks, boundaries) if spine else []
    turn_of = [0] * len(chunks)
    for j, idxs in enumerate(per_turn):
        for i in idxs:
            turn_of[i] = j
    pending = [i for i in range(len(chunks)) if i not in cached]
    # Read once, ahead of the first call rather than after the last: a turn is
    # written the moment it finishes, and its gap has to already be known closed.
    # An open gap yields "" here and the turn still stores — the closer is the
    # record of whether it had settled, not the gate on writing it.
    closers = ([] if not write_store or all(h is not None for h in hits)
               else _turn_closers(path, spine, until_ts))
    # One read of the parent for the whole run, not one per chunk: every chunk
    # of every turn anchors its agent bullets against the same launch rows.
    launch = _launch_rows(path) if closers else {}
    outstanding = [sum(1 for i in idxs if i in pending) for idxs in per_turn]
    parts_by_turn: list[list[dict]] = [[] for _ in spine]
    turn_failed = [False] * len(spine)
    stored = 0
    active = [chunks[i] for i in pending if _chunk_activity(chunks[i])]
    chunk_est = sum(_est_tokens(_chunk_prompt(agent)) + _est_tokens(_chunk_material(c))
                    for c in active)
    workers = max(1, min(_CHUNK_WORKERS, len(pending)))
    # A real ceiling, not an estimate: each call is capped at `_CALL_TIMEOUT`, so
    # the phase cannot outlast one timeout per wave of `workers`.
    ceiling = _fmt_secs(-(-len(pending) // workers) * _CALL_TIMEOUT)
    from_store = f" · {len(cached)} from the store" if cached else ""
    done = _Counter()
    # A cached chunk was charged nothing on this run, so it carries no usage and
    # no seconds — `_note` states what this run spent, not what the file cost.
    results: list[tuple[str, dict, float, str]] = [("", {}, 0.0, "")] * len(chunks)
    if pending:
        with _Ticker(lambda: f"haiku: {done.value}/{len(pending)} chunks · "
                             f"{_fmt_tokens(chunk_est)} tokens · at most {ceiling}"
                             + from_store):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                # `map` preserves submission order, so chunk order stays
                # chronological and a turn is written after every earlier one.
                for i, got in zip(pending, ex.map(_run_chunk,
                                                  [chunks[i] for i in pending])):
                    results[i] = got
                    if not closers:
                        continue
                    idx = turn_of[i]
                    text, usage, seconds, err = got
                    bullets, spans, agents, origins = (
                        _chunk_parts(text, chunks[i], launch) if text
                        else ([], [], [], []))
                    parts_by_turn[idx].append({
                        "material": _fingerprint(_chunk_material(chunks[i])),
                        "bullets": bullets, "spans": spans,
                        "agents": agents, "origins": origins,
                        "rows": _chunk_rows(chunks[i]),
                        # No events in it, so no call was made and no bullets can
                        # exist. Recorded, or the turn reads as a failed call.
                        "quiet": not _chunk_activity(chunks[i]),
                        "usage": usage or {}, "seconds": round(seconds, 3)})
                    turn_failed[idx] = turn_failed[idx] or bool(err)
                    outstanding[idx] -= 1
                    if outstanding[idx] == 0 and hits[idx] is None and _store_turn(
                            path.parent.name, spine[idx], closers[idx],
                            parts_by_turn[idx], turn_failed[idx], instructions,
                            HaikuSummariser.model, path.stem):
                        stored += 1
    if closers:
        TRACE.step("turn_store", written=stored, turns=len(spine),
                   closed=sum(1 for c in closers if c),
                   open_gaps=sum(1 for c in closers if not c),
                   dir=str(TURNS_DIR / path.parent.name))

    total_usage: dict = {}
    total_seconds = 0.0
    for _, usage, seconds, _err in results:
        total_seconds += seconds
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v

    # Every call failing degrades to verbatim sections plus a one-line notice;
    # a mix of hits and misses gives each failed gap its own notice. Bullets read
    # off disk are a timeline, so a run that made no successful call still has one.
    attempted = [results[i] for i in pending]
    all_failed = bool(attempted) and all(err for *_, err in attempted) and not cached
    unavailable = (f"*Timeline unavailable — {attempted[0][3]}.*"
                   if all_failed else "")
    errs = [err for *_, err in attempted if err]
    TRACE.step("_run_chunk", chunks=len(chunks), called=len(pending), workers=workers,
               from_store=len(cached), skipped=len(pending) - len(active),
               failed=len(errs), seconds=round(total_seconds, 1),
               est_tokens=chunk_est, first_error=_clip_line(errs[0], 90) if errs else "—")

    sliced: list[str] = []
    written = ""
    if interleave and not all_failed:
        stamps = sorted(boundaries)
        turn_bullets: list[list[str]] = [[] for _ in spine]
        turn_events: list[list[_Event]] = [[] for _ in spine]
        for i, hit in enumerate(hits):
            if hit is not None:
                turn_bullets[i] = [b for part in hit for b in part]
        for i, (chunk, (text, _, _, err)) in enumerate(zip(chunks, results)):
            # Re-derives which turn this chunk belongs to via its first event's timestamp.
            idx = max(0, bisect.bisect_right(stamps, chunk[0].when) - 1)
            turn_events[idx] += chunk
            if i in cached:
                continue
            if err:
                turn_bullets[idx].append(
                    f"*Digest unavailable for part of this gap — {err}.*")
            elif text:
                turn_bullets[idx].extend(_chunk_bullets(text))
        # Computed, not model-narrated — prepended ahead of the bullets it sits
        # beside, same reasoning as `_failures_section`/`_compaction_section`.
        # Prefers a git checkpoint diff over the transcript scan per turn,
        # wherever the PostToolUse hook covered it — see `_turn_files`.
        for idx, files in enumerate(_turn_files(turn_events, covers, project_dir,
                                                str(meta.path or ""))):
            if files:
                turn_bullets[idx] = files + turn_bullets[idx]
        sliced = _interleaved(spine, turn_bullets, [])
    elif not all_failed:
        parts: list[str] = []
        for text, _, _, err in results:
            if err:
                parts.append(f"*Digest unavailable for part of this window — {err}.*")
            elif text:
                parts.extend(_chunk_bullets(text))
        written = "\n".join(parts)

    timeline = [f"## {_MODEL_WRITTEN_HEADING}\n"]
    if sliced:
        timeline.append(
            "*Your turns below are verbatim. The bullets under each come from "
            f"a dedicated `claude -p --model {HaikuSummariser.model}` call "
            "scoped to just that turn's own events above (split into more "
            "than one call if that gap was large), run independently of every "
            "other turn's call. Where a bullet and the transcript disagree, "
            "the transcript wins.*\n")
        timeline += sliced
    else:
        timeline.append(
            "*The one non-verbatim section chsum prints: written by "
            f"`claude -p --model {HaikuSummariser.model}`, one call per chunk "
            "of the events above, each run independently from a verbatim "
            "extract of just its own chunk. Where it and the transcript "
            "disagree, the transcript wins.*\n")
        # The header left the spine out for a slice that then didn't happen —
        # every chunk failed. Your turns still belong in the document.
        if interleave and turns:
            timeline += _spine(turns)
        timeline.append(written or unavailable)
    out("\n".join(timeline).rstrip())
    flush_doc()
    # After the document, and on stderr: what the calls were actually charged,
    # summed across all of them — the one set of numbers here that is neither
    # verbatim nor estimated but measured.
    if total_usage:
        _note(_usage_note(total_usage, total_seconds, chunk_est))
    return 0


def _recap_start(path: pathlib.Path, turns: list[_Turn]) -> int | None:
    """Index of the first turn the store holds no breakdown for — where a bare
    `chsum recap` picks up. `None` when every turn is stored, which is "nothing
    new since the last one" rather than a window of zero turns.

    Existence, not the fingerprint match `_read_breakdown` demands: a stored
    file means a recap covered that turn, and editing `_CHUNK_PROMPT` should not
    silently reset every session to its beginning. The strict check still
    governs whether that turn's bullets are *reused* — this only decides where
    to start reading."""
    project_dir = path.parent.name
    # Summaries, not the file: a file holding notes alone marks nothing as
    # recapped, or a note against an unrecapped turn would move the start past it.
    stored = [bool(t.uuid) and bool((_load_doc(_turn_path(project_dir, t.uuid)) or {})
                                    .get("summaries")) for t in turns]
    first_new = next((i for i, done in enumerate(stored) if not done), None)
    TRACE.step("_recap_start", turns=len(turns), stored=sum(stored),
               start_turn=(first_new + 1) if first_new is not None else "(all stored)")
    return first_new


def cmd_recap(args) -> int:
    """A window of one conversation, verbatim, with a model-written timeline
    sliced under each of your turns.

    `--messages N [M]` names the ends as signed turns of yours, the spelling
    `digest` takes. Without them the window opens at the first turn the store
    holds no breakdown for, whichever conversation was named. `--full` is
    `--messages 1 -1`."""
    # Ahead of every other check: a registered name with no prompt behind it
    # would otherwise run the default's prompt and store the result as that
    # type's reading.
    chosen = getattr(args, "recap_type", _RECAP_TYPE_DEFAULT)
    purpose, prompt = _RECAP_TYPES[chosen]
    if prompt is None:
        written = sorted(n for n, (_, t) in _RECAP_TYPES.items() if t is not None)
        raise SystemExit(f"--type {chosen} returns {purpose}.\n"
                         f"No prompt is written for it yet — "
                         f"written: {', '.join(written)}")
    if args.messages == []:
        raise SystemExit("--messages needs one or two turn numbers "
                         "(1 your first, -1 your last)")
    # Ahead of `_target`, which picks a conversation: the listing reads the store
    # across the scope and has no conversation to pick.
    if args.list:
        if args.spec or args.file or args.last:
            raise SystemExit("--list reads the store across the project and takes "
                             "no conversation — `chsum recap <ref>` recaps one")
        return _list_annotations(
            "recap", max(40, min(shutil.get_terminal_size((100, 24)).columns, 88)),
            args.all)
    # `_pick_transcript` lists the live sessions when nothing is named; inside
    # Claude Code it returns the one you are in without asking.
    path, span = _target(args, here=_pick_transcript)
    if args.full and span:
        raise SystemExit("--full is the whole conversation and --messages names "
                         "a window: one or the other")
    if args.full:
        span = [1, -1]
    if not span:
        return _recap_since_last(args, path)
    turns = _your_turns(path)
    if not turns:
        raise SystemExit("nothing typed in that conversation — no turns to recap")
    # Positions stand in for `_span_rows`' row anchors, so one resolver orders
    # and clamps the pair for every command; `recap` then maps the two turn
    # numbers onto the `_Turn` objects it already holds.
    _, _, lo, hi = _resolve_span(list(range(len(turns))), span, len(turns))
    start, end = turns[lo - 1], turns[hi - 1]
    _status("reading the transcript…")
    meta = extract_meta(path)
    # The end turn bounds the window; the turns strictly between the two are
    # yours as well and belong in the record.
    inner = [t for t in turns if start.when < t.when <= end.when]
    # Ending on the last turn bounds the window by nothing: that turn's own work
    # is what follows it, and a bound at its timestamp would cut all of it.
    until = "" if hi == len(turns) else end.when
    TRACE.reproduce(f"chsum recap {ch_ref_for_path(path)} --messages {lo} {hi}")
    TRACE.step("cmd_recap", turns=len(turns), start_turn=lo, end_turn=hi,
               start_line=start.line, until=until or "(end of session)",
               inner=len(inner), asked=" ".join(str(v) for v in span))
    return _render_window(path, meta, start.line, start.when, start.text,
                          until, args, inner, live=False, anchor_uuid=start.uuid,
                          span_note=_turn_note(lo, hi, len(turns), span))


def _recap_since_last(args, path: pathlib.Path) -> int:
    """`recap` with no turn numbers: from the turn after the last one the store
    covers, to the end of the conversation. The window is already decided, so
    nothing is asked."""
    turns = _your_turns(path)
    if not turns:
        raise SystemExit("nothing typed in this conversation yet — no turns to recap")
    first_new = _recap_start(path, turns)
    if first_new is None:
        print(f"nothing new since the last recap ({_plural(len(turns), 'turn')} "
              f"already covered) — `chsum recap --full` for the whole session",
              file=sys.stderr)
        return 0
    start, end = turns[first_new], turns[-1]
    _status("reading the transcript…")
    meta = extract_meta(path)
    inner = [t for t in turns if start.when < t.when <= end.when]
    TRACE.reproduce(f"chsum recap {ch_ref_for_path(path)} "
                    f"--messages {first_new + 1} -1")
    TRACE.step("_recap_since_last", turns=len(turns), start_turn=first_new + 1,
               covered=first_new)
    # The last turn bounds the window by nothing: its own work is what follows it.
    return _render_window(path, meta, start.line, start.when, start.text,
                          "", args, inner, live=False, anchor_uuid=start.uuid,
                          span_note=_turn_note(first_new + 1, len(turns),
                                               len(turns), []))


def cmd_sessions(args) -> int:
    """One line per conversation, newest first. Triage: which were real work.
    Empty sessions are listed, not hidden, and so is the one running right now
    (tagged) — `--last` skips that one, since you're already in it."""
    cutoff = _parse_since(args.since) if args.since else None
    # `--all` spans every project, so five rows land you in one of them; a typed
    # `-n` overrides either figure, `0` included.
    limit = args.limit if args.limit is not None else (15 if args.all else 5)
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    # Every candidate is read rather than the newest N by mtime: mtime and the
    # last record disagree by over a minute on 289 of 367 transcripts here, and
    # by months where a session carries a default stamp, so an mtime shortlist
    # drops sessions that belong at the top. The meta cache is what makes
    # reading them all cost 0.14s.
    candidates = sorted(transcripts(local=not args.all,
                                    source=getattr(args, "source", "")))
    if cutoff is not None:
        # mtime is a cheap superset filter; a resumed old session has a recent one.
        candidates = [f for f in candidates if f.stat().st_mtime >= cutoff]
    metas = []
    for f in candidates:
        meta = extract_meta(f)
        if cutoff is not None:
            end = _parse_ts(meta.ended)
            if end and end.timestamp() < cutoff:
                continue
        metas.append(meta)
    if not metas:
        where = "any project" if args.all else "this project"
        print(f"no conversations in {where}", file=sys.stderr)
        return 1

    # Last activity, not start: a resumed session is as recent as you left it.
    metas.sort(key=lambda m: m.ended or m.started or "", reverse=True)
    shown = metas if limit <= 0 else metas[:limit]

    # The directory's own name, not `project_dir_name`'s slug: the slug joins
    # path segments on `-` and is the store's key, not a heading. The session's
    # directory rather than the run's, so the heading names the project whose
    # rows are below it.
    scope = "all projects" if args.all else _scope_label(False)
    window = f" · last {args.since}" if args.since else ""
    empty = sum(1 for m in shown if not _has_activity(m))
    # The two markdown lines go through the same gate `recap` uses;
    # the rows below are padded columns and `_dim`, not markdown.
    head = f"# Sessions — {scope}{window}\n*{_plural(len(shown), 'session')} · {empty} with no activity*\n"
    print(_md_ansi(head) if sys.stdout.isatty() and _colour_ok() else head)

    # Two lines each, as `mark --list`: a title is prose, and in a column the long
    # ones pushed every other field off the terminal.
    cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
    # By last activity: a resumed session belongs to the day you last worked
    # on it.
    by_day: dict[str, list[tuple]] = defaultdict(list)
    # The column earns its width only where the rows disagree: a corpus written
    # by one tool would spend it repeating that tool's name on every line.
    mixed = len({m.source for m in shown}) > 1
    for m in shown:
        assert m.path is not None  # every `m` here came from extract_meta, which always sets it
        # Tagged since its numbers are a snapshot — still being appended to.
        here = " · this session (in progress)" if m.path.stem == live else ""
        # Ids in full — `chsum digest <ref>/<id> --stdout` matches exactly, so a clipped one wouldn't resolve.
        delegated = [f"{r.id}  {_clip_line(r.description, cols - len(r.id) - 8)}"
                     if r.description else r.id for r in m.agents[:5]]
        if m.agent_count > len(delegated):
            # agent_count can exceed the sidecars we can name (Meta.agent_count).
            delegated.append(f"…and {m.agent_count - len(delegated)} more")
        by_day[(m.ended or m.started or "")[:10] or "undated"].append((
            ch_ref_for_path(m.path),
            m.duration or "-",
            str(m.prompts),
            str(len(m.edited)),
            str(m.agent_count) if m.agent_count else "-",
            f"⚑{len(m.notes)}" if m.notes else "-",
            _digest_cell(m, args.out),
            _recap_cell(m),
            *((sources.label_of(m.source),) if mixed else ()),
            *((m.project_name or "-",) if args.all else ()),
            # Marked, because provenance differs: one is Claude Code's reading of
            # the session, the other is yours.
            ("✎ " if m.renamed else "") + (m.title or "(untitled)") + here,
            delegated,  # past the width calculation, which stops at `heads`
        ))
    # dates fill the ref column; `--all` crosses projects, so each row names one
    heads = ("", *SESSION_HEADS[1:-1], *(("source",) if mixed else ()),
             *(("project",) if args.all else ()))
    title_i = len(heads)  # title and delegated sit past the width calculation
    # Widths across every day: columns that shift per group read as separate tables.
    widths = [max(len(r[i]) for rs in by_day.values() for r in (*rs, heads))
              for i in range(len(heads))]
    # Last of the padded columns under `--all`, and absent without it.
    proj_i = len(heads) - 1 if args.all else None
    palette = ({} if proj_i is None else _project_palette(
        r[proj_i] for rs in by_day.values() for r in rs))

    def row(r, paint: bool = False) -> str:
        cells = [f"{c:<{w}}" for c, w in zip(r, widths)]
        if paint and proj_i is not None:
            cells[proj_i] = _paint_project(cells[proj_i], r[proj_i], palette)
        return "  " + "  ".join(cells).rstrip()

    if not sys.stdout.isatty():
        # Piped or captured, one day per heading and one bullet per session, the
        # same markdown contract the listings and documents hold.
        doc = []
        for day, rows in by_day.items():
            doc.append(f"## {_pretty_day(day)}\n")
            for r in rows:
                fields = " · ".join(f"{h}: {c}" for h, c in zip(heads[1:], r[1:title_i])
                                    if h)
                doc.append(f"- `{r[0]}` — {r[title_i]}")
                doc.append(f"  - {fields}")
                for line in r[title_i + 1]:
                    doc.append(f"  - agent `{line}`")
            doc.append("")
        sys.stdout.write("\n".join(doc) + "\n")
        sys.stdout.flush()
        print("Read one: `chsum digest <ref> --stdout`   "
              "Most recent real session: `chsum digest --last --stdout`",
              file=sys.stderr)
        return 0
    gutter = 2 + widths[0] + 2  # past the ref column, where `dur` starts
    for day_no, (day, rows) in enumerate(by_day.items()):
        label = _pretty_day(day)
        if day_no:
            print(label)
        else:  # headers once, on the first date's line
            print(f"{label:<{gutter}}" + _dim(row(heads).strip()))
        for r in rows:
            print(row(r, paint=True))
            # Wrapped here, not by the terminal: a title folded at column 0 reads
            # as the next session.
            print(_dim("\n".join(textwrap.wrap(r[title_i], cols, initial_indent="    ↳ ",
                                               subsequent_indent="      "))))
            for line in r[title_i + 1]:
                print(_dim(f"      {line}"))
        print()
    sys.stdout.flush()
    print("Read one: `chsum digest <ref> --stdout`   "
          "Most recent real session: `chsum digest --last --stdout`",
          file=sys.stderr)
    return 0


def _pretty_day(day: str) -> str:
    """`2026-08-08` → `Sat 08 Aug 2026`. Left alone if it isn't a date."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
    except ValueError:
        return day


def _has_activity(m: Meta) -> bool:
    """Did the session go anywhere? One prompt answered with "what do you mean?"
    is exactly what this exists to skip. Delegated work counts."""
    return bool(m.edited) or bool(m.commands) or m.agent_count > 0 or m.prompts >= 2


def _parse_since(spec: str) -> float:
    m = re.fullmatch(r"(\d+)\s*([hdw])", spec.strip())
    if not m:
        raise SystemExit(f"--since expects forms like 7d, 24h, 2w (got {spec!r})")
    mult = {"h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return time.time() - int(m.group(1)) * mult


# ---------------------------------------------------------------------------
# Summariser seam
# ---------------------------------------------------------------------------


class SummariserError(RuntimeError):
    pass


class Summariser:
    """Where prose generation plugs in. A backend gets extracted material,
    never a raw transcript; its output prints beneath the verbatim record,
    labelled model-written."""

    def digest(self, system_prompt: str, material: str) -> str:
        raise NotImplementedError("no summariser backend configured")


# Scoped to one chunk, not a whole window: same hard rules (verbatim-only,
# outcomes as recorded, failures kept separate, no opinions), a lower bullet
# ceiling, and no "start each bullet with HH:MM" — the chunk boundary is the
# turn boundary now, known before the call is made.
def _chunk_prompt(agent: str = "Claude") -> str:
    """The chunk prompt, naming the tool whose session it describes. Claude's
    text comes back byte-identical, so the fingerprint every stored recap is
    keyed on holds and no past recap is invalidated by another format arriving.

    Attribution is what the substitution protects: the prompt tells the model
    which event kinds are the assistant's own work, and a prompt naming Claude
    over a Codex session would have the model write Claude's name against work
    Codex did."""
    return (_CHUNK_PROMPT if agent == "Claude"
            else _CHUNK_PROMPT.replace("Claude Code session", f"{agent} session")
                              .replace("Claude", agent))


_CHUNK_PROMPT = """\
Below is a verbatim extract from one slice of a Claude Code session: a
consecutive run of recorded events — assistant messages, tool calls, file
edits (with the edited text), commands and the tail of their output, subagent
activity — oldest first.

A line ending `… [+N chars not in this extract]` was shortened by the tool that
built this extract. The message itself was complete. Never describe it as cut
off, interrupted, incomplete, or unfinished — that is a fact about the extract,
not about what happened.

A `ran:` event opening with a quoted line carries the description typed
before the command ran, and the command follows on the lines beneath it. The
`output:` event after it carries what came back. Quote the description as the
stated intent and the output as the result; the description states nothing
about what the command returned.

Who did what, by event kind. `said:`, `ran:`, `edit:`, `tool:`, `output:`,
`failed:` and `spawn:` are all Claude's own work — `ran:` is a command Claude
ran through its Bash tool, however shell-like it looks. Only three kinds are
the user's: `you:` is something the user typed, `command:` is a slash command
the user typed, and `shell:` is a command the user typed at the `!` prompt,
followed by the tail of its output. An event tagged `agent <id>` is a subagent
Claude spawned.

Every event in the extract is numbered `#N`. Start each bullet with the
numbers of the events it describes — `#3`, `#3-#5`, or `#3 #7` — then a
space, then the sentence. The numbers are how each bullet is placed beside the
events it covers, so name every event the bullet draws on and no other.

Write a plain-language account of what happened in this slice, for someone
who stepped away from the screen and is coming back to it. Markdown bullet
list, oldest first.

How to write it:
- Full sentences, in ordinary words, that someone who does not know this
  codebase can follow. Not a changelog, not a list of tool calls.
- Lead with what changed for the user, then the mechanism: "the tool can now
  show what a run would cost before making the call, via a new --dry-run flag",
  not "added _dry_run_report()".
- Name the files, functions and commands inside those sentences, copied
  exactly, whenever they are the thing being described. The plain wording is
  the frame; the identifiers stay in it.
- Where the extract itself says why something was done — someone states a
  reason, a command's output prompts the next step — say so. Where it does
  not, describe only what was done.

Hard rules:
- State only what the extract shows. If it is not in the extract, it does not
  go in the account.
- Attribute by the kinds above. Claude ran the commands and made the edits:
  write "Claude ran …" or leave the subject out ("the file was rewritten").
  Never "the user ran" for a `ran:`, `edit:` or `tool:` event, and never
  "Claude" for a `you:`, `command:` or `shell:` event — the user typed those,
  and a `shell:` line is the command as typed, not a description of it.
- Report outcomes only as recorded ("pytest printed 4 passed"), never as a
  judgement ("successfully", "correctly", "works").
- No opinions, no advice, no closing summary, and no guesses about intent
  beyond what the extract states.
- Keep failed attempts, errors and corrections as their own bullets. Never
  merge a failure into the step that fixed it, and never report a check as
  passing at the time it failed. This rule outranks every instruction below
  about merging and length: if something has to give to fit the ceiling, merge
  the steps that worked and leave the failures standing.
- Otherwise merge steps that serve one change into one bullet.
- Leave out pure orientation — searching or reading files to find where
  something lives — unless what it turned up changed what was done next.
- Between 2 and 8 bullets: a slice is small, not a whole window. This is a
  ceiling, not a target: if you are at 8 and events remain, merge harder, do
  not continue past it.
- This extract is everything you get for this slice, however short — never
  ask for more of it or for the full session. If it holds only one message,
  write one bullet describing that message.
- Do not use any tools; answer from the extract alone.
Reply with only the bullet list, nothing else.
"""


# One reading of the same extract per name: `(what the reading returns, its
# prompt)`. The default carries `_CHUNK_PROMPT`, so a run with no `--type` sends
# what it sent before. A name whose prompt is None is registered and unwritten:
# the parser accepts it and `cmd_recap` stops on it, printing what the reading
# is for, so a run never reaches the model under a prompt that does not exist.
_RECAP_TYPES: dict[str, tuple[str, str | None]] = {
    "timeline": (
        "what happened across the turn, oldest first, as bullets under the "
        "turn that produced them",
        _CHUNK_PROMPT,
    ),
    "evidence": (
        "one entry per command, typed probe, change, verification or setup. A "
        "probe carries the run of output that settled it; a chain of probes "
        "converges on one found. A change carries the path it wrote, which the "
        "mask keeps where the text does not survive. A verification carries "
        "the verdict that came back. The grammar proves a write and the "
        "checkpoint's tree proves a call wrote nothing, so an entry resting on "
        "either prints as proven and the rest prints as judged; a turn whose "
        "checkpoint holds no difference carries reads only",
        None,
    ),
    "ambiguation": (
        "one entry per turn you typed: the readings its wording carries, and "
        "what the following turns settled about which one was meant",
        None,
    ),
}
_RECAP_TYPE_DEFAULT = "timeline"

class HaikuSummariser(Summariser):
    """Shells out to `claude -p --model haiku`: no SDK, no key handling — the
    user's existing Claude Code auth signs the call. Run from chsum's own state
    dir, since a print-mode run there would otherwise record a session of the
    project being summarised."""

    model = "haiku"

    def __init__(self) -> None:
        # What the call actually cost, filled in by `digest`. Empty when the
        # run reported nothing parseable — a caller checks it, never assumes it.
        self.usage: dict = {}
        self.seconds = 0.0

    def digest(self, system_prompt: str, material: str) -> str:
        """One `claude -p` call: `system_prompt` via `--system-prompt`,
        `material` alone on stdin."""
        exe = shutil.which("claude")
        if not exe:
            raise SummariserError("claude CLI not on PATH")
        cwd = CHSUM_DIR
        cwd.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                # JSON purely for accounting: it carries `usage`, tokens actually charged.
                [exe, "-p", "--model", self.model, "--output-format", "json",
                 "--system-prompt", system_prompt, "--tools", "", "--setting-sources", ""],
                input=material,
                capture_output=True, text=True, timeout=_CALL_TIMEOUT, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            TRACE.proc([exe, "-p", "--model", self.model], 124, time.monotonic() - started,
                       f"timeout, {len(material)} chars in")
            raise SummariserError("claude -p timed out after 240s") from None
        self.seconds = time.monotonic() - started
        TRACE.proc([exe, "-p", "--model", self.model, "--output-format", "json",
                    "--tools", "", "--setting-sources", ""],
                   proc.returncode, self.seconds, f"{len(material)} chars in")
        if proc.returncode != 0 or not proc.stdout.strip():
            err = (proc.stderr or proc.stdout).strip().splitlines()
            raise SummariserError(err[0][:200] if err else "claude -p printed nothing")
        # Falls back to raw stdout if the envelope is ever not what we expect:
        # the digest is the point, the accounting is not worth failing over.
        try:
            env = json.loads(proc.stdout)
            text = env["result"] if isinstance(env, dict) else ""
            if isinstance(env, dict) and isinstance(env.get("usage"), dict):
                self.usage = env["usage"]
        except (json.JSONDecodeError, KeyError, TypeError):
            text = proc.stdout
        if not isinstance(text, str) or not text.strip():
            raise SummariserError("claude -p returned no digest")
        return text.strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# Worked forms for the options whose shape a one-line help string cannot carry.
# Keyed by option string; the generic path prints the help string alone.
_OPTION_FORMS = {
    "--messages": [
        ("chsum digest <ref> --messages", "every message whole, calls between"),
        ("chsum digest <ref> --messages 3", "turn 3 alone"),
        ("chsum digest <ref> --messages 3 7", "turns 3 to 7"),
        ("chsum digest <ref> --messages -1", "your last turn"),
    ],
    "--tools": [
        ("chsum digest <ref> --tools", "every tool call, in order"),
        ("chsum digest <ref> --tools 3 7", "the calls inside turns 3 to 7"),
    ],
    "--commands": [
        ("chsum digest <ref> --commands", "every Bash invocation, in order"),
        ("chsum digest <ref> --commands -1", "the commands in your last turn"),
    ],
    "--call": [
        ("chsum digest <ref> --call 01CPKjYaSD", "that call and its whole output"),
    ],
    "--agents": [
        ("chsum digest <ref> --agents", "every subagent and its report"),
        ("chsum digest <ref>/<agent-id>", "one subagent's own digest"),
    ],
    "--stdout": [
        ("chsum digest <ref>", "writes the digest, prints its path"),
        ("chsum digest <ref> --messages", "writes <uuid>-messages.md beside it"),
        ("chsum digest --messages", "this session, written the same way"),
        ("chsum digest <ref> --messages --stdout", "prints it instead"),
        ("chsum digest <ref> --messages > out.md", "your own path, any view"),
    ],
    "--last": [
        ("chsum digest --last", "the most recent conversation that is not this one"),
        ("chsum digest --last 3", "the third most recent"),
    ],
    "--source": [
        ("chsum --source codex", "one tool's sessions"),
        ("chsum", "every tool's, with a source column where they differ"),
    ],
    "--since": [
        ("chsum --since 7d", "the last seven days"),
        ("chsum --since 2026-07-20", "since that date"),
    ],
}


def _option_help(parser, option: str, prog: str) -> str:
    """One option's own help, or "" where the parser declares no such option.
    Printed where `--help` follows an option, which asks about that option
    rather than about every option the command takes."""
    action = next((a for a in parser._actions if option in a.option_strings), None)
    if action is None:
        return ""
    spelling = ", ".join(action.option_strings)
    if action.nargs != 0 and action.metavar:
        spelling += f" {action.metavar}"
    lines = [f"{prog} {spelling}", ""]
    lines += ["  " + wl for wl in textwrap.wrap(action.help or "", 76)]
    forms = _OPTION_FORMS.get(option)
    if forms:
        lines.append("")
        width = max(len(cmd) for cmd, _ in forms)
        lines += [f"  {cmd:<{width}}   {what}" for cmd, what in forms]
    lines += ["", f"Every option: `{prog} --help`"]
    return "\n".join(lines)


def _scoped_help(raw: list[str], sub, top) -> str:
    """The help for the option `--help` was typed after, or "". A bare `--help`,
    or one following anything but an option, returns "" and the whole parser
    prints as it did.

    Three parsers are tried in the order a reader means them: the subcommand
    typed, then `sessions` which a bare `chsum` runs, then the top level."""
    where = next((i for i, tok in enumerate(raw) if tok in ("-h", "--help")), -1)
    if where < 1:
        return ""
    option = next((tok for tok in reversed(raw[:where]) if tok.startswith("-")), "")
    if not option:
        return ""
    name = next((tok for tok in raw[:where] if tok in sub.choices), "")
    tried = [(name, sub.choices[name])] if name else [
        ("sessions", sub.choices["sessions"]), ("", top)]
    for label, parser in tried:
        text = _option_help(parser, option, f"chsum {label}".rstrip())
        if text:
            return text
    return ""


def main(argv=None) -> int:
    _migrate_store()
    ap = argparse.ArgumentParser(
        prog="chsum",
        description="Work logs and reload-ready context from Claude Code conversations. "
                    "Deterministic: nothing invented. The one model-written section "
                    "(`recap`'s timeline) is labelled as such.",
    )
    ap.add_argument("--version", "-V", action="store_true",
                    help="which chsum this is, and what built it")
    ap.add_argument("--out", type=pathlib.Path, default=DIGEST_DIR,
                    help=f"digest directory (default: {DIGEST_DIR})")
    # A parent, not a top-level flag: `--out` sits on `ap` and so has to precede
    # the subcommand, and `--debug` is typed at the end of whatever you just ran.
    dbg = argparse.ArgumentParser(add_help=False)
    dbg.add_argument("--debug", action="store_true",
                     help="print what this run read, ran and resolved, for pasting "
                          "into a chsum session to reproduce from")
    # The second parent: `recap` and `digest` answer "which
    # conversation, which turns" in one spelling, so a window typed for one runs
    # on the other. `_target` reads exactly what this declares.
    win = argparse.ArgumentParser(add_help=False)
    win.add_argument("spec", nargs="*", metavar="REF|N",
                     help="ch_... ref from `chsum find`, and one or two turn "
                          "numbers (1 your first, -1 your last)")
    win.add_argument("--file", help="transcript path (derives the ref)")
    win.add_argument("--last", nargs="?", type=int, const=1, default=None, metavar="N",
                     help="the Nth most recent conversation that isn't this one "
                          "(default: 1)")
    win.add_argument("--all", action="store_true",
                     help="all projects (default: this one) — scopes --last and --list")
    # The numbers sit on the flag as well as on `spec`: argparse fills one run
    # of positionals, so `<ref> --messages -1` leaves the `-1` with nowhere to go.
    win.add_argument("--messages", nargs="*", metavar="N",
                     help="one or two turns to narrow to (1 the first, -1 the "
                          "last); on `digest`, bare is every message whole with "
                          "each call clipped to a line between them")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("sessions", parents=[dbg],
                       help="one line per conversation in this project (the default)")
    p.add_argument("-n", "--limit", type=int, default=None, metavar="N",
                   help="how many to list, 0 for all (default: 5, or 15 with --all)")
    p.add_argument("--since", default=None, help="window, e.g. 7d, 24h, 2w (default: all time)")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.add_argument("--source", default="", choices=sources.source_names(),
                   help="one tool's sessions (default: every tool's)")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser(
        "where", parents=[dbg],
        help="a locator like `01a0acf9:31` to the command that prints that row",
        description="Turns a locator chsum printed into a runnable `sed` "
                    "command. The command alone goes to stdout, so it pipes "
                    "into `sh` and substitutes into `$(…)`; what it resolved "
                    "to goes to stderr. A tool call id resolves to the row it "
                    "sits on, so a checkpoint stamp read out of `git reflog` "
                    "reaches the call that wrote the commit.")
    p.add_argument("locator",
                   help="`<session>:<line>`, `<session>:<first>-<last>`, "
                        "`<session>/<agent>:<line>`, a session on its own, or a "
                        "tool call id — which is what a checkpoint commit in "
                        "the git reflog is stamped with")
    p.add_argument("--git", action="store_true",
                   help="the checkpoint this call wrote: the files it changed "
                        "with their line ranges, and the `git diff` that shows "
                        "them. Needs per-turn checkpointing enabled for the "
                        "project (`chsum hook`)")
    p.set_defaults(func=cmd_where)

    p = sub.add_parser(
        "checkpoints", parents=[dbg],
        help="the git checkpoint chains this repo holds, and how to drop them",
        description="Each session that changed the tree chains its checkpoints "
                    "under `refs/chsum/<session>`. A chain is reachable, so git "
                    "never prunes it — retention is asked for here. Dropping a "
                    "chain leaves the transcript untouched and makes its commits "
                    "unreachable, which git reclaims on its next `gc`.")
    p.add_argument("--prune", metavar="AGE",
                   help="drop chains whose last checkpoint is older than this "
                        "(7d, 24h, 2w, or a date)")
    p.add_argument("--enable", action="store_true",
                   help="turn per-call checkpointing on for this project")
    p.add_argument("--disable", action="store_true",
                   help="turn it off; chains already recorded stay readable")
    p.add_argument("--migrate", action="store_true",
                   help="rebuild reflog-only checkpoints as chains, so they "
                        "survive a gc, the reflog expiring, and the removal of "
                        "the worktree they were written in")
    p.add_argument("--dry-run", action="store_true",
                   help="name what `--prune` or `--migrate` would do, and do "
                        "nothing")
    p.add_argument("--repo", default="", help="the repository (default: the cwd)")
    p.set_defaults(func=cmd_checkpoints)

    p = sub.add_parser("find", parents=[dbg], help="search conversations")
    p.add_argument("query", nargs="?")
    p.add_argument("--all", action="store_true", help="all workspaces (default: this one)")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--notes", "--marks", dest="notes", action="store_true",
                   help="search what you noted with `chsum note`")
    for mode in ("hybrid", "semantic", "lexical", "exact"):
        p.add_argument(f"--{mode}", dest="mode", action="store_const", const=mode)
    p.set_defaults(mode="hybrid", func=cmd_find)

    p = sub.add_parser("recap", parents=[dbg, win],
                       help="summarise this session since the last recap, or a chosen window")
    p.add_argument("--full", action="store_true",
                   help="the whole session, not just since the last recap")
    # The old spelling, and the one job it still has: the conversation you are
    # in, whatever ref or `--last` sits beside it.
    p.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true",
                   help="size breakdown of what would be sent; no model call")
    p.add_argument("--no-cache", dest="no_cache", action="store_true",
                   help=f"call for every chunk; neither read nor write {TURNS_DIR}")
    p.add_argument("--invalidate", action="store_true",
                   help="treat what's stored for this window as no longer standing: "
                        "call for every chunk and replace it")
    p.add_argument("--list", action="store_true",
                   help="this project's stored recap bullets, with the ids "
                        "`chsum note --delete` takes (--all: every project)")
    p.add_argument("--type", dest="recap_type", metavar="NAME",
                   choices=sorted(_RECAP_TYPES), default=_RECAP_TYPE_DEFAULT,
                   help="which reading runs over the extract: "
                        + "; ".join(f"{n} — {_RECAP_TYPES[n][0]}"
                                    for n in sorted(_RECAP_TYPES))
                        + f" (default: {_RECAP_TYPE_DEFAULT})")
    p.set_defaults(func=cmd_recap)

    p = sub.add_parser("digest", parents=[dbg, win],
                       help="deterministic digest of one conversation")
    p.add_argument("--stdout", action="store_true",
                   help="print instead of writing a file. Every view writes "
                        "one by default, named for the session and the view, "
                        "and prints its path")
    p.add_argument("--tools", nargs="*", metavar="N",
                   help="every tool call in order, unfiltered")
    p.add_argument("--commands", nargs="*", metavar="N",
                   help="every Bash invocation in order, unfiltered")
    p.add_argument("--writes", action="store_true",
                   help="every turn that changed the tree, with the files it "
                        "wrote and how many lines. Read from the git "
                        "checkpoints, so a write by any means counts")
    p.add_argument("--agents", nargs="*", metavar="",
                   help="every subagent and each report it sent back")
    p.add_argument("--call", metavar="ID",
                   help="one tool call whole, with its output")
    p.add_argument("--list", action="store_true",
                   help="this project's digest files, newest first (--all: every project)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser(
        "note", parents=[dbg], aliases=["annotate", "mark"],
        help="note this moment, for digests and claude-history to pick up",
        description="Files a note in chsum's store against the message it follows. "
                    "Prints nothing: nothing is recorded by the harness any more. "
                    "`annotate` and `mark` are the same command.",
    )
    p.add_argument("text", nargs="*", help="the note — quoted verbatim later")
    p.add_argument("--at", metavar="ID",
                   help="note an earlier message: a record id from --recent, or a row number")
    p.add_argument("--match", metavar="TEXT",
                   help="note the one message containing TEXT; lists candidates if "
                        "more than one matches")
    p.add_argument("--recent", nargs="?", type=int, const=20, default=None, metavar="N",
                   help="list the last N messages and tool calls (default 20), with "
                        "the record ids --at takes")
    p.add_argument("--session", action="store_true",
                   help="note the conversation itself, not the message you are at")
    p.add_argument("--list", action="store_true",
                   help="this project's notes, with the ids --delete takes "
                        "(--all: every project)")
    p.add_argument("--show", metavar="ID",
                   help="where an annotation landed: file, row, time, agent, and the "
                        "message it points at")
    p.add_argument("--context", type=int, default=3, metavar="N",
                   help="with --show: N records either side of it (default 3)")
    p.add_argument("--full", action="store_true",
                   help="with --list: whole text and whole targeted message, unclipped")
    p.add_argument("--delete", nargs="+", metavar="ID", help="remove annotations")
    p.add_argument("--all", action="store_true",
                   help="all projects (default: this one) — scopes --list, --show, --delete")
    p.add_argument("--file", help="transcript path (default: the session you're in)")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser(
        "annotations", parents=[dbg],
        help="the annotator claude-history calls: read|write|delete over JSON",
        description="One JSON object on stdin, one on stdout. `read` takes "
                    "{\"conversations\": [paths]} and serves every annotation the store "
                    "holds for them; `write` files a note; `delete` removes one by id.",
    )
    p.add_argument("op", choices=["read", "write", "delete"])
    p.set_defaults(func=cmd_annotations)

    p = sub.add_parser(
        "name", parents=[dbg], help="rename a conversation to what it actually was",
        description="Records the name in chsum's own store and appends one "
                    "`ai-title` record to the transcript, so /resume shows it too. "
                    "Nothing is invented: the title is your argv, verbatim.",
    )
    p.add_argument("words", nargs="*", metavar="[ch_REF] TITLE",
                   help="the title, quoted verbatim; lead with a ch_... ref to "
                        "rename a past conversation instead of this one")
    p.add_argument("--clear", action="store_true",
                   help="drop your name, back to Claude Code's")
    p.add_argument("--list", action="store_true",
                   help="this project's renamed conversations (--all: every project)")
    p.add_argument("--all", action="store_true",
                   help="all projects (default: this one)")
    p.add_argument("--no-resume", action="store_true",
                   help="rename in chsum only — leave the transcript, and /resume, alone")
    p.add_argument("--file", help="transcript path (default: the session you're in)")
    p.set_defaults(func=cmd_name)

    # No `parents=[dbg]`: this is a wire Claude Code calls, and `session-start`
    # writes JSON on stdout that Claude Code parses.
    p = sub.add_parser(
        "hook", help="what Claude Code runs from hooks/hooks.json, not a command you type",
        description="A wire Claude Code calls from hooks/hooks.json, not a command you "
                    "type: one JSON hook payload per event, on stdin. "
                    "`post-tool-use` writes the checkpoint commit `recap` reads back "
                    "out of the reflog, one per tool call that changed the tree, "
                    "stamped with the transcript row that caused it; "
                    "`session-start` prints the checkpoint opt-in "
                    "where this project's gate file is absent.",
    )
    p.add_argument("event", choices=["post-tool-use", "session-start"],
                   help="which hook fired")
    p.set_defaults(func=cmd_hook)

    # Bare `chsum` lists sessions: you usually want to pick one, and "most recent"
    # is often a dud. Anything naming a subcommand or asking for help is left alone.
    raw = list(argv) if argv is not None else sys.argv[1:]
    # Answered before the subcommand default is applied, or `--version` would be
    # read as an argument to `sessions`. Not an argparse `version` action: that
    # evaluates its text while the parser is built, so every other command would
    # pay `_build_line`'s two git calls to print a string it never uses.
    if any(tok in ("--version", "-V") for tok in raw):
        print(_build_line())
        return 0
    if not any(tok in sub.choices or tok in ("-h", "--help") for tok in raw):
        raw = ["sessions"] + raw
    # `--messages --help` asks what `--messages` takes. The whole option list
    # answers a question that was not put.
    scoped = _scoped_help(raw, sub, ap)
    if scoped:
        print(scoped)
        return 0
    args = ap.parse_args(raw)
    # `--file` is consumed by the live-session fallback, so record whether the
    # caller gave one before that happens: it decides print-vs-write.
    if args.cmd == "digest":
        args.file_given = bool(args.file)

    # Reset, not just enable: `main()` is callable more than once in a process
    # (tests do), and a second run must not inherit the first one's files.
    TRACE.reset(bool(getattr(args, "debug", False)), ["chsum", *raw])
    # The same run without the flag — what someone reproducing types first.
    TRACE.reproduce(shlex.join(["chsum", *(t for t in raw if t != "--debug")]))

    try:
        code = args.func(args)
    except HistoryError as e:
        print(f"error: {e}", file=sys.stderr)
        code = 2
    except BrokenPipeError:
        return 0  # the pipe is gone, so the block has nowhere to print either
    except KeyboardInterrupt:
        code = 130
    except SystemExit as e:
        # Its message is printed here rather than by the interpreter, so the
        # block lands after the error instead of above it.
        if e.code is None:
            code = 0
        elif isinstance(e.code, int):
            code = e.code
        else:
            print(e.code, file=sys.stderr)
            code = 1
        sys.stderr.flush()
    except BaseException:
        # A crash is the case a trace is most wanted in, so it prints before the
        # traceback rather than being lost to it.
        if TRACE.on:
            print(TRACE.render(1), flush=True)
        raise
    # After the command, before the block: the flush is one of the things the
    # block reports, and a crash above skips it along with the run's result.
    _meta_cache_flush()
    if TRACE.on:
        print(TRACE.render(code))
    return code


if __name__ == "__main__":
    sys.exit(main())
