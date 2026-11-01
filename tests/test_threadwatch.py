from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import threadwatch as tw


def record(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


class ParserTests(unittest.TestCase):
    def test_completed(self) -> None:
        event = tw.parse_record({
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "done",
            },
        })[0]
        self.assertEqual(event["type"], "completed")
        self.assertEqual(event["turn_id"], "turn-1")
        self.assertIsNone(event["http_status"])

    def test_failed_429(self) -> None:
        event = tw.parse_record({
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-2",
                "error": {
                    "message": "exceeded retry limit, last status: 429 Too Many Requests",
                    "codex_error_info": {
                        "response_too_many_failed_attempts": {"http_status_code": 429}
                    },
                },
            },
        })[0]
        self.assertEqual(event["type"], "failed")
        self.assertEqual(event["http_status"], 429)
        self.assertIn("429", event["summary"])

    def test_question(self) -> None:
        event = tw.parse_record({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "call-1",
                "arguments": "{}",
            },
        })[0]
        self.assertEqual(event["type"], "question")
        self.assertEqual(event["call_id"], "call-1")

    def test_approval(self) -> None:
        event = tw.parse_record({
            "type": "event_msg",
            "payload": {
                "type": "approval_requested",
                "call_id": "call-2",
                "message": "approve this",
            },
        })[0]
        self.assertEqual(event["type"], "approval")

    def test_partial_line_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            first = record({
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            })
            second = record({
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "request_user_input",
                    "call_id": "call-1",
                },
            })
            path.write_text(first + second[:30], encoding="utf-8")
            events, offset, partial = tw.read_appended(path, 0, "")
            self.assertEqual([event["type"] for event in events], ["completed"])
            self.assertTrue(partial)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(second[30:])
            events, offset, partial = tw.read_appended(path, offset, partial)
            self.assertEqual([event["type"] for event in events], ["question"])
            self.assertEqual(partial, "")

    def test_last_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text(
                record({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "a"}})
                + record({
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "b",
                        "error": {"message": "429"},
                    },
                }),
                encoding="utf-8",
            )
            event = tw.find_last_event(path, tw.DEFAULT_EVENT_TYPES)
            self.assertIsNotNone(event)
            self.assertEqual(event["type"], "failed")
            self.assertEqual(event["turn_id"], "b")


class CycleTests(unittest.TestCase):
    def test_detects_events_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "data"
            home = base / "codex"
            root.mkdir()
            (home / "sessions" / "2026" / "01" / "01").mkdir(parents=True)
            target = "11111111-1111-4111-8111-111111111111"
            wake = "22222222-2222-4222-8222-222222222222"
            session = home / "sessions" / "2026" / "01" / "01" / f"rollout-test-{target}.jsonl"
            session.write_text(
                record({"type": "session_meta", "payload": {"cwd": "E:\\project"}})
                + record({
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "error": {
                            "message": "429",
                            "codex_error_info": {
                                "response_too_many_failed_attempts": {"http_status_code": 429}
                            },
                        },
                    },
                })
                + record({
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-1",
                    },
                }),
                encoding="utf-8",
            )
            state = tw.default_state()
            state["watches"][target] = {
                "target_id": target,
                "wake_thread_id": wake,
                "project": "E:\\project",
                "events": list(tw.DEFAULT_EVENT_TYPES),
                "session_path": str(session),
                "offset": 0,
                "partial_b64": "",
                "pending_events": [],
                "start_mode": "from-start",
            }
            tw.save_state(root, state)
            messages: list[str] = []

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                messages.append(command[command.index("--message") + 1])
                return subprocess.CompletedProcess(command, 0, "queued", "")

            previous_command = os.environ.get("THREADWATCH_CODEX_COMMAND")
            os.environ["THREADWATCH_CODEX_COMMAND"] = sys.executable
            try:
                first = tw.process_cycle(root, tw.default_config(), home=home, queue_runner=runner)
                second = tw.process_cycle(root, tw.default_config(), home=home, queue_runner=runner)
            finally:
                if previous_command is None:
                    os.environ.pop("THREADWATCH_CODEX_COMMAND", None)
                else:
                    os.environ["THREADWATCH_CODEX_COMMAND"] = previous_command
            self.assertEqual(first["detected"], 2)
            self.assertEqual(first["queued"], 2)
            self.assertEqual(second["detected"], 0)
            self.assertEqual(len(messages), 2)
            self.assertTrue(any("http_status: 429" in message for message in messages))

            final = tw.load_state(root)
            self.assertEqual(final["stats"]["events_detected"], 2)
            self.assertEqual(len(final["seen"]), 2)
            self.assertEqual(final["watches"][target]["offset"], session.stat().st_size)

    def test_replay_last_pending_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "data"
            home = base / "codex"
            root.mkdir()
            (home / "sessions").mkdir(parents=True)
            target = "33333333-3333-4333-8333-333333333333"
            wake = "44444444-4444-4444-8444-444444444444"
            session = home / "sessions" / f"rollout-test-{target}.jsonl"
            session.write_text(
                record({"type": "session_meta", "payload": {"cwd": "E:\\project"}})
                + record({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old"}}),
                encoding="utf-8",
            )
            args = type("Args", (), {
                "target": target,
                "wake": wake,
                "project": "E:\\project",
                "label": "",
                "events": None,
                "replay_last": True,
            })()
            previous_home = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(home)
            try:
                tw.command_add(args, root)
            finally:
                if previous_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous_home
            state = tw.load_state(root)
            self.assertEqual(len(state["watches"][target]["pending_events"]), 1)


class MessageTests(unittest.TestCase):
    def test_message_contains_event_context(self) -> None:
        message = tw.build_wake_message(
            {"target_id": "a", "wake_thread_id": "b", "project": "E:\\project"},
            {"type": "failed", "turn_id": "turn", "http_status": 429, "summary": "quota"},
        )
        self.assertIn("event: failed", message)
        self.assertIn("http_status: 429", message)
        self.assertIn("target_thread: a", message)


if __name__ == "__main__":
    unittest.main()
