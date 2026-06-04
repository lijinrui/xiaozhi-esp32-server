import re
import time


class TTSChunker:
    def __init__(self, first_min_chars=8, first_max_chars=20, max_wait_ms=250, max_chars=60):
        self.first_min_chars = first_min_chars
        self.first_max_chars = first_max_chars
        self.max_wait_ms = max_wait_ms
        self.max_chars = max_chars
        self._buffer = ""
        self._started_at = None
        self._cancelled = False
        self._emitted_any = False

    def push(self, text, now=None):
        if self._cancelled or not text:
            return []
        now = time.monotonic() if now is None else now
        if self._started_at is None:
            self._started_at = now
        self._buffer += text
        return self._drain(now, force=False)

    def flush(self):
        if self._cancelled:
            return []
        return self._drain(time.monotonic(), force=True)

    def cancel(self):
        pending = 1 if self._buffer else 0
        self._buffer = ""
        self._cancelled = True
        return pending

    def _drain(self, now, force):
        chunks = []
        while self._buffer:
            chunk = self._next_chunk(now, force)
            if not chunk:
                break
            chunks.append(chunk)
            self._buffer = self._buffer[len(chunk):]
            self._buffer = self._buffer.lstrip()
            self._emitted_any = True
            self._started_at = now if self._buffer else None
        return chunks

    def _next_chunk(self, now, force):
        text = self._buffer
        if force:
            return text.strip()

        if not self._emitted_any:
            waited_ms = (now - self._started_at) * 1000 if self._started_at is not None else 0
            punctuation = self._find_punctuation(text, limit=self.first_max_chars)
            if punctuation is not None and punctuation >= self.first_min_chars:
                return text[:punctuation].strip()
            if len(text) >= self.first_max_chars:
                return text[:self.first_max_chars].strip()
            if len(text) >= self.first_min_chars and waited_ms >= self.max_wait_ms:
                return text.strip()
            return None

        punctuation = self._find_punctuation(text, limit=self.max_chars)
        if punctuation is not None:
            return text[:punctuation].strip()
        if len(text) >= self.max_chars:
            return text[:self.max_chars].strip()
        return None

    @staticmethod
    def _find_punctuation(text, limit):
        match = re.search(r"[。！？!?；;\n，、,]", text[:limit])
        if not match:
            return None
        return match.end()
