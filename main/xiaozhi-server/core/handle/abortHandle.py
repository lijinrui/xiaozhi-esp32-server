import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    turn_id = conn.cancel_current_turn("client_abort")
    conn.close_after_chat = False
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps(
            {
                "type": "tts",
                "state": "stop",
                "session_id": conn.session_id,
                **({"turn_id": turn_id} if turn_id is not None else {}),
            }
        )
    )
    conn.mark_tts_stop_sent(turn_id)
    conn.commit_spoken_history(turn_id)
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
