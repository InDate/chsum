"""chsum: work logs and reload-ready context from coding-agent conversations.

The package keeps the module name the hooks import. `chsum.core` holds the
commands and the readers; `chsum.sources` holds the session formats and the
translation each one needs to reach the common record shape.

Everything `core` exposed as a module attribute stays reachable here, so
`import chsum` followed by `chsum._hook_post_tool_use(...)` reads the same as
it did when the whole tool was one file.
"""
from __future__ import annotations

import pathlib

from .core import *  # noqa: F401,F403 — the CLI's whole surface
from .core import (  # noqa: F401 — named so the hooks and the console script resolve
    _hook_post_tool_use,
    _hook_session_start,
    _read_payload,
    main,
)

__all__ = ["main", "entrypoint"]


def entrypoint() -> str:
    """The file to invoke where no `chsum` command sits on PATH. A package
    directory cannot be run by path, so this names the launcher beside it,
    which a plugin ships and a hook hands to Claude Code."""
    launcher = pathlib.Path(__file__).resolve().parent.parent / "chsum.py"
    return str(launcher)
