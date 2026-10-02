# Recap

Everything is copied except the last section: a timeline written by
`claude -p --model haiku`, labelled as model-written.

- `chsum recap` — this session since the last typed prompt. It reads the
  session it runs in, so it is the user's second terminal, not your turn.
- `chsum recap <ref> --messages N M` — a window of another session, turns
  numbered as `chsum digest --messages` numbers them.
- No numbers — from the turn after the last one recapped; `--full` the whole
  session; `--last` the newest other session.
- `--dry-run` — prices it. `0 calls would be made` means it is stored and free.
- Turns are stored under `~/.local/share/chsum/turns/`; `--no-cache` bypasses
  the store, `--invalidate` replaces it.
- `--list` — bullets written, with the ids `chsum note --delete` takes.
