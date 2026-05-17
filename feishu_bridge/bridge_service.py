from __future__ import annotations

import json
import threading
import time
import contextlib
import traceback
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from feishu_bridge.appserver_sender import AppServerTurnOptions, AppServerTurnRunner
from feishu_bridge.codex_thread_bridge import (
    CodexBridgeError,
    CodexBridgeMessage,
    CodexThreadBridgeService,
    CodexThreadInfo,
)
from feishu_bridge.feishu_client import FeishuClient, FeishuStreamingCardRef, extract_text_content
from feishu_bridge.haindy_sender import HaindySimulationSender
from feishu_bridge.session_snapshots import SessionSnapshotStore
from feishu_bridge.settings import BridgeSettings
from feishu_bridge.state import BridgeStateStore, ChatBinding


MESSAGE_MODES = {"appserver", "direct", "simulate"}
MESSAGE_MODE_ALIASES = {
    "a": "appserver",
    "api": "appserver",
    "as": "appserver",
    "d": "direct",
    "s": "simulate",
}
APP_SERVER_NO_ROLLOUT_MARKER = "no rollout found for thread id"
DEFAULT_APP_SERVER_AUTO_RECOVERY_ROUNDS = 3
UNBOUND_THREAD_HINT = "当前会话还没有绑定 Codex 线程。\n先发 `/sessions` 查看，再用 `/attach <编号或线程ID>` 绑定。"


@dataclass(slots=True)
class StreamingReplyState:
    card: FeishuStreamingCardRef
    progress_parts: list[str]


class CodexFeishuBridgeService:
    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self.bridge = CodexThreadBridgeService(codex_home=settings.codex_home)
        self.feishu = FeishuClient(settings)
        self.simulator = HaindySimulationSender(settings)
        self.state_store = BridgeStateStore(settings.runtime_dir / "feishu_bridge_state.json")
        self.session_snapshot_store = SessionSnapshotStore(
            settings.runtime_dir / "feishu_bridge_session_snapshots.json"
        )
        self._bindings: dict[str, ChatBinding] = {}
        self._session_snapshots: dict[str, list[CodexThreadInfo]] = {}
        self._watchers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._suppressions: dict[str, list[tuple[float, str]]] = {}
        self._pending_ui_new_chats: set[str] = set()
        self._streaming_replies: dict[tuple[str, str], StreamingReplyState] = {}
        self._streaming_disabled_until = 0.0
        self._lock = threading.Lock()

    def serve_webhook_forever(self) -> None:
        self._prepare_runtime()
        server = self._build_server()
        print(f"Codex Feishu bridge listening on http://{self.settings.host}:{self.settings.port}/webhook/feishu")
        server.serve_forever()

    def process_webhook_payload(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        event_type = payload.get("type")
        if event_type == "url_verification":
            return HTTPStatus.OK, {"challenge": payload.get("challenge", "")}
        if not self.feishu.verify_token(payload):
            return HTTPStatus.FORBIDDEN, {"error": "invalid_verification_token"}

        header = payload.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return HTTPStatus.OK, {"ok": True, "ignored": True}
        event = payload.get("event") or {}
        sender = event.get("sender") or {}
        if sender.get("sender_type") == "app":
            return HTTPStatus.OK, {"ok": True, "ignored": True}
        message = event.get("message") or {}
        chat_id = str(message.get("chat_id") or "").strip()
        if not chat_id:
            return HTTPStatus.BAD_REQUEST, {"error": "missing_chat_id"}
        text = extract_text_content(str(message.get("content") or "")).strip()
        if not text:
            return HTTPStatus.OK, {"ok": True, "ignored": True}
        try:
            self._log(f"received webhook message chat_id={chat_id} text={text[:120]!r}")
            self.process_text_message(chat_id, text)
        except Exception as exc:
            self._recover_after_processing_error(chat_id, exc)
            self._log(
                f"failed to process webhook message chat_id={chat_id}: {exc!r}\n"
                f"{traceback.format_exc()}"
            )
            self._safe_send_text_message(chat_id, f"桥接处理失败: {exc}")
        return HTTPStatus.OK, {"ok": True}

    def _recover_after_processing_error(self, chat_id: str, exc: BaseException) -> None:
        with self._lock:
            had_pending_new_chat = chat_id in self._pending_ui_new_chats
            self._pending_ui_new_chats.discard(chat_id)
        mode = ""
        with contextlib.suppress(Exception):
            mode = self._message_mode()
        should_reset_haindy = mode == "simulate" or HaindySimulationSender.is_recoverable_error(exc)
        if should_reset_haindy:
            self.simulator.recover_after_error()
        self._log(
            "processed error recovery "
            f"chat_id={chat_id} reset_haindy={should_reset_haindy} "
            f"cleared_pending_new_chat={had_pending_new_chat}"
        )

    def process_text_message(self, chat_id: str, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        if normalized.startswith(self.settings.command_prefix):
            self._handle_command(chat_id, normalized)
            return
        self._handle_chat_message(chat_id, normalized)

    def _prepare_runtime(self) -> None:
        if not self.settings.app_id or not self.settings.app_secret:
            raise RuntimeError("Feishu bridge requires app_id/app_secret")
        self._load_bindings()
        self._load_session_snapshots()
        self._ensure_default_binding()
        self._start_all_watchers()

    def _build_server(self) -> ThreadingHTTPServer:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    self._write_json(HTTPStatus.OK, {"ok": True, "service": "codex-feishu-bridge"})
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:
                if self.path != "/webhook/feishu":
                    self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                content_length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                    return
                status, body = service.process_webhook_payload(payload)
                self._write_json(status, body)

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _write_json(self, status: int, body: dict[str, Any]) -> None:
                encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return ThreadingHTTPServer((self.settings.host, self.settings.port), Handler)

    def _load_bindings(self) -> None:
        raw = self.state_store.load()
        bindings: dict[str, ChatBinding] = {}
        for chat_id, item in raw.items():
            thread_id = str(item.get("thread_id") or "").strip()
            if not thread_id:
                continue
            bindings[chat_id] = ChatBinding(
                chat_id=chat_id,
                thread_id=thread_id,
                offset=max(int(item.get("offset") or 0), 0),
            )
        self._bindings = bindings

    def _save_bindings(self) -> None:
        self.state_store.save(self._bindings)

    def _load_session_snapshots(self) -> None:
        self._session_snapshots = self.session_snapshot_store.load()

    def _save_session_snapshots(self) -> None:
        self.session_snapshot_store.save(self._session_snapshots)

    def _ensure_default_binding(self) -> None:
        chat_id = self.settings.default_chat_id.strip()
        thread_id = self.settings.default_thread_id.strip()
        if not chat_id or not thread_id or chat_id in self._bindings:
            return
        rollout_path = self.bridge.resolve_rollout_path(thread_id)
        self._bindings[chat_id] = ChatBinding(
            chat_id=chat_id,
            thread_id=thread_id,
            offset=rollout_path.stat().st_size,
        )
        self._save_bindings()

    def _start_all_watchers(self) -> None:
        for chat_id, binding in list(self._bindings.items()):
            self._start_watcher(chat_id, binding.thread_id)

    def _start_watcher(self, chat_id: str, thread_id: str) -> None:
        existing = self._watchers.get(chat_id)
        if existing is not None:
            existing[1].set()
        stop_event = threading.Event()
        worker = threading.Thread(
            target=self._watch_thread,
            args=(chat_id, thread_id, stop_event),
            daemon=True,
            name=f"codex-feishu-watch-{chat_id}",
        )
        self._watchers[chat_id] = (worker, stop_event)
        worker.start()

    def _watch_thread(self, chat_id: str, thread_id: str, stop_event: threading.Event) -> None:
        thread: Any = None
        rollout_path: Path | None = None
        while not stop_event.is_set():
            try:
                thread = self.bridge.get_thread(thread_id)
                rollout_path = self.bridge.resolve_rollout_path(thread.thread_id)
                break
            except CodexBridgeError as exc:
                try:
                    rollout_path = self.bridge.resolve_rollout_path(thread_id)
                    thread = CodexThreadInfo(
                        thread_id=thread_id,
                        thread_name=thread_id,
                        updated_at="",
                    )
                    self._log(
                        "watcher using rollout without session index "
                        f"chat_id={chat_id} thread_id={thread_id}: {exc}"
                    )
                    break
                except CodexBridgeError:
                    pass
                time.sleep(max(self.settings.poll_interval_seconds, 0.2))
        if thread is None or rollout_path is None:
            return
        while not stop_event.is_set():
            with self._lock:
                binding = self._bindings.get(chat_id)
                if binding is None or binding.thread_id != thread_id:
                    return
                offset = binding.offset
            batch, next_offset = self.bridge.read_messages_since(rollout_path, thread, offset)
            if batch:
                try:
                    for message in batch:
                        if not self._should_forward(chat_id, message):
                            continue
                        self._forward_message(chat_id, message)
                except Exception as exc:
                    self._log(
                        "failed to forward Codex message "
                        f"chat_id={chat_id} thread_id={thread_id}: {exc!r}\n"
                        f"{traceback.format_exc()}"
                    )
                    time.sleep(max(self.settings.poll_interval_seconds, 0.2))
                    continue
                with self._lock:
                    current = self._bindings.get(chat_id)
                    if current and current.thread_id == thread_id:
                        current.offset = next_offset
                        self._save_bindings()
                continue
            time.sleep(max(self.settings.poll_interval_seconds, 0.2))

    def _should_forward(self, chat_id: str, message: CodexBridgeMessage) -> bool:
        if message.role == "assistant":
            return True
        if not self.settings.forward_desktop_user_messages:
            return False
        return not self._consume_suppressed_text(chat_id, message.text)

    def _consume_suppressed_text(self, chat_id: str, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return False
        now = time.time()
        entries = self._suppressions.get(chat_id, [])
        kept: list[tuple[float, str]] = []
        matched = False
        for ts, item_text in entries:
            if now - ts > 180.0:
                continue
            if not matched and item_text == normalized:
                matched = True
                continue
            kept.append((ts, item_text))
        self._suppressions[chat_id] = kept
        return matched

    def _remember_suppressed_text(self, chat_id: str, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        entries = self._suppressions.setdefault(chat_id, [])
        entries.append((time.time(), normalized))
        if len(entries) > 20:
            del entries[:-20]

    def _forward_message(self, chat_id: str, message: CodexBridgeMessage) -> None:
        if message.role == "assistant" and self._feishu_streaming_enabled():
            self._forward_assistant_streaming(chat_id, message)
            return
        self.feishu.send_text_message(chat_id, self._format_forward(message))

    def _forward_assistant_streaming(self, chat_id: str, message: CodexBridgeMessage) -> None:
        text = message.text.strip()
        if not text:
            return
        key = (chat_id, message.thread_id)
        phase = str(message.phase or "")
        try:
            with self._lock:
                state = self._streaming_replies.get(key)
            if state is None:
                state = StreamingReplyState(
                    card=self.feishu.create_streaming_card(chat_id),
                    progress_parts=[],
                )
                with self._lock:
                    self._streaming_replies[key] = state
                self._log(
                    "created Feishu streaming card "
                    f"chat_id={chat_id} thread_id={message.thread_id} "
                    f"card_id={state.card.card_id} message_id={state.card.message_id}"
                )
            if phase == "final_answer":
                self.feishu.finish_streaming_card(state.card, text)
                with self._lock:
                    self._streaming_replies.pop(key, None)
                return
            state.progress_parts.append(text)
            progress_text = "\n\n".join(part for part in state.progress_parts if part.strip())
            self.feishu.stream_card_content(state.card, progress_text)
        except Exception as exc:
            with self._lock:
                self._streaming_replies.pop(key, None)
                if self._is_feishu_streaming_scope_error(exc):
                    self._streaming_disabled_until = time.monotonic() + 600.0
            self._log(
                "Feishu streaming card failed, falling back to text "
                f"chat_id={chat_id} thread_id={message.thread_id}: {exc!r}"
            )
            self.feishu.send_text_message(chat_id, self._format_forward(message))

    def _feishu_streaming_enabled(self) -> bool:
        return (
            bool(getattr(self.settings, "feishu_streaming", False))
            and time.monotonic() >= self._streaming_disabled_until
        )

    @staticmethod
    def _is_feishu_streaming_scope_error(exc: BaseException) -> bool:
        message = str(exc)
        return "cardkit:card:write" in message or "Access denied" in message

    def _handle_command(self, chat_id: str, text: str) -> None:
        command_line = text[len(self.settings.command_prefix) :].strip()
        command, _, arg_text = command_line.partition(" ")
        command = command.lower()
        arg_text = arg_text.strip()
        if command == "help":
            self.feishu.send_text_message(chat_id, self._build_help_text())
            return
        if command == "sessions":
            self.feishu.send_text_message(chat_id, self._build_sessions_text(chat_id))
            return
        if command == "session":
            self.feishu.send_text_message(chat_id, self._handle_session_command(chat_id, arg_text))
            return
        if command == "attach":
            self.feishu.send_text_message(chat_id, self._attach_chat(chat_id, arg_text))
            return
        if command == "detach":
            self.feishu.send_text_message(chat_id, self._detach_chat(chat_id))
            return
        if command == "status":
            self.feishu.send_text_message(chat_id, self._build_status_text(chat_id))
            return
        if command == "mode":
            self.feishu.send_text_message(chat_id, self._handle_mode_command(arg_text))
            return
        if command in {"recover", "recovery"}:
            self.feishu.send_text_message(chat_id, self._handle_recover_command(chat_id))
            return
        if command in {"focus", "locate", "input"}:
            self.feishu.send_text_message(chat_id, self._handle_locate_input_command(chat_id))
            return
        self.feishu.send_text_message(chat_id, f"未识别命令: {command}\n\n{self._build_help_text()}")

    def _handle_chat_message(self, chat_id: str, text: str) -> None:
        mode = self._message_mode()
        if mode == "appserver":
            self._handle_appserver_chat_message(chat_id, text)
            return
        if mode == "direct":
            self._handle_direct_chat_message(chat_id, text)
            return
        if mode == "simulate":
            with self._lock:
                pending_new_chat = chat_id in self._pending_ui_new_chats
            if pending_new_chat:
                self._handle_pending_ui_new_chat_message(chat_id, text)
                return
            self._handle_simulated_chat_message(chat_id, text)
            return
        raise RuntimeError(f"Unsupported message_mode: {self.settings.message_mode!r}")

    def _handle_direct_chat_message(self, chat_id: str, text: str) -> None:
        self._remember_suppressed_text(chat_id, text)
        options = self._direct_turn_options()
        self._log(
            "direct mode starting isolated Codex thread "
            f"chat_id={chat_id} cwd={options.cwd} sandbox={options.sandbox}"
        )
        runner = AppServerTurnRunner(options)
        thread_id = runner.submit_new_thread_message(text)
        self._bind_chat_to_thread(chat_id, thread_id, offset=0)
        self._log(f"direct mode submitted message chat_id={chat_id} thread_id={thread_id}")

    def _handle_appserver_chat_message(self, chat_id: str, text: str) -> None:
        binding = self._require_binding(chat_id)
        if binding is None:
            return

        self._remember_suppressed_text(chat_id, text)
        options = self._appserver_turn_options()
        original_thread_id = binding.thread_id
        current_thread_id = binding.thread_id
        attempted_thread_ids: set[str] = set()
        max_rounds = self._appserver_auto_recovery_rounds()
        last_no_rollout_error: CodexBridgeError | None = None
        rebound_attempted = False
        self._log(
            "appserver mode submitting to bound Codex thread "
            f"chat_id={chat_id} thread_id={binding.thread_id} "
            f"cwd={options.cwd} sandbox={options.sandbox} "
            f"use_running_server={options.use_running_server} "
            f"websocket_url={options.websocket_url or ''}"
        )
        for round_number in range(1, max_rounds + 1):
            attempted_thread_ids.add(current_thread_id)
            try:
                AppServerTurnRunner(options).submit_existing_thread_message(current_thread_id, text)
                self._log(
                    "appserver mode submitted message "
                    f"chat_id={chat_id} thread_id={current_thread_id} round={round_number}"
                )
                if current_thread_id != original_thread_id:
                    self._safe_send_text_message(
                        chat_id,
                        "已自动处理 app-server resume 失败："
                        "原绑定线程不可恢复，已改绑最近线程并发送本条消息。\n"
                        f"原线程ID: {original_thread_id}\n"
                        f"当前线程ID: {current_thread_id}",
                    )
                return
            except CodexBridgeError as exc:
                if not self._is_appserver_no_rollout_error(exc):
                    raise
                last_no_rollout_error = exc
                self._log(
                    "appserver no-rollout recovery needed "
                    f"chat_id={chat_id} thread_id={current_thread_id} "
                    f"round={round_number}/{max_rounds}: {exc}"
                )
                if round_number >= max_rounds:
                    break
                if not rebound_attempted:
                    rebound_attempted = True
                    candidate = self._select_appserver_recovery_thread(attempted_thread_ids)
                    if candidate is not None:
                        current_thread_id = candidate.thread_id
                        self._bind_chat_to_thread(chat_id, current_thread_id)
                        self._log(
                            "appserver no-rollout recovery rebound to recent thread "
                            f"chat_id={chat_id} thread_id={current_thread_id} "
                            f"next_round={round_number + 1}/{max_rounds}"
                        )
                        continue
                if round_number + 1 <= max_rounds:
                    new_thread_id = self._submit_new_appserver_recovery_thread(
                        chat_id,
                        options,
                        text,
                        failed_thread_id=current_thread_id,
                        round_number=round_number + 1,
                        max_rounds=max_rounds,
                    )
                    self._safe_send_text_message(
                        chat_id,
                        "已自动处理 app-server resume 失败："
                        "原绑定线程不可恢复，已新建线程并发送本条消息。\n"
                        f"原线程ID: {original_thread_id}\n"
                        f"当前线程ID: {new_thread_id}",
                    )
                    return
        raise CodexBridgeError(
            f"App-server no-rollout 自动处理失败（最多 {max_rounds} 轮）: "
            f"{last_no_rollout_error}"
        ) from last_no_rollout_error

    def _appserver_auto_recovery_rounds(self) -> int:
        try:
            value = int(self.settings.appserver_auto_recovery_rounds)
        except (TypeError, ValueError):
            value = DEFAULT_APP_SERVER_AUTO_RECOVERY_ROUNDS
        return max(1, value)

    @staticmethod
    def _is_appserver_no_rollout_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return (
            "app-server thread/resume failed" in message
            and APP_SERVER_NO_ROLLOUT_MARKER in message
        )

    def _select_appserver_recovery_thread(
        self,
        attempted_thread_ids: set[str],
    ) -> CodexThreadInfo | None:
        try:
            threads = self.bridge.list_threads()
        except CodexBridgeError as exc:
            self._log(f"appserver no-rollout recovery could not list threads: {exc}")
            return None
        for thread in threads:
            if thread.thread_id in attempted_thread_ids:
                continue
            try:
                self.bridge.resolve_rollout_path(thread.thread_id)
            except CodexBridgeError:
                continue
            return thread
        return None

    def _submit_new_appserver_recovery_thread(
        self,
        chat_id: str,
        options: AppServerTurnOptions,
        text: str,
        *,
        failed_thread_id: str,
        round_number: int,
        max_rounds: int,
    ) -> str:
        self._log(
            "appserver no-rollout recovery creating new thread "
            f"chat_id={chat_id} failed_thread_id={failed_thread_id} "
            f"round={round_number}/{max_rounds}"
        )
        new_thread_id = AppServerTurnRunner(options).submit_new_thread_message(text)
        self._bind_chat_to_thread(chat_id, new_thread_id, offset=0)
        self._log(
            "appserver no-rollout recovery submitted in new thread "
            f"chat_id={chat_id} thread_id={new_thread_id} round={round_number}/{max_rounds}"
        )
        return new_thread_id

    def _handle_simulated_chat_message(self, chat_id: str, text: str) -> None:
        binding = self._require_binding(chat_id)
        if binding is None:
            return
        self._remember_suppressed_text(chat_id, text)
        self._log(f"simulate mode sending via HAINDY chat_id={chat_id} thread_id={binding.thread_id}")
        thread_name = self._thread_name_for_binding(binding.thread_id)
        session_id = self.simulator.send(text, thread_name=thread_name)
        self._log(
            f"simulate mode submitted via HAINDY chat_id={chat_id} thread_id={binding.thread_id} haindy_session={session_id}"
        )

    def _handle_pending_ui_new_chat_message(self, chat_id: str, text: str) -> None:
        self._remember_suppressed_text(chat_id, text)
        submitted_at = time.time()
        self._log(f"simulate mode sending pending new-chat message via HAINDY chat_id={chat_id}")
        session_id = self.simulator.send(text, thread_name="")
        thread, rollout_path = self._find_recent_user_message_thread(text, not_before=submitted_at - 1.0)
        with self._lock:
            self._pending_ui_new_chats.discard(chat_id)
            self._bindings[chat_id] = ChatBinding(
                chat_id=chat_id,
                thread_id=thread.thread_id,
                offset=rollout_path.stat().st_size,
            )
            self._save_bindings()
        self._start_watcher(chat_id, thread.thread_id)
        self._log(
            f"simulate mode bound pending new chat chat_id={chat_id} thread_id={thread.thread_id} haindy_session={session_id}"
        )

    def _require_binding(self, chat_id: str) -> ChatBinding | None:
        with self._lock:
            binding = self._bindings.get(chat_id)
        if binding is None:
            self.feishu.send_text_message(chat_id, UNBOUND_THREAD_HINT)
        return binding

    def _bind_chat_to_thread(self, chat_id: str, thread_id: str, *, offset: int | None = None) -> None:
        if offset is None:
            offset = 0
            try:
                rollout_path = self.bridge.resolve_rollout_path(thread_id)
                offset = rollout_path.stat().st_size
            except CodexBridgeError:
                pass
        with self._lock:
            self._bindings[chat_id] = ChatBinding(chat_id=chat_id, thread_id=thread_id, offset=offset)
            self._save_bindings()
        self._start_watcher(chat_id, thread_id)

    def _thread_name_for_binding(self, thread_id: str) -> str:
        try:
            thread = self.bridge.get_thread(thread_id)
            return str(thread.thread_name or thread_id or "")
        except CodexBridgeError:
            return str(thread_id or "")

    def _message_mode(self) -> str:
        mode = self.settings.message_mode.strip().lower()
        mode = MESSAGE_MODE_ALIASES.get(mode, mode)
        if mode not in MESSAGE_MODES:
            raise RuntimeError(f"message_mode must be one of {sorted(MESSAGE_MODES)}, got {mode!r}")
        return mode

    def _handle_mode_command(self, arg_text: str) -> str:
        target = arg_text.strip().lower()
        if not target:
            return f"当前消息模式: {self._message_mode()}\n可用: appserver, direct, simulate, a, d, s"
        target = MESSAGE_MODE_ALIASES.get(target, target)
        if target not in MESSAGE_MODES:
            return "用法: /mode appserver、/mode direct、/mode simulate、/mode a、/mode d 或 /mode s"
        self.settings.message_mode = target
        self._persist_message_mode(target)
        return f"已切换消息模式: {target}"

    def _persist_message_mode(self, mode: str) -> None:
        config_path = Path(__file__).resolve().parent / "local_settings.json"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            if not isinstance(payload, dict):
                payload = {}
            payload["message_mode"] = mode
            config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self._log(f"failed to persist message_mode={mode}: {exc!r}")

    def _handle_recover_command(self, chat_id: str) -> str:
        with self._lock:
            had_pending_new_chat = chat_id in self._pending_ui_new_chats
            self._pending_ui_new_chats.discard(chat_id)
        self.simulator.recover_after_error()
        self._log(
            "manual recovery completed "
            f"chat_id={chat_id} cleared_pending_new_chat={had_pending_new_chat}"
        )
        return (
            "已执行恢复：清理 HAINDY session 缓存、终止残留 HAINDY daemon，"
            "并清除本飞书会话的新建对话 pending 标记。\n"
            "当前 Codex 线程绑定未修改。"
        )

    def _handle_session_command(self, chat_id: str, arg_text: str) -> str:
        command = arg_text.strip().lower()
        if command != "new":
            return "用法: /session new"
        mode = self._message_mode()
        if mode in {"appserver", "direct"}:
            options = self._appserver_turn_options() if mode == "appserver" else self._direct_turn_options()
            thread_id = self._start_appserver_thread(chat_id, options)
            return f"已新建 {mode} Codex 线程并绑定。\n线程ID: {thread_id}"
        session_id = self.simulator.new_chat()
        with self._lock:
            self._pending_ui_new_chats.add(chat_id)
        return (
            "已在 Codex UI 触发新建对话。\n"
            "下一条普通消息会发送到这个新对话，并自动绑定真实线程。\n"
            f"HAINDY session: {session_id}"
        )

    def _handle_locate_input_command(self, chat_id: str) -> str:
        if self._message_mode() != "simulate":
            return "手动定位输入框只在 simulate 模式可用。先用 `/mode s` 切换。"
        with self._lock:
            binding = self._bindings.get(chat_id)
        thread_name = self._thread_name_for_binding(binding.thread_id) if binding else ""
        self._log(f"simulate mode manually locating Codex input chat_id={chat_id}")
        session_id = self.simulator.locate_input(thread_name=thread_name)
        return (
            "已重新截图定位并聚焦 Codex 消息输入框。\n"
            "后续普通消息会优先使用这次缓存的坐标。\n"
            f"HAINDY session: {session_id}"
        )

    def _start_appserver_thread(self, chat_id: str, options: AppServerTurnOptions) -> str:
        thread_id = AppServerTurnRunner(options).create_thread()
        self._bind_chat_to_thread(chat_id, thread_id)
        return thread_id

    def _direct_turn_options(self) -> AppServerTurnOptions:
        return self._build_turn_options(
            cwd=self.settings.direct_cwd,
            sandbox=self.settings.direct_sandbox,
            approval_policy=self.settings.direct_approval_policy,
            model=self.settings.direct_model,
            timeout_seconds=self.settings.direct_turn_timeout_seconds,
            use_running_server=False,
        )

    def _appserver_turn_options(self) -> AppServerTurnOptions:
        return self._build_turn_options(
            cwd=self.settings.appserver_cwd or self.settings.direct_cwd,
            sandbox=self.settings.appserver_sandbox or self.settings.direct_sandbox,
            approval_policy=(
                self.settings.appserver_approval_policy
                or self.settings.direct_approval_policy
            ),
            model=self.settings.appserver_model or self.settings.direct_model,
            timeout_seconds=(
                self.settings.appserver_turn_timeout_seconds
                or self.settings.direct_turn_timeout_seconds
                or 600
            ),
            use_running_server=bool(self.settings.appserver_use_running_server),
            websocket_url=self.settings.appserver_websocket_url,
        )

    @staticmethod
    def _build_turn_options(
        *,
        cwd: Path,
        sandbox: str,
        approval_policy: str,
        model: str,
        timeout_seconds: float,
        use_running_server: bool,
        websocket_url: str = "",
    ) -> AppServerTurnOptions:
        return AppServerTurnOptions(
            cwd=cwd.expanduser().resolve(),
            sandbox=str(sandbox or "").strip() or "danger-full-access",
            approval_policy=str(approval_policy or "").strip() or None,
            model=str(model or "").strip() or None,
            timeout_seconds=max(float(timeout_seconds), 30.0),
            use_running_server=use_running_server,
            websocket_url=str(websocket_url or "").strip() or None,
        )

    def _build_help_text(self) -> str:
        prefix = self.settings.command_prefix
        return (
            "可用命令:\n"
            f"{prefix}sessions 查看最近 Codex 线程\n"
            f"{prefix}session new 新建 Codex 对话\n"
            f"{prefix}attach <编号或线程ID> 绑定到已有线程；simulate 下 1-9 优先用 Ctrl+数字切换\n"
            f"{prefix}attach n <编号> 按 Codex 侧栏编号切换；1-9 优先用 Ctrl+数字，超过 9 才走行号兜底\n"
            f"{prefix}focus 手动截图定位并缓存 Codex 消息输入框坐标\n"
            f"{prefix}status 查看当前绑定状态\n"
            f"{prefix}mode [appserver|direct|simulate|a|d|s] 查看或切换消息模式\n"
            f"{prefix}recover 清理 HAINDY 运行态但保留当前绑定\n"
            f"{prefix}detach 解除当前绑定\n"
            "appserver 会 resume 绑定线程并通过 Codex app-server 发 turn；"
            "direct 会新开独立 Codex 线程；simulate 会通过 HAINDY 操作当前 Codex UI。"
        )

    def _build_sessions_text(self, chat_id: str) -> str:
        threads = self.bridge.list_threads()
        if not threads:
            return "没有找到可用的 Codex 线程。"
        visible_threads = threads[:10]
        with self._lock:
            self._session_snapshots[chat_id] = visible_threads
            self._save_session_snapshots()
        lines = ["最近 Codex 线程（按最近对话时间排序，最多 10 个）:"]
        for index, item in enumerate(visible_threads, start=1):
            lines.append(f"{index}. {item.thread_name}")
            lines.append(f"ID: {item.thread_id}")
        lines.append(
            "使用 `/attach 1` 或 `/attach <线程ID>` 绑定。数字编号固定为本次列表快照；"
            "appserver 下后续消息会直接 resume 该线程；simulate 下 1-9 走 Codex Ctrl+数字快捷键。"
        )
        return "\n".join(lines)

    def _attach_chat(self, chat_id: str, arg_text: str) -> str:
        target = arg_text.strip()
        if not target:
            return "用法: /attach <编号或线程ID>，或 /attach n <编号>"
        row_switch_number: int | None = None
        parts = target.split()
        if parts and parts[0].lower() == "n":
            if len(parts) != 2 or not parts[1].isdigit():
                return "用法: /attach n <编号>，例如 /attach n 1"
            row_switch_number = int(parts[1])
            target = parts[1]
        live_threads = self.bridge.list_threads()
        thread_id = ""
        thread_name = ""
        shortcut_number: int | None = None
        if target.lower() == "latest":
            if live_threads:
                thread_id = live_threads[0].thread_id
                thread_name = live_threads[0].thread_name
                shortcut_number = 1
        elif target.isdigit():
            index = int(target) - 1
            if row_switch_number is not None:
                if 0 <= index < len(live_threads):
                    thread_id = live_threads[index].thread_id
                    thread_name = live_threads[index].thread_name
                    if 0 <= index < 9:
                        shortcut_number = index + 1
            else:
                with self._lock:
                    snapshot = list(self._session_snapshots.get(chat_id) or [])
                if not snapshot:
                    return "数字编号需要先发 `/sessions` 刷新列表快照；也可以直接用 `/attach <线程ID>`。"
                if 0 <= index < len(snapshot):
                    thread_id = snapshot[index].thread_id
                    thread_name = snapshot[index].thread_name
                    if 0 <= index < 9:
                        shortcut_number = index + 1
        else:
            thread_id = target
            with self._lock:
                snapshot = list(self._session_snapshots.get(chat_id) or [])
            snapshot_match = next(
                ((index, item) for index, item in enumerate(snapshot) if item.thread_id == target),
                None,
            )
            if snapshot_match is not None and snapshot_match[0] < 9:
                shortcut_number = snapshot_match[0] + 1
            live_match = next(
                ((index, item) for index, item in enumerate(live_threads) if item.thread_id == target),
                None,
            )
            if shortcut_number is None and live_match is not None and live_match[0] < 9:
                shortcut_number = live_match[0] + 1
            match = live_match[1] if live_match is not None else None
            thread_name = match.thread_name if match else thread_id
        if not thread_id:
            return "没有找到目标线程，请先发 `/sessions` 查看。"
        mode = self._message_mode()
        if mode == "simulate":
            self._log(f"simulate mode switching Codex UI chat_id={chat_id} thread_id={thread_id}")
            try:
                if shortcut_number is not None:
                    try:
                        session_id = self.simulator.switch_thread_by_shortcut(shortcut_number)
                        switch_method = f"快捷键 Ctrl+{shortcut_number}"
                    except RuntimeError as shortcut_exc:
                        self._log(
                            "shortcut attach failed, falling back "
                            f"chat_id={chat_id} thread_id={thread_id}: {shortcut_exc!r}"
                        )
                        if row_switch_number is not None:
                            session_id = self.simulator.switch_thread_by_row(
                                row_switch_number,
                                thread_name or thread_id,
                            )
                            switch_method = (
                                f"侧栏第 {row_switch_number} 行"
                                f"（快捷键 Ctrl+{shortcut_number} 失败后兜底）"
                            )
                        else:
                            session_id = self.simulator.switch_thread(thread_name or thread_id)
                            switch_method = (
                                f"标题 OCR（快捷键 Ctrl+{shortcut_number} 失败后兜底）"
                            )
                elif row_switch_number is not None:
                    session_id = self.simulator.switch_thread_by_row(
                        row_switch_number,
                        thread_name or thread_id,
                    )
                    switch_method = f"侧栏第 {row_switch_number} 行"
                else:
                    session_id = self.simulator.switch_thread(thread_name or thread_id)
                    switch_method = "标题 OCR"
            except RuntimeError as exc:
                self._log(
                    "simulate attach failed before binding update "
                    f"chat_id={chat_id} thread_id={thread_id}: {exc!r}"
                )
                return (
                    "切换 Codex UI 失败，已保留原绑定，没有写入新的绑定状态。\n"
                    f"目标线程: {thread_name or thread_id}\n"
                    f"线程ID: {thread_id}\n"
                    f"错误: {exc}\n"
                    "可以先发 `/mode d` 切到 direct，或稍后再试 simulate 切换。"
                )
            self._bind_chat_to_thread(chat_id, thread_id)
            with self._lock:
                self._pending_ui_new_chats.discard(chat_id)
                self._save_bindings()
            return (
                f"已绑定并切换 Codex UI 到线程: {thread_name}\n"
                f"切换方式: {switch_method}\n"
                f"线程ID: {thread_id}\n执行: {session_id}"
            )
        if mode == "appserver":
            try:
                AppServerTurnRunner(self._appserver_turn_options()).ensure_existing_thread_ready(thread_id)
            except CodexBridgeError as exc:
                return (
                    "绑定失败：目标线程无法通过 Codex app-server 恢复。\n"
                    f"目标线程: {thread_name or thread_id}\n"
                    f"线程ID: {thread_id}\n"
                    f"错误: {exc}"
                )
        self._bind_chat_to_thread(chat_id, thread_id)
        with self._lock:
            self._pending_ui_new_chats.discard(chat_id)
            self._save_bindings()
        if mode == "appserver":
            return f"已绑定到 Codex app-server 线程: {thread_name}\n线程ID: {thread_id}"
        return f"已绑定到 Codex 线程: {thread_name}\n线程ID: {thread_id}"

    def _find_recent_user_message_thread(
        self,
        text: str,
        *,
        not_before: float,
        timeout_seconds: float = 10.0,
    ) -> tuple[Any, Path]:
        normalized = text.strip()
        deadline = time.time() + timeout_seconds
        while True:
            threads = self.bridge.list_threads()
            for thread in threads[:25]:
                try:
                    rollout_path = self.bridge.resolve_rollout_path(thread.thread_id)
                except CodexBridgeError:
                    continue
                if rollout_path.stat().st_mtime < not_before:
                    continue
                messages = self.bridge.load_history(rollout_path, thread, limit=30)
                for message in reversed(messages):
                    if message.role == "user" and message.text.strip() == normalized:
                        return thread, rollout_path
            if time.time() >= deadline:
                break
            time.sleep(0.5)
        raise RuntimeError("已发送到 Codex UI，但无法从 rollout 反查新线程 ID。请用 `/sessions` 后 `/attach <编号>` 手动绑定。")

    def _detach_chat(self, chat_id: str) -> str:
        with self._lock:
            binding = self._bindings.pop(chat_id, None)
            self._save_bindings()
        watcher = self._watchers.pop(chat_id, None)
        if watcher is not None:
            watcher[1].set()
        if binding is None:
            return "当前会话没有绑定 Codex 线程。"
        return f"已解除绑定: {binding.thread_id}"

    def _build_status_text(self, chat_id: str) -> str:
        with self._lock:
            binding = self._bindings.get(chat_id)
        if binding is None:
            return f"当前会话未绑定 Codex 线程。\n消息模式: {self._message_mode()}"
        try:
            thread = self.bridge.get_thread(binding.thread_id)
            thread_name = thread.thread_name
        except CodexBridgeError:
            thread_name = binding.thread_id
        return f"当前绑定线程: {thread_name}\n线程ID: {binding.thread_id}\n消息模式: {self._message_mode()}"

    def _safe_send_text_message(self, chat_id: str, text: str) -> None:
        try:
            self.feishu.send_text_message(chat_id, text)
        except Exception:
            self._log(f"failed to send Feishu message chat_id={chat_id}")
            return

    @staticmethod
    def _format_forward(message: CodexBridgeMessage) -> str:
        if message.role == "assistant":
            return message.text.strip()
        speaker = "Codex" if message.role == "assistant" else "User"
        return f"[{speaker}][{message.thread_name}]\n{message.text.strip()}"

    @staticmethod
    def _log(message: str) -> None:
        line = f"[bridge] {message}"
        print(line, flush=True)
        log_path = Path(__file__).resolve().parent / "runtime" / "bridge.stdout.log"
        with contextlib.suppress(Exception):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")

