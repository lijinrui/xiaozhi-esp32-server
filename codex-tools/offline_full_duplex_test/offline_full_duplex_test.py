#!/usr/bin/env python3
"""Offline full-duplex state-machine verifier.

This intentionally uses text listen/detect messages and fake providers. It
validates server-side interrupt decisions without ESP32 hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import websockets


@dataclass
class EventLog:
    messages: list[dict[str, Any]] = field(default_factory=list)
    binary_packets: int = 0

    def clear_runtime(self) -> None:
        self.messages.clear()
        self.binary_packets = 0


def monotonic() -> float:
    return time.monotonic()


async def send_json(ws, payload: dict[str, Any]) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def receiver(ws, log: EventLog, events: dict[str, asyncio.Event]) -> None:
    async for message in ws:
        if isinstance(message, bytes):
            log.binary_packets += 1
            events["audio"].set()
            continue
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            data = {"raw": message}
        log.messages.append(data)
        if data.get("type") == "hello":
            events["hello"].set()
        if data.get("type") == "tts" and data.get("state") in ("sentence_start", "start"):
            events["tts_started"].set()
        if data.get("type") == "tts" and data.get("state") == "stop":
            events["tts_stop"].set()
        if data.get("type") == "turn_metrics":
            events["turn_metrics"].set()


def tts_stops(log: EventLog) -> list[dict[str, Any]]:
    return [m for m in log.messages if m.get("type") == "tts" and m.get("state") == "stop"]


def turn_ids(log: EventLog) -> set[str]:
    return {m["turn_id"] for m in log.messages if m.get("turn_id")}


async def wait_for_playback(events: dict[str, asyncio.Event], timeout: float) -> None:
    await asyncio.wait_for(
        asyncio.wait(
            [
                asyncio.create_task(events["tts_started"].wait()),
                asyncio.create_task(events["audio"].wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        ),
        timeout=timeout,
    )


async def hello(ws, events: dict[str, asyncio.Event], args: argparse.Namespace) -> None:
    await send_json(
        ws,
        {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": False},
            "audio_params": {
                "format": "opus",
                "sample_rate": args.sample_rate,
                "channels": 1,
                "frame_duration": 60,
            },
        },
    )
    await asyncio.wait_for(events["hello"].wait(), timeout=args.hello_timeout)


async def hard_interrupt_case(ws, log: EventLog, events: dict[str, asyncio.Event], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    log.clear_runtime()
    for event in events.values():
        event.clear()

    await send_json(ws, {"type": "listen", "state": "detect", "text": args.turn1, "mode": "auto"})
    await wait_for_playback(events, args.tts_start_timeout)
    before_turn_ids = turn_ids(log)
    before_abort_audio = log.binary_packets
    abort_at = monotonic()
    await send_json(ws, {"type": "abort", "reason": "offline_full_duplex_test"})
    await asyncio.wait_for(events["tts_stop"].wait(), timeout=args.stop_timeout)
    stop_at = monotonic()
    await asyncio.sleep(args.gap_after_abort)
    audio_after_abort = log.binary_packets - before_abort_audio
    events["tts_started"].clear()
    events["audio"].clear()
    await send_json(ws, {"type": "listen", "state": "detect", "text": args.turn2, "mode": "auto"})
    await wait_for_playback(events, args.tts_start_timeout)
    after_turn_ids = turn_ids(log)

    if (stop_at - abort_at) * 1000 > args.max_abort_to_stop_ms:
        failures.append("hard interrupt stop too slow")
    if audio_after_abort > args.allowed_stale_packets:
        failures.append("old turn audio leaked after hard interrupt")
    if len(after_turn_ids - before_turn_ids) < 1:
        failures.append("second turn_id was not observed")
    if not tts_stops(log):
        failures.append("hard interrupt did not emit tts stop")
    await send_json(ws, {"type": "abort", "reason": "offline_full_duplex_case_cleanup"})
    await asyncio.sleep(args.gap_after_abort)
    return failures


async def weak_backchannel_case(ws, log: EventLog, events: dict[str, asyncio.Event], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    log.clear_runtime()
    for event in events.values():
        event.clear()

    await send_json(ws, {"type": "listen", "state": "detect", "text": args.turn1, "mode": "auto"})
    await wait_for_playback(events, args.tts_start_timeout)
    before_stop_count = len(tts_stops(log))
    before_turn_ids = set(turn_ids(log))
    await send_json(ws, {"type": "listen", "state": "detect", "text": args.backchannel, "mode": "auto"})
    await asyncio.sleep(args.backchannel_observe)

    new_stops = len(tts_stops(log)) - before_stop_count
    new_turn_ids = turn_ids(log) - before_turn_ids
    if new_stops:
        failures.append("weak backchannel emitted tts stop")
    if new_turn_ids:
        failures.append("weak backchannel created a new turn")

    await send_json(ws, {"type": "abort", "reason": "offline_full_duplex_cleanup"})
    return failures


async def run(args: argparse.Namespace) -> int:
    headers = {"device-id": args.device_id, "client-id": args.client_id}
    log = EventLog()
    events = {
        "hello": asyncio.Event(),
        "tts_started": asyncio.Event(),
        "audio": asyncio.Event(),
        "tts_stop": asyncio.Event(),
        "turn_metrics": asyncio.Event(),
    }
    async with websockets.connect(args.url, additional_headers=headers) as ws:
        recv_task = asyncio.create_task(receiver(ws, log, events))
        await hello(ws, events, args)
        failures = []
        failures.extend(await hard_interrupt_case(ws, log, events, args))
        failures.extend(await weak_backchannel_case(ws, log, events, args))
        recv_task.cancel()

    print("Offline full-duplex test")
    print("------------------------")
    print(f"hard_interrupt: {'PASS' if not any('hard' in f or 'old turn' in f or 'second' in f for f in failures) else 'FAIL'}")
    print(f"weak_backchannel: {'PASS' if not any('weak' in f for f in failures) else 'FAIL'}")
    print(f"failures: {len(failures)}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline full-duplex server verifier")
    parser.add_argument("--url", default="ws://127.0.0.1:8010/xiaozhi/v1/")
    parser.add_argument("--device-id", default="offline-full-duplex-device")
    parser.add_argument("--client-id", default="offline-full-duplex-client")
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument("--turn1", default="请用十句话介绍小智机器人，并且慢慢说。")
    parser.add_argument("--turn2", default="停一下，改成一句话。")
    parser.add_argument("--backchannel", default="嗯")
    parser.add_argument("--hello-timeout", type=float, default=5)
    parser.add_argument("--tts-start-timeout", type=float, default=10)
    parser.add_argument("--stop-timeout", type=float, default=2)
    parser.add_argument("--gap-after-abort", type=float, default=0.4)
    parser.add_argument("--backchannel-observe", type=float, default=1.0)
    parser.add_argument("--max-abort-to-stop-ms", type=float, default=500)
    parser.add_argument("--allowed-stale-packets", type=int, default=2)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
