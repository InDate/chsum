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
import json
import os
import pathlib
import re
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
DIGEST_DIR = pathlib.Path.home() / ".claude" / "chsum" / "digests"

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
        raise HistoryError("claude-history not found on PATH")
    proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
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


def last_message_number(ref: str) -> int:
    """Highest message ordinal, the upper bound for a full read. Handles both
    `outline` shapes (segmented and bare), or short conversations read as empty."""
    end = 0
    for line in _history("agent", "outline", ref, "--no-budget").splitlines():
        if line.startswith("seg "):
            m = re.search(r"m(\d+)\.\.m(\d+)", line)
            if m:
                end = max(end, int(m.group(2)))
        elif m := re.match(r"m(\d+)\s", line):
            end = max(end, int(m.group(1)))
    return end


def uuid_for_ref(ref: str) -> str:
    for line in _history("agent", "outline", ref, "--no-budget").splitlines():
        if line.startswith("conversation "):
            return _fields(line).get("uuid", "")
    return ""


@dataclass
class Message:
    n: int
    role: str
    anchor: str
    text: str
    line: int = 0  # 1-based line of its first record in the JSONL; 0 when unknown


def read_messages(ref: str, start: int = 1, end: int | None = None) -> list[Message]:
    """Parses `agent read` output (already stripped of tool sludge) into
    messages with their mN ordinal and ma_ anchor."""
    end = end or last_message_number(ref)
    if not end:
        return []
    raw = _history("agent", "read", f"{ref}:m{start}..m{end}", "--no-budget")
    msgs: list[Message] = []
    cur: Message | None = None
    body: list[str] = []
    for line in raw.splitlines():
        if line.startswith("message "):
            if cur:
                cur.text = "\n".join(body).strip()
                msgs.append(cur)
            f = _fields(line)
            # Bare positional token ("message m17 ..."), not key=value — read off directly.
            m = re.match(r"message\s+m(\d+)", line)
            cur = Message(
                n=int(m.group(1)) if m else 0,
                role=f.get("role", "?"),
                anchor=f.get("anchor", ""),
                text="",
                line=int(f["line"]) if f.get("line", "").isdigit() else 0,
            )
            body = []
        elif line.startswith("| ") or line == "|":
            body.append(line[2:] if len(line) > 1 else "")
    if cur:
        cur.text = "\n".join(body).strip()
        msgs.append(cur)
    return msgs


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

NAMES_PATH = DIGEST_DIR.parent / "names.json"

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
    agent: str = ""  # sidecar it came from; blank for the parent's own
    n: int = 0  # message ordinal, only on the digest path
    anchor: str = ""


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
    if _MARK_GREP not in data:
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
            marks.append(Mark(reason=_MARK_TAIL_RE.sub("", m.group("reason")).strip(),
                              rec=str(rec.get("uuid") or ""),
                              at=fields.get("at", ""),
                              line=lineno,
                              when=str(rec.get("timestamp") or "")))
    if any(m.at for m in marks):
        _resolve_marked(data, marks, path)
        # A mark typed in the transcript can name a record in a sidecar: while an
        # agent is running, that is the file the conversation is landing in.
        for side, agent in mark_sources(path)[1:]:
            missing = [m for m in marks if m.at and not m.at_line]
            if not missing:
                break
            _resolve_marked(side.read_text(errors="replace"), missing, side, agent)
    if any(not m.at for m in marks):
        _resolve_here(data, marks, path)
    return marks, revoked


def _text_at_line(lines: list[str], lineno: int) -> str:
    """Whole text of the record on that line — what `--list --full` shows."""
    if not lineno or lineno > len(lines):
        return ""
    try:
        rec = json.loads(lines[lineno - 1])
    except json.JSONDecodeError:
        return ""
    return _record_text(rec).strip() if isinstance(rec, dict) else ""


def _resolve_here(data: str, marks: list[Mark], path: pathlib.Path | None = None) -> None:
    """Point a bare `chsum mark` at the last real thing said before it (its own
    record is command output, not a target), skipping its own plumbing. Ordered
    by timestamp once sidecars are in play, since two files' line numbers don't
    order against each other; falls back to line order otherwise."""
    said: list[tuple[str, int, pathlib.Path | None, str, str]] = []
    for src, agent in (mark_sources(path) if path else [(None, "")]):
        raw_text = data if src is None or src == path else src.read_text(errors="replace")
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
                         next((ln for ln in text.splitlines() if ln.strip()), "")))
    said.sort(key=lambda s: (s[0], s[1]))
    for mk in marks:
        if mk.at:
            continue
        if mk.when:
            prior = [s for s in said if (s[0], s[1]) < (mk.when, mk.line)]
        else:
            prior = [s for s in said if s[2] in (None, path) and s[1] < mk.line]
        if prior:
            _ts, mk.at_line, mk.at_path, agent, mk.quote = prior[-1]
            if agent:
                mk.agent = agent


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
                    mk.agent = agent
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
    stamps, edited, read, cmds = [], [], [], []
    for rec in _records(side):
        if rec.get("timestamp"):
            stamps.append(rec["timestamp"])
        if rec.get("type") in ("user", "assistant"):
            _collect_tools(rec, edited, read, cmds)
    if stamps:
        stamps.sort()
        run.duration = _fmt_secs(active_seconds(stamps))
    run.edited, run.commands = edited, _dedupe(cmds)
    run.marks, run.revoked = _scan_marks_raw(side)
    for mk in run.marks:
        mk.agent = run.id
    return run


def extract_meta(path: pathlib.Path) -> Meta:
    meta = Meta(uuid=path.stem, path=path)
    stamps, edited, read, cmds = [], [], [], []
    for rec in _records(path):
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
            meta.spawned += _collect_tools(rec, edited, read, cmds)
        if rec.get("type") == "user" and _is_typed_prompt(rec):
            meta.prompts += 1
    # Yours wins over Claude Code's own later `ai-title` appends.
    named = load_names().get(meta.uuid, "")
    meta.renamed = bool(named)
    meta.title = named or meta.ai_title or "(untitled)"
    own_edits = set(edited)

    # Fold subagent tool use into the parent; prompts stay parent-only.
    meta.agents = [extract_agent(s) for s in subagent_transcripts(path)]
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


def _is_typed_prompt(rec: dict) -> bool:
    """A user record carrying text the human actually wrote. Most user-role records
    are tool_results; the rest is harness scaffolding (interrupts, notifications)
    and `!` runs (see `is_typed_prompt`)."""
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


def _collect_tools(rec: dict, edited: list, read: list, cmds: list) -> int:
    """Append this record's tool use to the accumulators; return agents spawned."""
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


def _timed_bullets(pairs: list[tuple[str, str]], limit: int) -> list[str]:
    """Same shape as `_bullets`, stamped: `pairs` is (when, text), the overflow
    line hand-built because `_bullets` itself has no room for a time column."""
    out = [f"- {_hhmm(when)}  `{text}`" for when, text in pairs[:limit]]
    if len(pairs) > limit:
        out.append(f"- …and {len(pairs) - limit} more")
    return out


def _clip(text: str, limit: int, hint: str = "read the anchor") -> str:
    # The hint is a parameter because agent digests have no anchors — pointing at
    # one invites the reader to invent a ref that claude-history will reject.
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
    """Text messages straight from a transcript file. Sidecars only — claude-history
    has no per-agent ref. Anchors stay empty: `ma_` values are claude-history's
    to mint, and a fabricated one is worse than none."""
    msgs: list[Message] = []
    for rec in _records(path):
        role = rec.get("type")
        if role not in ("user", "assistant"):
            continue
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
            msgs.append(Message(n=len(msgs) + 1, role=role, anchor="", text=text))
    return msgs


_SIDECAR_HINT = "read the sidecar under Drill down"

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
        parts += _render_marks(run.marks, [], show_agent=False) + [""]

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
    parts.append("Inlined into the parent read (untagged, all agents at once): "
                 f"`claude-history agent read {parent_ref}:mN..mN --subagents --no-budget`\n")
    return "\n".join(parts).rstrip() + "\n"


def render_digest(meta: Meta, ref: str, msgs: list[Message], *,
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
        parts += _render_marks(meta.marks, msgs) + [""]

    # The intent trail: verbatim, in order. This is the summary, uninvented.
    parts.append("## What I asked for\n")
    if not prompts:
        parts.append("*No user prompts recorded.*\n")
    else:
        shown = prompts[:max_prompts]
        for m in shown:
            parts.append(f"**m{m.n}**\n")
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
        parts += _bullets(meta.commands, 10) + [""]

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
            parts.append(f"**{label}** (m{m.n})\n")
            parts.append(_quote(_clip(m.text, 600)) + "\n")
    else:
        parts.append("*Nothing recorded.*\n")

    parts.append("## Drill down\n")
    parts.append(f"Read any message: `claude-history agent read {ref}:mN..mN --no-budget`\n")
    cited = _citable_anchors(msgs, prompts[:max_prompts] + (tail or []))
    if cited:
        parts.append("Durable anchors (survive renumbering if the transcript changes):\n")
        parts += [f"- m{n} → `{a}`" for n, a in cited[:12]] + [""]
    return "\n".join(parts).rstrip() + "\n"


def _locate_mark(mark: Mark, msgs: list[Message]) -> None:
    """Give a mark its mN and anchor only on an exact line match; unplaced marks
    fall back to their record id, and the quote carries the content either way."""
    target = mark.at_line or mark.line
    # A sidecar has no per-agent ref to number lines against, so it gets its agent id instead.
    if not target or mark.agent:
        return
    for m in msgs:
        if m.line == target:
            mark.n, mark.anchor = m.n, m.anchor
            return


def _render_marks(marks: list[Mark], msgs: list[Message],
                  show_agent: bool = True) -> list[str]:
    out = []
    for mk in marks:
        _locate_mark(mk, msgs)
        where = []
        if mk.n:
            where.append(f"m{mk.n}")
        if mk.anchor:
            where.append(f"`{mk.anchor}`")
        if mk.agent and show_agent:
            where.append(f"agent `{mk.agent}`")
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


def _citable_anchors(all_msgs: list[Message], cited: list[Message]) -> list[tuple[int, str]]:
    """Anchors safe to publish: present, unique, one per message."""
    counts: dict[str, int] = {}
    for m in all_msgs:
        if m.anchor:
            counts[m.anchor] = counts.get(m.anchor, 0) + 1
    out, seen = [], set()
    for m in cited:
        if m.anchor and counts.get(m.anchor) == 1 and m.n not in seen:
            seen.add(m.n)
            out.append((m.n, m.anchor))
    return sorted(out)


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
        got = uuid_for_ref(ref)
        if got != path.stem:
            raise SystemExit(
                f"derived ref resolved to {got or '(nothing)'}, expected {path.stem}.\n"
                "claude-history's ref scheme has probably changed — use `chsum find` instead."
            )
        return ref
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
    uuid = uuid_for_ref(ref)
    if not uuid:
        raise HistoryError(f"{ref} did not resolve to a conversation")
    path = _path_for_uuid(uuid)
    if not path:
        raise HistoryError(f"no transcript on disk for {uuid}")
    return path


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
    return meta, render_digest(meta, ref, read_messages(ref))


def cmd_digest(args) -> int:
    ref = resolve_ref(args)
    meta, md = _digest_for(ref)
    if args.stdout:
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
    ref = resolve_ref(args)
    meta, md = _digest_for(ref)
    parent_ref, agent_id = _split_agent_ref(ref)
    print("<!-- Extracted verbatim from the transcript by chsum. No model wrote this;")
    print("     nothing here is paraphrased. Quotes may be clipped — full text is in")
    if agent_id:
        # chsum's own address, not a claude-history one — `agent read` would fail.
        print("     the sidecar named under Drill down. -->")
    else:
        print(f"     the transcript: claude-history agent read {ref}:mN..mN --no-budget -->")
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
            return path
    cands = sorted(transcripts(local=True), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        raise SystemExit("no conversation found for this project")
    return cands[0]


def _mark_target(path: pathlib.Path, spec: str) -> str:
    """Resolve `--at` to a full record uuid: either a uuid prefix from
    `chsum mark --recent` (looked for in sidecars too, since `--recent` lists
    them), or an mN, which needs claude-history to place it and stays parent-only."""
    if re.fullmatch(r"m\d+", spec):
        ref = ch_ref_for_path(path)
        want = int(spec[1:])
        msg = next((m for m in read_messages(ref) if m.n == want and m.line), None)
        if not msg:
            raise SystemExit(
                f"claude-history can't place {spec} in this transcript yet — its view of a "
                "live session lags behind the file.\nPick from `chsum mark --recent 20` "
                "and pass the record id instead."
            )
        line = msg.line
    else:
        line = 0
    prefix = spec.lower()
    hits = []
    for src, _agent in ([(path, "")] if line else mark_sources(path)):
        for lineno, raw in enumerate(src.read_text(errors="replace").splitlines(), start=1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            uid = rec.get("uuid") if isinstance(rec, dict) else ""
            if not isinstance(uid, str) or not uid:
                continue
            if (line and lineno == line) or (not line and uid.startswith(prefix)):
                hits.append(uid)
    if not hits:
        raise SystemExit(f"no record matching {spec!r} in {path.name}")
    if len(hits) > 1:
        raise SystemExit(f"{spec!r} matches {len(hits)} records — use more characters")
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


def _match_target(path: pathlib.Path, needle: str) -> str:
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
        hits.append((uid, rec, label, agent))
    if not hits:
        raise SystemExit(f"nothing in this conversation matches {needle!r}")
    if len(hits) > 1:
        print(f"{len(hits)} matches — mark one with --at, or give a longer string:",
              file=sys.stderr)
        for uid, rec, label, agent in hits:
            who = f"agent {agent[:8]}" if agent else (
                "you" if rec.get("type") == "user" else "claude")
            when = str(rec.get("timestamp") or "")[11:16]
            print(f"  {uid[:8]}  {when:<5}  {who:<14}  {label[:80]}", file=sys.stderr)
        raise SystemExit(2)
    return hits[0][0]


def cmd_mark(args) -> int:
    """Mark a moment as notable. Writes nothing: printing the sentinel is the
    whole mechanism — see the Marks section above for why."""
    path = pathlib.Path(args.file).expanduser().resolve() if args.file else live_transcript()
    if not path.exists():
        raise SystemExit(f"no such transcript: {path}")

    if args.list:
        # The session's marks (agents' folded in too), same pooled definition extract_meta uses.
        marks = extract_meta(path).marks
        if not marks:
            print("nothing marked in this conversation", file=sys.stderr)
            return 1
        cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
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
            print(f"\n{_plural(len(marks), 'mark')}.", file=sys.stderr)
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
        print('\nDrop one: `chsum mark --revoke <id>`', file=sys.stderr)
        return 0

    if args.revoke:
        # Agents' marks included: either side can retract the other's.
        known = {mk.rec: mk for mk in extract_meta(path).marks}
        out = []
        for spec in args.revoke:
            hits = [uid for uid in known if uid.startswith(spec)]
            if not hits:
                raise SystemExit(f"no live mark {spec!r} — `chsum mark --list` shows them")
            if len(hits) > 1:
                raise SystemExit(f"{spec!r} matches {len(hits)} marks — use more characters")
            out.append(f"{MARK_SENTINEL} revoke={hits[0]} | "
                       f"dropped: {_clip_line(known[hits[0]].reason, 120)}")
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
    at = (_mark_target(path, args.at) if args.at
          else _match_target(path, args.match) if args.match else "")

    print(f"{MARK_SENTINEL}{f' at={at}' if at else ''} | {reason}")
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
            return p
    where = "this project" if local else "any project"
    raise SystemExit(f"no conversation #{nth} in {where}"
                     if seen else f"no conversations found in {where}")


# ---------------------------------------------------------------------------
# Catch-up: `last --here`
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


# `is_error` also flags a tool use *you* declined — not a failure of the work.
_DECLINED_RE = re.compile(r"^the user (doesn'?t|does not) want to proceed",
                          re.IGNORECASE)
# Second signal for what `is_error` misses: a traceback whose command still
# exited 0. Anchored to line start, so output *quoting* an error isn't caught.
_FAIL_RES = [re.compile(p, re.MULTILINE) for p in (
    r"^Traceback \(most recent call last\):",
    r"^\w*(Error|Exception): \S",
)]


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
    `<bash-…>` records are excluded, or anchoring could catch up from its own footprint."""
    found = None
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "user":
            continue
        text = _typed_text(rec)
        if text and is_typed_prompt(text):
            found = (lineno, rec)
    return found


def _tool_event(part: dict) -> tuple[str, str]:
    """(kind, text) for one tool_use block."""
    name, inp = str(part.get("name") or "?"), part.get("input") or {}
    if name == "Bash" and isinstance(inp.get("command"), str):
        return "ran", _clip_line(inp["command"], 200)
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
    for src, agent in mark_sources(path):
        # id -> the command, not just its id: a failure has to be able to name
        # what failed, and by the time the result lands the tool_use is gone.
        pending: dict[str, str] = {}
        for lineno, raw in enumerate(src.read_text(errors="replace").splitlines(), start=1):
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
                continue
            ts = str(rec.get("timestamp") or "")
            live = (lineno > anchor_line) if src == path else (ts > anchor_ts)
            if until_ts and ts and ts > until_ts:
                live = False
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str) and live and rec["type"] == "assistant":
                if content.strip() and not notice_kind(content):
                    events.append(_Event(ts, agent, "said", _clip(content, 600, "clipped")))
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and live and rec["type"] == "assistant":
                    t = part.get("text", "").strip()
                    if t and not notice_kind(t):
                        events.append(_Event(ts, agent, "said", _clip(t, 600, "clipped")))
                elif part.get("type") == "tool_use":
                    if part.get("name") == "Bash":
                        cmd = (part.get("input") or {}).get("command")
                        # First line, not a flattened clip: a heredoc script
                        # squashed onto one line is 200 chars of its own source,
                        # where `python3 - <<'PY'` identifies it at a glance.
                        pending[str(part.get("id") or "")] = _clip_line(
                            cmd.strip().splitlines()[0], 120) if isinstance(cmd, str) and cmd.strip() else ""
                    if live:
                        events.append(_Event(ts, agent, *_tool_event(part)))
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
                                                 f"{cmd}\n{_fail_excerpt(out)}"))
                        else:
                            events.append(_Event(ts, agent, "output", head + out[-400:]))
    # Stable, so a record's own blocks stay in the order they were emitted.
    events.sort(key=lambda e: e.when)
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
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "user":
            continue
        ts = str(rec.get("timestamp") or "")
        text = _typed_text(rec)
        if isinstance(text, str) and is_typed_prompt(text):
            turns.append(_Turn(lineno, ts, "said", text.strip()))
            continue
        answered = _answered(rec)
        if answered:
            turns.append(_Turn(lineno, ts, "answered", answered))
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
                           ("failed", "failure")):
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
    return chunks


_KIND_LABELS = {"said": "what Claude said", "edit": "file edits",
                "ran": "commands run", "output": "command output",
                "failed": "failures (command + error)", "you": "your own turns",
                "spawn": "subagents spawned", "tool": "other tool calls"}


def _failures_section(events: list[_Event]) -> list[str]:
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
    """One chunk digest's bullets. Falls back to the preamble when a reply has
    no bullet markers at all, rather than dropping the model's only output for
    that chunk silently."""
    preamble, bullets = _timeline_bullets(text)
    return bullets or preamble


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


def _cost_rows(events: list[_Event], boundaries: list[str]):
    """`events` chunked exactly as a live run would, then sized off exactly what
    each chunk's call would send. Shared by `_dry_run_report` and the wizard's
    cost step so the two can't drift into quoting different numbers.

    Returns `(rows, by_kind)`: `rows` is one `(chunk, material_chars, called)`
    per chunk, `called` from `_chunk_activity` so "chunks called" matches what
    a real run would spend. `by_kind` aggregates only the called chunks."""
    chunks = _chunk_events(events, boundaries, _CHUNK_MAX_CHARS)
    by_kind: dict[str, list[int]] = {}
    rows: list[tuple[list[_Event], int, bool]] = []
    for chunk in chunks:
        called = _chunk_activity(chunk)
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
                    cmd: str = "last --here") -> str:
    """`--dry-run`'s answer to "what is this going to cost, and why". Prices it
    the way a live run actually spends it, via `_cost_rows`, so totals reconcile
    to what a real run would send rather than approximating it. Characters are
    the computed fact; tokens carry `~` since `_est_tokens` is chars/4."""
    rows, by_kind = _cost_rows(events, boundaries)
    called = [(chunk, chars) for chunk, chars, ok in rows if ok]
    n_called, n_total = len(called), len(rows)

    prompt_chars = len(_CHUNK_PROMPT) * n_called
    material_chars = sum(chars for _, chars in called)
    total_chars = prompt_chars + material_chars
    pct = lambda n: f"{round(100 * n / total_chars)}%" if total_chars else "0%"
    size = lambda n: f"{n:,} chars · {_fmt_tokens(round(n / 4))} tokens · {pct(n)}"

    out = ["## Dry run — nothing was sent\n",
           "*No model call was made. Every figure below is measured off the exact "
           f"text `{cmd}` would split into chunks and pipe to "
           f"`claude -p --model {HaikuSummariser.model}`, one call per chunk.*\n"]
    skipped = n_total - n_called
    out.append(f"{_plural(n_called, 'call')} would be made"
               + (f" ({_plural(skipped, 'chunk')} skipped — nothing in them but "
                  "your own turns)" if skipped else "") + ".\n")
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


def _pick_transcript(live_only: bool = True) -> pathlib.Path:
    """`last --here`'s picker — used only here; `live_transcript` itself stays
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
        return ranked[default - 1]
    if raw.isdigit() and 1 <= int(raw) <= len(ranked):
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


class _Ticker:
    """Elapsed-time status while a blocking `subprocess.run` call sits with no
    real progress to report."""

    def __init__(self, label: str):
        self._label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        start = time.monotonic()
        while not self._stop.wait(2):
            _status(f"… {self._label} ({int(time.monotonic() - start)}s)")

    def __enter__(self) -> "_Ticker":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        _clear_status()


def cmd_catchup(args) -> int:
    # Pick before the status line starts, or `_status`'s stderr writes garble the picker.
    path = _pick_transcript()
    _status("reading the live transcript…")
    meta = extract_meta(path)
    anchor = _last_prompt(path)
    if not anchor:
        _clear_status()
        raise SystemExit("nothing typed in this conversation yet — no prompt to catch up from")
    anchor_line, anchor_rec = anchor
    return _render_window(path, meta, anchor_line, str(anchor_rec.get("timestamp") or ""),
                          _typed_text(anchor_rec), "", args)


def _render_window(path: pathlib.Path, meta: Meta, anchor_line: int, anchor_ts: str,
                   prompt_text: str, until_ts: str, args, turns: list[_Turn] | None = None) -> int:
    """The document, for a window with a start and an optional end. One body
    for `last --here` and `recap` — they differ only in how the window was
    chosen. `until_ts` empty means "to now", the live case."""
    def out(text: str, **kw) -> None:
        print(_md_ansi(text) if _colour_ok() else text, **kw)

    live = not until_ts
    # `--dry-run` makes no call, so there's nothing to slice.
    interleave = bool(turns) and not live and not getattr(args, "dry_run", False)
    # Prints before the slower event walk, so the first thing on screen is what you typed.
    _clear_status()
    header = [f"# {'Catch-up' if live else 'Recap'} — {meta.title}"]
    bits = [meta.project_name, f"your prompt at {_hhmm(anchor_ts)}"]
    bits.append(f"snapshot at {datetime.now().astimezone().strftime('%H:%M')} — "
                "the transcript trails the live screen" if live
                else f"window ends {_hhmm(until_ts)}")
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
    for e in events:
        if e.kind == "edit":
            edited.append(e.text.splitlines()[0])
        elif e.kind == "ran" and _is_notable_command(e.text):
            cmds.append(e.text)
            cmd_times.setdefault(e.text, e.when)
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
        since += _bullets(edited, 12) + [""]
    if cmds:
        since.append("Commands:")
        since += _timed_bullets([(cmd_times[c], c) for c in cmds], 8) + [""]
    if agents_seen:
        desc = {r.id: r.description for r in meta.agents}
        since.append("Agents at work:")
        since += [f"- `{a}`" + (f"  {desc[a]}" if desc.get(a) else "") for a in agents_seen]
        since.append("")

    since += _failures_section(events)

    said = [e for e in events if e.kind == "said"]
    if said:
        last = said[-1]
        who = f" (agent {last.agent[:8]})" if last.agent else ""
        since.append(f"## Last thing Claude said{who} ({_hhmm(last.when)})\n")
        since.append(_quote(last.text) + "\n")

    _clear_status()
    out("\n".join(since), flush=True)

    # Built off `turns`/`live` directly, not `interleave`, so a dry run still
    # prices the chunks a real run would make.
    spine = ([_Turn(anchor_line, anchor_ts, "said", prompt_text)] + list(turns or [])
             if turns and not live else [])
    boundaries = [t.when for t in spine]

    if getattr(args, "dry_run", False):
        _clear_status()
        out(_dry_run_report(events, boundaries, "last --here" if live else "recap"))
        return 0

    # One call per chunk, run independently and in parallel — no chunk's call
    # ever sees another chunk's material or output.
    chunks = _chunk_events(events, boundaries, _CHUNK_MAX_CHARS)

    def _run_chunk(chunk: list[_Event]) -> tuple[str, dict, float, str]:
        """(bullets text, usage, seconds, error) — never raises, so one chunk's
        failure can't break `executor.map`'s order for the rest."""
        if not _chunk_activity(chunk):
            return "", {}, 0.0, ""
        summariser = HaikuSummariser()
        try:
            text = summariser.digest(_CHUNK_PROMPT, _chunk_material(chunk))
            return text, summariser.usage, summariser.seconds, ""
        except SummariserError as e:
            return "", summariser.usage, summariser.seconds, str(e)

    chunk_est = sum(_est_tokens(_CHUNK_PROMPT) + _est_tokens(_chunk_material(c))
                    for c in chunks if _chunk_activity(c))
    with _Ticker(f"haiku is writing {_plural(len(chunks), 'chunk')} of the timeline "
                 f"— {_fmt_tokens(chunk_est)} tokens total"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(chunks))) as ex:
            # `map` preserves submission order, so chunk order stays chronological.
            results = list(ex.map(_run_chunk, chunks))

    total_usage: dict = {}
    total_seconds = 0.0
    for _, usage, seconds, _err in results:
        total_seconds += seconds
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v

    # Every chunk failing degrades to verbatim sections plus a one-line notice;
    # a mix of hits and misses gives each failed gap its own notice.
    all_failed = bool(results) and all(err for *_, err in results)
    unavailable = (f"*Timeline unavailable — {results[0][3]}. Everything above "
                   "is still verbatim.*" if all_failed else "")

    sliced: list[str] = []
    written = ""
    if interleave and not all_failed:
        stamps = sorted(boundaries)
        turn_bullets: list[list[str]] = [[] for _ in spine]
        for chunk, (text, _, _, err) in zip(chunks, results):
            # Re-derives which turn this chunk belongs to via its first event's timestamp.
            idx = max(0, bisect.bisect_right(stamps, chunk[0].when) - 1)
            if err:
                turn_bullets[idx].append(
                    f"*Digest unavailable for part of this gap — {err}. The "
                    "verbatim record above still covers it.*")
            elif text:
                turn_bullets[idx].extend(_chunk_bullets(text))
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
        0: "↑↓ move · enter choose session · esc quit",
        1: "↑↓ move · enter set START · esc back to sessions",
        2: "↑↓ move · enter set END · esc back to start",
        3: "enter run the recap · esc back to end",
    }

    def __init__(self, cands: list[pathlib.Path]):
        self.cands = cands
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
        # Newest last and selected, same default as the typed picker.
        self.cursor = max(0, len(cands) - 1)

    @staticmethod
    def _session_row(p: pathlib.Path) -> tuple[str, str, str]:
        m = extract_meta(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        title = ("✎ " if m.renamed else "") + (m.title or "(untitled)")
        return (_ago(last_activity(p), mtime), m.duration or "-", title)

    # -- drawing ---------------------------------------------------------
    def _draw(self, scr) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        head = {0: "which session?", 1: "start where?", 2: "end where?",
                3: "what this will cost"}[self.step]
        scr.addnstr(0, 0, head, w - 1, curses.A_BOLD)
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
            aw = max((len(r[0]) for r in self.rows), default=3)
            dw = max((len(r[1]) for r in self.rows), default=3)
            return [f"{a:<{aw}}  {d:<{dw}}  {t}" for a, d, t in self.rows]
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
        events = _events_since(self.path, a.line, a.when, b.when)
        inner = [t for t in self.turns if a.when < t.when <= b.when]
        events += [_Event(t.when, "", "you", t.text) for t in inner]
        events.sort(key=lambda e: e.when)
        # Mirrors `_render_window`'s `spine`, so this prices the N calls a real run would make.
        boundaries = [t.when for t in [a] + inner]
        rows, by_kind = _cost_rows(events, boundaries)
        called = [(chunk, chars) for chunk, chars, ok in rows if ok]
        n_called, n_total = len(called), len(rows)
        prompt_chars = len(_CHUNK_PROMPT) * n_called
        material_chars = sum(chars for _, chars in called)
        total_chars = prompt_chars + material_chars
        est = n_called * _est_tokens(_CHUNK_PROMPT) + sum(round(c / 4) for _, c in called)
        self.cost = [
            f"turns {lo + 1}–{hi + 1}   {_hhmm(a.when)} → {_hhmm(b.when)}"
            f"   {_plural(len(events), 'event')}   {_plural(n_called, 'call')}"
            + (f" ({n_total - n_called} skipped)" if n_total > n_called else ""),
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
            elif key == curses.KEY_HOME:
                self.cursor = 0
            elif key == curses.KEY_END:
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
            picked = _Wizard(cands).run(scr)
        return ("ok", picked) if picked else ("quit", None)
    except (curses.error, OSError):
        # Falls back rather than failing the command; CHSUM_TUI_DEBUG surfaces why.
        if os.environ.get("CHSUM_TUI_DEBUG"):
            import traceback
            pathlib.Path(os.environ["CHSUM_TUI_DEBUG"]).write_text(traceback.format_exc())
        return "unavailable", None


def cmd_recap(args) -> int:
    """Any session, any range of your turns — `last --here` with both ends
    chosen instead of "since the last thing I typed, to now"."""
    verdict, picked = _run_wizard(args)
    if verdict == "quit":
        return 0  # you escaped out: nothing chosen, nothing to print
    if picked:
        path, start, end = picked
        turns = _your_turns(path)
    else:
        path = pathlib.Path(args.file) if args.file else (
            path_for_ref(args.ref) if args.ref else _pick_transcript(live_only=False))
        turns = _your_turns(path)
        if not turns:
            raise SystemExit("nothing typed in that conversation — no turns to pick from")
        start, end = _pick_range(turns, args)
    _status("reading the transcript…")
    meta = extract_meta(path)
    # The end turn bounds the window; the turns strictly between the two are
    # yours as well and belong in the record.
    inner = [t for t in turns if start.when < t.when <= end.when]
    return _render_window(path, meta, start.line, start.when, start.text,
                          end.when, args, inner)


def cmd_last(args) -> int:
    if args.here:
        return cmd_catchup(args)
    # There is no model call on this path, so there is nothing to not-make: say
    # so rather than accepting the flag and silently ignoring it.
    if getattr(args, "dry_run", False):
        raise SystemExit("--dry-run only means something with --here "
                         "(nothing else in `last` calls a model)")
    args.file = str(latest_transcript(local=not args.all, nth=args.nth))
    args.ref = None
    return cmd_context(args)


def cmd_sessions(args) -> int:
    """One line per conversation, newest first. Triage: which were real work.
    Empty sessions are listed, not hidden, and so is the one running right now
    (tagged) — `last` skips that one, since you're already in it."""
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
    print("Read one: `chsum context <ref>`   Most recent real session: `chsum last`",
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
        cwd = DIGEST_DIR.parent
        cwd.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                # JSON purely for accounting: it carries `usage`, tokens actually charged.
                [exe, "-p", "--model", self.model, "--output-format", "json",
                 "--system-prompt", system_prompt, "--tools", "", "--setting-sources", ""],
                input=material,
                capture_output=True, text=True, timeout=240, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            raise SummariserError("claude -p timed out after 240s") from None
        self.seconds = time.monotonic() - started
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
                    "(`last --here`'s timeline) is labelled as such.",
    )
    ap.add_argument("--out", type=pathlib.Path, default=DIGEST_DIR,
                    help=f"digest directory (default: {DIGEST_DIR})")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("sessions", help="one line per conversation in this project (the default)")
    p.add_argument("-n", "--limit", type=int, default=5, metavar="N",
                   help="how many to list, 0 for all (default: 5)")
    p.add_argument("--since", default=None, help="window, e.g. 7d, 24h, 2w (default: all time)")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("last", help="context for your most recent conversation")
    p.add_argument("-n", "--nth", type=int, default=1, metavar="N",
                   help="Nth most recent instead of the last (default: 1)")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.add_argument("--here", action="store_true",
                   help="the session running right now: what has happened since "
                        "your last prompt, ending in a model-written timeline")
    p.add_argument("--dry-run", action="store_true",
                   help="with --here: print the verbatim record and a size "
                        "breakdown of what would be sent, and make no model call")
    p.set_defaults(func=cmd_last)

    p = sub.add_parser("find", help="search conversations")
    p.add_argument("query", nargs="?")
    p.add_argument("--all", action="store_true", help="all workspaces (default: this one)")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--marks", action="store_true",
                   help="search what you marked with `chsum mark` (query optional)")
    for mode in ("hybrid", "semantic", "lexical", "exact"):
        p.add_argument(f"--{mode}", dest="mode", action="store_const", const=mode)
    p.set_defaults(mode="hybrid", func=cmd_find)

    p = sub.add_parser("recap", help="summarise a chosen range of one conversation")
    p.add_argument("ref", nargs="?", help="ch_... ref (default: pick a session)")
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
    p.set_defaults(func=cmd_recap)

    p = sub.add_parser("digest", help="deterministic digest of one conversation")
    p.add_argument("ref", nargs="?", help="ch_... ref from `chsum find`")
    p.add_argument("--file", help="transcript path (derives the ref)")
    p.add_argument("--stdout", action="store_true", help="print instead of writing a file")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("context", help="reload artifact for pasting back into Claude")
    p.add_argument("ref", nargs="?")
    p.add_argument("--file")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser(
        "mark", help="flag this moment as notable, for the digest to pick up",
        description="Prints a marker. Nothing is written: run through Claude Code's "
                    "`!` prefix, the harness records the run itself, so the mark lands "
                    "in the transcript where you typed it.",
    )
    p.add_argument("reason", nargs="*", help="why this matters — quoted verbatim later")
    p.add_argument("--at", metavar="ID",
                   help="mark an earlier message: a record id from --recent, or mN")
    p.add_argument("--match", metavar="TEXT",
                   help="mark the one message containing TEXT; lists candidates if "
                        "more than one matches")
    p.add_argument("--recent", nargs="?", type=int, const=20, default=None, metavar="N",
                   help="list the last N messages and tool calls (default 20), with "
                        "the record ids --at takes")
    p.add_argument("--list", action="store_true",
                   help="marks made in this conversation, with the ids --revoke takes")
    p.add_argument("--full", action="store_true",
                   help="with --list: whole reason and whole marked message, unclipped")
    p.add_argument("--revoke", nargs="+", metavar="ID",
                   help="drop marks made earlier (they stay in the transcript, "
                        "but stop counting)")
    p.add_argument("--file", help="transcript path (default: the session you're in)")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser(
        "name", help="rename a conversation to what it actually was",
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

    p = sub.add_parser("journal", help="chronological work log")
    p.add_argument("--since", default="7d", help="window, e.g. 7d, 24h, 2w")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.set_defaults(func=cmd_journal)

    # Bare `chsum` lists sessions: you usually want to pick one, and "most recent"
    # is often a dud. Anything naming a subcommand or asking for help is left alone.
    raw = list(argv) if argv is not None else sys.argv[1:]
    if not any(tok in sub.choices or tok in ("-h", "--help") for tok in raw):
        raw = ["sessions"] + raw
    args = ap.parse_args(raw)
    if args.cmd in ("digest", "context") and not args.ref and not args.file:
        ap.error(f"{args.cmd}: need a ch_... ref or --file")

    try:
        return args.func(args)
    except HistoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
