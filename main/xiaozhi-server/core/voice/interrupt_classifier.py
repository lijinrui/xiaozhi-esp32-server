"""播放中用户输入的插话打断分类器。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.voice.turn_strategies import (
    RuleBasedTurnStartStrategy,
    TurnStartDecision,
)


class InterruptDecision(str, Enum):
    IGNORE = "ignore"
    SOFT_INTERRUPT = "soft_interrupt"
    HARD_INTERRUPT = "hard_interrupt"
    RESUME_PREVIOUS = "resume_previous"


@dataclass(frozen=True)
class InterruptResult:
    decision: InterruptDecision
    reason: str

    @property
    def should_cancel(self) -> bool:
        return self.decision == InterruptDecision.HARD_INTERRUPT


class RuleBasedInterruptClassifier:
    """不依赖具体 ASR/TTS 服务商的规则分类器。

    这版规则刻意偏保守：明确纠正和明显问题会立即打断，
    附和词会被忽略，短而模糊的插话会先作为软打断候选。
    """

    def __init__(self, start_strategy: RuleBasedTurnStartStrategy | None = None):
        self.start_strategy = start_strategy or RuleBasedTurnStartStrategy()

    def classify(
        self,
        *,
        is_speaking: bool,
        text: str | None,
        wake_word: bool = False,
        is_final: bool = True,
    ) -> InterruptResult:
        if not is_speaking:
            return InterruptResult(InterruptDecision.IGNORE, "not_speaking")

        start = self.start_strategy.decide(text, wake_word=wake_word, is_final=is_final)
        if start.decision == TurnStartDecision.IGNORE:
            return InterruptResult(InterruptDecision.IGNORE, start.reason)
        if start.decision == TurnStartDecision.CANDIDATE:
            return InterruptResult(InterruptDecision.SOFT_INTERRUPT, start.reason)
        return InterruptResult(InterruptDecision.HARD_INTERRUPT, start.reason)


@dataclass(frozen=True)
class LegacyInterruptDecision:
    action: str
    reason: str


class InterruptClassifier:
    """旧测试和旧调用点使用的兼容包装。

    新代码应直接使用 RuleBasedInterruptClassifier，它返回枚举形式的
    InterruptResult；旧接口仍返回字符串 action。
    """

    def __init__(self, classifier: RuleBasedInterruptClassifier | None = None):
        self.classifier = classifier or RuleBasedInterruptClassifier()

    def classify(
        self,
        playback_state=None,
        vad_event=None,
        asr_partial=None,
        asr_final=None,
        wake_word=None,
        current_turn_id=None,
    ):
        is_speaking = bool(current_turn_id) and playback_state in ("speaking", "playing", "tts")
        result = self.classifier.classify(
            is_speaking=is_speaking,
            text=asr_final or asr_partial or "",
            wake_word=bool(wake_word),
            is_final=asr_final is not None,
        )
        action = {
            InterruptDecision.IGNORE: "ignore",
            InterruptDecision.SOFT_INTERRUPT: "soft_interrupt",
            InterruptDecision.HARD_INTERRUPT: "hard_interrupt",
            InterruptDecision.RESUME_PREVIOUS: "resume_previous",
        }[result.decision]
        return LegacyInterruptDecision(action, result.reason)
