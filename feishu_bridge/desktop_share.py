from __future__ import annotations

import argparse
import contextlib
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from feishu_bridge.codex_thread_bridge import CodexAppServerClient, _resolve_codex_command
from feishu_bridge.settings import APP_DIR


DEFAULT_APP_SERVER_URL = "ws://127.0.0.1:17920"
DEFAULT_SOCKS_HOST = "127.0.0.1"
DEFAULT_SOCKS_PORT = 1080


def _runtime_dir() -> Path:
    path = APP_DIR / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_ws_endpoint(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError(f"Expected ws:// or wss:// URL, got: {url}")
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"Expected URL with explicit host and port, got: {url}")
    return parsed.hostname, int(parsed.port)


def _tcp_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    with contextlib.suppress(OSError):
        with socket.create_connection((host, port), timeout=timeout):
            return True
    return False


def _set_user_environment(name: str, value: str | None) -> None:
    if sys.platform != "win32":
        raise RuntimeError("User environment updates are only implemented on Windows.")
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        if value is None:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, name)
        else:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        hwnd_broadcast,
        wm_settingchange,
        0,
        "Environment",
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )


def _get_user_environment(name: str) -> str:
    if sys.platform != "win32":
        return os.environ.get(name, "")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return ""
    return str(value)


@dataclass(slots=True)
class ManagedProcess:
    proc: subprocess.Popen[bytes]
    stdout_handle: BinaryIO
    stderr_handle: BinaryIO

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
        if self.proc.poll() is None:
            self.proc.kill()
        self.stdout_handle.close()
        self.stderr_handle.close()


class SharedAppServerSupervisor:
    def __init__(
        self,
        url: str,
        managed: ManagedProcess,
        *,
        watch_interval: float,
        exit_grace_seconds: float,
    ) -> None:
        self.url = url
        self._managed = managed
        self._watch_interval = max(float(watch_interval), 0.5)
        self._exit_grace_seconds = max(float(exit_grace_seconds), 0.0)
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start_desktop_restart_watch(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._watch_desktop_lifecycle,
            daemon=True,
            name="codex-desktop-appserver-supervisor",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
        with self._lock:
            self._managed.stop()

    def _watch_desktop_lifecycle(self) -> None:
        seen_desktop = bool(_codex_desktop_process_ids())
        absent_since: float | None = None
        while not self._stopped.wait(self._watch_interval):
            desktop_present = bool(_codex_desktop_process_ids())
            if desktop_present:
                seen_desktop = True
                absent_since = None
                continue
            if not seen_desktop:
                continue
            if absent_since is None:
                absent_since = time.time()
                continue
            if time.time() - absent_since < self._exit_grace_seconds:
                continue
            print("Codex Desktop exited; restarting shared app-server to clear loaded thread state")
            self._restart_app_server()
            seen_desktop = False
            absent_since = None

    def _restart_app_server(self) -> None:
        with self._lock:
            self._managed.stop()
            self._managed = start_shared_app_server(self.url)
            if self._managed is None:
                raise RuntimeError(f"shared app-server was not restarted because {self.url} is already in use")


def _codex_desktop_process_ids() -> set[int]:
    if sys.platform != "win32":
        return set()
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process -Filter \"Name = 'Codex.exe'\" | "
        "Where-Object { "
        "($_.ExecutablePath -like '*\\WindowsApps\\OpenAI.Codex_*\\app\\Codex.exe') "
        "-or ($_.CommandLine -like '*\\WindowsApps\\OpenAI.Codex_*\\app\\Codex.exe*') "
        "} | ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    process_ids: set[int] = set()
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        with contextlib.suppress(ValueError):
            process_ids.add(int(line))
    return process_ids


def start_shared_app_server(url: str) -> ManagedProcess | None:
    host, port = _parse_ws_endpoint(url)
    if _tcp_listening(host, port):
        print(f"shared app-server already listening on {host}:{port}")
        return None

    runtime = _runtime_dir()
    stdout = (runtime / "desktop_shared_appserver.stdout.log").open("ab", buffering=0)
    stderr = (runtime / "desktop_shared_appserver.stderr.log").open("ab", buffering=0)
    command = _resolve_codex_command() + ["app-server", "--listen", url]
    proc = subprocess.Popen(command, stdout=stdout, stderr=stderr)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"shared app-server exited with code {proc.returncode}")
        if _tcp_listening(host, port):
            print(f"shared app-server started on {url} pid={proc.pid}")
            return ManagedProcess(proc=proc, stdout_handle=stdout, stderr_handle=stderr)
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"timed out waiting for shared app-server on {url}")


class Socks5Proxy:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server: socket.socket | None = None
        self._stopped = threading.Event()
        self._threads: list[threading.Thread] = []

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(64)
            server.settimeout(0.5)
            self._server = server
            print(f"SOCKS5 proxy listening on {self.host}:{self.port}")
            while not self._stopped.is_set():
                try:
                    client, address = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stopped.is_set():
                        break
                    raise
                worker = threading.Thread(
                    target=self._handle_client,
                    args=(client, address),
                    daemon=True,
                )
                worker.start()
                self._threads.append(worker)

    def stop(self) -> None:
        self._stopped.set()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()

    def _handle_client(self, client: socket.socket, address: tuple[str, int]) -> None:
        with client:
            client.settimeout(15)
            try:
                self._socks_handshake(client)
                target_host, target_port = self._read_connect_request(client)
                with socket.create_connection((target_host, target_port), timeout=15) as upstream:
                    upstream.settimeout(None)
                    client.settimeout(None)
                    client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                    self._relay(client, upstream)
            except Exception as exc:
                print(f"SOCKS5 client {address[0]}:{address[1]} failed: {exc}")
                with contextlib.suppress(OSError):
                    client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")

    def _socks_handshake(self, client: socket.socket) -> None:
        header = self._recv_exact(client, 2)
        version, method_count = header[0], header[1]
        if version != 5:
            raise RuntimeError(f"unsupported SOCKS version: {version}")
        methods = self._recv_exact(client, method_count)
        if 0 not in methods:
            client.sendall(b"\x05\xff")
            raise RuntimeError("client did not offer no-auth SOCKS method")
        client.sendall(b"\x05\x00")

    def _read_connect_request(self, client: socket.socket) -> tuple[str, int]:
        version, command, _reserved, address_type = self._recv_exact(client, 4)
        if version != 5 or command != 1:
            raise RuntimeError("only SOCKS5 CONNECT is supported")
        if address_type == 1:
            host = socket.inet_ntoa(self._recv_exact(client, 4))
        elif address_type == 3:
            length = self._recv_exact(client, 1)[0]
            host = self._recv_exact(client, length).decode("idna")
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, self._recv_exact(client, 16))
        else:
            raise RuntimeError(f"unsupported SOCKS address type: {address_type}")
        port = struct.unpack("!H", self._recv_exact(client, 2))[0]
        print(f"SOCKS5 CONNECT {host}:{port}")
        return host, port

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise RuntimeError("unexpected EOF")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        stop = threading.Event()

        def copy(source: socket.socket, target: socket.socket) -> None:
            try:
                while not stop.is_set():
                    data = source.recv(65536)
                    if not data:
                        break
                    target.sendall(data)
            finally:
                stop.set()
                with contextlib.suppress(OSError):
                    target.shutdown(socket.SHUT_WR)
                with contextlib.suppress(OSError):
                    source.shutdown(socket.SHUT_RD)

        threads = [
            threading.Thread(target=copy, args=(left, right), daemon=True),
            threading.Thread(target=copy, args=(right, left), daemon=True),
        ]
        for thread in threads:
            thread.start()
        while not stop.is_set():
            time.sleep(0.1)


def probe_app_server(url: str) -> None:
    with CodexAppServerClient(websocket_url=url) as client:
        loaded = client.list_loaded_threads(limit=10)
    print(f"app-server probe ok: loaded_threads={len(loaded)}")


def run_serve(args: argparse.Namespace) -> int:
    print(
        "disabled: the shared Desktop app-server transport is deprecated. "
        "Leave CODEX_APP_SERVER_WS_URL unset and use bridge appserver mode, "
        "which starts Codex app-server over stdio."
    )
    return 2


def run_env(args: argparse.Namespace) -> int:
    if args.appserver_url and not args.clear:
        print(
            "refusing to set CODEX_APP_SERVER_WS_URL: this legacy Desktop "
            "transport can break Codex login and session resume. Leave it unset."
        )
        return 2
    _set_user_environment("CODEX_APP_SERVER_WS_URL", None)
    print("cleared user CODEX_APP_SERVER_WS_URL")
    return 0


def run_check(args: argparse.Namespace) -> int:
    host, port = _parse_ws_endpoint(args.appserver_url)
    print(f"app-server TCP {host}:{port}: {'listening' if _tcp_listening(host, port) else 'closed'}")
    print(
        f"SOCKS5 TCP {args.socks_host}:{args.socks_port}: "
        f"{'listening' if _tcp_listening(args.socks_host, args.socks_port) else 'closed'}"
    )
    print(f"process CODEX_APP_SERVER_WS_URL={os.environ.get('CODEX_APP_SERVER_WS_URL', '')}")
    print(f"user CODEX_APP_SERVER_WS_URL={_get_user_environment('CODEX_APP_SERVER_WS_URL')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Legacy Codex Desktop shared app-server helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Disabled legacy shared app-server experiment")
    serve.add_argument("--appserver-url", default=DEFAULT_APP_SERVER_URL)
    serve.add_argument("--socks-host", default=DEFAULT_SOCKS_HOST)
    serve.add_argument("--socks-port", type=int, default=DEFAULT_SOCKS_PORT)
    serve.add_argument("--start-appserver", action="store_true")
    serve.add_argument("--restart-appserver-on-desktop-exit", action=argparse.BooleanOptionalAction, default=True)
    serve.add_argument("--desktop-watch-interval", type=float, default=1.0)
    serve.add_argument("--desktop-exit-grace-seconds", type=float, default=1.0)
    serve.add_argument("--probe", action="store_true")
    serve.set_defaults(func=run_serve)

    env = subparsers.add_parser("env", help="Clear the legacy user-level Desktop websocket URL")
    env.add_argument("--appserver-url", default="")
    env.add_argument("--clear", action="store_true")
    env.set_defaults(func=run_env)

    check = subparsers.add_parser("check", help="Check local shared Desktop transport prerequisites")
    check.add_argument("--appserver-url", default=DEFAULT_APP_SERVER_URL)
    check.add_argument("--socks-host", default=DEFAULT_SOCKS_HOST)
    check.add_argument("--socks-port", type=int, default=DEFAULT_SOCKS_PORT)
    check.set_defaults(func=run_check)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
