from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 从 recording_mode 复用进入逻辑，避免循环导入
from plugins_func.functions.recording_mode import enter_recording_mode

retreat_mode_desc = {
    "type": "function",
    "function": {
        "name": "retreat_mode",
        "description": (
            "当用户说'退下'、'你退下'、'小智退下'、'退下吧'、'先退下'等指令时调用。"
            "进入只听不说的录音/拾音模式，设备不再生成 LLM 回复或 TTS 播报。"
            "用户再次说唤醒词时会自动恢复对话。"
            "典型触发：'退下'、'你退下'、'小智退下'、'先退下吧'、'退下等我叫你'等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "用户指令的原文（可选），用于日志记录",
                }
            },
            "required": [],
        },
    },
}


@register_function("retreat_mode", retreat_mode_desc, ToolType.SYSTEM_CTL)
def retreat_mode(conn: "ConnectionHandler", reason: str | None = None):
    """处理'退下'指令：进入录音模式，播放简短回应。"""
    try:
        # 先进入录音模式（复用现有逻辑）
        result = enter_recording_mode(conn, reason=reason or "用户退下指令")
        if result.action == Action.NOTFOUND:
            # 已经在录音模式
            return ActionResponse(
                action=Action.RESPONSE,
                result="已在录音模式",
                response="我已经在只听不说的状态啦，有事叫我哦。",
            )

        # 返回简短回应后进入录音模式
        return ActionResponse(
            action=Action.RESPONSE,
            result="已进入录音模式",
            response="好的，我先退下了，有事叫我哦。",
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"处理退下指令失败: {e}")
        return ActionResponse(
            action=Action.RESPONSE,
            result="退下指令处理失败",
            response="抱歉，处理退下指令时出错了。",
        )
