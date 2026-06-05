import unittest

from core.voice.barge_in_config import (
    BARGE_IN_MODE_NORMAL,
    BARGE_IN_MODE_OFF,
    BARGE_IN_MODE_SENSITIVE,
    resolve_barge_in_config,
)


class BargeInConfigTest(unittest.TestCase):
    def test_off_mode_disables_interrupt(self):
        mode, should_interrupt, use_classifier, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "off"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_OFF)
        self.assertFalse(should_interrupt)
        self.assertTrue(use_classifier)
        self.assertFalse(soft_as_hard)

    def test_normal_mode_keeps_soft_interrupt_non_canceling(self):
        mode, should_interrupt, use_classifier, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "normal"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_NORMAL)
        self.assertTrue(should_interrupt)
        self.assertTrue(use_classifier)
        self.assertFalse(soft_as_hard)

    def test_sensitive_mode_treats_soft_as_hard(self):
        mode, should_interrupt, use_classifier, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "sensitive"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_SENSITIVE)
        self.assertTrue(should_interrupt)
        self.assertTrue(use_classifier)
        self.assertTrue(soft_as_hard)

    def test_legacy_fields_are_honored_when_mode_is_absent(self):
        mode, should_interrupt, use_classifier, soft_as_hard = resolve_barge_in_config(
            {
                "interrupt_tts_on_segment": False,
                "enable_interrupt_classifier": False,
            }
        )
        self.assertEqual(mode, BARGE_IN_MODE_NORMAL)
        self.assertFalse(should_interrupt)
        self.assertFalse(use_classifier)
        self.assertFalse(soft_as_hard)

    def test_unknown_mode_falls_back_to_normal(self):
        mode, should_interrupt, use_classifier, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "very-fast"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_NORMAL)
        self.assertTrue(should_interrupt)
        self.assertTrue(use_classifier)
        self.assertFalse(soft_as_hard)


if __name__ == "__main__":
    unittest.main()
