# Noting

Run `chsum note "<text>"` as a Bash call; the user types `! chsum note "…"`.
Write the note to be read cold months later, not as a label.

## An earlier moment

- `--match "<phrase>"` — ignores case, punctuation and markdown; several matches
  list candidates for `--at`.
- `--recent 20` — the latest messages and calls, with the ids `--at` takes.

## An agent's words

`--match`, `--recent` and a bare `chsum note` search running agents too. A note
an agent makes folds into the parent, tagged `agent <id>`: tell agents to note
what they find, since their digest is thin. The user's `!` reaches the session,
not the agent, so note an agent's words from the session with `--match`.

## Listing, locating, deleting

- `--list` — notes with ids (`--full` for whole messages); `chsum recap --list`
  for recap bullets; `--all` for every project.
- `--delete <id>` — removes either kind. Delete a user's note only when asked.
- `--show <id>` — its file, row, time and agent, then the message
  (`--context N` for neighbours). Use it in place of searching a transcript by
  hand.

claude-history, registered as an annotator (`chsum annotations`), reads and
writes the same notes.
