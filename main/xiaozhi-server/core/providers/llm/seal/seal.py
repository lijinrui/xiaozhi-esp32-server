#!/usr/bin/env python3
"""
Seal Gateway WebSocket LLM Provider for XiaoZhi ESP32 Server

配置示例 (config.yaml):
    LLM:
      SealLLM:
        type: seal
        url: ws://10.40.52.197:18789
        token: your_token_here
        session_key: agent:main:main
        agent_id: main
"""

import json
import uuid
import websocket
from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    """Seal Gateway WebSocket LLM Provider"""
    
    def __init__(self, config):
        self.url = config.get("url", "ws://10.40.52.197:18789")
        self.token = config.get("token", "")
        self.session_key = config.get("session_key", "agent:main:main")
        self.agent_id = config.get("agent_id", "main")
        
        if not self.token:
            logger.bind(tag=TAG).error("Seal LLM: token is required")
        
        logger.bind(tag=TAG).info(f"Seal LLM initialized: {self.url}")
    
    def _connect(self):
        """建立 WebSocket 连接并完成认证"""
        ws = None
        try:
            # 1. 连接 WebSocket
            ws = websocket.create_connection(self.url, timeout=10)

            # 2. 接收 challenge
            raw_challenge = ws.recv()
            try:
                challenge = json.loads(raw_challenge)
            except json.JSONDecodeError as e:
                raise ConnectionError(f"Invalid challenge response: {raw_challenge[:200]}")
            logger.bind(tag=TAG).debug(f"Received challenge: {challenge.get('event')}")

            # 3. 发送 connect 认证
            connect_msg = {
                "type": "req",
                "id": str(uuid.uuid4()),
                "method": "connect",
                "params": {
                    "minProtocol": 3,
                    "maxProtocol": 3,
                    "client": {
                        "id": "cli",
                        "version": "1.0.0",
                        "platform": "linux",
                        "mode": "cli"
                    },
                    "role": "operator",
                    "scopes": ["operator.read", "operator.write"],
                    "auth": {"token": self.token}
                }
            }
            ws.send(json.dumps(connect_msg))

            # 4. 等待认证响应
            raw_resp = ws.recv()
            try:
                resp = json.loads(raw_resp)
            except json.JSONDecodeError as e:
                raise ConnectionError(f"Invalid auth response: {raw_resp[:200]}")

            if not resp.get("ok"):
                error = resp.get("error", "Unknown error")
                logger.bind(tag=TAG).error(f"Seal connect failed: {error}")
                raise ConnectionError(f"Seal authentication failed: {error}")

            logger.bind(tag=TAG).debug("Seal connected successfully")
            return ws

        except Exception as e:
            if ws:
                try:
                    ws.close()
                except:
                    pass
            logger.bind(tag=TAG).error(f"Seal connection error: {e}")
            raise
    
    def _send_message(self, ws, message):
        """
        发送消息到 Seal

        Args:
            ws: WebSocket 连接
            message: 用户当前消息（Seal session 会自动维护上下文）
        """
        msg = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "sessions.send",
            "params": {
                "key": self.session_key,
                "message": message
            }
        }
        ws.send(json.dumps(msg))

        # 等待发送确认
        try:
            raw_resp = ws.recv()
            resp = json.loads(raw_resp)
            if not resp.get("ok"):
                error = resp.get("error", "Unknown error")
                logger.bind(tag=TAG).warning(f"Seal send warning: {error}")
        except json.JSONDecodeError as e:
            logger.bind(tag=TAG).warning(f"Invalid send response: {raw_resp[:200]}")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"Error receiving send confirmation: {e}")
    
    def response(self, session_id, dialogue, **kwargs):
        """
        流式响应生成器

        Args:
            session_id: 会话ID
            dialogue: 对话历史（只提取最后一条用户消息发送，Seal session 自动维护上下文）

        Yields:
            str: 生成的文本片段
        """
        ws = None
        try:
            # 建立连接
            ws = self._connect()

            # 提取最后一条用户消息
            if not dialogue:
                logger.bind(tag=TAG).warning("Empty dialogue")
                return

            last_message = dialogue[-1]
            if last_message.get("role") != "user":
                logger.bind(tag=TAG).warning("Last message is not from user")
                return

            user_message = last_message.get("content", "")
            if not user_message:
                logger.bind(tag=TAG).warning("Empty user message")
                return

            # 发送消息（Seal 的 session 会自动维护上下文）
            self._send_message(ws, user_message)

            # 接收流式回复
            ws.settimeout(60)  # 60秒超时
            full_text = ""

            while True:
                try:
                    raw_data = ws.recv()
                    try:
                        data = json.loads(raw_data)
                    except json.JSONDecodeError as e:
                        logger.bind(tag=TAG).error(f"Invalid JSON received: {e}, data: {raw_data[:200]}")
                        break

                    event = data.get("event")

                    # 跳过心跳
                    if event in ["tick", "health"]:
                        continue

                    # 处理 agent 事件（流式回复）
                    if event == "agent":
                        payload = data.get("payload", {})
                        stream = payload.get("stream", "")

                        if stream == "assistant":
                            text = payload.get("data", {}).get("text", "")
                            if text and text != full_text:
                                # 只返回新增的部分
                                new_text = text[len(full_text):]
                                full_text = text
                                if new_text:
                                    yield new_text

                        elif stream == "done":
                            logger.bind(tag=TAG).debug("Stream completed")
                            break

                    # 处理 chat 事件（备选）
                    elif event == "chat":
                        payload = data.get("payload", {})
                        chat_msg = payload.get("message", {})

                        if chat_msg.get("role") == "assistant":
                            content = chat_msg.get("content", [])
                            for item in content:
                                if item.get("type") == "text":
                                    text = item.get("text", "")
                                    if text and text != full_text:
                                        new_text = text[len(full_text):]
                                        full_text = text
                                        if new_text:
                                            yield new_text

                        if payload.get("state") == "completed":
                            break

                    # 处理错误
                    elif data.get("type") == "res" and "error" in data:
                        error = data["error"]
                        logger.bind(tag=TAG).error(f"Seal error: {error}")
                        yield f"[Error: {error.get('message', 'Unknown error')}]"
                        break

                except websocket.WebSocketTimeoutException:
                    logger.bind(tag=TAG).warning("Seal response timeout")
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Seal response error: {e}")
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(f"Seal LLM error: {e}")
            yield f"[Error: {str(e)}]"

        finally:
            if ws:
                try:
                    ws.close()
                except Exception as e:
                    logger.bind(tag=TAG).debug(f"Error closing websocket: {e}")
    
    def response_no_stream(self, system_prompt, user_prompt, **kwargs):
        """非流式响应（将流式结果合并）"""
        dialogue = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        result = ""
        for part in self.response("", dialogue, **kwargs):
            result += part
        return result
