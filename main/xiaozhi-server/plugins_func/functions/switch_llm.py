from __future__ import annotations

from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from config.config_loader import get_project_dir, read_config
from core.utils import llm as llm_utils
from core.utils.dialogue import Message

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 模块加载时检测 pypinyin 可用性，缺失则给出明确安装提示
try:
    from pypinyin import lazy_pinyin

    _PINYIN_AVAILABLE = True
except Exception:
    _PINYIN_AVAILABLE = False
    logger.bind(tag=TAG).warning(
        "pypinyin 未安装，switch_llm 的拼音同音字匹配将不可用。"
        "请执行: pip install pypinyin==0.53.0"
    )


def _to_pinyin(text: str) -> str:
    """将中文文本转为无分隔符的拼音小写字符串，用于处理同音字 ASR 误识别。
    未安装 pypinyin 或转换失败时返回原字符串。
    """
    if not _PINYIN_AVAILABLE:
        return text
    try:
        return "".join(lazy_pinyin(text))
    except Exception:
        return text


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
    text = "".join(str(name).split()).lower()
    replacements = {
        "模型": "",
        "大模型": "",
        "llm": "",
        "三十": "30",
        "三": "3",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    for char in "-_/.:@":
        text = text.replace(char, "")
    return text


def _split_aliases(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        parts = raw.replace("、", ",").replace(";", ",").split(",")
        return [p.strip() for p in parts if p.strip()]
    return []


def _get_custom_llm_keys() -> set:
    try:
        custom_config = read_config(get_project_dir() + "data/.config.yaml")
    except Exception:
        return set()
    llm_config = custom_config.get("LLM", {}) if isinstance(custom_config, dict) else {}
    if not isinstance(llm_config, dict):
        return set()
    return {key for key, value in llm_config.items() if isinstance(value, dict)}


def _filter_llm_config(llm_config: dict, allowed_keys: set | None) -> dict:
    if not allowed_keys:
        return llm_config
    return {key: value for key, value in llm_config.items() if key in allowed_keys}


def _build_alias_entries(llm_config: dict) -> list:
    """[(normalized_alias, raw_alias, module_key)]；module key、model_name 和 aliases 都纳入匹配。
    如果候选词包含中文，会额外生成一条拼音 normalized 的 entry，用于处理同音字 ASR 误识别。
    """
    entries = []
    for module_key, block in llm_config.items():
        if not isinstance(block, dict):
            continue
        candidates = [module_key, block.get("model_name", "")]
        candidates.extend(_split_aliases(block.get("aliases")))
        for cand in candidates:
            norm = _normalize(cand)
            if norm:
                entries.append((norm, str(cand), module_key))
                pinyin = _to_pinyin(norm)
                if pinyin != norm:
                    entries.append((pinyin, str(cand), module_key))
    return entries


def _try_match(query: str, entries: list) -> str | None:
    """在 entries 中尝试匹配 query，返回 module_key 或 None。"""
    # 1. 精确匹配：配置 key / model_name / aliases。
    exact_keys = sorted({mk for norm, raw, mk in entries if query == norm})
    if len(exact_keys) == 1:
        return exact_keys[0]
    if len(exact_keys) > 1:
        return None

    # 2. 包含匹配：支持“切到海螺模型”“用千问30b”等自然说法。
    candidates = [
        (len(norm), raw, mk)
        for norm, raw, mk in entries
        if len(norm) >= 2 and (norm in query or query in norm)
    ]
    if candidates:
        return max(candidates)[2]

    # 3. 轻量模糊匹配：兜底处理少量 ASR 误识别或中英混写。
    scored = [
        (SequenceMatcher(None, query, norm).ratio(), len(norm), raw, mk)
        for norm, raw, mk in entries
        if len(norm) >= 3
    ]
    scored = [x for x in scored if x[0] >= 0.72]
    if scored:
        return max(scored)[3]

    return None


def _match_model_name(model_name: str, llm_config: dict, preferred_keys: set | None = None):
    query = _normalize(model_name)
    if not query:
        return None

    if preferred_keys:
        preferred_config = _filter_llm_config(llm_config, preferred_keys)
        preferred_match = _match_model_name(model_name, preferred_config, None)
        if preferred_match:
            return preferred_match

    entries = _build_alias_entries(llm_config)

    # 先用原始文本匹配（汉字、英文、数字等）
    result = _try_match(query, entries)
    if result:
        return result

    # 拼音兜底：处理同音字 ASR 误识别，如“延周”-> yanzhou 匹配 alias “砚舟”
    query_pinyin = _to_pinyin(query)
    if query_pinyin and query_pinyin != query:
        result = _try_match(query_pinyin, entries)
        if result:
            return result

    return None


def get_switch_llm_model_options(llm_config: dict) -> str:
    custom_keys = _get_custom_llm_keys()
    if custom_keys:
        llm_config = _filter_llm_config(llm_config, custom_keys)

    options = []
    for module_key, block in llm_config.items():
        if not isinstance(block, dict):
            continue
        aliases = _split_aliases(block.get("aliases"))
        model_name = block.get("model_name")
        labels = aliases[:]
        if model_name:
            labels.append(str(model_name))
        if labels:
            options.append(f"{module_key}({', '.join(labels)})")
        else:
            options.append(module_key)
    return "；".join(options)


@register_function("switch_llm", switch_llm_function_desc, ToolType.SYSTEM_CTL)
def switch_llm(conn: "ConnectionHandler", model_name: str):
    if not model_name or not str(model_name).strip():
        return ActionResponse(
            action=Action.RESPONSE,
            result="缺少模型名",
            response="想切到哪个模型呀？",
        )

    llm_config = conn.config.get("LLM", {}) or {}
    target_key = _match_model_name(model_name, llm_config, _get_custom_llm_keys())

    if not target_key:
        options = get_switch_llm_model_options(llm_config)
        return ActionResponse(
            action=Action.RESPONSE,
            result=f"未找到模型 {model_name}",
            response=f"没找到 {model_name} 这个模型哦。可切换的是：{options}",
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
