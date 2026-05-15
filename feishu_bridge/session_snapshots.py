from __future__ import annotations

import json
from pathlib import Path

from feishu_bridge.codex_thread_bridge import CodexThreadInfo


class SessionSnapshotStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, list[CodexThreadInfo]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        chats = payload.get("chats")
        if not isinstance(chats, dict):
            return {}
        snapshots: dict[str, list[CodexThreadInfo]] = {}
        for chat_id, items in chats.items():
            if not isinstance(chat_id, str) or not isinstance(items, list):
                continue
            parsed = [item for item in (self._parse_item(raw) for raw in items) if item]
            if parsed:
                snapshots[chat_id] = parsed[:10]
        return snapshots

    def save(self, snapshots: dict[str, list[CodexThreadInfo]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "chats": {
                chat_id: [self._serialize_item(item) for item in items[:10]]
                for chat_id, items in snapshots.items()
            }
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _parse_item(item: object) -> CodexThreadInfo | None:
        if not isinstance(item, dict):
            return None
        thread_id = str(item.get("thread_id") or "").strip()
        if not thread_id:
            return None
        return CodexThreadInfo(
            thread_id=thread_id,
            thread_name=str(item.get("thread_name") or thread_id),
            updated_at=str(item.get("updated_at") or ""),
        )

    @staticmethod
    def _serialize_item(item: CodexThreadInfo) -> dict[str, str]:
        return {
            "thread_id": item.thread_id,
            "thread_name": item.thread_name,
            "updated_at": item.updated_at,
        }
