from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ChatBinding:
    chat_id: str
    thread_id: str
    offset: int


class BridgeStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        bindings = payload.get("bindings")
        if not isinstance(bindings, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for chat_id, item in bindings.items():
            if not isinstance(chat_id, str) or not isinstance(item, dict):
                continue
            try:
                offset = int(item.get("offset") or 0)
            except (TypeError, ValueError):
                offset = 0
            result[chat_id] = {
                "thread_id": str(item.get("thread_id") or ""),
                "offset": offset,
            }
        return result

    def save(self, bindings: dict[str, ChatBinding]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bindings": {
                chat_id: {"thread_id": binding.thread_id, "offset": binding.offset}
                for chat_id, binding in bindings.items()
            }
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
