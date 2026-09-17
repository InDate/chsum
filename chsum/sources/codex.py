"""Codex CLI sessions, under `~/.codex/sessions`.

A rollout is one JSON object per line, wrapping its content in a `payload` and
naming the kind at the top level. Five properties decide the mapping:

- A reply is recorded twice, as `response_item/message` and again inside
  `event_msg/item_completed`. Only `response_item` is read; both would count
  every reply twice.
- `response_item/reasoning` carries `encrypted_content`, sealed server-side
  with no key on this machine, and a null `content`. The blob is dropped; the
  plaintext `summary`, present on roughly a third, becomes a thinking block —
  the row chsum already skips by name for Claude Code.
- Shell runs arrive as `exec_command` with JSON arguments or as a custom `exec`
  whose JavaScript embeds `tools.exec_command({...})`. Both become `Bash`.
- `apply_patch` carries a patch naming each file it touches, and emits one call
  per file: `Write` for an added file, `Edit` for one changed or removed.
- Approval requests, environment context and transcript deltas are written as
  the user. They carry `sourceToolUseID`, which chsum reads as loaded rather
  than typed, so they stay in the transcript and count as no turn.

An imported thread carries one timestamp on every record, stamped when the
import landed, so its duration reads `0s` however many turns it holds — 46 of
the 102 rollouts measured here.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from typing import Iterator

from . import Source, register

CODEX_ROOT = pathlib.Path(
    os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")
) / "sessions"

# Openings of a user record the Codex harness writes on the user's behalf. Each
# is a fixed string at the start of the text, matched rather than guessed, so a
# turn that merely quotes one is not discarded.
_INJECTED_OPENINGS = (
    ">>> APPROVAL REQUEST START",
    ">>> TRANSCRIPT DELTA START",
    ">>> TRANSCRIPT START",
    "The following is the Codex agent history",
    "The Codex agent has requested",
    "Assess the exact planned action",
    "Planned action JSON:",
    "Reviewed Codex session id:",
    "<no retained transcript delta",
    # The project instructions file, loaded into the user's side of the
    # conversation, as Claude Code's `<system-reminder>` is. The attachment
    # manifest `# Files mentioned by the user:` is absent by design: it shares
    # its part with the request typed beside it.
    "# AGENTS.md instructions",
)

# A harness envelope: a user record opening with `<tag>` and closing it. The
# sandbox state, the plugin and skill lists and an interrupt notice all arrive
# this way. The shape is the test rather than a list of names, which would have
# to grow with each Codex release; all 17 tags measured close themselves.
_ENVELOPE_RE = re.compile(r"^\s*<([a-zA-Z_][a-zA-Z_ ]*)>")

# The answer to `request_user_input`, which Codex writes back as a user record
# wrapping JSON. Claude Code carries the same exchange in
# `toolUseResult.answers`, so the answers are lifted into that field and the
# record reads as an answered question rather than a wall of markup.
_QUESTION_REPLY_TAG = "send_user_message_question_reply"

# Voice and delegated input: `<input>` holds the words the user said, so the
# turn survives its envelope. The sibling `<realtime_conversation>` carries
# instructions rather than words and is left to the envelope rule.
_DELEGATION_INPUT_RE = re.compile(
    r"<realtime_delegation>.*?<input>(.*?)</input>", re.DOTALL)

# Codex tool name to the Claude Code tool whose shape the readers already
# parse. A name absent here keeps its own spelling and passes through as a tool
# call the readers count but do not classify, which is what an MCP call is.
_SHELL_TOOLS = ("exec_command", "shell", "exec", "local_shell")
_AGENT_TOOLS = ("spawn_agent",)
_IMAGE_TOOLS = ("view_image",)

# `tools.exec_command({...})` inside the JavaScript an `exec` call carries. The
# brace run is matched lazily so the first complete argument object is taken.
_EMBEDDED_EXEC_RE = re.compile(r"exec_command\(\s*(\{.*?\})\s*\)", re.DOTALL)

# `*** Add File: path`, and the two verbs beside it, one per file in a patch.
_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE)


def discover() -> list[pathlib.Path]:
    """Every rollout in the tree, walked recursively: Codex files a session
    under the date it started, not under its project.

    Recent Codex versions rewrite a rollout older than seven days as
    `.jsonl.zst`. Those are left alone — a zstd decoder is a dependency outside
    the standard library.
    """
    if not CODEX_ROOT.is_dir():
        return []
    return sorted(CODEX_ROOT.glob("**/rollout-*.jsonl"))


def session_cwd(path: pathlib.Path) -> str:
    """The directory a rollout ran in, from `session_meta` at its head. The
    scan stops at 50 records, so a rollout carrying none costs a head."""
    try:
        with path.open(errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 50:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                payload = rec.get("payload")
                if rec.get("type") == "session_meta" and isinstance(payload, dict):
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        pass
    return ""


def _session_id(path: pathlib.Path) -> str:
    """The id the filename carries after the timestamp:
    `rollout-2026-08-02T18-00-28-019fc1ea-….jsonl` → `019fc1ea-…`."""
    stem = path.stem
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 5 else stem


def _uuid(session: str, ordinal: int) -> str:
    """A record's identity, from the session and its position rather than drawn
    fresh: a note names this id, and a re-translation has to reproduce it."""
    digest = hashlib.sha1(f"{session}:{ordinal}".encode()).hexdigest()
    return (f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}"
            f"-{digest[16:20]}-{digest[20:32]}")


def _text_parts(content) -> list[str]:
    """The text of each content part, kept apart: a record holds the project
    instructions in one and the environment in the next, and the two joined read
    as one typed message."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("input_text", "output_text", "text", "summary_text"):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                out.append(text)
    return out


def _text_of(content) -> str:
    """The text of a Codex content list. `input_text` and `output_text` carry
    it; an `encrypted_content` part carries a sealed blob and is dropped."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("input_text", "output_text", "text", "summary_text"):
            text = part.get("text")
            if isinstance(text, str):
                out.append(text)
    return "\n".join(out)


def _command_of(payload: dict) -> str:
    """The shell command a tool call runs, from either shape Codex writes it
    in: JSON arguments on a `function_call`, or a `tools.exec_command({...})`
    call inside the JavaScript on a custom `exec`."""
    raw = payload.get("arguments")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            args = {}
        if isinstance(args, dict):
            cmd = args.get("cmd") or args.get("command")
            if isinstance(cmd, list):
                return " ".join(str(c) for c in cmd)
            if isinstance(cmd, str):
                return cmd
    body = payload.get("input")
    if isinstance(body, str):
        match = _EMBEDDED_EXEC_RE.search(body)
        if match:
            try:
                args = json.loads(match.group(1))
            except json.JSONDecodeError:
                return body
            cmd = args.get("cmd") or args.get("command") if isinstance(args, dict) else None
            if isinstance(cmd, list):
                return " ".join(str(c) for c in cmd)
            if isinstance(cmd, str):
                return cmd
        return body
    return ""


def _patch_files(body: str) -> list[tuple[str, str]]:
    """Each file a patch touches, as (tool, path). `Add` writes a file that was
    absent, so it maps to `Write`; `Update` and `Delete` change one that was
    present, so both map to `Edit`."""
    out = []
    for verb, path in _PATCH_FILE_RE.findall(body or ""):
        out.append(("Write" if verb == "Add" else "Edit", path.strip()))
    return out


def _tool_parts(payload: dict) -> list[dict]:
    """The tool-call parts one Codex call becomes: a patch over three files
    becomes three, so each lands in the files-changed view."""
    name = payload.get("name") or ""
    call_id = str(payload.get("call_id") or payload.get("id") or "")
    if name == "apply_patch":
        files = _patch_files(payload.get("input") if isinstance(payload.get("input"), str)
                             else str(payload.get("arguments") or ""))
        if files:
            return [{"type": "tool_use", "id": f"{call_id}:{i}", "name": tool,
                     "input": {"file_path": path}}
                    for i, (tool, path) in enumerate(files)]
        return [{"type": "tool_use", "id": call_id, "name": "Edit", "input": {}}]
    if name in _SHELL_TOOLS:
        command = _command_of(payload)
        return [{"type": "tool_use", "id": call_id, "name": "Bash",
                 "input": {"command": command}}]
    if name in _AGENT_TOOLS:
        args = payload.get("arguments")
        task = ""
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                task = parsed.get("task_name", "") if isinstance(parsed, dict) else ""
            except json.JSONDecodeError:
                task = ""
        return [{"type": "tool_use", "id": call_id, "name": "Task",
                 "input": {"description": task}}]
    if name in _IMAGE_TOOLS:
        args = payload.get("arguments")
        path = ""
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                path = parsed.get("path", "") if isinstance(parsed, dict) else ""
            except json.JSONDecodeError:
                path = ""
        return [{"type": "tool_use", "id": call_id, "name": "Read",
                 "input": {"file_path": path}}]
    # An MCP call and anything else keep their own name: counted, unclassified.
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"arguments": args}
    return [{"type": "tool_use", "id": call_id, "name": str(name),
             "input": args if isinstance(args, dict) else {}}]


def _injected(text: str) -> bool:
    """True where the harness wrote this user record rather than the user."""
    if text.lstrip().startswith(_INJECTED_OPENINGS):
        return True
    tag = _ENVELOPE_RE.match(text)
    return bool(tag) and f"</{tag.group(1)}>" in text


def _question_call(item_id: str) -> str:
    """The call id inside a `questionItemId`, which holds
    `["request_user_input_async", "call_…", 0]` as JSON."""
    try:
        parts = json.loads(item_id)
    except (json.JSONDecodeError, TypeError):
        return ""
    return parts[1] if isinstance(parts, list) and len(parts) > 1 else ""


def _question_answers(text: str) -> tuple[dict, str]:
    """`({question: answer}, call_id)` from a question-reply record, or
    `({}, "")` — on which the record stays as its own text. The call id joins
    the answers to the options the asking call offered."""
    body = text.strip()
    if _QUESTION_REPLY_TAG not in body:
        return {}, ""
    inner = body.partition(f"<{_QUESTION_REPLY_TAG}>")[2]
    inner = inner.rpartition(f"</{_QUESTION_REPLY_TAG}>")[0] or inner
    try:
        items = json.loads(inner.strip())
    except json.JSONDecodeError:
        return {}, ""
    if not isinstance(items, list):
        return {}, ""
    answers, call = {}, ""
    for item in items:
        if not isinstance(item, dict):
            continue
        question, answer = item.get("question"), item.get("answer")
        if isinstance(question, str) and isinstance(answer, str):
            answers[question] = answer
            call = call or _question_call(item.get("questionItemId", ""))
    return answers, call


def _asked_questions(payload: dict) -> list:
    """The questions a `request_user_input` call offered, in Claude Code's
    `toolUseResult.questions` shape, so one renderer serves both.

    Codex writes two shapes: `{title, options: ["…"]}` and `{question, header,
    options: [{label, description}]}`. Both normalise here — the question text
    is the key the answers join on, so a `title` left unmapped would drop the
    options from the record that answered it."""
    raw = payload.get("arguments")
    if not isinstance(raw, str):
        return []
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        return []
    questions = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(questions, list):
        return []
    out = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        text = q.get("question") or q.get("title")
        if not isinstance(text, str) or not text.strip():
            continue
        options = []
        for option in q.get("options") or []:
            if isinstance(option, str):
                options.append({"label": option})
            elif isinstance(option, dict):
                options.append({"label": str(option.get("label") or ""),
                                "description": str(option.get("description") or "")})
        entry = {"question": text, "options": options}
        if isinstance(q.get("header"), str) and q["header"]:
            entry["header"] = q["header"]
        out.append(entry)
    return out


def _delegated_input(text: str) -> str:
    """What the user said inside a delegation envelope, or "". The routed
    transcript sits beside it and is not the turn."""
    match = _DELEGATION_INPUT_RE.search(text)
    return match.group(1).strip() if match else ""


def translate(path: pathlib.Path) -> Iterator[dict]:
    """One rollout as common-shape records, in order. Each carries the session
    id and the cwd in force at that point, which fixes its project."""
    session = _session_id(path)
    cwd = ""
    ordinal = 0
    parent = ""
    # Questions offered, by the call that asked them. The answer arrives as a
    # later user record naming that call, and carries no options of its own.
    offered: dict[str, list] = {}

    def emit(kind: str, content: list, **extra) -> dict:
        nonlocal ordinal, parent
        ordinal += 1
        rec = {
            "type": kind,
            "uuid": _uuid(session, ordinal),
            "parentUuid": parent or None,
            "sessionId": session,
            "cwd": cwd,
            "timestamp": stamp,
            "message": {"role": "user" if kind == "user" else "assistant",
                        "content": content},
        }
        rec.update(extra)
        parent = rec["uuid"]
        return rec

    try:
        handle = path.open(errors="replace")
    except OSError:
        return
    with handle as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            payload = rec.get("payload")
            stamp = rec.get("timestamp") or ""
            kind = rec.get("type")

            if kind == "session_meta" and isinstance(payload, dict):
                cwd = payload.get("cwd") or cwd
                continue
            if kind == "turn_context" and isinstance(payload, dict):
                # A turn can run in a directory the session did not start in.
                cwd = payload.get("cwd") or cwd
                continue
            if kind != "response_item" or not isinstance(payload, dict):
                # `event_msg` repeats `response_item`; the rest is token
                # counts and sandbox state.
                continue

            item = payload.get("type")
            if item == "message":
                role = payload.get("role")
                text = _text_of(payload.get("content"))
                if not text:
                    continue
                if role == "assistant":
                    yield emit("assistant", [{"type": "text", "text": text}])
                elif role == "user":
                    parts = _text_parts(payload.get("content"))
                    answers, call = _question_answers(text)
                    if answers:
                        # The shape Claude Code writes for the same exchange:
                        # an empty body, the pairs in `toolUseResult.answers`,
                        # and no `sourceToolUseID` — which would drop the
                        # record from the row views.
                        result = {"answers": answers}
                        if offered.get(call):
                            result["questions"] = offered[call]
                        yield emit("user", [{"type": "text", "text": ""}],
                                   toolUseResult=result)
                        continue
                    spoken = _delegated_input(text)
                    if spoken:
                        # The envelope is the harness's, the words inside are
                        # the user's.
                        yield emit("user", [{"type": "text", "text": spoken}])
                        continue
                    # The first part decides. An approval record quotes a
                    # whole transcript whose turns match nothing, and an
                    # attachment manifest shares its part with the request.
                    extra = ({"sourceToolUseID": f"{session}:harness"}
                             if parts and _injected(parts[0]) else {})
                    yield emit("user", [{"type": "text", "text": text}], **extra)
                elif role == "developer":
                    # Sandbox and permission instructions, not typed.
                    yield emit("user", [{"type": "text", "text": text}],
                               sourceToolUseID=f"{session}:developer")
            elif item == "reasoning":
                summary = _text_of(payload.get("summary"))
                if summary:
                    yield emit("assistant",
                               [{"type": "thinking", "thinking": summary,
                                 "signature": ""}])
            elif item in ("function_call", "custom_tool_call"):
                if str(payload.get("name") or "").startswith("request_user_input"):
                    asked = _asked_questions(payload)
                    if asked:
                        offered[str(payload.get("call_id") or "")] = asked
                yield emit("assistant", _tool_parts(payload))
            elif item in ("function_call_output", "custom_tool_call_output"):
                call_id = str(payload.get("call_id") or "")
                output = payload.get("output")
                if isinstance(output, dict):
                    output = output.get("content") or json.dumps(output)
                yield emit("user", [{"type": "tool_result",
                                     "tool_use_id": call_id,
                                     "content": str(output or "")}])


register(Source(name="codex", label="codex", discover=discover,
                cwd_of=session_cwd, translate=translate,
                session_id=_session_id, agent="Codex"))
