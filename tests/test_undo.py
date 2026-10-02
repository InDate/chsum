"""`chsum undo` and `chsum redo`: a step is one checkpoint the hook wrote, and
each undo or redo writes a checkpoint of its own naming the step.

These commands write to a user's working tree, so the properties asserted here
are the ones whose failure would be felt there: a step reversed that was not
asked for, a range half-applied without saying so, a record that drifts from
the files.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import types
import unittest
from unittest import mock

from harness import RepoCase, git

from chsum import checkpoints, core

SESSION = "11111111-2222-3333-4444-555555555555"


class UndoCase(RepoCase):
    """A repo with checkpointing on, and `cmd_undo` pointed at it as the
    running session."""

    def setUp(self) -> None:
        super().setUp()
        self.enable()
        self.write("f.txt", "1\n2\n3\n4\n5\n6\n7\n8\n")
        git(self.repo, "commit", "-qam", "eight lines")
        patches = [
            mock.patch.object(core, "live_transcript",
                              lambda: pathlib.Path(f"/nowhere/{SESSION}.jsonl")),
            mock.patch.object(core, "extract_meta",
                              lambda path: types.SimpleNamespace(project=str(self.repo))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def step(self, old: str, new: str, call: str) -> None:
        """One tool call that rewrites one line, and the hook's checkpoint."""
        path = self.repo / "f.txt"
        path.write_text(path.read_text().replace(f"{old}\n", f"{new}\n"))
        self.checkpoint(SESSION, call)

    def run_cmd(self, action: str, step: str | None = None,
                detail: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = core.cmd_undo(types.SimpleNamespace(action=action, step=step,
                                                     limit=10, detail=detail))
        return rc, out.getvalue()

    def lines(self) -> list[str]:
        return (self.repo / "f.txt").read_text().split()

    def stacks(self) -> tuple[list[str], list[str]]:
        """The calls in place and undone, newest first each."""
        placed, undone = core._undo_stacks(checkpoints.chain_entries(self.repo, SESSION))
        return [e.call for e in placed], [e.call for e in undone]


class Subjects(unittest.TestCase):
    def test_every_shape_the_builder_writes_parses_back(self):
        when, stamp = "2026-10-02T12:00:01.000Z", "2026-10-02T12:00:00.000Z"
        for kw in ({}, {"call": "toolu_1"},
                   {"action": "undo", "step": stamp}, {"action": "redo", "step": stamp}):
            subject = checkpoints._checkpoint_message("sess", when, **kw)
            m = checkpoints._CHECKPOINT_RE.match(subject)
            self.assertIsNotNone(m, subject)
            self.assertEqual((m["session"], m["when"], m["call"] or "",
                              m["action"] or "", m["step"] or ""),
                             ("sess", when, kw.get("call", ""),
                              kw.get("action", ""), kw.get("step", "")))


class UndoesAndRedoes(UndoCase):
    def setUp(self) -> None:
        super().setUp()
        self.step("2", "two", "toolu_a")
        self.step("4", "four", "toolu_b")
        self.step("6", "six", "toolu_c")

    def test_undo_one_reverses_the_newest_step(self):
        rc, _ = self.run_cmd("undo", "1")
        self.assertEqual(rc, 0)
        self.assertEqual(self.lines(), ["1", "two", "3", "four", "5", "6", "7", "8"])
        self.assertEqual(self.stacks(), (["toolu_b", "toolu_a"], ["toolu_c"]))

    def test_undo_one_again_reverses_the_step_before(self):
        self.run_cmd("undo", "1")
        self.run_cmd("undo", "1")
        self.assertEqual(self.lines(), ["1", "two", "3", "4", "5", "6", "7", "8"])
        # Most recently undone first: redo 1 reverses the last undo.
        self.assertEqual(self.stacks(), (["toolu_a"], ["toolu_b", "toolu_c"]))

    def test_redo_one_reapplies_the_most_recently_undone(self):
        self.run_cmd("undo", "1")
        self.run_cmd("undo", "1")
        self.run_cmd("redo", "1")
        self.assertEqual(self.lines(), ["1", "two", "3", "four", "5", "6", "7", "8"])
        self.assertEqual(self.stacks(), (["toolu_b", "toolu_a"], ["toolu_c"]))

    def test_each_action_is_a_checkpoint_naming_its_step(self):
        stamp = checkpoints.chain_entries(self.repo, SESSION)[-1].when
        self.run_cmd("undo", "1")
        tip = checkpoints.chain_entries(self.repo, SESSION)[-1]
        self.assertEqual((tip.action, tip.step), ("undo", stamp))
        # The undo's checkpoint holds the undo's diff alone.
        diff = git(self.repo, "diff", "-U0", f"{tip.sha}^", tip.sha).stdout
        changed = [l for l in diff.splitlines()
                   if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        self.assertEqual(changed, ["-six", "+6"])

    def test_the_hook_after_an_undo_writes_nothing_more(self):
        self.run_cmd("undo", "1")
        before = len(self.chain(SESSION))
        self.checkpoint(SESSION, "toolu_the_bash_call_that_ran_it")
        self.assertEqual(len(self.chain(SESSION)), before)

    def test_an_undo_back_to_head_s_tree_is_still_recorded(self):
        self.run_cmd("undo", "1-3")
        self.assertEqual(self.status(), "")
        self.assertEqual(self.stacks(), ([], ["toolu_a", "toolu_b", "toolu_c"]))

    def test_a_range_undoes_newest_first_and_redoes_in_reverse(self):
        self.run_cmd("undo", "1-2")
        self.assertEqual(self.lines(), ["1", "two", "3", "4", "5", "6", "7", "8"])
        self.run_cmd("redo", "1-2")
        self.assertEqual(self.lines(), ["1", "two", "3", "four", "5", "six", "7", "8"])
        self.assertEqual(self.stacks(), (["toolu_c", "toolu_b", "toolu_a"], []))

    def test_an_older_step_undoes_alone(self):
        self.run_cmd("undo", "2")
        self.assertEqual(self.lines(), ["1", "two", "3", "4", "5", "six", "7", "8"])
        self.assertEqual(self.stacks(), (["toolu_c", "toolu_a"], ["toolu_b"]))

    def test_edits_since_the_last_checkpoint_become_a_step_of_their_own(self):
        path = self.repo / "f.txt"
        path.write_text(path.read_text().replace("8\n", "eight\n"))
        self.run_cmd("undo", "1")
        # Step 1 was named before the edit was recorded, so it is still `c`.
        self.assertEqual(self.lines(), ["1", "two", "3", "four", "5", "6", "7", "eight"])
        self.assertEqual(self.stacks()[0], ["", "toolu_b", "toolu_a"])

    def test_detail_changes_nothing(self):
        before = (self.lines(), self.chain(SESSION))
        rc, out = self.run_cmd("undo", "1", detail=True)
        self.assertEqual(rc, 0)
        self.assertEqual((self.lines(), self.chain(SESSION)), before)
        self.assertIn("⎿ Updated f.txt (+1 -1)", out)
        # A removed line by its number before the step, an added one by after.
        self.assertIn("6 - 6", out)
        self.assertIn("6 + six", out)


class Refuses(UndoCase):
    def test_a_step_whose_neighbour_changed_leaves_the_tree_alone(self):
        self.step("4", "four", "toolu_a")
        path = self.repo / "f.txt"
        path.write_text(path.read_text().replace("5\n", "five\n"))
        rc, _ = self.run_cmd("undo", "1")
        self.assertEqual(rc, 1)
        self.assertEqual(self.lines(), ["1", "2", "3", "four", "five", "6", "7", "8"])

    def test_an_edit_two_lines_away_does_not_block(self):
        self.step("4", "four", "toolu_a")
        path = self.repo / "f.txt"
        path.write_text(path.read_text().replace("6\n", "six\n"))
        rc, _ = self.run_cmd("undo", "1")
        self.assertEqual(rc, 0)
        self.assertEqual(self.lines(), ["1", "2", "3", "4", "5", "six", "7", "8"])

    def test_a_range_stops_at_the_first_step_that_fails(self):
        self.step("2", "two", "toolu_a")
        self.step("6", "six", "toolu_b")
        path = self.repo / "f.txt"
        path.write_text(path.read_text().replace("3\n", "three\n"))
        # Step 1 (`b`) undoes; step 2 (`a`) sits next to the edit and stops.
        rc, _ = self.run_cmd("undo", "1-2")
        self.assertEqual(rc, 1)
        self.assertEqual(self.lines(), ["1", "two", "three", "4", "5", "6", "7", "8"])
        self.assertEqual(self.stacks()[1], ["toolu_b"])

    def test_a_number_past_the_list_is_refused(self):
        self.step("2", "two", "toolu_a")
        with self.assertRaises(SystemExit):
            self.run_cmd("undo", "2")
        with self.assertRaises(SystemExit):
            self.run_cmd("undo", "x")


if __name__ == "__main__":
    unittest.main()
