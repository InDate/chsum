# Recap

`recap` is the one chsum command that ends in model-written text: a timeline
written by `claude -p --model haiku` from the verbatim record printed above it,
in a single clearly-labelled section. Everything above that section is copied.

## The live case

`chsum recap` with no arguments prints what's happened in this session since the
last typed prompt. It reads the transcript of the session it's run from, which
mid-turn is the one you're already inside, so it is the user's second-terminal
view of progress rather than a command to run on your own turn.

## A window of another session

`chsum recap <ref> --messages N M` reloads that window: the user's turns in it
verbatim, with a timeline sliced under each one. `1` is their first turn and
`-1` their last, in either order, and the numbers are the ones `chsum digest
--messages` takes, so a window found in a digest runs here unchanged.

- No numbers — the window runs from the turn after the last one already
  recapped; `--full` takes the whole session.
- `--dry-run` prices the call without making it. `0 calls would be made` means
  the window is already stored and rerunning it is free.
- Each turn's bullets are kept under `~/.local/share/chsum/turns/` once that
  turn's gap has closed. `--no-cache` bypasses the store in both directions,
  `--invalidate` replaces what's stored for the window.
- `chsum recap --last` recaps the most recent session that isn't this one.
- `chsum recap --list` shows the bullets already written, with the ids
  `chsum note --delete` takes.
