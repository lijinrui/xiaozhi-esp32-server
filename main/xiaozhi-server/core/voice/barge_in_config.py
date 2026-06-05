"""播放中插话打断模式的配置解析工具。"""

BARGE_IN_MODE_OFF = "off"
BARGE_IN_MODE_NORMAL = "normal"
BARGE_IN_MODE_SENSITIVE = "sensitive"
BARGE_IN_MODES = {BARGE_IN_MODE_OFF, BARGE_IN_MODE_NORMAL, BARGE_IN_MODE_SENSITIVE}


def resolve_barge_in_config(asr_config):
    """返回 (模式, 是否允许打断播放, 是否把软打断当作硬打断)。

    barge_in_mode 是唯一的播放中插话开关：
      off: ASR 文本不打断当前播放。
      normal: 只有明确插话才打断当前播放。
      sensitive: 明确插话和疑似插话都会打断当前播放。
    """
    mode = str(asr_config.get("barge_in_mode", BARGE_IN_MODE_NORMAL) or "").strip().lower()
    if mode not in BARGE_IN_MODES:
        mode = BARGE_IN_MODE_NORMAL
    return (
        mode,
        mode != BARGE_IN_MODE_OFF,
        mode == BARGE_IN_MODE_SENSITIVE,
    )
