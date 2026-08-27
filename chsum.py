#!/usr/bin/env python3
"""chsum — Claude Code conversations as work logs and reload-ready context.

Every line of output is copied verbatim or computed, never invented. Prose
generation lives behind the `Summariser` seam and prints beneath the verbatim
record, labelled model-written.

Commands: sessions (default), last, find, digest, context, mark, journal.
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import contextlib
import curses
import hashlib
import importlib.metadata
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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

PROJECTS_ROOT = pathlib.Path(
    os.environ.get("CLAUDE_CONFIG_DIR", pathlib.Path.home() / ".claude")
) / "projects"
# chsum's own state, in a root of its own: `~/.claude` holds what Claude Code
# wrote, and nothing here is read by Claude Code.
CHSUM_DIR = pathlib.Path.home() / ".chsum"
DIGEST_DIR = CHSUM_DIR / "digests"
TURNS_DIR = CHSUM_DIR / "turns"

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
        lines = ["--- chsum debug ---",
                 f"invocation: {shlex.join(self.argv)}",
                 f"cwd: {self._short(pathlib.Path.cwd())}",
                 f"projects: {self._short(PROJECTS_ROOT)}/  (…/ below)",
                 f"{_build_line()} · exit {exit_code}",
                 f"files ({len(rows)})"]
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


def _build_line() -> str:
    """Which chsum ran. The commit resolves because the install is editable —
    `chsum.py` in the checkout is what executes."""
    try:
        dist = importlib.metadata.version("chsum")
    except importlib.metadata.PackageNotFoundError:
        dist = ""
    here = pathlib.Path(__file__).resolve().parent
    # The checkout's own pyproject outranks the installed metadata, which an
    # editable install froze at install time: `chsum.py` here is what ran.
    # Regex rather than tomllib, which is 3.11+ and this file targets lower.
    src = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read_pyproject(here))
    version = src.group(1) if src else (dist or "unknown")
    stale = f" (installed {dist})" if dist and src and dist != src.group(1) else ""
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
    return f"chsum {version}{stale}{build} · python {py} · {sys.platform}"


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


def transcripts(local: bool = False) -> list[pathlib.Path]:
    """Addressable conversations only: two levels, no agent-* sidecars
    (mirrors claude-history's discover_agent_keys, service.rs:477-486)."""
    if not PROJECTS_ROOT.is_dir():
        return []
    out = [p for p in PROJECTS_ROOT.glob("*/*.jsonl") if not p.name.startswith("agent-")]
    if local:
        want = project_dir_name(pathlib.Path.cwd())
        out = [p for p in out if p.parent.name == want]
    return sorted(out)


# ---------------------------------------------------------------------------
# claude-history
# ---------------------------------------------------------------------------


class HistoryError(RuntimeError):
    pass


def _history(*args: str, timeout: int = 600) -> str:
    exe = shutil.which("claude-history")
    if not exe:
        TRACE.step("claude-history", found="no")
        raise HistoryError("claude-history not found on PATH")
    started = time.monotonic()
    proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    TRACE.proc(["claude-history", *args], proc.returncode, time.monotonic() - started,
               f"{len(proc.stdout)} chars out")
    if "agent-error" in proc.stdout:
        raise HistoryError(
            f"claude-history rejected {' '.join(args)}: "
            f"{proc.stdout.strip().splitlines()[0]}"
        )
    if proc.returncode != 0:
        raise HistoryError(proc.stderr.strip() or proc.stdout.strip() or "unknown failure")
    return proc.stdout


def _fields(line: str) -> dict:
    """Parse `key=value` tokens from a protocol line, ignoring any trailing `| text`."""
    head = line.split(" | ", 1)[0]
    return dict(t.split("=", 1) for t in head.split() if "=" in t)


@dataclass
class Hit:
    ref: str
    uuid: str
    title: str


def search(query: str, *, local: bool, mode: str, top: int) -> list[Hit]:
    args = ["agent", "search", query, "--top", str(top), f"--{mode}",
            "--local" if local else "--all"]
    hits = []
    for line in _history(*args).splitlines():
        if line.startswith("conversation "):
            f = _fields(line)
            hits.append(Hit(
                ref=f.get("ref", ""), uuid=f.get("uuid", ""),
                title=line.split(" | ", 1)[1].strip() if " | " in line else "(untitled)",
            ))
    return hits


@dataclass
class Message:
    n: int
    role: str
    text: str
    line: int = 0  # 1-based line of its first record in the JSONL; 0 when unknown


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


def is_real_prompt(text: str) -> bool:
    t = text.strip()
    if len(t) < 2:
        return False
    return not any(m in t for m in _NOISE_MARKERS)


def is_typed_prompt(text: str) -> bool:
    """`is_real_prompt`, minus `!` runs — something you did, not something you
    said. Mark paths stay on `is_real_prompt`, since marks are typed via `!`."""
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


# Steering turns with no standalone meaning ("yes", "ok, do that") — counted,
# not shown in the trail.
_ACK_RE = re.compile(
    r"^(y(es|ep|eah|up)?|no(pe)?|ok(ay)?|sure|thanks|ta|cool|nice|good|great|perfect|"
    r"do (it|that)|go ahead|carry on|continue|next|yes please|please do|"
    r"correct|right|exactly|agreed|fine|stop|wait|hmm+)"
    r"[\s.,!?*)]*$",
    re.IGNORECASE,
)


def is_substantive(text: str) -> bool:
    """Does this prompt say anything on its own? Filtered from the trail but
    still counted, not silently erased."""
    t = text.strip()
    return len(t) >= 12 and not _ACK_RE.match(t)


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
    marks: list[Mark] = field(default_factory=list)
    revoked: set[str] = field(default_factory=set)  # reconciled with the parent's
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
    marks: list[Mark] = field(default_factory=list)  # `chsum mark` calls, parent-only
    path: pathlib.Path | None = None

    @property
    def agent_count(self) -> int:
        """Sidecars can be missing or outnumber visible Agent calls, so take the larger."""
        return max(self.spawned, len(self.agents))

    @property
    def date(self) -> str:
        return self.started[:10]

    @property
    def project_name(self) -> str:
        return pathlib.Path(self.project).name if self.project else "?"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
# `chsum name` both records the name in chsum's own store (authoritative, since
# Claude Code's own `ai-title` would otherwise overwrite it) and appends one more
# `ai-title` record so /resume shows it too — the one exception to writing
# nothing to a transcript.

NAMES_PATH = CHSUM_DIR / "names.json"

_names_cache: tuple[float, dict[str, str]] | None = None


def _load_store() -> dict[str, dict]:
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
    return {uuid: entry["title"] for uuid, entry in _load_store().items()}


def save_name(uuid: str, title: str | None, was: str = "") -> None:
    """Write-then-rename, so two sessions renaming at once can't leave a torn
    file — the store is the only place a name survives Claude Code's next title."""
    global _names_cache
    store = dict(_load_store())
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
# Marks
# ---------------------------------------------------------------------------
# `chsum mark <reason>` writes nothing: run via `!`, Claude Code's own recording
# of the run is what plants the sentinel in the transcript, with an mN and
# ma_ anchor for free. Detection reads only captured command output, so a
# quoted mark pasted into a later session doesn't read back as a mark of it.

MARK_SENTINEL = "⚑ chsum-mark v1"
# Line-anchored: unanchored, this line's own text would match and forge a mark.
_MARK_RE = re.compile(
    r"^(?:<bash-stdout>)?⚑ chsum-mark v1(?P<fields>[^|]*)\|(?P<reason>.*)", re.MULTILINE
)
# Ends at the first closing tag, not the last — stderr's tag follows stdout's in the same record.
_MARK_TAIL_RE = re.compile(r"</bash-\w+>.*$", re.DOTALL)
_MARK_GREP = "chsum-mark"  # ASCII pre-filter: cheap, and survives any escaping


@dataclass
class Mark:
    reason: str  # verbatim argv
    rec: str = ""  # record uuid of the sentinel itself — what `--revoke` names
    at: str = ""  # record uuid of the message marked; "" means "here"
    line: int = 0  # 1-based JSONL line of the sentinel's own record
    at_line: int = 0  # ditto for the marked record, once resolved
    at_path: pathlib.Path | None = None  # file `at_line` is a line of, if not the transcript
    quote: str = ""  # first line of the marked message, verbatim
    when: str = ""
    agent: str = ""  # sidecar the mark was made in; blank for the parent's own
    # Kept apart from `agent`: a mark typed in the parent can point into a running
    # agent's sidecar, and only the target's side decides whether an mN exists.
    at_agent: str = ""  # sidecar the marked record lives in


@dataclass
class _MarkTarget:
    """Where a mark points, resolved before the sentinel prints. Stamped into the
    sentinel so reading a mark back is a lookup, not a search of every file."""
    uuid: str
    line: int = 0
    agent: str = ""


def _record_text(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _tool_result_text(rec: dict) -> str:
    """Captured output of a tool call — where a mark lands when Claude ran it,
    rather than the user typing `! chsum mark`."""
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    out = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool_result":
            continue
        body = part.get("content")
        if isinstance(body, str):
            out.append(body)
        elif isinstance(body, list):
            out += [p.get("text", "") for p in body
                    if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(out)


def scan_marks(path: pathlib.Path) -> list[Mark]:
    """Live marks in one transcript."""
    marks, revoked = _scan_marks_raw(path)
    return _apply_revocations(marks, revoked)


def _apply_revocations(marks: list[Mark], revoked: set[str]) -> list[Mark]:
    if not revoked:
        return marks
    return [m for m in marks if not any(m.rec.startswith(r) for r in revoked if r)]


def _scan_marks_raw(path: pathlib.Path) -> tuple[list[Mark], set[str]]:
    """Marks and revocations in one file, kept apart so a parent and its sidecars
    can be reconciled: either side may retract a mark the other made. Its own
    pass rather than part of `extract_meta` because line numbers are the only
    bridge back to mN, and `_records` doesn't carry them."""
    try:
        data = path.read_text(errors="replace")
    except OSError:
        return [], set()
    TRACE.file(path, "marks")
    if _MARK_GREP not in data:
        # Said explicitly: "no sentinel anywhere in this file" is the answer to
        # a mark that didn't show up, and the walk below never runs to report it.
        TRACE.step("scan_marks", file=path.name, marks=0, sentinel="absent")
        return [], set()
    marks: list[Mark] = []
    revoked: set[str] = set()
    for lineno, raw in enumerate(data.splitlines(), start=1):
        if _MARK_GREP not in raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "user":
            continue
        text = _record_text(rec) if "<bash-stdout>" in raw else ""
        for m in _MARK_RE.finditer(f"{text}\n{_tool_result_text(rec)}"):
            fields = dict(t.split("=", 1) for t in m.group("fields").split() if "=" in t)
            if fields.get("revoke"):
                # Nothing was written to take back, so the record stays and the mark drops.
                revoked.add(fields["revoke"])
                continue
            stamp = fields.get("line", "")
            marks.append(Mark(reason=_MARK_TAIL_RE.sub("", m.group("reason")).strip(),
                              rec=str(rec.get("uuid") or ""),
                              at=fields.get("at", ""),
                              line=lineno,
                              at_line=int(stamp) if stamp.isdigit() else 0,
                              at_agent=fields.get("agent", ""),
                              when=str(rec.get("timestamp") or "")))
    # A stamp is a copy, so it is checked against the uuid it claims before it is
    # used — anything that disagrees drops back to the search below.
    for mk in marks:
        if mk.at_line and not _verify_stamp(mk, path):
            TRACE.step("_verify_stamp", mark=mk.rec[:8], at=mk.at[:8], line=mk.at_line,
                       agent=mk.at_agent or "—", match="no", fallback="walk")
            mk.at_line, mk.at_agent, mk.at_path = 0, "", None
    stamped = sum(1 for m in marks if m.at_line)
    if any(m.at and not m.at_line for m in marks):
        _resolve_marked(data, marks, path)
        # A mark typed in the transcript can name a record in a sidecar: while an
        # agent is running, that is the file the conversation is landing in.
        for side, agent in mark_sources(path)[1:]:
            missing = [m for m in marks if m.at and not m.at_line]
            if not missing:
                break
            TRACE.file(side, "marks")
            _resolve_marked(side.read_text(errors="replace"), missing, side, agent)
    if any(not m.at for m in marks):
        _resolve_here(data, marks, path)
    # After resolution, not before: whether the walk placed what the stamp missed
    # is the answer to a mark that came back without a row.
    TRACE.step("scan_marks", file=path.name, marks=len(marks), revoked=len(revoked),
               stamped=stamped, placed=sum(1 for m in marks if m.at_line),
               unplaced=sum(1 for m in marks if not m.at_line))
    return marks, revoked


def _mark_file(path: pathlib.Path, agent: str) -> pathlib.Path | None:
    """The file an `agent=` stamp names. Resolved through the parent transcript,
    since a mark read out of a sidecar names its siblings by the parent's ids."""
    root = (path.parent.parent.with_suffix(".jsonl")
            if path.parent.name == "subagents" else path)
    if not agent:
        return root
    return next((s for s, a in mark_sources(root) if a == agent), None)


def _verify_stamp(mark: Mark, path: pathlib.Path) -> bool:
    """Confirm the stamped row still holds the record the mark names, and fill the
    quote from it. False sends the mark back to the search that predates stamping."""
    src = _mark_file(path, mark.at_agent)
    if not src or not src.exists() or not mark.at:
        return False
    try:
        lines = src.read_text(errors="replace").splitlines()
    except OSError:
        return False
    TRACE.file(src, "stamp", records=len(lines))
    if mark.at_line > len(lines):
        return False
    try:
        rec = json.loads(lines[mark.at_line - 1])
    except json.JSONDecodeError:
        return False
    uid = rec.get("uuid") if isinstance(rec, dict) else ""
    if not isinstance(uid, str) or not uid.startswith(mark.at):
        return False
    mark.at_path = src
    text = _record_text(rec).strip()
    mark.quote = next((ln for ln in text.splitlines() if ln.strip()), "")
    return True


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


def _resolve_here(data: str, marks: list[Mark], path: pathlib.Path | None = None) -> None:
    """Place a bare mark made before stamping existed: the last real thing said
    before its own record, which is command output and never a target itself."""
    said = _said_records(path, data)
    for mk in marks:
        if mk.at:
            continue
        if mk.when:
            prior = [s for s in said if (s[0], s[1]) < (mk.when, mk.line)]
        else:
            prior = [s for s in said if s[2] in (None, path) and s[1] < mk.line]
        if prior:
            _ts, mk.at_line, mk.at_path, agent, mk.quote, _uid = prior[-1]
            if agent:
                mk.at_agent = agent


def _resolve_marked(data: str, marks: list[Mark], src: pathlib.Path | None = None,
                    agent: str = "") -> None:
    """Fill in where each `at=` mark points, from the record it names."""
    want = {m.at for m in marks if m.at}
    for lineno, raw in enumerate(data.splitlines(), start=1):
        if not any(w in raw for w in want):
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        uid = rec.get("uuid") if isinstance(rec, dict) else ""
        if not isinstance(uid, str) or not uid:
            continue
        for mk in marks:
            if mk.at and uid.startswith(mk.at) and not mk.at_line:
                mk.at_line, mk.at_path = lineno, src
                if agent:
                    # Lines of a sidecar, which has no mN: the id stands in for the
                    # ordinal, exactly as it does for a mark an agent made itself.
                    mk.at_agent = agent
                text = _record_text(rec).strip()
                mk.quote = next((ln for ln in text.splitlines() if ln.strip()), "")


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
    run.marks, run.revoked = _scan_marks_raw(side)
    for mk in run.marks:
        mk.agent = run.id
    return run


def extract_meta(path: pathlib.Path) -> Meta:
    meta = Meta(uuid=path.stem, path=path)
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
        if rec.get("type") == "user" and _is_typed_prompt(rec, command_ids):
            meta.prompts += 1
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

    # Marks fold in like edits and commands do. Revocations pool first, so either
    # side can retract the other's.
    own_marks, revoked = _scan_marks_raw(path)
    for run in meta.agents:
        revoked |= run.revoked
    for run in meta.agents:
        run.marks = _apply_revocations(run.marks, revoked)
    all_marks = _apply_revocations(own_marks, revoked)
    all_marks += [mk for run in meta.agents for mk in run.marks]
    # By when they were made, so a delegated mark sits where it happened.
    meta.marks = sorted(all_marks, key=lambda mk: mk.when or "")
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


def _is_notable_command(cmd: str) -> bool:
    """Did this command change something, build, or test?"""
    first = cmd.split()[0] if cmd.split() else ""
    first = first.rsplit("/", 1)[-1]
    if first in ("sudo", "time", "nohup"):
        parts = cmd.split()
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
            cmd = inp["command"].strip().splitlines()[0]
            if all_cmds is not None:
                all_cmds.append(cmd)
            if _is_notable_command(cmd):
                cmds.append(cmd[:120])
    return spawned


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _yaml(v: str) -> str:
    s = str(v)
    return json.dumps(s) if (":" in s or s.startswith(("[", "{", "#", "*", "&"))) else s


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _bullets(items: list[str], limit: int) -> list[str]:
    out = [f"- `{i}`" for i in items[:limit]]
    if len(items) > limit:
        out.append(f"- …and {len(items) - limit} more")
    return out


def _timed_bullets(pairs: list[tuple[str, str, str, int]], limit: int) -> list[str]:
    """Same shape as `_bullets`, stamped, located and counted: `pairs` is (when,
    locator, text, runs), the overflow line hand-built because `_bullets` itself
    has no room for the extra columns. An empty locator prints nothing in its
    place. `runs` above 1 is stated rather than collapsed silently: a command is
    shown by its first line, so several different scripts share one bullet."""
    out = [f"- {_hhmm(when)}  " + (f"`{loc}`  " if loc else "") + f"`{text}`"
           + (f"  ×{runs}" if runs > 1 else "")
           for when, loc, text, runs in pairs[:limit]]
    if len(pairs) > limit:
        out.append(f"- …and {len(pairs) - limit} more")
    return out


def _located_bullets(items: list[str], where: dict[str, str], limit: int) -> list[str]:
    """`_bullets` with the row each item was first seen on. Deduplicated lists lose
    the event behind them, so the row is carried alongside rather than recovered."""
    out = [f"- `{i}`" + (f"  `{where[i]}`" if where.get(i) else "") for i in items[:limit]]
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


def _dim(text: str) -> str:
    """Grey for quoted transcript text, so a reason and the message it marks don't
    read as one voice."""
    if not _colour_ok():
        return text
    return f"\033[2m{text}\033[0m"


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

_MD_QUOTE_RE = re.compile(r"^> ?(.*)$")
_MD_HEADING_RE = re.compile(r"^(#{1,3}) (.*)$")
_MD_ITALIC_LINE_RE = re.compile(r"^\*(.+)\*$")
_MD_BULLET_RE = re.compile(r"^- (.*)$")
# A bullet whose entire content is one code span, optionally led by an HH:MM
# time — the `_bullets`/`_timed_bullets` shape — gets coloured whole instead of
# fighting a wrap boundary that might land inside the backticks.
_MD_BULLET_CODE_RE = re.compile(r"^(?:(\d\d:\d\d)  )?`([^`]+)`$")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_INLINE_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def _md_inline(segment: str) -> str:
    """`code`/`**bold**` styling for one already-wrapped segment. The regexes
    only see this segment, so a pair split across a wrap boundary just leaves
    its marker literal on both sides — total, never raises (see `_md_ansi`)."""
    segment = _MD_INLINE_CODE_RE.sub(lambda m: f"\033[36m{m.group(1)}\033[0m", segment)
    segment = _MD_INLINE_BOLD_RE.sub(lambda m: f"\033[1m{m.group(1)}\033[0m", segment)
    return segment


def _md_ansi(text: str) -> str:
    """Presentation-only markdown→ANSI for catch-up on a tty; the piped/captured
    document stays exact markdown. Line-based: an unmatched line passes through
    unchanged, so it can never raise on transcript text. Quote lines are matched
    first and exclusively, the same forgery guard as `_quote`. Lines are wrapped
    as plain text before colouring, since an ANSI escape would throw off
    `textwrap`'s width math."""
    cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
    out = []
    # Whose words the current blockquote is, tracked from the section heading
    # above it (quoting is identical markup either way). Green for yours, dim for Claude's.
    mine = False
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        m = _MD_QUOTE_RE.match(line)
        if m:
            # The bar rides every continuation, initial and subsequent alike —
            # a folded quote that lost it on line two would read as prose.
            colour = "\033[32m" if mine else "\033[2m"
            content = m.group(1)
            if not content.strip():
                out.append(f"{colour}│\033[0m")
                continue
            wrapped = textwrap.wrap(content, cols, initial_indent="│ ",
                                     subsequent_indent="│ ") or ["│ "]
            out.extend(f"{colour}{wl}\033[0m" for wl in wrapped)
            continue
        m = _MD_HEADING_RE.match(line)
        if m:
            rest = m.group(2)
            # A quoted heading never reaches here (`_MD_QUOTE_RE` is checked
            # first and exclusively), so this can't be forged by transcript text.
            mine = rest.startswith(("You said", "Then you said", "You answered"))
            colour = "\033[1;33m" if rest == _MODEL_WRITTEN_HEADING else "\033[1m"
            out.append(f"{colour}{rest}\033[0m")
            continue
        m = _MD_ITALIC_LINE_RE.match(line)
        if m:
            wrapped = textwrap.wrap(m.group(1), cols) or [""]
            out.extend(f"\033[2m{wl}\033[0m" for wl in wrapped)
            continue
        m = _MD_BULLET_RE.match(line)
        if m:
            content = m.group(1)
            cm = _MD_BULLET_CODE_RE.match(content)
            if cm:
                prefix = "- " + (cm.group(1) + "  " if cm.group(1) else "")
                indent = " " * len(prefix)
                wrapped = textwrap.wrap(cm.group(2), cols, initial_indent=prefix,
                                         subsequent_indent=indent) or [prefix]
                out.extend(f"{wl[:len(prefix)]}\033[36m{wl[len(prefix):]}\033[0m"
                           for wl in wrapped)
                continue
            # Any other bullet: two-space hanging indent so continuations align
            # under the text, not under the "- " marker.
            wrapped = textwrap.wrap(content, cols, initial_indent="- ",
                                     subsequent_indent="  ") or ["- "]
            out.extend(wl[:2] + _md_inline(wl[2:]) for wl in wrapped)
            continue
        wrapped = textwrap.wrap(line, cols) or [line]
        out.extend(_md_inline(wl) for wl in wrapped)
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
    if meta.marks:
        lines.append(f"marks: {len(meta.marks)}")
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
        if text:
            msgs.append(Message(n=len(msgs) + 1, role=role, text=text, line=lineno))
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

    if run.marks:
        parts.append("## Notable\n")
        parts.append("*Marked by this agent with `chsum mark`. Reasons are verbatim.*\n")
        # No ordinals: sidecar messages have none, and no anchors to cite either.
        # The agent id is redundant here — the whole digest is that agent.
        parts += _render_marks(run.marks, "", show_agent=False) + [""]

    parts.append("## Task\n")
    task = next((m.text for m in msgs if m.role == "user"), "")
    parts.append(_quote(_clip(task, 500, _SIDECAR_HINT)) + "\n" if task
                 else "*No instruction recorded.*\n")

    if run.edited:
        parts.append("## Files changed\n")
        parts += _bullets(run.edited, 20) + [""]

    if run.commands:
        parts.append("## Commands run\n")
        parts += _bullets(run.commands, 10) + [""]

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
            parts.append(f"`{_clip(cmd, 200, 'clipped')}`\n")
            parts.append(_quote(out) + "\n")

    parts.append("## Drill down\n")
    parts.append(f"Full sidecar: `{run.path}`\n")
    parts.append(f"Its rows, addressable: `chsum digest {parent_ref}/{run.id} --messages`  ·  "
                 "`--tools`  ·  `--commands`\n")
    return "\n".join(parts).rstrip() + "\n"


def render_digest(meta: Meta, ref: str, msgs: list[Message], *,
                  path: pathlib.Path | None = None,
                  prompt_clip: int = 300, max_prompts: int = 25) -> str:
    typed = [m for m in msgs if m.role == "user" and is_typed_prompt(m.text)]
    prompts = [m for m in typed if is_substantive(m.text)]
    steering = len(typed) - len(prompts)
    parts = [frontmatter(meta, ref), ""]

    parts.append(f"# {meta.title}\n")
    when = f"{meta.date} · {meta.duration}" if meta.duration else meta.date
    parts.append(f"*{when} · {meta.project_name}"
                 + (f" · `{meta.branch}`" if meta.branch else "") + "*\n")

    if meta.marks:
        # First, because someone chose these by hand mid-session — they outrank
        # anything extraction picked out.
        parts.append("## Notable\n")
        parts.append("*Marked during the session with `chsum mark`. "
                     "Reasons are verbatim.*\n")
        parts += _render_marks(meta.marks, meta.uuid) + [""]

    # The intent trail: verbatim, in order. This is the summary, uninvented.
    parts.append("## What I asked for\n")
    if not prompts:
        parts.append("*No user prompts recorded.*\n")
    else:
        shown = prompts[:max_prompts]
        for m in shown:
            # The row it sits on, not an ordinal: a reader opens this with `sed`.
            parts.append(f"**`{meta.uuid[:8]}:{m.line}`**\n" if m.line
                         else f"**message {m.n}**\n")
            parts.append(_quote(_clip(m.text, prompt_clip)) + "\n")
        trailer = []
        if len(prompts) > len(shown):
            trailer.append(f"{len(prompts) - len(shown)} more prompts")
        if steering:
            trailer.append(f"{steering} short steering replies not shown "
                           f"(“yes”, “ok, do that”)")
        if trailer:
            parts.append(f"*…and {', '.join(trailer)}.*\n")

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
        parts.append(f"One agent's own digest: `chsum context {ref}/<id>`\n")

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
                     else "Last thing Claude said")
            where = f"`{meta.uuid[:8]}:{m.line}`" if m.line else f"message {m.n}"
            parts.append(f"**{label}** ({where})\n")
            parts.append(_quote(_clip(m.text, 600)) + "\n")
    else:
        parts.append("*Nothing recorded.*\n")

    parts += _drill_block(ref, path)
    return "\n".join(parts).rstrip() + "\n"


def _drill_block(ref: str, path: pathlib.Path | None) -> list[str]:
    """Where the conversation is, and how to open a row of it. Every locator above
    expands here, and `sed` resolves them — nothing in this document requires a
    second tool to be installed before it can be read."""
    if not path:
        return []
    out = ["## Drill down\n", f"Transcript: `{path}`"]
    sides = subagent_transcripts(path)
    out += [f"- subagent `{sc.stem.removeprefix('agent-')}` — `{sc}`" for sc in sides]
    out += ["",
            f"One row: `sed -n '<line>p' {path} | jq`",
            f"Every message: `chsum digest {ref} --messages`  ·  "
            f"every tool call: `--tools`  ·  every command: `--commands`\n"]
    return out


def _render_marks(marks: list[Mark], session: str,
                  show_agent: bool = True) -> list[str]:
    """A mark names the row it points at. The mark already carries that row and the
    file it is in, so nothing is looked up and nothing needs claude-history to
    resolve what comes out."""
    out = []
    for mk in marks:
        where = []
        # The target's file decides: a target's line is a line of the target's file.
        agent = mk.at_agent or mk.agent
        target = mk.at_line or mk.line
        if target and session:
            who = f"{session[:8]}/{agent[:8]}" if agent else session[:8]
            where.append(f"`{who}:{target}`")
        elif agent and show_agent:
            where.append(f"agent `{agent}`")
        head = f"- **{_clip_line(mk.reason, 300)}**"
        if where:
            head += f"  ({' · '.join(where)})"
        elif mk.at:
            head += f"  (record `{mk.at[:8]}`)"
        elif mk.rec:
            head += f"  (mark `{mk.rec[:8]}`)"
        out.append(head)
        if mk.quote:
            out.append(f"  > {_clip_line(mk.quote, 160)}")
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
        # A derived value is checked against what it claims, the same rule as
        # `_verify_stamp`: the ref has to resolve back to the file it came from.
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


def _find_marks(args) -> int:
    """Marks matching a query. Pure JSONL and plain substring matching — there are
    few marks and they're short, so an index would buy nothing and cost exactness."""
    q = (args.query or "").lower()
    rows = []
    for p in transcripts(local=not args.all):
        meta = extract_meta(p)
        for mk in meta.marks:
            if q and q not in mk.reason.lower():
                continue
            rows.append((meta, mk))
    if not rows:
        print("no marks" + (f" matching {args.query!r}" if q else ""), file=sys.stderr)
        return 1
    rows.sort(key=lambda r: r[1].when or r[0].ended or "", reverse=True)
    for meta, mk in rows[:args.top]:
        print(f"{ch_ref_for_path(meta.path)}  {(mk.when or meta.ended or '')[:10]}  "
              f"⚑ {mk.reason}")
    print(f"\nRead one: `chsum context <ref>`", file=sys.stderr)
    return 0


def cmd_find(args) -> int:
    if args.marks:
        return _find_marks(args)
    if not args.query:
        raise SystemExit("find: need a query (or --marks to list marks)")
    if args.mode in ("hybrid", "semantic"):
        # Embedding search is tens of seconds warm, and several minutes the very
        # first time while the index builds. Say so rather than looking hung.
        print(f"searching ({args.mode}; --lexical is much faster for exact terms)…",
              file=sys.stderr)
    hits = search(args.query, local=not args.all, mode=args.mode, top=args.top)
    if not hits:
        print("no matches", file=sys.stderr)
        return 1
    for h in hits:
        path = _path_for_uuid(h.uuid)
        meta = extract_meta(path) if path else Meta()
        print(f"{h.ref}  {meta.date or '??????????'}  "
              f"{meta.project_name[:22]:<22}  {h.title}")
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


# What each flag selects. A Bash call is its own kind rather than a filter applied
# over `tool`, so both flags read the same rows without a second test per row.
_ROW_KINDS = {
    "messages": ("message",),
    "tools": ("tool", "command"),
    "commands": ("command",),
}


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
    for src, agent in mark_sources(path):
        if only_agent and agent != only_agent:
            continue
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
        for lineno, rec in parsed:
            role = rec.get("type")
            if role not in ("user", "assistant"):
                continue
            ts = str(rec.get("timestamp") or "")
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
            if text:
                out.append(_Row(ts, agent, "message", "", text, role, lineno, src))
    # A parent's line numbers and a sidecar's don't order against each other;
    # only a clock does. Same reason `mark_sources`' readers sort by timestamp.
    out.sort(key=lambda r: r.when)
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


def _sources_block(rows: list[_Row], session: str) -> list[str]:
    """Full path per source, a `sed` line, and the loop that reads a stretch. The
    locator on a row is short enough to read across hundreds of rows; the path it
    expands to has to be stated somewhere, or the row reaches nothing on its own."""
    if not rows:
        return []
    seen: dict[str, pathlib.Path | None] = {}
    for r in rows:
        seen.setdefault(_locator(r, session).rsplit(":", 1)[0], r.source)
    first = rows[0]
    some = " ".join(str(r.line) for r in rows[:3])
    return (["## Sources\n"]
            + [f"- `{k}` — `{p}`" for k, p in seen.items()]
            + [f"\nOne record: `sed -n '{first.line}p' {first.source} | jq`",
               "A stretch: `for l in " + some + "; do sed -n \"${l}p\" "
               f"{first.source} | jq -r '.message.content'; done`\n"])


def find_row(path: pathlib.Path, spec: str) -> tuple[_Row, str]:
    """The row an id prefix names, with the captured output where it has one. An
    ambiguity lists candidates rather than picking, same rule as `_find_mark`: the
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
    TRACE.step("find_row", tool_id=row.tool_id, agent=row.agent or "(parent)",
               output_chars=len(out))
    return row, out


def render_rows(meta: Meta, ref: str, rows: list[_Row], what: str) -> str:
    """One line per row. A call's first line, not a flattened clip: a heredoc
    squashed onto one line is 120 chars of its own source, where `python3 - <<'PY'`
    identifies it at a glance. The whole text sits one `sed` away, which is what
    the locator on each row is for."""
    _, agent_id = _split_agent_ref(ref)
    # The scope is stated, not inferred from whether the locators carry an agent
    # part: a session whose agents ran nothing renders identically to one scoped away.
    scope = (f"Every {what[:-1]} row in subagent `{agent_id}`."
             if agent_id else f"Every {what[:-1]} row in this conversation, its own and its agents'.")
    out = [f"# {what.capitalize()} — {_plural(len(rows), 'row')}, in order\n",
           f"*{meta.title}*\n",
           f"*{scope}",
           "Nothing is filtered, deduplicated or clipped away here — the digest's",
           "own sections do all three; this section does none.*\n"]
    if not rows:
        out.append(f"*No {what} recorded.*\n")
        return "\n".join(out)
    out += _sources_block(rows, meta.uuid)
    day = ""
    for r in rows:
        if r.when[:10] != day:
            day = r.when[:10]
            out.append(f"\n## {day}\n")
        ident = f"`{_short_id(r.tool_id)}`  " if r.tool_id else ""
        first = next(iter(r.text.splitlines()), "")
        out.append(f"- {ident}`{_locator(r, meta.uuid)}`  {_hhmmss(r.when)}  "
                   f"{r.label}  `{_clip_line(first, 120)}`")
    if any(r.tool_id for r in rows):
        out.append(f"\nOne call whole, with its output: `chsum digest {ref} --call <id>`\n")
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


def _resolve_or_live(args) -> str:
    """The ref a digest is about. Bare means the session you are in, `--last`
    the most recent one that isn't — the same rule `chsum last` used before it
    became this flag."""
    if not args.ref and not args.file:
        path = (latest_transcript(local=not args.all, nth=args.nth)
                if args.last else live_transcript())
        args.file = str(path)
    return resolve_ref(args)


def cmd_digest(args) -> int:
    ref = _resolve_or_live(args)
    view = next((k for k in _ROW_KINDS if getattr(args, k, False)), "")
    if args.call or view:
        # Lookups reached from a hint in the digest, not artifacts to keep, so
        # they print where the digest itself writes a file.
        parent_ref, agent_id = _split_agent_ref(ref)
        path = _parent_path(parent_ref)
        if args.call:
            row, out = find_row(path, args.call)
            sys.stdout.write(render_row_detail(row, out, ref))
            return 0
        sys.stdout.write(render_rows(extract_meta(path), ref,
                                     collect_rows(path, _ROW_KINDS[view], agent_id), view))
        return 0
    meta, md = _digest_for(ref)
    # A no-argument command that silently writes a file is a surprise: the bare
    # form prints, and naming a session is what asks for one on disk.
    if args.stdout or not (args.ref or args.file_given or args.last):
        sys.stdout.write(md)
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    _, agent_id = _split_agent_ref(ref)
    dest = args.out / (f"{meta.uuid}-agent-{agent_id}.md" if agent_id else f"{meta.uuid}.md")
    dest.write_text(md)
    print(f"wrote {dest}")
    return 0


def cmd_context(args) -> int:
    """Reload artifact. Same content as the digest, with a provenance header so a
    future reader knows exactly how much to trust it (answer: it's verbatim)."""
    ref = _resolve_or_live(args)
    meta, md = _digest_for(ref)
    parent_ref, agent_id = _split_agent_ref(ref)
    print("<!-- Extracted verbatim from the transcript by chsum. No model wrote this;")
    print("     nothing here is paraphrased. Quotes may be clipped — full text is in")
    if agent_id:
        print("     the sidecar named under Drill down. -->")
    else:
        print(f"     the transcript, at the rows named: sed -n '<line>p' <file> | jq -->")
    print()
    sys.stdout.write(md)
    return 0


def live_transcript() -> pathlib.Path:
    """The conversation running right now — the opposite of `latest_transcript`,
    which skips it. Falls back to the newest file in this project so `chsum mark`
    still resolves something when the env var is missing."""
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if live:
        path = PROJECTS_ROOT / project_dir_name(pathlib.Path.cwd()) / f"{live}.jsonl"
        if path.exists():
            TRACE.step("live_transcript", via="CLAUDE_CODE_SESSION_ID", uuid=live)
            return path
    cands = sorted(transcripts(local=True), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        raise SystemExit("no conversation found for this project")
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


_MARK_INPUT_RE = re.compile(r"\s*<bash-input>\s*chsum\s+mark\b")
_BASH_OUT_RE = re.compile(r"\s*<bash-(?:stdout|stderr)>")


class _Machinery:
    """`chsum mark`'s own footprint, tracked in file order — excluded from
    matching (a tool_use record is written before the command runs, so without
    this every `--match` finds itself). Invocations only, not talk about them,
    or a conversation about this feature becomes unmarkable. One instance per
    file: adjacency is a fact about the file, and timestamp sort interleaves sidecars."""

    def __init__(self) -> None:
        self.after_mark = False

    def sees(self, text: str, kind: str = "text") -> bool:
        # Tool calls hang off a record rather than being one, so they carry no
        # state: nothing is written between a tool_use and the record after it.
        if kind == "tool":
            return bool(re.search(r"\bchsum\s+mark\b", text))
        if _BASH_OUT_RE.match(text):
            was, self.after_mark = self.after_mark, False
            return was or MARK_SENTINEL in text
        self.after_mark = bool(_MARK_INPUT_RE.match(text))
        return self.after_mark or MARK_SENTINEL in text


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


def _find_mark(marks: list[Mark], spec: str) -> list[Mark]:
    """Every mark on the record an id prefix names. One record can carry several —
    a single command printing several sentinels — and the id names the record, so
    they come back together rather than as an ambiguity nothing can narrow.
    Shared by `--revoke` and `--show`, so a prefix that resolves for one resolves
    for the other."""
    hits = [mk for mk in marks if mk.rec.startswith(spec)]
    if not hits:
        raise SystemExit(f"no live mark {spec!r} — `chsum mark --list` shows them")
    if len({mk.rec for mk in hits}) > 1:
        raise SystemExit(f"{spec!r} matches {len(hits)} marks — use more characters")
    return hits


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


def _show_mark(path: pathlib.Path, marks: list[Mark], context: int, cols: int) -> int:
    """Where the marks on one record landed, and what surrounds them. The header
    carries the whole answer to "where is this" — file, row, time, record id,
    agent — so nothing downstream has to search the transcript again to place it.
    Several reasons print above one location: they share the record they name."""
    mk = next((m for m in marks if m.at_line), marks[0])
    src = mk.at_path or _mark_file(path, mk.at_agent) or path
    where = [f"{src}:{mk.at_line}" if mk.at_line else str(src)]
    if mk.when:
        where.append(_hhmmss(mk.when))
    if mk.at and mk.at_line:
        where.append(f"record {mk.at[:8]}")
    agent = mk.at_agent or mk.agent
    if agent:
        where.append(f"agent {agent}")
    if mk.at_line:
        where.append(f"line {mk.at_line}")
    for one in marks:
        print(f"{one.rec[:8]}  {one.reason}")
    print(_dim("  " + "  ·  ".join(where)))
    if not mk.at_line:
        # Naming the record it points at, not just "nothing": a mark quoted out of
        # another conversation names a record this file has never held.
        print(_dim(f"\n  record {mk.at[:8]} is not in this file" if mk.at
                   else "\n  (nothing before it)"))
        return 0
    rows = _context_rows(src, mk.at_line, max(0, context))
    for lineno, rec in rows or []:
        print()
        who = f"agent {agent[:8]}" if agent else (
            "you" if rec.get("type") == "user" else "claude")
        # Local clock, matching the header's own stamp — two clocks in one view
        # read as two different moments.
        head = f"{'▸' if lineno == mk.at_line else ' '} {lineno:>6}  " \
               f"{_hhmmss(str(rec.get('timestamp') or ''))}  {who}"
        print(head if lineno == mk.at_line else _dim(head))
        body = _record_text(rec).strip()
        if lineno != mk.at_line:
            # The marked record prints whole; its neighbours are orientation, and
            # the row number above each one says where to read the rest.
            body = _clip(body, 400, f"line {lineno} of {src.name}")
        for para in body.splitlines() + [f"⚙ {t}" for t in _tool_lines(rec)]:
            if not para.strip():
                print()
                continue
            text = "\n".join(textwrap.wrap(para, cols, initial_indent="    ",
                                           subsequent_indent="    "))
            print(text if lineno == mk.at_line else _dim(text))
    if not rows:
        print(_dim(f"\n  row {mk.at_line} is not in {src.name}"))
    return 0


def cmd_mark(args) -> int:
    """Mark a moment as notable. Writes nothing: printing the sentinel is the
    whole mechanism — see the Marks section above for why."""
    path = pathlib.Path(args.file).expanduser().resolve() if args.file else live_transcript()
    if not path.exists():
        raise SystemExit(f"no such transcript: {path}")

    # Named on every path that quotes the transcript: a result that disagrees with
    # another invocation is unreadable without knowing which file each one searched.
    cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))

    if args.show:
        marks = extract_meta(path).marks
        if not marks:
            print(f"nothing marked in {path}", file=sys.stderr)
            return 1
        return _show_mark(path, _find_mark(marks, args.show), args.context, cols)

    if args.list:
        # The session's marks (agents' folded in too), same pooled definition extract_meta uses.
        marks = extract_meta(path).marks
        if not marks:
            print(f"nothing marked in {path}", file=sys.stderr)
            return 1
        if args.full:
            # The stored quote is only the marked message's first line; the rest comes back off disk.
            # Keyed by file: a mark can point into a sidecar, whose line numbers mean nothing here.
            lines_of: dict[pathlib.Path, list[str]] = {}
            for i, mk in enumerate(marks):
                if i:
                    print()
                print(f"{mk.rec[:8]}  {mk.reason}")
                src = mk.at_path or path
                if src not in lines_of:
                    lines_of[src] = src.read_text(errors="replace").splitlines()
                    TRACE.file(src, "list-full", records=len(lines_of[src]))
                body = _text_at_line(lines_of[src], mk.at_line) or mk.quote
                first = True  # the ↳ opens the message, it doesn't bullet its paragraphs
                for para in (body or "(nothing before it)").splitlines():
                    if not para.strip():
                        print()
                        continue
                    print(_dim("\n".join(textwrap.wrap(
                        para, cols, initial_indent="  ↳ " if first else "    ",
                        subsequent_indent="    "))))
                    first = False
            sys.stdout.flush()
            print(f"\n{_plural(len(marks), 'mark')} in {path}", file=sys.stderr)
            return 0
        # Two lines each: a reason and the message it marks are both prose.
        rows = [(_clip_line(mk.reason, 60),
                 f"{mk.rec[:8]}  agent {mk.agent[:8]}" if mk.agent else mk.rec[:8],
                 _clip_line(mk.quote, 96) or "(nothing before it)") for mk in marks]
        width = max(len(r[0]) for r in rows)
        # Wrapped here rather than left to the terminal: a quote that folds at
        # column 0 reads as a new mark.
        for i, (reason, rec, quote) in enumerate(rows):
            if i:
                print()
            print(f"{reason:<{width}}  {rec}")
            print(_dim("\n".join(textwrap.wrap(quote, cols, initial_indent="  ↳ ",
                                               subsequent_indent="    "))))
        # Flushed first, or the hint jumps the list: stdout is block-buffered when
        # piped, stderr never is.
        sys.stdout.flush()
        print(f"\n{path}\nShow one: `chsum mark --show <id>`  ·  "
              "drop one: `chsum mark --revoke <id>`", file=sys.stderr)
        return 0

    if args.revoke:
        # Agents' marks included: either side can retract the other's.
        marks = extract_meta(path).marks
        out = []
        for spec in args.revoke:
            # One sentinel per record: `_apply_revocations` drops by record id, so
            # naming it once retracts every mark that record carries.
            hits = _find_mark(marks, spec)
            out.append(f"{MARK_SENTINEL} revoke={hits[0].rec} | dropped: "
                       f"{_clip_line(' / '.join(mk.reason for mk in hits), 120)}")
        print("\n".join(out))
        if not os.environ.get("CLAUDECODE"):
            print("warning: not running inside Claude Code, so nothing recorded this.",
                  file=sys.stderr)
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
              'Mark one: `chsum mark --at <id> "<reason>"`', file=sys.stderr)
        return 0

    reason = " ".join(args.reason).strip()
    if not reason:
        # Named separately from the bare case: `--match "<phrase>"` with no reason
        # reads as complete, and the generic message sent one reader off diagnosing
        # the match instead of the missing argument.
        if args.match or args.at:
            flag = "--match" if args.match else "--at"
            raise SystemExit(
                f'{flag} says which message to mark, not why — add a reason:\n'
                f'  chsum mark {flag} "{(args.match or args.at)}" "why this matters"')
        raise SystemExit('nothing to mark — try: chsum mark "why this matters"')
    # One line, because the sentinel is parsed back out of a single record.
    reason = " ".join(reason.split())
    if args.at and args.match:
        raise SystemExit("--at and --match name the same thing two ways; use one")
    target = (_mark_target(path, args.at) if args.at
              else _match_target(path, args.match) if args.match
              else _here_target(path))
    # Row and file are stamped beside the uuid so reading the mark back is a lookup;
    # both values are digits or hex, which the space-separated field grammar requires.
    fields = ""
    if target:
        fields = f" at={target.uuid}"
        if target.line:
            fields += f" line={target.line}"
        if target.agent:
            fields += f" agent={target.agent}"

    print(f"{MARK_SENTINEL}{fields} | {reason}")
    if not os.environ.get("CLAUDECODE"):
        print("warning: not running inside Claude Code, so nothing recorded this. "
              "Run it as `! chsum mark …` in a session.", file=sys.stderr)
    return 0


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
        names = load_names()
        if not names:
            print("nothing renamed yet", file=sys.stderr)
            return 1
        for uuid, title in names.items():
            path = _path_for_uuid(uuid)
            print(f"{ch_ref_for_path(path) if path else uuid}  {title}")
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
        was = _load_store().get(path.stem, {}).get("was", "")
        save_name(path.stem, None)
        # Put Claude Code's title back the same way — appended, never deleted — or
        # /resume keeps showing the name chsum just forgot.
        if was and not args.no_resume:
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
    data carries on past it — unanchored, it forges the same way `scan_marks` guards against."""
    if _DECLINED_RE.match(body.strip()):
        return False
    if is_error:
        return True
    tail = body[-500:]
    return any(r.search(tail) for r in _FAIL_RES)


def _typed_text(rec: dict) -> str:
    """Only the parts you typed. The harness rides `<system-reminder>` blocks in
    the same record as a prompt, and `_record_text` would quote them with it."""
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
        # First line, not a flattened clip: a heredoc squashed onto one line is
        # 200 chars of its own source, where `python3 - <<'PY'` identifies it.
        first = next(iter(inp["command"].strip().splitlines()), "")
        return "ran", _clip_line(first, 200)
    if name in _FILE_TOOLS and isinstance(inp.get("file_path"), str):
        # Edited text rides along verbatim — what the summariser reads function
        # names out of, instead of chsum parsing code. `content` (a Write) is
        # capped lower since it's a whole file, not a change.
        bits = [inp["file_path"]]
        for key, label, lim in (("old_string", "was", 1500), ("new_string", "now", 4000),
                                ("content", "now", 2000)):
            if isinstance(inp.get(key), str) and inp[key].strip():
                bits.append(f"--- {label} ---\n{_clip(inp[key], lim, 'clipped')}")
        return "edit", "\n".join(bits)
    if name in _AGENT_TOOLS:
        desc, prompt = str(inp.get("description") or ""), str(inp.get("prompt") or "")
        return "spawn", _clip_line(f"{desc}: {prompt}" if desc else prompt, 400)
    if name == "Skill" and isinstance(inp.get("skill"), str):
        # The loaded body is dropped as a turn, so this call is the only record
        # of which skill ran and what it was asked — `skill`/`args` are its keys,
        # neither of which the generic detail scan below covers.
        args = str(inp.get("args") or "").strip()
        return "tool", _clip_line(f"Skill: {inp['skill']}"
                                  + (f" — {args}" if args else ""), 400)
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
                cmd = _COMMAND_NAME_RE.search(_record_text(rec))
                if cmd:
                    cargs = _COMMAND_ARGS_RE.search(_record_text(rec))
                    detail = cargs.group(1).strip() if cargs else ""
                    events.append(_Event(ts, agent, "command",
                                         _clip_line(cmd.group(1)
                                                    + (f" {detail}" if detail else ""), 200),
                                         lineno, src))
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


def _turn_activity(path: pathlib.Path, turns: list[_Turn]) -> list[str]:
    """What happened after each of your turns, one summary per turn. One pass,
    events bucketed to the turn they followed — a per-turn walk would re-read
    the file once per turn. Counts only: the picker is a place to choose from, not to read."""
    if not turns:
        return []
    events = _events_since(path, 0, "")
    stamps = [t.when for t in turns]
    tally: list[Counter] = [Counter() for _ in turns]
    agents: list[set] = [set() for _ in turns]
    for e in events:
        # The turn this event followed: rightmost turn at or before it.
        i = bisect.bisect_right(stamps, e.when) - 1
        if i < 0:
            continue
        tally[i][e.kind] += 1
        if e.agent:
            agents[i].add(e.agent)
    out = []
    for counts, seen in zip(tally, agents):
        bits = []
        for kind, word in (("edit", "edit"), ("ran", "cmd"), ("tool", "tool"),
                           ("command", "slash cmd"), ("failed", "failure")):
            if counts[kind]:
                bits.append(_plural(counts[kind], word))
        if seen:
            bits.append(_plural(len(seen), "agent"))
        out.append(" · ".join(bits))
    return out


# Sized to keep a chunk call cheap against the harness floor without
# fragmenting into so many calls that their fixed overhead dominates.
_CHUNK_MAX_CHARS = 8_000


def _chunk_material(events: list[_Event]) -> str:
    """What one chunk call sees: only its own events, oldest first, nothing
    else — no other chunk's material or output, and no prompt text repeated
    across every chunk. Isolation by construction: a thread handed this string
    has no way to see what any other chunk produced."""
    return "\n".join(_event_block(e) for e in events)


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


def _turn_closers(path: pathlib.Path, spine: list[_Turn], until_ts: str) -> list[str]:
    """Per turn, the uuid of the last assistant record in its gap once that gap
    is closed, else "". A gap still open takes more records after the ones a
    breakdown was built from, so nothing derived from it is storable.

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
    out = [uid if bounded(i) or last_reason[i] in _CLOSING_REASONS else ""
           for i, uid in enumerate(last_uuid)]
    # The final gap of the session being appended to right now stays open
    # whatever it closed on: the next record lands in it.
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if out and not until_ts and live and path.stem == live:
        out[-1] = ""
    TRACE.step("_turn_closers", turns=len(spine), closed=sum(1 for u in out if u),
               open=sum(1 for u in out if not u),
               by_reason=sum(1 for r in last_reason if r in _CLOSING_REASONS),
               until=until_ts or "(end of session)")
    return out


def _read_breakdown(project_dir: str, turn: _Turn, instructions: str, model: str,
                    materials: list[str]) -> list[list[str]] | None:
    """This turn's stored bullets, one list per chunk, or None. A hit needs the
    entry to name the same instructions and model, to hold one part per chunk,
    and every part to name the material of the chunk it is read for — the store
    is checked against what it claims, the same rule as `_verify_stamp`."""
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
                     instructions: str, model: str, parts: list[dict]) -> None:
    """One turn's breakdown onto disk. An entry written under different
    instructions stays beside the new one rather than being dropped: the text a
    past recap printed remains findable after `_CHUNK_PROMPT` changes."""
    target = _turn_path(project_dir, turn.uuid)
    doc: dict = {}
    try:
        loaded = json.loads(target.read_text())
        if isinstance(loaded, dict):
            doc = loaded
    except (OSError, ValueError):
        doc = {}
    doc.update({"uuid": turn.uuid, "when": turn.when, "kind": turn.kind,
                "closed_by": closed_by})
    kept = [e for e in (doc.get("summaries") or [])
            if isinstance(e, dict) and (e.get("instructions") != instructions
                                        or e.get("model") != model)]
    doc["summaries"] = kept + [{
        "instructions": instructions, "model": model,
        "written": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "parts": parts}]
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written whole and moved into place, so a concurrent read never opens half a file.
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    tmp.replace(target)


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
                failed: bool, instructions: str, model: str) -> bool:
    """One finished turn onto disk, the moment its own chunks are in — a run
    killed part-way keeps every turn that completed. A turn holding a failed
    call is not written, or the missing part would come back as a hit."""
    if failed or not turn.uuid or not closed_by:
        return False
    if not any(part["bullets"] for part in parts):
        return False
    _write_breakdown(project_dir, turn, closed_by, instructions, model, parts)
    return True


_KIND_LABELS = {"said": "what Claude said", "edit": "file edits",
                "ran": "commands run", "output": "command output",
                "failed": "failures (command + error)", "you": "your own turns",
                "spawn": "subagents spawned", "tool": "other tool calls",
                "command": "slash commands you ran",
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


_CHECKPOINT_RE = re.compile(r"^chsum-checkpoint: (\S+) @ (\S+)$")


def _checkpoint_shas(project_dir: pathlib.Path | None, session_uuid: str) -> list[tuple[str, str]]:
    """(timestamp, sha) per turn the `hooks/chsum_checkpoint.py` Stop hook
    committed-then-reset-away for this session, oldest first (the raw reflog
    is newest-first). Never raises: no repo, no `git`, no hook installed, or
    a reflog that's already aged the entries out (see CLAUDE.md) all degrade
    to `[]`, same as a session that never had checkpoints at all — this must
    be exactly as forgiving as the rest of this file's summariser-failure
    handling, even though no model call is involved."""
    if not project_dir:
        return []
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["git", "reflog", "show", "HEAD", "--format=%H %gs"],
            cwd=project_dir, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        TRACE.step("_checkpoint_shas", repo=str(project_dir), git=type(e).__name__,
                   checkpoints=0, fallback="_files_touched")
        return []
    TRACE.proc(["git", "-C", str(project_dir), "reflog", "show", "HEAD"],
               proc.returncode, time.monotonic() - started,
               f"{len(proc.stdout.splitlines())} reflog entries")
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
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
            out.append((m.group(2), sha))
    out.reverse()
    TRACE.step("_checkpoint_shas", repo=str(project_dir), session=session_uuid[:8],
               checkpoints=len(out),
               span=f"{out[0][0]}→{out[-1][0]}" if out else "—")
    return out


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


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
    TRACE.proc(["git", "-C", str(project_dir), "diff", "-U0", prev_ref, cur_ref],
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


def _turn_checkpoints(spine: list[_Turn], until_ts: str,
                      project_dir: pathlib.Path | None, session_uuid: str) -> list[str]:
    """Per turn, the sha of the checkpoint covering its window, or "". The
    reflog arithmetic alone — no diffs — so the counts are available before the
    model calls that `_turn_files` runs after, and the reflog is read once.

    `spine[i].when` to `spine[i+1].when` (or `until_ts` for the last turn) is
    each turn's window — same rightmost-boundary convention `_turn_activity`
    bisects on. A window holding more than one checkpoint (shouldn't normally
    happen — one hook firing per turn) takes the last."""
    covers = [""] * len(spine)
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
        if j > ci:
            covers[i] = checkpoints[j - 1][1]
        ci = j
    covered = sum(1 for c in covers if c)
    TRACE.step("_turn_checkpoints", turns=len(spine), checkpoints=n,
               checkpoint=covered, transcript=len(covers) - covered,
               until=until_ts or "(none)")
    return covers


def _turn_files(turn_events: list[list[_Event]], covers: list[str],
                project_dir: pathlib.Path | None) -> list[list[str]]:
    """Per-turn files-touched, one entry per turn — a real `git diff` between
    checkpoints wherever `covers[i]` names one, since a checkpoint sees every
    change regardless of how it was made (a raw `sed -i`, not just
    Edit/Write/MultiEdit) and is never stale; `_files_touched`'s transcript
    scan for every other turn."""
    out: list[list[str]] = []
    last_sha: str | None = None
    diffs = 0
    for i, evs in enumerate(turn_events):
        sha = covers[i] if i < len(covers) else ""
        if not sha:
            out.append(_files_touched(evs))
            continue
        # The very first checkpoint seen has no prior checkpoint to diff
        # from, so its baseline is its own first parent — the branch tip
        # right before checkpointing started, not "nothing".
        prev = last_sha if last_sha is not None else f"{sha}^"
        out.append([f"- checkpoint `{sha[:12]}` — `git show {sha[:12]}` for this "
                    "turn's exact snapshot"] + _checkpoint_diff_files(project_dir, prev, sha))
        last_sha = sha
        diffs += 1
    TRACE.step("_turn_files", turns=len(turn_events), diffs=diffs)
    return out


def _checkpoint_source(covers: list[str]) -> list[str]:
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
        out.append(f"{head}  `{cmd}`" if cmd else head)
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


def _chunk_bullets(text: str) -> list[str]:
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
        return bullets
    if not preamble:
        return []
    return [f"- *no bullet list came back for this turn; the call replied:* "
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
    each chunk's call would send. Shared by `_dry_run_report` and the wizard's
    cost step so the two can't drift into quoting different numbers.

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
                    cached: set[int] | frozenset[int] = frozenset()) -> str:
    """`--dry-run`'s answer to "what is this going to cost, and why". Prices it
    the way a live run actually spends it, via `_cost_rows`, so totals reconcile
    to what a real run would send rather than approximating it. Characters are
    the computed fact; tokens carry `~` since `_est_tokens` is chars/4."""
    rows, by_kind = _cost_rows(events, boundaries, cached)
    called = [(chunk, chars) for chunk, chars, ok in rows if ok]
    n_called = len(called)

    prompt_chars = len(_CHUNK_PROMPT) * n_called
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
    kind_rows = [(_KIND_LABELS.get(k, k), v[1], v[0]) for k, v in by_kind.items()]
    kind_rows.sort(key=lambda r: -r[1])
    out.append(f"- instructions (the chunk prompt) — {size(len(_CHUNK_PROMPT))} "
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
    est = n_called * _est_tokens(_CHUNK_PROMPT) + sum(round(c / 4) for _, c in called)
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


def _local_when(ts: str, mtime: float) -> str:
    """Full local datetime for the picker's `when` column, in the same form the
    day headings use — `_ago`'s coarse age reads two same-day sessions as one.
    Same `mtime` fallback as `_ago`, for a conversation with no clock of its own."""
    t = _parse_ts(ts) if ts else None
    when = t.astimezone() if t else datetime.fromtimestamp(mtime).astimezone()
    return when.strftime("%a %d %b %Y %H:%M")


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
    rows = []
    for p in ranked:
        m = extract_meta(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        age = _ago(last_activity(p), mtime)
        title = ("✎ " if m.renamed else "") + (m.title or "(untitled)")
        rows.append((age, m.duration or "-", title))
    default = len(ranked)  # newest = last row = the one Enter picks
    idx_w, age_w, dur_w = len(str(default)), max(len(r[0]) for r in rows), \
        max(len(r[1]) for r in rows)
    # stderr here, not stdout, so `_colour_ok` needs telling which stream to ask.
    bold, dim, reset = ("\033[1m", "\033[2m", "\033[0m") if _colour_ok(sys.stderr) else ("", "", "")
    # Capped, same pattern as `cmd_sessions`: an unclipped title wraps at the
    # terminal's own column 0 and reads as a second row, not a folded one.
    cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
    title_w = max(10, cols - (idx_w + age_w + dur_w + 8))
    for i, (age, dur, title) in enumerate(rows, start=1):
        idx = f"{bold}{i:>{idx_w}}{reset}"
        age_c = f"{dim}{age:<{age_w}}{reset}"
        dur_c = f"{dim}{dur:<{dur_w}}{reset}"
        print(f"  {idx}  {age_c}  {dur_c}  {_clip_line(title, title_w)}", file=sys.stderr)
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
                   live: bool = True, anchor_uuid: str = "") -> int:
    """The document, for a window with a start and an optional end. One body
    for a bare `recap` and a ranged one — they differ only in how the window was
    chosen. `live` says which: bare runs to now, a chosen range to its end. An empty `until_ts` bounds the window by nothing, which is "to now"
    live and "to the end of the session" in a recap — so `live` is passed in
    rather than derived from it."""
    def out(text: str, **kw) -> None:
        print(_md_ansi(text) if _colour_ok() else text, **kw)

    # `--dry-run` makes no call, so there's nothing to slice.
    interleave = bool(turns) and not live and not getattr(args, "dry_run", False)
    # Prints before the slower event walk, so the first thing on screen is what you typed.
    _clear_status()
    header = [f"# {'Catch-up' if live else 'Recap'} — {meta.title}"]
    bits = [meta.project_name, f"your prompt at {_hhmm(anchor_ts)}"]
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
    # Your own turns are events too, or the extract shows a direction change with no cause.
    if turns:
        events += [_Event(t.when, "", "you", t.text) for t in turns]
        events.sort(key=lambda e: e.when)
    _status(f"{_plural(len(events), 'event')} since {_hhmm(anchor_ts)}")

    if not events:
        _clear_status()
        out("*Nothing recorded in that window — either it just started, or the "
            "transcript hasn't caught up yet.*", flush=True)
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

    if getattr(args, "dry_run", False):
        _clear_status()
        cached: set[int] = set()
        if (spine and not getattr(args, "no_cache", False)
                and not getattr(args, "invalidate", False)):
            _, cached = _cached_turns(
                path.parent.name, spine,
                _chunk_events(events, boundaries, _CHUNK_MAX_CHARS), boundaries,
                _fingerprint(_CHUNK_PROMPT, HaikuSummariser.model),
                HaikuSummariser.model)
        out(_dry_run_report(events, boundaries,
                            "recap", cached))
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
                text = summariser.digest(_CHUNK_PROMPT, _chunk_material(chunk))
                return text, summariser.usage, summariser.seconds, ""
            except SummariserError as e:
                return "", summariser.usage, summariser.seconds, str(e)
        finally:
            done.bump()  # counted whichever way it ended, or the line stalls short

    # The store is read only where the document slices onto turns: a bare recap
    # names its chunks by nothing durable, and its tail gap is open by construction.
    instructions = _fingerprint(_CHUNK_PROMPT, HaikuSummariser.model)
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
    closers = ([] if not write_store or all(h is not None for h in hits)
               else _turn_closers(path, spine, until_ts))
    outstanding = [sum(1 for i in idxs if i in pending) for idxs in per_turn]
    parts_by_turn: list[list[dict]] = [[] for _ in spine]
    turn_failed = [False] * len(spine)
    stored = 0
    active = [chunks[i] for i in pending if _chunk_activity(chunks[i])]
    chunk_est = sum(_est_tokens(_CHUNK_PROMPT) + _est_tokens(_chunk_material(c))
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
                    parts_by_turn[idx].append({
                        "material": _fingerprint(_chunk_material(chunks[i])),
                        "bullets": _chunk_bullets(text) if text else [],
                        "usage": usage or {}, "seconds": round(seconds, 3)})
                    turn_failed[idx] = turn_failed[idx] or bool(err)
                    outstanding[idx] -= 1
                    if outstanding[idx] == 0 and hits[idx] is None and _store_turn(
                            path.parent.name, spine[idx], closers[idx],
                            parts_by_turn[idx], turn_failed[idx], instructions,
                            HaikuSummariser.model):
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
    unavailable = (f"*Timeline unavailable — {attempted[0][3]}. Everything above "
                   "is still verbatim.*" if all_failed else "")
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
                    f"*Digest unavailable for part of this gap — {err}. The "
                    "verbatim record above still covers it.*")
            elif text:
                turn_bullets[idx].extend(_chunk_bullets(text))
        # Computed, not model-narrated — prepended ahead of the bullets it sits
        # beside, same reasoning as `_failures_section`/`_compaction_section`.
        # Prefers a git checkpoint diff over the transcript scan per turn,
        # wherever the `chsum_checkpoint.py` Stop hook covered it — see `_turn_files`.
        for idx, files in enumerate(_turn_files(turn_events, covers, project_dir)):
            if files:
                turn_bullets[idx] = files + turn_bullets[idx]
        sliced = _interleaved(spine, turn_bullets, [])
    elif not all_failed:
        parts: list[str] = []
        for text, _, _, err in results:
            if err:
                parts.append(f"*Digest unavailable for part of this window — {err}. "
                             "The verbatim record above still covers it.*")
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
    # After the document, and on stderr: what the calls were actually charged,
    # summed across all of them — the one set of numbers here that is neither
    # verbatim nor estimated but measured.
    if total_usage:
        _note(_usage_note(total_usage, total_seconds, chunk_est))
    return 0


@contextlib.contextmanager
def _tty_curses():
    """curses drawn on `/dev/tty` with fd 1 pointed at it for the duration —
    fd 1 is the document, so stdout is restored before anything prints. Raises
    if there's no controlling terminal; the caller falls back to typed prompts."""
    tty = os.open("/dev/tty", os.O_RDWR)
    saved = os.dup(1)
    scr = None
    try:
        os.dup2(tty, 1)
        scr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        scr.keypad(True)
        # 80ms: long enough that a split arrow-key sequence isn't misread as Escape.
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(80)
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # terminals that can't hide the cursor still work
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
        except curses.error:
            pass
        yield scr
    finally:
        if scr is not None:
            scr.keypad(False)
            curses.echo()
            curses.nocbreak()
            curses.endwin()
        os.dup2(saved, 1)
        os.close(saved)
        os.close(tty)


class _Wizard:
    """Arrow-key selection for `recap`: session, then start turn, then end turn,
    then what it will cost before anything is spent. Escape steps back rather
    than quitting, since every step is a guess you refine."""

    HELP = {
        0: "↑↓ move · f/l first/last · enter choose session · esc quit",
        1: "↑↓ move · f/l first/last · enter set START · esc back to sessions",
        2: "↑↓ move · f/l first/last · enter set END · esc back to start",
        3: "enter run the recap · esc back to end",
    }

    def __init__(self, cands: list[pathlib.Path], args=None):
        self.cands = cands
        # Held for the cost step alone: `--no-cache` has to price the same way it runs.
        self.args = args
        self.step = 0
        self.cursor = 0
        self.top = 0
        self.path: pathlib.Path | None = None
        self.turns: list[_Turn] = []
        self.acts: list[str] = []
        self.start = 0
        self.end = 0
        self.cost: list[str] = []
        self.lines: list[str] = []
        self.line_turn: list[int] = []
        self.line_kind: list[str] = []
        self.rows = [self._session_row(p) for p in cands]
        # Widths across every row and the header, so the columns line up as one
        # table. The title is last and unpadded — it is prose and runs long.
        self.widths = [max(len(r[i]) for r in (*self.rows, self.SESSION_HEADS))
                       for i in range(len(self.SESSION_HEADS) - 1)]
        # Newest last and selected, same default as the typed picker.
        self.cursor = max(0, len(cands) - 1)

    # Same columns `chsum` prints, in the same order — this is the screen where
    # you pick a session, and duration alone doesn't say which one was real work.
    SESSION_HEADS = ("when", "dur", "prompts", "files", "agents", "marks", "")

    @staticmethod
    def _session_row(p: pathlib.Path) -> tuple[str, ...]:
        m = extract_meta(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        title = ("✎ " if m.renamed else "") + (m.title or "(untitled)")
        return (_local_when(last_activity(p), mtime), m.duration or "-", str(m.prompts),
                str(len(m.edited)), str(m.agent_count) if m.agent_count else "-",
                f"⚑{len(m.marks)}" if m.marks else "-", title)

    def _session_line(self, row: tuple[str, ...]) -> str:
        return ("  ".join(f"{c:<{w}}" for c, w in zip(row, self.widths))
                + "  " + row[-1]).rstrip()

    # -- drawing ---------------------------------------------------------
    def _draw(self, scr) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        head = {0: "which session?", 1: "start where?", 2: "end where?",
                3: "what this will cost"}[self.step]
        scr.addnstr(0, 0, head, w - 1, curses.A_BOLD)
        if self.step == 0:
            # The blank line under the heading, spent on column names — the
            # numbers below it are unreadable without them.
            scr.addnstr(1, 0, self._session_line(self.SESSION_HEADS), w - 1, curses.A_DIM)
        body_h = max(1, h - 3)
        if self.step == 3:
            for i, line in enumerate(self.cost[:body_h]):
                scr.addnstr(2 + i, 0, line, w - 1)
        else:
            lines = self._lines()
            # The cursor is a turn, but a turn is several lines: scroll so its
            # whole block is on screen, not just the line the index points at.
            owners = self.line_turn if self.step else list(range(len(lines)))
            mine = [i for i, o in enumerate(owners) if o == self.cursor] or [0]
            if mine[0] < self.top:
                self.top = mine[0]
            elif mine[-1] >= self.top + body_h:
                self.top = min(mine[0], mine[-1] - body_h + 1)
            self.top = max(0, min(self.top, max(0, len(lines) - body_h)))
            for i, text in enumerate(lines[self.top:self.top + body_h]):
                idx = self.top + i
                owner = owners[idx]
                # Two channels: colour says what the line is, gutter+bold says whether it's in range.
                kind = self.line_kind[idx] if self.step else "said"
                attr = {"said": curses.A_NORMAL,
                        "answered": curses.color_pair(2),
                        "act": curses.A_DIM}.get(kind, curses.A_NORMAL)
                if self._in_range(owner):
                    attr |= curses.A_BOLD
                if owner == self.cursor:
                    attr |= curses.A_REVERSE
                if self.step:
                    gutter = "│ " if self._in_range(owner) else "  "
                    scr.addnstr(2 + i, 0, gutter, 2, curses.color_pair(1))
                    scr.addnstr(2 + i, 2, text.ljust(w - 3)[:w - 3], w - 3, attr)
                else:
                    scr.addnstr(2 + i, 0, text.ljust(w - 1)[:w - 1], w - 1, attr)
        scr.addnstr(h - 1, 0, self.HELP[self.step][:w - 1], w - 1, curses.A_DIM)
        scr.refresh()

    def _in_range(self, idx: int) -> bool:
        """Rows that would be included, highlighted as the end moves — the point
        of doing this on a screen rather than by typing two numbers."""
        if self.step != 2:
            return False
        lo, hi = sorted((self.start, self.cursor))
        return lo <= idx <= hi

    def _build_turn_lines(self, width: int) -> None:
        """Each turn as however many lines its text needs, plus what happened
        after it on a line of its own. Nothing you typed is shortened here —
        this is the screen where you decide what to include."""
        n = len(self.turns)
        w = len(str(n))
        # Two columns of gutter, drawn separately, so range membership can be
        # shown without spending the text's own colour on it.
        indent = " " * (w + 8)
        body_w = max(24, width - len(indent) - 3)
        self.lines, self.line_turn, self.line_kind = [], [], []
        for i, t in enumerate(self.turns):
            flag = "?" if t.kind == "answered" else " "
            head = f"{i + 1:>{w}}{flag} {_hhmm(t.when)}  "
            para = [ln for raw in t.text.splitlines() or [""]
                    for ln in (textwrap.wrap(raw, body_w) or [""])]
            for j, ln in enumerate(para):
                self.lines.append((head if j == 0 else indent) + ln)
                self.line_turn.append(i)
                self.line_kind.append(t.kind)  # "said" | "answered"
            act = self.acts[i] if i < len(self.acts) else ""
            if act:
                # Tagged to the same turn so it highlights with it, not as a row you could land on.
                self.lines.append(f"{indent}↳ {act}")
                self.line_turn.append(i)
                self.line_kind.append("act")

    def _lines(self) -> list[str]:
        if self.step == 0:
            return [self._session_line(r) for r in self.rows]
        return self.lines

    # -- steps -----------------------------------------------------------
    def _load_session(self, scr) -> bool:
        scr.erase()
        scr.addnstr(0, 0, "reading the transcript…", scr.getmaxyx()[1] - 1)
        scr.refresh()
        self.turns = _your_turns(self.cands[self.cursor])
        if not self.turns:
            scr.addnstr(2, 0, "nothing typed in that session — esc to go back",
                        scr.getmaxyx()[1] - 1)
            scr.refresh()
            scr.getch()
            return False
        self.path = self.cands[self.cursor]
        self.acts = _turn_activity(self.path, self.turns)
        self._build_turn_lines(scr.getmaxyx()[1] - 1)
        return True

    def _load_cost(self, scr) -> None:
        scr.erase()
        scr.addnstr(0, 0, "sizing the extract…", scr.getmaxyx()[1] - 1)
        scr.refresh()
        lo, hi = sorted((self.start, self.cursor))
        a, b = self.turns[lo], self.turns[hi]
        assert self.path is not None
        # Same far-edge rule as `cmd_recap`, or the price and the run disagree.
        until = "" if hi == len(self.turns) - 1 else b.when
        events = _events_since(self.path, a.line, a.when, until)
        inner = [t for t in self.turns if a.when < t.when <= b.when]
        events += [_Event(t.when, "", "you", t.text) for t in inner]
        events.sort(key=lambda e: e.when)
        # Mirrors `_render_window`'s `spine`, so this prices the N calls a real run would make.
        spine = [a] + inner
        boundaries = [t.when for t in spine]
        cached: set[int] = set()
        consulted = not getattr(self.args, "no_cache", False)
        if consulted:
            _, cached = _cached_turns(
                self.path.parent.name, spine,
                _chunk_events(events, boundaries, _CHUNK_MAX_CHARS), boundaries,
                _fingerprint(_CHUNK_PROMPT, HaikuSummariser.model),
                HaikuSummariser.model)
        rows, by_kind = _cost_rows(events, boundaries, cached)
        called = [(chunk, chars) for chunk, chars, ok in rows if ok]
        n_called, n_total = len(called), len(rows)
        prompt_chars = len(_CHUNK_PROMPT) * n_called
        material_chars = sum(chars for _, chars in called)
        total_chars = prompt_chars + material_chars
        est = n_called * _est_tokens(_CHUNK_PROMPT) + sum(round(c / 4) for _, c in called)
        self.cost = [
            f"turns {lo + 1}–{hi + 1}   {_hhmm(a.when)} → "
            f"{_hhmm(b.when) if until else 'end of session'}"
            f"   {_plural(len(events), 'event')}   {_plural(n_called, 'call')}"
            # Stated at zero too: a silent line cannot be told apart from a
            # store that was never read.
            + (f"   {len(cached)} of {n_total} chunks stored" if consulted else "")
            + (f"   {n_total - n_called - len(cached)} quiet"
               if n_total - n_called - len(cached) > 0 else ""),
            "",
        ]
        kind_rows = sorted(((_KIND_LABELS.get(k, k), v[1], v[0]) for k, v in by_kind.items()),
                           key=lambda r: -r[1])
        for label, chars, cnt in kind_rows:
            self.cost.append(f"  {label:<28} {_plural(cnt, 'event'):>12}"
                             f"  {_fmt_tokens(round(chars / 4)):>7} tokens")
        self.cost += [
            f"  {'chunk instructions':<28} {_plural(n_called, 'call'):>12}"
            f"  {_fmt_tokens(round(prompt_chars / 4)):>7} tokens",
            "",
            f"  extract          {total_chars:>9,} chars   ~{est:,} tokens",
            f"  claude -p floor  {'':>9}          ~{_HARNESS_FLOOR:,} tokens × {n_called}"
            f"  (measured {_HARNESS_FLOOR_WHEN})",
            f"  total                                ~{est + _HARNESS_FLOOR * n_called:,} tokens",
        ]

    @staticmethod
    def _decode_escape(scr) -> int:
        """An undecoded `ESC [ X` / `ESC O X` sequence to its key, else 27.
        Non-blocking, so a lone Escape reports immediately rather than stalling."""
        scr.nodelay(True)
        try:
            nxt = scr.getch()
            if nxt not in (ord("["), ord("O")):
                return 27
            final = scr.getch()
        finally:
            scr.nodelay(False)
        return {ord("A"): curses.KEY_UP, ord("B"): curses.KEY_DOWN,
                ord("5"): curses.KEY_PPAGE, ord("6"): curses.KEY_NPAGE,
                ord("H"): curses.KEY_HOME, ord("F"): curses.KEY_END}.get(final, 27)

    def run(self, scr) -> tuple[pathlib.Path, _Turn, _Turn] | None:
        while True:
            self._draw(scr)
            key = scr.getch()
            if key in (ord("q"), 3):  # q, ctrl-c
                return None
            if key == 27:
                # Escape, or an arrow whose sequence ncurses didn't decode for this terminal.
                key = self._decode_escape(scr)
            if key == 27:  # a real Escape: back one step, or out of the first
                if self.step == 0:
                    return None
                self.step -= 1
                if self.step == 1:
                    self.cursor = self.start
                continue
            if key in (curses.KEY_ENTER, 10, 13):
                if self.step == 0:
                    if self._load_session(scr):
                        self.step, self.cursor, self.top = 1, 0, 0
                elif self.step == 1:
                    self.start = self.cursor
                    self.step = 2
                elif self.step == 2:
                    self.end = self.cursor
                    self._load_cost(scr)
                    self.step = 3
                else:
                    lo, hi = sorted((self.start, self.end))
                    assert self.path is not None
                    return self.path, self.turns[lo], self.turns[hi]
                continue
            if self.step == 3:
                continue
            # Steps 1 and 2 move by turn, not by line — a multi-line turn is one thing to choose.
            n = len(self.rows) if self.step == 0 else len(self.turns)
            h = max(1, scr.getmaxyx()[0] - 3)
            moves = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                     curses.KEY_PPAGE: -h, curses.KEY_NPAGE: h}
            if key in moves:
                self.cursor = max(0, min(n - 1, self.cursor + moves[key]))
            # `f`/`l` alongside Home/End: the ends are what you reach for, and a
            # terminal that swallows Home/End still has letters.
            elif key in (curses.KEY_HOME, ord("f")):
                self.cursor = 0
            elif key in (curses.KEY_END, ord("l")):
                self.cursor = n - 1


def _ask(prompt: str, default: int, hi: int) -> int:
    """One numbered choice on stderr, defaulting on empty input. stderr because
    stdout is the document, and written rather than passed to `input()` for the
    same reason — `input`'s own prompt goes to stdout."""
    bold, reset = ("\033[1m", "\033[0m") if _colour_ok(sys.stderr) else ("", "")
    print(f"{prompt} [{bold}{default}{reset}]: ", end="", file=sys.stderr, flush=True)
    try:
        raw = input().strip()
    except EOFError:
        raw = ""
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= hi:
        return int(raw)
    raise SystemExit(f"not a turn number: {raw!r}")


def _pick_range(turns: list[_Turn], args) -> tuple[_Turn, _Turn]:
    """Start and end of the window, from your own turns. `--from/--to` skip the
    prompts entirely, so a recap is repeatable and scriptable; without them it
    only asks at a tty, or a script blocking on `input()` would hang."""
    n = len(turns)
    lo, hi = getattr(args, "from_", 0), getattr(args, "to", 0)
    if not (lo and hi) and not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise SystemExit("recap needs --from and --to when it can't ask "
                         "(not a terminal)")
    if not (lo and hi):
        show = turns if getattr(args, "all_turns", False) else turns[-30:]
        if len(show) < n:
            print(f"  … {n - len(show)} earlier turns hidden (--all-turns for all)",
                  file=sys.stderr)
        w = len(str(n))
        dim, reset = ("\033[2m", "\033[0m") if _colour_ok(sys.stderr) else ("", "")
        cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
        for t in show:
            i = turns.index(t) + 1
            # `?` marks an answer to the question tool: the turn exists and
            # steered the session, but you did not type it.
            flag = "?" if t.kind == "answered" else " "
            body = _clip_line(t.text.replace("\n", " · "), max(20, cols - w - 12))
            print(f"  {i:>{w}}{flag} {dim}{_hhmm(t.when)}{reset}  {body}", file=sys.stderr)
        lo = lo or _ask("start turn?", 1, n)
        hi = hi or _ask("end turn?", n, n)
    if not (1 <= lo <= n and 1 <= hi <= n):
        raise SystemExit(f"turn out of range: this session has {n}")
    if lo > hi:
        lo, hi = hi, lo  # asked backwards is a slip, not a different request
    return turns[lo - 1], turns[hi - 1]


def _run_wizard(args):
    """`("ok", selection)`, `("quit", None)`, or `("unavailable", None)` — kept
    apart because escaping the first step (a decision) must not fall through
    to the typed prompts the way an unavailable terminal does."""
    why = ("--no-tui" if getattr(args, "no_tui", False) else
           "--from/--to given" if (args.from_ or args.to) else
           "session named" if (args.ref or args.file) else
           "stdin is not a terminal" if not sys.stdin.isatty() else "")
    if os.environ.get("CHSUM_TUI_DEBUG"):
        pathlib.Path(os.environ["CHSUM_TUI_DEBUG"]).write_text(f"guard: {why or 'none'}\n")
    if why:
        return "unavailable", None
    cands = transcripts(local=True)
    if not cands:
        return "unavailable", None

    def recency(p: pathlib.Path) -> tuple[str, float]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (last_activity(p), mtime)

    cands = sorted(cands, key=recency)[-40:]
    try:
        with _tty_curses() as scr:
            picked = _Wizard(cands, args).run(scr)
        return ("ok", picked) if picked else ("quit", None)
    except (curses.error, OSError):
        # Falls back rather than failing the command; CHSUM_TUI_DEBUG surfaces why.
        if os.environ.get("CHSUM_TUI_DEBUG"):
            import traceback
            pathlib.Path(os.environ["CHSUM_TUI_DEBUG"]).write_text(traceback.format_exc())
        return "unavailable", None


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
    stored = [bool(t.uuid) and _turn_path(project_dir, t.uuid).exists() for t in turns]
    first_new = next((i for i, done in enumerate(stored) if not done), None)
    TRACE.step("_recap_start", turns=len(turns), stored=sum(stored),
               start_turn=(first_new + 1) if first_new is not None else "(all stored)")
    return first_new


def _recap_target(args) -> pathlib.Path:
    """Which conversation a recap is about. Bare means the one you are in — the
    same context-awareness `--here` had, now the default."""
    if args.file:
        return pathlib.Path(args.file)
    if args.last:
        return latest_transcript(local=not args.all)
    ref = args.session or args.ref
    if ref:
        return path_for_ref(ref)
    return live_transcript()


def cmd_recap(args) -> int:
    """A window of one conversation, verbatim, with a model-written timeline
    sliced under each of your turns.

    Bare: the session you are in, from the turn after the last one the store
    covers. `--full` takes the whole of it, `--session`/`--last`/`--file` name a
    different one, `--from/--to` name the ends. Only a named session with no
    ends asks."""
    named = bool(args.file or args.last or args.session or args.ref)
    if args.here or (not named and not args.full and not (args.from_ and args.to)):
        return _recap_since_last(args)

    picked = None
    if named or args.full:
        path = _recap_target(args)
    else:
        # Ends given for the session you are in.
        path = live_transcript()
    # `--last` names a whole conversation, the way `chsum last` did; only
    # `--session`/a bare ref leaves the ends open for the picker.
    whole = args.full or (args.last and not (args.from_ and args.to))
    if not (whole or (args.from_ and args.to)):
        # A named session with no ends: the picker, as before.
        verdict, picked = _run_wizard(args)
        if verdict == "quit":
            return 0  # you escaped out: nothing chosen, nothing to print
    if picked:
        path, start, end = picked
        turns = _your_turns(path)
    else:
        turns = _your_turns(path)
        if not turns:
            raise SystemExit("nothing typed in that conversation — no turns to pick from")
        if whole:
            start, end = turns[0], turns[-1]
        else:
            start, end = _pick_range(turns, args)
    _status("reading the transcript…")
    meta = extract_meta(path)
    # The end turn bounds the window; the turns strictly between the two are
    # yours as well and belong in the record.
    inner = [t for t in turns if start.when < t.when <= end.when]
    # Ending on the last turn bounds the window by nothing: that turn's own work
    # is what follows it, and a bound at its timestamp would cut all of it.
    # Compared by value, not identity: the wizard picked its turn out of its own
    # `_your_turns` list and this one is read again, so the last turn of the
    # window and the last turn of the file are equal objects and never the same one.
    until = "" if end == turns[-1] else end.when
    # The interactively-picked range as flags, or the range can't be typed again.
    TRACE.reproduce(f"chsum recap --session {ch_ref_for_path(path)} "
                    f"--from {turns.index(start) + 1} --to {turns.index(end) + 1}")
    TRACE.step("cmd_recap", turns=len(turns), start_turn=turns.index(start) + 1,
               end_turn=turns.index(end) + 1, start_line=start.line,
               until=until or "(end of session)", inner=len(inner),
               picked_by="wizard" if picked else ("whole session" if whole else "flags"))
    return _render_window(path, meta, start.line, start.when, start.text,
                          until, args, inner, live=False, anchor_uuid=start.uuid)


def _recap_since_last(args) -> int:
    """`chsum recap` with nothing else: the session you are in, from the turn
    after the last one the store covers. No picker — the window is already
    decided, and asking would be asking a question with one answer."""
    # `_pick_transcript` asks when several sessions are live and falls back to
    # `live_transcript` off a tty — the behaviour `cmd_catchup` had here.
    path = _pick_transcript()
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
                    f"--from {first_new + 1} --to {len(turns)}")
    TRACE.step("_recap_since_last", turns=len(turns), start_turn=first_new + 1,
               covered=first_new)
    # The last turn bounds the window by nothing: its own work is what follows it.
    return _render_window(path, meta, start.line, start.when, start.text,
                          "", args, inner, live=False, anchor_uuid=start.uuid)


def cmd_sessions(args) -> int:
    """One line per conversation, newest first. Triage: which were real work.
    Empty sessions are listed, not hidden, and so is the one running right now
    (tagged) — `--last` skips that one, since you're already in it."""
    cutoff = _parse_since(args.since) if args.since else None
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    metas = []
    for p in transcripts(local=not args.all):
        if cutoff is not None and p.stat().st_mtime < cutoff:
            continue
        meta = extract_meta(p)
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
    shown = metas if args.limit <= 0 else metas[:args.limit]

    scope = "all projects" if args.all else project_dir_name(pathlib.Path.cwd())
    window = f" · last {args.since}" if args.since else ""
    empty = sum(1 for m in shown if not _has_activity(m))
    print(f"# Sessions — {scope}{window}")
    print(f"*{len(shown)} of {len(metas)} shown · {empty} with no activity*\n")

    # Two lines each, as `mark --list`: a title is prose, and in a column the long
    # ones pushed every other field off the terminal.
    cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
    # By last activity, as `journal`: a resumed session belongs to the day you
    # last worked on it.
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for m in shown:
        assert m.path is not None  # every `m` here came from extract_meta, which always sets it
        # Tagged since its numbers are a snapshot — still being appended to.
        here = " · this session (in progress)" if m.path.stem == live else ""
        # Ids in full — `chsum context <ref>/<id>` matches exactly, so a clipped one wouldn't resolve.
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
            f"⚑{len(m.marks)}" if m.marks else "-",
            # Marked, because provenance differs: one is Claude Code's reading of
            # the session, the other is yours.
            ("✎ " if m.renamed else "") + (m.title or "(untitled)") + here,
            delegated,  # past the width calculation, which stops at `heads`
        ))
    heads = ("", "dur", "prompts", "files", "agents", "marks")  # dates fill the ref column
    # Widths across every day: columns that shift per group read as separate tables.
    widths = [max(len(r[i]) for rs in by_day.values() for r in (*rs, heads))
              for i in range(len(heads))]
    row = lambda r: "  " + "  ".join(f"{c:<{w}}" for c, w in zip(r, widths)).rstrip()
    gutter = 2 + widths[0] + 2  # past the ref column, where `dur` starts
    for day_no, (day, rows) in enumerate(by_day.items()):
        label = _pretty_day(day)
        if day_no:
            print(label)
        else:  # headers once, on the first date's line
            print(f"{label:<{gutter}}" + _dim(row(heads).strip()))
        for r in rows:
            print(row(r))
            # Wrapped here, not by the terminal: a title folded at column 0 reads
            # as the next session.
            print(_dim("\n".join(textwrap.wrap(r[6], cols, initial_indent="    ↳ ",
                                               subsequent_indent="      "))))
            for line in r[7]:
                print(_dim(f"      {line}"))
        print()
    sys.stdout.flush()
    print("Read one: `chsum context <ref>`   Most recent real session: `chsum context --last`",
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


def cmd_journal(args) -> int:
    """Chronological work log. Pure JSONL — no claude-history calls, so it stays
    fast across the whole corpus."""
    cutoff = _parse_since(args.since)
    metas = []
    for p in transcripts(local=not args.all):
        # mtime is a cheap superset filter; a resumed old session has a recent one.
        if p.stat().st_mtime < cutoff:
            continue
        meta = extract_meta(p)
        if not _has_activity(meta):  # aborted, tool-only, or went nowhere
            continue
        end = _parse_ts(meta.ended)
        if end and end.timestamp() < cutoff:
            continue
        metas.append(meta)
    if not metas:
        print("no conversations in that window", file=sys.stderr)
        return 1

    # By last activity: a resumed session belongs to the day you last worked on it.
    metas.sort(key=lambda m: m.ended or "")
    by_day: dict[str, list[Meta]] = defaultdict(list)
    for m in metas:
        by_day[(m.ended or "")[:10] or "undated"].append(m)

    total_files = len({f for m in metas for f in m.edited})
    span = f"last {args.since}" + ("" if args.all else " · this project")
    print(f"# Work log — {span}\n")
    print(f"*{_plural(len(metas), 'session')} · {_plural(len(by_day), 'day')} · "
          f"{_plural(total_files, 'file')} changed*\n")

    for day, sessions in by_day.items():
        print(f"## {_pretty_day(day)}\n")
        for m in sessions:
            assert m.path is not None  # every `m` here came from extract_meta, which always sets it
            bits = [b for b in (m.duration, m.project_name,
                                f"`{m.branch}`" if m.branch else "",
                                f"resumed from {m.date}"
                                if m.resumed and m.date != (m.ended or "")[:10] else "") if b]
            print(f"### {m.title}")
            print(f"*{' · '.join(bits)}*\n")
            for mk in m.marks:
                print(f"⚑ {mk.reason}")
            if m.marks:
                print()
            # Same five-then-count as the listing. A week's log is mostly a
            # question of what got worked on, and "2 agents" doesn't answer it.
            for run in m.agents[:5]:
                bits = [b for b in (run.duration,
                                    f"{_plural(len(run.edited), 'file')}"
                                    if run.edited else "",
                                    f"{_plural(len(run.commands), 'command')}"
                                    if run.commands and not run.edited else "") if b]
                print(f"↳ {run.description or run.id}"
                      + (f" ({', '.join(bits)})" if bits else ""))
            if m.agent_count > len(m.agents[:5]):
                # agent_count can exceed the sidecars there are names for.
                print(f"↳ …and {m.agent_count - len(m.agents[:5])} more")
            if m.agent_count:
                print()
            if m.edited:
                print(f"Changed {len(m.edited)} file(s): "
                      + ", ".join(f"`{f}`" for f in m.edited[:6])
                      + (f" +{len(m.edited) - 6} more" if len(m.edited) > 6 else ""))
                print()
            print(f"`chsum context {ch_ref_for_path(m.path)}`\n")
    return 0


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
_CHUNK_PROMPT = """\
Below is a verbatim extract from one slice of a Claude Code session: a
consecutive run of recorded events — assistant messages, tool calls, file
edits (with the edited text), commands and the tail of their output, subagent
activity — oldest first.

A line ending `… [+N chars not in this extract]` was shortened by the tool that
built this extract. The message itself was complete. Never describe it as cut
off, interrupted, incomplete, or unfinished — that is a fact about the extract,
not about what happened.

Who did what, by event kind. `said:`, `ran:`, `edit:`, `tool:`, `output:`,
`failed:` and `spawn:` are all Claude's own work. Only two kinds are the
user's: `you:` is something the user typed, and `command:` is a slash command
the user ran. An event tagged `agent <id>` is a subagent Claude spawned.

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
  Never "the user ran" for a `ran:`, `edit:` or `tool:` event.
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="chsum",
        description="Work logs and reload-ready context from Claude Code conversations. "
                    "Deterministic: nothing invented. The one model-written section "
                    "(`recap`'s timeline) is labelled as such.",
    )
    ap.add_argument("--out", type=pathlib.Path, default=DIGEST_DIR,
                    help=f"digest directory (default: {DIGEST_DIR})")
    # A parent, not a top-level flag: `--out` sits on `ap` and so has to precede
    # the subcommand, and `--debug` is typed at the end of whatever you just ran.
    dbg = argparse.ArgumentParser(add_help=False)
    dbg.add_argument("--debug", action="store_true",
                     help="print what this run read, ran and resolved, for pasting "
                          "into a chsum session to reproduce from")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("sessions", parents=[dbg],
                       help="one line per conversation in this project (the default)")
    p.add_argument("-n", "--limit", type=int, default=5, metavar="N",
                   help="how many to list, 0 for all (default: 5)")
    p.add_argument("--since", default=None, help="window, e.g. 7d, 24h, 2w (default: all time)")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("find", parents=[dbg], help="search conversations")
    p.add_argument("query", nargs="?")
    p.add_argument("--all", action="store_true", help="all workspaces (default: this one)")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--marks", action="store_true",
                   help="search what you marked with `chsum mark` (query optional)")
    for mode in ("hybrid", "semantic", "lexical", "exact"):
        p.add_argument(f"--{mode}", dest="mode", action="store_const", const=mode)
    p.set_defaults(mode="hybrid", func=cmd_find)

    p = sub.add_parser("recap", parents=[dbg],
                       help="summarise this session since the last recap, or a chosen range")
    p.add_argument("ref", nargs="?", help="ch_... ref (same as --session)")
    p.add_argument("--session", metavar="REF",
                   help="recap this conversation instead of the one you're in")
    p.add_argument("--last", action="store_true",
                   help="the most recent conversation that isn't this one")
    p.add_argument("--full", action="store_true",
                   help="the whole session, not just since the last recap")
    p.add_argument("--all", action="store_true",
                   help="with --last: all projects (default: this one)")
    # The old spelling. `recap` with no arguments is now what this meant.
    p.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--file", help="transcript path")
    p.add_argument("--from", dest="from_", type=int, default=0, metavar="N",
                   help="start at your Nth turn (default: ask)")
    p.add_argument("--to", type=int, default=0, metavar="N",
                   help="end at your Nth turn (default: ask)")
    p.add_argument("--all-turns", dest="all_turns", action="store_true",
                   help="list every turn when asking, not just the last 30")
    p.add_argument("--no-tui", dest="no_tui", action="store_true",
                   help="typed prompts instead of the arrow-key picker")
    p.add_argument("--dry-run", action="store_true",
                   help="size breakdown of what would be sent; no model call")
    p.add_argument("--no-cache", dest="no_cache", action="store_true",
                   help=f"call for every chunk; neither read nor write {TURNS_DIR}")
    p.add_argument("--invalidate", action="store_true",
                   help="treat what's stored for this window as no longer standing: "
                        "call for every chunk and replace it")
    p.set_defaults(func=cmd_recap)

    p = sub.add_parser("digest", parents=[dbg], help="deterministic digest of one conversation")
    p.add_argument("ref", nargs="?", help="ch_... ref from `chsum find`")
    p.add_argument("--file", help="transcript path (derives the ref)")
    p.add_argument("--stdout", action="store_true", help="print instead of writing a file")
    p.add_argument("--last", action="store_true",
                   help="the most recent conversation that isn't this one")
    p.add_argument("-n", "--nth", type=int, default=1, metavar="N",
                   help="with --last: Nth most recent instead of the last (default: 1)")
    p.add_argument("--all", action="store_true",
                   help="with --last: all projects (default: this one)")
    p.add_argument("--messages", action="store_true",
                   help="every message in order, unfiltered, each locating its record")
    p.add_argument("--tools", action="store_true",
                   help="every tool call in order, unfiltered")
    p.add_argument("--commands", action="store_true",
                   help="every Bash invocation in order, unfiltered")
    p.add_argument("--call", metavar="ID",
                   help="one tool call whole, with its output")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("context", parents=[dbg], help="reload artifact for pasting back into Claude")
    p.add_argument("ref", nargs="?")
    p.add_argument("--last", action="store_true",
                   help="the most recent conversation that isn't this one")
    p.add_argument("-n", "--nth", type=int, default=1, metavar="N",
                   help="with --last: Nth most recent instead of the last (default: 1)")
    p.add_argument("--all", action="store_true",
                   help="with --last: all projects (default: this one)")
    p.add_argument("--file")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser(
        "mark", parents=[dbg], help="flag this moment as notable, for the digest to pick up",
        description="Prints a marker. Nothing is written: run through Claude Code's "
                    "`!` prefix, the harness records the run itself, so the mark lands "
                    "in the transcript where you typed it.",
    )
    p.add_argument("reason", nargs="*", help="why this matters — quoted verbatim later")
    p.add_argument("--at", metavar="ID",
                   help="mark an earlier message: a record id from --recent, or a row number")
    p.add_argument("--match", metavar="TEXT",
                   help="mark the one message containing TEXT; lists candidates if "
                        "more than one matches")
    p.add_argument("--recent", nargs="?", type=int, const=20, default=None, metavar="N",
                   help="list the last N messages and tool calls (default 20), with "
                        "the record ids --at takes")
    p.add_argument("--list", action="store_true",
                   help="marks made in this conversation, with the ids --revoke takes")
    p.add_argument("--show", metavar="ID",
                   help="where a mark landed: file, row, time, agent, and the "
                        "message it points at")
    p.add_argument("--context", type=int, default=3, metavar="N",
                   help="with --show: N records either side of it (default 3)")
    p.add_argument("--full", action="store_true",
                   help="with --list: whole reason and whole marked message, unclipped")
    p.add_argument("--revoke", nargs="+", metavar="ID",
                   help="drop marks made earlier (they stay in the transcript, "
                        "but stop counting)")
    p.add_argument("--file", help="transcript path (default: the session you're in)")
    p.set_defaults(func=cmd_mark)

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
    p.add_argument("--list", action="store_true", help="every conversation you renamed")
    p.add_argument("--no-resume", action="store_true",
                   help="rename in chsum only — leave the transcript, and /resume, alone")
    p.add_argument("--file", help="transcript path (default: the session you're in)")
    p.set_defaults(func=cmd_name)

    p = sub.add_parser("journal", parents=[dbg], help="chronological work log")
    p.add_argument("--since", default="7d", help="window, e.g. 7d, 24h, 2w")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.set_defaults(func=cmd_journal)

    # Bare `chsum` lists sessions: you usually want to pick one, and "most recent"
    # is often a dud. Anything naming a subcommand or asking for help is left alone.
    raw = list(argv) if argv is not None else sys.argv[1:]
    if not any(tok in sub.choices or tok in ("-h", "--help") for tok in raw):
        raw = ["sessions"] + raw
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
    if TRACE.on:
        print(TRACE.render(code))
    return code


if __name__ == "__main__":
    sys.exit(main())
