import os
import json
import time
from datetime import datetime
from typing import TYPE_CHECKING

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

RECORDING_ROOT = "data/recordings"
RECORDING_VAD_TIMEOUT_SECONDS = 1800

enter_recording_mode_desc = {
    "type": "function",
    "function": {
        "name": "enter_recording_mode",
        "description": (
            "当用户希望把设备变成'常驻拾音器/录音模式/会议记录/只听不说'时调用。"
            "进入后设备只做 ASR + 说话人识别并持久化，不再生成 LLM 回复或 TTS 播报。"
            "典型触发：'切换到录音模式'、'开始录音'、'开会议记录'、'只听别说话'等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "用户进入录音模式的目的（可选），例如'会议记录'",
                }
            },
            "required": [],
        },
    },
}

exit_recording_mode_desc = {
    "type": "function",
    "function": {
        "name": "exit_recording_mode",
        "description": (
            "当用户希望结束'录音模式/拾音器/会议记录'回到正常对话时调用。"
            "典型触发：'退出录音模式'、'结束录音'、'停止记录'、'恢复对话'等。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _device_dir(conn: "ConnectionHandler") -> str:
    device_id = (conn.headers or {}).get("device-id") or "unknown"
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in device_id)
    path = os.path.join(RECORDING_ROOT, safe)
    os.makedirs(path, exist_ok=True)
    return path


def _open_session(conn: "ConnectionHandler", reason: str | None) -> dict:
    start_dt = datetime.now()
    filename = start_dt.strftime("%Y%m%d_%H%M%S") + ".jsonl"
    file_path = os.path.join(_device_dir(conn), filename)
    handle = open(file_path, "a", encoding="utf-8")
    meta = {
        "_meta": "session_start",
        "ts": start_dt.isoformat(timespec="seconds"),
        "device_id": (conn.headers or {}).get("device-id"),
        "reason": reason,
    }
    handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
    handle.flush()
    return {
        "file_path": file_path,
        "file_handle": handle,
        "start_time": start_dt,
    }


def append_recording(conn: "ConnectionHandler", text: str) -> None:
    """追加一条 ASR 结果到当前录音 session。供 receiveAudioHandle 调用。"""
    session = getattr(conn, "recording_session", None)
    if not session:
        return
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "speaker": getattr(conn, "current_speaker", None),
        "content": text,
    }
    try:
        session["file_handle"].write(json.dumps(record, ensure_ascii=False) + "\n")
        session["file_handle"].flush()
    except Exception as e:
        logger.bind(tag=TAG).error(f"写录音失败: {e}")


def close_recording_session(conn: "ConnectionHandler", reason: str = "manual") -> None:
    """关闭录音 session，断连/退出/异常时调用，幂等。"""
    session = getattr(conn, "recording_session", None)
    if not session:
        return
    try:
        end = {
            "_meta": "session_end",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
        }
        session["file_handle"].write(json.dumps(end, ensure_ascii=False) + "\n")
        session["file_handle"].flush()
        session["file_handle"].close()
    except Exception as e:
        logger.bind(tag=TAG).error(f"关闭录音文件失败: {e}")
    finally:
        conn.recording_session = None


@register_function("enter_recording_mode", enter_recording_mode_desc, ToolType.SYSTEM_CTL)
def enter_recording_mode(conn: "ConnectionHandler", reason: str | None = None):
    if getattr(conn, "recording_session", None):
        return ActionResponse(
            action=Action.RESPONSE,
            result="录音模式已在进行中",
            response="已经在录音模式啦。",
        )
    try:
        conn.recording_session = _open_session(conn, reason)
        logger.bind(tag=TAG).info(
            f"进入录音模式: file={conn.recording_session['file_path']} reason={reason}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result="已进入录音模式",
            response="好的，已切到录音模式，我只听不说啦。说'退出录音模式'就能恢复。",
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"进入录音模式失败: {e}")
        return ActionResponse(
            action=Action.RESPONSE,
            result=f"进入录音模式失败: {e}",
            response="抱歉，进入录音模式失败了。",
        )


@register_function("exit_recording_mode", exit_recording_mode_desc, ToolType.SYSTEM_CTL)
def exit_recording_mode(conn: "ConnectionHandler"):
    if not getattr(conn, "recording_session", None):
        return ActionResponse(
            action=Action.RESPONSE,
            result="当前不在录音模式",
            response="当前不在录音模式哦。",
        )
    file_path = conn.recording_session.get("file_path")
    close_recording_session(conn, reason="user_exit")
    logger.bind(tag=TAG).info(f"退出录音模式: file={file_path}")
    return ActionResponse(
        action=Action.RESPONSE,
        result="已退出录音模式",
        response="已退出录音模式，咱们继续聊。",
    )
