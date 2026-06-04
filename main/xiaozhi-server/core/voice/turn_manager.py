import time
from collections import OrderedDict


class TurnManager:
    def __init__(self, session_id, enabled=True, max_sentence_bindings=20):
        self.session_id = session_id
        self.enabled = enabled
        self.max_sentence_bindings = max_sentence_bindings
        self.turn_seq = 0
        self.current_turn_id = None
        self.cancelled_turn_ids = set()
        self.metrics = {}
        self.sentence_turn_map = OrderedDict()
        self.active_tool_futures = {}

    def begin_turn(self, query=None, source="chat"):
        if not self.enabled:
            return None
        self.turn_seq += 1
        turn_id = f"{self.session_id}:{self.turn_seq}"
        now = time.monotonic()
        self.current_turn_id = turn_id
        self.metrics[turn_id] = {
            "turn_id": turn_id,
            "session_id": self.session_id,
            "query": query,
            "source": source,
            "started_at": now,
            "llm_first_token_at": None,
            "tts_first_audio_at": None,
            "abort_received_at": None,
            "tts_stop_sent_at": None,
            "cancel_reason": None,
            "stale_audio_packets": 0,
            "dropped_llm_tokens": 0,
            "dropped_tts_chunks": 0,
            "dropped_tool_results": 0,
            "spoken_commit_chars": 0,
            "spoken_text": "",
            "history_truncated": False,
            "interrupt_decision": None,
            "interrupt_reason": None,
        }
        return turn_id

    def cancel_turn(self, turn_id=None, reason="abort", interrupt_decision=None, interrupt_reason=None):
        if not self.enabled:
            return None, []
        turn_id = turn_id or self.current_turn_id
        futures = []
        if turn_id:
            self.cancelled_turn_ids.add(turn_id)
            metrics = self.metrics.setdefault(turn_id, {"turn_id": turn_id})
            metrics["abort_received_at"] = time.monotonic()
            metrics["cancel_reason"] = reason
            if interrupt_decision is not None:
                metrics["interrupt_decision"] = interrupt_decision
            if interrupt_reason is not None:
                metrics["interrupt_reason"] = interrupt_reason
            futures = self.active_tool_futures.pop(turn_id, [])
            if self.current_turn_id == turn_id:
                self.current_turn_id = None
        return turn_id, futures

    def is_current(self, turn_id):
        if not self.enabled:
            return True
        if turn_id is None:
            return True
        return turn_id == self.current_turn_id and turn_id not in self.cancelled_turn_ids

    def bind_sentence(self, sentence_id, turn_id):
        if not self.enabled or not sentence_id or not turn_id:
            return
        self.sentence_turn_map[sentence_id] = turn_id
        self.sentence_turn_map.move_to_end(sentence_id)
        while len(self.sentence_turn_map) > self.max_sentence_bindings:
            self.sentence_turn_map.popitem(last=False)

    def get_sentence_turn_id(self, sentence_id):
        return self.sentence_turn_map.get(sentence_id)

    def bind_tool_future(self, turn_id, future):
        if not self.enabled or turn_id is None or future is None:
            return
        self.active_tool_futures.setdefault(turn_id, []).append(future)

    def unbind_tool_future(self, turn_id, future):
        if not self.enabled or turn_id is None or future is None:
            return
        futures = self.active_tool_futures.get(turn_id)
        if not futures:
            return
        try:
            futures.remove(future)
        except ValueError:
            return
        if not futures:
            self.active_tool_futures.pop(turn_id, None)

    def mark_first_token(self, turn_id):
        metrics = self._get_metrics(turn_id)
        if metrics and metrics.get("llm_first_token_at") is None:
            metrics["llm_first_token_at"] = time.monotonic()

    def mark_first_audio(self, turn_id):
        metrics = self._get_metrics(turn_id)
        if metrics and metrics.get("tts_first_audio_at") is None:
            metrics["tts_first_audio_at"] = time.monotonic()

    def mark_tts_stop_sent(self, turn_id):
        metrics = self._get_metrics(turn_id)
        if metrics:
            metrics["tts_stop_sent_at"] = time.monotonic()

    def mark_llm_token_dropped(self, turn_id, count=1):
        self._increment(turn_id, "dropped_llm_tokens", count)

    def mark_tts_chunk_dropped(self, turn_id, count=1):
        self._increment(turn_id, "dropped_tts_chunks", count)

    def mark_audio_packet_dropped(self, turn_id, count=1):
        self._increment(turn_id, "stale_audio_packets", count)

    def mark_tool_result_dropped(self, turn_id, count=1):
        self._increment(turn_id, "dropped_tool_results", count)

    def mark_tts_chunk_sent(self, turn_id, text=None, estimated_audio_ms=None):
        if not text:
            return
        metrics = self._get_metrics(turn_id)
        if not metrics:
            return
        current = metrics.get("spoken_text") or ""
        metrics["spoken_text"] = f"{current}{text}"
        metrics["spoken_commit_chars"] = len(metrics["spoken_text"])
        if estimated_audio_ms is not None:
            metrics["last_estimated_audio_ms"] = estimated_audio_ms

    def get_spoken_commit(self, turn_id):
        metrics = self.metrics.get(turn_id) if self.enabled else None
        if not metrics:
            return ""
        return metrics.get("spoken_text") or ""

    def mark_history_truncated(self, turn_id):
        metrics = self._get_metrics(turn_id)
        if metrics:
            metrics["history_truncated"] = True

    def get_turn_metrics(self, turn_id):
        return self.metrics.get(turn_id)

    def _get_metrics(self, turn_id):
        if not self.enabled or turn_id is None:
            return None
        return self.metrics.setdefault(turn_id, {"turn_id": turn_id})

    def _increment(self, turn_id, field, count):
        metrics = self._get_metrics(turn_id)
        if metrics:
            metrics[field] = int(metrics.get(field) or 0) + int(count)


def delta_ms(start, end):
    if start is None or end is None:
        return None
    return round((end - start) * 1000, 1)

