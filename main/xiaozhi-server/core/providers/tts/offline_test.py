import time

from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import InterfaceType, SentenceType


class TTSProvider(TTSProviderBase):
    """Deterministic TTS for offline cancel-path tests.

    It emits valid Opus silence packets directly, avoiding external TTS services,
    ffmpeg, and real audio files while still exercising the server audio egress
    queue and websocket binary send path.
    """

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.frame_delay_sec = float(config.get("frame_delay_sec", 0.02))
        self.frames_per_segment = int(config.get("frames_per_segment", 12))

    async def text_to_speak(self, text, output_file):
        return None

    def to_tts_stream(self, text, opus_handler=None):
        sentence_id = getattr(self, "current_sentence_id", None)
        self.queue_audio(SentenceType.FIRST, None, text, sentence_id)
        silence = b"\xF8\xFF\xFE"
        for _ in range(self.frames_per_segment):
            if self.conn.client_abort or not self.conn.is_current_sentence(sentence_id):
                break
            time.sleep(self.frame_delay_sec)
            if opus_handler:
                opus_handler(silence)
