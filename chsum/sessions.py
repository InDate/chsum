"""Claude Code's registry of running local sessions: one JSON file per process
under `~/.claude/sessions/<pid>.json`, holding the session's id, its messaging
name and the socket its messages arrive on. A session that has ended leaves the
registry, so every lookup here answers for sessions running now.

A message reaches a session at `uds:<socket>`, the address `SendMessage` takes.
"""
from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Iterator

REGISTRY = pathlib.Path.home() / ".claude" / "sessions"


def _entries() -> Iterator[dict]:
    for f in REGISTRY.glob("*.json"):
        try:
            d = json.loads(f.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            yield d


def address(session_id: str) -> str:
    """The `SendMessage` address of the running session with this id, '' where
    no running session carries it."""
    for d in _entries():
        if d.get("sessionId") == session_id and d.get("messagingSocketPath"):
            return f"uds:{d['messagingSocketPath']}"
    return ""


def socket_name(socket: str) -> str:
    """The messaging name of the running session listening on `socket`, ''
    where no running session lists it."""
    for d in _entries():
        if d.get("messagingSocketPath") == socket:
            return str(d.get("name") or "")
    return ""


def by_name(target: str) -> dict | None:
    """A running session's registry entry by its messaging name. A `[ref]`
    suffix is dropped: the name alone addresses a session where no other
    shares it, and two matches return None."""
    name = re.sub(r"\s*\[[^\]]*\]\s*$", "", target)
    hits = [d for d in _entries() if d.get("name") == name and d.get("sessionId")]
    return hits[0] if len(hits) == 1 else None
