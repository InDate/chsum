# Noting

Everything `chsum note` does beyond filing the moment it is run at, which
`SKILL.md` covers.

`chsum note` files a note in chsum's own store against the message it follows,
and prints nothing. Run it as `! chsum note "…"` typed by the user, or as a
normal Bash tool call by you. `chsum annotate` and `chsum mark` are the same
command.

`<text>` is free text, copied verbatim into the digest — write the note you'd
want to read cold months later, not a label.

## A moment further back

"Note that bit about the sidecars":

- `chsum note --match "<phrase from it>" "<text>"` — matching ignores case,
  punctuation, and markdown. Several matches and it lists candidates instead of
  guessing; pick one with `--at`.
- `chsum note --recent 20` lists the last 20 messages and tool calls with the
  record ids `--at` takes.

## From an agent

`--match`, `--recent` and a bare `chsum note` search the running agents' work
too, so something an agent just said is notable while it is still running.

Notes made by a subagent fold into the parent session, tagged `agent <id>` with
the row in that sidecar. Worth telling an agent to note what it finds: its
digest is thin, and a note survives into the parent's.

The user's `!` prefix reaches the session, not the agent they are addressing, so
a moment worth keeping from an agent is noted either by the agent itself, or
afterwards from the session with `--match "<phrase it said>"`.

## Listing, locating, deleting

`chsum note --list` shows this project's notes with their ids (`--full` for the
whole targeted message), and `chsum recap --list` the bullets `recap` wrote;
`--all` widens either to every project. `--delete <id>` takes either kind and
removes it from the store; delete a note the user made when they ask for it.

`chsum note --show <id>` takes the same id and prints where it landed — the
file, the row, the time, the agent — then the targeted message whole, with
`--context N` records either side. Use it before writing any code that walks a
transcript by hand: file and row are stamped into the note as it is made, so
this answers "where is this" without a search.

## claude-history

The same store is what claude-history reads and writes when it is registered as
an annotator there (`chsum annotations`, in the README). A note typed in its
viewer and one typed here are the same thing.
