import unittest

from core.connection import ConnectionHandler


class AdaptiveToolsTest(unittest.TestCase):
    def make_conn(self):
        conn = ConnectionHandler.__new__(ConnectionHandler)
        conn.config = {
            "selected_module": {"LLM": "MLXQwen3LLM"},
            "LLM": {
                "MLXQwen3LLM": {
                    "function_call": {
                        "adaptive_tools": True,
                        "tool_trigger_keywords": ["音量"],
                    }
                },
                "MiniMaxLLM": {"aliases": ["海螺"]},
            },
            "plugins": {
                "home_assistant": {
                    "devices": [
                        "客厅,灯带,light.ke_ting_deng_dai",
                        "卧室,空调,climate.wo_shi_kong_tiao",
                    ]
                }
            },
        }
        return conn

    def test_plain_chat_skips_tools(self):
        conn = self.make_conn()
        self.assertFalse(conn._should_use_function_tools("介绍一下小智", depth=0))

    def test_explicit_tool_phrases_use_tools(self):
        conn = self.make_conn()
        for text in (
            "把音量调高",
            "切换模型到海螺",
            "打开客厅灯带",
            "卧室空调关掉",
            "杭州天气怎么样",
            "今天有什么新闻",
            "帮我查一下 mlx prompt cache",
            "明天的黄历宜忌是什么",
        ):
            with self.subTest(text=text):
                self.assertTrue(conn._should_use_function_tools(text, depth=0))

    def test_recursive_calls_keep_tools(self):
        conn = self.make_conn()
        self.assertTrue(conn._should_use_function_tools(None, depth=1))

    def test_llm_inject_fewshot_override_wins(self):
        conn = self.make_conn()
        conn.config["LLM"]["MLXQwen3LLM"]["function_call"]["inject_fewshot"] = False
        conn.config["Intent"] = {"function_call": {"inject_fewshot": True}}

        self.assertFalse(conn._should_inject_tool_call_fewshot())

    def test_global_inject_fewshot_by_model_name(self):
        conn = self.make_conn()
        conn.config["LLM"]["MLXQwen3LLM"]["model_name"] = "qwen3-30b-a3b-2507"
        conn.config["Intent"] = {
            "function_call": {
                "inject_fewshot": True,
                "inject_fewshot_by_llm": {"qwen3-30b-a3b-2507": False},
            }
        }

        self.assertFalse(conn._should_inject_tool_call_fewshot())

    def test_inject_fewshot_defaults_to_enabled(self):
        conn = self.make_conn()

        self.assertTrue(conn._should_inject_tool_call_fewshot())


if __name__ == "__main__":
    unittest.main()
