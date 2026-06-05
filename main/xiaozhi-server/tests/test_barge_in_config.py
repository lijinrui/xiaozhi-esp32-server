import unittest

from core.voice.barge_in_config import (
    BARGE_IN_MODE_NORMAL,
    BARGE_IN_MODE_OFF,
    BARGE_IN_MODE_SENSITIVE,
    resolve_barge_in_config,
)


class BargeInConfigTest(unittest.TestCase):
    def test_off_mode_disables_interrupt(self):
        mode, should_interrupt, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "off"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_OFF)
        self.assertFalse(should_interrupt)
        self.assertFalse(soft_as_hard)

    def test_normal_mode_keeps_soft_interrupt_non_canceling(self):
        mode, should_interrupt, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "normal"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_NORMAL)
        self.assertTrue(should_interrupt)
        self.assertFalse(soft_as_hard)

    def test_sensitive_mode_treats_soft_as_hard(self):
        mode, should_interrupt, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "sensitive"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_SENSITIVE)
        self.assertTrue(should_interrupt)
        self.assertTrue(soft_as_hard)

    def test_mode_defaults_to_normal(self):
        mode, should_interrupt, soft_as_hard = resolve_barge_in_config({})
        self.assertEqual(mode, BARGE_IN_MODE_NORMAL)
        self.assertTrue(should_interrupt)
        self.assertFalse(soft_as_hard)

    def test_unknown_mode_falls_back_to_normal(self):
        mode, should_interrupt, soft_as_hard = resolve_barge_in_config(
            {"barge_in_mode": "very-fast"}
        )
        self.assertEqual(mode, BARGE_IN_MODE_NORMAL)
        self.assertTrue(should_interrupt)
        self.assertFalse(soft_as_hard)


if __name__ == "__main__":
    unittest.main()
