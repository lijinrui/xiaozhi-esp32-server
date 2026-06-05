import unittest
from concurrent.futures import Future

from core.voice.turn_manager import TurnManager


class TurnManagerTest(unittest.TestCase):
    def test_cancel_marks_metrics_and_cancels_futures(self):
        manager = TurnManager("session")
        turn_id = manager.begin_turn("hello", source="test")
        future = Future()
        manager.bind_tool_future(turn_id, future)

        cancelled_turn_id, futures = manager.cancel_turn(turn_id, reason="hard_interrupt")

        self.assertEqual(cancelled_turn_id, turn_id)
        self.assertEqual(futures, [future])
        self.assertFalse(manager.is_current(turn_id))
        metrics = manager.get_turn_metrics(turn_id)
        self.assertEqual(metrics["cancel_reason"], "hard_interrupt")

    def test_drop_counters_and_spoken_commit(self):
        manager = TurnManager("session")
        turn_id = manager.begin_turn("hello")
        manager.mark_llm_token_dropped(turn_id, 2)
        manager.mark_tts_chunk_dropped(turn_id)
        manager.mark_audio_packet_dropped(turn_id, 3)
        manager.mark_tool_result_dropped(turn_id)
        manager.mark_tts_chunk_sent(turn_id, "第一步。")

        metrics = manager.get_turn_metrics(turn_id)
        self.assertEqual(metrics["dropped_llm_tokens"], 2)
        self.assertEqual(metrics["dropped_tts_chunks"], 1)
        self.assertEqual(metrics["stale_audio_packets"], 3)
        self.assertEqual(metrics["dropped_tool_results"], 1)
        self.assertEqual(manager.get_spoken_commit(turn_id), "第一步。")

    def test_tool_timing_markers(self):
        manager = TurnManager("session")
        turn_id = manager.begin_turn("明天黄历")

        manager.mark_tool_choice(turn_id, ["get_lunar"])
        manager.mark_tool_choice(turn_id, ["get_lunar", "hass_get_state"])
        manager.mark_tool_call_started(turn_id)
        manager.mark_tool_result(turn_id)
        manager.mark_tool_result_llm_started(turn_id)
        manager.mark_tool_result_llm_first_delta(turn_id)
        manager.mark_tool_result_llm_first_token(turn_id)

        metrics = manager.get_turn_metrics(turn_id)
        self.assertEqual(metrics["tool_names"], ["get_lunar", "hass_get_state"])
        self.assertIsNotNone(metrics["tool_choice_at"])
        self.assertIsNotNone(metrics["tool_call_started_at"])
        self.assertIsNotNone(metrics["tool_result_at"])
        self.assertIsNotNone(metrics["tool_result_llm_started_at"])
        self.assertIsNotNone(metrics["tool_result_llm_first_delta_at"])
        self.assertIsNotNone(metrics["tool_result_llm_first_token_at"])

    def test_disabled_manager_is_transparent(self):
        manager = TurnManager("session", enabled=False)
        self.assertIsNone(manager.begin_turn("hello"))
        self.assertTrue(manager.is_current("anything"))


if __name__ == "__main__":
    unittest.main()
