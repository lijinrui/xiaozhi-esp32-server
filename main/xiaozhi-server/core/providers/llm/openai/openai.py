import httpx
import openai
from openai.types import CompletionUsage
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase
from urllib.parse import urlparse

TAG = __name__
logger = setup_logging()

# 需要禁用思考模式的平台域名及其对应参数（默认关闭思考模式）
THINKING_DISABLED_DOMAINS = {
    "aliyuncs.com": {"enable_thinking": False},
    "bigmodel.cn": {"thinking": {"type": "disabled"}},
    "moonshot.cn": {"thinking": {"type": "disabled"}},
    "volces.com": {"thinking": {"type": "disabled"}},
}


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        
        timeout_config = config.get("timeout")
        if isinstance(timeout_config, dict):
            # 细粒度超时配置
            custom_timeout = httpx.Timeout(
                pool=timeout_config.get("pool", 2.0),
                connect=timeout_config.get("connect", 3.0),
                write=timeout_config.get("write", 5.0),
                read=timeout_config.get("read", 60.0)
            )
        elif isinstance(timeout_config, (int, float)) and timeout_config > 0:
            # 兼容旧的单一超时配置（整数或浮点数）
            custom_timeout = httpx.Timeout(timeout_config)
        else:
            # 未配置或配置无效，使用默认值
            custom_timeout = httpx.Timeout(300)

        param_defaults = {
            "max_tokens": int,
            "temperature": lambda x: round(float(x), 1),
            "top_p": lambda x: round(float(x), 1),
            "frequency_penalty": lambda x: round(float(x), 1),
        }

        for param, converter in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else None,
                )
            except (ValueError, TypeError):
                setattr(self, param, None)

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        # 单轮模式：开启后只发送 system + few-shot + 本轮消息给上游
        # 适用于上游自己维护会话上下文的场景（如 openclaw 等）
        self.single_turn = bool(config.get("single_turn", False))

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=custom_timeout)

    @staticmethod
    def normalize_dialogue(dialogue):
        """自动修复 dialogue 中缺失 content 的消息"""
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    @staticmethod
    def _split_fewshot_and_history(messages):
        """切分 dialogue 为 (systems, fewshot, history)。

        Dialogue.get_llm_dialogue_with_memory 的拼装顺序：
          system(static) → few-shot(可选) → system(dynamic, 可选) → 真实历史
        优先用第二条 system 作为分界；缺失时用 fewshot_ id 前缀兜底。
        """
        system_idx = [i for i, m in enumerate(messages) if m.get("role") == "system"]
        systems = [messages[i] for i in system_idx]

        if len(system_idx) >= 2:
            sys2 = system_idx[1]
            fewshot = [m for m in messages[:sys2] if m.get("role") != "system"]
            history = messages[sys2 + 1:]
            return systems, fewshot, history

        fewshot_end = -1
        for i, m in enumerate(messages):
            role = m.get("role")
            if role == "tool":
                tcid = m.get("tool_call_id") or ""
                if str(tcid).startswith("fewshot_"):
                    fewshot_end = i
            elif role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tcid and str(tcid).startswith("fewshot_"):
                        fewshot_end = i
                        break
        if fewshot_end >= 0 and fewshot_end + 1 < len(messages):
            nxt = messages[fewshot_end + 1]
            if nxt.get("role") == "assistant" and nxt.get("content") and not nxt.get("tool_calls"):
                fewshot_end += 1

        if fewshot_end < 0:
            history = [m for m in messages if m.get("role") != "system"]
            return systems, [], history

        start = (system_idx[0] + 1) if system_idx else 0
        fewshot = messages[start:fewshot_end + 1]
        history = messages[fewshot_end + 1:]
        return systems, fewshot, history

    def _trim_to_single_turn(self, messages):
        """单轮裁剪：保留 system + few-shot + 本轮消息（含 in-flight tool 链）。"""
        systems, fewshot, history = self._split_fewshot_and_history(messages)

        # 在 history 内倒序找最后一条"真实"user 消息（跳过 MAX_DEPTH 兜底的 [系统提示]）
        turn_start = None
        for i in range(len(history) - 1, -1, -1):
            m = history[i]
            if m.get("role") != "user":
                continue
            content = m.get("content", "") or ""
            if isinstance(content, str) and content.startswith("[系统提示]"):
                continue
            turn_start = i
            break

        current_turn = history if turn_start is None else history[turn_start:]

        result = []
        if systems:
            result.append(systems[0])
        result.extend(fewshot)
        if len(systems) > 1:
            result.extend(systems[1:])
        result.extend(current_turn)
        logger.bind(tag=TAG).debug(
            f"single_turn 裁剪: {len(messages)} -> {len(result)} 条 "
            f"(system={len(systems)}, fewshot={len(fewshot)}, current_turn={len(current_turn)})"
        )
        return result

    def _apply_thinking_disabled(self, request_params: dict):
        """根据域名自动禁用思考模式"""
        parsed_url = urlparse(self.base_url)
        domain = parsed_url.netloc
        for disabled_domain, params in THINKING_DISABLED_DOMAINS.items():
            if disabled_domain in domain:
                request_params.setdefault("extra_body", {}).update(params)
                logger.bind(tag=TAG).info(f"为域名 {domain} 禁用思考模式，参数: {params}")
                break

    def response(self, session_id, dialogue, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)
        if self.single_turn:
            dialogue = self._trim_to_single_turn(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "user": "xiaozhi-fixed-session",
        }

        # 添加可选参数,只有当参数不为None时才添加
        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)

        responses = self.client.chat.completions.create(**request_params)

        is_active = True
        try:            
            for chunk in responses:
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                    content = getattr(delta, "content", "") if delta else ""
                except IndexError:
                    content = ""
                if content:
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        yield content
        finally:
            responses.close()

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)
        if self.single_turn:
            dialogue = self._trim_to_single_turn(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "tools": functions,
            "user": "xiaozhi-fixed-session",
        }

        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)

        stream = self.client.chat.completions.create(**request_params)

        try:
            for chunk in stream:
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", "")
                    tool_calls = getattr(delta, "tool_calls", None)
                    yield content, tool_calls
                elif isinstance(getattr(chunk, "usage", None), CompletionUsage):
                    usage_info = getattr(chunk, "usage", None)
                    logger.bind(tag=TAG).info(
                        f"Token 消耗：输入 {getattr(usage_info, 'prompt_tokens', '未知')}，"
                        f"输出 {getattr(usage_info, 'completion_tokens', '未知')}，"
                        f"共计 {getattr(usage_info, 'total_tokens', '未知')}"
                    )
        finally:
            stream.close()
