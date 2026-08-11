#!/usr/bin/env python3
"""chsum — Claude Code conversations as work logs and reload-ready context.

DETERMINISTIC: every line of output is copied verbatim from a transcript or
computed from it. Digests feed back into future sessions, where an invented
claim would become ground truth. Prose generation waits behind the `Summariser`
seam at the bottom.

Commands: sessions (default), last, find, digest, context, mark, journal.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

PROJECTS_ROOT = pathlib.Path(
    os.environ.get("CLAUDE_CONFIG_DIR", pathlib.Path.home() / ".claude")
) / "projects"
DIGEST_DIR = pathlib.Path.home() / ".claude" / "chsum" / "digests"

# ---------------------------------------------------------------------------
# ch_ ref derivation
# ---------------------------------------------------------------------------
# Reimplements claude-history's AgentConversationRef::from_parts
# (src/agent/refs.rs:31-54): length-prefixed 128-bit FNV-1a over
# ["agent-v1", project_dir_name, session_filename]. Their versioned internal, so
# anything derived is verified against the uuid they report before it's trusted.

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
    """Addressable conversations only: two levels, no agent-* sidecars.

    Mirrors claude-history's discover_agent_keys (src/agent/service.rs:477-486).
    """
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
    """Highest message ordinal — the upper bound for a full read.

    outline has two shapes: `seg m1..m38` for long conversations, bare `m1 role=…`
    lines for short ones. Handle both, or short ones silently read as empty.
    """
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
    """Parse `agent read` output into messages with their mN and ma_ anchor.

    claude-history is the text source because it has already stripped tool sludge.
    """
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
            # The ordinal is a bare positional token ("message m17 role=..."),
            # not a key=value pair, so it has to be read off directly.
            m = re.match(r"message\s+m(\d+)", line)
            cur = Message(
                n=int(m.group(1)) if m else 0,
                role=f.get("role", "?"),
                anchor=f.get("anchor", ""),
                text="",
                # The bridge from a JSONL record to a message ordinal: marks are
                # made against record uuids, but cited as mN.
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
# Harness scaffolding that appears in the user role but isn't something the user
# typed. Measured across the corpus: interrupts and task-notifications dominate.

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


# Steering turns that carry no standalone meaning. Measured on the corpus: ~31% of
# prompts in conversational sessions are these, and listed in a trail they read as
# noise ("yes", "ok, do that"). They're counted rather than shown.
_ACK_RE = re.compile(
    r"^(y(es|ep|eah|up)?|no(pe)?|ok(ay)?|sure|thanks|ta|cool|nice|good|great|perfect|"
    r"do (it|that)|go ahead|carry on|continue|next|yes please|please do|"
    r"correct|right|exactly|agreed|fine|stop|wait|hmm+)"
    r"[\s.,!?*)]*$",
    re.IGNORECASE,
)


def is_substantive(text: str) -> bool:
    """Does this prompt say anything on its own?

    "yes" read cold months later carries nothing — its meaning lived in the message
    it answered. Filtered from the trail, but counted, so it isn't silently erased.
    """
    t = text.strip()
    return len(t) >= 12 and not _ACK_RE.match(t)


# Harness-generated last words. They occupy the assistant's final turn but say
# nothing about the work, so "where I left off" would otherwise report the manner
# of death instead of the state. Matched, not guessed: each is a fixed string the
# harness emits.
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
    """Last assistant message with content, plus any notice that came after it.

    Both are returned because both are true and neither substitutes for the other:
    the notice says how the session ended, the message says where the work was.
    """
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
        """Sidecars can be missing (older sessions, pruned) or outnumber the
        visible Agent calls (an agent that spawned its own), so trust whichever
        is larger."""
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
# What a session is called is Claude Code's `ai-title` record, and it appends a
# refined one all session long — dozens per conversation, last wins. So `chsum
# name` does two things, because neither alone holds:
#
#   1. Records the name in chsum's own store, keyed by uuid. Authoritative here:
#      a live session's next `ai-title` would otherwise overwrite you within the
#      minute.
#   2. Appends one more `ai-title` record to the transcript, so /resume and every
#      other reader of the JSONL show the name too.
#
# (2) is the one exception to "chsum writes nothing to a transcript", and it is
# an *append* of a record type Claude Code appends constantly — never a rewrite
# of a line already written, so a concurrent flush has nothing to collide with.
# Neither step invents anything: the title is argv, verbatim.

NAMES_PATH = DIGEST_DIR.parent / "names.json"

_names_cache: tuple[float, dict[str, str]] | None = None


def _load_store() -> dict[str, dict]:
    """Raw store: uuid → {"title", "was"}. `was` is what Claude Code called it at
    the moment you renamed, kept so `--clear` can put that record back — otherwise
    the appended title outlives the name and /resume never reverts."""
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
    """uuid → your name for it. Cached on mtime: the listing path asks once per
    session, and 195 re-reads of the same file is the whole cost of `journal`."""
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
    """One more `ai-title` record, byte-identical in shape to Claude Code's own.

    Written as a single append of a complete line. The leading newline guard is
    for the live transcript: reading it mid-flush can find the last line without
    its terminator, and appending onto that would fuse two records into one
    unparseable line.
    """
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
# `chsum mark <reason>` writes nothing at all. Run through Claude Code's `!`
# prefix, the harness records the run for us: the sentinel below lands in the
# transcript as a `<bash-stdout>` user record, at the point in the conversation
# where it was typed, carrying the reason verbatim from argv. So there is no
# injection into a file Claude Code is concurrently appending to, no second store
# to keep in sync, and the mark inherits an mN and a durable ma_ anchor for free.
#
# Detection looks only at captured command output — a `<bash-stdout>` record from a
# `!` run, or a Bash tool_result when Claude ran it. So a digest that quotes a mark
# and gets pasted into a later session doesn't read back as a mark of that session.

MARK_SENTINEL = "⚑ chsum-mark v1"
# Line-anchored, allowing only the harness's own wrapper in front: unanchored, a
# `grep chsum-mark chsum.py` in some future session would match the pattern below
# in this file's own source and forge a mark out of it.
_MARK_RE = re.compile(
    r"^(?:<bash-stdout>)?⚑ chsum-mark v1(?P<fields>[^|]*)\|(?P<reason>.*)", re.MULTILINE
)
# The harness wraps captured output, and a `!` run's stderr tags follow stdout's in
# the same record, so the reason ends at the first closing tag — not the last.
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
    can be reconciled: either side may retract a mark the other made.

    Its own pass rather than part of `extract_meta` because line numbers are the
    only bridge back to mN, and `_records` doesn't carry them. Cost on the listing
    path is one substring scan of the file — sessions with no marks stop there.
    """
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
                # A retraction is itself just another line of output: nothing was
                # written to take back, so the record stays and the mark drops.
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
    """Point a bare `chsum mark` at the message it followed.

    Its own record is command output, which says nothing about what was being
    marked — you marked what was on the screen. So the target is the last thing
    said before it, skipping the mark's own plumbing.

    "Before" is by timestamp once sidecars are in play: what was on screen while an
    agent ran was written to the sidecar, and line numbers of two files don't
    order against each other. Marks with no timestamp of their own fall back to
    line order in the transcript, which is what they had before.
    """
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
    """Time worked, not wall-clock. Sessions resume days later, so first→last
    overstates badly (one reads as 92h). Gaps over IDLE_GAP_SECONDS are excluded."""
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
    # Yours wins over Claude Code's, and over any later `ai-title` it appends —
    # that is the point of keeping a store as well as appending one.
    named = load_names().get(meta.uuid, "")
    meta.renamed = bool(named)
    meta.title = named or meta.ai_title or "(untitled)"
    own_edits = set(edited)

    # Fold subagent tool use into the parent: a session that delegated everything
    # would otherwise read as no activity. Prompts stay parent-only.
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

    # Marks fold in like edits and commands do — a mark made by the agent you sent
    # to do the work is a mark on the session. Revocations are pooled first, so
    # either side can retract the other's: the agent is doing the session's work,
    # not keeping its own books.
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


# Look-only commands. One session logged 118, nearly all greps — they bury the
# few that did something.
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
    are tool_results; the rest is harness scaffolding (interrupts, notifications)."""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
    else:
        return False
    return any(is_real_prompt(t) for t in texts)


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


def _clip(text: str, limit: int, hint: str = "read the anchor") -> str:
    # The hint is a parameter because agent digests have no anchors — pointing at
    # one invites the reader to invent a ref that claude-history will reject.
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [+{len(text) - limit} chars, {hint}]"


def _dim(text: str) -> str:
    """Grey for quoted transcript text, so a reason and the message it marks don't
    read as one voice. Off unless stdout is a terminal: `!` runs get captured, and
    an escape sequence stored in a transcript is there forever. FORCE_COLOR turns
    it on anyway, NO_COLOR always wins."""
    if os.environ.get("NO_COLOR"):
        return text
    if not (sys.stdout.isatty() or os.environ.get("FORCE_COLOR")):
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
    """Text messages straight from a transcript file. Sidecars only.

    claude-history has no per-agent ref — `--subagents` inlines agent messages into
    the parent read untagged — so these are parsed here. Anchors stay empty: `ma_`
    values are claude-history's to mint, and a fabricated one is worse than none.
    """
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
    """Last turn long enough to carry a result. None if `exclude` already is one.

    Speech wins over thinking; thinking is the fallback because an agent killed
    mid-run may never have said anything substantial out loud.
    """
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
    """Last Bash command in a transcript and the tail of what it printed.

    An agent that dies mid-run leaves its state in test output, not in speech.
    Verbatim, and the tail rather than the head: the verdict line is at the end.
    """
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

    # An agent killed mid-run often ends on tool narration ("let me read X"), which
    # says nothing about what it found. The last long turn usually does, and it is
    # still verbatim — a different message, not a summary of one.
    meaty = _last_meaty(msgs, exclude=final)
    if meaty:
        what = ("Its last reasoning block (thinking, not spoken)"
                if meaty.role == "thinking" else "Its last substantial message")
        parts.append(f"## {what}\n")
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
    typed = [m for m in msgs if m.role == "user" and is_real_prompt(m.text)]
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
    """Give a mark its mN and anchor only when a message starts on the very line
    it points at.

    Nothing looser survives contact: a read of a live transcript comes back sparse
    — measured at 38 messages spanning ordinals up to m327 — so "the last message
    starting before this line" attributes a mark to whatever survived the gap. One
    mark landed on an `ok` three hundred messages away. Unplaced marks fall back to
    their record id, and the quote carries the content either way.
    """
    target = mark.at_line or mark.line
    # An agent mark's lines are its sidecar's, and claude-history has no per-agent
    # ref to number them against — so it gets its agent id instead of an ordinal.
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
    """Anchors safe to publish: present, unique, one per message.

    They are content-addressed, so byte-identical messages share one and
    `read --anchor` fails with ambiguous-ref (measured: "[Request interrupted by
    user]" collides). Dropped rather than emitted — mN alone still works.
    """
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
        if m.role == "user" and is_real_prompt(m.text):
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
    `chsum mark --recent`, or an mN, which needs claude-history to place it.

    A uuid is looked for in the sidecars too, because `--recent` lists them; an mN
    is the transcript's own numbering and stays parent-only.
    """
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
    its sidecars, each with the agent id to label it by.

    Delegated work is the session's work — while an agent is running, what is on
    screen is being written to a sidecar, not the transcript, so a haystack of the
    transcript alone can't see the thing you are asking to mark.
    """
    return [(path, "")] + [(s, s.stem.removeprefix("agent-"))
                           for s in subagent_transcripts(path)]


def _recent_actions(path: pathlib.Path, limit: int,
                    skip_machinery: bool = False) -> list[tuple[int, dict, str, str, str, str]]:
    """(line, record, label, full text, kind, agent) for the last `limit` things
    that happened: messages either side typed, and every tool call, in order.
    `limit=0` is all. Sidecars are folded in, so a running agent's work is markable.

    `skip_machinery` drops `chsum mark`'s own footprint — for `--match`, whose
    haystack it would otherwise poison. `--recent` keeps it: that view is a plain
    account of what happened, and a `!` run is a thing that happened.

    The label is one line, for listing; the full text is what `--match` searches,
    so a phrase buried in the middle of a long message is still findable.

    Straight from the JSONL, not claude-history: this has to be right about what
    just happened, and their read of a live transcript lags behind the file.
    Tool *results* are left out — you mark the action, and a mark resolves to the
    message that contains it either way.
    """
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
    """Down to words and spaces, for matching a half-remembered phrase.

    Nobody retypes `Currently **no** —` when they mean "currently no", so case,
    markdown, and punctuation are all noise here. Only matching is folded; every
    mark still quotes the original bytes.
    """
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


_MARK_INPUT_RE = re.compile(r"\s*<bash-input>\s*chsum\s+mark\b")
_BASH_OUT_RE = re.compile(r"\s*<bash-(?:stdout|stderr)>")


class _Machinery:
    """`chsum mark`'s own footprint, tracked in file order.

    Excluded from matching because the tool_use record is written *before* the
    command runs: without this, every `--match` finds itself, and past runs stay
    in the haystack forever.

    A `!` run lands as two records — the `<bash-input>`, then whatever it printed
    — and only a *successful* mark's output carries the sentinel; a failure's
    output quotes the phrase you searched for back at you. So captured output is
    judged by the command above it, not by what it says. Same for the ambiguity
    list, `--recent`, and `--list`, which quote other messages verbatim.

    Invocations only, not talk about them. A command is machinery wherever `chsum
    mark` appears in it (they get chained); a message is machinery only if it *is*
    a `!` run, that run's output, or carries the sentinel. Match on prose and a
    conversation about this feature becomes unmarkable.

    One instance per file, `sees` called once per record in file order: adjacency
    is a fact about the file, and sorting by timestamp interleaves the sidecars.
    """

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
        # The session's marks, not the transcript's: an agent's are the session's
        # too, and `--list` showing fewer than the digest counts reads as a bug in
        # whichever one you check second. extract_meta already pools revocations
        # across the boundary, so this is that one definition, not a second.
        marks = extract_meta(path).marks
        if not marks:
            print("nothing marked in this conversation", file=sys.stderr)
            return 1
        # Capped well under a wide terminal: this output is usually read back
        # inside Claude Code, which re-wraps at its own width, and a fold it
        # chooses ignores the indent.
        cols = max(40, min(shutil.get_terminal_size((100, 24)).columns, 88))
        if args.full:
            # The stored quote is only the marked message's first line, so the rest
            # comes back off disk here rather than being carried around for a view
            # nobody usually asks for.
            # Keyed by file: a mark can point into a sidecar, whose line numbers
            # mean nothing in the transcript.
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
        # Two lines each: a reason and the message it marks are both prose, and on
        # one line the long one eats the other.
        # Width off the rendered strings, not the limit: the clip marker is two
        # more characters, and sizing to the limit knocks those rows out of line.
        # Whose mark it is, in the id column: the reason rarely says, and a mark you
        # don't remember making is one an agent made for you.
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
    """A ref (or a uuid, or a prefix of either) to its transcript.

    Matched against locally derived refs rather than asked of claude-history: the
    refs `chsum sessions` printed came from the same function, so a paste from the
    listing resolves here by construction, and renaming costs no subprocess.
    """
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
    """Rename a conversation to what it actually was.

    The ref is optional and leads: `chsum name ch_… "…"` renames that one, a bare
    `chsum name "…"` renames the session you're in. Told apart by the `ch_` prefix
    rather than by a flag, because the ref you have was copied from the listing
    one line above.
    """
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
        # Reverting the store is not enough: the ai-title we appended is the last
        # one in the file, so /resume would keep showing a name chsum no longer
        # knows. Put Claude Code's own title back the same way — appended, never
        # deleted, so both records stay and the later one wins.
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
    """The conversation's own last timestamp, read from the tail of the file.

    Not `st_mtime`: anything that touches a transcript without writing to it — a
    backup, an indexer — rewrites the mtime, and `last` then returns whatever was
    touched most recently. Measured once with four transcripts stamped to the same
    minute and `last` two days behind `sessions`, which orders by this instead.

    Tail-read rather than `extract_meta`, which parses every record: `--all` is
    225 transcripts here, 5.4s to parse and 0.05s to tail. Returns "" if the last
    records carry no timestamp; the caller falls back to mtime for those.
    """
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

    Sessions that went nowhere are skipped (`chsum sessions` lists those), as is
    the session doing the running when invoked from inside Claude Code.
    """
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


def cmd_last(args) -> int:
    args.file = str(latest_transcript(local=not args.all, nth=args.nth))
    args.ref = None
    return cmd_context(args)


def cmd_sessions(args) -> int:
    """One line per conversation, newest first. Triage: which were real work.

    Empty ones are listed, not hidden — knowing a session was a dead end is the
    answer to "where did that work go". So is the one running right now: it is
    tagged, not skipped, because a listing that silently omits today's work reads
    as work that never happened. `last` still skips it — you are already in it.
    """
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
        # Tagged, because its numbers are a snapshot: Claude Code is still
        # appending, so counts and duration are behind by however much of the
        # conversation has not been flushed yet.
        here = " · this session (in progress)" if m.path.stem == live else ""
        # Named, not just counted: "3 agents" is the least useful thing to say
        # about a session that delegated its work. Ids in full — `chsum context
        # <ref>/<id>` matches exactly, so a clipped one wouldn't resolve.
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
            if m.edited:
                print(f"Changed {len(m.edited)} file(s): "
                      + ", ".join(f"`{f}`" for f in m.edited[:6])
                      + (f" +{len(m.edited) - 6} more" if len(m.edited) > 6 else ""))
                print()
            print(f"`chsum context {ch_ref_for_path(m.path)}`\n")
    return 0


# ---------------------------------------------------------------------------
# Summariser seam — deliberately empty for now
# ---------------------------------------------------------------------------


class Summariser:
    """Where prose generation plugs in. Nothing above needs a model, and nothing
    above should change when one arrives.

    A backend gets the extracted material (intent trail, last exchange), never the
    raw transcript, and its output is additive — layered on top of the verbatim
    record so a wrong sentence can be checked against the quotes beneath it.
    """

    def summarise(self, meta: Meta, msgs: list[Message]) -> str:
        raise NotImplementedError("no summariser backend configured")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="chsum",
        description="Work logs and reload-ready context from Claude Code conversations. "
                    "Fully deterministic: no model, nothing invented.",
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
