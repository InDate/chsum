#!/usr/bin/env python3
"""Launcher for the `chsum` package sitting beside this file.

A package directory cannot be run by path, and two callers need one that can:
the plugin's hooks name a file for a session with no `chsum` on PATH, and CI
runs `python3 chsum.py --help` to read the command list off the parser. Both
reach the package through here.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chsum.core import main  # noqa: E402 — after the path is set

raise SystemExit(main())
