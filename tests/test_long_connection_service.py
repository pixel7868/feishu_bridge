from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace

from feishu_bridge.long_connection_service import CodexFeishuLongConnectionBridgeService
from feishu_bridge.settings import BridgeSettings


class RecordingLongConnectionBridgeService(CodexFeishuLongConnectionBridgeService):
    def __init__(self) -> None:
        super().__init__(BridgeSettings())
        self.received: list[tuple[str, str]] = []

    def process_text_message(self, chat_id: str, text: str) -> None:
        self.received.append((chat_id, text))

    @staticmethod
    def _log(message: str) -> None:
        return


def make_message_event(message_id: str, text: str) -> SimpleNamespace:
    message = SimpleNamespace(
        message_id=message_id,
        message_id_v2="",
        root_id="",
        message_type="text",
        chat_id="chat-1",
        content=json.dumps({"text": text}),
        create_time=str(int(time.time() * 1000)),
    )
    event = SimpleNamespace(
        sender=SimpleNamespace(sender_type="user"),
        message=message,
        event_id=f"event-{message_id}",
    )
    return SimpleNamespace(event=event)


class LongConnectionBridgeServiceTests(unittest.TestCase):
    def test_repeated_attach_commands_with_distinct_message_ids_are_processed(self) -> None:
        service = RecordingLongConnectionBridgeService()

        service._on_message_receive(make_message_event("message-1", "$attach 1"))
        service._on_message_receive(make_message_event("message-2", "$attach 1"))

        self.assertEqual(service.received, [("chat-1", "$attach 1"), ("chat-1", "$attach 1")])

    def test_same_message_id_is_still_deduped(self) -> None:
        service = RecordingLongConnectionBridgeService()

        service._on_message_receive(make_message_event("message-1", "$attach 1"))
        service._on_message_receive(make_message_event("message-1", "$attach 1"))

        self.assertEqual(service.received, [("chat-1", "$attach 1")])


if __name__ == "__main__":
    unittest.main()
