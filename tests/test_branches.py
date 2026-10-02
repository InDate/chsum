"""Branches a rewind leaves in a transcript, and the session copy that resumes one.

A rewind writes the edited prompt as a second child of the record the first one
hangs off. Parallel tool calls also give a record two children, so the fixture
below carries both: one rewind, and one parallel pair whose first result is a
leaf with no typed prompt beneath it.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chsum import core  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"


def _rec(uid: str, parent: str | None, kind: str, ts: str, content, **extra) -> dict:
    return {"type": kind, "uuid": uid, "parentUuid": parent, "sessionId": SESSION,
            "timestamp": f"2026-10-02T{ts}.000Z", "cwd": "/work/proj",
            "message": {"role": kind, "content": content}, **extra}


def _prompt(uid, parent, ts, text):
    return _rec(uid, parent, "user", ts, text, origin={"kind": "human"})


def _call(uid, parent, ts, tool_id, name="Bash", **inp):
    return _rec(uid, parent, "assistant", ts,
                [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}])


def _result(uid, parent, ts, tool_id):
    return _rec(uid, parent, "user", ts,
                [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
                toolUseResult={"stdout": "ok"})


def _reply(uid, parent, ts, text):
    return _rec(uid, parent, "assistant", ts, [{"type": "text", "text": text}])


# p1 → two parallel calls; r0 answers the first and is a leaf → reply a2.
# From a2: "first try" (branch 1, abandoned), then "second try" (branch 2),
# which edits a file and is written last.
TRUNK = [
    _prompt("p1", None, "01:00:00", "start the work"),
    _call("c1", "p1", "01:00:01", "toolu_x", command="ls"),
    _call("c2", "c1", "01:00:01", "toolu_y", command="pwd"),
    _result("r0", "c1", "01:00:02", "toolu_x"),
    _result("r1", "c2", "01:00:02", "toolu_y"),
    _reply("a2", "r1", "01:00:03", "done"),
]
ABANDONED = [
    _prompt("p2", "a2", "01:05:00", "first try"),
    _reply("a3", "p2", "01:05:05", "answer to first try"),
]
KEPT = [
    _prompt("p3", "a2", "01:10:00", "second try"),
    _call("c4", "p3", "01:10:05", "toolu_e", name="Edit", file_path="/work/proj/f.py"),
    _result("r4", "c4", "01:10:06", "toolu_e"),
    _reply("a4", "r4", "01:12:00", "edited"),
    _prompt("p4", "a4", "01:13:00", "and then this"),
    _reply("a5", "p4", "01:14:00", "all done"),
]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="chsum-branches-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name) / "-work-proj"
        self.dir.mkdir()

    def transcript(self, records: list[dict]) -> pathlib.Path:
        path = self.dir / f"{SESSION}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path


class Finding(Case):
    def test_a_rewind_yields_one_branch_per_prompt_sequence(self):
        branches = core._branches(self.transcript(TRUNK + ABANDONED + KEPT))
        self.assertEqual([[r["uuid"] for r in b.prompts] for b in branches],
                         [["p1", "p2"], ["p1", "p3", "p4"]])

    def test_a_parallel_call_dead_end_is_not_a_branch(self):
        branches = core._branches(self.transcript(TRUNK))
        self.assertEqual(len(branches), 1,
                         "r0 is a leaf, but its prompts are a prefix of the trunk's")

    def test_each_branch_counts_the_prompts_it_shares(self):
        branches = core._branches(self.transcript(TRUNK + ABANDONED + KEPT))
        self.assertEqual([b.shared for b in branches], [1, 1])

    def test_branches_order_by_where_they_diverge(self):
        # Written out of order: the file's order does not decide the numbering.
        branches = core._branches(self.transcript(TRUNK + KEPT + ABANDONED))
        self.assertEqual(branches[0].prompts[1]["uuid"], "p2")

    def test_the_latest_tip_is_the_one_resume_continues(self):
        branches = core._branches(self.transcript(TRUNK + ABANDONED + KEPT))
        self.assertEqual(core._resumed_branch(branches), 1)

    def test_a_compaction_boundary_is_followed_to_the_start(self):
        boundary = {"type": "system", "subtype": "compact_boundary", "uuid": "cb",
                    "parentUuid": None, "logicalParentUuid": "a2",
                    "sessionId": SESSION, "timestamp": "2026-10-02T01:04:00.000Z"}
        after = [dict(ABANDONED[0], parentUuid="cb"), ABANDONED[1]]
        branches = core._branches(self.transcript(TRUNK + [boundary] + after))
        self.assertEqual([r["uuid"] for r in branches[0].prompts], ["p1", "p2"])


class Rendering(Case):
    def test_the_branch_line_counts_its_own_records_only(self):
        branches = core._branches(self.transcript(TRUNK + ABANDONED + KEPT))
        line = core._branch_line(branches[1])
        self.assertIn("2 prompts", line)
        self.assertIn("1 file", line)
        self.assertIn("1 tool call", line,
                      "the trunk's two parallel calls sit before the divergence")

    def test_the_view_groups_under_the_divergence_and_marks_the_resumed_one(self):
        path = self.transcript(TRUNK + ABANDONED + KEPT)
        md = core.render_branches(core._meta_from_transcript(path), "ch_x",
                                  core._branches(path))
        self.assertIn("## After prompt 1", md)
        self.assertIn("### Branch 1\n", md)
        self.assertIn("### Branch 2 · resumed by default", md)
        self.assertIn("> second try", md)
        self.assertIn("> and then this", md)

    def test_one_branch_says_so(self):
        path = self.transcript(TRUNK)
        md = core.render_branches(core._meta_from_transcript(path), "ch_x",
                                  core._branches(path))
        self.assertIn("No rewinds", md)

    def test_a_prompt_under_a_branch_heading_renders_as_typed(self):
        ansi = core._md_ansi("### Branch 1\n\n> first try\n")
        self.assertIn("\033[32m", ansi, "green is the colour of what you typed")


class SessionCopy(Case):
    def test_the_copy_holds_the_chain_under_a_new_session_id(self):
        path = self.transcript(TRUNK + ABANDONED + KEPT)
        branch = core._branches(path)[0]
        dest, wrote = core._branch_session(path, branch, 1, "Title")
        self.assertTrue(wrote)
        rows = [json.loads(ln) for ln in dest.read_text().splitlines()]
        chain = [r for r in rows if r.get("uuid")]
        self.assertEqual([r["uuid"] for r in chain],
                         ["p1", "c1", "c2", "r1", "a2", "p2", "a3"])
        self.assertTrue(all(r["sessionId"] == dest.stem for r in chain))
        self.assertEqual(rows[-1], {"type": "ai-title", "aiTitle": "Title · branch 1",
                                    "sessionId": dest.stem})

    def test_asking_again_keeps_what_was_added_to_the_copy(self):
        path = self.transcript(TRUNK + ABANDONED + KEPT)
        branch = core._branches(path)[0]
        dest, _ = core._branch_session(path, branch, 1, "Title")
        with dest.open("a") as fh:
            fh.write('{"type": "marker"}\n')
        again, wrote = core._branch_session(path, branch, 1, "Title")
        self.assertEqual((again, wrote), (dest, False))
        self.assertIn('"marker"', dest.read_text())


class SessionList(Case):
    def test_meta_carries_the_branch_count(self):
        self.assertEqual(core._meta_from_transcript(
            self.transcript(TRUNK + ABANDONED + KEPT)).branches, 2)
        self.assertEqual(core._meta_from_transcript(self.transcript(TRUNK)).branches, 1)

    def test_the_column_sits_under_its_heading(self):
        meta = core._meta_from_transcript(self.transcript(TRUNK + ABANDONED + KEPT))
        cells = core._session_cells(meta, pathlib.Path(self._tmp.name))
        self.assertEqual(cells[core.SESSION_HEADS.index("branches") - 1], "2")

    def test_one_branch_prints_a_dash(self):
        meta = core._meta_from_transcript(self.transcript(TRUNK))
        cells = core._session_cells(meta, pathlib.Path(self._tmp.name))
        self.assertEqual(cells[core.SESSION_HEADS.index("branches") - 1], "-")


if __name__ == "__main__":
    unittest.main()
