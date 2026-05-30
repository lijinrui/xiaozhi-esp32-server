from typing import List, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import IntentProviderBase
from plugins_func.functions.play_music import initialize_music_handler
from config.logger import setup_logging
from core.utils.util import get_system_error_response
import re
import json
import hashlib
import time

_EMOTION_TAG_RE = re.compile(r'\[\[emotion:[a-zA-Z_]+\]\]')



TAG = __name__
logger = setup_logging()


class IntentProvider(IntentProviderBase):
    def __init__(self, config):
        super().__init__(config)
        self.llm = None
        self.promot = ""
        # 导入全局缓存管理器
        from core.utils.cache.manager import cache_manager, CacheType

        self.cache_manager = cache_manager
        self.CacheType = CacheType
        self.history_count = config.get("history_count", 1)  # 意图识别历史条数，默认1条足够
        self.temperature = config.get("temperature")  # 意图识别专用温度，默认走LLM配置

    def get_intent_system_prompt(self, functions_list: str) -> str:
        """
        根据配置的意图选项和可用函数动态生成系统提示词
        Args:
            functions: 可用的函数列表，JSON格式字符串
        Returns:
            格式化后的系统提示词
        """

        # 构建函数说明部分
        functions_desc = "可用的函数列表：\n"
        for func in functions_list:
            func_info = func.get("function", {})
            name = func_info.get("name", "")
            desc = func_info.get("description", "")
            params = func_info.get("parameters", {})

            functions_desc += f"\n函数名: {name}\n"
            functions_desc += f"描述: {desc}\n"

            if params:
                functions_desc += "参数:\n"
                for param_name, param_info in params.get("properties", {}).items():
                    param_desc = param_info.get("description", "")
                    param_type = param_info.get("type", "")
                    functions_desc += f"- {param_name} ({param_type}): {param_desc}\n"
                    # 把 enum 可选值也显式写进 prompt，小模型也能看懂
                    enum_values = param_info.get("enum")
                    if enum_values:
                        functions_desc += f"  只能从以下值中选择: {', '.join(str(v) for v in enum_values)}\n"

            functions_desc += "---\n"

        prompt = (
            "【严格格式要求】你必须只能返回JSON格式，绝对不能返回任何自然语言！\n\n"
            "你是一个意图识别助手。请分析用户的最后一句话，判断用户意图并调用相应的函数。\n\n"
            "【重要规则】以下类型的查询请直接返回result_for_context，无需调用函数：\n"
            "- 询问当前时间（如：现在几点、当前时间、查询时间等）\n"
            "- 询问今天日期（如：今天几号、今天星期几、今天是什么日期等）\n"
            "- 询问今天农历（如：今天农历几号、今天什么节气等）\n"
            "- 询问所在城市（如：我现在在哪里、你知道我在哪个城市吗等）"
            "系统会根据上下文信息直接构建回答。\n\n"
            "- 如果用户使用疑问词（如'怎么'、'为什么'、'如何'）询问退出相关的问题（例如'怎么退出了？'），注意这不是让你退出，请返回 {'function_call': {'name': 'continue_chat'}\n"
            "- 仅当用户明确使用'退出系统'、'结束对话'、'我不想和你说话了'等指令时，才触发 handle_exit_intent\n\n"
            f"{functions_desc}\n"
            "处理步骤:\n"
            "1. 分析用户输入，确定用户意图\n"
            "2. 检查是否为上述基础信息查询（时间、日期等），如是则返回result_for_context\n"
            "3. 只能从上面的可用函数列表中选择最匹配的函数，禁止返回列表外的函数名\n"
            "4. 如果找到匹配的函数，生成对应的function_call 格式\n"
            '5. 如果没有找到匹配的函数，返回{"function_call": {"name": "continue_chat"}}\n\n'
            "返回格式要求：\n"
            "1. 必须返回纯JSON格式，不要包含任何其他文字\n"
            "2. 必须包含function_call字段\n"
            "3. function_call必须包含name字段\n"
            "4. 如果函数需要参数，必须包含arguments字段\n\n"
            "示例：\n"
            "```\n"
            "用户: 现在几点了？\n"
            '返回: {"function_call": {"name": "result_for_context"}}\n'
            "```\n"
            "```\n"
            "用户: 当前电池电量是多少？\n"
            '返回: {"function_call": {"name": "get_battery_level", "arguments": {"response_success": "当前电池电量为{value}%", "response_failure": "无法获取Battery的当前电量百分比"}}}\n'
            "```\n"
            "```\n"
            "用户: 当前屏幕亮度是多少？\n"
            '返回: {"function_call": {"name": "self_screen_get_brightness"}}\n'
            "```\n"
            "```\n"
            "用户: 设置屏幕亮度为50%\n"
            '返回: {"function_call": {"name": "self_screen_set_brightness", "arguments": {"brightness": 50}}}\n'
            "```\n"
            "```\n"
            "用户: 我想结束对话\n"
            '返回: {"function_call": {"name": "handle_exit_intent", "arguments": {"say_goodbye": "goodbye"}}}\n'
            "```\n"
            "```\n"
            "用户: 打开客厅的灯\n"
            '返回: {"function_call": {"name": "hass_set_state", "arguments": {"entity_id": "light.ke_ting_deng_dai", "state": {"type": "turn_on"}}}}\n'
            "```\n"
            "```\n"
            "用户: 关闭客厅所有的灯\n"
            '返回: {"function_calls": [{"name": "hass_set_state", "arguments": {"entity_id": "light.ke_ting_deng_dai", "state": {"type": "turn_off"}}}, {"name": "hass_set_state", "arguments": {"entity_id": "light.ke_ting_gui_dao_deng", "state": {"type": "turn_off"}}}, {"name": "hass_set_state", "arguments": {"entity_id": "light.ke_ting_tong_deng", "state": {"type": "turn_off"}}}]}\n'
            "```\n"
            "```\n"
            "用户: 你好啊\n"
            '返回: {"function_call": {"name": "continue_chat"}}\n'
            "```\n\n"
            "注意：\n"
            "1. 只返回JSON格式，不要包含任何其他文字\n"
            '2. 优先检查用户查询是否为基础信息（时间、日期等），如是则返回{"function_call": {"name": "result_for_context"}}，不需要arguments参数\n'
            '3. 如果没有找到匹配的函数，返回{"function_call": {"name": "continue_chat"}}\n'
            "4. 禁止返回未出现在可用函数列表中的函数名，即使用户问题看起来适合某个常见工具\n"
            "5. 确保返回的JSON格式正确，包含所有必要的字段\n"
            "6. result_for_context不需要任何参数，系统会自动从上下文获取信息\n"
            "特殊说明：\n"
            "- 当用户说'所有'、'全部'时，如果该位置有多个同类设备，必须返回 function_calls 数组，每个设备单独调用一次\n"
            "- 当用户单次输入包含多个不同指令时（如'打开灯并且调高音量'），也请返回 function_calls 数组\n"
            "- function_calls 格式示例：{'function_calls': [{...}, {...}]}\n\n"
            "【最终警告】绝对禁止输出任何自然语言、表情符号或解释文字！只能输出有效JSON格式！违反此规则将导致系统错误！"
        )
        return prompt

    def replyResult(self, text: str, original_text: str):
        try:
            llm_result = self.llm.response_no_stream(
                system_prompt=text,
                user_prompt="请根据以上内容，像人类一样说话的口吻回复用户，要求简洁，请直接返回结果。用户现在说："
                + original_text,
            )
            return llm_result
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in generating reply result: {e}")
            return get_system_error_response(self.config)

    async def detect_intent(
        self, conn: "ConnectionHandler", dialogue_history: List[Dict], text: str
    ) -> str:
        if not self.llm:
            raise ValueError("LLM provider not set")
        if conn.func_handler is None:
            return '{"function_call": {"name": "continue_chat"}}'

        # 记录整体开始时间
        total_start_time = time.time()

        # 打印使用的模型信息
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"使用意图识别模型: {model_info}")

        # 计算缓存键
        cache_key = hashlib.md5((conn.device_id + text).encode()).hexdigest()

        # 检查缓存
        cached_intent = self.cache_manager.get(self.CacheType.INTENT, cache_key)
        if cached_intent is not None:
            cache_time = time.time() - total_start_time
            logger.bind(tag=TAG).debug(
                f"使用缓存的意图: {cache_key} -> {cached_intent}, 耗时: {cache_time:.4f}秒"
            )
            return cached_intent

        if self.promot == "":
            functions = conn.func_handler.get_functions()
            if hasattr(conn, "mcp_client"):
                mcp_tools = conn.mcp_client.get_available_tools()
                if mcp_tools is not None and len(mcp_tools) > 0:
                    if functions is None:
                        functions = []
                    functions.extend(mcp_tools)

            self.promot = self.get_intent_system_prompt(functions)

        music_config = initialize_music_handler(conn)
        music_file_names = music_config["music_file_names"]
        prompt_music = f"{self.promot}\n<musicNames>{music_file_names}\n</musicNames>"

        home_assistant_cfg = conn.config["plugins"].get("home_assistant")
        if home_assistant_cfg:
            devices = home_assistant_cfg.get("devices", [])
        else:
            devices = []
        if len(devices) > 0:
            hass_prompt = "\n下面是我家智能设备列表（位置，设备名，entity_id），可以通过homeassistant控制\n"
            for device in devices:
                hass_prompt += device + "\n"
            prompt_music += hass_prompt

        logger.bind(tag=TAG).debug(f"User prompt: {prompt_music}")

        # 构建用户对话历史的提示（过滤掉 emotion 标记，避免干扰意图识别）
        msgStr = ""

        # 获取最近的对话历史
        start_idx = max(0, len(dialogue_history) - self.history_count)
        for i in range(start_idx, len(dialogue_history)):
            clean_content = _EMOTION_TAG_RE.sub('', dialogue_history[i].content)
            msgStr += f"{dialogue_history[i].role}: {clean_content}\n"

        msgStr += f"User: {text}\n"
        user_prompt = f"current dialogue:\n{msgStr}"

        # 记录预处理完成时间
        preprocess_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(f"意图识别预处理耗时: {preprocess_time:.4f}秒")

        # 使用LLM进行意图识别
        llm_start_time = time.time()
        logger.bind(tag=TAG).debug(f"开始LLM意图识别调用, 模型: {model_info}")

        try:
            # 确保提示词是UTF-8编码的字符串
            if isinstance(prompt_music, bytes):
                prompt_music = prompt_music.decode('utf-8')
            if isinstance(user_prompt, bytes):
                user_prompt = user_prompt.decode('utf-8')

            # 如配置了意图识别专用 temperature 则使用，否则走 LLM 默认配置
            llm_kwargs = {}
            if self.temperature is not None:
                llm_kwargs["temperature"] = self.temperature
            intent = self.llm.response_no_stream(
                system_prompt=prompt_music, user_prompt=user_prompt, **llm_kwargs
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in intent detection LLM call: {e}")
            return '{"function_call": {"name": "continue_chat"}}'

        # 记录LLM调用完成时间
        llm_time = time.time() - llm_start_time
        logger.bind(tag=TAG).debug(
            f"外挂的大模型意图识别完成, 模型: {model_info}, 调用耗时: {llm_time:.4f}秒"
        )

        # 记录后处理开始时间
        postprocess_start_time = time.time()

        # 清理和解析响应
        intent = intent.strip()

        # 改进：先尝试按行提取多个 JSON 对象（解决多个 function_call 贪婪匹配粘在一起的问题）
        json_lines = [line.strip() for line in intent.split('\n')
                      if line.strip().startswith('{') and line.strip().endswith('}')]
        function_calls = []
        parsed_intent = None
        for line in json_lines:
            try:
                obj = json.loads(line)
                if "function_call" in obj:
                    function_calls.append(obj["function_call"])
                elif parsed_intent is None:
                    parsed_intent = obj
            except json.JSONDecodeError:
                continue

        if len(function_calls) == 1:
            intent = json.dumps({"function_call": function_calls[0]})
        elif len(function_calls) > 1:
            intent = json.dumps({"function_calls": function_calls})
            logger.bind(tag=TAG).info(f"识别到多个意图调用: {len(function_calls)} 个")
        elif parsed_intent is not None:
            intent = json.dumps(parsed_intent)
        else:
            # 回退到原始贪婪匹配
            match = re.search(r"\{.*\}", intent, re.DOTALL)
            if match:
                intent = match.group(0)

        # 记录总处理时间
        total_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(
            f"【意图识别性能】模型: {model_info}, 总耗时: {total_time:.4f}秒, LLM调用: {llm_time:.4f}秒, 查询: '{text[:20]}...'"
        )

        # 尝试解析为JSON
        try:
            intent_data = json.loads(intent)
            # 处理单个 function_call
            if "function_call" in intent_data:
                function_data = intent_data["function_call"]
                function_name = function_data.get("name")
                function_args = function_data.get("arguments", {})

                if (
                    function_name
                    and function_name not in ("continue_chat", "result_for_context")
                    and not conn.func_handler.has_tool(function_name)
                ):
                    logger.bind(tag=TAG).warning(
                        f"llm 返回了未启用的意图函数: {function_name}，已转为普通对话"
                    )
                    return '{"function_call": {"name": "continue_chat"}}'

                logger.bind(tag=TAG).info(
                    f"llm 识别到意图: {function_name}, 参数: {function_args}"
                )

                if function_name == "result_for_context":
                    logger.bind(tag=TAG).info(
                        "检测到result_for_context意图，将使用上下文信息直接回答"
                    )
                elif function_name == "continue_chat":
                    clean_history = [
                        msg
                        for msg in conn.dialogue.dialogue
                        if msg.role not in ["tool", "function"]
                    ]
                    conn.dialogue.dialogue = clean_history
                else:
                    logger.bind(tag=TAG).info(f"检测到函数调用意图: {function_name}")

            # 处理多个 function_calls
            elif "function_calls" in intent_data:
                names = [fc.get("name") for fc in intent_data["function_calls"]]
                logger.bind(tag=TAG).info(f"检测到多个函数调用意图: {names}")

            # 统一缓存处理和返回
            self.cache_manager.set(self.CacheType.INTENT, cache_key, intent)
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).debug(f"意图后处理耗时: {postprocess_time:.4f}秒")
            return intent
        except json.JSONDecodeError:
            # 后处理时间
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).error(
                f"无法解析意图JSON: {intent}, 后处理耗时: {postprocess_time:.4f}秒"
            )
            # 如果解析失败，默认返回继续聊天意图
            return '{"function_call": {"name": "continue_chat"}}'
