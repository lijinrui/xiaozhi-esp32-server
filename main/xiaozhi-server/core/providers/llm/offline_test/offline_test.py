import time


class LLMProvider:
    """Deterministic streaming LLM for offline cancel-path tests."""

    def __init__(self, config):
        self.delay_sec = float(config.get("delay_sec", 0.08))

    def response(self, session_id, dialogue):
        text = "我是砚舟，一个可以被打断的桌面机器人。现在我会慢慢说，方便测试播放中插话。"
        for chunk in self._chunks(text):
            time.sleep(self.delay_sec)
            yield chunk

    def response_with_functions(self, session_id, dialogue, functions=None):
        for chunk in self.response(session_id, dialogue):
            yield chunk, None

    @staticmethod
    def _chunks(text):
        for index in range(0, len(text), 4):
            yield text[index : index + 4]
