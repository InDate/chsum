# Chat Summary (chsum)

Pick up from where you left off with deterministic summaries of previous
sessions. Replay the last 10 messages, or a range of specific messages between
you and the agent, or get a digest of everything you typed with the tools used
and files changed. This rehydrates context surgically.

```sh
chsum digest --last                       # everything you typed, tools used, files changed
chsum digest --last --messages -10 -1     # the last 10 messages
chsum digest --last --messages 3 10       # a range of them
```

I've turned off auto-compact and use all my context tokens, knowing I can start
the next session with one command: `chsum digest --last`.

chsum also:

- **Undo a change inside a session**, one change at a time, no matter the tool
  used to change the file. To use it: when chsum is first loaded into a
  git-enabled repo, the agent asks if you want checkpointing enabled. See the
  [implementation details](#checkpoints-and-undo) below.
- **Load back an abandoned path.** Started a session, then rewound to a
  specific spot to continue along another path? chsum makes it easy to load
  back the other, "abandoned" path with `chsum digest --branches`. See the
  [implementation details](#branches) on how this works.
- **See what a subagent is doing** before its report arrives. A session
  launches an agent and cannot get its details without overloading its own
  context. chsum works around that by sharing condensed updates of the files
  being edited, with just the short prose an agent normally outputs between
  them. The session can easily see whether the agent has sidetracked, and give
  clear nudges to correct it, or stop it. See the
  [implementation details](#subagents) for how this works.
- **Mark a moment to get back to.** Something interesting happened during a
  session and you want to get back to it easily? Use `chsum mark "<reason>"`
  inside the conversation, and the previous reply is included in the digests.
  See the [implementation details](#marks) for how this works.

It reads Claude Code and Codex CLI sessions alike.

Run `chsum` to list this project's sessions:

```
Sessions — chsum
5 sessions · 1 with no activity

Fri 02 Oct 2026                        dur    prompts  files  agents  branches  notes  digest  recap
  ch_92e5397d588f409d6ca4b843317d4f97  2h37m  57       8      -       3         -      -       -
    ↳ Commit staged changes
  ch_34fa3ce4453202d47be9c173100427ef  1h00m  11       1      -       2         -      -       -
    ↳ Fitness landscape digest --branches feature

Fri 25 Sep 2026
  ch_c4e516f02a4084eb9e1daf3bc2b1ab85  1h10m  19       3      -       2         -      -       -
    ↳ Chsum digest output formatting
  ch_f111160d4a5e2b477d689babcba77d35  1m     1        0      -       -         -      -       -
    ↳ Bench screenshots during recording
  ch_3f91d2af6f621b4b61790a280fafb5e3  1s     0        0      -       -         -      -       -
    ↳ (untitled)

Read one: `chsum digest <ref> --stdout`   Most recent real session: `chsum digest --last --stdout`
```

## What to run

| You want | Run |
|---|---|
| What we did yesterday, or to pick up where we left off | `chsum digest --last` |
| The context of an old session in a new one | `chsum digest <ref> --stdout` |
| What Claude did with each thing you said | `chsum recap` |
| What Claude has done so far, from a second terminal | `chsum recap` |
| Which session it was, and which went anywhere | `chsum` |
| A session by what was said in it | `chsum find "…"` |
| The exact command Claude ran, or its output | `chsum digest <ref> --commands`, `--call <id>` |
| A subagent's report, or everything it did | `chsum digest --agents`, `<ref>/<agent-id>` |
| The raw record behind `01a0acf9:31` | `chsum where 01a0acf9:31` |
| This session's ref | `chsum here` |
| The path a rewind left behind | `chsum digest <ref> --branches` |
| A change Claude made, taken back | `chsum undo` |
| The moment that mattered, findable later | `chsum note "…"` |
| A session titled for what it became | `chsum name "…"` |
| This week's work, across projects | `chsum --since 7d --all -n 0` |
| Your notes in claude-history's viewer | [`chsum annotations`](#chsum-annotations) |

Coming from 2.x: [what changed](#upgrading-to-30).

## Install

As a Claude Code plugin, which brings the skill, the hooks and the code:

```
/plugin marketplace add InDate/indate-tools
/plugin install chsum@indate-tools
```

The hooks run the `chsum.py` beside them, so nothing needs to be on `PATH`.
For a `chsum` command of your own, link that file once:

```sh
ln -s ~/.claude/plugins/cache/indate-tools/chsum/<version>/chsum.py ~/.local/bin/chsum
```

Working on chsum itself: `pipx install --editable .` from the checkout.

Requirements:

- Python 3.10+. No third-party packages, no network.
- [`claude-history`](https://github.com/raine/claude-history) on `PATH`, for
  `chsum find` only.
- `claude` on `PATH`, for `chsum recap`'s timeline only.

### Letting Claude run it

`! chsum …` runs it yourself. For Claude to run it unprompted, allow it in
`~/.claude/settings.json` or a project's `.claude/settings.local.json`:

```json
{ "permissions": { "allow": ["Bash(chsum:*)"] } }
```

chsum reads transcripts you already have and writes to its own store (see
[Where chsum writes](#where-chsum-writes)); `undo` and `redo` also write the
files they restore. A plugin cannot grant itself this permission.

## Picking a conversation, and a window of it

`recap` and `digest` take the same selectors:

```sh
chsum digest                          # the session you are in
chsum digest ch_3654a13c              # by the ref the listing prints
chsum digest --last                   # the newest session other than this one
chsum digest --last 2                 # the one before that
chsum digest --file path/to/session.jsonl
chsum digest --messages 3 10          # your turns 3 to 10
chsum digest --messages -1            # your last turn, and everything after it
chsum digest ch_da4e…/a728cd49…       # one subagent, as <parent-ref>/<agent-id>
```

A turn is one thing you typed, or one answer you picked from a question, and
everything up to your next. `1` is your first and `-1` your last; two numbers
name a run, both ends included. The header states what they resolved to:
`-10 -1 — your turns 45–54 of 54`.

`--last` orders by last activity. Everything scopes to the current project;
`--all` widens it.

## `chsum`

The project's sessions, newest activity first:

```
Thu 06 Aug 2026                        dur    prompts  files  agents  branches  notes  digest   recap
  ch_c120431a267b202aebf0b38f6c3c1b69  5h38m  78       14     -       -         ⚑2     current  3/12 · 2h ago
    ↳ Plan the import pipeline from the sample files

Wed 05 Aug 2026
  ch_da4e99d42e5efab11ebdedc22fb65145  3h03m  30       12     2       2         -      stale    -
    ↳ Set up the dev server
      a43c4ff4401ca693e  Quieten the test suite
      a81d77b6cba4a46b3  Fix the retry backoff
```

```sh
chsum                 # the five most recent
chsum -n 25           # more; 0 for all
chsum --since 7d      # the last week
chsum --all           # every project, with a project column
```

- **Dead ends are listed**, since "that went nowhere" is often the answer. The
  header counts them: one prompt, no files, no agents.
- **Subagents are named**, with the id `chsum digest <ref>/<id>` takes.
- **`branches`** counts the paths a rewind left; `chsum digest <ref> --branches`
  lists them.
- **`digest`** is `current`, `stale` (the transcript has grown since) or `-`.
- **`recap`** is turns recapped over turns there are, and when.
- **`✎`** marks a title you gave with `chsum name`.

## `chsum recap`

What Claude did with what you said. Each of your turns quoted, then everything
before you spoke again: files touched, commands, agents and failures, computed
from the transcript, and beneath them a timeline written by
`claude -p --model haiku` from that turn's events alone.

```sh
chsum recap                                    # this session, since the last recap
chsum recap --last                             # the previous session
chsum recap ch_3654a13c --messages 3 10        # turns 3 to 10 of a session
chsum recap --full                             # the whole session
chsum recap --messages 3 10 --dry-run          # the cost, no model call
chsum recap --messages 3 10 --invalidate       # summarise again, replacing what's stored
chsum recap --messages 3 10 --no-cache         # neither read nor write the store
```

```
### You said (21:43)

> ok

- **21:45** Created a new `is_typed_prompt()` helper that filters out
  `<bash-…>` records, and updated five call sites to use it.
- **21:47** Tested the fix against a live session; chsum now reports
  `prompts: 0` for the dead 2-second session and skips it.
```

### The bare run

`chsum recap` with nothing after it is the running session since your last
prompt, failures first. Run it from a second terminal while Claude works; there
it lists the project's recent sessions and Enter takes the newest.

### Cost

Each turn is its own small `claude -p` call, run in parallel, with Claude Code's
system prompt and tools stripped to about 158 tokens of fixed cost. `--dry-run`
prices a window without calling; after a real run one line on stderr gives what
it cost:

```
haiku: 5,974 in (5,042 cache read) · 4,866 out · 56.0s · extract estimated ~3,211, harness ~2,763
```

A turn's bullets are stored once its gap closes, so recapping a window twice
costs nothing the second time. They are reused only while the instructions, the
model and the turn's events are unchanged. `0 calls would be made` means the
window is already stored.

### Files touched

Each turn's files come from a git checkpoint where one exists (every change,
current line ranges) and from the transcript otherwise (`Edit`, `Write` and
`MultiEdit` only). The recap counts which: `4 of 6 turns from a checkpoint`.
Checkpoints are opt-in; see [`chsum hook`](#chsum-hook).

## `chsum digest`

One conversation: your prompts in order, the files changed and commands that did
something, the last exchange, and the `sed` line that opens any row. It writes a
file and prints the path; `--stdout` prints the document instead. Every view
below follows that rule.

https://github.com/user-attachments/assets/93f3cdac-cf82-403a-8333-5c8e42159fd7

```sh
chsum digest                              # this session
chsum digest <ref> --stdout               # that one, printed
chsum digest <ref>/<agent-id>             # one subagent
chsum digest --list                       # this project's digest files; --all every project
```

| Section | Source |
|---|---|
| Frontmatter: ref, title, project, branch, start, duration, counts | computed |
| **Notable**: your `chsum note`s | copied |
| **What I asked for**: your prompts, each with its row and what the turn did | copied |
| **Files changed** / **Commands run** | parsed from tool calls |
| **Delegated**: each subagent and its address | parsed from sidecars |
| **Where I left off**: last prompt and last reply | copied |
| **Drill down**: transcript paths and the `sed` that opens a row | computed |

```
> when I do --list on a note, it prints out the entire directory which looks terrible

*`49932ac7:37` · 19s · 2 commands · 1 reply*
```

- **A row is an address.** `1f271ca8:441` is line 441 of that session's
  transcript; `1f271ca8/a190d601:87` is line 87 of a subagent's.
- **Cuts are marked.** `[+N chars, sed the row below]` and `…and N more` say
  when you are reading a fragment.
- **"Where I left off" is two scans.** The last prompt and the last reply may be
  far apart.
- **A subagent's work counts toward the turn that launched it**; files only an
  agent touched are marked `(agent)`.

### The row views

```sh
chsum digest <ref> --messages               # every message whole, each call a line between
chsum digest <ref> --tools                  # every tool call
chsum digest <ref> --commands               # every Bash call
chsum digest <ref> --call <id>              # one call and its output, whole
chsum digest <ref> --agents                 # every subagent and its report
chsum digest <ref> --writes                 # each turn's file changes, from the checkpoints
chsum digest <ref> --branches               # the paths a rewind left; --branches <n> resumes one
chsum digest <ref>/<agent-id> --messages    # everything that agent said and called
```

Each writes `<uuid>-<view>.md` beside the digest. Rows run in time order across
the transcript and its sidecars, each opening with its call id and a
`<session>:<line>` locator, grouped under turn headings:

```
*turn 8 · 16m · 6 tool calls · 5 files · 108 commands · 12 replies*
```

Numbers after `--messages`, `--tools` or `--commands` narrow to a window of your
turns; there `--tools` and `--commands` print their rows whole:

```sh
chsum digest --last --messages -1           # your last turn
chsum digest <ref> --tools -5 -1            # calls across your last five turns
chsum digest <ref>/<agent-id> --tools 2 -1  # an agent's own turns
```

- **Each message is labelled by its sender**: `user`, `assistant`,
  `agent … returned`, `message from <session>`, `coordinator`. Only what you
  typed opens a turn.
- **Skipped rows are named** inside a window: `⋯ 4 rows not in this view`, each
  with its locator.
- **`## Sources`** at the top maps every locator to its file.

## `chsum where`

A locator chsum printed, turned into the command that prints that record:

```
$ chsum where 01a0acf9:31
sed -n '31p' ~/.local/share/chsum/translated/codex/…/01a0acf9-….jsonl | jq
```

| You have | You type |
|---|---|
| one row | `chsum where 01a0acf9:31` |
| a run of rows | `chsum where 01a0acf9:31-40` |
| a subagent's row | `chsum where 01a0acf9/a9f0f78b:3` |
| a session | `chsum where 01a0acf9` |
| a tool call id | `chsum where toolu_01V7rDx5Le` |

The command alone goes to stdout, so `chsum where 01a0acf9:31 | sh` runs it.
`--git` gives the checkpoint that call wrote and the `git diff` for it.

## `chsum undo` and `chsum redo`

Take back a change made this session, file by file or step by step. A step is
one tool call's change to the tree; each file of a step is lettered.

```
$ chsum undo
28 steps in place, newest first
  1     chsum/core.py:7109-7110
  2  a  chsum/core.py:7076
     b  tests/test_undo.py:63-66
  3     chsum/checkpoints.py:65
```

```sh
chsum undo 1               # reverse the newest step; again for the one before
chsum undo 2b              # one file of step 2
chsum undo 1-3             # the three newest
chsum undo 2 --detail      # the step's diff; nothing changes
chsum undo --detail        # every step's diff; --reverse puts the newest last
chsum undo README.md       # that file's changes, numbered 1 newest
chsum undo README.md 2     # reverse change 2 to it alone
chsum redo 1               # put back the last undo
```

- **Ask Claude to undo**, and Claude Code shows the reversed diff under the call.
- **Numbers shift after every change**, since 1 is always the newest. Act on a
  list you just printed.
- **A change that no longer applies is refused whole.** A later edit on a line
  next to it blocks it, and nothing is written. A range stops there.
- **A file name finds its path.** `chsum undo SKILL.md` finds
  `skills/chsum/SKILL.md` when only one changed file ends that way.
- **The oldest step includes what was uncommitted** when the session began; the
  list notes it.

Each undo and redo is itself a checkpoint, so both lists survive restarts. Both
need checkpointing on and act on the running session only.

## `chsum find`

A conversation, or a note, by what it said. The one command that runs
`claude-history`.

```sh
chsum find "playback rate pitch shift"     # by meaning
chsum find "ENOENT" --lexical              # identifiers, filenames, errors: sub-second
chsum find --notes "backoff"               # your notes
chsum find "…" --all                       # every project
```

The default search takes tens of seconds, and minutes the first time while its
index builds. Where ranking could not run, it says so:
`8 hits ranked by position only`.

Every listing prints padded colour columns on a terminal and markdown anywhere
else, so a pipe or a model reads whole records.

## `chsum note`

Marks the moment that mattered, so the digest says which part was the point.

```sh
! chsum note "the backoff approach, after two dead ends"
```

It prints nothing and never writes the transcript. The note leads the digest
under **Notable**, counts as `⚑` in the listing, and is searchable with
`chsum find --notes`.

An earlier moment, by id or by phrase:

```sh
! chsum note --recent 20                                 # recent messages and calls, with ids
! chsum note --at be74e21f "the thundering-herd point"   # by id, or a row number
! chsum note --match "where it dies" "the timeout gap"   # by something it said
```

Matching ignores case, punctuation and markdown; several matches list the
candidates and file nothing.

While you are talking to an agent, `!` goes to the agent. Note its words
afterwards with `--match`, or ask the agent to note as it works; its notes fold
into the parent, tagged `agent <id>`.

```sh
! chsum note --list                   # this project's notes; --all every project; --full whole text
! chsum note --show dde43c3c#1        # where it landed, with --context N around it
! chsum note --delete dde43c3c#2      # several ids at once
! chsum recap --list                  # the bullets recap wrote, same ids
```

## `chsum name`

Claude Code titles a session from its first question. `chsum name` titles it for
what it became, in chsum and in `/resume`:

```sh
chsum name "retry: design + build"                 # this session
chsum name ch_3654a13c "retry: design + build"     # another
chsum name --list                                  # renamed sessions; --all every project
chsum name --clear                                 # back to Claude Code's title
chsum name --no-resume "…"                         # chsum only
```

It appends one `ai-title` record, the shape Claude Code appends itself, and
keeps the name in chsum's store. Claude Code re-titles a running session as it
grows, so `/resume` can drift back; chsum keeps yours.

## `chsum annotations`

The wire [claude-history](https://github.com/raine/claude-history) calls to read
and write notes. Register it in `~/.config/claude-history/config.toml`:

```toml
[annotations]
write_to = "chsum"

[annotators.chsum]
command = "chsum annotations"
```

Notes and recap bullets then show at their rows in its viewer and match in
`claude-history agent search`; a note typed there (`a`) or deleted there (`d`)
goes through chsum.

<img src="https://raw.githubusercontent.com/InDate/chsum/main/meta/chsum_notes_in_claude-history.webp" alt="chsum notes shown at their rows in claude-history's viewer" width="800" />

## `chsum hook`

What the plugin's hooks run; not a command you type. It writes git checkpoints:
one commit per tool call that changed the tree, which `recap`, `--writes`,
`where --git` and `undo` read.

```
chsum hook post-tool-use    # after each tool call: checkpoint the tree
chsum hook session-start    # at session start: raise the opt-in, once
```

- **Opt-in per project.** Nothing is recorded until `.git/chsum-checkpoint`
  reads `enabled`. Where it is absent, Claude asks you once at session start.
- **Your repo is untouched.** Checkpoints chain under `refs/chsum/<session>`,
  built without touching `HEAD`, your index, your working tree or your commit
  hooks. `git log`, `git status` and `git branch` show nothing.
- **`git show <checkpoint>`** is one call's change.
- **Kept until dropped.** A ref survives `gc` and worktree removal.

```sh
chsum checkpoints                   # the chains, and whether it is on
chsum checkpoints --enable          # on for this project; --disable off
chsum checkpoints --prune 30d       # drop chains older than 30 days; --dry-run first
chsum checkpoints --migrate         # rebuild 2.x reflog checkpoints as chains
```

## Upgrading to 3.0

- **Every view writes a file and prints its path.** Add `--stdout` where a
  script read the document from stdout:
  `chsum digest <ref> --messages --stdout > out.md`.
- **Checkpoints are kept until dropped.** 2.x left them in the reflog to expire;
  3.0 chains them under refs. `chsum checkpoints --prune 30d` is the old
  retention, and `--migrate` moves 2.x checkpoints onto chains.

## Which tools' sessions it reads

Claude Code (`~/.claude/projects`) and Codex CLI (`~/.codex/sessions`), together
in every listing, digest and search. `chsum --source codex` narrows to one; a
`source` column appears where both are present.

A Codex rollout is translated once into Claude Code's record shape under
`<data dir>/chsum/translated/codex/`, so its locators name that file. Three
things in the output come from Codex itself:

- **No titles.** Rows read `(untitled)` until `chsum name`.
- **`0s` durations** on imported threads, which carry one timestamp throughout.
- **Compressed rollouts** (`.jsonl.zst`, older than seven days) are skipped:
  reading them needs a zstd decoder outside the standard library.

A third tool is one file in `chsum/sources/`.

## Where chsum writes

```
<data dir>/chsum/
  digests/<uuid>.md                  a digest (`--out` to change)
  digests/<uuid>-<view>.md           a row view
  names.json                         your session names
  turns/<project>/<turn-uuid>.json   recap bullets and notes, per turn
  translated/<tool>/…/<id>.jsonl     another tool's sessions, translated
```

The data dir is `$CHSUM_DIR`, else `$XDG_DATA_HOME/chsum`, else
`%LOCALAPPDATA%\chsum` on Windows and `~/.local/share/chsum` elsewhere.

Beyond that: `chsum name` appends one `ai-title` record to a transcript, the
hook writes refs under `refs/chsum/`, and `undo` and `redo` write the files they
restore.

## Which version am I running

```
$ chsum --version
chsum 3.0.3 (15f423c) · python 3.10.11 · darwin
```

The commit beside the version is what built it. After a version bump an
editable install's metadata goes stale; the line says so and names the fix,
`pipx install --editable . --force`.

## Reporting something that looks wrong

Add `--debug` to the command and paste the block it prints beneath the output:
what the run read, ran and resolved, ending with `reproduce` lines. It names
records without copying their text, so it is safe to share and readable on the
machine that made it.

```
--- chsum debug ---
invocation: chsum digest ch_8b0a671d… --stdout --debug
files (1)
  ch_8b0a671d…  meta,turns  947.1K  671 recs  …/9a9e9ac5-….jsonl
steps (3)
  resolve_ref    via=argv ref=ch_8b0a671d…
reproduce
  chsum digest ch_8b0a671d… --stdout
--- end chsum debug ---
```

## Prose, and where it's allowed

`recap`'s timeline is the one model-written output, through `claude -p --model
haiku` with your existing Claude Code login. Any summariser gets the extracted
material only, and its output sits beneath the verbatim record so each sentence
can be checked against it.

## Implementation details

### Transcripts and digests

Claude Code writes every session to disk as a transcript, one record per line:
each message, each tool call and its result, and a link from each record to the
one before it. A subagent writes a transcript of its own beside its parent's.
chsum reads these files directly. Codex CLI records its sessions in a different
shape, so chsum translates each one into Claude Code's shape once and reads the
copy; every view then works the same for both.

A digest is built by walking a transcript from start to end. The fields each
record carries separate your typed prompts from tool results and harness text,
and the files, commands and agents in between are counted from the tool calls
themselves. Nothing is generated along the way, so every line in the output
points back to a line in a transcript, and the locators printed beside each row
are those line numbers.

### Branches

A rewind leaves the earlier path in place: the edited prompt is written as a
second record hanging off the same parent. Following the parent links from
each record that nothing points back to traces every path through the
conversation, and paths that share their typed prompts up to a point are
grouped where they part. That grouping is the branch list.

### Checkpoints and undo

Checkpoints come from a hook that runs after each tool call. Where the working
tree differs from the last checkpoint, the hook records the tree as a git
commit under a ref of the session's own, beside your branches and never on
them. The difference between one commit and the next is what changed in the
files between those two calls, however the change was made.

Undo reads that difference back and applies it in reverse, and redo applies it
forward. Each undo and redo is recorded as a commit on the same chain, naming
the step it acted on, so the lists of steps in place and steps undone are
rebuilt from the chain alone. Git applies a change to every file or to none,
so a change whose lines were edited since is refused and the files stay as
they were.

### Subagents

A subagent writes its own transcript beside its parent's as it works, one record
at a time, so its progress is on disk before its report exists. chsum reads that
file directly. Each message the agent writes prints whole, and each tool call it
makes prints as a single line naming the tool and the file or command, so the
output grows with what the agent said rather than with the size of its edits
and command output. The parent session reads that progress when it runs the
command, and nothing reaches its context between runs.

### Marks

A mark is stored in chsum's own data directory, not in the transcript. When
`chsum mark` runs, it finds the newest message before the command in the
running session's transcript and records the reason beside that message's
location and its first line, filed under the turn it belongs to. A digest reads
these records for its session and prints each one under **Notable**, ahead of
everything else, with the message it points at quoted beneath it.

### Names

Session names and recap bullets live in the same data directory. The one write
chsum makes to a transcript is the title record a rename appends.
