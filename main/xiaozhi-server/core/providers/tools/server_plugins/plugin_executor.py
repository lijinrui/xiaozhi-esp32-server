"""服务端插件工具执行器"""

from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import all_function_registry, Action, ActionResponse


class ServerPluginExecutor(ToolExecutor):
    """服务端插件工具执行器"""

    def __init__(self, conn: "ConnectionHandler"):
        self.conn = conn
        self.config = conn.config

    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行服务端插件工具"""
        func_item = all_function_registry.get(tool_name)
        if not func_item:
            return ActionResponse(
                action=Action.NOTFOUND, response=f"插件函数 {tool_name} 不存在"
            )

        try:
            # 根据工具类型决定如何调用
            if hasattr(func_item, "type"):
                func_type = func_item.type
                if func_type.code in [4, 5]:  # SYSTEM_CTL, IOT_CTL (需要conn参数)
                    result = func_item.func(conn, **arguments)
                elif func_type.code == 2:  # WAIT
                    result = func_item.func(**arguments)
                elif func_type.code == 3:  # CHANGE_SYS_PROMPT
                    result = func_item.func(conn, **arguments)
                else:
                    result = func_item.func(**arguments)
            else:
                # 默认不传conn参数
                result = func_item.func(**arguments)

            return result

        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                response=str(e),
            )

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有注册的服务端插件工具"""
        tools = {}

        # 获取必要的函数
        necessary_functions = [
            "handle_exit_intent",
            "get_lunar",
            "enter_recording_mode",
            "exit_recording_mode",
            "switch_llm",
        ]

        # 获取配置中的函数
        config_functions = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ].get("functions", [])

        # 转换为列表
        if not isinstance(config_functions, list):
            try:
                config_functions = list(config_functions)
            except TypeError:
                config_functions = []

        # 合并所有需要的函数
        all_required_functions = list(set(necessary_functions + config_functions))

        for func_name in all_required_functions:
            func_item = all_function_registry.get(func_name)
            if func_item:
                # 从函数注册中获取描述
                fun_description = (
                    self.config.get("plugins", {})
                    .get(func_name, {})
                    .get("description", "")
                )
                if fun_description is not None and len(fun_description) > 0:
                    if "function" in func_item.description and isinstance(
                        func_item.description["function"], dict
                    ):
                        func_item.description["function"][
                            "description"
                        ] = fun_description

                # 新闻插件：根据配置更新新闻源参数描述
                if func_name == "get_news_from_newsnow":
                    self._init_news_source_description(func_item, func_name)

                if func_name == "switch_llm":
                    self._init_switch_llm_description(func_item)

                if func_name in ("hass_get_state", "hass_set_state"):
                    self._init_hass_entity_id_enum(func_item)

                tools[func_name] = ToolDefinition(
                    name=func_name,
                    description=func_item.description,
                    tool_type=ToolType.SERVER_PLUGIN,
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的服务端插件工具"""
        return tool_name in all_function_registry

    def _init_news_source_description(self, func_item, func_name):
        """根据连接配置初始化新闻工具的参数描述"""
        news_sources = (
            self.config.get("plugins", {})
            .get(func_name, {})
            .get("news_sources", "")
        )
        if not news_sources:
            news_sources = "澎湃新闻;百度热搜;财联社"
        sources_str = news_sources.replace(";", "、")
        try:
            func_item.description["function"]["parameters"]["properties"]["source"][
                "description"
            ] = f"新闻源的标准中文名称，例如{sources_str}等。可选参数，如果不提供则使用默认新闻源"
        except (KeyError, TypeError):
            pass

    def _init_switch_llm_description(self, func_item):
        """根据当前 LLM 配置补充可切换模型和别名，让 LLM 直接从配置 key 中选择最接近的。"""
        try:
            from plugins_func.functions.switch_llm import (
                get_switch_llm_model_options,
                _get_custom_llm_keys,
                _filter_llm_config,
            )

            llm_config = self.config.get("LLM", {}) or {}
            custom_keys = _get_custom_llm_keys()
            if custom_keys:
                llm_config = _filter_llm_config(llm_config, custom_keys)

            options = get_switch_llm_model_options(llm_config)
            enum_keys = [k for k, v in llm_config.items() if isinstance(v, dict)]

            choice_instruction = (
                f"\n\n【重要】用户说的模型名可能是语音识别（ASR）的同音字误识别。"
                f"你必须从以下列表中选择语义或发音最接近用户说法的配置 key 返回，"
                f"不要原样返回用户口述的不在列表中的名称。"
                f"例如用户说'延周'应匹配'OpenClawLLM'，用户说'海罗'应匹配'MiniMaxLLM'。\n"
                f"可选模型及别名：{options}"
            )

            func_item.description["function"]["description"] += choice_instruction
            func_item.description["function"]["parameters"]["properties"]["model_name"][
                "description"
            ] += choice_instruction

            # 加 enum 硬约束，强制 LLM 只能从配置 key 里选（主流 function calling 均支持）
            if enum_keys:
                func_item.description["function"]["parameters"]["properties"]["model_name"][
                    "enum"
                ] = enum_keys
        except Exception:
            pass

    def _init_hass_entity_id_enum(self, func_item):
        """从 home_assistant 配置中提取 entity_id 列表，注入到 entity_id 参数的 enum 中，防止模型瞎猜。"""
        try:
            plugins = self.config.get("plugins", {})
            ha_cfg = plugins.get("home_assistant") or plugins.get("hass_get_state")
            if not ha_cfg:
                return
            devices = ha_cfg.get("devices", [])
            if not devices:
                return
            entity_ids = []
            for device in devices:
                parts = str(device).split(",")
                if len(parts) >= 3:
                    entity_ids.append(parts[2].strip())
            if not entity_ids:
                return
            # 注入 enum
            params = func_item.description["function"]["parameters"]["properties"]
            if "entity_id" in params:
                params["entity_id"]["enum"] = entity_ids
                params["entity_id"][
                    "description"
                ] += f"\n只能从以下列表中选择，禁止自创：{', '.join(entity_ids)}"
        except Exception:
            pass
