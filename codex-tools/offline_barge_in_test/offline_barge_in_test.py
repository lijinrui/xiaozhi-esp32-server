#!/usr/bin/env python3
"""
Offline barge-in verifier for xiaozhi-esp32-server.

This client simulates an ESP32 device without real hardware:
1. Connects to the websocket server.
2. Sends hello.
3. Sends a text-only listen/detect message as turn 1.
4. Waits until TTS begins or audio packets arrive.
5. Sends abort to simulate user interruption.
6. Sends a second text-only listen/detect message.
7. Prints timing and stale-audio metrics.

It intentionally starts with text mode because it validates the core cancel path
without requiring a microphone, Opus encoder, or ESP32.
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
class Metrics:
    connected_at: float = 0.0
    hello_at: float = 0.0
    turn1_sent_at: float = 0.0
    first_stt_at: float | None = None
    first_tts_start_at: float | None = None
    first_sentence_at: float | None = None
    first_audio_at: float | None = None
    abort_sent_at: float | None = None
    tts_stop_after_abort_at: float | None = None
    turn2_sent_at: float | None = None
    turn2_first_stt_at: float | None = None
    turn2_first_tts_start_at: float | None = None
    turn2_first_audio_at: float | None = None
    turn1_id: str | None = None
    turn2_id: str | None = None
    stop_turn_id: str | None = None
    turn_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    binary_before_abort: int = 0
    binary_after_abort_before_turn2: int = 0
    binary_after_turn2: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


def now() -> float:
    return time.monotonic()


def delta_ms(start: float | None, end: float | None) -> str:
    if start is None or end is None:
        return "n/a"
    return f"{(end - start) * 1000:.1f}ms"


async def send_json(ws, payload: dict[str, Any]) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def receiver(ws, metrics: Metrics, state: dict[str, Any]) -> None:
    async for message in ws:
        t = now()
        if isinstance(message, bytes):
            if metrics.abort_sent_at is None:
                metrics.binary_before_abort += 1
            elif metrics.turn2_sent_at is None:
                metrics.binary_after_abort_before_turn2 += 1
            else:
                metrics.binary_after_turn2 += 1

            if metrics.first_audio_at is None:
                metrics.first_audio_at = t
            if metrics.turn2_sent_at is not None and metrics.turn2_first_audio_at is None:
                metrics.turn2_first_audio_at = t
            state["saw_audio"].set()
            continue

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            data = {"raw": message}
        metrics.messages.append(data)

        msg_type = data.get("type")
        turn_id = data.get("turn_id")
        if msg_type == "hello":
            metrics.hello_at = t
            state["hello"].set()
        elif msg_type == "stt":
            if metrics.turn2_sent_at is None:
                metrics.first_stt_at = metrics.first_stt_at or t
                metrics.turn1_id = metrics.turn1_id or turn_id
            else:
                metrics.turn2_first_stt_at = metrics.turn2_first_stt_at or t
                metrics.turn2_id = metrics.turn2_id or turn_id
        elif msg_type == "tts":
            tts_state = data.get("state")
            if tts_state == "start":
                if metrics.turn2_sent_at is None:
                    metrics.first_tts_start_at = metrics.first_tts_start_at or t
                    metrics.turn1_id = metrics.turn1_id or turn_id
                else:
                    metrics.turn2_first_tts_start_at = metrics.turn2_first_tts_start_at or t
                    metrics.turn2_id = metrics.turn2_id or turn_id
            elif tts_state == "sentence_start":
                if metrics.turn2_sent_at is None:
                    metrics.first_sentence_at = metrics.first_sentence_at or t
                    metrics.turn1_id = metrics.turn1_id or turn_id
                else:
                    metrics.turn2_first_tts_start_at = metrics.turn2_first_tts_start_at or t
                    metrics.turn2_id = metrics.turn2_id or turn_id
                state["tts_started"].set()
            elif tts_state == "stop":
                if metrics.abort_sent_at is not None and metrics.turn2_sent_at is None:
                    metrics.tts_stop_after_abort_at = metrics.tts_stop_after_abort_at or t
                    metrics.stop_turn_id = metrics.stop_turn_id or turn_id
                    state["tts_stopped"].set()
        elif msg_type == "turn_metrics" and turn_id:
            metrics.turn_metrics[turn_id] = data


async def run(args: argparse.Namespace) -> int:
    metrics = Metrics()
    state = {
        "hello": asyncio.Event(),
        "tts_started": asyncio.Event(),
        "tts_stopped": asyncio.Event(),
        "saw_audio": asyncio.Event(),
    }

    headers = {
        "device-id": args.device_id,
        "client-id": args.client_id,
    }
    if args.authorization:
        headers["authorization"] = args.authorization

    metrics.connected_at = now()
    async with websockets.connect(args.url, additional_headers=headers) as ws:
        recv_task = asyncio.create_task(receiver(ws, metrics, state))

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
        await asyncio.wait_for(state["hello"].wait(), timeout=args.hello_timeout)

        metrics.turn1_sent_at = now()
        await send_json(
            ws,
            {
                "type": "listen",
                "state": "detect",
                "text": args.turn1,
                "mode": "auto",
            },
        )

        try:
            await asyncio.wait_for(
                asyncio.wait(
                    [asyncio.create_task(state["tts_started"].wait()), asyncio.create_task(state["saw_audio"].wait())],
                    return_when=asyncio.FIRST_COMPLETED,
                ),
                timeout=args.tts_start_timeout,
            )
        except asyncio.TimeoutError:
            print("ERROR: TTS did not start before timeout.")
            recv_task.cancel()
            return 2

        if args.abort_delay > 0:
            await asyncio.sleep(args.abort_delay)

        metrics.abort_sent_at = now()
        await send_json(
            ws,
            {
                "type": "abort",
                "reason": "offline_barge_in_test",
            },
        )

        try:
            await asyncio.wait_for(state["tts_stopped"].wait(), timeout=args.stop_timeout)
        except asyncio.TimeoutError:
            print("WARN: did not receive tts stop after abort before timeout.")

        await asyncio.sleep(args.gap_after_abort)

        metrics.turn2_sent_at = now()
        await send_json(
            ws,
            {
                "type": "listen",
                "state": "detect",
                "text": args.turn2,
                "mode": "auto",
            },
        )

        await asyncio.sleep(args.observe_after_turn2)

        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    print("Offline barge-in test metrics")
    print("----------------------------")
    print(f"URL: {args.url}")
    print(f"Turn 1: {args.turn1}")
    print(f"Turn 2: {args.turn2}")
    print(f"hello latency: {delta_ms(metrics.connected_at, metrics.hello_at)}")
    print(f"turn1 -> first STT: {delta_ms(metrics.turn1_sent_at, metrics.first_stt_at)}")
    print(f"turn1 -> TTS start: {delta_ms(metrics.turn1_sent_at, metrics.first_tts_start_at)}")
    print(f"turn1 -> sentence_start: {delta_ms(metrics.turn1_sent_at, metrics.first_sentence_at)}")
    print(f"turn1 -> first audio: {delta_ms(metrics.turn1_sent_at, metrics.first_audio_at)}")
    print(f"abort -> tts stop: {delta_ms(metrics.abort_sent_at, metrics.tts_stop_after_abort_at)}")
    print(f"turn2 -> STT: {delta_ms(metrics.turn2_sent_at, metrics.turn2_first_stt_at)}")
    print(f"turn2 -> TTS start: {delta_ms(metrics.turn2_sent_at, metrics.turn2_first_tts_start_at)}")
    print(f"turn2 -> first audio: {delta_ms(metrics.turn2_sent_at, metrics.turn2_first_audio_at)}")
    print(f"turn1_id: {metrics.turn1_id}")
    print(f"turn2_id: {metrics.turn2_id}")
    print(f"stop_turn_id: {metrics.stop_turn_id}")
    print("cancel_reason: offline_barge_in_test")
    if metrics.stop_turn_id and metrics.stop_turn_id in metrics.turn_metrics:
        turn_metric = metrics.turn_metrics[metrics.stop_turn_id]
        print(f"server cancel_reason: {turn_metric.get('cancel_reason')}")
        print(f"server llm_first_token_ms: {turn_metric.get('llm_first_token_ms')}")
        print(f"server tts_first_audio_ms: {turn_metric.get('tts_first_audio_ms')}")
        print(f"server abort_to_stop_ms: {turn_metric.get('abort_to_stop_ms')}")
        print(f"server stale_packets: {turn_metric.get('stale_packets')}")
    print(f"binary packets before abort: {metrics.binary_before_abort}")
    print(f"binary packets after abort before turn2: {metrics.binary_after_abort_before_turn2}")
    print(f"binary packets after turn2: {metrics.binary_after_turn2}")

    failures: list[str] = []

    abort_to_stop_ms = None
    if metrics.abort_sent_at is not None and metrics.tts_stop_after_abort_at is not None:
        abort_to_stop_ms = (metrics.tts_stop_after_abort_at - metrics.abort_sent_at) * 1000

    if metrics.first_sentence_at is None and metrics.first_audio_at is None:
        failures.append("turn1 did not reach sentence_start/audio")
    if metrics.tts_stop_after_abort_at is None:
        failures.append("did not receive tts stop after abort")
    elif abort_to_stop_ms is not None and abort_to_stop_ms > args.max_abort_to_stop_ms:
        failures.append(
            f"abort -> tts stop exceeded limit ({abort_to_stop_ms:.1f}ms > {args.max_abort_to_stop_ms:.1f}ms)"
        )
    if metrics.turn2_first_tts_start_at is None and metrics.turn2_first_audio_at is None:
        failures.append("turn2 did not reach TTS start/audio")
    if not metrics.turn1_id:
        failures.append("turn1_id missing from server messages")
    if not metrics.turn2_id:
        failures.append("turn2_id missing from server messages")
    if metrics.turn1_id and metrics.turn2_id and metrics.turn1_id == metrics.turn2_id:
        failures.append("turn1_id and turn2_id should be different")
    if metrics.stop_turn_id and metrics.turn1_id and metrics.stop_turn_id != metrics.turn1_id:
        failures.append("abort stop_turn_id did not match turn1_id")
    if not metrics.stop_turn_id or metrics.stop_turn_id not in metrics.turn_metrics:
        failures.append("server turn_metrics missing for interrupted turn")
    else:
        turn_metric = metrics.turn_metrics[metrics.stop_turn_id]
        for field_name in (
            "turn_id",
            "cancel_reason",
            "llm_first_token_ms",
            "tts_first_audio_ms",
            "abort_to_stop_ms",
            "stale_packets",
        ):
            if field_name not in turn_metric:
                failures.append(f"server turn_metrics missing {field_name}")
        if turn_metric.get("turn_id") != metrics.stop_turn_id:
            failures.append("server turn_metrics turn_id mismatch")
        if turn_metric.get("cancel_reason") is None:
            failures.append("server turn_metrics cancel_reason is empty")
        server_abort_to_stop = turn_metric.get("abort_to_stop_ms")
        if (
            isinstance(server_abort_to_stop, (int, float))
            and server_abort_to_stop > args.max_abort_to_stop_ms
        ):
            failures.append(
                "server abort_to_stop_ms exceeded limit "
                f"({server_abort_to_stop:.1f}ms > {args.max_abort_to_stop_ms:.1f}ms)"
            )
        server_stale_packets = turn_metric.get("stale_packets")
        if (
            isinstance(server_stale_packets, int)
            and server_stale_packets > args.allowed_stale_packets
        ):
            failures.append(
                "server stale_packets exceeded limit "
                f"({server_stale_packets} > {args.allowed_stale_packets})"
            )
    if metrics.binary_after_abort_before_turn2 > args.allowed_stale_packets:
        failures.append(
            "stale audio packets after abort exceeded limit "
            f"({metrics.binary_after_abort_before_turn2} > {args.allowed_stale_packets})"
        )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: abort path completed within configured barge-in tolerances.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline barge-in verifier")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/xiaozhi/v1/")
    parser.add_argument("--device-id", default="offline-test-device")
    parser.add_argument("--client-id", default="offline-test-client")
    parser.add_argument("--authorization", default="")
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument(
        "--turn1",
        default="请用三句话介绍一下小智机器人，并且慢慢说。",
    )
    parser.add_argument("--turn2", default="停一下，改成只说一句话。")
    parser.add_argument("--hello-timeout", type=float, default=5.0)
    parser.add_argument("--tts-start-timeout", type=float, default=30.0)
    parser.add_argument("--stop-timeout", type=float, default=5.0)
    parser.add_argument("--abort-delay", type=float, default=0.2)
    parser.add_argument("--gap-after-abort", type=float, default=0.5)
    parser.add_argument("--observe-after-turn2", type=float, default=8.0)
    parser.add_argument("--allowed-stale-packets", type=int, default=2)
    parser.add_argument("--max-abort-to-stop-ms", type=float, default=500.0)
    return parser.parse_args()


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
