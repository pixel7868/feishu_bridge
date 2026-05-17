from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
PATH_SETTING_KEYS = {"codex_home", "runtime_dir", "direct_cwd", "appserver_cwd", "haindy_cwd"}


@dataclass(slots=True)
class BridgeSettings:
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    command_prefix: str = "$"
    receive_mode: str = "long_connection"
    host: str = "127.0.0.1"
    port: int = 9000
    default_chat_id: str = ""
    default_thread_id: str = ""
    poll_interval_seconds: float = 1.0
    forward_desktop_user_messages: bool = True
    feishu_streaming: bool = True
    message_mode: str = "direct"
    direct_cwd: Path = Path.home()
    direct_sandbox: str = "danger-full-access"
    direct_approval_policy: str = "never"
    direct_model: str = ""
    direct_turn_timeout_seconds: int = 600
    appserver_cwd: Path | None = None
    appserver_sandbox: str = ""
    appserver_approval_policy: str = ""
    appserver_model: str = ""
    appserver_turn_timeout_seconds: int = 0
    appserver_use_running_server: bool = False
    appserver_websocket_url: str = ""
    appserver_auto_recovery_rounds: int = 3
    haindy_command: str = "haindy"
    haindy_command_args: list[str] | None = None
    haindy_cwd: Path = Path.home()
    haindy_session_id: str = ""
    haindy_timeout_seconds: int = 180
    haindy_focus_instruction: str = (
        "bring the existing Codex desktop app window to the foreground and click the message input box"
    )
    haindy_locate_input_instruction: str = (
        "screenshot locate and re-locate the existing Codex desktop app message input box, "
        "then click it and cache the corrected coordinates; no clipboard handoff and no submit"
    )
    haindy_paste_instruction: str = "paste the current clipboard into the focused Codex message input"
    haindy_submit_instruction: str = "press Enter to submit the message"
    haindy_switch_thread_instruction: str = (
        "找到codex窗口，切换到 Codex 左侧会话列表中标题为 {thread_name} 的线程，"
        "点击该线程并聚焦消息输入框，不要发送消息"
    )
    haindy_new_chat_instruction: str = (
        "找到codex窗口，点击 Codex 的新建对话或 New Chat 按钮，"
        "停留在新对话的消息输入框，不要发送消息"
    )
    codex_home: Path = Path.home() / ".codex"
    runtime_dir: Path = APP_DIR / "runtime"


def _apply_settings(settings: BridgeSettings, raw: dict) -> None:
    for key, value in raw.items():
        if not hasattr(settings, key):
            continue
        if key in PATH_SETTING_KEYS and isinstance(value, str):
            setattr(settings, key, Path(value))
            continue
        setattr(settings, key, value)


def _read_settings_file(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def _secrets_path_for(config_path: Path) -> Path:
    override = os.environ.get("FEISHU_BRIDGE_SECRETS", "").strip()
    if override:
        return Path(override)
    return config_path.with_name("local_secrets.json")


def load_settings(path: Path | None = None) -> BridgeSettings:
    settings = BridgeSettings()
    config_path = path or (APP_DIR / "local_settings.json")
    if config_path.exists():
        _apply_settings(settings, _read_settings_file(config_path))

    secrets_path = _secrets_path_for(config_path)
    if secrets_path.exists():
        _apply_settings(settings, _read_settings_file(secrets_path))

    if str(settings.appserver_websocket_url or "").strip():
        raise ValueError(
            "appserver_websocket_url is deprecated for feishu_bridge. "
            "Leave it empty so appserver mode uses `codex app-server --listen stdio://`; "
            "do not point Codex Desktop at CODEX_APP_SERVER_WS_URL."
        )
    return settings
