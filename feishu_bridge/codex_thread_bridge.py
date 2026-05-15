from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterator


FORWARDABLE_EVENT_TYPES = {"user_message", "agent_message"}


@dataclass(slots=True)
class CodexThreadInfo:
    thread_id: str
    thread_name: str
    updated_at: str


@dataclass(slots=True)
class CodexBridgeMessage:
    thread_id: str
    thread_name: str
    timestamp: str
    role: str
    phase: str | None
    text: str


class CodexBridgeError(RuntimeError):
    """Raised when a Codex desktop thread cannot be discovered or parsed."""


def _resolve_codex_command() -> list[str]:
    if sys.platform != "win32":
        return [shutil.which("codex") or "codex"]

    wrapper = shutil.which("codex") or shutil.which("codex.ps1")
    if not wrapper:
        return ["codex"]

    wrapper_path = Path(wrapper)
    if wrapper_path.suffix.lower() != ".ps1":
        return [str(wrapper_path)]

    base_dir = wrapper_path.parent
    vendor_binary = (
        base_dir
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "codex"
        / "codex.exe"
    )
    if vendor_binary.exists():
        return [str(vendor_binary)]

    node_binary = base_dir / "node.exe"
    codex_js = base_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if node_binary.exists() and codex_js.exists():
        return [str(node_binary), str(codex_js)]

    return ["pwsh", "-File", str(wrapper_path)]


class CodexThreadBridgeService:
    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = (codex_home or (Path.home() / ".codex")).expanduser().resolve()
        self.session_index_path = self.codex_home / "session_index.jsonl"
        self.sessions_dir = self.codex_home / "sessions"
        self.archived_sessions_dir = self.codex_home / "archived_sessions"

    def list_threads(self) -> list[CodexThreadInfo]:
        if not self.session_index_path.exists():
            raise CodexBridgeError(f"Codex session index not found: {self.session_index_path}")

        threads_by_id: dict[str, CodexThreadInfo] = {}
        for raw_line in self.session_index_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = str(payload.get("id") or "").strip()
            if not thread_id:
                continue
            thread = CodexThreadInfo(
                thread_id=thread_id,
                thread_name=str(payload.get("thread_name") or thread_id),
                updated_at=str(payload.get("updated_at") or ""),
            )
            existing = threads_by_id.get(thread_id)
            if existing and thread.updated_at < existing.updated_at:
                continue
            if existing and (not thread.thread_name or thread.thread_name == thread_id) and existing.thread_name:
                thread = CodexThreadInfo(
                    thread_id=thread.thread_id,
                    thread_name=existing.thread_name,
                    updated_at=thread.updated_at,
                )
            threads_by_id[thread_id] = thread
        threads = list(threads_by_id.values())
        threads.sort(key=self._thread_recent_timestamp, reverse=True)
        return threads

    def get_thread(self, thread_id: str | None = None) -> CodexThreadInfo:
        threads = self.list_threads()
        if not threads:
            raise CodexBridgeError("No Codex desktop threads found.")
        if not thread_id:
            return threads[0]
        for thread in threads:
            if thread.thread_id == thread_id:
                return thread
        raise CodexBridgeError(f"Thread not found in session index: {thread_id}")

    def resolve_rollout_path(self, thread_id: str) -> Path:
        matches: list[Path] = []
        for root in (self.sessions_dir, self.archived_sessions_dir):
            if not root.exists():
                continue
            matches.extend(root.rglob(f"rollout-*{thread_id}.jsonl"))
        if not matches:
            raise CodexBridgeError(f"Rollout file not found for thread: {thread_id}")
        matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return matches[0]

    def _thread_recent_timestamp(self, thread: CodexThreadInfo) -> float:
        try:
            return self.resolve_rollout_path(thread.thread_id).stat().st_mtime
        except CodexBridgeError:
            pass
        try:
            normalized = thread.updated_at.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except (ValueError, OSError):
            return 0.0

    def load_history(self, rollout_path: Path, thread: CodexThreadInfo, limit: int) -> list[CodexBridgeMessage]:
        if limit <= 0:
            return []
        recent: deque[CodexBridgeMessage] = deque(maxlen=limit)
        with rollout_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                message = self._parse_line(raw_line, thread)
                if message is not None:
                    recent.append(message)
        return list(recent)

    def read_messages_since(
        self,
        rollout_path: Path,
        thread: CodexThreadInfo,
        offset: int,
    ) -> tuple[list[CodexBridgeMessage], int]:
        messages: list[CodexBridgeMessage] = []
        with rollout_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    return messages, handle.tell()
                if not raw_line.endswith(b"\n"):
                    handle.seek(line_start)
                    return messages, line_start
                try:
                    decoded = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                message = self._parse_line(decoded, thread)
                if message is not None:
                    messages.append(message)

    def follow_messages(
        self,
        rollout_path: Path,
        thread: CodexThreadInfo,
        offset: int,
        poll_interval: float,
    ) -> Iterator[tuple[list[CodexBridgeMessage], int]]:
        current_offset = offset
        while True:
            batch, next_offset = self.read_messages_since(rollout_path, thread, current_offset)
            if batch:
                current_offset = next_offset
                yield batch, current_offset
                continue
            current_offset = next_offset
            time.sleep(poll_interval)

    @staticmethod
    def format_message(message: CodexBridgeMessage) -> str:
        phase = message.phase or "message"
        return f"[Codex][{message.thread_name}][{message.role}][{phase}]\n{message.text.strip()}"

    def _parse_line(self, raw_line: str, thread: CodexThreadInfo) -> CodexBridgeMessage | None:
        line = raw_line.strip()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if payload.get("type") != "event_msg":
            return None
        body = payload.get("payload") or {}
        event_type = str(body.get("type") or "")
        if event_type not in FORWARDABLE_EVENT_TYPES:
            return None
        text = str(body.get("message") or "").strip()
        if not text:
            return None
        role = "assistant" if event_type == "agent_message" else "user"
        phase = body.get("phase")
        if phase is not None:
            phase = str(phase)
        return CodexBridgeMessage(
            thread_id=thread.thread_id,
            thread_name=thread.thread_name,
            timestamp=str(payload.get("timestamp") or ""),
            role=role,
            phase=phase,
            text=text,
        )

    @staticmethod
    def extract_agent_messages_from_turn(thread: CodexThreadInfo, turn: dict[str, Any]) -> list[CodexBridgeMessage]:
        messages: list[CodexBridgeMessage] = []
        for item in turn.get("items", []):
            if item.get("type") != "agentMessage":
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            phase = item.get("phase")
            if phase is not None:
                phase = str(phase)
            messages.append(
                CodexBridgeMessage(
                    thread_id=thread.thread_id,
                    thread_name=thread.thread_name,
                    timestamp="",
                    role="assistant",
                    phase=phase,
                    text=text,
                )
            )
        return messages


class CodexAppServerClient:
    def __init__(
        self,
        command: list[str] | None = None,
        *,
        use_running_server: bool = True,
        process_cwd: Path | None = None,
        websocket_url: str | None = None,
    ) -> None:
        app_server_args = ["app-server", "proxy"] if use_running_server else ["app-server", "--listen", "stdio://"]
        self._command = list(command or _resolve_codex_command()) + app_server_args
        self._process_cwd = process_cwd
        self._websocket_url = str(websocket_url or "").strip()
        self._proc: subprocess.Popen[str] | None = None
        self._ws: Any | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._pending: dict[str, Queue[dict[str, Any]]] = {}
        self._notifications: Queue[dict[str, Any]] = Queue()
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._reader_error: str = ""
        self._next_id = 1
        self._lock = threading.Lock()

    def __enter__(self) -> "CodexAppServerClient":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None or self._ws is not None:
            return
        if self._websocket_url:
            self._start_websocket()
        else:
            self._start_process()
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "codex-feishu-bridge", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=15.0,
        )
        self.notify("initialized", {})

    def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _start_process(self) -> None:
        self._proc = subprocess.Popen(
            self._command,
            cwd=str(self._process_cwd) if self._process_cwd is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()

    def _start_websocket(self) -> None:
        try:
            import websocket
        except ImportError as exc:
            raise CodexBridgeError(
                "websocket-client is required for appserver_websocket_url; "
                "install it with `python -m pip install websocket-client`"
            ) from exc
        self._ws = websocket.create_connection(
            self._websocket_url,
            timeout=15,
            suppress_origin=True,
        )
        self._reader = threading.Thread(target=self._read_websocket, daemon=True)
        self._reader.start()

    def list_threads(self, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.request("thread/list", dict(params or {}), timeout=15.0)
        return list(result.get("data") or [])

    def start_thread(
        self,
        *,
        cwd: Path | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = str(cwd)
        if sandbox:
            params["sandbox"] = sandbox
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if model:
            params["model"] = model
        result = self.request("thread/start", params, timeout=20.0)
        return dict(result.get("thread") or {})

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return self.request("thread/resume", {"threadId": thread_id}, timeout=20.0)

    def list_loaded_threads(self, limit: int | None = None) -> list[str]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        result = self.request("thread/loaded/list", params, timeout=10.0)
        return [str(item) for item in result.get("data") or []]

    def read_thread(self, thread_id: str, include_turns: bool = False) -> dict[str, Any]:
        result = self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
            timeout=20.0,
        )
        return dict(result.get("thread") or {})

    def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        cwd: Path | None = None,
        sandbox_policy: dict[str, Any] | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if cwd is not None:
            params["cwd"] = str(cwd)
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if model:
            params["model"] = model
        result = self.request(
            "turn/start",
            params,
            timeout=20.0,
        )
        return dict(result.get("turn") or {})

    def wait_for_turn_completed(self, thread_id: str, turn_id: str, timeout: float = 180.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.1)
            try:
                notification = self._notifications.get(timeout=remaining)
            except Empty:
                continue
            if notification.get("method") != "turn/completed":
                continue
            params = notification.get("params") or {}
            turn = params.get("turn") or {}
            if params.get("threadId") == thread_id and turn.get("id") == turn_id:
                return turn
        raise CodexBridgeError(f"Timed out waiting for turn completion: {turn_id}")

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            request_id = str(self._next_id)
            self._next_id += 1
            queue: Queue[dict[str, Any]] = Queue()
            self._pending[request_id] = queue
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.time() + timeout
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise Empty
                try:
                    response = queue.get(timeout=min(remaining, 0.2))
                    break
                except Empty:
                    proc = self._proc
                    if proc is not None and proc.poll() is not None:
                        raise
                    if self._ws is not None and self._reader_error:
                        raise
        except Empty as exc:
            self._pending.pop(request_id, None)
            detail = self._build_process_error_detail()
            message = f"Timed out waiting for app-server response: {method}"
            if detail:
                message = f"{message}; {detail}"
            raise CodexBridgeError(message) from exc
        if "error" in response:
            raise CodexBridgeError(f"App-server {method} failed: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            return {}
        return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is not None:
            self._ws.send(json.dumps(payload, ensure_ascii=False))
            return
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise CodexBridgeError("Codex app-server is not running.")
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = payload.get("id")
            if response_id is not None:
                queue = self._pending.pop(str(response_id), None)
                if queue is not None:
                    queue.put(payload)
                continue
            if isinstance(payload.get("method"), str):
                self._notifications.put(payload)

    def _read_websocket(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            while True:
                raw_message = ws.recv()
                if not raw_message:
                    return
                if isinstance(raw_message, bytes):
                    raw_message = raw_message.decode("utf-8")
                try:
                    payload = json.loads(str(raw_message).strip())
                except json.JSONDecodeError:
                    continue
                response_id = payload.get("id")
                if response_id is not None:
                    queue = self._pending.pop(str(response_id), None)
                    if queue is not None:
                        queue.put(payload)
                    continue
                if isinstance(payload.get("method"), str):
                    self._notifications.put(payload)
        except Exception as exc:
            if self._ws is not None:
                self._reader_error = repr(exc)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            stripped = line.strip()
            if stripped:
                self._stderr_lines.append(stripped)

    def _build_process_error_detail(self) -> str:
        proc = self._proc
        parts: list[str] = []
        if proc is not None and proc.poll() is not None:
            parts.append(f"process exited with code {proc.returncode}")
        if self._websocket_url:
            parts.append(f"websocket_url={self._websocket_url}")
        if self._reader_error:
            parts.append(f"reader error: {self._reader_error}")
        if self._stderr_lines:
            parts.append("stderr: " + " | ".join(self._stderr_lines))
        return "; ".join(parts)
