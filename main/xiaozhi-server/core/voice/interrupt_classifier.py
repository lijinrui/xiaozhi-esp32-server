"""Interruption classifier for speaking-time user input."""

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
    """Provider-agnostic interruption classifier.

    The first version is deliberately conservative: explicit corrections and
    questions cancel immediately, backchannels are ignored, short ambiguous
    speech becomes a soft interruption candidate.
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
    """Compatibility wrapper for older tests and call sites.

    New code should use RuleBasedInterruptClassifier, which returns enum-based
    InterruptResult objects. The older API returns string actions.
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
