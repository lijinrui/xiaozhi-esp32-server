import re
from dataclasses import dataclass


@dataclass
class InterruptDecision:
    action: str
    reason: str


class InterruptClassifier:
    HARD_PHRASES = (
        "停一下",
        "等等",
        "等一下",
        "别说了",
        "暂停",
        "不是",
        "不对",
        "我说的是",
    )
    IGNORE_PHRASES = {"嗯", "好", "对", "哦", "可以", "行", "好的", "嗯嗯"}

    def classify(
        self,
        playback_state=None,
        vad_event=None,
        asr_partial=None,
        asr_final=None,
        wake_word=None,
        current_turn_id=None,
    ):
        text = self._normalize(asr_final or asr_partial or "")
        if not current_turn_id or playback_state not in ("speaking", "playing", "tts"):
            return InterruptDecision("ignore", "not_playing")
        if wake_word:
            return InterruptDecision("hard_interrupt", "wake_word")
        if not text:
            return InterruptDecision("ignore", "empty")
        if self._is_backchannel(text):
            return InterruptDecision("ignore", "backchannel")
        if any(phrase in text for phrase in self.HARD_PHRASES):
            return InterruptDecision("hard_interrupt", "explicit_stop_phrase")
        if asr_final and self._looks_like_new_question(text):
            return InterruptDecision("hard_interrupt", "new_question")
        return InterruptDecision("soft_interrupt", "await_final")

    def _is_backchannel(self, text):
        return text in self.IGNORE_PHRASES or (len(text) <= 2 and text in self.IGNORE_PHRASES)

    @staticmethod
    def _looks_like_new_question(text):
        return bool(re.search(r"(吗|呢|怎么|什么|为什么|换一个|另一个|天气|几点|多少)", text))

    @staticmethod
    def _normalize(text):
        return re.sub(r"[\s，。！？!?；;、,.]", "", text or "")

