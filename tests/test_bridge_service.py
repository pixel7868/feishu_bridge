from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from feishu_bridge.bridge_service import CodexFeishuBridgeService
from feishu_bridge.codex_thread_bridge import CodexBridgeError, CodexThreadBridgeService
from feishu_bridge.settings import BridgeSettings
from feishu_bridge.state import ChatBinding


class RecordingFeishuClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_text_message(self, chat_id: str, text: str, *, message_uuid: str | None = None) -> None:
        self.messages.append((chat_id, text))


class RolloutOnlyBridge:
    def __init__(self, rollout_path: Path) -> None:
        self.rollout_path = rollout_path
        self.parser = CodexThreadBridgeService(codex_home=rollout_path.parent)

    def get_thread(self, thread_id: str):
        raise CodexBridgeError(f"Thread not found in session index: {thread_id}")

    def resolve_rollout_path(self, thread_id: str) -> Path:
        return self.rollout_path

    def read_messages_since(self, rollout_path, thread, offset):
        return self.parser.read_messages_since(rollout_path, thread, offset)


def write_event(handle, event_type: str, message: str, *, phase: str | None = None) -> None:
    payload = {"type": event_type, "message": message}
    if phase is not None:
        payload["phase"] = phase
    handle.write(json.dumps({"type": "event_msg", "payload": payload}, ensure_ascii=False) + "\n")


class BridgeServiceWatcherTests(unittest.TestCase):
    def test_watcher_reads_rollout_when_session_index_is_missing(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            rollout_path = root / "rollout-2026-05-17T16-12-49-thread-1.jsonl"
            with rollout_path.open("w", encoding="utf-8") as handle:
                write_event(handle, "user_message", "test")
                write_event(handle, "agent_message", "direct reply", phase="final_answer")

            settings = BridgeSettings(
                runtime_dir=root,
                poll_interval_seconds=0.01,
                forward_desktop_user_messages=False,
                feishu_streaming=False,
            )
            service = CodexFeishuBridgeService(settings)
            service.bridge = RolloutOnlyBridge(rollout_path)
            service.feishu = RecordingFeishuClient()
            service._bindings["chat-1"] = ChatBinding("chat-1", "thread-1", 0)

            stop_event = threading.Event()
            worker = threading.Thread(
                target=service._watch_thread,
                args=("chat-1", "thread-1", stop_event),
                daemon=True,
            )
            worker.start()

            deadline = time.time() + 2
            while time.time() < deadline and not service.feishu.messages:
                time.sleep(0.01)
            stop_event.set()
            worker.join(timeout=1)

            self.assertEqual(len(service.feishu.messages), 1)
            self.assertIn("direct reply", service.feishu.messages[0][1])


if __name__ == "__main__":
    unittest.main()
