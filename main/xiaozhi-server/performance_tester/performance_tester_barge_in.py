from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from core.voice.barge_in_config import (  # noqa: E402
    BARGE_IN_MODE_NORMAL,
    BARGE_IN_MODE_OFF,
    BARGE_IN_MODE_SENSITIVE,
    resolve_barge_in_config,
)
from core.voice.interrupt_classifier import (  # noqa: E402
    InterruptDecision,
    RuleBasedInterruptClassifier,
)


MODES = (BARGE_IN_MODE_OFF, BARGE_IN_MODE_NORMAL, BARGE_IN_MODE_SENSITIVE)

TEST_CASES = [
    {
        "id": "backchannel_um",
        "group": "不应打断: 附和",
        "text": "嗯",
        "expected": {"off": False, "normal": False, "sensitive": False},
    },
    {
        "id": "backchannel_ok",
        "group": "不应打断: 附和",
        "text": "好的",
        "expected": {"off": False, "normal": False, "sensitive": False},
    },
    {
        "id": "noise_placeholder",
        "group": "不应打断: 噪声占位",
        "text": "咳嗽",
        "expected": {"off": False, "normal": False, "sensitive": False},
    },
    {
        "id": "explicit_stop",
        "group": "normal 应打断: 明确叫停",
        "text": "停一下",
        "expected": {"off": False, "normal": True, "sensitive": True},
    },
    {
        "id": "explicit_wait",
        "group": "normal 应打断: 明确叫停",
        "text": "等等，我重新问",
        "expected": {"off": False, "normal": True, "sensitive": True},
    },
    {
        "id": "correction_wrong_target",
        "group": "normal 应打断: 纠正",
        "text": "不是这个，我问的是天气",
        "expected": {"off": False, "normal": True, "sensitive": True},
    },
    {
        "id": "question_weather",
        "group": "normal 应打断: 新问题",
        "text": "那明天天气怎么样",
        "expected": {"off": False, "normal": True, "sensitive": True},
    },
    {
        "id": "question_rephrase",
        "group": "normal 应打断: 新问题",
        "text": "能不能换一种说法",
        "expected": {"off": False, "normal": True, "sensitive": True},
    },
    {
        "id": "wake_word",
        "group": "normal 应打断: 唤醒词",
        "text": "",
        "wake_word": True,
        "expected": {"off": False, "normal": True, "sensitive": True},
    },
    {
        "id": "hesitation_short_partial",
        "group": "仅 sensitive 打断: 短插话",
        "text": "那个",
        "is_final": False,
        "expected": {"off": False, "normal": False, "sensitive": True},
    },
    {
        "id": "ordinary_final_pause",
        "group": "仅 sensitive 打断: 用户停顿前半句",
        "text": "我觉得这个回答还可以",
        "expected": {"off": False, "normal": False, "sensitive": True},
        "audio_prompt": "我觉得这个回答还可以，停顿半秒，然后继续说：不过你能不能再短一点",
    },
    {
        "id": "ordinary_final_environment",
        "group": "仅 sensitive 打断: 普通陈述",
        "text": "今天外面声音有点大",
        "expected": {"off": False, "normal": False, "sensitive": True},
    },
]


def should_cancel(mode: str, decision: InterruptDecision) -> bool:
    _, should_interrupt, soft_as_hard = resolve_barge_in_config({"barge_in_mode": mode})
    if not should_interrupt:
        return False
    if decision == InterruptDecision.HARD_INTERRUPT:
        return True
    return decision == InterruptDecision.SOFT_INTERRUPT and soft_as_hard


def evaluate_case(classifier: RuleBasedInterruptClassifier, case: dict, mode: str) -> dict:
    started = time.perf_counter_ns()
    result = classifier.classify(
        is_speaking=True,
        text=case["text"],
        wake_word=bool(case.get("wake_word", False)),
        is_final=case.get("is_final", True),
    )
    elapsed_us = (time.perf_counter_ns() - started) / 1000
    actual = should_cancel(mode, result.decision)
    expected = case["expected"][mode]
    return {
        "id": case["id"],
        "group": case["group"],
        "mode": mode,
        "text": case["text"],
        "decision": result.decision.value,
        "reason": result.reason,
        "cancel": actual,
        "expected": expected,
        "ok": actual == expected,
        "elapsed_us": elapsed_us,
    }


def benchmark_case(
    classifier: RuleBasedInterruptClassifier,
    case: dict,
    mode: str,
    repeat: int,
) -> dict:
    elapsed = []
    last = None
    for _ in range(repeat):
        result = evaluate_case(classifier, case, mode)
        elapsed.append(result["elapsed_us"])
        last = result
    last["avg_us"] = statistics.fmean(elapsed)
    last["p95_us"] = statistics.quantiles(elapsed, n=20)[18] if repeat >= 20 else max(elapsed)
    return last


def print_table(rows: list[dict]) -> None:
    headers = ("case", "mode", "cancel", "decision", "reason", "avg_us", "p95_us", "ok")
    widths = {header: len(header) for header in headers}
    for row in rows:
        values = {
            "case": row["id"],
            "mode": row["mode"],
            "cancel": "Y" if row["cancel"] else "N",
            "decision": row["decision"],
            "reason": row["reason"],
            "avg_us": f"{row['avg_us']:.2f}",
            "p95_us": f"{row['p95_us']:.2f}",
            "ok": "OK" if row["ok"] else "FAIL",
        }
        row["_values"] = values
        for key, value in values.items():
            widths[key] = max(widths[key], len(value))

    print(" | ".join(header.ljust(widths[header]) for header in headers))
    print("-+-".join("-" * widths[header] for header in headers))
    for row in rows:
        values = row["_values"]
        print(" | ".join(values[header].ljust(widths[header]) for header in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description="播放中插话打断模式的行为和耗时测试工具。")
    parser.add_argument("--repeat", type=int, default=10000, help="每个用例/模式重复执行的次数")
    parser.add_argument("--json", action="store_true", help="输出 JSON，而不是表格")
    parser.add_argument(
        "--cases-json",
        type=Path,
        help="导出原始用例，后续生成离线音频时复用",
    )
    args = parser.parse_args()

    classifier = RuleBasedInterruptClassifier()
    rows = []
    for case in TEST_CASES:
        for mode in MODES:
            rows.append(benchmark_case(classifier, case, mode, args.repeat))

    if args.cases_json:
        args.cases_json.write_text(
            json.dumps(TEST_CASES, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows)

    failed = [row for row in rows if not row["ok"]]
    if failed:
        print(f"\nFAILED: {len(failed)} case/mode combinations did not match expectations.")
        return 1
    print(f"\nOK: {len(rows)} case/mode combinations matched expectations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
