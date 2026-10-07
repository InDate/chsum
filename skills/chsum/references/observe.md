# Observe: what goes in, what comes out

An observer is a base conversation holding a skill directory as its system
prompt and the watched files as its first turn. chsum denies no call itself: a
call reaches review only through a hook the repo carries, and a review ends
only in the calls the skill directory approves.

## Starting one

```sh
chsum observe start --skill <dir> --match '<regex>' --model opus   # from the root
chsum observe preview --skill <dir> --match '<regex>'              # what start sends, no model call
chsum observe reload --name <name>                                 # rebuild an expired base
```

`--root` defaults to the working directory; `--match` is a full match on the
root-relative path. `--name` defaults to the skill directory's name. An
observer is found again by root and name, so `status`, `reload` and `stop`
run from the root or carry `--root`.

## Input: the repo's hook

A PreToolUse hook in the repo's `.claude/settings.json` holds the selection:
which tools and which paths go to review. Every call outside it runs without
one. A call inside it goes to `chsum observe hold` with the hook's payload on
stdin, unchanged.

`hold` prints one JSON object:

| Field | Value |
|---|---|
| `decision` | `deny` for a call now held; `allow` for a rerun of a call passed earlier |
| `reason` | with `deny`: the text the hook returns as `permissionDecisionReason` |
| `edit` | the edit number, for `chsum observe show <n>` |

The hook returns `deny` with that reason, and returns nothing on `allow`, so
the call runs. `hold` exits 1, with the cause on stderr, for a payload with no
`tool_name`, a root with no observer, an expired base, and an Edit whose
`old_string` is absent from the file. A hook that lets the call run on exit 1
keeps a stopped observer from blocking work.

A Write, Edit or MultiEdit to a watched file is held as the file's text after
the call, shown to the reviewer as a diff. Any other call is held as its tool
and input, shown as JSON, on the observer at the payload's `cwd` (or
`--root`, `--name`).

`.claude/hooks/observe.py` in the chsum repo is a working hook: it holds
Write, Edit and MultiEdit under one folder, and Bash commands naming it.

## Review

`hold` starts a reviewer: a background session forked from the base, so it
reads the base's prefix from cache. The reviewer messages the calling session
over cross-session messaging, and the exchange runs to an accept, a decline
or a pass. A held call stays open for an hour; past that, the next chsum
hook closes it as dropped. While one edit on a file is open, a second Write
or Edit to that file from the same session returns the same edit, and its
change is lost: send the next change after the first closes.

## Output: what the reviewer runs

`allowed-tools.txt` in the skill directory lists the reviewer's approved
calls, one permission rule per line, `{file}` standing for the held file's
absolute path:

```
Read(/{file})
Write(/{file})
Edit(/{file})
Bash(npm run lint:*)
```

With no such file, the list is the three `{file}` rules above. A rule naming
`{file}` drops out for a held call with no file. SendMessage and
`chsum observe close` are approved on every list. The rules stay within the
base's tools, SendMessage, Bash, Read, Write and Edit: a fork sending another
tool list misses the base's cached prefix. The file sits in the skill
directory, so it also reaches the base's system prompt, and editing it takes
a `reload`.

The reviewer ends every edit with `chsum observe close <n> --title '<name>'`.
The outcome follows from the files:

| Outcome | Where it comes from |
|---|---|
| applied | the held file changed since the hold; for a call with no file, any watched file differs from the base. The base takes the change as a diff turn. |
| dropped | nothing changed |
| passed | `close --pass`: the call holds to the rules and the approved calls cannot carry it out. The calling session's rerun of the same call, same tool and input, runs once and the edit reads `released`. |

A changed rerun matches no clearance and is held again.

## Reading it

```sh
chsum observe --follow --name <name>   # every hold, message and close as it happens
chsum observe show --name <name>       # every edit with its outcome
chsum observe show <n> --name <name>   # one exchange whole
chsum observe status --name <name>     # cache, drift, growth
```

The status line's `cache NK` is the base's size. A fork's messages land in the
fork's own transcript, so the number holds through an exchange and rises by
one diff turn per applied edit.
