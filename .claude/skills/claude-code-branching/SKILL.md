---
name: claude-code-branching
description: Branching a Claude Code session into a side session — forking it, sending its prose to another model, resuming a side session, and what each costs and reads from the prompt cache. Use when hydrating a side agent with a conversation, or pricing one run against another.
---

# Claude Code branching

A branch is a second session started from an existing one, with its own
session id. chsum's `--branches` names a different object: the path a rewind
leaves inside one transcript.

All cache facts below hold for a Claude subscription within plan usage.
Sources: code.claude.com/docs/en/prompt-caching and cli-reference (read
2026-10-05), and scratch-project measurements on that date.

## Two routes

**Fork** — the parent's history, read from its cache:

```
claude -p --resume <session-id> --fork-session --output-format json < message.txt
```

Run from the parent's directory, on its model, with no `--model`,
`--system-prompt`, `--tools` or `--setting-sources` override, so the request
opens on the parent's cached bytes. Measured, 245,467-token Opus session:
243,777 read, 1,688 written. The branch holds every parent turn, including
turns after the reply under study, which can carry the correction a check on
that reply exists to produce.

**Prose** — the transcript's messages, sent fresh:

```
chsum digest <ref> --messages --stdout > material.md
claude -p --model <model> --system-prompt "$(cat prompt.txt)" \
  --tools "" --setting-sources "" --output-format json < material.md
```

Shares no prefix with the parent, so every token is written: 29,665 on the
same session (Sonnet), against chsum's characters ÷ 4 estimate of 16.8K. It
can be cut at the reply under study and runs on a session of any age. `chsum
recap` runs this route with `CLAUDE_CODE_PROMPT_CACHE_TTL=5m` (1.25× writes, not
2×; nothing resumes it) and `--no-session-persistence`.

A branch given one reply alone reported terms as having no referent where the
referent stood earlier; both routes carry the conversation and avoided it.

## How the cache matches

Each request carries tools, system prompt, then messages. The server stores
the prefix up to a cache point, scoped to model, machine and working
directory. A later request reads an entry when its bytes match up to that
point; lookup steps back at most 20 positions. One changed byte voids every
entry after it.

| Change | Cache |
|---|---|
| none (resume, fork, rewind) | read |
| tool list (whole-tool deny, MCP server added/removed, likely an upgrade) | miss |
| model, or fast mode first turned on | miss |
| working directory | miss |
| >~20 positions past the last cache point | miss |
| lifetime lapsed | miss |

## Lifetime

Every read or write restarts an entry's clock.

| Bucket | Lifetime | Setting |
|---|---|---|
| main conversation (interactive, `-p`, SDK) | 1h | `promptCacheTtl` |
| subagents, forks, workflows, compaction | 5m | `subagentPromptCacheTtl` |

Values `5m`/`1h`. First match wins: `FORCE_PROMPT_CACHING_5M=1`,
`CLAUDE_CODE_PROMPT_CACHE_TTL` / `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`, the
setting, an agent's `cacheTtl` frontmatter, `ENABLE_PROMPT_CACHING_1H=1`.
`usage.cache_creation` splits writes into `ephemeral_1h_input_tokens` and
`ephemeral_5m_input_tokens`.

Measured (Sonnet):

| Request | Gap | Read | Written |
|---|---|---|---|
| main conversation resumed | 5m01s | 33,950 | 64 |
| agent mid-task, default | 5m34s | 6,987 | 15,920 |
| agent mid-task, 1h setting | 5m32s | 20,743 | 2,173 |

An agent whose single tool call runs past 5 minutes rewrites its history on
the next request. The 1h setting holds it, at 2× write instead of 1.25×: about
0.75 × its final prefix extra over its run, against ~1.25 × prefix per gap
lost.

## Agents

An agent's first request reads the prefix shared by agents — tool definitions
and agent system prompt — only while another agent of the same type, model
and directory used it within 5 minutes; otherwise it writes it. Measured:
"late" read 6,987 written by "early" seconds before; the first agent of a
session read 0; a background agent read 5,761, so launch mode shifts it.

A message to a running agent lands as a `queued_command` beside its next
tool result. It sends no request, so it neither refreshes nor breaks the
cache (that step read 21,681, wrote 211).

A resume (SendMessage to a finished agent) splits on tool calls:

| Agent | Tool calls | Gap | Read | Written |
|---|---|---|---|---|
| fgtool | 1 | <1m | 22,811 | 42 |
| stepper | 6 | 1m43s | 21,681 | 613 |
| stepper again | | 8s | 22,294 | 41 |
| 4 agents | 0 | 57s–5m14s | 5,761–6,987 | ~13,700 |

An agent that made a tool call reads its history while warm, and the resume
restarts the clock. An agent that answered in one request rewrites it on every
resume (cause unidentified; its first `prompt_snapshot` lacks fields the later
one carries). An expired agent's resume costs its own history while another
agent keeps the shared prefix warm, its whole prefix otherwise.

An agent stays warm only by a parent message inside its lifetime; each costs
a parent turn reading the parent's whole prefix. A worker run as its own
session (`claude -p`, or one reached by session-to-session messaging) falls in
the main bucket: 1h lifetime, resumable, held by `chsum warm`. Measured: a
session-to-session message to a worker idle 6m34s read 43,096, wrote 249.

A `fork` agent inherits the parent's prefix, so its first request reads the
parent's cache; its writes are 5m. Docs only: `-p` offers no `fork` type.

`chsum cache [ref]` reports each conversation and agent: warm or expired,
time left, prefix, and the resume cost under these rules.

## What a resume replays

The system prompt is recorded on the first request (`prompt_snapshot`) and
reused across `--resume`, `--continue` and upgrades until compaction;
`--system-prompt-snapshot off` and `--bare` rebuild it. `CLAUDE.md`, date,
environment, skill listing, deferred tools and hook output are attachments
replayed as recorded; an edit appends at the tail. The upfront tool list is
rebuilt from current settings and opens the request.

| Change before `claude -p --resume` | Read | Written |
|---|---|---|
| none (Haiku) | 22,247 | 151 |
| existing `SKILL.md` edited | 22,398 | 90 |
| project `CLAUDE.md` edited | 22,488 | 218 |
| `--disallowedTools WebFetch` (deferred) | 22,706 | 160 |
| `--disallowedTools Bash` | 0 | 19,928 |
| `"deny": ["Bash"]` in settings (Sonnet) | 0 | 28,358 |

The docs say a whole-tool deny keeps the cache under tool search; on resume
it missed both times. Scoped denies and allow/ask rules leave the tool list
alone (docs, unmeasured).

`chsum warm [ref]` pings an idle main conversation before expiry
with `claude -p --resume --no-session-persistence`, at the session's own
lifetime (a ping at the other lifetime read 10,470 of 28,028). The transcript
stays untouched; it runs until interrupted, a miss, or `--for`. `chsum warm` reaches session ids only, so agents
fall outside it.

## Branch resume check

Each `assistant` record carries `timestamp`, `message.model`, `version` and
`usage.cache_creation`, so expiry is computable offline. `chsum digest
--branches N` refuses when the entries expired, the Claude Code version
differs, or `settings.json`, `settings.local.json` or `.mcp.json` changed
after the last request; it pins `--model`, and `--force` overrides. Measured
pass: a rewind branch resumed after 1m46s read 43,165, wrote 893. A rewound
branch shares the trunk's prefix to the split, which later trunk turns keep
warm.

## Reading results

`--output-format json` returns `session_id`, `usage`, `result`. Price from
`usage`: on a fork, `modelUsage` and `total_cost_usd` carried the parent's
running totals. `/usage` shows hit ratio, misses, warm state and the likely
cause of the last miss.

## Not yet tested

- Why a no-tool agent misses on resume.
- A resume across an upgrade.
- A whole-tool deny added inside a live process.
- A `fork` agent's cache read.
