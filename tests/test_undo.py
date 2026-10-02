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
import os
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
                detail: bool = False, reverse: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = core.cmd_undo(types.SimpleNamespace(action=action,
                                                     step=step.split() if step else [],
                                                     limit=10, detail=detail,
                                                     reverse=reverse))
        return rc, out.getvalue()

    def lines(self) -> list[str]:
        return (self.repo / "f.txt").read_text().split()

    def stacks(self) -> tuple[list[str], list[str]]:
        """The calls in place and undone, newest first each."""
        placed, undone = core._undo_stacks(
            checkpoints.chain_entries(self.repo, SESSION),
            lambda step: [l for l, _, _ in core._step_files(self.repo, step)])
        return [e.call for e, _ in placed], [e.call for e, _ in undone]


class Subjects(unittest.TestCase):
    def test_every_shape_the_builder_writes_parses_back(self):
        when, stamp = "2026-10-02T12:00:01.000Z", "2026-10-02T12:00:00.000Z"
        for kw in ({}, {"call": "toolu_1"},
                   {"action": "undo", "step": stamp}, {"action": "redo", "step": stamp},
                   {"action": "undo", "step": stamp, "part": "b"},
                   {"action": "redo", "step": stamp, "part": "aa"}):
            subject = checkpoints._checkpoint_message("sess", when, **kw)
            m = checkpoints._CHECKPOINT_RE.match(subject)
            self.assertIsNotNone(m, subject)
            self.assertEqual((m["session"], m["when"], m["call"] or "",
                              m["action"] or "", m["step"] or "", m["part"] or ""),
                             ("sess", when, kw.get("call", ""), kw.get("action", ""),
                              kw.get("step", ""), kw.get("part", "")))

    def test_letters_run_a_to_z_then_two_letters(self):
        self.assertEqual([core._letter(i) for i in (0, 1, 25, 26, 27)],
                         ["a", "b", "z", "aa", "ab"])


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


class OneFileOfAStep(UndoCase):
    """A step that changed x, y and z: its files are `a`, `b` and `c`."""

    def setUp(self) -> None:
        super().setUp()
        for name in "xyz":
            self.write(f"{name}.txt", "1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "three files")
        for name in "xyz":
            self.write(f"{name}.txt", "changed\n")
        self.checkpoint(SESSION, "toolu_xyz")

    def files(self) -> str:
        return " ".join((self.repo / f"{n}.txt").read_text().strip() for n in "xyz")

    def sides(self) -> tuple[list, list]:
        """The letters each list holds for the step, `None` for all of it."""
        placed, undone = core._undo_stacks(
            checkpoints.chain_entries(self.repo, SESSION),
            lambda step: [l for l, _, _ in core._step_files(self.repo, step)])
        return ([sorted(s) if s is not None else None for _, s in placed],
                [sorted(s) for _, s in undone])

    def test_one_letter_undoes_that_file_alone(self):
        self.run_cmd("undo", "1b")
        self.assertEqual(self.files(), "changed 1 changed")
        self.assertEqual(self.sides(), ([["a", "c"]], [["b"]]))
        tip = checkpoints.chain_entries(self.repo, SESSION)[-1]
        self.assertEqual((tip.action, tip.part), ("undo", "b"))

    def test_letters_hold_after_one_file_is_undone(self):
        self.run_cmd("undo", "1b")
        _, out = self.run_cmd("undo")
        self.assertIn("a  x.txt", out)
        self.assertIn("c  z.txt", out)
        self.assertNotIn("y.txt", out)

    def test_the_whole_step_takes_the_files_still_in_place(self):
        self.run_cmd("undo", "1b")
        self.run_cmd("undo", "1")
        self.assertEqual(self.files(), "1 1 1")
        self.assertEqual(self.sides(), ([], [["a", "b", "c"]]))

    def test_redo_of_one_letter_then_the_rest(self):
        self.run_cmd("undo", "1")
        self.run_cmd("redo", "1b")
        self.assertEqual(self.files(), "1 changed 1")
        self.assertEqual(self.sides(), ([["b"]], [["a", "c"]]))
        self.run_cmd("redo", "1")
        self.assertEqual(self.files(), "changed changed changed")
        self.assertEqual(self.sides(), ([None], []))

    def test_a_letter_on_the_other_side_is_refused(self):
        self.run_cmd("undo", "1b")
        with self.assertRaises(SystemExit):
            self.run_cmd("undo", "1b")
        self.assertEqual(self.files(), "changed 1 changed")

    def test_detail_with_a_letter_shows_that_file_alone(self):
        _, out = self.run_cmd("undo", "1c", detail=True)
        self.assertIn("Updated z.txt", out)
        self.assertNotIn("x.txt", out)
        self.assertEqual(self.files(), "changed changed changed")

    def test_a_step_of_one_file_takes_no_letter(self):
        self.step("2", "two", "toolu_one")
        _, out = self.run_cmd("undo")
        self.assertIn("  1     f.txt:2", out)


class OneFileSHistory(UndoCase):
    """f.txt changed by three steps, the middle one also changing sub/g.txt.
    The file view numbers f.txt's own changes: 1 is `c`, 2 `b`, 3 `a`."""

    def setUp(self) -> None:
        super().setUp()
        self.write("sub/g.txt", "g\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "g")
        self.step("2", "two", "toolu_a")
        path = self.repo / "f.txt"
        path.write_text(path.read_text().replace("4\n", "four\n"))
        self.write("sub/g.txt", "G\n")
        self.checkpoint(SESSION, "toolu_b")
        self.step("6", "six", "toolu_c")
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def g(self) -> str:
        return (self.repo / "sub/g.txt").read_text().strip()

    def test_the_file_view_numbers_that_file_s_changes(self):
        _, out = self.run_cmd("undo", "f.txt")
        self.assertIn("3 changes to f.txt in place", out)
        rows = [l.split()[0] for l in out.splitlines()[1:4]]
        self.assertEqual(rows, ["1", "2", "3"])
        self.assertIn(" 6", out.splitlines()[1])

    def test_a_change_number_reverses_that_file_alone(self):
        self.run_cmd("undo", "f.txt 2")
        self.assertEqual(self.lines(), ["1", "two", "3", "4", "5", "six", "7", "8"])
        self.assertEqual(self.g(), "G")

    def test_redo_by_the_file_s_number_puts_it_back(self):
        self.run_cmd("undo", "f.txt 2")
        _, out = self.run_cmd("redo", "f.txt")
        self.assertIn("1 change to f.txt undone", out)
        self.run_cmd("redo", "f.txt 1")
        self.assertEqual(self.lines(), ["1", "two", "3", "four", "5", "six", "7", "8"])

    def test_a_range_of_a_file_s_changes(self):
        self.run_cmd("undo", "f.txt 1-3")
        self.assertEqual(self.lines(), ["1", "2", "3", "4", "5", "6", "7", "8"])
        self.assertEqual(self.g(), "G")

    def test_a_directory_takes_the_files_under_it(self):
        _, out = self.run_cmd("undo", "sub")
        self.assertIn("1 change to sub in place", out)
        self.assertIn("sub/g.txt:1", out)

    def test_a_name_finds_the_one_changed_path_ending_in_it(self):
        os.chdir(self.repo / "sub")
        _, out = self.run_cmd("undo", "g.txt")
        self.assertIn("to sub/g.txt in place", out)

    def test_a_letter_is_refused_in_the_file_view(self):
        with self.assertRaises(SystemExit):
            self.run_cmd("undo", "f.txt 1a")

    def test_the_file_view_s_hint_names_no_whole_step_form(self):
        _, out = self.run_cmd("undo", "f.txt")
        hint = out.split("\n\n", 1)[1]
        self.assertIn("chsum undo f.txt <n>", hint)
        self.assertNotIn("<letter>", hint)

    def test_detail_heads_each_change_and_indents_its_diff(self):
        _, out = self.run_cmd("undo", "f.txt", detail=True)
        lines = out.splitlines()
        heads = [l for l in lines if l.startswith("change ")]
        self.assertEqual([h.split(" · ")[0] for h in heads],
                         ["change 1", "change 2", "change 3"])
        self.assertIn("    ⎿ Updated f.txt", out)
        self.assertNotIn("g.txt", out)

    def test_reverse_puts_the_newest_last(self):
        _, out = self.run_cmd("undo", "f.txt", detail=True, reverse=True)
        heads = [l.split(" · ")[0] for l in out.splitlines() if l.startswith("change ")]
        self.assertEqual(heads, ["change 3", "change 2", "change 1"])

    def test_detail_alone_prints_every_step(self):
        _, out = self.run_cmd("undo", detail=True)
        heads = [l for l in out.splitlines() if l.startswith("step ")]
        self.assertEqual(heads, ["step 1", "step 2a, 2b", "step 3"])


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
