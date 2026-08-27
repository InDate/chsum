# PLAN.md — decisions ledger

Decisions spoken but not executed. A decision leaves this file only by being
built or by being ruled dead in writing here.

## 2026-08-27 — every reference is a row

Governing sentence: no document chsum prints needs claude-history to resolve —
messages, tools and commands become views built from raw JSONL and addressed by
`<session>:<line>`, `mN` and `ma_…` leave the output entirely, and claude-history
remains only inside `chsum find`, where the embedding index has no local
equivalent.

### Built 2026-08-27

- **One path, three views.** `_Row`, `collect_rows`, `render_rows`, `find_row`,
  `render_row_detail`, `_ROW_KINDS`; `--messages`, `--tools`, `--commands`.
- **`messages_from_jsonl` is every digest's reader**, recording each message's
  row. Measured a superset of what it replaced: 676 messages against
  `claude-history agent read`'s 544 over twelve sessions, and 0 of the 86 it
  dropped appear anywhere in the text claude-history returned.
- **Every reference is a locator.** Quoted prompts, marks, the last exchange and
  `_failures_section` all name `<session>:<line>`; `_drill_block` states the full
  paths and the `sed`. 16 of 16 locators in one digest resolved to a record.
- **Removed:** `read_messages`, `last_message_number`, `uuid_for_ref`,
  `_locate_mark`, `_citable_anchors`, `Message.anchor`, `Mark.n`, `Mark.anchor`.
  Closes devharness #12 — a digest now reports `procs (0)`.
- **Recap and catch-up rows locate themselves.** Every `_Event` records its row;
  `_failures_section`, and the `since` block's files and commands via
  `_located_bullets` and a widened `_timed_bullets`, print it.

### Deferred, not ruled dead

- `_event_block` deliberately carries no locator: a model reads that extract.

## 2026-08-27 — the commands timeline

Governing sentence: the digest's `Commands run` section keeps the ten it prints
today, its overflow line names a `chsum` invocation that outputs every Bash
command the session and its agents ran, in timestamp order, unfiltered and
uncollapsed, and each command in that output names a further invocation that
prints that command's own captured output.

### Built 2026-08-27

- **`chsum digest <ref> --commands`** — every Bash call in timestamp order
  across the transcript and its sidecars, one clipped line each, nothing
  filtered or deduplicated. `_Command`, `collect_commands`, `render_commands`.
  An agent ref narrows to that sidecar and the header names the scope.
- **`chsum digest <ref> --command <id>`** — one command and its captured output
  whole, `find_command` + `render_command_output`, prefix-matched on the id with
  an ambiguity list rather than a pick.
- **The overflow line carries both counts and the invocation.**
  `meta.command_total` / `AgentRun.command_total`, counted in `_collect_tools`
  on the read `extract_meta` already does.
- **`_short_id`** — display form of a `tool_use` id, added during the build and
  not in the approved plan's identifier list.
- **Every row locates its own record.** `_Command.line` / `_Command.source`,
  `_locator` (`<session>:<line>`, `<session>/<agent>:<line>`), `_sources_block`
  (full path per source, plus a worked `sed` line). `render_command_output`
  states the file, the line and the `sed` that opens it. Verified by resolving
  every row of four documents through `sed`: 370 of 370 landed on a record
  carrying that row's tool id.

### Deferred, not ruled dead

- The same `…and N more` dead-end at `chsum.py:1466` (subagent digest commands)
  and in catch-up's `since` block. Parked by decision on 2026-08-27.
- The hint appears only where the ten overflow. A session with fewer than ten
  notable commands and many raw ones shows no pointer to the timeline. Follows
  the governing sentence as approved; not raised as a defect.

## 2026-08-19 — recap far edge and Skill bodies

Source: a recap of session `4ac57141` that ended at the chsum skill body, quoted
2,000 chars of it as a turn, and cut the two replies that answered the question
the session was asking.

### Built 2026-08-19

- **A recap ending on the last turn runs to the end of the session.** `until`
  empty, `live` passed into `_render_window` instead of derived, `_turn_files`
  reading empty as "no bound".
- **A record the Skill tool injected is not a turn.** `_tool_injected` on
  `sourceToolUseID`, guarding `_is_typed_prompt`, `_your_turns`, `_last_prompt`
  and `messages_from_jsonl`.
- **A record loaded by a slash command is not a turn.** Same guard, second
  shape: `_command_prompt_ids` joins a body to the `<command-name>` record by
  `promptId`. 32 dropped against 325 kept, where `isMeta` alone took all 357.
- **The act survives where the body does not.** A slash command becomes a
  `"command"` event; `_tool_event` names the skill and args on a `Skill` call
  instead of printing the bare tool name.
- **The wizard's session list carries `chsum`'s columns**, and `when` is a full
  local datetime (`_local_when`) rather than `_ago`'s coarse age.
- **The ticker states a denominator and a ceiling.** `_Counter` bumped per
  finished chunk, `_CALL_TIMEOUT` × waves of `_CHUNK_WORKERS` as the bound.
  Source: a 2,116s run that showed a rising clock and nothing else.
- **`f`/`l` select the first and last entry** on every wizard list step.

### Resolved 2026-08-19

The `Message`-layer gap was posted here as deferred and then measured away.
`render_digest` and `_last_exchange` never see a skill body: `claude-history
agent read` strips them, measured across 14 sessions covering 44 skill-body
records, none emitted as a user message. The agent-digest path did have the
defect and it was structural, not prose — `messages_from_jsonl` holds the raw
record, so `_tool_injected` reaches it directly. Three sampled sidecars dropped
7,804 / 7,804 / 5,201 chars of quoted skill.

## 2026-08-20 — the turn store

Governing sentence: every turn whose gap has closed carries a breakdown file on
disk, named by the turn's own record `uuid`, holding each summary written for
that turn beside the instructions and model that produced it; a recap reads that
store first and calls the model only for the turns absent from it.

### Built 2026-08-20

- **chsum's state moved to `~/.chsum`.** `CHSUM_DIR` is the one place the path
  is written; `DIGEST_DIR`, `NAMES_PATH`, `TURNS_DIR` and `HaikuSummariser`'s
  cwd hang off it. No fallback read of `~/.claude/chsum` — the files moved once.
- **One file per turn, named by the record `uuid`.** A fork preserves `uuid`,
  rewrites `sessionId`, and reorders the rows, so neither of the other two names
  one turn across two branch files.
- **A gap closes on a later boundary or on a closing `stop_reason`**, either
  alone being insufficient: the boundary test cannot close the last turn of a
  window running to the session end, and the closing value cannot close an
  interrupted turn, whose stretch ends on `tool_use` (38 of 634 measured).
- **Existing is not a hit; matching is.** Instructions, model, part count and
  each part's material are all checked before a stored breakdown is read back.
- **`summaries` is an array.** A breakdown written under a different
  `_CHUNK_PROMPT` stays beside the new one rather than being orphaned.
- **`--dry-run` and the wizard's cost step price only the misses**, and name a
  stored chunk apart from a quiet one. The cost screen states the stored count
  at zero too, or a silent line reads the same as a store never consulted.
- **A turn reaches disk as its own chunks land** (`_store_turn`), not at the end
  of the run. Source: a 660.9s run over 24 chunks, where a kill at any point
  before the final step stored nothing. Killed at 75s of a 20-chunk window, 3
  turns survived and read back as hits. `_turn_closers` moved ahead of the calls.
- **The recap far edge is compared by value, not identity** (`end == turns[-1]`).
  The wizard picks out of its own `_your_turns` list and `cmd_recap` reads the
  file again, so `is` sent every picker-chosen recap down the bounded branch:
  `ch_ebc493cb` cut at 11:33:12 against a session running to 11:33:22,
  `ch_78b9c150` cut at 15:02:55. The flags path was fixed 2026-08-19; the picker
  path carried it until now. No stored breakdown was invalidated — in both cases
  the affected turn was the session's last, whose gap was open and never written.

Measured, session `c77196cc` turns 1–3: 30.1s and two `claude -p` calls, then
0.4s and none, the two documents byte-identical. `--no-cache` on the same
window still prices 2 calls.

### Deferred, not ruled dead

- **A sidecar appending after the parent stretch closes.** Closure is judged on
  the parent transcript; 1 session in 101 has a sidecar ending later than its
  parent. That turn's stored breakdown is short by whatever the sidecar wrote.
  `closed_by` is the uuid a later check would resolve against.
- **The destination screen.** Spoken 2026-08-19 16:26 and not built: recap
  finishing to a choice of clipboard, disk or terminal, in markdown, plain
  text, HTML or JSON, where the JSON describes everything used to produce the
  document. Three of the four formats fall out of `_md_ansi`'s line classifier.

## 2026-08-19 — mark provenance and `--show`

Source: an investigation into session `fbccf4aa` (devharness) that cost sixteen
tool calls, ten of them ad-hoc Python re-implementing transcript navigation.
Six gaps named. Two taken, four deferred.

### Built 2026-08-19

- **`mark --show <id>`, with `--context N`.** `--list` printed a clipped quote
  and an 8-char id; the id resolved under `--revoke` and nothing else.
- **Marks capture provenance at mark time.** `line=` and `agent=` stamped into
  the sentinel, verified against the uuid on read, search kept as the fallback.
- **`mark --list` disagreeing with itself by invocation path.** Minimum fix
  taken: the resolved transcript path prints on every run, and on the empty one.
  The reported symptom (printing `ix`, exit 1 from a Bash tool call) was never
  reproduced — only the file-naming fix was made.

### Deferred, not ruled dead

- **A raw row view.** `digest`, `recap` and `journal` are all summary-shaped.
  Nothing prints a `tool_use` input or a `tool_result` body verbatim. The
  evidence that settled the investigation was `Edit.new_string` byte lengths and
  `message.id` values, which no chsum surface exposes.
- **An agent graph per session.** Agent id, first line of prompt, model, and
  where two agents share a message-id prefix. The answer lived in a sibling
  agent's transcript that nothing in the mark pointed to. Cheaper than it looked:
  `agent-<id>.meta.json` carries `isFork`, `parentAgentId` and `spawnDepth`, and
  `extract_agent` already opens that file for `agentType`/`model`/`description`.
  The message-id prefix work is only needed for *session* forks, which carry no
  declaration.
- **A cross-agent write timeline.** Every `Edit`/`Write` sorted by timestamp,
  with payload size. This is the view that cracked the investigation and it had
  to be built by hand. It is also the check that discriminates a real
  external-modification notice from a fabricated one — sibling writes to the same
  path in the same window.

### Resolved 2026-08-19

The duplicate-agent condition was an `Agent` tool call with
`subagent_type: "fork"`: documented mechanism, not an anomaly. Source:
`~/.claude/docs/claude-code-fork-semantics.md`, written against Claude Code
`2.1.224`, which records the same pair (`a14ee1c313ce9a8c3`, 29-message shared
prefix) and the field behaviour behind it — `uuid` and `message.id` survive the
copy, `sessionId` is rewritten to the containing file. Whether the condition
occurs in other sessions is still unchecked; the doc's own detection recipes
(section 6) are what would settle it.
