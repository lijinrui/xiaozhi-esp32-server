from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1] / "main" / "xiaozhi-server"
sys.path.insert(0, str(SERVER_ROOT))

from core.voice.interrupt_classifier import (  # noqa: E402
    InterruptDecision,
    RuleBasedInterruptClassifier,
)
from core.voice.turn_strategies import (  # noqa: E402
    RuleBasedTurnStartStrategy,
    TurnStartDecision,
    normalize_utterance,
)


class TurnStartStrategyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = RuleBasedTurnStartStrategy()

    def test_backchannel_is_ignored(self) -> None:
        for text in ("嗯", "好", "对", "哦", "可以", "行"):
            with self.subTest(text=text):
                result = self.strategy.decide(text)
                self.assertEqual(result.decision, TurnStartDecision.IGNORE)
                self.assertEqual(result.reason, "backchannel")

    def test_noise_placeholder_is_ignored(self) -> None:
        for text in ("咳嗽", "笑声", "noise", "silence"):
            with self.subTest(text=text):
                result = self.strategy.decide(text)
                self.assertEqual(result.decision, TurnStartDecision.IGNORE)

    def test_explicit_interrupt_starts_turn(self) -> None:
        for text in ("停一下", "等等", "别说了", "不是这个", "我问的是天气"):
            with self.subTest(text=text):
                result = self.strategy.decide(text)
                self.assertEqual(result.decision, TurnStartDecision.START)
                self.assertTrue(result.reason.startswith("explicit_interrupt"))

    def test_question_like_text_starts_turn(self) -> None:
        result = self.strategy.decide("那明天天气怎么样")
        self.assertEqual(result.decision, TurnStartDecision.START)
        self.assertEqual(result.reason, "question_like")

    def test_short_ambiguous_partial_is_candidate(self) -> None:
        result = self.strategy.decide("那个", is_final=False)
        self.assertEqual(result.decision, TurnStartDecision.CANDIDATE)

    def test_normalize_removes_punctuation(self) -> None:
        self.assertEqual(normalize_utterance(" 嗯， "), "嗯")


class InterruptClassifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = RuleBasedInterruptClassifier()

    def test_non_speaking_never_interrupts(self) -> None:
        result = self.classifier.classify(is_speaking=False, text="停一下")
        self.assertEqual(result.decision, InterruptDecision.IGNORE)
        self.assertFalse(result.should_cancel)

    def test_backchannel_does_not_interrupt_while_speaking(self) -> None:
        result = self.classifier.classify(is_speaking=True, text="嗯")
        self.assertEqual(result.decision, InterruptDecision.IGNORE)
        self.assertFalse(result.should_cancel)

    def test_explicit_interrupt_hard_cancels(self) -> None:
        result = self.classifier.classify(is_speaking=True, text="停一下，改成一句话")
        self.assertEqual(result.decision, InterruptDecision.HARD_INTERRUPT)
        self.assertTrue(result.should_cancel)

    def test_correction_hard_cancels(self) -> None:
        result = self.classifier.classify(is_speaking=True, text="不是，我问的是天气")
        self.assertEqual(result.decision, InterruptDecision.HARD_INTERRUPT)
        self.assertTrue(result.should_cancel)

    def test_short_partial_is_soft_interrupt(self) -> None:
        result = self.classifier.classify(is_speaking=True, text="那个", is_final=False)
        self.assertEqual(result.decision, InterruptDecision.SOFT_INTERRUPT)
        self.assertFalse(result.should_cancel)

    def test_wake_word_hard_cancels(self) -> None:
        result = self.classifier.classify(is_speaking=True, text="", wake_word=True)
        self.assertEqual(result.decision, InterruptDecision.HARD_INTERRUPT)
        self.assertTrue(result.should_cancel)


if __name__ == "__main__":
    unittest.main()

