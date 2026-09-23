# Contributing

`README.md` is the user-facing doc. This file is the mechanics.

## Setup

```sh
pipx install --editable .
```

The `chsum` package in this checkout is then what runs, from anywhere. Python
≥3.10, stdlib only — `dependencies` in `pyproject.toml` stays empty, which is
what lets chsum run against any corpus with no venv. `claude-history` is a
runtime requirement but a separate binary; only the session listing works
without it.

## Layout

```
chsum.py              launcher: a package directory cannot be run by path, and
                      the plugin's hooks and CI both need one that can
chsum/
  __init__.py         what `import chsum` gives the hooks; `main`, `entrypoint`
  __main__.py         `python3 -m chsum`
  core.py             the commands, the readers, the renderers
  checkpoints.py      the git layer: one commit per call that changed the tree
  sources/
    __init__.py       the registry, and the translated-transcript cache
    claude.py         `~/.claude/projects`, read where it lies
    codex.py          `~/.codex/sessions`, translated to the common shape
```

`checkpoints.py` holds the git plumbing and imports nothing from `core` — the
dependency runs one way, and `core` installs its tracer with `set_tracer` rather
than the layer reaching back for it. The views that render checkpoints and the
`checkpoints` command stay in `core`.

Adding a tool means one file under `sources/` and one `register(...)` call:
where its sessions sit, which directory each ran in, and a translation to the
record shape `core` reads. Nothing in `core` names a format.

A translated file is cached against its original's size and mtime **and** the
bytes of the translator module. Editing `codex.py` therefore rebuilds every
Codex translation on the next run; without that, a corrected rule would serve
its old output forever, since a finished session is never written to again.

## Checking a change

```sh
python3 -m unittest discover -s tests -t tests
```

Stdlib `unittest`, no test dependency, ~10s. Each git test builds a throwaway
repository under `tempfile` and binds every call with `git -C <repo>`, so a test
that resolves a path wrongly fails against that repository rather than reaching
the checkout it runs from. `publish.yml` runs the suite before it builds.

The suite covers what fails outside chsum — a moved `HEAD`, a disturbed index, a
lost history — and the translation and view rules that were measured rather than
assumed. It does not cover the renderers' prose. Compile as well, then run the
commands the change could reach:

```sh
python3 -m compileall -q chsum chsum.py hooks
chsum                      # listing
chsum digest --last        # digest, incl. a subagent one if the session had any
chsum --since 7d -n 0     # listing over a window
chsum --all --source codex # another tool's sessions, translated on the way in
chsum find "…" --all       # search, which runs one pass per corpus and merges
chsum mark --list          # from inside Claude Code — marks need a live session
```

Read the output rather than checking it exits 0. Every line lands in a future
context window, so a wrong or unmarked-truncated one is the failure mode.

## Releasing

The version lives in `pyproject.toml`, and `publish.yml` refuses a tag that
disagrees with it.

```sh
# bump it, commit, then
git tag v1.0.4 && git push origin v1.0.4
```

The tag publishes to PyPI via trusted publishing (no stored token) and asks
[InDate/indate-tools](https://github.com/InDate/indate-tools) to repin. The
marketplace opens a PR: publishing and pushing an update to installed users are
separate decisions.

PyPI version numbers cannot be reused. A bad release is fixed by the next one.
