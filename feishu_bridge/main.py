from __future__ import annotations

import argparse
import os
from pathlib import Path

from feishu_bridge.bridge_service import CodexFeishuBridgeService
from feishu_bridge.long_connection_service import CodexFeishuLongConnectionBridgeService
from feishu_bridge.settings import load_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Feishu <-> Codex desktop bridge")
    parser.add_argument("--config", type=Path, default=None, help="Path to bridge local_settings.json")
    parser.add_argument(
        "--mode",
        choices=("webhook", "long_connection"),
        default="",
        help="Override receive mode from config",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    (settings.runtime_dir / "bridge.pid").write_text(str(os.getpid()), encoding="utf-8")
    if args.mode:
        settings.receive_mode = args.mode
    if settings.receive_mode == "long_connection":
        CodexFeishuLongConnectionBridgeService(settings).serve_forever()
        return 0
    CodexFeishuBridgeService(settings).serve_webhook_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
