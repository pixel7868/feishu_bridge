from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_bridge.settings import load_settings


def test_load_settings_overlays_local_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "local_settings.json"
    secrets_path = tmp_path / "local_secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "message_mode": "direct",
                "direct_cwd": "C:\\public",
                "app_id": "",
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(
        json.dumps(
            {
                "app_id": "cli_private",
                "app_secret": "private_secret",
                "direct_cwd": "D:\\private_workspace",
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.message_mode == "direct"
    assert settings.app_id == "cli_private"
    assert settings.app_secret == "private_secret"
    assert settings.direct_cwd == Path("D:\\private_workspace")


def test_load_settings_rejects_websocket_url_in_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "local_settings.json"
    secrets_path = tmp_path / "local_secrets.json"
    config_path.write_text("{}", encoding="utf-8")
    secrets_path.write_text(
        json.dumps({"appserver_websocket_url": "ws://127.0.0.1:17920"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="appserver_websocket_url is deprecated"):
        load_settings(config_path)

