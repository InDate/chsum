"""The cache clock `chsum cache`, the hooks and `chsum warm` share, and the ping.

A ping runs as `claude -p --resume … --no-session-persistence`, so it leaves the
transcript as it was. The clock reads the transcript's last request and the
ping log beside it; the fixtures below write both into a temporary directory.
"""
from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chsum import core  # noqa: E402

SESSION = "66666666-7777-8888-9999-000000000000"
FIVE_MIN = {"ephemeral_5m_input_tokens": 120, "ephemeral_1h_input_tokens": 0}


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request(t0: float, reply_secs: float) -> list[dict]:
    """A prompt sent at `t0` and a reply written as two blocks, the last
    landing `reply_secs` later, each block carrying the request's usage."""
    usage = {"input_tokens": 5, "cache_read_input_tokens": 20_000,
             "cache_creation_input_tokens": 120, "cache_creation": FIVE_MIN}
    msg = {"role": "assistant", "id": "msg_1", "model": "claude-haiku-4-5-20251001",
           "usage": usage}
    return [
        {"type": "user", "uuid": "p1", "parentUuid": None, "sessionId": SESSION,
         "timestamp": _ts(t0), "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "p1", "sessionId": SESSION,
         "timestamp": _ts(t0 + reply_secs / 2),
         "message": {**msg, "content": [{"type": "thinking", "thinking": ""}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "sessionId": SESSION,
         "timestamp": _ts(t0 + reply_secs),
         "message": {**msg, "content": [{"type": "text", "text": "done"}],
                     "stop_reason": "end_turn"}},
    ]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="chsum-cache-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        for name, value in (("CHSUM_DIR", self.root / "data"),
                            ("PINGS_DIR", self.root / "data" / "pings")):
            patcher = mock.patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.t0 = 1_791_000_000.0

    def transcript(self, records: list[dict]) -> pathlib.Path:
        path = self.root / f"{SESSION}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path


class RequestStart(Case):
    def test_clock_starts_at_the_record_the_request_carried(self) -> None:
        records = _request(self.t0, reply_secs=90)
        last, lifetime = core._cache_basis(records)
        self.assertEqual(lifetime, 300)
        self.assertAlmostEqual(core._request_start(records, last), self.t0, places=2)

    def test_first_block_bounds_the_start_where_the_parent_is_missing(self) -> None:
        records = _request(self.t0, reply_secs=90)[1:]
        last, _ = core._cache_basis(records)
        self.assertAlmostEqual(core._request_start(records, last), self.t0 + 45, places=2)


class PingLog(Case):
    def test_no_log_reads_as_no_ping(self) -> None:
        self.assertEqual(core._last_ping(SESSION), 0.0)

    def test_newest_logged_ping_is_returned(self) -> None:
        core._log_ping(SESSION, self.t0 + 100, {"cache_read_input_tokens": 20_000})
        core._log_ping(SESSION, self.t0 + 250, {"cache_read_input_tokens": 20_000})
        self.assertEqual(core._last_ping(SESSION), self.t0 + 250)

    def test_a_torn_last_line_falls_back_to_the_one_before(self) -> None:
        core._log_ping(SESSION, self.t0 + 100, {})
        with (core.PINGS_DIR / f"{SESSION}.jsonl").open("a") as f:
            f.write('{"start": ')
        self.assertEqual(core._last_ping(SESSION), self.t0 + 100)


class CacheRow(Case):
    def test_age_runs_from_the_request_start(self) -> None:
        records = _request(self.t0, reply_secs=90)
        row = core._cache_row(records, self.t0 + 200, SESSION)
        self.assertEqual(row["age"], 200)
        self.assertFalse(row["pinged"])

    def test_a_logged_ping_restarts_the_clock(self) -> None:
        records = _request(self.t0, reply_secs=1)
        core._log_ping(SESSION, self.t0 + 280, {"cache_read_input_tokens": 20_000})
        row = core._cache_row(records, self.t0 + 400, SESSION)
        self.assertEqual(row["age"], 120)
        self.assertTrue(row["pinged"])
        self.assertTrue(core._cache_state(row).startswith("warm"))

    def test_a_row_without_a_session_reads_no_pings(self) -> None:
        records = _request(self.t0, reply_secs=1)
        core._log_ping(SESSION, self.t0 + 280, {})
        row = core._cache_row(records, self.t0 + 400)
        self.assertEqual(row["age"], 400)
        self.assertFalse(row["pinged"])


class PromptHook(Case):
    def run_hook(self, now: float) -> str:
        path = self.transcript(_request(self.t0, reply_secs=1))
        out = io.StringIO()
        with mock.patch.object(core.time, "time", return_value=now), redirect_stdout(out):
            code = core._hook_user_prompt_submit(
                {"session_id": SESSION, "transcript_path": str(path)})
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_a_prompt_after_expiry_is_held_once(self) -> None:
        held = json.loads(self.run_hook(self.t0 + 400))
        self.assertEqual(held["decision"], "block")
        self.assertEqual(self.run_hook(self.t0 + 401), "")

    def test_a_ping_inside_the_lifetime_lets_the_prompt_through(self) -> None:
        core._log_ping(SESSION, self.t0 + 280, {"cache_read_input_tokens": 20_000})
        self.assertEqual(self.run_hook(self.t0 + 400), "")


def _reply(usage: dict | None = None, *, is_error: bool = False, result: str = "ok",
           code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    body = {"type": "result", "is_error": is_error, "result": result}
    if usage is not None:
        body["usage"] = usage
    return subprocess.CompletedProcess([], code, stdout=json.dumps(body), stderr=stderr)


class WarmPing(unittest.TestCase):
    def ping(self, proc: subprocess.CompletedProcess) -> dict:
        with mock.patch.object(core.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(core.subprocess, "run", return_value=proc):
            return core._warm_ping(SESSION, "/work", "claude-haiku-4-5-20251001", 300)

    def test_a_hit_returns_its_usage(self) -> None:
        usage = {"input_tokens": 3, "cache_read_input_tokens": 20_000,
                 "cache_creation_input_tokens": 0}
        self.assertEqual(self.ping(_reply(usage)), usage)

    def test_a_hook_block_with_zero_usage_is_an_error(self) -> None:
        zeros = {"input_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        out = self.ping(_reply(zeros, result="UserPromptSubmit operation blocked by hook"))
        self.assertIn("blocked by hook", out["error"])

    def test_is_error_is_an_error(self) -> None:
        usage = {"input_tokens": 3, "cache_read_input_tokens": 20_000}
        out = self.ping(_reply(usage, is_error=True, result="rate limited"))
        self.assertIn("rate limited", out["error"])

    def test_a_failed_exit_with_no_reply_carries_stderr(self) -> None:
        proc = subprocess.CompletedProcess([], 1, stdout="", stderr="No conversation found")
        self.assertIn("No conversation found", self.ping(proc)["error"])


class PingWait(unittest.TestCase):
    def test_wait_spans_half_to_fifty_eight_sixtieths(self) -> None:
        with mock.patch.object(core.random, "uniform", side_effect=lambda a, b: a):
            self.assertEqual(core._ping_wait(3600), 1800)
        with mock.patch.object(core.random, "uniform", side_effect=lambda a, b: b):
            self.assertAlmostEqual(core._ping_wait(3600), 3480)


class Warm(Case):
    def test_pings_are_logged_and_a_failure_stops_the_run(self) -> None:
        path = self.transcript(_request(self.t0, reply_secs=1))
        clock = [self.t0 + 10]
        hit = {"input_tokens": 3, "cache_read_input_tokens": 20_000,
               "cache_creation_input_tokens": 0}
        replies = iter([hit, {"error": "exit 0, blocked"}])
        out = io.StringIO()
        with mock.patch.object(core, "path_for_ref", return_value=path), \
                mock.patch.object(core, "_transcript_cwd", return_value="/work"), \
                mock.patch.object(core, "_warm_ping", side_effect=lambda *a: next(replies)), \
                mock.patch.object(core, "_ping_wait", return_value=200), \
                mock.patch.object(core.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(core.time, "sleep",
                                  side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                redirect_stdout(out):
            code = core.cmd_warm(types.SimpleNamespace(ref=SESSION, duration=""))
        self.assertEqual(code, 1)
        log = [json.loads(line) for line in
               (core.PINGS_DIR / f"{SESSION}.jsonl").read_text().splitlines()]
        self.assertEqual(len(log), 1)
        self.assertAlmostEqual(log[0]["start"], self.t0 + 200, delta=1)
        self.assertIn("ping read 20,000", out.getvalue())
        self.assertIn("ping failed: exit 0, blocked", out.getvalue())


if __name__ == "__main__":
    unittest.main()
