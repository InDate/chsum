"""What a rewind cut, `chsum resume`, the handoff's delivery, the status line,
the binary a resume runs on, and Claude Code's registry of running sessions.

The fixture is one conversation: a first turn, a branch that reads a file and
replies, and a rewind back to the first turn's reply. A request's usage carries
the prefix it sent, which the status line's arithmetic reads. No test runs a
model: where a side request could start one, `subprocess.run` raises.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chsum import core, sessions  # noqa: E402
from harness import git  # noqa: E402

SESSION = "aaaaaaaa-0000-4000-8000-000000000001"


def _ts(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Conversation:
    """Records stamped from `t0` forward, one second apart unless told."""

    def __init__(self, t0: float, cwd: str = "/work/proj") -> None:
        self.t, self.cwd = t0, cwd

    def _rec(self, uid, parent, kind, content, gap=1.0, **extra) -> dict:
        self.t += gap
        return {"type": kind, "uuid": uid, "parentUuid": parent, "sessionId": SESSION,
                "timestamp": _ts(self.t), "cwd": self.cwd,
                "message": {"role": kind, "content": content}, **extra}

    def prompt(self, uid, parent, text, gap=1.0):
        return self._rec(uid, parent, "user", text, gap, origin={"kind": "human"})

    def request(self, uid, parent, size, content, version="2.1.292"):
        rec = self._rec(uid, parent, "assistant", content, version=version)
        rec["message"].update(id=f"msg_{uid}", model="claude-opus-5-5", usage={
            "input_tokens": 0, "cache_read_input_tokens": size - 100,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_1h_input_tokens": 100}})
        return rec

    def reply(self, uid, parent, size, text):
        return self.request(uid, parent, size, [{"type": "text", "text": text}])

    def call(self, uid, parent, size, tool_id, name="Read", **inp):
        return self.request(uid, parent, size, [{"type": "tool_use", "id": tool_id,
                                                 "name": name, "input": inp}])

    def result(self, uid, parent, tool_id):
        return self._rec(uid, parent, "user", [{"type": "tool_result", "tool_use_id": tool_id,
                                                "content": "ok"}], toolUseResult={"stdout": "ok"})

    def command(self, uid, parent, text):
        return self._rec(uid, parent, "user", f"<bash-input>{text}</bash-input>")


def _fixture(t0: float) -> tuple[Conversation, list[dict]]:
    """p1 → a1; the branch p2 → c2 → r2 → a2 reads a file; nothing written
    past a2 yet, so the newest record is the cut branch's tip."""
    c = Conversation(t0)
    return c, [
        c.prompt("p1", None, "start"),
        c.reply("a1", "p1", 30_000, "started"),
        c.prompt("p2", "a1", "read the file"),
        c.call("c2", "p2", 40_000, "toolu_read", file_path="/work/proj/f.py"),
        c.result("r2", "c2", "toolu_read"),
        c.reply("a2", "r2", 60_000, "it defines f"),
    ]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="chsum-rewind-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        for name, value in (("CHSUM_DIR", self.root / "data"),
                            ("PINGS_DIR", self.root / "data" / "pings")):
            patcher = mock.patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def transcript(self, records: list[dict]) -> pathlib.Path:
        path = self.root / f"{SESSION}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path

    def append(self, path: pathlib.Path, rec: dict) -> None:
        with path.open("a") as f:
            f.write(json.dumps(rec) + "\n")


class FindCut(Case):
    def test_a_rewind_cuts_the_branch_under_the_point_the_new_prompt_hangs_off(self) -> None:
        c, records = _fixture(1_791_000_000.0)
        records += [c.prompt("p3", "a1", "next"), c.reply("a3", "p3", 31_000, "ok")]
        cut = core._find_cut(records)
        self.assertEqual(cut.point["uuid"], "a1")
        self.assertEqual([[r["uuid"] for r in p][0] for p in cut.paths], ["p2"])
        self.assertEqual(cut.paths[0][-1]["uuid"], "a2")
        self.assertEqual(cut.since, 1)

    def test_a_rewind_past_an_earlier_one_cuts_both_branches_newest_first(self) -> None:
        c, records = _fixture(1_791_000_000.0)
        records += [c.prompt("p4", "a2", "first follow-up"), c.reply("a4", "p4", 61_000, "x"),
                    c.prompt("p5", "a2", "second follow-up"), c.reply("a5", "p5", 61_000, "y"),
                    c.prompt("p3", "a1", "next")]
        cut = core._find_cut(records)
        self.assertEqual(cut.point["uuid"], "a1")
        self.assertEqual([p[-1]["uuid"] for p in cut.paths], ["a5", "a4"])

    def test_a_chain_with_no_rewind_has_no_cut(self) -> None:
        _, records = _fixture(1_791_000_000.0)
        self.assertIsNone(core._find_cut(records))


class RewindStatus(Case):
    def test_the_cursor_stands_at_the_first_turn_past_the_rewind_that_ran_a_tool(self) -> None:
        c, records = _fixture(1_791_000_000.0)
        records += [c.prompt("p3", "a1", "just talk"), c.reply("a3", "p3", 31_000, "talked"),
                    c.prompt("p4", "a3", "now run it"),
                    c.call("c4", "p4", 33_000, "toolu_run", name="Bash", command="ls"),
                    c.result("r4", "c4", "toolu_run"), c.reply("a4", "r4", 35_000, "ran")]
        status = core._rewind_status(records)
        self.assertEqual(status["cursor"], "now run it")
        self.assertEqual(status["cut"], 35_000 - 31_000)
        self.assertAlmostEqual(status["paid"], 0.05 * (2_000 + 4_000))

    def test_each_cut_saves_its_size_on_every_later_request_less_the_handoff(self) -> None:
        c, records = _fixture(1_791_000_000.0)
        records += [c.prompt("p3", "a1", "next")]
        records += [c.reply(f"a{i}", "p3" if i == 10 else f"a{i - 1}", 31_000, "x")
                    for i in range(10, 40)]
        status = core._rewind_status(records)
        cut, s = 60_000 - 30_000, core._HANDOFF_TOKENS
        handoff = (core._OUTPUT_RATE + 2.0) * s + 0.05 * 60_000
        self.assertAlmostEqual(status["saved"], 30 * 0.05 * (cut - s) - handoff)


class ResumeCut(Case):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(core, "_RESUME_WAIT", 3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _land_later(self, path: pathlib.Path, rec: dict) -> threading.Thread:
        """The command's records reach the transcript only after it exits."""
        thread = threading.Thread(target=lambda: (time.sleep(0.5), self.append(path, rec)))
        thread.start()
        self.addCleanup(thread.join)
        return thread

    def test_the_record_written_after_exit_names_the_point_and_the_cut(self) -> None:
        started = time.time()
        c, records = _fixture(started - 60)
        c.cwd = str(self.root)
        path = self.transcript(records)
        c.t = started
        self._land_later(path, c.command("m1", "a1", "chsum resume"))
        found = core._resume_cut(SESSION, path, started)
        self.assertEqual(found["point"], "a1")
        self.assertEqual(found["tip"], "a2")
        d = core._side_dir(SESSION)
        self.assertTrue((d / "a1.digest").exists())
        self.assertTrue((d / "resume.running").exists())

    def test_a_command_with_no_rewind_before_it_writes_the_reason(self) -> None:
        started = time.time()
        c, records = _fixture(started - 60)
        path = self.transcript(records)
        c.t = started
        self._land_later(path, c.command("m1", "a2", "chsum resume"))
        self.assertIsNone(core._resume_cut(SESSION, path, started))
        got = [json.loads(f.read_text()) for f in core._side_dir(SESSION).glob("*.json")]
        self.assertIn("no rewind preceded it", got[0]["error"])
        self.assertFalse((core._side_dir(SESSION) / "resume.running").exists())


class SideHandoffSkips(Case):
    def _run(self, records: list[dict], **patches) -> dict:
        path = self.transcript(records)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(core.subprocess, "run",
                                                  side_effect=AssertionError("a model call")))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(core, name, value))
            core._side_handoff({"session_id": SESSION, "transcript_path": str(path),
                                "tip": "a2", "point": "a1", "words": "start"})
        return json.loads((core._side_dir(SESSION) / "a1.json").read_text())

    def test_missing_input_writes_nothing(self) -> None:
        self.assertEqual(core._side_handoff({}), 0)
        self.assertFalse((core.CHSUM_DIR / "side-handoff").exists())

    def test_an_expired_branch_is_not_resumed(self) -> None:
        _, records = _fixture(time.time() - 3 * 3600)
        self.assertIn("expired", self._run(records)["error"])

    def test_a_version_no_longer_installed_is_not_resumed(self) -> None:
        _, records = _fixture(time.time() - 120)
        got = self._run(records, _claude_at=lambda version: "")
        self.assertIn("2.1.292", got["error"])
        self.assertIn("no longer installed", got["error"])


class Delivery(Case):
    def _dir(self) -> pathlib.Path:
        d = core._side_dir(SESSION)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_a_prompt_while_writing_carries_a_note(self) -> None:
        (self._dir() / "resume.running").write_text(json.dumps({"started": time.time()}))
        with mock.patch.object(sessions, "address", return_value=""):
            note = core._side_handoff_text({"session_id": SESSION}, prompt=True)
        self.assertEqual(note, "chsum: writing the rewind handoff; it goes in with the next prompt.")

    def test_a_ready_handoff_goes_in_once_with_its_digest(self) -> None:
        d = self._dir()
        (d / "a1.json").write_text(json.dumps({"point": "a1", "words": "start", "text": "## Changes\n- none",
                                               "read": 1, "wrote": 2, "output": 3}))
        (d / "a1.digest").write_text("# Rewound — x")
        first = core._side_handoff_text({"session_id": SESSION}, prompt=True)
        self.assertIn("# Rewound — x", first)
        self.assertIn("## Changes\n- none", first)
        self.assertEqual(core._side_handoff_text({"session_id": SESSION}, prompt=True), "")

    def test_a_handoff_sent_as_a_message_leaves_only_the_digest(self) -> None:
        d = self._dir()
        (d / "a1.json").write_text(json.dumps({"point": "a1", "words": "start", "sent": "uds:/x"}))
        (d / "a1.digest").write_text("# Rewound — x")
        got = core._side_handoff_text({"session_id": SESSION}, prompt=True)
        self.assertIn("# Rewound — x", got)
        self.assertIn("arrived as a message", got)

    def test_a_message_from_another_session_does_not_take_the_handoff(self) -> None:
        d = self._dir()
        (d / "a1.json").write_text(json.dumps({"point": "a1", "text": "## Changes"}))
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(core.observe, "on_hook"):
            core._hook_user_prompt_submit({"session_id": SESSION, "transcript_path": "/nope",
                                           "prompt": "<cross-session-message from=\"uds:/x\">hi"})
        self.assertEqual(out.getvalue(), "")
        self.assertTrue((d / "a1.json").exists())


class HandoffState(Case):
    def test_each_state_reads_off_the_files_the_side_request_leaves(self) -> None:
        d = core._side_dir(SESSION)
        d.mkdir(parents=True)
        now = time.time()
        self.assertEqual(core._handoff_state(SESSION, now), "")
        (d / "resume.running").write_text(json.dumps({"started": now - 12}))
        self.assertEqual(core._handoff_state(SESSION, now), "rewind handoff: writing · 12s")
        (d / "resume.running").write_text(json.dumps({"started": now - core._RUNNING_STALE - 1}))
        (d / "a1.json").write_text(json.dumps({"sent": "uds:/x"}))
        self.assertEqual(core._handoff_state(SESSION, now), "rewind handoff: sent as a message")
        (d / "a1.json").write_text(json.dumps({"text": "## Changes"}))
        self.assertEqual(core._handoff_state(SESSION, now),
                         "rewind handoff: ready, goes in with your next prompt")
        (d / "a1.json").rename(d / "a1.delivered")
        self.assertEqual(core._handoff_state(SESSION, time.time()), "rewind handoff: delivered")


class ClaudeAt(Case):
    def setUp(self) -> None:
        super().setUp()
        versions = self.root / "versions"
        versions.mkdir()
        for v in ("2.1.1", "2.1.2"):
            (versions / v).write_text("#!/bin/sh\n")
            (versions / v).chmod(0o755)
        self.link = self.root / "bin" / "claude"
        self.link.parent.mkdir()
        self.link.symlink_to(versions / "2.1.2")
        patcher = mock.patch.object(core.shutil, "which", return_value=str(self.link))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_kept_version_resolves_to_its_own_binary(self) -> None:
        self.assertEqual(core._claude_at("2.1.1"), str((self.root / "versions" / "2.1.1").resolve()))

    def test_a_version_no_longer_kept_resolves_to_nothing(self) -> None:
        self.assertEqual(core._claude_at("2.1.0"), "")

    def test_no_version_resolves_to_the_binary_on_path(self) -> None:
        self.assertEqual(core._claude_at(""), str(self.link))


class Registry(Case):
    def setUp(self) -> None:
        super().setUp()
        reg = self.root / "sessions"
        reg.mkdir()
        for pid, sid, name in ((1, "s-1", "chsum-3f"), (2, "s-2", "dup"), (3, "s-3", "dup")):
            (reg / f"{pid}.json").write_text(json.dumps(
                {"sessionId": sid, "name": name, "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock"}))
        (reg / "4.json").write_text("not json")
        patcher = mock.patch.object(sessions, "REGISTRY", reg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_address_socket_and_name_lookups(self) -> None:
        self.assertEqual(sessions.address("s-1"), "uds:/tmp/cc-socks/1.sock")
        self.assertEqual(sessions.address("s-9"), "")
        self.assertEqual(sessions.socket_name("/tmp/cc-socks/1.sock"), "chsum-3f")
        self.assertEqual(sessions.by_name("chsum-3f [ref]")["sessionId"], "s-1")
        self.assertIsNone(sessions.by_name("dup"))


class DiskState(Case):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "chsum tests")
        (self.repo / "a.py").write_text("one\n")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-q", "-m", "a")
        self.sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_the_file_against_the_branch_s_last_checkpoint_of_it(self) -> None:
        self.assertEqual(core._disk_state(self.repo, "a.py", self.sha), "on disk as the branch left it")
        (self.repo / "a.py").write_text("two\n")
        self.assertEqual(core._disk_state(self.repo, "a.py", self.sha), "changed on disk since the branch")
        (self.repo / "a.py").unlink()
        self.assertEqual(core._disk_state(self.repo, "a.py", self.sha), "gone from disk")

    def test_only_files_a_call_of_the_branch_names_are_kept(self) -> None:
        c = Conversation(1_791_000_000.0)
        files = {"a.py": {}, "b.py": {}, "c.py": {}}
        records = [c.call("w", None, 1, "t1", name="Write", file_path=str(self.repo / "a.py")),
                   c.call("x", "w", 1, "t2", name="Bash", command="python3 b.py > out.txt")]
        self.assertEqual(set(core._named_by_calls(self.repo, records, files)), {"a.py", "b.py"})


if __name__ == "__main__":
    unittest.main()
