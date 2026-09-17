"""The git checkpoint layer: what it writes, what it refuses to touch, and what
survives once git collects its garbage.

This is the code that writes to a user's repository, so the properties asserted
here are the ones whose failure would be felt outside chsum: a moved `HEAD`, a
disturbed index, a lost history.
"""
from __future__ import annotations

import pathlib
import unittest

from harness import RepoCase, git

from chsum import checkpoints


class WritesAChain(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_each_call_chains_onto_the_last(self):
        for n in (1, 2, 3):
            self.write("f.txt", f"line 1\nchange {n}\n")
            self.checkpoint("sess", f"toolu_{n}")
        subjects = self.chain("sess")
        self.assertEqual(len(subjects), 3)
        # Newest first, and each names the call that caused it.
        self.assertTrue(subjects[0].endswith("toolu_3"))
        self.assertTrue(subjects[-1].endswith("toolu_1"))

    def test_git_show_is_the_call_s_own_diff(self):
        self.write("f.txt", "line 1\nfirst\n")
        self.checkpoint("sess", "toolu_1")
        self.write("g.txt", "second file\n")
        self.checkpoint("sess", "toolu_2")
        ref = checkpoints.checkpoint_ref("sess")
        # The tip against its parent: one file, not both, because the chain
        # parents each checkpoint on the last rather than on the base commit.
        stat = git(self.repo, "show", "--stat", "--format=", ref).stdout
        self.assertIn("g.txt", stat)
        self.assertNotIn("f.txt", stat)

    def test_head_is_never_written(self):
        before = self.head()
        self.write("f.txt", "line 1\nchanged\n")
        self.checkpoint("sess", "toolu_1")
        self.assertEqual(self.head(), before)

    def test_the_user_s_index_is_left_alone(self):
        self.write("staged.txt", "staged by the user\n")
        git(self.repo, "add", "staged.txt")
        self.write("f.txt", "line 1\nchanged\n")
        # Read after the change and before the checkpoint, so what is compared
        # is the checkpoint's effect and not the edit that gave it something to
        # record. `A  staged.txt` is the entry that must survive: the staging
        # steps run against a throwaway index, never this one.
        before = self.status()
        self.checkpoint("sess", "toolu_1")
        self.assertEqual(self.status(), before)
        self.assertIn("A  staged.txt", self.status())

    def test_a_call_that_wrote_nothing_adds_no_checkpoint(self):
        self.write("f.txt", "line 1\nchanged\n")
        self.checkpoint("sess", "toolu_1")
        # No file touched between the two calls: the tree matches the last
        # checkpoint's, so the second records nothing.
        self.checkpoint("sess", "toolu_readonly")
        self.assertEqual(len(self.chain("sess")), 1)

    def test_a_declined_project_writes_nothing(self):
        git_dir = checkpoints._git_dir(self.repo)
        (git_dir / checkpoints.GATE_NAME).write_text("declined\n")
        self.write("f.txt", "line 1\nchanged\n")
        self.checkpoint("sess", "toolu_1")
        self.assertEqual(self.chain("sess"), [])

    def test_two_sessions_keep_separate_chains(self):
        self.write("f.txt", "line 1\na\n")
        self.checkpoint("one", "toolu_a")
        self.write("f.txt", "line 1\nb\n")
        self.checkpoint("two", "toolu_b")
        self.assertEqual(len(self.chain("one")), 1)
        self.assertEqual(len(self.chain("two")), 1)


class SurvivesCollection(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_a_chain_outlives_a_gc(self):
        self.write("f.txt", "line 1\nkept\n")
        self.checkpoint("sess", "toolu_1")
        self.collect_garbage()
        self.assertEqual(len(self.chain("sess")), 1)

    def test_a_reflog_checkpoint_does_not(self):
        # The mechanism chains replaced: the commit is referenced by nothing, so
        # collection takes it and the stamp that named it.
        self.write("f.txt", "line 1\nlost\n")
        sha = self.plant_reflog_checkpoint("old", "2026-01-01T00:00:00.000Z", "toolu_x")
        self.collect_garbage()
        self.assertFalse(self.object_exists(sha),
                         "an unreferenced checkpoint should have been pruned")


class Reads(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_reads_a_chain_back_in_order(self):
        for n in (1, 2, 3):
            self.write("f.txt", f"line 1\n{n}\n")
            self.checkpoint("sess", f"toolu_{n}")
        marks = checkpoints._checkpoint_shas(self.repo, "sess")
        self.assertEqual([call for _when, _sha, call in marks],
                         ["toolu_1", "toolu_2", "toolu_3"])
        # Oldest first, which is what every caller downstream walks.
        self.assertEqual(sorted(m[0] for m in marks), [m[0] for m in marks])

    def test_merges_a_session_that_spans_both_mechanisms(self):
        self.write("f.txt", "line 1\nold\n")
        self.plant_reflog_checkpoint("sess", "2026-01-01T00:00:00.000Z", "toolu_old")
        self.write("f.txt", "line 1\nnew\n")
        self.checkpoint("sess", "toolu_new")
        marks = checkpoints._checkpoint_shas(self.repo, "sess")
        self.assertEqual([call for _w, _s, call in marks], ["toolu_old", "toolu_new"])

    def test_a_migrated_checkpoint_is_not_counted_twice(self):
        # A rebuild copies the tree and message under a new sha, so a merge
        # keyed on sha would report the same checkpoint from both sources.
        self.write("f.txt", "line 1\nold\n")
        self.plant_reflog_checkpoint("sess", "2026-01-01T00:00:00.000Z", "toolu_old")
        marks = checkpoints._checkpoint_shas(self.repo, "sess")
        checkpoints._migrate_chain(self.repo, "sess", marks)
        after = checkpoints._checkpoint_shas(self.repo, "sess")
        self.assertEqual(len(after), 1)

    def test_reflog_sessions_names_every_session_it_holds(self):
        self.write("f.txt", "line 1\na\n")
        self.plant_reflog_checkpoint("one", "2026-01-01T00:00:00.000Z", "toolu_a")
        self.write("f.txt", "line 1\nb\n")
        self.plant_reflog_checkpoint("two", "2026-01-02T00:00:00.000Z", "toolu_b")
        found = checkpoints.reflog_sessions(self.repo)
        self.assertEqual(found, {"one": 1, "two": 1})


class Migrates(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()
        for n in (1, 2, 3):
            self.write("f.txt", f"line 1\nold {n}\n")
            self.plant_reflog_checkpoint(
                "sess", f"2026-01-0{n}T00:00:00.000Z", f"toolu_{n}")

    def test_rebuilds_reflog_checkpoints_as_a_chain(self):
        marks = checkpoints._checkpoint_shas(self.repo, "sess")
        written, skipped, note = checkpoints._migrate_chain(self.repo, "sess", marks)
        self.assertEqual((written, skipped, note), (3, 0, ""))
        self.assertEqual(len(self.chain("sess")), 3)

    def test_the_rebuilt_chain_survives_collection(self):
        marks = checkpoints._checkpoint_shas(self.repo, "sess")
        checkpoints._migrate_chain(self.repo, "sess", marks)
        self.collect_garbage()
        self.assertEqual(len(checkpoints._checkpoint_shas(self.repo, "sess")), 3)

    def test_trees_and_stamps_are_carried_over(self):
        before = checkpoints._checkpoint_shas(self.repo, "sess")
        trees = [checkpoints._tree_of(self.repo, sha) for _w, sha, _c in before]
        checkpoints._migrate_chain(self.repo, "sess", before)
        after = checkpoints._checkpoint_shas(self.repo, "sess")
        self.assertEqual([checkpoints._tree_of(self.repo, sha) for _w, sha, _c in after],
                         trees)
        self.assertEqual([(w, c) for w, _s, c in after],
                         [(w, c) for w, _s, c in before])

    def test_running_it_twice_changes_nothing(self):
        marks = checkpoints._checkpoint_shas(self.repo, "sess")
        checkpoints._migrate_chain(self.repo, "sess", marks)
        tip = checkpoints._ref_tip(self.repo, checkpoints.checkpoint_ref("sess"))
        again = checkpoints._checkpoint_shas(self.repo, "sess")
        self.assertEqual(len(again), 3)
        # Nothing is behind, so the command would not call the rebuild at all.
        chained = len(checkpoints._chain_shas(self.repo, "sess"))
        self.assertEqual(chained, len(again))
        self.assertEqual(checkpoints._ref_tip(
            self.repo, checkpoints.checkpoint_ref("sess")), tip)

    def test_progress_is_reported_per_checkpoint(self):
        seen = []
        marks = checkpoints._checkpoint_shas(self.repo, "sess")
        checkpoints._migrate_chain(self.repo, "sess", marks,
                                   lambda done, of: seen.append((done, of)))
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])


class Worktrees(RepoCase):
    """A linked worktree keeps its own `HEAD`, index and reflog, and shares
    `refs/`. The chain is written to the shared store, which is what lets it
    outlive the worktree."""

    def setUp(self) -> None:
        super().setUp()
        git(self.repo, "branch", "-q", "side")
        self.linked = self.repo.parent / "linked"
        git(self.repo, "worktree", "add", "-q", str(self.linked), "side")
        git_dir = checkpoints._git_dir(self.linked)
        (git_dir / checkpoints.GATE_NAME).write_text("enabled\n")

    def test_a_checkpoint_written_in_a_worktree_is_readable_from_the_main_one(self):
        (self.linked / "f.txt").write_text("line 1\nfrom the worktree\n")
        checkpoints._write_checkpoint(self.linked, checkpoints._git_dir(self.linked),
                                      "wt", "toolu_w")
        self.assertEqual(len(checkpoints._checkpoint_shas(self.repo, "wt")), 1)

    def test_it_outlives_the_worktree_and_a_gc(self):
        (self.linked / "f.txt").write_text("line 1\nfrom the worktree\n")
        checkpoints._write_checkpoint(self.linked, checkpoints._git_dir(self.linked),
                                      "wt", "toolu_w")
        git(self.repo, "worktree", "remove", "--force", str(self.linked))
        self.collect_garbage()
        self.assertEqual(len(checkpoints._checkpoint_shas(self.repo, "wt")), 1)

    def test_the_worktree_s_head_is_not_moved(self):
        before = git(self.linked, "rev-parse", "HEAD").stdout.strip()
        (self.linked / "f.txt").write_text("line 1\nchanged\n")
        checkpoints._write_checkpoint(self.linked, checkpoints._git_dir(self.linked),
                                      "wt", "toolu_w")
        self.assertEqual(git(self.linked, "rev-parse", "HEAD").stdout.strip(), before)


class Prunes(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_dropping_a_ref_leaves_the_commits_collectable(self):
        self.write("f.txt", "line 1\ndoomed\n")
        self.checkpoint("sess", "toolu_1")
        ref = checkpoints.checkpoint_ref("sess")
        tip = checkpoints._ref_tip(self.repo, ref)
        git(self.repo, "update-ref", "-d", ref, tip)
        self.assertEqual(checkpoints._ref_tip(self.repo, ref), "")
        self.collect_garbage()
        self.assertFalse(self.object_exists(tip))


if __name__ == "__main__":
    unittest.main()
