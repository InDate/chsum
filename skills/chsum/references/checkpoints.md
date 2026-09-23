# Git checkpoints

The chain behind `chsum digest --writes` and the `±` marks: its shape, how a row
reaches the commit and the diff, retention, and the per-project opt-in.

## The chain

A checkpoint commits per tool call that changed the tree, chained under
`refs/chsum/<session-uuid>`. `commit-tree` builds the object and `update-ref`
publishes it, so `HEAD`, the index and the working tree are never written: a
checkpoint never becomes a branch tip, the user's commit hooks never fire, and
`git log` and `git status` show none of it. A ref is a gc root, so a chain
outlives a `git gc`, the reflog expiring, and the removal of the worktree it was
written in.

A subagent's calls chain under the parent session's uuid — one chain per
session, sidecars included.

## Tracing one change into git

Every row in `--messages`, `--tools` and `--commands` opens with the call id
(`01B4FF2EPx`), and each checkpoint commit closes its subject with that id as
`toolu_<id>`. That is the bridge from a row to the diff:

```sh
git log --format='%H %s' $(git for-each-ref --format='%(refname)' refs/chsum/) | grep <call-id>
git show <sha>                     # what that call changed — its parent is the checkpoint before it
git diff <first-sha>^ <last-sha>   # a span: a turn's first and last write, or an agent's
```

`git for-each-ref refs/chsum/` alone lists the chains with the session uuid each
one carries, matching the uuid in a digest's frontmatter and the short form in
every row locator. A call that changed nothing committed no checkpoint and has
no sha to find.

## Retention

`chsum checkpoints` lists the chains a repo holds, with a count and a date each.
`--prune 7d` drops chains whose last checkpoint is older than that, leaving the
transcripts untouched and the commits unreachable for the next `gc`. `--migrate`
rebuilds pre-chain checkpoints out of `HEAD`'s reflog, which makes them survive
a gc; a SessionStart hook raises this where a project holds reflog-only ones.

## The opt-in

The PostToolUse hook (`chsum hook post-tool-use`) ships installed and inert:
nothing is committed until a project opts in. Where `.git/chsum-checkpoint` is
absent, a SessionStart hook injects the ask, carrying the mechanism and the
wording — follow that message when it arrives, ask the user once, and write
`enabled` or `declined` into the gate file based on their answer.

To change a decision already made, edit `.git/chsum-checkpoint` directly — write
`enabled` or `declined` to flip it, or delete the file to get the ask again next
session.
