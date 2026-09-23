# Naming a session

Rename a session with `chsum name "<title>"`. The title is argv, quoted
verbatim, and lands in chsum's store plus one `ai-title` record in the
transcript, so it shows in every chsum view (flagged `✎`) and in `/resume`.

- `chsum name <ch_ref> "<title>"` — rename a past session rather than this one.
- `chsum name --list` — this project's renamed sessions; `--all` every project.
- `chsum name --clear` — back to Claude Code's own title.
- `chsum name --no-resume` — rename in chsum only, leaving the transcript and
  `/resume` untouched.

Claude Code re-titles the running session as the conversation grows, so
`/resume` may drift back to its own title even though chsum keeps the user's.
