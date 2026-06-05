import unittest

from core.voice.tts_chunker import TTSChunker


class TTSChunkerTest(unittest.TestCase):
    def test_first_chunk_waits_for_min_chars_or_timeout(self):
        chunker = TTSChunker(first_min_chars=4, first_max_chars=8, max_wait_ms=200)
        self.assertEqual(chunker.push("好", now=0.0), [])
        self.assertEqual(chunker.push("的", now=0.1), [])
        self.assertEqual(chunker.push("我来", now=0.21), ["好的我来"])

    def test_long_sentence_is_split(self):
        chunker = TTSChunker(first_min_chars=4, first_max_chars=8, max_chars=10)
        chunks = chunker.push("你可以先打开设置然后找到网络选项接着选择你的WiFi。", now=0.0)
        chunks += chunker.flush()

        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks[:-1]))

    def test_cancel_drops_pending(self):
        chunker = TTSChunker(first_min_chars=8)
        self.assertEqual(chunker.push("短句", now=0.0), [])
        self.assertEqual(chunker.cancel(), 1)
        self.assertEqual(chunker.flush(), [])


if __name__ == "__main__":
    unittest.main()
