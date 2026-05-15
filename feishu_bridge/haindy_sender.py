from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from feishu_bridge.settings import BridgeSettings


class HaindySimulationSender:
    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self.session_path = settings.runtime_dir / "haindy_session.json"

    def send(self, text: str, *, thread_name: str = "") -> str:
        def operation(session_id: str) -> None:
            self._set_clipboard(text)
            self._send_with_session(session_id, text, thread_name)

        return self._run_with_session_retry(operation)

    def switch_thread(self, thread_name: str) -> str:
        safe_thread_name = str(thread_name or "").strip()
        if not safe_thread_name:
            raise RuntimeError("切换 Codex 线程需要线程名称。")
        instruction = self.settings.haindy_switch_thread_instruction.format(
            thread_name=safe_thread_name
        )
        return self.act(instruction, thread_name=safe_thread_name)

    def switch_thread_by_row(self, row_number: int, thread_name: str) -> str:
        safe_row_number = int(row_number)
        if safe_row_number < 1:
            raise RuntimeError("按行切换 Codex 线程需要正整数行号。")
        safe_thread_name = str(thread_name or "").strip()
        instruction = (
            "找到codex窗口，按 Codex 左侧会话列表第 "
            f"{safe_row_number} 行切换线程，不使用 OCR 标题识别，"
            "点击该行并聚焦消息输入框，不要发送消息"
        )
        return self.act(instruction, thread_name=safe_thread_name)

    def switch_thread_by_shortcut(self, shortcut_number: int) -> str:
        safe_shortcut_number = int(shortcut_number)
        if safe_shortcut_number < 1 or safe_shortcut_number > 9:
            raise RuntimeError("Codex 快捷键切换只支持 1-9。")
        self._focus_codex_window()
        self._send_ctrl_number(safe_shortcut_number)
        time.sleep(0.35)
        return f"local-hotkey:ctrl+{safe_shortcut_number}"

    def new_chat(self) -> str:
        return self.act(self.settings.haindy_new_chat_instruction)

    def locate_input(self, *, thread_name: str = "") -> str:
        return self.act(self.settings.haindy_locate_input_instruction, thread_name=thread_name)

    def act(self, instruction: str, *, thread_name: str = "") -> str:
        instruction_path = self._write_instruction_file(
            self._with_thread_name(instruction, thread_name)
        )
        try:
            return self._run_with_session_retry(
                lambda session_id: self._run_checked(
                    ["act", "--instruction-file", str(instruction_path), "--session", session_id]
                )
            )
        finally:
            self._delete_instruction_file(instruction_path)

    def _run_with_session_retry(self, operation: Callable[[str], Any]) -> str:
        self._ensure_command_available()
        session_id = self._ensure_session_id()
        try:
            operation(session_id)
            return session_id
        except RuntimeError as exc:
            if not self._should_retry_with_new_session(exc):
                raise
            self._reset_haindy_session()
            session_id = self._ensure_session_id()
            operation(session_id)
            return session_id

    def _ensure_command_available(self) -> None:
        command = self._command_prefix()
        if shutil.which(command[0]) is None and not Path(command[0]).exists():
            raise RuntimeError(
                f"未找到 HAINDY 命令: {command[0]!r}。先配置 haindy_command，或切回 direct 模式。"
            )

    def _send_with_session(self, session_id: str, text: str, thread_name: str) -> None:
        focus_instruction = self.settings.haindy_focus_instruction
        if "找到codex窗口" in text:
            focus_instruction = f"{focus_instruction}。用户明确要求：找到codex窗口"
        self._run_act_checked(focus_instruction, session_id=session_id, thread_name=thread_name)
        self._run_act_checked(
            self.settings.haindy_paste_instruction,
            session_id=session_id,
            thread_name=thread_name,
        )
        self._run_act_checked(
            self.settings.haindy_submit_instruction,
            session_id=session_id,
            thread_name=thread_name,
        )

    def _focus_codex_window(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("本地快捷键切换目前只支持 Windows。")

        hwnd = self._find_codex_window()
        if not hwnd:
            raise RuntimeError("没有找到 Codex 窗口，无法发送 Ctrl+数字快捷键。")

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SW_RESTORE = 9

        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetFocus.argtypes = [wintypes.HWND]
        user32.SetFocus.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        user32.ShowWindow(hwnd, SW_RESTORE)
        current_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached_threads: list[int] = []
        try:
            for thread_id in {int(target_thread), int(foreground_thread)}:
                if thread_id and thread_id != int(current_thread):
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached_threads.append(thread_id)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            for thread_id in attached_threads:
                user32.AttachThreadInput(current_thread, thread_id, False)
        if int(user32.GetForegroundWindow()) != int(hwnd):
            raise RuntimeError("无法把 Codex 窗口切到前台，未发送 Ctrl+数字快捷键。")
        time.sleep(0.2)

    def _find_codex_window(self) -> int:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int

        candidates: list[tuple[int, str]] = []

        def callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if "codex" in title.lower():
                candidates.append((int(hwnd), title))
            return True

        if not user32.EnumWindows(EnumWindowsProc(callback), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not candidates:
            return 0

        def rank(item: tuple[int, str]) -> tuple[int, int]:
            title = item[1].lower()
            if title == "codex":
                return (0, len(title))
            if title.startswith("codex"):
                return (1, len(title))
            return (2, len(title))

        return sorted(candidates, key=rank)[0][0]

    def _send_ctrl_number(self, shortcut_number: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.keybd_event.argtypes = [
            wintypes.BYTE,
            wintypes.BYTE,
            wintypes.DWORD,
            wintypes.ULONG,
        ]
        user32.keybd_event.restype = None

        VK_CONTROL = 0x11
        KEYEVENTF_KEYUP = 0x0002
        number_vk = ord(str(shortcut_number))
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(number_vk, 0, 0, 0)
        user32.keybd_event(number_vk, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def _ensure_session_id(self) -> str:
        configured = self.settings.haindy_session_id.strip()
        if configured:
            return configured
        cached = self._load_cached_session_id()
        if cached:
            return cached
        payload = self._run_checked(["session", "new", "--desktop"])
        session_id = self._extract_session_id(payload)
        if not session_id:
            raise RuntimeError(f"HAINDY session new did not return a session_id: {payload}")
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(
            json.dumps({"session_id": session_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return session_id

    def _load_cached_session_id(self) -> str:
        if not self.session_path.exists():
            return ""
        try:
            payload = json.loads(self.session_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return ""
        return str(payload.get("session_id") or "").strip()

    def _clear_cached_session_id(self) -> None:
        try:
            self.session_path.unlink()
        except FileNotFoundError:
            return

    def _reset_haindy_session(self) -> None:
        self._clear_cached_session_id()
        self._terminate_haindy_daemons()

    def recover_after_error(self) -> None:
        self._reset_haindy_session()

    @staticmethod
    def _terminate_haindy_daemons() -> None:
        if sys.platform != "win32":
            return
        script = (
            "Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*haindy.main __tool_call_daemon*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with contextlib.suppress(Exception):
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                timeout=10,
                **kwargs,
            )

    @staticmethod
    def _is_missing_session_error(exc: RuntimeError) -> bool:
        text = str(exc)
        return "No active session found" in text or "active session" in text and "not found" in text

    @staticmethod
    def _should_retry_with_new_session(exc: RuntimeError) -> bool:
        text = str(exc)
        return (
            HaindySimulationSender._is_missing_session_error(exc)
            or "Windows OCR did not find Codex thread" in text
            or "OCR did not find Codex thread" in text
            or "No connection could be made" in text
            or "ConnectionRefusedError" in text
        )

    @staticmethod
    def is_recoverable_error(exc: BaseException) -> bool:
        text = str(exc)
        return (
            "HAINDY command failed" in text
            or "Haindy encountered an internal error" in text
            or "Windows OCR did not find Codex thread" in text
            or "OCR did not find Codex thread" in text
            or "No active session found" in text
            or "ConnectionRefusedError" in text
            or "No connection could be made" in text
        )

    def _run_checked(self, args: list[str]) -> dict[str, Any]:
        command = self._command_prefix()
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            [*command, *args],
            cwd=str(self.settings.haindy_cwd),
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            errors="replace",
            timeout=max(int(self.settings.haindy_timeout_seconds), 10),
            **kwargs,
        )
        payload = self._parse_json_output(proc.stdout)
        status = str(payload.get("status") or "").lower()
        if proc.returncode != 0 or status in {"failure", "error"}:
            detail = str(payload.get("response") or "").strip()
            if not detail:
                detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"HAINDY command failed ({' '.join(args)}): {detail}")
        return payload

    def _run_act_checked(self, instruction: str, *, session_id: str, thread_name: str) -> dict[str, Any]:
        instruction_path = self._write_instruction_file(
            self._with_thread_name(instruction, thread_name)
        )
        try:
            return self._run_checked(
                ["act", "--instruction-file", str(instruction_path), "--session", session_id]
            )
        finally:
            self._delete_instruction_file(instruction_path)

    def _command_prefix(self) -> list[str]:
        command = str(self.settings.haindy_command or "").strip()
        if not command:
            command = "haindy"
        args = self.settings.haindy_command_args or []
        return [command, *[str(item) for item in args]]

    @staticmethod
    def _with_thread_name(instruction: str | None, thread_name: str | None) -> str:
        normalized = str(thread_name or "").strip()
        base_instruction = str(instruction or "").strip()
        if not normalized:
            return base_instruction
        return f"{base_instruction}\nfeishu_thread_name={normalized}"

    def _write_instruction_file(self, instruction: str) -> Path:
        directory = self.settings.runtime_dir / "haindy_instructions"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid4().hex}.txt"
        path.write_text(str(instruction or "").strip(), encoding="utf-8")
        return path

    @staticmethod
    def _delete_instruction_file(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    @staticmethod
    def _parse_json_output(stdout: str) -> dict[str, Any]:
        text = stdout.strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"HAINDY returned non-JSON output: {text[:500]}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"HAINDY returned unexpected JSON output: {payload!r}")
        return payload

    @staticmethod
    def _extract_session_id(payload: dict[str, Any]) -> str:
        candidates = [
            payload.get("session_id"),
            (payload.get("session") or {}).get("session_id") if isinstance(payload.get("session"), dict) else None,
            (payload.get("meta") or {}).get("session_id") if isinstance(payload.get("meta"), dict) else None,
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _set_clipboard(text: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError("simulate mode clipboard handoff currently supports Windows only.")

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL

        payload = str(text or "") + "\0"
        data = payload.encode("utf-16-le")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        clipboard_open = False
        ownership_transferred = False
        try:
            locked = kernel32.GlobalLock(handle)
            if not locked:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                ctypes.memmove(locked, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)

            last_error = 0
            for _ in range(10):
                if user32.OpenClipboard(None):
                    clipboard_open = True
                    break
                last_error = ctypes.get_last_error()
                time.sleep(0.05)
            if not clipboard_open:
                raise ctypes.WinError(last_error)
            if not user32.EmptyClipboard():
                raise ctypes.WinError(ctypes.get_last_error())
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            ownership_transferred = True
        finally:
            if clipboard_open:
                user32.CloseClipboard()
            if not ownership_transferred:
                kernel32.GlobalFree(handle)

