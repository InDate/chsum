# Contributing

`CLAUDE.md` is the design brief — the rules a change has to hold to, and why.
Read it before changing `chsum.py`. This file is the mechanics.

## Setup

```sh
pipx install --editable .
```

`chsum.py` in this checkout is then what runs, from anywhere. Python ≥3.10,
stdlib only — `dependencies` in `pyproject.toml` stays empty, which is what lets
chsum run against any corpus with no venv. `claude-history` is a runtime
requirement but a separate binary; only the session listing works without it.

## Checking a change

There is no test suite. Compile, then run the commands the change could reach:

```sh
python3 -m compileall -q chsum.py
chsum                      # listing
chsum last                 # digest, incl. a subagent one if the session had any
chsum journal --since 7d
chsum mark --list          # from inside Claude Code — marks need a live session
```

Read the output rather than checking it exits 0. Every line lands in a future
context window, so a wrong or unmarked-truncated one is the failure mode.

## Releasing

The version lives in **two** files — `pyproject.toml` and the
`skills/chsum/SKILL.md` frontmatter. Both ship, and `publish.yml` refuses a tag
that disagrees with either.

```sh
# bump both, commit, then
git tag v1.0.4 && git push origin v1.0.4
```

The tag publishes to PyPI via trusted publishing (no stored token) and asks
[InDate/indate-tools](https://github.com/InDate/indate-tools) to repin. The
marketplace opens a PR: publishing and pushing an update to installed users are
separate decisions.

PyPI version numbers cannot be reused. A bad release is fixed by the next one.
