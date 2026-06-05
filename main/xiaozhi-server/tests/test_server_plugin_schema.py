import json
import unittest
from copy import deepcopy

from plugins_func.loadplugins import auto_import_modules
from plugins_func.register import all_function_registry
from core.providers.tools.server_plugins.plugin_executor import ServerPluginExecutor


class DummyConn:
    def __init__(self):
        self.config = {
            "selected_module": {"Intent": "function_call"},
            "Intent": {
                "function_call": {
                    "functions": ["hass_get_state", "hass_set_state"],
                }
            },
            "LLM": {
                "OpenClawLLM": {
                    "aliases": ["砚舟", "openclaw"],
                    "model_name": "openclaw/yanzhou",
                },
                "MLXQwen3LLM": {
                    "aliases": ["mlx", "本地mlx"],
                    "model_name": "default_model",
                },
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


class ServerPluginSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auto_import_modules("plugins_func.functions")

    def test_dynamic_tool_schema_does_not_mutate_registry(self):
        watched = ["switch_llm", "hass_get_state", "hass_set_state"]
        original = {
            name: deepcopy(all_function_registry[name].description)
            for name in watched
        }

        lengths_by_round = []
        for _ in range(5):
            tools = ServerPluginExecutor(DummyConn()).get_tools()
            lengths_by_round.append(
                {
                    name: len(
                        json.dumps(
                            tools[name].description,
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                    for name in watched
                }
            )

        self.assertTrue(all(row == lengths_by_round[0] for row in lengths_by_round))
        for name in watched:
            self.assertEqual(original[name], all_function_registry[name].description)


if __name__ == "__main__":
    unittest.main()
