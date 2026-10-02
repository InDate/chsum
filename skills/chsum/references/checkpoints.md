# Git checkpoints

## The chain

One commit per tool call that changed the tree, chained under
`refs/chsum/<session-uuid>`; a subagent chains under its parent's uuid.
`commit-tree` and `update-ref` write it, so `HEAD`, the index, the working tree
and commit hooks are untouched, and `git log` and `git status` show nothing. The
ref survives `gc`, reflog expiry and worktree removal.

A session and its agents share one tree and one ref, so a change lands in the
checkpoint of whichever call's hook commits next. Counts hold; attribution can
be off by a neighbouring call.

## A row to its diff

Rows in `--messages`, `--tools` and `--commands` open with a call id; the
checkpoint that call wrote ends its subject with `toolu_<id>`.

```sh
git log --format='%H %s' $(git for-each-ref --format='%(refname)' refs/chsum/) | grep <call-id>
git show <sha>                     # that call's change
git diff <first-sha>^ <last-sha>   # a span: a turn, or an agent's work
```

A call that changed nothing has no checkpoint.

## Undo and redo

`chsum undo` applies a step's diff reversed through `git apply`, which writes
every file of the step or none, then commits a checkpoint whose subject ends
`undo <step-stamp>`, or `undo <step-stamp>/<letter>` for one file. `chsum redo`
does the same forward. Both lists are rebuilt from these subjects.

## Retention

`chsum checkpoints` lists the chains. `--prune 7d` drops chains older than that,
leaving transcripts alone. `--migrate` moves pre-chain checkpoints out of the
reflog onto chains, so `gc` keeps them.

## The opt-in

The hook records nothing until `.git/chsum-checkpoint` reads `enabled`. Where
the file is absent, a SessionStart message carries the ask: ask the user once
and write `enabled` or `declined`. Edit or delete the file to change the answer.
