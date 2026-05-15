from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request

from feishu_bridge.settings import BridgeSettings

STREAMING_ELEMENT_ID = "streaming_content"


@dataclass(slots=True)
class FeishuStreamingCardRef:
    card_id: str
    message_id: str
    sequence: int = 1


def extract_text_content(raw_content: str) -> str:
    if not raw_content:
        return ""
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError:
        return ""
    return str(payload.get("text") or "").strip()


class FeishuClient:
    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self._sdk_client = None

    def verify_token(self, payload: dict) -> bool:
        expected = self.settings.verification_token
        if not expected:
            return True
        header = payload.get("header") or {}
        token = header.get("token") or payload.get("token")
        return token == expected

    def get_tenant_access_token(self) -> str:
        req = request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps(
                {"app_id": self.settings.app_id, "app_secret": self.settings.app_secret}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with request.urlopen(req, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
        if body.get("code") != 0:
            raise RuntimeError(f"failed to fetch tenant access token: {body}")
        token = body.get("tenant_access_token")
        if not token:
            raise RuntimeError("tenant_access_token missing in Feishu response")
        return str(token)

    def send_text_message(self, chat_id: str, text: str, *, message_uuid: str | None = None) -> None:
        token = self.get_tenant_access_token()
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        if message_uuid:
            payload["uuid"] = str(message_uuid)[:64]
        req = request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"failed to send message: {exc.code} {detail}") from exc
        if body.get("code") != 0:
            raise RuntimeError(f"failed to send message: {body}")

    def create_streaming_card(self, chat_id: str) -> FeishuStreamingCardRef:
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        client = self._get_sdk_client()
        create_response = client.cardkit.v1.card.create(
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(self._build_streaming_card("", completed=False), ensure_ascii=False))
                .build()
            )
            .build()
        )
        self._raise_sdk_error(create_response, "create streaming card")
        card_id = str(getattr(create_response.data, "card_id", "") or "").strip()
        if not card_id:
            raise RuntimeError("create streaming card failed: card_id missing")

        content = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
        send_response = client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        self._raise_sdk_error(send_response, "send streaming card")
        message_id = str(getattr(send_response.data, "message_id", "") or "").strip()
        if not message_id:
            raise RuntimeError("send streaming card failed: message_id missing")
        return FeishuStreamingCardRef(card_id=card_id, message_id=message_id, sequence=1)

    def stream_card_content(self, ref: FeishuStreamingCardRef, text: str) -> FeishuStreamingCardRef:
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        content = self._normalize_card_text(text) or "处理中..."
        sequence = ref.sequence + 1
        response = self._get_sdk_client().cardkit.v1.card_element.content(
            ContentCardElementRequest.builder()
            .card_id(ref.card_id)
            .element_id(STREAMING_ELEMENT_ID)
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(content)
                .sequence(sequence)
                .build()
            )
            .build()
        )
        self._raise_sdk_error(response, "stream card content")
        ref.sequence = sequence
        return ref

    def finish_streaming_card(self, ref: FeishuStreamingCardRef, text: str) -> FeishuStreamingCardRef:
        from lark_oapi.api.cardkit.v1 import (
            Card,
            SettingsCardRequest,
            SettingsCardRequestBody,
            UpdateCardRequest,
            UpdateCardRequestBody,
        )

        sequence = ref.sequence + 1
        settings_response = self._get_sdk_client().cardkit.v1.card.settings(
            SettingsCardRequest.builder()
            .card_id(ref.card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(json.dumps({"streaming_mode": False}, ensure_ascii=False))
                .sequence(sequence)
                .build()
            )
            .build()
        )
        self._raise_sdk_error(settings_response, "close streaming card")

        sequence += 1
        update_response = self._get_sdk_client().cardkit.v1.card.update(
            UpdateCardRequest.builder()
            .card_id(ref.card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(
                    Card.builder()
                    .type("card_json")
                    .data(json.dumps(self._build_streaming_card(text, completed=True), ensure_ascii=False))
                    .build()
                )
                .sequence(sequence)
                .build()
            )
            .build()
        )
        self._raise_sdk_error(update_response, "finish streaming card")
        ref.sequence = sequence
        return ref

    def _get_sdk_client(self):
        if self._sdk_client is None:
            import lark_oapi as lark

            self._sdk_client = (
                lark.Client.builder()
                .app_id(self.settings.app_id)
                .app_secret(self.settings.app_secret)
                .build()
            )
        return self._sdk_client

    @staticmethod
    def _raise_sdk_error(response: object, action: str) -> None:
        success = getattr(response, "success", None)
        if callable(success) and success():
            return
        code = getattr(response, "code", None)
        msg = getattr(response, "msg", "")
        log_id = ""
        get_log_id = getattr(response, "get_log_id", None)
        if callable(get_log_id):
            log_id = str(get_log_id() or "")
        detail = f"code={code} msg={msg}"
        if log_id:
            detail = f"{detail} log_id={log_id}"
        raise RuntimeError(f"{action} failed: {detail}")

    @classmethod
    def _build_streaming_card(cls, text: str, *, completed: bool) -> dict:
        content = cls._normalize_card_text(text)
        summary_text = cls._plain_summary(content)
        body_elements = [
            {
                "tag": "markdown",
                "content": content,
                "text_align": "left",
                "text_size": "normal_v2",
                "margin": "0px 0px 0px 0px",
                "element_id": STREAMING_ELEMENT_ID,
            }
        ]
        if not completed:
            body_elements.append(
                {
                    "tag": "markdown",
                    "content": "<font color='grey'>Codex 正在回复...</font>",
                    "text_size": "notation",
                    "element_id": "streaming_status",
                }
            )
        return {
            "schema": "2.0",
            "config": {
                "streaming_mode": not completed,
                "locales": ["zh_cn", "en_us"],
                "summary": {
                    "content": summary_text or "Codex reply",
                    "i18n_content": {
                        "zh_cn": summary_text or "Codex 回复",
                        "en_us": summary_text or "Codex reply",
                    },
                },
            },
            "body": {"elements": body_elements},
        }

    @staticmethod
    def _normalize_card_text(text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        return normalized[:12000]

    @staticmethod
    def _plain_summary(text: str) -> str:
        summary = " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())
        for token in ("`", "*", "_", "#", ">", "[", "]", "(", ")", "~"):
            summary = summary.replace(token, "")
        return summary[:120]
