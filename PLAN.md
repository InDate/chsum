# PLAN.md — decisions ledger

Decisions spoken but not executed. A decision leaves this file only by being
built or by being ruled dead in writing here.

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
