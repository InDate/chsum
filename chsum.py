#!/usr/bin/env python3
"""chsum — turn Claude Code conversations into work logs and reload-ready context.

Everything here is DETERMINISTIC. No model is involved, so nothing can be
hallucinated: every line of output is either copied verbatim from a transcript
or computed from it. That property is the whole point — a digest that feeds
back into a future Claude session must not contain invented claims.

The load-bearing insight: your own prompts already are a faithful record of what
you were trying to do. Extracting them in order gives a real intent trail for
free, which is most of what a summary would have said anyway.

Prose generation (a TL;DR, a narrative) is the one thing this can't do without a
model. That slots in behind the `Summariser` seam at the bottom of this file —
Haiku first to benchmark quality, local MLX after. Nothing above that seam
changes when it lands.

Commands:
  find     search conversations
  digest   deterministic digest of one conversation
  context  reload artifact: facts + intent trail + last exchange + anchor map
  journal  chronological work log across a time window
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
# (src/agent/refs.rs:31-54, 424-435): a length-prefixed 128-bit FNV-1a over
# ["agent-v1", project_dir_name, session_filename].
#
# This is a versioned internal of another tool, so anything derived here is
# verified against the uuid claude-history reports before it is trusted.

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
    """The addressable conversations: two levels only, no agent-* sidecars.

    Mirrors claude-history's discover_agent_keys (src/agent/service.rs:477-486).
    subagents/agent-*.jsonl are reachable via --subagents when reading, but are
    not conversations in their own right.
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
    """Highest message ordinal, i.e. the upper bound for a full read.

    outline has two shapes: `seg m1..m38 ...` ranges for long conversations, and
    bare per-message lines (`m1 role=user ...`) for short ones. Handle both, or
    short conversations silently read as empty.
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


def read_messages(ref: str, start: int = 1, end: int | None = None) -> list[Message]:
    """Parse `agent read` output into messages carrying their mN and ma_ anchor.

    claude-history has already stripped tool sludge and normalised the text, which
    is exactly why it's the text source rather than the raw JSONL.
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

    A digest read cold, months later, gets nothing from "yes" — the meaning lived in
    the message it was answering. Short affirmations and bare acknowledgements are
    filtered out of the trail; the count is still reported so the back-and-forth
    isn't silently erased.
    """
    t = text.strip()
    return len(t) >= 12 and not _ACK_RE.match(t)


# ---------------------------------------------------------------------------
# Deterministic metadata, straight from the transcript
# ---------------------------------------------------------------------------

_FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
_READ_TOOLS = {"Read"}
_AGENT_TOOLS = {"Agent", "Task"}  # Task is the older name for the same thing


@dataclass
class AgentRun:
    """One subagent the session spawned, from its sidecar transcript.

    Held per-agent as well as merged into the parent so a session digest can name
    what was delegated in one line each, and hand out an address for the rest.
    """
    id: str = ""  # sidecar stem minus the agent- prefix; the address
    agent_type: str = ""
    model: str = ""
    description: str = ""  # the task line from the Agent call
    spawn_depth: int = 1
    duration: str = ""
    edited: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    path: pathlib.Path | None = None


@dataclass
class Meta:
    uuid: str = ""
    title: str = "(untitled)"
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


IDLE_GAP_SECONDS = 30 * 60


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def active_seconds(stamps: list[str]) -> int:
    """Time actually spent working, not wall-clock from first record to last.

    Sessions get resumed hours or days later, so first→last badly overstates
    effort (the corpus has a session reading as "92h16m"). Gaps longer than
    IDLE_GAP_SECONDS are treated as "walked away" and excluded.
    """
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
    return run


def extract_meta(path: pathlib.Path) -> Meta:
    meta = Meta(uuid=path.stem, path=path)
    stamps, edited, read, cmds = [], [], [], []
    for rec in _records(path):
        if rec.get("timestamp"):
            stamps.append(rec["timestamp"])
        if rec.get("type") == "ai-title" and rec.get("aiTitle"):
            meta.title = rec["aiTitle"]  # refined over the session; last wins
        if not meta.project and rec.get("cwd"):
            meta.project = rec["cwd"]
        if rec.get("gitBranch"):
            meta.branch = rec["gitBranch"]
        if rec.get("type") in ("user", "assistant"):
            meta.records += 1
            meta.spawned += _collect_tools(rec, edited, read, cmds)
        if rec.get("type") == "user" and _is_typed_prompt(rec):
            meta.prompts += 1
    own_edits = set(edited)

    # A delegated edit is still an edit the session made, so fold the subagents'
    # tool use into the parent's totals — otherwise a session that handed the
    # work to agents reads as no activity. Prompts stay parent-only: nobody
    # typed a subagent's instructions.
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
    # Project-relative only once the cwd is known, which is why it happens here
    # rather than in extract_agent.
    for run in meta.agents:
        run.edited = keep(run.edited)
    meta.agent_only = set(meta.edited) - set(keep(own_edits))
    return meta


# Scratch space and agent bookkeeping. Real work, but not changes to the project,
# and they crowd out the files that matter in a work log.
_NON_PROJECT_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/")
_NON_PROJECT_PARTS = ("/scratchpad/", "/.claude/plans/", "/.claude/projects/")


# Commands that only look at things. One session logged 118 commands, almost all
# greps and heads — listing them buries the few that actually did something.
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
    """A user record carrying text the human actually wrote.

    Most user-role records are tool_result payloads; the rest can be harness
    scaffolding (interrupts, notifications, slash-command echoes).
    """
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


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [+{len(text) - limit} chars, read the anchor]"


def _quote(text: str) -> str:
    """Render transcript text as a blockquote.

    Necessary, not decorative: quoted messages routinely contain their own markdown
    headings, and pasted verbatim those become sections of *this* document — an
    assistant reply containing "## Summary" silently forges a digest section.
    Blockquoting neutralises that, and correctly marks the text as not ours.
    """
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
    if meta.agent_count:
        lines.append(f"subagents: {meta.agent_count}  # their edits are counted above")
    lines += ["generated_by: chsum (deterministic extraction, no model)", "---"]
    return "\n".join(lines)


def messages_from_jsonl(path: pathlib.Path) -> list[Message]:
    """Text messages straight from a transcript file.

    Only used for subagent sidecars. claude-history is the text source everywhere
    else because it strips tool sludge for us, but it has no per-agent ref —
    `--subagents` inlines agent messages into the parent read with nothing saying
    which agent produced them — so sidecars have to be parsed here. Anchors stay
    empty: `ma_` values are claude-history's to mint, and a made-up one that
    doesn't resolve is worse than none.
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
        else:
            continue
        text = "\n".join(t for t in texts if t.strip()).strip()
        if text:
            msgs.append(Message(n=len(msgs) + 1, role=role, anchor="", text=text))
    return msgs


def render_agent_digest(meta: Meta, parent_ref: str, run: AgentRun) -> str:
    """One subagent's work. Same shape as a session digest, minus the intent trail:
    an agent gets one instruction, so 'what I asked for' is a single block."""
    msgs = messages_from_jsonl(run.path) if run.path else []
    lines = ["---", f"ref: {parent_ref}/{run.id}", f"parent: {parent_ref}",
             f"agent: {run.agent_type or 'agent'}"]
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

    parts.append("## Task\n")
    task = next((m.text for m in msgs if m.role == "user"), "")
    parts.append(_quote(_clip(task, 900)) + "\n" if task
                 else "*No instruction recorded.*\n")

    if run.edited:
        parts.append("## Files changed\n")
        parts += _bullets(run.edited, 30) + [""]

    if run.commands:
        parts.append("## Commands run\n")
        parts += _bullets(run.commands, 15) + [""]

    # Not "final report": an agent that was interrupted or steered mid-run ends on
    # whatever it happened to be saying, and calling that a conclusion would be a
    # claim the transcript doesn't support.
    parts.append("## Last thing it said\n")
    final = next((m.text for m in reversed(msgs) if m.role == "assistant"), "")
    parts.append(_quote(_clip(final, 1800)) + "\n" if final
                 else "*Nothing recorded.*\n")

    parts.append("## Drill down\n")
    parts.append(f"Full sidecar: `{run.path}`\n")
    parts.append("Inlined into the parent read (untagged, all agents at once): "
                 f"`claude-history agent read {parent_ref}:mN..mN --subagents --no-budget`\n")
    return "\n".join(parts).rstrip() + "\n"


def render_digest(meta: Meta, ref: str, msgs: list[Message], *,
                  prompt_clip: int = 400, max_prompts: int = 40) -> str:
    typed = [m for m in msgs if m.role == "user" and is_real_prompt(m.text)]
    prompts = [m for m in typed if is_substantive(m.text)]
    steering = len(typed) - len(prompts)
    parts = [frontmatter(meta, ref), ""]

    parts.append(f"# {meta.title}\n")
    when = f"{meta.date} · {meta.duration}" if meta.duration else meta.date
    parts.append(f"*{when} · {meta.project_name}"
                 + (f" · `{meta.branch}`" if meta.branch else "") + "*\n")

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
        # Marked, not separated: it's one session's work either way, but a file you
        # never touched yourself is worth knowing about before you go looking for
        # the turn where you changed it.
        shown_files = meta.edited[:30]
        parts += [f"- `{f}`" + ("  (agent)" if f in meta.agent_only else "")
                  for f in shown_files]
        if len(meta.edited) > len(shown_files):
            parts.append(f"- …and {len(meta.edited) - len(shown_files)} more")
        parts.append("")

    if meta.agents:
        parts.append("## Delegated\n")
        for run in meta.agents[:12]:
            bits = [b for b in (f"{run.agent_type or 'agent'}"
                                + (f"/{run.model}" if run.model else ""),
                                run.duration,
                                f"{_plural(len(run.edited), 'file')}, "
                                f"{_plural(len(run.commands), 'command')}",
                                f"depth {run.spawn_depth}" if run.spawn_depth > 1 else "") if b]
            parts.append(f"- `{run.id}`  {' · '.join(bits)}")
            if run.description:
                parts.append(f"  {run.description}")
        if len(meta.agents) > 12:
            parts.append(f"- …and {len(meta.agents) - 12} more")
        parts.append("")
        parts.append(f"One agent's own digest: `chsum context {ref}/<id>`\n")

    if meta.commands:
        parts.append("## Commands run\n")
        parts += _bullets(meta.commands, 15) + [""]

    parts.append("## Where I left off\n")
    tail = _last_exchange(msgs)
    if tail:
        # Deliberately not labelled as an exchange: these are found by two separate
        # backward scans and can be far apart, so the reply usually is not answering
        # the prompt above it.
        for m in tail:
            label = ("Last thing I asked" if m.role == "user"
                     else "Last thing Claude said")
            parts.append(f"**{label}** (m{m.n})\n")
            parts.append(_quote(_clip(m.text, 900)) + "\n")
    else:
        parts.append("*Nothing recorded.*\n")

    parts.append("## Drill down\n")
    parts.append(f"Read any message: `claude-history agent read {ref}:mN..mN --no-budget`\n")
    cited = _citable_anchors(msgs, prompts[:max_prompts] + (tail or []))
    if cited:
        parts.append("Durable anchors (survive renumbering if the transcript changes):\n")
        parts += [f"- m{n} → `{a}`" for n, a in cited[:20]] + [""]
    return "\n".join(parts).rstrip() + "\n"


def _citable_anchors(all_msgs: list[Message], cited: list[Message]) -> list[tuple[int, str]]:
    """Anchors safe to publish: present, unique, one entry per message.

    Anchors are content-addressed, so two messages with byte-identical text share
    one anchor and `read --anchor` then fails with ambiguous-ref. Verified against
    the corpus: repeated harness lines like "[Request interrupted by user]" collide.
    Ambiguous anchors are dropped rather than emitted — an mN alone is still useful,
    but a citation that errors (or worse, silently resolves elsewhere) is not.
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
    for m in reversed(msgs):
        if m.role == "assistant" and m.text.strip():
            out.append(m)
            break
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


def cmd_find(args) -> int:
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
        # The agent ref is chsum's own address, not a claude-history one: pointing
        # a reader at `agent read ch_…/a38…` would just fail.
        print("     the sidecar named under Drill down. -->")
    else:
        print(f"     the transcript: claude-history agent read {ref}:mN..mN --no-budget -->")
    print()
    sys.stdout.write(md)
    return 0


def latest_transcript(local: bool = True, nth: int = 1) -> pathlib.Path:
    """The nth-most-recent real conversation, newest first.

    Ordered by last activity, not filename: a resumed session is "last" if you
    touched it last. Transcripts with no activity (aborted, tool-only, or a
    single prompt that went nowhere) aren't conversations you had, so they're
    skipped — `chsum sessions` lists those.

    Run from inside Claude Code, the newest transcript is the session doing the
    running — excluded, since "the last conversation" then means the one before.
    """
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    cands = [p for p in transcripts(local=local) if p.stem != live]
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
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
    """One line per conversation in this project, newest first.

    The point is triage: which sessions were real work and which were a typo you
    abandoned. Empty ones are listed rather than hidden — knowing a session was a
    dead end is the answer to "where did that work go", and silently dropping it
    just makes you look for it twice.
    """
    cutoff = _parse_since(args.since) if args.since else None
    live = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    metas = []
    for p in transcripts(local=not args.all):
        if p.stem == live:
            continue
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

    rows = []
    for m in shown:
        rows.append((
            ch_ref_for_path(m.path),
            (m.ended or m.started or "")[:10] or "??????????",
            m.duration or "-",
            str(m.prompts),
            str(len(m.edited)),
            str(m.agent_count) if m.agent_count else "-",
            "" if _has_activity(m) else "empty",
            m.title,
        ))
    heads = ("ref", "date", "dur", "prompts", "files", "agents", "", "title")
    widths = [max(len(r[i]) for r in (*rows, heads)) for i in range(len(heads) - 1)]
    fmt = lambda r: "  ".join(
        [f"{c:<{w}}" for c, w in zip(r, widths)] + [r[-1]]
    ).rstrip()
    print(fmt(heads))
    for r in rows:
        print(fmt(r))
    print("\nRead one: `chsum context <ref>`   Most recent real session: `chsum last`")
    return 0


def _has_activity(m: Meta) -> bool:
    """A session counts as activity if you drove it somewhere.

    Prompts alone don't qualify — a single prompt answered with "what do you
    mean?" is exactly the session this listing exists to let you skip past.
    Delegated work counts: `edited` and `commands` already include the
    subagents', and spawning one at all is more than a dead end.
    """
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
        # mtime is a cheap superset filter; the authoritative test is when the
        # work happened, since a resumed old session has a recent mtime.
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

    # Group by last activity, not first: a resumed session belongs to the day you
    # last worked on it, which is what "what did I do this week" is asking.
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
        pretty = day
        try:
            pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
        except ValueError:
            pass
        print(f"## {pretty}\n")
        for m in sessions:
            bits = [b for b in (m.duration, m.project_name,
                                f"`{m.branch}`" if m.branch else "",
                                f"resumed from {m.date}"
                                if m.resumed and m.date != (m.ended or "")[:10] else "") if b]
            print(f"### {m.title}")
            print(f"*{' · '.join(bits)}*\n")
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
    """Where prose generation will plug in.

    Nothing above this line needs a model, and nothing above it should change when
    one arrives. The planned order is Haiku first (to establish what good output
    looks like and what it costs), then a local MLX backend measured against it.

    A backend receives the already-extracted, already-denoised material — the
    intent trail and last exchange — never the raw transcript. Its output is
    additive: a TL;DR and narrative layered on top of the verbatim record, never
    replacing it, so a wrong sentence can always be checked against the quotes
    sitting directly beneath it.
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
    p.add_argument("-n", "--limit", type=int, default=25, metavar="N",
                   help="how many to list, 0 for all (default: 25)")
    p.add_argument("--since", default=None, help="window, e.g. 7d, 24h, 2w (default: all time)")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("last", help="context for your most recent conversation")
    p.add_argument("-n", "--nth", type=int, default=1, metavar="N",
                   help="Nth most recent instead of the last (default: 1)")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.set_defaults(func=cmd_last)

    p = sub.add_parser("find", help="search conversations")
    p.add_argument("query")
    p.add_argument("--all", action="store_true", help="all workspaces (default: this one)")
    p.add_argument("--top", type=int, default=8)
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

    p = sub.add_parser("journal", help="chronological work log")
    p.add_argument("--since", default="7d", help="window, e.g. 7d, 24h, 2w")
    p.add_argument("--all", action="store_true", help="all projects (default: this one)")
    p.set_defaults(func=cmd_journal)

    # Bare `chsum` lists the project's sessions: you nearly always want to pick
    # one, not have the most recent dumped at you — and "most recent" is often a
    # session you abandoned after one prompt. Anything naming a real subcommand
    # or asking for help is left alone.
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
