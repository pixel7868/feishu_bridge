from __future__ import annotations

import json
import traceback
import time
from typing import Any

from feishu_bridge.bridge_service import CodexFeishuBridgeService
from feishu_bridge.settings import BridgeSettings


def _load_lark_oapi() -> Any:
    try:
        import lark_oapi as lark  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Feishu long-connection mode requires local `lark_oapi`.") from exc
    return lark


class CodexFeishuLongConnectionBridgeService(CodexFeishuBridgeService):
    def __init__(self, settings: BridgeSettings) -> None:
        super().__init__(settings)
        self._ws_client: Any | None = None
        self._recent_event_keys: dict[str, float] = {}
        self._dedupe_ttl_seconds = 120.0
        self._started_at_epoch_ms = int(time.time() * 1000)
        self._stale_grace_ms = 10_000

    def serve_forever(self) -> None:
        self._prepare_runtime()
        lark = _load_lark_oapi()
        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.settings.encrypt_key or "",
                self.settings.verification_token or "",
                lark.LogLevel.INFO,
            )
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )
        client = lark.ws.Client(
            self.settings.app_id,
            self.settings.app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=event_handler,
        )
        self._ws_client = client
        print("Codex Feishu long-connection bridge started for event: im.message.receive_v1")
        client.start()

    def _on_message_receive(self, data: Any) -> None:
        event = getattr(data, "event", None)
        if event is None:
            return
        sender = getattr(event, "sender", None)
        sender_type = str(getattr(sender, "sender_type", "") or "").strip().lower()
        if sender_type and sender_type != "user":
            self._log(f"ignored non-user long-connection sender_type={sender_type}")
            return
        message = getattr(event, "message", None)
        if message is None:
            return
        message_created_at = self._message_created_at_ms(message)
        if (
            message_created_at is not None
            and message_created_at < self._started_at_epoch_ms - self._stale_grace_ms
        ):
            self._log(
                "ignored stale long-connection message "
                f"message_id={getattr(message, 'message_id', '')} "
                f"created_at={message_created_at} bridge_started_at={self._started_at_epoch_ms}"
            )
            return
        if str(getattr(message, "message_type", "") or "").strip().lower() != "text":
            return
        chat_id = str(getattr(message, "chat_id", "") or "").strip()
        if not chat_id:
            return
        text = self._extract_text_from_message_content(getattr(message, "content", None))
        if not text:
            return
        dedupe_key = self._message_dedupe_key(event, message, chat_id, text)
        if self._is_duplicate_message(dedupe_key):
            self._log(
                f"ignored duplicate long-connection message chat_id={chat_id} key={dedupe_key}"
            )
            return
        try:
            self._log(f"received long-connection message chat_id={chat_id} text={text[:120]!r}")
            self.process_text_message(chat_id, text)
        except Exception as exc:
            self._recover_after_processing_error(chat_id, exc)
            self._log(
                f"failed to process long-connection message chat_id={chat_id}: {exc!r}\n"
                f"{traceback.format_exc()}"
            )
            self._safe_send_text_message(chat_id, f"桥接处理失败: {exc}")

    @staticmethod
    def _extract_text_from_message_content(content: Any) -> str:
        if not isinstance(content, str):
            return ""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return ""
        return str(payload.get("text") or "").strip()

    def _message_dedupe_key(self, event: Any, message: Any, chat_id: str, text: str) -> str:
        for owner, attr in (
            (message, "message_id"),
            (message, "message_id_v2"),
            (message, "root_id"),
            (event, "event_id"),
        ):
            value = str(getattr(owner, attr, "") or "").strip()
            if value:
                return f"id:{value}"
        message_created_at = self._message_created_at_ms(message)
        if message_created_at is not None:
            return f"fallback:{chat_id}:{message_created_at}:{text}"
        return f"fallback:{chat_id}:{text}"

    @staticmethod
    def _message_created_at_ms(message: Any) -> int | None:
        for attr in ("create_time", "created_at", "create_time_ms"):
            raw = getattr(message, attr, None)
            if raw is None:
                continue
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            if value < 10_000_000_000:
                value *= 1000
            return value
        return None

    def _is_duplicate_message(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            expired = [
                item_key
                for item_key, seen_at in self._recent_event_keys.items()
                if now - seen_at > self._dedupe_ttl_seconds
            ]
            for item_key in expired:
                self._recent_event_keys.pop(item_key, None)
            seen_at = self._recent_event_keys.get(key)
            self._recent_event_keys[key] = now
            if seen_at is None:
                return False
            return now - seen_at <= self._dedupe_ttl_seconds
