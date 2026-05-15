from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from feishu_bridge.appserver_sender import AppServerTurnOptions, AppServerTurnRunner
from feishu_bridge.codex_thread_bridge import CodexBridgeError


class FakeAppServerClient:
    def __init__(
        self,
        *,
        loaded_results: list[Any] | None = None,
        resume_error: CodexBridgeError | None = None,
    ) -> None:
        self.loaded_results = list(loaded_results or [])
        self.resume_error = resume_error
        self.started = False
        self.closed = False
        self.resume_calls: list[str] = []
        self.turn_calls: list[dict[str, Any]] = []

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeAppServerClient":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def list_loaded_threads(self, limit: int | None = None) -> list[str]:
        if not self.loaded_results:
            return []
        result = self.loaded_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return list(result)

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        self.resume_calls.append(thread_id)
        if self.resume_error is not None:
            raise self.resume_error
        return {}

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
        self.turn_calls.append(
            {
                "thread_id": thread_id,
                "text": text,
                "cwd": cwd,
                "sandbox_policy": sandbox_policy,
                "approval_policy": approval_policy,
                "model": model,
            }
        )
        return {"id": "turn-1"}

    def wait_for_turn_completed(self, thread_id: str, turn_id: str, timeout: float = 180.0) -> dict[str, Any]:
        return {"id": turn_id, "thread_id": thread_id}


class FakeRunner(AppServerTurnRunner):
    def __init__(self, client: FakeAppServerClient) -> None:
        super().__init__(
            AppServerTurnOptions(
                cwd=Path("C:/workspace"),
                sandbox="danger-full-access",
                approval_policy="never",
                model=None,
                timeout_seconds=30,
                wait_for_completion=True,
            )
        )
        self.client = client

    def _open_client(self) -> FakeAppServerClient:  # type: ignore[override]
        return self.client


class AppServerTurnRunnerTests(unittest.TestCase):
    def test_loaded_thread_skips_resume(self) -> None:
        client = FakeAppServerClient(loaded_results=[["thread-1"]])

        FakeRunner(client).submit_existing_thread_message("thread-1", "hello")

        self.assertTrue(client.started)
        self.assertEqual(client.resume_calls, [])
        self.assertEqual(len(client.turn_calls), 1)
        self.assertEqual(client.turn_calls[0]["thread_id"], "thread-1")

    def test_ensure_existing_thread_ready_loads_unloaded_thread_without_turn(self) -> None:
        client = FakeAppServerClient(loaded_results=[[]])

        FakeRunner(client).ensure_existing_thread_ready("thread-1")

        self.assertTrue(client.started)
        self.assertTrue(client.closed)
        self.assertEqual(client.resume_calls, ["thread-1"])
        self.assertEqual(client.turn_calls, [])

    def test_ensure_existing_thread_ready_skips_loaded_thread(self) -> None:
        client = FakeAppServerClient(loaded_results=[["thread-1"]])

        FakeRunner(client).ensure_existing_thread_ready("thread-1")

        self.assertEqual(client.resume_calls, [])
        self.assertEqual(client.turn_calls, [])

    def test_unloaded_thread_resumes_before_turn(self) -> None:
        client = FakeAppServerClient(loaded_results=[[]])

        FakeRunner(client).submit_existing_thread_message("thread-1", "hello")

        self.assertEqual(client.resume_calls, ["thread-1"])
        self.assertEqual(len(client.turn_calls), 1)

    def test_stale_path_resume_error_continues_when_thread_is_loaded(self) -> None:
        client = FakeAppServerClient(
            loaded_results=[[], ["thread-1"]],
            resume_error=CodexBridgeError(
                "App-server thread/resume failed: cannot resume running thread thread-1 "
                "with stale path: requested `C:\\Users\\x\\rollout.jsonl`, "
                "active `\\\\?\\C:\\Users\\x\\rollout.jsonl`"
            ),
        )

        FakeRunner(client).submit_existing_thread_message("thread-1", "hello")

        self.assertEqual(client.resume_calls, ["thread-1"])
        self.assertEqual(len(client.turn_calls), 1)

    def test_non_stale_resume_error_is_raised(self) -> None:
        client = FakeAppServerClient(
            loaded_results=[[]],
            resume_error=CodexBridgeError("App-server thread/resume failed: no rollout found for thread id thread-1"),
        )

        with self.assertRaises(CodexBridgeError):
            FakeRunner(client).submit_existing_thread_message("thread-1", "hello")
        self.assertEqual(client.turn_calls, [])


if __name__ == "__main__":
    unittest.main()
