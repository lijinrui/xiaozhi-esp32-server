import unittest

from core.utils.dialogue import Dialogue, Message
from core.voice.turn_manager import TurnManager


class SpokenCommitHistoryTest(unittest.TestCase):
    def test_interrupted_turn_commits_only_spoken_text(self):
        dialogue = Dialogue()
        manager = TurnManager("session")
        turn_id = manager.begin_turn("question")
        manager.mark_tts_chunk_sent(turn_id, "第一步打开设置。")
        manager.cancel_turn(turn_id, reason="hard_interrupt")

        spoken = manager.get_spoken_commit(turn_id)
        if spoken:
            dialogue.put(Message(role="assistant", content=spoken))
            manager.mark_history_truncated(turn_id)

        self.assertEqual(dialogue.dialogue[-1].content, "第一步打开设置。")
        self.assertNotIn("第二步进入网络", dialogue.dialogue[-1].content)
        self.assertTrue(manager.get_turn_metrics(turn_id)["history_truncated"])


if __name__ == "__main__":
    unittest.main()

