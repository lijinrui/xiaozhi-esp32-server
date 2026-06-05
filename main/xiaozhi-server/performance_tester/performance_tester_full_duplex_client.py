from __future__ import annotations

import argparse
import asyncio
import html as html_lib
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

import websockets


SERVER_ROOT = Path(__file__).resolve().parents[1]
os.chdir(SERVER_ROOT)
sys.path.insert(0, str(SERVER_ROOT))

from core.utils.util import audio_to_data  # noqa: E402


DEFAULT_URL = "ws://127.0.0.1:8000/xiaozhi/v1/"
DEFAULT_AUDIO_DIR = SERVER_ROOT / "tmp" / "barge_in_audio"
DEFAULT_PROMPT_AUDIO = DEFAULT_AUDIO_DIR / "long_full_duplex_prompt.wav"
DEFAULT_REPORT = SERVER_ROOT.parent.parent / "codex-reports" / "2026-06-05_full_duplex_client_e2e_report.html"


CASES = [
    {
        "id": "explicit_stop",
        "label": "明确停止",
        "scene": "小智正在回答一段较长内容时，用户插话说“停一下”。",
        "expectation": "normal 模式应判定为明确打断，停止当前 TTS；如果随后有新回复，不能算作旧回复残留。",
        "audio_file": "explicit_stop.wav",
        "expected_stop": True,
    },
    {
        "id": "explicit_wait",
        "label": "明确叫停",
        "scene": "小智正在回答一段较长内容时，用户插话说“等等我重新问”。",
        "expectation": "normal 模式应判定为明确打断，停止当前 TTS；如果随后有新回复，不能算作旧回复残留。",
        "audio_file": "explicit_wait.wav",
        "expected_stop": True,
    },
    {
        "id": "correction_wrong_target",
        "label": "纠正目标",
        "scene": "小智正在回答时，用户插话说“不是这个，我问的是天气”。",
        "expectation": "normal 模式应判定为纠正类打断，停止当前 TTS，并进入新一轮处理。",
        "audio_file": "correction_wrong_target.wav",
        "expected_stop": True,
    },
    {
        "id": "question_weather",
        "label": "新问题",
        "scene": "小智正在回答时，用户直接插入新的问题“那明天天气怎么样”。",
        "expectation": "normal 模式应判定为新问题打断，停止当前 TTS，并进入新一轮处理。",
        "audio_file": "question_weather.wav",
        "expected_stop": True,
    },
    {
        "id": "question_rephrase",
        "label": "要求换说法",
        "scene": "小智正在回答时，用户插话说“能不能换一种说法”。",
        "expectation": "normal 模式应判定为新指令打断，停止当前 TTS，并进入新一轮处理。",
        "audio_file": "question_rephrase.wav",
        "expected_stop": True,
    },
    {
        "id": "lunar_tool_query",
        "label": "工具查询",
        "scene": "小智正在回答时，用户插话问“明天的黄历宜忌是什么”。",
        "expectation": "normal 模式应判定为新问题打断，停止当前 TTS；新 turn 应进入真实 function_call 工具链查询黄历。",
        "audio_file": "lunar_tool_query.wav",
        "expected_stop": True,
    },
    {
        "id": "backchannel_um",
        "label": "短附和",
        "scene": "小智正在回答时，用户只说“嗯”。",
        "expectation": "normal 模式不应把短附和当成硬打断。",
        "audio_file": "backchannel_um.wav",
        "expected_stop": False,
    },
    {
        "id": "backchannel_ok",
        "label": "确认附和",
        "scene": "小智正在回答时，用户只说“好的”。",
        "expectation": "normal 模式不应把确认附和当成硬打断。",
        "audio_file": "backchannel_ok.wav",
        "expected_stop": False,
    },
    {
        "id": "hesitation_short_partial",
        "label": "短犹豫",
        "scene": "小智正在回答时，用户只说“那个”。",
        "expectation": "normal 模式不应把短犹豫当成硬打断。",
        "audio_file": "hesitation_short_partial.wav",
        "expected_stop": False,
    },
    {
        "id": "ordinary_final_pause",
        "label": "普通插话",
        "scene": "小智正在回答时，用户说“我觉得这个回答还可以”这类普通完整短句。",
        "expectation": "normal 模式不应把普通插话当成硬打断，因此不要求服务端发送 tts stop。",
        "audio_file": "ordinary_final_pause.wav",
        "expected_stop": False,
    },
    {
        "id": "ordinary_final_environment",
        "label": "普通陈述",
        "scene": "小智正在回答时，用户说“今天外面声音有点大”。",
        "expectation": "normal 模式不应把普通环境陈述当成硬打断。",
        "audio_file": "ordinary_final_environment.wav",
        "expected_stop": False,
    },
]


class EventLog:
    def __init__(self):
        self.started = time.monotonic()
        self.events = []
        self.active_tts_turn_id = None

    def now_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000

    def add(self, kind: str, **data):
        self.events.append({"t_ms": self.now_ms(), "kind": kind, **data})

    def first(self, kind: str, predicate=None):
        for event in self.events:
            if event["kind"] != kind:
                continue
            if predicate is None or predicate(event):
                return event
        return None

    def last(self, kind: str, predicate=None):
        for event in reversed(self.events):
            if event["kind"] != kind:
                continue
            if predicate is None or predicate(event):
                return event
        return None


def wav_duration_ms(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate() * 1000


async def receive_loop(ws, log: EventLog, stop_event: asyncio.Event):
    try:
        async for message in ws:
            if isinstance(message, bytes):
                log.add("audio", bytes=len(message), turn_id=log.active_tts_turn_id)
                continue
            try:
                payload = json.loads(message)
            except Exception:
                log.add("text", raw=message)
                continue
            msg_type = payload.get("type")
            if msg_type == "tts":
                state = payload.get("state")
                turn_id = payload.get("turn_id")
                if state in ("start", "sentence_start") and turn_id:
                    log.active_tts_turn_id = turn_id
                elif state == "stop" and (
                    turn_id is None or turn_id == log.active_tts_turn_id
                ):
                    log.active_tts_turn_id = None
                log.add(
                    "tts",
                    state=state,
                    text=payload.get("text"),
                    turn_id=turn_id,
                )
                if state == "stop":
                    stop_event.set()
            elif msg_type == "stt":
                log.add("stt", text=payload.get("text"), turn_id=payload.get("turn_id"))
            elif msg_type == "turn_metrics":
                log.add("turn_metrics", metrics=payload)
            else:
                log.add("json", payload=payload)
    except websockets.exceptions.ConnectionClosed:
        log.add("connection_closed")


async def wait_for_event(log: EventLog, kind: str, timeout: float, predicate=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = log.first(kind, predicate)
        if event:
            return event
        await asyncio.sleep(0.02)
    return None


async def send_hello(ws):
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
                "features": {
                    "mcp": False,
                    "emoji": False,
                },
            },
            ensure_ascii=False,
        )
    )


async def inject_audio(ws, opus_frames: list[bytes], frame_ms: int, log: EventLog):
    log.add("interrupt_audio_start", frames=len(opus_frames))
    for frame in opus_frames:
        await ws.send(frame)
        await asyncio.sleep(frame_ms / 1000)
    await ws.send(json.dumps({"type": "listen", "state": "stop"}))
    log.add("interrupt_audio_end")


async def send_audio_utterance(ws, opus_frames: list[bytes], frame_ms: int, log: EventLog):
    log.add("prompt_audio_start", frames=len(opus_frames))
    await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
    for frame in opus_frames:
        await ws.send(frame)
        await asyncio.sleep(frame_ms / 1000)
    await ws.send(json.dumps({"type": "listen", "state": "stop"}))
    log.add("prompt_audio_end")


def summarize_case(
    case: dict,
    log: EventLog,
    inject_start_ms: float | None,
    prompt_duration_ms: float,
    interrupt_duration_ms: float,
    round_index: int,
):
    first_audio = log.first("audio")
    prompt_start = log.first("prompt_audio_start")
    prompt_end = log.first("prompt_audio_end")
    interrupt_end = log.first("interrupt_audio_end")
    first_stt = log.first("stt")
    first_tts_start = log.first("tts", lambda event: event.get("state") == "start")
    prompt_stt = None
    interrupt_stt = None
    if first_tts_start:
        prompt_stt = log.last(
            "stt", lambda event: event["t_ms"] < first_tts_start["t_ms"]
        )
    if inject_start_ms is not None:
        interrupt_stt = log.last(
            "stt", lambda event: event["t_ms"] >= inject_start_ms
        )
    first_stop_after_inject = None
    if inject_start_ms is not None:
        first_stop_after_inject = log.first(
            "tts",
            lambda event: event.get("state") == "stop" and event["t_ms"] >= inject_start_ms,
        )
    all_audio_after_inject = [
        event for event in log.events
        if event["kind"] == "audio" and inject_start_ms is not None and event["t_ms"] >= inject_start_ms
    ]
    stale_audio_after_stop = []
    new_audio_after_stop = []
    next_tts_start_after_stop = None
    if first_stop_after_inject:
        stop_ms = first_stop_after_inject["t_ms"]
        stopped_turn_id = first_stop_after_inject.get("turn_id")
        next_tts_start_after_stop = log.first(
            "tts",
            lambda event: event.get("state") == "start" and event["t_ms"] > stop_ms,
        )
        next_start_ms = (
            next_tts_start_after_stop["t_ms"] if next_tts_start_after_stop else None
        )
        for event in all_audio_after_inject:
            if event["t_ms"] <= stop_ms:
                continue
            if next_start_ms is not None and event["t_ms"] >= next_start_ms:
                new_audio_after_stop.append(event)
            elif stopped_turn_id is None or event.get("turn_id") in (None, stopped_turn_id):
                stale_audio_after_stop.append(event)

    prompt_turn_id = first_tts_start.get("turn_id") if first_tts_start else None
    interrupt_turn_id = (
        next_tts_start_after_stop.get("turn_id")
        if next_tts_start_after_stop
        else None
    )
    prompt_turn_metrics = log.first(
        "turn_metrics",
        lambda event: event.get("metrics", {}).get("turn_id") == prompt_turn_id,
    )
    if not prompt_turn_metrics:
        prompt_turn_metrics = log.first("turn_metrics")
    interrupt_turn_metrics = log.first(
        "turn_metrics",
        lambda event: event.get("metrics", {}).get("turn_id") == interrupt_turn_id,
    )
    if not interrupt_turn_metrics and prompt_turn_metrics:
        prompt_metrics_id = prompt_turn_metrics.get("metrics", {}).get("turn_id")
        interrupt_turn_metrics = log.first(
            "turn_metrics",
            lambda event: event.get("metrics", {}).get("turn_id") != prompt_metrics_id,
        )
    turn_metrics = prompt_turn_metrics
    abort_to_stop_ms = None
    llm_first_delta_ms = None
    llm_first_token_ms = None
    tts_first_audio_ms = None
    tool_names = []
    tool_choice_ms = None
    tool_call_ms = None
    tool_result_llm_first_delta_ms = None
    tool_result_llm_first_token_ms = None
    stale_audio_packets_server = None
    if turn_metrics:
        metrics = turn_metrics.get("metrics", {})
        abort_to_stop_ms = metrics.get("abort_to_stop_ms")
        llm_first_delta_ms = metrics.get("llm_first_delta_ms")
        llm_first_token_ms = metrics.get("llm_first_token_ms")
        tts_first_audio_ms = metrics.get("tts_first_audio_ms")
        tool_names = metrics.get("tool_names") or []
        tool_choice_ms = metrics.get("tool_choice_ms")
        tool_call_ms = metrics.get("tool_call_ms")
        tool_result_llm_first_delta_ms = metrics.get("tool_result_llm_first_delta_ms")
        tool_result_llm_first_token_ms = metrics.get("tool_result_llm_first_token_ms")
        stale_audio_packets_server = metrics.get("stale_audio_packets")

    interrupt_metrics_payload = (
        interrupt_turn_metrics.get("metrics") if interrupt_turn_metrics else None
    )
    interrupt_llm_first_delta_ms = None
    interrupt_llm_first_token_ms = None
    interrupt_tts_first_audio_ms = None
    interrupt_tool_names = []
    interrupt_tool_choice_ms = None
    interrupt_tool_call_ms = None
    interrupt_tool_result_llm_first_delta_ms = None
    interrupt_tool_result_llm_first_token_ms = None
    if interrupt_metrics_payload:
        interrupt_llm_first_delta_ms = interrupt_metrics_payload.get("llm_first_delta_ms")
        interrupt_llm_first_token_ms = interrupt_metrics_payload.get("llm_first_token_ms")
        interrupt_tts_first_audio_ms = interrupt_metrics_payload.get("tts_first_audio_ms")
        interrupt_tool_names = interrupt_metrics_payload.get("tool_names") or []
        interrupt_tool_choice_ms = interrupt_metrics_payload.get("tool_choice_ms")
        interrupt_tool_call_ms = interrupt_metrics_payload.get("tool_call_ms")
        interrupt_tool_result_llm_first_delta_ms = interrupt_metrics_payload.get(
            "tool_result_llm_first_delta_ms"
        )
        interrupt_tool_result_llm_first_token_ms = interrupt_metrics_payload.get(
            "tool_result_llm_first_token_ms"
        )

    valid = first_audio is not None and inject_start_ms is not None
    invalid_reason = None
    if first_audio is None:
        invalid_reason = "未收到首个 TTS 音频，未进入播放中打断阶段"
    elif inject_start_ms is None:
        invalid_reason = "未注入打断音频，未进入打断验证阶段"

    stopped = first_stop_after_inject is not None
    expected_stop = case["expected_stop"]
    ok = valid and stopped == expected_stop
    if stopped and stale_audio_after_stop:
        ok = False
    cache_values = [
        value
        for value in (llm_first_delta_ms, interrupt_llm_first_delta_ms)
        if value is not None
    ]
    cache_observed = bool(cache_values)
    cache_ok = round_index == 1 or not cache_values or all(value < 1000 for value in cache_values)
    if round_index > 1 and not cache_ok:
        ok = False

    return {
        "round": round_index,
        "id": case["id"],
        "label": case["label"],
        "scene": case["scene"],
        "expectation": case["expectation"],
        "expected_stop": expected_stop,
        "stopped": stopped,
        "valid": valid,
        "invalid_reason": invalid_reason,
        "ok": ok,
        "prompt_duration_ms": prompt_duration_ms,
        "interrupt_duration_ms": interrupt_duration_ms,
        "prompt_start_ms": prompt_start["t_ms"] if prompt_start else None,
        "prompt_end_ms": prompt_end["t_ms"] if prompt_end else None,
        "first_stt_ms": first_stt["t_ms"] if first_stt else None,
        "prompt_stt_ms": prompt_stt["t_ms"] if prompt_stt else None,
        "prompt_stt_text": prompt_stt.get("text") if prompt_stt else None,
        "interrupt_stt_ms": interrupt_stt["t_ms"] if interrupt_stt else None,
        "interrupt_stt_text": interrupt_stt.get("text") if interrupt_stt else None,
        "first_tts_start_ms": first_tts_start["t_ms"] if first_tts_start else None,
        "first_audio_ms": first_audio["t_ms"] if first_audio else None,
        "interrupt_start_ms": inject_start_ms,
        "interrupt_end_ms": interrupt_end["t_ms"] if interrupt_end else None,
        "stop_ms": first_stop_after_inject["t_ms"] if first_stop_after_inject else None,
        "prompt_turn_id": prompt_turn_id,
        "interrupt_turn_id": interrupt_turn_id,
        "next_tts_start_after_stop_ms": (
            next_tts_start_after_stop["t_ms"] if next_tts_start_after_stop else None
        ),
        "asr_after_prompt_end_ms": (
            prompt_stt["t_ms"] - prompt_end["t_ms"]
            if prompt_stt and prompt_end and prompt_stt["t_ms"] >= prompt_end["t_ms"]
            else None
        ),
        "llm_first_delta_ms": llm_first_delta_ms,
        "llm_cache_observed": cache_observed,
        "llm_first_token_ms": llm_first_token_ms,
        "llm_cache_ok": cache_ok,
        "tool_names": tool_names,
        "tool_choice_ms": tool_choice_ms,
        "tool_call_ms": tool_call_ms,
        "tool_result_llm_first_delta_ms": tool_result_llm_first_delta_ms,
        "tool_result_llm_first_token_ms": tool_result_llm_first_token_ms,
        "interrupt_llm_first_delta_ms": interrupt_llm_first_delta_ms,
        "interrupt_llm_first_token_ms": interrupt_llm_first_token_ms,
        "interrupt_tts_first_audio_ms_server": interrupt_tts_first_audio_ms,
        "interrupt_tool_names": interrupt_tool_names,
        "interrupt_tool_choice_ms": interrupt_tool_choice_ms,
        "interrupt_tool_call_ms": interrupt_tool_call_ms,
        "interrupt_tool_result_llm_first_delta_ms": interrupt_tool_result_llm_first_delta_ms,
        "interrupt_tool_result_llm_first_token_ms": interrupt_tool_result_llm_first_token_ms,
        "tts_first_audio_ms_server": tts_first_audio_ms,
        "tts_start_after_stt_ms": (
            first_tts_start["t_ms"] - prompt_stt["t_ms"]
            if first_tts_start and prompt_stt
            else None
        ),
        "audio_after_tts_start_ms": (
            first_audio["t_ms"] - first_tts_start["t_ms"]
            if first_audio and first_tts_start
            else None
        ),
        "audio_after_prompt_end_ms": (
            first_audio["t_ms"] - prompt_end["t_ms"]
            if first_audio and prompt_end
            else None
        ),
        "interrupt_to_stop_ms": (
            first_stop_after_inject["t_ms"] - inject_start_ms
            if first_stop_after_inject and inject_start_ms is not None
            else None
        ),
        "abort_to_stop_ms": abort_to_stop_ms,
        "audio_after_interrupt": len(all_audio_after_inject),
        "stale_audio_after_stop": len(stale_audio_after_stop),
        "new_audio_after_stop": len(new_audio_after_stop),
        "audio_after_stop": len(stale_audio_after_stop),
        "stale_audio_packets_server": stale_audio_packets_server,
        "interrupt_turn_metrics": interrupt_metrics_payload,
        "stt_texts": [event.get("text") for event in log.events if event["kind"] == "stt"],
        "events": log.events,
    }


async def run_case(args, case: dict, round_index: int):
    audio_path = Path(args.audio_dir) / case["audio_file"]
    opus_frames = await audio_to_data(str(audio_path), is_opus=True, use_cache=False)
    interrupt_duration_ms = wav_duration_ms(audio_path)
    prompt_audio_path = Path(args.prompt_audio)
    prompt_opus_frames = await audio_to_data(str(prompt_audio_path), is_opus=True, use_cache=False)
    prompt_duration_ms = wav_duration_ms(prompt_audio_path)
    log = EventLog()
    stop_event = asyncio.Event()
    inject_start_ms = None

    headers = {
        "device-id": args.device_id,
        "client-id": args.client_id,
    }
    url = args.url
    if "device-id=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}device-id={args.device_id}&client-id={args.client_id}"

    async with websockets.connect(url, additional_headers=headers, max_size=10_000_000) as ws:
        receiver = asyncio.create_task(receive_loop(ws, log, stop_event))
        await send_hello(ws)
        await wait_for_event(log, "json", 3)
        await asyncio.sleep(args.init_wait_ms / 1000)

        await send_audio_utterance(ws, prompt_opus_frames, args.frame_ms, log)

        first_audio = await wait_for_event(log, "audio", args.tts_timeout_sec)
        if not first_audio:
            receiver.cancel()
            return summarize_case(
                case, log, None, prompt_duration_ms, interrupt_duration_ms, round_index
            )

        await asyncio.sleep(args.interrupt_after_ms / 1000)
        inject_start_ms = log.now_ms()
        await inject_audio(ws, opus_frames, args.frame_ms, log)
        await asyncio.sleep(args.after_interrupt_wait_sec)
        receiver.cancel()
        try:
            await receiver
        except asyncio.CancelledError:
            pass

    return summarize_case(
        case, log, inject_start_ms, prompt_duration_ms, interrupt_duration_ms, round_index
    )


def fmt_ms(value):
    if value is None:
        return "-"
    return f"{value:.1f}ms"


def fmt_s(value):
    if value is None:
        return "-"
    return f"{value / 1000:.2f}s"


def yes_no(value):
    return "是" if value else "否"


def fmt_tools(tool_names):
    if not tool_names:
        return "-"
    return ", ".join(str(name) for name in tool_names)


def write_html_report(path: Path, rows: list[dict], args):
    passed = sum(1 for row in rows if row["ok"])
    valid_count = sum(1 for row in rows if row["valid"])
    total = len(rows)
    warmup_rows = [row for row in rows if row["round"] == 1]
    measured_rows = [row for row in rows if row["round"] > 1]
    avg_interrupt_to_stop = [
        row["interrupt_to_stop_ms"] for row in rows if row["interrupt_to_stop_ms"] is not None
    ]
    avg_stop = statistics.fmean(avg_interrupt_to_stop) if avg_interrupt_to_stop else None
    avg_response_after_prompt = [
        row["audio_after_prompt_end_ms"]
        for row in rows
        if row["audio_after_prompt_end_ms"] is not None
    ]
    avg_response = (
        statistics.fmean(avg_response_after_prompt) if avg_response_after_prompt else None
    )
    measured_llm_first_token = []
    for row in measured_rows:
        if row["llm_first_delta_ms"] is not None:
            measured_llm_first_token.append(row["llm_first_delta_ms"])
        if row["interrupt_llm_first_delta_ms"] is not None:
            measured_llm_first_token.append(row["interrupt_llm_first_delta_ms"])
    avg_measured_llm_first_token = (
        statistics.fmean(measured_llm_first_token)
        if measured_llm_first_token
        else None
    )
    cache_passed = sum(
        1
        for value in measured_llm_first_token
        if value < 1000
    )
    prompt_duration = rows[0]["prompt_duration_ms"] if rows else None

    table_rows = []
    case_sections = []
    for row in rows:
        if not row["valid"]:
            result = '<span class="invalid">INVALID</span>'
        elif row["ok"]:
            result = '<span class="ok">OK</span>'
        else:
            result = '<span class="fail">FAIL</span>'

        stt_text = (
            f"首轮：{row['prompt_stt_text'] or '-'}；插话：{row['interrupt_stt_text'] or '-'}"
        )
        table_rows.append(
            f"""
            <tr class="{'' if row['ok'] else 'failed'}">
              <td>{row['round']}</td>
              <td><code>{row['id']}</code></td>
              <td>{html_lib.escape(row['label'])}</td>
              <td>{html_lib.escape(row['scene'])}</td>
              <td>{html_lib.escape(stt_text)}</td>
              <td>{'有效' if row['valid'] else '无效'}</td>
              <td>{row['invalid_reason'] or '-'}</td>
              <td>{yes_no(row['expected_stop'])}</td>
              <td>{yes_no(row['stopped'])}</td>
              <td>{fmt_s(row['interrupt_duration_ms'])}</td>
              <td>{fmt_ms(row['audio_after_prompt_end_ms'])}</td>
              <td>{fmt_ms(row['interrupt_to_stop_ms'])}</td>
              <td>{fmt_ms(row['abort_to_stop_ms'])}</td>
              <td>{fmt_ms(row['llm_first_delta_ms'])}</td>
              <td>{fmt_ms(row['llm_first_token_ms'])}</td>
              <td>{fmt_ms(row['interrupt_llm_first_delta_ms'])}</td>
              <td>{fmt_ms(row['interrupt_llm_first_token_ms'])}</td>
              <td>{html_lib.escape(fmt_tools(row['interrupt_tool_names']))}</td>
              <td>{fmt_ms(row['interrupt_tool_choice_ms'])}</td>
              <td>{fmt_ms(row['interrupt_tool_call_ms'])}</td>
              <td>{fmt_ms(row['interrupt_tool_result_llm_first_delta_ms'])}</td>
              <td>{fmt_ms(row['interrupt_tool_result_llm_first_token_ms'])}</td>
              <td>{row['audio_after_interrupt']}</td>
              <td>{row['stale_audio_after_stop']}</td>
              <td>{row['new_audio_after_stop']}</td>
              <td>{row['stale_audio_packets_server'] if row['stale_audio_packets_server'] is not None else '-'}</td>
              <td>{result}</td>
            </tr>
            """
        )

        timeline_rows = [
            ("连接初始化完成", row["prompt_start_ms"], None),
            ("首次 ASR partial", row["first_stt_ms"], None),
            ("首轮用户音频上传结束", row["prompt_end_ms"], row["prompt_duration_ms"]),
            ("首轮识别文本就绪", row["prompt_stt_ms"], row["asr_after_prompt_end_ms"]),
            ("收到 TTS start", row["first_tts_start_ms"], row["tts_start_after_stt_ms"]),
            ("收到首个 TTS 音频包", row["first_audio_ms"], row["audio_after_tts_start_ms"]),
            ("开始注入打断音频", row["interrupt_start_ms"], None),
            ("收到插话 ASR 文本", row["interrupt_stt_ms"], None),
            ("打断音频上传结束", row["interrupt_end_ms"], row["interrupt_duration_ms"]),
            ("收到 TTS stop", row["stop_ms"], row["interrupt_to_stop_ms"]),
            ("收到新一轮 TTS start", row["next_tts_start_after_stop_ms"], None),
        ]
        timeline_rows = sorted(
            (item for item in timeline_rows if item[1] is not None),
            key=lambda item: item[1],
        )
        timeline_html = []
        for name, at_ms, cost_ms in timeline_rows:
            cost = "" if cost_ms is None else f"<span>阶段耗时 {fmt_ms(cost_ms)}</span>"
            timeline_html.append(
                f"""
                <li>
                  <strong>{html_lib.escape(name)}</strong>
                  <em>T+{fmt_ms(at_ms)}</em>
                  {cost}
                </li>
                """
            )

        case_sections.append(
            f"""
            <section class="case-card">
              <div class="case-head">
                <div>
                  <h3>{html_lib.escape(row['label'])} <code>{row['id']}</code></h3>
                  <p>第 {row['round']} 轮：{html_lib.escape(row['scene'])}</p>
                  <p class="muted">预期：{html_lib.escape(row['expectation'])}</p>
                </div>
                <div>{result}</div>
              </div>
              <div class="case-metrics">
                <div><span>首轮输入音频</span><strong>{fmt_s(row['prompt_duration_ms'])}</strong></div>
                <div><span>插话音频</span><strong>{fmt_s(row['interrupt_duration_ms'])}</strong></div>
                <div><span>首轮 LLM 首 delta</span><strong>{fmt_ms(row['llm_first_delta_ms'])}</strong></div>
                <div><span>插话 LLM 首 delta</span><strong>{fmt_ms(row['interrupt_llm_first_delta_ms'])}</strong></div>
                <div><span>插话可播报文本</span><strong>{fmt_ms(row['interrupt_llm_first_token_ms'])}</strong></div>
                <div><span>插话工具</span><strong>{html_lib.escape(fmt_tools(row['interrupt_tool_names']))}</strong></div>
                <div><span>工具选择</span><strong>{fmt_ms(row['interrupt_tool_choice_ms'])}</strong></div>
                <div><span>工具执行</span><strong>{fmt_ms(row['interrupt_tool_call_ms'])}</strong></div>
                <div><span>工具结果后首 delta</span><strong>{fmt_ms(row['interrupt_tool_result_llm_first_delta_ms'])}</strong></div>
                <div><span>工具结果后可播报</span><strong>{fmt_ms(row['interrupt_tool_result_llm_first_token_ms'])}</strong></div>
                <div><span>首轮上传结束到首包</span><strong>{fmt_ms(row['audio_after_prompt_end_ms'])}</strong></div>
                <div><span>打断到 stop</span><strong>{fmt_ms(row['interrupt_to_stop_ms'])}</strong></div>
                <div><span>stop 后旧音频</span><strong>{row['stale_audio_after_stop']} 包</strong></div>
                <div><span>stop 后新回复音频</span><strong>{row['new_audio_after_stop']} 包</strong></div>
              </div>
              <ol class="timeline">
                {''.join(timeline_html)}
              </ol>
            </section>
            """
        )

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>全双工仿真客户端 E2E 报告</title>
  <style>
    :root {{ --bg:#f6f7f9; --panel:#fff; --ink:#1f2933; --muted:#667085; --line:#d9dee7; --green:#13795b; --green-bg:#e8f5ef; --red:#b42318; --red-bg:#fff0ee; --amber:#854a0e; --amber-bg:#fff7d6; --blue:#175cd3; --blue-bg:#edf4ff; --shadow:0 10px 30px rgba(31,41,51,.08); }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; line-height:1.55; }}
    main {{ max-width:1240px; margin:0 auto; padding:28px 22px 46px; }}
    header {{ display:grid; grid-template-columns:1fr auto; gap:18px; align-items:end; margin-bottom:18px; }}
    h1 {{ margin:0 0 8px; font-size:28px; letter-spacing:0; }}
    h2 {{ margin:0 0 14px; font-size:18px; letter-spacing:0; }}
    h3 {{ margin:0 0 8px; font-size:17px; letter-spacing:0; }}
    p {{ margin:0; }}
    .muted {{ color:var(--muted); }}
    .timestamp {{ padding:12px 14px; border:1px solid var(--line); border-radius:6px; background:var(--panel); color:var(--muted); font-size:13px; text-align:right; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:18px 0; }}
    .card,.panel,.case-card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); }}
    .card {{ min-height:106px; padding:16px; }}
    .label {{ color:var(--muted); font-size:13px; }}
    .value {{ margin-top:8px; font-size:30px; font-weight:760; line-height:1; }}
    .note {{ margin-top:8px; color:var(--muted); font-size:13px; }}
    .panel {{ padding:18px; margin-top:14px; }}
    .case-card {{ padding:18px; margin-top:14px; }}
    .case-head {{ display:grid; grid-template-columns:1fr auto; gap:14px; align-items:start; }}
    .case-metrics {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; margin:14px 0; }}
    .case-metrics div {{ padding:10px 12px; border:1px solid var(--line); border-radius:6px; background:#f8fafc; }}
    .case-metrics span {{ display:block; color:var(--muted); font-size:12px; }}
    .case-metrics strong {{ display:block; margin-top:4px; font-size:16px; }}
    .timeline {{ position:relative; display:grid; gap:8px; margin:0; padding:0; list-style:none; }}
    .timeline li {{ display:grid; grid-template-columns:minmax(180px,1fr) 110px minmax(120px,.7fr); gap:10px; align-items:center; padding:9px 10px; border:1px solid var(--line); border-radius:6px; background:#fff; }}
    .timeline strong {{ font-size:13px; }}
    .timeline em {{ color:var(--blue); font-style:normal; font-weight:700; }}
    .timeline span {{ color:var(--muted); font-size:13px; }}
    table {{ width:100%; border-collapse:collapse; min-width:1480px; font-size:13px; }}
    th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ background:#f2f4f7; color:#344054; font-size:12px; }}
    .table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:8px; }}
    .failed {{ background:#fff8f7; }}
    .ok {{ display:inline-block; padding:3px 8px; border-radius:999px; background:var(--green-bg); color:var(--green); font-weight:700; }}
    .fail {{ display:inline-block; padding:3px 8px; border-radius:999px; background:var(--red-bg); color:var(--red); font-weight:700; }}
    .invalid {{ display:inline-block; padding:3px 8px; border-radius:999px; background:var(--amber-bg); color:var(--amber); font-weight:700; }}
    code {{ padding:2px 5px; border:1px solid var(--line); border-radius:4px; background:#f8fafc; font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; font-size:.92em; }}
    pre {{ overflow:auto; padding:14px; border-radius:6px; background:#111827; color:#fff; font-size:13px; }}
    @media (max-width:900px) {{ header,.grid,.case-head,.case-metrics {{ grid-template-columns:1fr; }} main {{ padding:20px 14px; }} .timeline li {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>全双工仿真客户端 E2E 报告</h1>
      <p class="muted">真实 websocket 客户端仿真：首轮用户输入和播放中插话都使用 wav 音频转 Opus 后按 {args.frame_ms}ms 一帧流式发送，触发真实 ASR / LLM / function_call / TTS。</p>
    </div>
    <div class="timestamp">生成时间<br>2026-06-05</div>
  </header>
  <section class="grid">
    <div class="card"><div class="label">通过用例</div><div class="value">{passed}/{total}</div><div class="note">第 1 轮为暖机轮，其余轮次要求 LLM 首 token &lt; 1s</div></div>
    <div class="card"><div class="label">测试轮次</div><div class="value">{args.rounds}</div><div class="note">每轮新建 websocket，服务进程不重启</div></div>
    <div class="card"><div class="label">平均首轮上传结束到首包</div><div class="value">{fmt_ms(avg_response)}</div><div class="note">ASR final + LLM/function_call + TTS 首包</div></div>
    <div class="card"><div class="label">后续轮 LLM 首 delta</div><div class="value">{fmt_ms(avg_measured_llm_first_token)}</div><div class="note">命中 &lt;1s：{cache_passed}/{len(measured_llm_first_token)}</div></div>
  </section>
  <section class="panel">
    <h2>测试口径</h2>
    <p>仿真客户端先发送一段真实首轮用户语音，等客户端收到第一包 TTS 音频后再等待 {args.interrupt_after_ms}ms，然后发送真实插话语音。报告里的 T+ 时间都是从单个用例开始计时；“ASR final 到 LLM 首 delta”来自服务端 turn_metrics，用于判断 MLX prompt cache；“ASR final 到可播报文本”表示 function_call/direct_answer 中真正能送 TTS 的首段文本；“stop 后旧音频”只统计 stop 到下一次 TTS start 之前的音频，新 turn 回复会单独列为“stop 后新回复音频”。</p>
  </section>
  <section class="panel">
    <h2>用例时间线</h2>
    {''.join(case_sections)}
  </section>
  <section class="panel">
    <h2>结果明细</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>轮次</th><th>用例</th><th>类型</th><th>实际场景</th><th>ASR 识别</th><th>阶段状态</th><th>无效原因</th>
            <th>预期 stop</th><th>实际 stop</th>
            <th>插话音频时长</th><th>首轮上传结束到首包</th>
            <th>打断到 stop</th><th>服务端 abort_to_stop</th>
            <th>首轮 LLM 首 delta</th><th>首轮可播报文本</th>
            <th>插话 LLM 首 delta</th><th>插话可播报文本</th>
            <th>插话工具</th><th>工具选择</th><th>工具执行</th><th>工具结果后首 delta</th><th>工具结果后可播报</th>
            <th>打断后音频包</th><th>stop 后旧音频</th><th>stop 后新回复</th><th>服务端丢弃旧音频</th><th>结果</th>
          </tr>
        </thead>
        <tbody>
          {''.join(table_rows)}
        </tbody>
      </table>
    </div>
  </section>
  <section class="panel">
    <h2>复现命令</h2>
    <pre>main/xiaozhi-server/venv/bin/python main/xiaozhi-server/performance_tester/performance_tester_full_duplex_client.py \\
  --url {args.url} \\
  --interrupt-after-ms {args.interrupt_after_ms}</pre>
  </section>
</main>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


async def async_main(args):
    selected = CASES
    if args.case:
        selected = [case for case in CASES if case["id"] in set(args.case)]
    rows = []
    for round_index in range(1, args.rounds + 1):
        for case in selected:
            rows.append(await run_case(args, case, round_index))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            if not row["valid"]:
                status = "INVALID"
            elif row["ok"]:
                status = "OK"
            else:
                status = "FAIL"
            print(
                f"round={row['round']}",
                row["id"],
                status,
                "stopped=", row["stopped"],
                "prompt_delta=", fmt_ms(row["llm_first_delta_ms"]),
                "prompt_speakable=", fmt_ms(row["llm_first_token_ms"]),
                "interrupt_delta=", fmt_ms(row["interrupt_llm_first_delta_ms"]),
                "interrupt_speakable=", fmt_ms(row["interrupt_llm_first_token_ms"]),
                "interrupt_tools=", fmt_tools(row["interrupt_tool_names"]),
                "interrupt_to_stop=", fmt_ms(row["interrupt_to_stop_ms"]),
                "stale_audio_after_stop=", row["stale_audio_after_stop"],
                "new_audio_after_stop=", row["new_audio_after_stop"],
            )
    if args.report:
        write_html_report(Path(args.report), rows, args)
        print(f"报告已写入: {args.report}")
    return 0 if all(row["ok"] for row in rows) else 1


def main():
    parser = argparse.ArgumentParser(description="全双工仿真客户端 E2E 测试")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--device-id", default="codex-full-duplex-client")
    parser.add_argument("--client-id", default="codex-client")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR))
    parser.add_argument("--prompt-audio", default=str(DEFAULT_PROMPT_AUDIO))
    parser.add_argument("--frame-ms", type=int, default=60)
    parser.add_argument("--interrupt-after-ms", type=int, default=700)
    parser.add_argument("--init-wait-ms", type=int, default=1500)
    parser.add_argument("--tts-timeout-sec", type=float, default=60)
    parser.add_argument("--after-interrupt-wait-sec", type=float, default=5)
    parser.add_argument("--case", action="append")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    raise SystemExit(asyncio.run(async_main(parser.parse_args())))


if __name__ == "__main__":
    main()
