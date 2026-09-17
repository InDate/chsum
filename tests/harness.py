"""A throwaway git repository per test, and the calls that drive it.

Every test here writes to a temporary directory and nothing else. The git calls
are bound with `-C <repo>` rather than a working directory, so a test that
resolves its path wrongly fails on that repo instead of reaching the checkout it
runs from.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chsum import checkpoints  # noqa: E402


def git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    """One git call against `repo`, raising where it fails — a test that cannot
    set its fixture up must not go on to assert about it."""
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


class RepoCase(unittest.TestCase):
    """A test with a repository of its own, removed when it ends."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="chsum-test-")
        self.repo = pathlib.Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "chsum tests")
        # An identity of its own, so a global `commit.gpgsign` cannot stall the
        # suite waiting for a passphrase.
        git(self.repo, "config", "commit.gpgsign", "false")
        self.write("f.txt", "line 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.addCleanup(self._tmp.cleanup)

    # -- fixture -----------------------------------------------------------

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def enable(self) -> None:
        """Opt this repo in, the way `chsum checkpoints --enable` does."""
        git_dir = checkpoints._git_dir(self.repo)
        assert git_dir is not None
        (git_dir / checkpoints.GATE_NAME).write_text("enabled\n")

    def checkpoint(self, session: str, call: str) -> None:
        """One checkpoint, through the same function the hook calls."""
        git_dir = checkpoints._git_dir(self.repo)
        assert git_dir is not None
        checkpoints._write_checkpoint(self.repo, git_dir, session, call)

    def plant_reflog_checkpoint(self, session: str, when: str, call: str) -> str:
        """A checkpoint in the shape chsum wrote before chains existed: commit
        onto HEAD, then reset away, leaving it in `HEAD`'s reflog alone."""
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--no-verify", "-m",
            f"{checkpoints._CHECKPOINT_PREFIX}{session} @ {when} {call}")
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "reset", "-q", "--soft", self.base)
        return sha

    # -- observation -------------------------------------------------------

    def chain(self, session: str) -> list[str]:
        """The subjects on a session's chain, newest first."""
        ref = checkpoints.checkpoint_ref(session)
        proc = subprocess.run(["git", "-C", str(self.repo), "log", "--format=%s", ref],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return []
        return [l for l in proc.stdout.splitlines()
                if l.startswith(checkpoints._CHECKPOINT_PREFIX)]

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def status(self) -> str:
        return git(self.repo, "status", "--porcelain").stdout

    def object_exists(self, sha: str) -> bool:
        """Whether git still holds an object. A pruned commit is the difference
        between a checkpoint that was kept and one that was collected."""
        return subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", sha],
                              capture_output=True).returncode == 0

    def collect_garbage(self) -> None:
        """Expire everything unreachable and prune it — what a `git gc` does to
        a checkpoint nothing references."""
        git(self.repo, "reflog", "expire", "--expire=now",
            "--expire-unreachable=now", "--all")
        git(self.repo, "gc", "--prune=now", "-q")
