"""A subagent's report, from the call that returns it to the rows that print it.

A report travels in up to three records: the agent's handback call in its own
transcript, a peer message in the parent where the harness delivered it as a
user record, and a task notification whose result points back at the peer
message. The parent keeps the peer message only for some returns — the rest
arrive as attachments, which no row reads — so each case below names which copy
a row is drawn from.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chsum import core  # noqa: E402

AGENT = "a1b2c3d4e5f6a7b8c"
SPAWN = "toolu_01Spawn"
PREAMBLE = ("[Subagent hand-back] The text below is the final report of a subagent "
            "this session delegated to. The report follows:\n")


def _user(ts: str, content, **extra) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": content},
            **extra}


def _handback(ts: str, report: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_{ts[-6:-1]}", "name": "SubagentHandback",
         "input": {"message": report}}]}}


def _pointer(tokens: int, tools: int, ms: int) -> str:
    return (f"<task-notification>\n<task-id>{AGENT}</task-id>\n"
            f"<tool-use-id>{SPAWN}</tool-use-id>\n<status>completed</status>\n"
            f"<summary>Agent \"review\" finished</summary>\n"
            f"<result>This agent's report was delivered to you as a message from "
            f"\"{AGENT}\" (its SubagentHandback call). Read it there; it is not "
            f"repeated here.\n</result>\n<usage><subagent_tokens>{tokens}</subagent_tokens>"
            f"<tool_uses>{tools}</tool_uses><duration_ms>{ms}</duration_ms></usage>\n"
            f"</task-notification>")


PARENT = [
    _user("2026-09-25T04:00:00.000Z", "review the route", origin={"kind": "human"}),
    {"type": "assistant", "timestamp": "2026-09-25T04:00:01.000Z", "message": {
        "role": "assistant", "content": [{"type": "tool_use", "id": SPAWN, "name": "Agent",
                                          "input": {"description": "review"}}]}},
    _user("2026-09-25T04:00:02.000Z",
          [{"type": "tool_result", "tool_use_id": SPAWN, "content": "launched"}]),
    # The first return reached the parent as an attachment: its pointer is left
    # in the queue record alone, and no peer message holds its report.
    {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-09-25T04:02:00.000Z",
     "content": _pointer(54154, 7, 56677)},
    _user("2026-09-25T04:03:00.000Z", "thanks", origin={"kind": "human"}),
    _user("2026-09-25T04:05:00.000Z",
          f"Another Claude session sent a message:\n<agent-message from=\"{AGENT}\">\n"
          f"{PREAMBLE}  Second report.\n  - indented once\n</agent-message>",
          isMeta=True, turnOrigin="peer",
          origin={"kind": "peer", "from": AGENT, "handback": True,
                  "body": f"{PREAMBLE}  Second report.\n  - indented once\n"}),
    _user("2026-09-25T04:05:10.000Z", _pointer(72000, 11, 61000),
          origin={"kind": "task-notification"}, turnOrigin="task_notification"),
    _user("2026-09-25T04:05:20.000Z",
          "The previous response failed to produce a valid tool call. Please retry "
          "the tool call now.", isMeta=True),
]

SIDECAR = [
    _user("2026-09-25T04:00:03.000Z", "Review one route."),
    _user("2026-09-25T04:00:04.000Z",
          "<system-reminder>\nYour final report is delivered through SubagentHandback."
          "\n</system-reminder>", isMeta=True),
    _handback("2026-09-25T04:01:59.000Z", "First report."),
    _handback("2026-09-25T04:04:59.000Z", "Second report.\n- indented once"),
]


class Senders(unittest.TestCase):
    """A message another sender put in an agent's conversation: labelled by its
    sender, and opening the agent's next turn when the view is scoped to it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="chsum-test-")
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "sess.jsonl"
        self.path.write_text(json.dumps(_user("2026-09-25T04:00:00.000Z", "go",
                                              origin={"kind": "human"})) + "\n")
        side = self.path.parent / self.path.stem / "subagents" / f"agent-{AGENT}.jsonl"
        side.parent.mkdir(parents=True)
        recs = [
            _user("2026-09-25T04:00:01.000Z", "Research the question."),
            _user("2026-09-25T04:01:00.000Z",
                  "Another Claude session sent a message while you were working:\n"
                  "<agent-message from=\"caller\">\nWrap up now.\n</agent-message>",
                  isMeta=True, origin={"kind": "peer", "from": "caller",
                                       "name": "general-purpose", "body": "Wrap up now.\n"}),
            _user("2026-09-25T04:02:00.000Z",
                  "The coordinator sent a message while you were working: Change of "
                  "direction.", isMeta=True, origin={"kind": "coordinator"}),
            _user("2026-09-25T04:03:00.000Z",
                  "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated event.\n\n"
                  "<task-notification>\n<task-id>nested01</task-id>\n"
                  "<tool-use-id>toolu_01Nested</tool-use-id>\n<status>completed</status>\n"
                  "<result>Nested report.</result>\n</task-notification>",
                  isMeta=True, origin={"kind": "task-notification"}),
        ]
        recs.insert(1, {"type": "assistant", "timestamp": "2026-09-25T04:00:02.000Z",
                        "message": {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "toolu_01Nested", "name": "Agent",
                             "input": {"description": "nested"}}]}})
        side.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def test_each_sender_is_named_and_opens_the_agents_turn(self):
        rows = [(r.label, r.text, r.starts_turn)
                for r in core.collect_rows(self.path, core._ROW_KINDS["messages"], AGENT)
                if r.kind == "message"]
        self.assertEqual(rows, [
            ("user", "Research the question.", True),
            ("message from general-purpose", "Wrap up now.", True),
            ("coordinator", "Change of direction.", True),
            ("agent nested01 returned", "Nested report.", False),
        ])


class Handbacks(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="chsum-test-")
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "sess.jsonl"
        side = self.path.parent / self.path.stem / "subagents" / f"agent-{AGENT}.jsonl"
        side.parent.mkdir(parents=True)
        for path, recs in ((self.path, PARENT), (side, SIDECAR)):
            path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def rows(self, only_agent: str = "") -> list:
        return [r for r in core.collect_rows(self.path, core._ROW_KINDS["messages"],
                                             only_agent)
                if r.kind == "message"]

    def test_each_report_prints_once_with_its_stop(self):
        returned = [(r.agent, r.text, r.label) for r in self.rows()
                    if r.label.startswith("agent ")]
        self.assertEqual(returned, [
            (AGENT, "First report.",
             f"agent {AGENT} returned · completed · 54k tokens · 7 tools · 56s"),
            ("", "Second report.\n- indented once",
             f"agent {AGENT} returned · completed · 72k tokens · 11 tools · 1m"),
        ])

    def test_no_row_carries_harness_text(self):
        texts = "\n".join(r.text for r in self.rows())
        for noise in ("system-reminder", "Another Claude session", "delivered to you",
                      "failed to produce a valid tool call"):
            self.assertNotIn(noise, texts)

    def test_scoped_to_the_agent_its_own_reports_stay(self):
        returned = [r.text for r in self.rows(AGENT) if r.label.startswith("agent ")]
        self.assertEqual(returned, ["First report.", "Second report.\n- indented once"])

    def test_only_typed_prompts_open_a_turn(self):
        self.assertEqual([r.text for r in self.rows() if r.starts_turn],
                         ["review the route", "thanks"])

    def test_the_anchor_is_the_last_typed_prompt(self):
        line, _ = core._last_prompt(self.path)
        self.assertEqual(line, 5)

    def test_agents_view_lists_every_return_with_its_source(self):
        got = [(r.result, r.in_sidecar, r.status) for r in core._agent_reports(self.path)]
        self.assertEqual(got, [
            ("First report.", True, "completed · 54k tokens · 7 tools · 56s"),
            ("Second report.\n- indented once", False,
             "completed · 72k tokens · 11 tools · 1m"),
        ])

    def test_drill_block_names_each_agent_by_its_whole_ref(self):
        block = "\n".join(core._drill_block("ch_0123", self.path))
        self.assertIn(f"- `ch_0123/{AGENT}` — ", block)


if __name__ == "__main__":
    unittest.main()
