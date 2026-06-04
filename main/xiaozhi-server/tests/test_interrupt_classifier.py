import unittest

from core.voice.interrupt_classifier import InterruptClassifier


class InterruptClassifierTest(unittest.TestCase):
    def setUp(self):
        self.classifier = InterruptClassifier()

    def test_hard_interrupt_phrases(self):
        for text in ("停一下", "等等", "不是", "不对", "我说的是另一个"):
            decision = self.classifier.classify(
                playback_state="speaking",
                asr_final=text,
                current_turn_id="turn",
            )
            self.assertEqual(decision.action, "hard_interrupt")

    def test_backchannel_is_ignored(self):
        for text in ("嗯", "好", "对", "哦", "可以", "行"):
            decision = self.classifier.classify(
                playback_state="speaking",
                asr_partial=text,
                current_turn_id="turn",
            )
            self.assertEqual(decision.action, "ignore")

    def test_soft_interrupt_waits_for_final(self):
        decision = self.classifier.classify(
            playback_state="speaking",
            asr_partial="那个",
            current_turn_id="turn",
        )
        self.assertEqual(decision.action, "soft_interrupt")

    def test_new_question_is_hard_on_final(self):
        decision = self.classifier.classify(
            playback_state="speaking",
            asr_final="那天气呢",
            current_turn_id="turn",
        )
        self.assertEqual(decision.action, "hard_interrupt")


if __name__ == "__main__":
    unittest.main()

