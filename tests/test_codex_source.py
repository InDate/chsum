"""The Codex translation: a rollout's records to the common shape.

Every case here is a record shape measured in a real rollout, not an invented
one — the counts in the module docstring of `sources/codex.py` come from the
same corpus these fixtures were taken from.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chsum.sources import codex  # noqa: E402


def rollout(*payloads) -> pathlib.Path:
    """A rollout file holding the given payloads, one per line."""
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", prefix="rollout-2026-08-02T18-00-28-019fc1ea-f2be-75c1-b152-b12a26fdb6f9",
        delete=False)
    for kind, payload in payloads:
        handle.write(json.dumps({"timestamp": "2026-08-02T10:00:40.030Z",
                                 "type": kind, "payload": payload}) + "\n")
    handle.close()
    return pathlib.Path(handle.name)


def message(role: str, *texts: str) -> tuple:
    return ("response_item", {"type": "message", "role": role,
                              "content": [{"type": "input_text", "text": t}
                                          for t in texts]})


class Messages(unittest.TestCase):
    def translate(self, *payloads) -> list[dict]:
        path = rollout(*payloads)
        try:
            return list(codex.translate(path))
        finally:
            path.unlink()

    def test_a_typed_prompt_becomes_a_user_record(self):
        recs = self.translate(message("user", "fix the parser"))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["type"], "user")
        self.assertEqual(recs[0]["message"]["content"][0]["text"], "fix the parser")
        self.assertNotIn("sourceToolUseID", recs[0])

    def test_a_reply_becomes_an_assistant_record(self):
        recs = self.translate(("response_item", {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}]}))
        self.assertEqual(recs[0]["type"], "assistant")

    def test_an_event_msg_copy_of_a_reply_is_not_translated_twice(self):
        # `event_msg/item_completed` repeats what `response_item` holds.
        recs = self.translate(
            message("user", "hello"),
            ("event_msg", {"type": "item_completed",
                           "item": {"type": "UserMessage",
                                    "content": [{"type": "text", "text": "hello"}]}}))
        self.assertEqual(len(recs), 1)

    def test_a_harness_envelope_counts_as_no_turn(self):
        recs = self.translate(message(
            "user", "<environment_context>\n  <cwd>/tmp</cwd>\n</environment_context>"))
        self.assertIn("sourceToolUseID", recs[0],
                      "an envelope is loaded, not typed, and must not count as a turn")

    def test_an_unclosed_tag_is_left_as_a_turn(self):
        # A user typing `<div>` without closing it is not a harness envelope.
        recs = self.translate(message("user", "<div> is the wrapper I meant"))
        self.assertNotIn("sourceToolUseID", recs[0])

    def test_the_first_part_decides_a_multi_part_record(self):
        # An approval record opens with its own marker and then quotes a whole
        # transcript, whose quoted turns match nothing.
        recs = self.translate(message(
            "user",
            "The following is the Codex agent history whose request action you are assessing:",
            "[1] user: something the user said earlier"))
        self.assertIn("sourceToolUseID", recs[0])

    def test_an_attachment_manifest_keeps_the_request_beside_it(self):
        recs = self.translate(message(
            "user", "# Files mentioned by the user:\n\n## a.txt\n\n## My request follows"))
        self.assertNotIn("sourceToolUseID", recs[0],
                         "the manifest shares its part with the request typed beside it")

    def test_delegated_speech_survives_its_envelope(self):
        recs = self.translate(message(
            "user",
            "<realtime_delegation>\n  <input>tell me about the parser</input>\n"
            "  <transcript_delta>noise</transcript_delta>\n</realtime_delegation>"))
        self.assertEqual(recs[0]["message"]["content"][0]["text"],
                         "tell me about the parser")
        self.assertNotIn("sourceToolUseID", recs[0])


class Reasoning(unittest.TestCase):
    def translate(self, *payloads) -> list[dict]:
        path = rollout(*payloads)
        try:
            return list(codex.translate(path))
        finally:
            path.unlink()

    def test_the_encrypted_blob_is_dropped(self):
        recs = self.translate(("response_item", {
            "type": "reasoning", "summary": [], "content": None,
            "encrypted_content": "gAAAAABqWZFwI4USpPCauWNnGoSsrRUO"}))
        self.assertEqual(recs, [], "a record with no readable text carries nothing")

    def test_a_plaintext_summary_becomes_a_thinking_block(self):
        recs = self.translate(("response_item", {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "**Refactoring the parser**"}],
            "content": None, "encrypted_content": "gAAAAAB"}))
        self.assertEqual(recs[0]["message"]["content"][0]["type"], "thinking")
        self.assertNotIn("gAAAAAB", json.dumps(recs[0]))


class ToolCalls(unittest.TestCase):
    def translate(self, *payloads) -> list[dict]:
        path = rollout(*payloads)
        try:
            return list(codex.translate(path))
        finally:
            path.unlink()

    def parts(self, rec) -> list[dict]:
        return [p for p in rec["message"]["content"] if p.get("type") == "tool_use"]

    def test_exec_command_arguments_become_a_bash_call(self):
        recs = self.translate(("response_item", {
            "type": "function_call", "name": "exec_command", "call_id": "call_1",
            "arguments": json.dumps({"cmd": "ls -la", "workdir": "/tmp"})}))
        part = self.parts(recs[0])[0]
        self.assertEqual(part["name"], "Bash")
        self.assertEqual(part["input"]["command"], "ls -la")

    def test_a_javascript_exec_yields_the_command_inside_it(self):
        recs = self.translate(("response_item", {
            "type": "custom_tool_call", "name": "exec", "call_id": "call_2",
            "input": 'const r = await tools.exec_command({"cmd":"git status",'
                     '"workdir":"/tmp"});\ntext(r.output);\n'}))
        part = self.parts(recs[0])[0]
        self.assertEqual(part["name"], "Bash")
        self.assertEqual(part["input"]["command"], "git status")

    def test_a_patch_yields_one_call_per_file(self):
        recs = self.translate(("response_item", {
            "type": "custom_tool_call", "name": "apply_patch", "call_id": "call_3",
            "input": "*** Begin Patch\n*** Update File: src/a.js\n@@\n-x\n+y\n"
                     "*** Add File: src/b.js\n+new\n"}))
        parts = self.parts(recs[0])
        self.assertEqual([(p["name"], p["input"]["file_path"]) for p in parts],
                         [("Edit", "src/a.js"), ("Write", "src/b.js")])

    def test_an_mcp_call_keeps_its_own_name(self):
        recs = self.translate(("response_item", {
            "type": "function_call", "name": "navigate", "call_id": "call_4",
            "arguments": json.dumps({"action": "goto", "url": "http://x"})}))
        self.assertEqual(self.parts(recs[0])[0]["name"], "navigate")

    def test_a_question_reply_carries_the_answers_and_the_options(self):
        asked = {"questions": [{"title": "Which shape?",
                                "options": ["Rebuild it", "Patch it"]}]}
        reply = ("[{\"questionItemId\":\"[\\\"request_user_input\\\",\\\"call_5\\\",0]\","
                 "\"question\":\"Which shape?\",\"answer\":\"Rebuild it\"}]")
        recs = self.translate(
            ("response_item", {"type": "function_call", "name": "request_user_input",
                               "call_id": "call_5", "arguments": json.dumps(asked)}),
            message("user", f"<send_user_message_question_reply>\n{reply}\n"
                            "</send_user_message_question_reply>"))
        answer = recs[-1]
        self.assertEqual(answer["toolUseResult"]["answers"], {"Which shape?": "Rebuild it"})
        self.assertEqual(answer["toolUseResult"]["questions"][0]["options"],
                         [{"label": "Rebuild it"}, {"label": "Patch it"}])
        # Claude Code's shape: an empty body and no `sourceToolUseID`, so the
        # readers count it as a turn and print the pairs rather than markup.
        self.assertEqual(answer["message"]["content"][0]["text"], "")
        self.assertNotIn("sourceToolUseID", answer)

    def test_both_question_shapes_normalise(self):
        # `{question, header, options:[{label, description}]}` is the other one.
        asked = {"questions": [{"question": "Which?", "header": "Shape",
                                "options": [{"label": "A", "description": "first"}]}]}
        got = codex._asked_questions({"arguments": json.dumps(asked)})
        self.assertEqual(got[0]["question"], "Which?")
        self.assertEqual(got[0]["header"], "Shape")
        self.assertEqual(got[0]["options"][0], {"label": "A", "description": "first"})


class Identity(unittest.TestCase):
    def test_the_session_id_comes_off_the_filename(self):
        path = pathlib.Path(
            "rollout-2026-08-02T18-00-28-019fc1ea-f2be-75c1-b152-b12a26fdb6f9.jsonl")
        self.assertEqual(codex._session_id(path),
                         "019fc1ea-f2be-75c1-b152-b12a26fdb6f9")

    def test_record_ids_are_stable_across_translations(self):
        # A note is filed against one of these, so a second translation of the
        # same rollout has to produce the same id.
        first = codex._uuid("sess", 4)
        self.assertEqual(first, codex._uuid("sess", 4))
        self.assertNotEqual(first, codex._uuid("sess", 5))
        self.assertNotEqual(first, codex._uuid("other", 4))

    def test_the_cwd_in_force_is_carried_onto_each_record(self):
        path = rollout(
            ("session_meta", {"session_id": "s", "cwd": "/repo/one"}),
            message("user", "first"),
            ("turn_context", {"cwd": "/repo/two"}),
            message("user", "second"))
        try:
            recs = list(codex.translate(path))
        finally:
            path.unlink()
        self.assertEqual(recs[0]["cwd"], "/repo/one")
        self.assertEqual(recs[1]["cwd"], "/repo/two",
                         "a turn can run in a directory the session did not start in")


if __name__ == "__main__":
    unittest.main()
