# PLAN.md — decisions ledger

Decisions spoken but not executed. A decision leaves this file only by being
built or by being ruled dead in writing here.

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
