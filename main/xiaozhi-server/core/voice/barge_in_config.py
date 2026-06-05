"""Barge-in mode configuration helpers."""

BARGE_IN_MODE_OFF = "off"
BARGE_IN_MODE_NORMAL = "normal"
BARGE_IN_MODE_SENSITIVE = "sensitive"
BARGE_IN_MODES = {BARGE_IN_MODE_OFF, BARGE_IN_MODE_NORMAL, BARGE_IN_MODE_SENSITIVE}


def resolve_barge_in_config(asr_config):
    """Return (mode, should_interrupt, use_classifier, soft_as_hard).

    New configs should use barge_in_mode:
      off: playback is not interrupted by ASR text.
      normal: only clear interruptions cancel playback.
      sensitive: clear and soft interruptions both cancel playback.

    Older interrupt_tts_on_segment / enable_interrupt_classifier configs are
    still honored when barge_in_mode is absent.
    """
    mode = str(asr_config.get("barge_in_mode", "") or "").strip().lower()
    if mode:
        if mode not in BARGE_IN_MODES:
            mode = BARGE_IN_MODE_NORMAL
        return (
            mode,
            mode != BARGE_IN_MODE_OFF,
            True,
            mode == BARGE_IN_MODE_SENSITIVE,
        )

    should_interrupt = asr_config.get("interrupt_tts_on_segment", True)
    use_classifier = asr_config.get("enable_interrupt_classifier", True)
    return BARGE_IN_MODE_NORMAL, should_interrupt, use_classifier, False
