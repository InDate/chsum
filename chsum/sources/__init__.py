"""The session formats chsum reads, and how each one reaches the common shape.

Every reader below this layer consumes one record shape: a JSON object per
line carrying `type`, `timestamp`, `message` and the tool parts beneath it.
That shape is the interface, not any one format's property.

chsum opens transcripts at around twenty sites, each by path, each numbering
the lines it reads, and drill-down output names those numbers in `sed` commands
the reader runs. A record shaped in memory would print a line number matching
no file, so a source already writing the common shape is read in place and
every other is written once to a file of its own.

A source supplies where its sessions sit, which directory each ran in, and a
translation. Registering it is the whole cost of adding a format.

A translated file is cached under the data home and rebuilt when its original
or the translator changes.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterator

# The source whose records already sit in the common shape, so its files are
# read where they lie. Named here so `source_of` has a value to return for a
# path outside the translated tree.
IN_PLACE = "claude"


@dataclass(frozen=True)
class Source:
    """One session format.

    `discover` yields the session files the format writes. `cwd_of` gives the
    directory a session ran in, which fixes its project. `session_id` gives the
    id it is addressed by, which names the translated file and so every ref and
    row label; the default reads the filename. `translate` yields common-shape
    records, and stays None where the format already writes them.
    """
    name: str
    label: str
    discover: Callable[[], list[pathlib.Path]]
    cwd_of: Callable[[pathlib.Path], str]
    translate: Callable[[pathlib.Path], Iterator[dict]] | None = None
    session_id: Callable[[pathlib.Path], str] = lambda path: path.stem
    # What the assistant is called in the lines naming it — "Last thing Codex
    # said". One tool's name over another's session misattributes the work.
    agent: str = "the assistant"

    @property
    def in_place(self) -> bool:
        """True where the format's own records are the common shape, so no
        translation stands between the reader and the file."""
        return self.translate is None


_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    """Add a format to the set every listing reads."""
    _REGISTRY[source.name] = source
    return source


def registry() -> dict[str, Source]:
    return dict(_REGISTRY)


def source_names() -> list[str]:
    return sorted(_REGISTRY)


def translated_root(data_home: pathlib.Path) -> pathlib.Path:
    """Where translated transcripts are written. Under the data home, beside
    the digests and names, since nothing here is read back by the tool that
    wrote the original."""
    return data_home / "translated"


def corpus_root(data_home: pathlib.Path, source_name: str) -> pathlib.Path:
    """One source's translated sessions, laid out as a config directory:
    `<root>/projects/<slug>/<id>.jsonl`. `claude-history` walks that shape and
    takes its root from `CLAUDE_CONFIG_DIR`, so a search pointed here covers
    this source's sessions with the same index and ranking the others get."""
    return translated_root(data_home) / source_name


def corpus_env(data_home: pathlib.Path, source_name: str) -> dict:
    """The environment that points `claude-history` at one source's corpus."""
    return {"CLAUDE_CONFIG_DIR": str(corpus_root(data_home, source_name))}


@lru_cache(maxsize=None)
def _translator_fingerprint(name: str) -> str:
    """Twelve hex over the bytes of the module translating this format. A
    rewritten rule fails the freshness comparison and rebuilds what it produced;
    without it a corrected rule serves its old output forever, since a finished
    session is never written to again."""
    module = _REGISTRY[name].translate.__module__ if name in _REGISTRY else ""
    path = getattr(sys.modules.get(module), "__file__", "")
    try:
        return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def _stamp(path: pathlib.Path, source_name: str) -> dict:
    st = path.stat()
    return {"source_path": str(path), "size": st.st_size, "mtime": st.st_mtime,
            "translator": _translator_fingerprint(source_name)}


def _fresh(sidecar: pathlib.Path, origin: pathlib.Path, source_name: str) -> bool:
    """True where the held translation was written from this exact file at this
    exact size and mtime, by this exact translator. An original that grew by
    one turn fails the comparison and is translated again."""
    try:
        held = json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    try:
        return held == _stamp(origin, source_name)
    except OSError:
        return False


def materialise(source: Source, origin: pathlib.Path, data_home: pathlib.Path,
                project_dir_name: Callable[[pathlib.Path], str]) -> pathlib.Path | None:
    """Write one session to disk in the common shape and return its path. A
    translation still matching its original is reused, so a listing over an
    unchanged corpus writes nothing.

    It is named for the session id and filed under the slug of the directory the
    session ran in — the layout the readers walk, where `path.stem` is the id
    and `path.parent.name` is the project. The write lands on a temporary name
    and moves into place, so an interrupted run leaves no half-written file.
    """
    cwd = source.cwd_of(origin) or "unknown"
    slug = project_dir_name(pathlib.Path(cwd))
    # `projects/` below the source: the layout a config directory has, so the
    # search binary reads this tree by pointing `CLAUDE_CONFIG_DIR` at it.
    out_dir = corpus_root(data_home, source.name) / "projects" / slug
    # Named for the session id: `path.stem` is the uuid every ref, row label
    # and note key is built from.
    stem = source.session_id(origin)
    # `<stem>.meta.json` is taken: subagent sidecars carry that suffix, and a
    # stamp answering to the same glob would be read as one.
    out = out_dir / f"{stem}.jsonl"
    sidecar = out_dir / f"{stem}.source.json"
    if out.exists() and _fresh(sidecar, origin, source.name):
        return out

    assert source.translate is not None  # an in-place source never reaches here
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".jsonl.partial")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in source.translate(origin):
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(out)
        sidecar.write_text(json.dumps(_stamp(origin, source.name)))
    except OSError:
        return None
    return out


def translated_transcripts(data_home: pathlib.Path,
                           project_dir_name: Callable[[pathlib.Path], str],
                           only: str = "") -> list[pathlib.Path]:
    """Every session of every translating source, written to disk in the common
    shape. `only` limits the walk to one source by name."""
    out: list[pathlib.Path] = []
    for source in _REGISTRY.values():
        if source.in_place or (only and source.name != only):
            continue
        for origin in source.discover():
            path = materialise(source, origin, data_home, project_dir_name)
            if path is not None:
                out.append(path)
    return sorted(out)


def source_of(path: pathlib.Path, data_home: pathlib.Path) -> str:
    """Which format a transcript came from, read off where it sits. A path
    under the translated root carries its source as the directory below that
    root; every other path is read in place and belongs to `IN_PLACE`."""
    try:
        rel = path.relative_to(translated_root(data_home))
    except ValueError:
        return IN_PLACE
    return rel.parts[0] if rel.parts else IN_PLACE


def translating_names() -> list[str]:
    """Every source that writes a translated corpus, in registration order."""
    return [s.name for s in _REGISTRY.values() if not s.in_place]


def label_of(name: str) -> str:
    source = _REGISTRY.get(name)
    return source.label if source else name


def agent_of(name: str) -> str:
    """What to call the assistant in a session this source wrote."""
    source = _REGISTRY.get(name)
    return source.agent if source else "the assistant"


# Registering on import is what makes a format visible to the readers. The
# imports sit at the foot of the module so each one finds `register` defined.
from . import claude as _claude  # noqa: E402,F401
from . import codex as _codex  # noqa: E402,F401
