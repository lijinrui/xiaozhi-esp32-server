"""Internal voice pipeline frames.

These frames are server-internal only. They do not change the ESP32 websocket
protocol; they give turn/interruption decisions a typed lifecycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def monotonic_ts() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class VoiceFrame:
    session_id: str
    turn_id: str | None = None
    timestamp: float = field(default_factory=monotonic_ts)


@dataclass(frozen=True)
class UserStartedSpeakingFrame(VoiceFrame):
    source: str = "unknown"
    text: str | None = None


@dataclass(frozen=True)
class InterruptCandidateFrame(VoiceFrame):
    source: str = "unknown"
    text: str | None = None
    playback_state: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InterruptionFrame(VoiceFrame):
    old_turn_id: str | None = None
    reason: str = "unknown"
    mode: str = "hard"


@dataclass(frozen=True)
class TurnCancelFrame(VoiceFrame):
    cancelled_turn_id: str | None = None
    reason: str = "unknown"

