"""The readers and renderers that turn records into what a reader sees.

Each case here is a property whose failure was observed during the work rather
than imagined: a suffix built by one site and parsed by another, a locator that
had to grow a new form, and the three blocks an answered question prints.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chsum import core  # noqa: E402


class ViewFilenames(unittest.TestCase):
    """`_view_suffix` names a view's file and `_digest_stem` reads it back. The
    two are inverses; a view added to one and missed by the other leaves a file
    written that the listing cannot resolve."""

    CASES = [("", ""), ("messages", ""), ("tools", ""), ("commands", ""),
             ("agents", ""), ("call", "call_KABn4"), ("agent", "a9f0f78b"),
             ("writes", "")]

    def test_every_view_round_trips(self):
        uuid = "01a0acf9-4ee4-7290-a500-2dd0d1a5c046"
        for view, ident in self.CASES:
            with self.subTest(view=view or "digest"):
                stem = uuid + core._view_suffix(view, ident)
                back_uuid, back_view = core._digest_stem(stem)
                self.assertEqual(back_uuid, uuid,
                                 "a uuid holds hyphens, so the suffix must match "
                                 "at the end rather than split on one")
                self.assertTrue(back_view.startswith(view))

    def test_the_digest_itself_carries_no_suffix(self):
        self.assertEqual(core._view_suffix(""), "")
        self.assertEqual(core._digest_stem("abc-123"), ("abc-123", ""))

    def test_every_view_has_a_name_for_the_line_that_confirms_it(self):
        for view, ident in self.CASES:
            with self.subTest(view=view or "digest"):
                label = core._view_label(core._view_suffix(view, ident))
                self.assertTrue(label and not label.startswith("-"))


class Locators(unittest.TestCase):
    """What `chsum where` accepts. Every form here is one chsum prints."""

    def test_a_row(self):
        self.assertEqual(core._locator_parts("01a0acf9:31"),
                         ("01a0acf9", "", "31", ""))

    def test_a_run_of_rows(self):
        self.assertEqual(core._locator_parts("01a0acf9:31-40"),
                         ("01a0acf9", "", "31,40", ""))

    def test_a_subagent_row(self):
        session, agent, lines, call = core._locator_parts("01a0acf9/a9f0f78b:3")
        self.assertEqual((session, agent, lines, call),
                         ("01a0acf9", "a9f0f78b", "3", ""))

    def test_a_session_on_its_own(self):
        self.assertEqual(core._locator_parts("01a0acf9"),
                         ("01a0acf9", "", "", ""))

    def test_a_checkpoint_stamp_from_the_reflog(self):
        # `<session>:<call-id>`, both halves as a checkpoint commit holds them.
        session, _agent, lines, call = core._locator_parts(
            "a1e047f5:toolu_01V7rDx5LeJQJqbW9wPbJ178")
        self.assertEqual(session, "a1e047f5")
        self.assertEqual(call, "toolu_01V7rDx5LeJQJqbW9wPbJ178")
        self.assertEqual(lines, "", "a call id stands in for the line")

    def test_a_bare_call_id(self):
        for prefix in ("toolu_", "call_", "ctc_", "fc_"):
            with self.subTest(prefix=prefix):
                session, _agent, _lines, call = core._locator_parts(prefix + "abc123")
                self.assertEqual(session, "")
                self.assertEqual(call, prefix + "abc123")

    def test_backticks_are_stripped(self):
        # A locator is usually pasted out of chsum's own output.
        self.assertEqual(core._locator_parts("`01a0acf9:31`")[2], "31")

    def test_nonsense_says_what_a_locator_looks_like(self):
        with self.assertRaises(SystemExit) as caught:
            core._locator_parts("not a locator!!")
        self.assertIn("01a0acf9:31", str(caught.exception))


class AnsweredQuestions(unittest.TestCase):
    """An answer to the question tool prints as three blocks: what was asked,
    what was offered, what was chosen."""

    RESULT = {
        "questions": [{"question": "Which shape?", "header": "Deploy shape",
                       "options": [{"label": "Full reuse", "description": "everything"},
                                   {"label": "Just deploy", "description": "narrow"}]}],
        "answers": {"Which shape?": "Full reuse"},
    }

    def test_three_blocks_in_order(self):
        blocks = "\n".join(core._asked_blocks(self.RESULT, 400))
        self.assertLess(blocks.index("Question asked"), blocks.index("Options given"))
        self.assertLess(blocks.index("Options given"), blocks.index("Answered"))

    def test_the_labels_sit_outside_the_quotes(self):
        # `_md_ansi` leaves a quote's markdown literal so transcript text cannot
        # forge styling; a label inside one would print its asterisks.
        for line in "\n".join(core._asked_blocks(self.RESULT, 400)).splitlines():
            if "Question asked" in line or "Answered" in line:
                self.assertFalse(line.startswith(">"), line)

    def test_the_header_folds_into_the_label(self):
        blocks = "\n".join(core._asked_blocks(self.RESULT, 400))
        self.assertIn("**Question asked — Deploy shape**", blocks)

    def test_a_free_text_question_has_no_options_block(self):
        bare = {"answers": {"What next?": "carry on"}}
        blocks = "\n".join(core._asked_blocks(bare, 400))
        self.assertNotIn("Options given", blocks)
        self.assertIn("Answered", blocks)

    def test_an_empty_result_renders_nothing(self):
        self.assertEqual(core._asked_blocks({}, 400), [])
        self.assertEqual(core._asked_blocks({"answers": {}}, 400), [])

    def test_indent_nests_the_blocks_under_a_row(self):
        nested = "\n".join(core._asked_blocks(self.RESULT, 400, "  "))
        self.assertIn("  **Question asked", nested)
        self.assertIn("  > ", nested, "markdown reads two spaces as the item's content")


class TerminalRendering(unittest.TestCase):
    """`_md_ansi` turns the markdown into what a terminal shows."""

    def test_a_quoted_line_keeps_its_bar_on_every_wrapped_row(self):
        out = core._md_ansi("> " + "word " * 60)
        bars = [l for l in out.splitlines() if "│" in l]
        self.assertGreater(len(bars), 1)
        self.assertTrue(all("│" in l for l in bars))

    def test_writes_are_lit_apart_from_the_dim_line_around_them(self):
        out = core._md_ansi("*turn 32 · 5 commands · 4 writes*")
        self.assertIn(core._ANSI_WROTE, out)
        # The dim run is closed before the colour opens, or the colour renders
        # dimmed — which is what made it read as burnt orange.
        self.assertIn(core._ANSI_OFF + core._ANSI_WROTE, out)

    def test_a_diffstat_pair_is_lit_green_and_red(self):
        out = core._md_ansi("- `chsum/core.py`  +7515 −0")
        self.assertIn(core._ANSI_ADDED, out)
        self.assertIn(core._ANSI_REMOVED, out)

    def test_a_bullet_that_merely_holds_a_number_is_left_alone(self):
        out = core._md_ansi("- there were +12 of them")
        self.assertNotIn(core._ANSI_ADDED, out)

    def test_a_row_head_colours_the_quote_beneath_it_by_role(self):
        user = core._md_ansi("- `a1e047f5:31`  09:31:17  user\n\n> mine\n")
        agent = core._md_ansi("- `a1e047f5:34`  09:31:26  assistant\n\n> theirs\n")
        self.assertIn("\033[32m", user)
        self.assertNotIn("\033[32m", agent)


if __name__ == "__main__":
    unittest.main()
