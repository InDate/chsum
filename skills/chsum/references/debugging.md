# When output looks wrong

Append `--debug` to any chsum command. Beneath the normal output it prints what
that run read (transcripts and sidecars, with refs and record counts), ran
(subprocesses with exit codes), and resolved (each step with its inputs and
result, including the fallbacks a normal run prints nothing about).

No transcript text is copied — the block names records rather than carrying
them, so it assumes the reader is on the same machine.

Ask the user to re-run the failing command with `--debug` on the end and paste
the block. Its `reproduce` lines are the command to run again and, where one was
resolved, the `claude-history agent read` that opens the conversation.
