from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from feishu_bridge.codex_thread_bridge import CodexAppServerClient, CodexBridgeError


@dataclass(slots=True)
class AppServerTurnOptions:
    cwd: Path
    sandbox: str
    approval_policy: str | None
    model: str | None
    timeout_seconds: float
    use_running_server: bool = False
    websocket_url: str | None = None
    wait_for_completion: bool = False


class AppServerTurnRunner:
    def __init__(self, options: AppServerTurnOptions) -> None:
        self.options = options

    def create_thread(self) -> str:
        with self._open_client() as client:
            return self._create_thread(client)

    def submit_new_thread_message(self, text: str) -> str:
        client = self._open_client()
        background_owns_client = False
        try:
            client.start()
            thread_id = self._create_thread(client)
            background_owns_client = self._start_turn(client, thread_id, text)
            return thread_id
        finally:
            if not background_owns_client:
                client.close()

    def submit_existing_thread_message(self, thread_id: str, text: str) -> None:
        client = self._open_client()
        background_owns_client = False
        try:
            client.start()
            self._ensure_thread_ready(client, thread_id)
            background_owns_client = self._start_turn(client, thread_id, text)
        finally:
            if not background_owns_client:
                client.close()

    def ensure_existing_thread_ready(self, thread_id: str) -> None:
        with self._open_client() as client:
            self._ensure_thread_ready(client, thread_id)

    def _create_thread(self, client: CodexAppServerClient) -> str:
        thread = client.start_thread(
            cwd=self.options.cwd,
            sandbox=self.options.sandbox,
            approval_policy=self.options.approval_policy,
            model=self.options.model,
        )
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            raise RuntimeError(f"Codex app-server did not return a thread id: {thread}")
        return thread_id

    def _open_client(self) -> CodexAppServerClient:
        return CodexAppServerClient(
            use_running_server=self.options.use_running_server,
            process_cwd=self.options.cwd,
            websocket_url=self.options.websocket_url,
        )

    def _should_resume_thread(self, client: CodexAppServerClient, thread_id: str) -> bool:
        return not self._thread_is_loaded(client, thread_id)

    def _ensure_thread_ready(self, client: CodexAppServerClient, thread_id: str) -> None:
        if not self._should_resume_thread(client, thread_id):
            return
        try:
            client.resume_thread(thread_id)
        except CodexBridgeError as exc:
            if not self._can_continue_loaded_thread_after_resume_error(client, thread_id, exc):
                raise

    def _thread_is_loaded(self, client: CodexAppServerClient, thread_id: str) -> bool:
        with contextlib.suppress(Exception):
            return thread_id in set(client.list_loaded_threads(limit=200))
        return False

    def _can_continue_loaded_thread_after_resume_error(
        self,
        client: CodexAppServerClient,
        thread_id: str,
        exc: CodexBridgeError,
    ) -> bool:
        return self._is_stale_path_resume_error(exc) and self._thread_is_loaded(client, thread_id)

    @staticmethod
    def _is_stale_path_resume_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return "cannot resume running thread" in message and "stale path" in message

    def _start_turn(
        self,
        client: CodexAppServerClient,
        thread_id: str,
        text: str,
    ) -> bool:
        turn = client.start_turn(
            thread_id,
            text,
            cwd=self.options.cwd,
            sandbox_policy=self._turn_sandbox_policy(),
            approval_policy=self.options.approval_policy,
            model=self.options.model,
        )
        turn_id = str(turn.get("id") or "").strip()
        if not turn_id:
            return False
        if self.options.wait_for_completion:
            client.wait_for_turn_completed(
                thread_id,
                turn_id,
                timeout=max(float(self.options.timeout_seconds), 30.0),
            )
            return False
        self._wait_and_close_in_background(client, thread_id, turn_id)
        return True

    def _wait_and_close_in_background(
        self,
        client: CodexAppServerClient,
        thread_id: str,
        turn_id: str,
    ) -> None:
        timeout = max(float(self.options.timeout_seconds), 30.0)

        def worker() -> None:
            try:
                with contextlib.suppress(Exception):
                    client.wait_for_turn_completed(thread_id, turn_id, timeout=timeout)
            finally:
                with contextlib.suppress(Exception):
                    client.close()

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"codex-appserver-turn-{turn_id}",
        ).start()

    def _turn_sandbox_policy(self) -> dict[str, Any]:
        normalized = self.options.sandbox.strip().lower()
        if normalized == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if normalized == "workspace-write":
            return {
                "type": "workspaceWrite",
                "networkAccess": True,
                "writableRoots": [str(self.options.cwd)],
            }
        if normalized == "read-only":
            return {"type": "readOnly", "networkAccess": True}
        return {"type": "dangerFullAccess"}
