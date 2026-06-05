from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np


SERVER_ROOT = Path(__file__).resolve().parents[1]
os.chdir(SERVER_ROOT)
sys.path.insert(0, str(SERVER_ROOT))

from config.settings import load_config  # noqa: E402
from core.utils.asr import create_instance as create_asr_instance  # noqa: E402
from core.voice.barge_in_config import resolve_barge_in_config  # noqa: E402
from core.voice.interrupt_classifier import (  # noqa: E402
    InterruptDecision,
    RuleBasedInterruptClassifier,
)


def load_rule_test_cases():
    module_path = SERVER_ROOT / "performance_tester" / "performance_tester_barge_in.py"
    spec = importlib.util.spec_from_file_location("barge_in_rule_cases", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载规则测试用例: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MODES, module.TEST_CASES


MODES, TEST_CASES = load_rule_test_cases()


DEFAULT_AUDIO_DIR = SERVER_ROOT / "tmp" / "barge_in_audio"
DEFAULT_FRAME_MS = 60


def case_audio_text(case: dict) -> str:
    if case.get("audio_text"):
        return case["audio_text"]
    if case["id"] == "wake_word":
        return "小智小智"
    if case["id"] == "ordinary_final_pause":
        return "我觉得这个回答还可以。"
    return case["text"]


def safe_case_filename(case_id: str) -> str:
    return f"{case_id}.wav"


def run_command(args: list[str]) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def generate_audio_files(audio_dir: Path, voice: str | None) -> None:
    audio_dir.mkdir(parents=True, exist_ok=True)
    for case in TEST_CASES:
        text = case_audio_text(case)
        if not text:
            continue
        wav_path = audio_dir / safe_case_filename(case["id"])
        aiff_path = wav_path.with_suffix(".aiff")
        say_cmd = ["say"]
        if voice:
            say_cmd.extend(["-v", voice])
        say_cmd.extend(["-o", str(aiff_path), text])
        run_command(say_cmd)
        run_command([
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            "-c",
            "1",
            str(aiff_path),
            str(wav_path),
        ])
        aiff_path.unlink(missing_ok=True)


def read_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        if channels != 1 or sample_width != 2 or sample_rate != 16000:
            raise ValueError(
                f"{path} 需要是 16kHz/单声道/16bit WAV，当前是 "
                f"{sample_rate}Hz/{channels}ch/{sample_width * 8}bit"
            )
        return wav_file.readframes(wav_file.getnframes())


def build_asr_provider(asr_name: str | None):
    config = load_config()
    selected_asr = asr_name or config.get("selected_module", {}).get("ASR")
    if not selected_asr:
        raise ValueError("配置里没有 selected_module.ASR，请用 --asr 指定")
    asr_config = config.get("ASR", {}).get(selected_asr)
    if not asr_config:
        raise ValueError(f"找不到 ASR 配置: {selected_asr}")
    module_type = asr_config.get("type", selected_asr)
    provider = create_asr_instance(module_type, asr_config, delete_audio_file=True)
    provider.audio_format = "pcm"
    return selected_asr, provider


def is_streaming_sherpa_provider(asr_provider) -> bool:
    return (
        hasattr(asr_provider, "recognizer")
        and hasattr(asr_provider.recognizer, "create_stream")
    )


def should_cancel(mode: str, decision: InterruptDecision) -> bool:
    _, should_interrupt, soft_as_hard = resolve_barge_in_config({"barge_in_mode": mode})
    if not should_interrupt:
        return False
    if decision == InterruptDecision.HARD_INTERRUPT:
        return True
    return decision == InterruptDecision.SOFT_INTERRUPT and soft_as_hard


async def run_one_case(asr_provider, classifier, case: dict, audio_dir: Path, repeat: int) -> dict:
    audio_path = audio_dir / safe_case_filename(case["id"])
    if not audio_path.exists():
        return {
            "id": case["id"],
            "group": case["group"],
            "audio": str(audio_path),
            "error": "缺少音频文件",
            "ok": False,
        }

    pcm = read_wav_pcm(audio_path)
    asr_times_ms = []
    rule_times_us = []
    texts = []
    last_result = None

    for idx in range(repeat):
        session_id = f"barge-in-e2e-{case['id']}-{idx}"
        asr_started = time.perf_counter()
        text, _ = await asr_provider.speech_to_text_wrapper([pcm], session_id, "pcm")
        asr_times_ms.append((time.perf_counter() - asr_started) * 1000)
        recognized = (text or "").strip()
        texts.append(recognized)

        wake_word = bool(case.get("wake_word")) or any(
            word in recognized for word in ("小智", "你好小智")
        )
        rule_started = time.perf_counter_ns()
        interrupt = classifier.classify(
            is_speaking=True,
            text=recognized,
            wake_word=wake_word,
            is_final=case.get("is_final", True),
        )
        rule_times_us.append((time.perf_counter_ns() - rule_started) / 1000)

        modes = {}
        mode_ok = {}
        for mode in MODES:
            actual = should_cancel(mode, interrupt.decision)
            expected = case["expected"][mode]
            modes[mode] = actual
            mode_ok[mode] = actual == expected

        last_result = {
            "id": case["id"],
            "group": case["group"],
            "audio": str(audio_path),
            "expected_text": case_audio_text(case),
            "recognized_text": recognized,
            "decision": interrupt.decision.value,
            "reason": interrupt.reason,
            "modes": modes,
            "expected": case["expected"],
            "mode_ok": mode_ok,
            "ok": all(mode_ok.values()),
        }

    last_result["asr_avg_ms"] = statistics.fmean(asr_times_ms)
    last_result["asr_p95_ms"] = (
        statistics.quantiles(asr_times_ms, n=20)[18] if repeat >= 20 else max(asr_times_ms)
    )
    last_result["rule_avg_us"] = statistics.fmean(rule_times_us)
    last_result["rule_p95_us"] = (
        statistics.quantiles(rule_times_us, n=20)[18] if repeat >= 20 else max(rule_times_us)
    )
    last_result["all_recognized_texts"] = texts
    return last_result


def decode_ready_streams(asr_provider, stream) -> str:
    while asr_provider.recognizer.is_ready(stream):
        asr_provider.recognizer.decode_stream(stream)
    result = asr_provider.recognizer.get_result(stream)
    return result if isinstance(result, str) else getattr(result, "text", "")


def collect_streaming_events(
    asr_provider,
    pcm: bytes,
    frame_ms: int,
    tail_padding_ms: int,
) -> tuple[list[dict], float]:
    stream = asr_provider.recognizer.create_stream()
    sample_rate = 16000
    samples_per_frame = int(sample_rate * frame_ms / 1000)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    events = []
    last_text = ""

    started = time.perf_counter()
    for offset in range(0, len(samples), samples_per_frame):
        chunk = samples[offset : offset + samples_per_frame]
        if len(chunk) == 0:
            continue
        stream.accept_waveform(sample_rate, chunk)
        current_text = decode_ready_streams(asr_provider, stream).strip()
        audio_ms = min(offset + len(chunk), len(samples)) / sample_rate * 1000
        if current_text and current_text != last_text:
            events.append(
                {
                    "kind": "interim",
                    "audio_ms": audio_ms,
                    "text": current_text,
                }
            )
            last_text = current_text

    tail_padding = np.zeros(int(tail_padding_ms * sample_rate / 1000), dtype=np.float32)
    if len(tail_padding):
        stream.accept_waveform(sample_rate, tail_padding)
    if hasattr(stream, "input_finished"):
        stream.input_finished()
    final_text = decode_ready_streams(asr_provider, stream).strip()
    if final_text and final_text != last_text:
        events.append(
            {
                "kind": "final",
                "audio_ms": len(samples) / sample_rate * 1000,
                "text": final_text,
            }
        )
    elif final_text:
        events.append(
            {
                "kind": "final",
                "audio_ms": len(samples) / sample_rate * 1000,
                "text": final_text,
            }
        )

    return events, (time.perf_counter() - started) * 1000


async def run_one_streaming_case(
    asr_provider,
    classifier,
    case: dict,
    audio_dir: Path,
    repeat: int,
    frame_ms: int,
    tail_padding_ms: int,
) -> dict:
    audio_path = audio_dir / safe_case_filename(case["id"])
    if not audio_path.exists():
        return {
            "id": case["id"],
            "group": case["group"],
            "audio": str(audio_path),
            "error": "缺少音频文件",
            "ok": False,
        }

    pcm = read_wav_pcm(audio_path)
    asr_times_ms = []
    rule_times_us = []
    last_result = None

    for _ in range(repeat):
        events, asr_time_ms = collect_streaming_events(
            asr_provider,
            pcm,
            frame_ms,
            tail_padding_ms,
        )
        asr_times_ms.append(asr_time_ms)
        first_cancel = {mode: None for mode in MODES}
        event_results = []

        for event in events:
            recognized = event["text"]
            wake_word = bool(case.get("wake_word")) or any(
                word in recognized for word in ("小智", "你好小智")
            )
            rule_started = time.perf_counter_ns()
            interrupt = classifier.classify(
                is_speaking=True,
                text=recognized,
                wake_word=wake_word,
                is_final=event["kind"] == "final",
            )
            rule_times_us.append((time.perf_counter_ns() - rule_started) / 1000)

            modes = {}
            for mode in MODES:
                actual = should_cancel(mode, interrupt.decision)
                modes[mode] = actual
                if actual and first_cancel[mode] is None:
                    first_cancel[mode] = event["audio_ms"]

            event_results.append(
                {
                    **event,
                    "decision": interrupt.decision.value,
                    "reason": interrupt.reason,
                    "modes": modes,
                }
            )

        final_event = event_results[-1] if event_results else {}
        mode_ok = {
            mode: bool(first_cancel[mode] is not None) == case["expected"][mode]
            for mode in MODES
        }
        last_result = {
            "id": case["id"],
            "group": case["group"],
            "audio": str(audio_path),
            "expected_text": case_audio_text(case),
            "recognized_text": final_event.get("text", ""),
            "decision": final_event.get("decision", "ignore"),
            "reason": final_event.get("reason", "empty"),
            "modes": {mode: first_cancel[mode] is not None for mode in MODES},
            "cancel_audio_ms": first_cancel,
            "expected": case["expected"],
            "mode_ok": mode_ok,
            "ok": all(mode_ok.values()),
            "events": event_results,
        }

    last_result["asr_avg_ms"] = statistics.fmean(asr_times_ms)
    last_result["asr_p95_ms"] = (
        statistics.quantiles(asr_times_ms, n=20)[18] if repeat >= 20 else max(asr_times_ms)
    )
    last_result["rule_avg_us"] = statistics.fmean(rule_times_us) if rule_times_us else 0
    last_result["rule_p95_us"] = (
        statistics.quantiles(rule_times_us, n=20)[18]
        if len(rule_times_us) >= 20
        else (max(rule_times_us) if rule_times_us else 0)
    )
    return last_result


def yn(value: bool) -> str:
    return "Y" if value else "N"


def print_table(rows: list[dict]) -> None:
    headers = (
        "case",
        "asr_ms",
        "rule_us",
        "recognized",
        "decision",
        "reason",
        "off",
        "normal",
        "sensitive",
        "cancel_ms",
        "ok",
    )
    values_by_row = []
    widths = {header: len(header) for header in headers}
    for row in rows:
        if row.get("error"):
            values = {
                "case": row["id"],
                "asr_ms": "-",
                "rule_us": "-",
                "recognized": row["error"],
                "decision": "-",
                "reason": "-",
                "off": "-",
                "normal": "-",
                "sensitive": "-",
                "cancel_ms": "-",
                "ok": "FAIL",
            }
        else:
            values = {
                "case": row["id"],
                "asr_ms": f"{row['asr_avg_ms']:.1f}",
                "rule_us": f"{row['rule_avg_us']:.2f}",
                "recognized": row["recognized_text"][:24],
                "decision": row["decision"],
                "reason": row["reason"],
                "off": yn(row["modes"]["off"]),
                "normal": yn(row["modes"]["normal"]),
                "sensitive": yn(row["modes"]["sensitive"]),
                "cancel_ms": format_cancel_ms(row.get("cancel_audio_ms")),
                "ok": "OK" if row["ok"] else "FAIL",
            }
        values_by_row.append(values)
        for key, value in values.items():
            widths[key] = max(widths[key], len(value))

    print(" | ".join(header.ljust(widths[header]) for header in headers))
    print("-+-".join("-" * widths[header] for header in headers))
    for values in values_by_row:
        print(" | ".join(values[header].ljust(widths[header]) for header in headers))


def format_cancel_ms(cancel_audio_ms: dict | None) -> str:
    if not cancel_audio_ms:
        return "-"
    parts = []
    for mode in ("normal", "sensitive"):
        value = cancel_audio_ms.get(mode)
        if value is not None:
            parts.append(f"{mode}:{value:.0f}")
    return ",".join(parts) if parts else "-"


async def async_main(args) -> int:
    audio_dir = Path(args.audio_dir)
    if args.generate_audio:
        generate_audio_files(audio_dir, args.voice)

    asr_name, asr_provider = build_asr_provider(args.asr)
    classifier = RuleBasedInterruptClassifier()
    rows = []
    for case in TEST_CASES:
        if args.case and case["id"] not in args.case:
            continue
        if args.stream:
            if not is_streaming_sherpa_provider(asr_provider):
                raise ValueError("--stream 目前需要 SherpaASRStream 这类 sherpa-onnx 流式 ASR")
            rows.append(
                await run_one_streaming_case(
                    asr_provider,
                    classifier,
                    case,
                    audio_dir,
                    args.repeat,
                    args.frame_ms,
                    args.tail_padding_ms,
                )
            )
        else:
            rows.append(await run_one_case(asr_provider, classifier, case, audio_dir, args.repeat))

    if args.json:
        print(
            json.dumps(
                {"asr": asr_name, "stream": args.stream, "rows": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"ASR: {asr_name}")
        print(f"模式: {'流式' if args.stream else '非流式'}")
        print(f"音频目录: {audio_dir}")
        print_table(rows)

    failed = [row for row in rows if not row.get("ok")]
    if failed:
        print(f"\nFAILED: {len(failed)} 个用例未符合预期。")
        return 1
    print(f"\nOK: {len(rows)} 个用例符合预期。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="播放中插话打断的离线音频 E2E 测试工具。")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="测试音频目录")
    parser.add_argument("--generate-audio", action="store_true", help="用 macOS say 生成测试音频")
    parser.add_argument("--voice", help="macOS say 语音名称，例如 Tingting 或 Eddy")
    parser.add_argument("--asr", help="指定 ASR 配置名；默认使用 selected_module.ASR")
    parser.add_argument("--stream", action="store_true", help="按固定帧长流式送入 ASR")
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS, help="流式送入 ASR 的帧长，单位毫秒")
    parser.add_argument("--tail-padding-ms", type=int, default=1200, help="流式音频结束后追加的静音时长，单位毫秒")
    parser.add_argument("--repeat", type=int, default=1, help="每条音频重复识别次数")
    parser.add_argument("--case", action="append", help="只运行指定 case，可重复传入")
    parser.add_argument("--json", action="store_true", help="输出 JSON，而不是表格")
    return asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
