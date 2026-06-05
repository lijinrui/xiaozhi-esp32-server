import unittest

from core.providers.tts.base import TTSProviderBase
from core.utils.tts import MarkdownCleaner


class DummyTTSProvider(TTSProviderBase):
    async def text_to_speak(self, text, output_file):
        return b""


class TTSTextCleaningTest(unittest.TestCase):
    def test_markdown_cleaner_removes_loose_emotion_tag(self):
        text = MarkdownCleaner.clean_markdown("emotion:thinking]]小智的全双工语音能力")

        self.assertEqual(text, "小智的全双工语音能力")

    def test_prepare_tts_texts_cleans_display_but_keeps_correct_words_for_tts_only(self):
        tts = DummyTTSProvider(
            {
                "correct_words": ["小智|小志"],
            },
            delete_audio_file=True,
        )

        tts_text, display_text = tts._prepare_tts_texts(
            "[[emotion:happy]]小智的全双工语音能力"
        )

        self.assertEqual(display_text, "小智的全双工语音能力")
        self.assertEqual(tts_text, "小志的全双工语音能力")


if __name__ == "__main__":
    unittest.main()
