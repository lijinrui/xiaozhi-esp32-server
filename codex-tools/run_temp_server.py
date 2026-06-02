#!/usr/bin/env python3
"""Run a local verification server on alternate ports.

This intentionally avoids the product server on 8000/8003 while testing
worktree changes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from pathlib import Path
import sys


SERVER_ROOT = Path(__file__).resolve().parents[1] / "main" / "xiaozhi-server"
os.chdir(SERVER_ROOT)
sys.path.insert(0, str(SERVER_ROOT))

from config.settings import load_config
from core.http_server import SimpleHttpServer
from core.websocket_server import WebSocketServer


async def run(args: argparse.Namespace) -> None:
    config = load_config()
    config["server"]["ip"] = args.host
    config["server"]["port"] = args.ws_port
    config["server"]["http_port"] = args.http_port
    config["server"].setdefault("auth", {})["enabled"] = False
    config["server"]["auth_key"] = config["server"].get("auth_key") or uuid.uuid4().hex
    config["enable_turn_guard"] = True
    config["send_turn_metrics_to_client"] = True
    if args.offline_test_providers:
        config.setdefault("selected_module", {})["LLM"] = "OfflineTestLLM"
        config.setdefault("selected_module", {})["TTS"] = "OfflineTestTTS"
        config.setdefault("selected_module", {})["Intent"] = "nointent"
        config.setdefault("LLM", {})["OfflineTestLLM"] = {
            "type": "offline_test",
            "delay_sec": args.llm_delay,
        }
        config.setdefault("TTS", {})["OfflineTestTTS"] = {
            "type": "offline_test",
            "frame_delay_sec": args.tts_frame_delay,
            "frames_per_segment": args.tts_frames_per_segment,
            "output_dir": "tmp/",
        }
    print(f"TEMP_SERVER_WS=ws://{args.host}:{args.ws_port}/xiaozhi/v1/", flush=True)
    await asyncio.gather(
        WebSocketServer(config).start(),
        SimpleHttpServer(config).start(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run temporary xiaozhi server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ws-port", type=int, default=8010)
    parser.add_argument("--http-port", type=int, default=8013)
    parser.add_argument("--offline-test-providers", action="store_true")
    parser.add_argument("--llm-delay", type=float, default=0.08)
    parser.add_argument("--tts-frame-delay", type=float, default=0.02)
    parser.add_argument("--tts-frames-per-segment", type=int, default=12)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
