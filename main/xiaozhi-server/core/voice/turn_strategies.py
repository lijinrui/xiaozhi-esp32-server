"""Turn start strategies for realtime voice interruption decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TurnStartDecision(str, Enum):
    IGNORE = "ignore"
    CANDIDATE = "candidate"
    START = "start"


@dataclass(frozen=True)
class TurnStartResult:
    decision: TurnStartDecision
    reason: str


BACKCHANNEL_WORDS = {
    "嗯",
    "嗯嗯",
    "好",
    "好的",
    "对",
    "对的",
    "哦",
    "噢",
    "可以",
    "行",
    "是",
    "是的",
    "啊",
}

EXPLICIT_INTERRUPT_PHRASES = (
    "停一下",
    "停下",
    "停止",
    "暂停",
    "别说了",
    "不要说了",
    "等等",
    "等一下",
    "先别说",
    "打断一下",
    "不是",
    "不对",
    "错了",
    "我说的是",
    "我问的是",
    "换一个",
    "重新说",
)

QUESTION_HINTS = (
    "吗",
    "呢",
    "什么",
    "怎么",
    "为什么",
    "多少",
    "哪里",
    "哪个",
    "能不能",
    "可不可以",
    "?",
    "？",
)

NOISE_PLACEHOLDER_RE = re.compile(r"^(笑声|咳嗽|噪声|杂音|沉默|silence|noise|cough|laugh)$", re.I)


def normalize_utterance(text: str | None) -> str:
    if text is None:
        return ""
    return re.sub(r"[\s，。！？、,.!?;；：:\"'“”‘’（）()【】\[\]]+", "", text).strip().lower()


def contains_any(text: str, phrases: tuple[str, ...]) -> str | None:
    for phrase in phrases:
        if phrase and phrase in text:
            return phrase
    return None


class RuleBasedTurnStartStrategy:
    """Small deterministic strategy for barge-in turn starts.

    It intentionally avoids provider-specific assumptions. Partial/final ASR
    text can both pass through this strategy.
    """

    def __init__(self, min_candidate_chars: int = 2, min_start_chars: int = 4):
        self.min_candidate_chars = min_candidate_chars
        self.min_start_chars = min_start_chars

    def decide(
        self,
        text: str | None,
        *,
        wake_word: bool = False,
        is_final: bool = True,
    ) -> TurnStartResult:
        normalized = normalize_utterance(text)

        if wake_word:
            return TurnStartResult(TurnStartDecision.START, "wake_word")
        if not normalized:
            return TurnStartResult(TurnStartDecision.IGNORE, "empty")
        if NOISE_PLACEHOLDER_RE.match(normalized):
            return TurnStartResult(TurnStartDecision.IGNORE, "noise_placeholder")
        if normalized in BACKCHANNEL_WORDS:
            return TurnStartResult(TurnStartDecision.IGNORE, "backchannel")

        phrase = contains_any(normalized, EXPLICIT_INTERRUPT_PHRASES)
        if phrase:
            return TurnStartResult(TurnStartDecision.START, f"explicit_interrupt:{phrase}")

        if len(normalized) < self.min_candidate_chars:
            return TurnStartResult(TurnStartDecision.IGNORE, "too_short")

        if any(hint in normalized for hint in QUESTION_HINTS) and len(normalized) >= self.min_start_chars:
            return TurnStartResult(TurnStartDecision.START, "question_like")

        if is_final and len(normalized) >= self.min_start_chars:
            return TurnStartResult(TurnStartDecision.START, "final_enough")

        return TurnStartResult(TurnStartDecision.CANDIDATE, "short_candidate")

