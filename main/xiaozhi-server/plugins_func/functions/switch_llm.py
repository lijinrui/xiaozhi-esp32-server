from typing import TYPE_CHECKING

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from core.utils import llm as llm_utils
from core.utils.dialogue import Message

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


switch_llm_function_desc = {
    "type": "function",
    "function": {
        "name": "switch_llm",
        "description": (
            "当用户希望切换底层大模型时调用，例如'切换到豆包'、'换成 deepseek'、"
            "'用通义千问'、'切回智谱'。仅切换主对话模型，不影响意图识别和记忆。"
            "**不**用于切换录音模式或角色（这两个有专门的 enter_recording_mode "
            "和 change_role 函数）。必须能从用户话里识别到具体模型名（豆包/"
            "deepseek/通义/智谱等）才触发；只说'切换/换一个'没有指向具体模型时不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": (
                        "用户口述的模型名，可以是中文别名（豆包、通义、深度求索）"
                        "或配置 key（DoubaoLLM）。"
                    ),
                },
            },
            "required": ["model_name"],
        },
    },
}


def _normalize(name: str) -> str:
    return "".join(name.split()).lower()


def _split_aliases(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        parts = raw.replace("、", ",").replace(";", ",").split(",")
        return [p.strip() for p in parts if p.strip()]
    return []


def _build_alias_index(llm_config: dict) -> dict:
    """{normalized_alias: module_key}；module key 自身与各 alias 都纳入索引。"""
    index = {}
    for module_key, block in llm_config.items():
        if not isinstance(block, dict):
            continue
        candidates = [module_key]
        candidates.extend(_split_aliases(block.get("aliases")))
        for cand in candidates:
            norm = _normalize(cand)
            if norm and norm not in index:
                index[norm] = module_key
    return index


@register_function("switch_llm", switch_llm_function_desc, ToolType.SYSTEM_CTL)
def switch_llm(conn: "ConnectionHandler", model_name: str):
    if not model_name or not str(model_name).strip():
        return ActionResponse(
            action=Action.RESPONSE,
            result="缺少模型名",
            response="想切到哪个模型呀？",
        )

    llm_config = conn.config.get("LLM", {}) or {}
    alias_index = _build_alias_index(llm_config)
    target_key = alias_index.get(_normalize(model_name))

    if not target_key:
        return ActionResponse(
            action=Action.RESPONSE,
            result=f"未找到模型 {model_name}",
            response=f"没找到 {model_name} 这个模型哦。",
        )

    current_key = conn.config.get("selected_module", {}).get("LLM")
    if target_key == current_key:
        return ActionResponse(
            action=Action.RESPONSE,
            result=f"已是 {target_key}",
            response=f"已经在用 {target_key} 啦。",
        )

    block = llm_config.get(target_key) or {}
    llm_type = target_key if "type" not in block else block["type"]

    try:
        new_llm = llm_utils.create_instance(llm_type, block)
    except Exception as e:
        logger.bind(tag=TAG).error(f"切换 LLM 失败 {target_key}: {e}")
        return ActionResponse(
            action=Action.RESPONSE,
            result=f"切换失败: {e}",
            response=f"切换 {target_key} 失败了，等下再试试。",
        )

    conn.llm = new_llm
    conn.config.setdefault("selected_module", {})["LLM"] = target_key
    conn.dialogue.put(
        Message(
            role="user",
            content=f"[系统提示] 已切换到 {target_key} 模型，请你以新模型身份继续对话。",
        )
    )
    logger.bind(tag=TAG).info(f"已切换主 LLM: {current_key} -> {target_key}")

    return ActionResponse(
        action=Action.RESPONSE,
        result=f"已切换到 {target_key}",
        response=f"好的，已经切换到 {target_key} 啦。",
    )
