# 2026-06-06 全双工中断 E2E 记录

## 本轮目标

使用真实服务栈和真实语音流式输入验证全双工中断：

- 客户端通过 websocket 连接真实 xiaozhi server。
- 首轮用户语音和播放中插话都使用 wav 转 Opus 后按 60ms 一帧发送。
- 服务端真实经过 ASR、LLM、function_call/tools、TTS。
- 除首轮 warmup 外，语音输入结束到 LLM 首 delta 应小于 1s。

## 本轮修改

1. 补充 adaptive tools 触发词
   - 文件：`main/xiaozhi-server/core/connection.py`
   - 原因：普通聊天跳过 tools 可以降低 MLX 延迟，但“天气/新闻/搜索/黄历/宜忌”等自然说法如果不触发 tools，会导致本该调用工具的请求漏掉。
   - 处理：补充天气、新闻、搜索、阴历、黄历、宜忌、节气等触发词；保留 LLM 别名和 HA 设备名动态触发。
   - 验证：`tests.test_adaptive_tools` 通过。

2. 增加真实工具查询中断 case
   - 文件：`main/xiaozhi-server/performance_tester/performance_tester_full_duplex_client.py`
   - 新 case：`lunar_tool_query`
   - 语音：`main/xiaozhi-server/tmp/barge_in_audio/lunar_tool_query.wav`
   - 场景：小智正在长回答时，用户插话“明天的黄历宜忌是什么”。
   - 目的：验证不仅普通聊天能中断，触发 function_call 的工具链也能在真实语音 E2E 里跑通。
   - 备注：黄历 case 主要用于验证已有 `get_lunar` 工具链路，不应作为产品常用工具代表；后续又补测了更贴近日常场景的天气工具链路。

3. 给 LLM 增加稳定日期上下文
   - 文件：`main/xiaozhi-server/data/.config.yaml`
   - 文件：`main/xiaozhi-server/core/utils/dialogue.py`
   - 原因：真实工具查询中，LLM 曾把“明天”错误解析为 `2024-05-18`。这是线上真实风险，不是测试脚本问题。
   - 处理：在 prompt 末尾增加 `<context>`，只提供一天内稳定的 `today_date` / `today_weekday` / `lunar_date`，避免分钟级 `current_time` 影响 prompt cache。
   - 验证：重跑后 `get_lunar` 参数变为 `{'date': '2026-06-07', 'query': '宜忌'}`。

4. 修复 TTS 字幕/日志中的 emotion 残片
   - 文件：`main/xiaozhi-server/core/providers/tts/base.py`
   - 文件：`main/xiaozhi-server/tests/test_tts_text_cleaning.py`
   - 原因：系统已有 emotion 处理和 MarkdownCleaner。实际 TTS 合成前已经清理了 `emotion:thinking]]`，但 TTS 基类排队给 `sentence_start`、日志和上报时仍使用原始分段文本，导致字幕/日志出现残片。
   - 处理：TTS 基类统一生成 `tts_text` 和 `display_text`；两者都先走 Markdown/emotion 清理，`correct_words` 仍只影响 `tts_text`，不改变字幕原词。
   - 验证：重启服务后真实 E2E 日志中 `发送第一段语音: 小智的全双工语音能力`，不再是 `emotion:thinking]]小智的全双工语音能力`。

## 验证命令

```bash
venv/bin/python -m py_compile core/utils/dialogue.py core/connection.py performance_tester/performance_tester_full_duplex_client.py
venv/bin/python -m unittest tests.test_adaptive_tools tests.test_turn_manager tests.test_server_plugin_schema
venv/bin/python -m py_compile core/providers/tts/base.py core/connection.py core/utils/tts.py
venv/bin/python -m unittest tests.test_tts_text_cleaning tests.test_adaptive_tools tests.test_turn_manager tests.test_server_plugin_schema
venv/bin/python performance_tester/performance_tester_full_duplex_client.py --case lunar_tool_query --rounds 2
venv/bin/python performance_tester/performance_tester_full_duplex_client.py --rounds 2
```

## 关键结果

完整 E2E：11 个 case x 2 轮，全部 OK。

- 应硬打断：`explicit_stop`、`explicit_wait`、`correction_wrong_target`、`question_weather`、`question_rephrase`、`lunar_tool_query` 全部发送 stop。
- 不应硬打断：`backchannel_um`、`backchannel_ok`、`hesitation_short_partial`、`ordinary_final_pause`、`ordinary_final_environment` 均未发送 stop。
- 所有 stop 后旧音频包数均为 0。
- 第二轮所有可观测 LLM 首 delta 均小于 1s：
  - `explicit_stop`: 534.9ms
  - `explicit_wait`: 557.5ms
  - `correction_wrong_target`: 532.2ms
  - `question_weather`: 560.3ms
  - `question_rephrase`: 587.9ms
  - `lunar_tool_query`: 550.2ms

真实工具链验证：

- `lunar_tool_query` 触发真实 `get_lunar`。
- 修复前工具参数错误：`date=2024-05-18`。
- 修复后工具参数正确：`date=2026-06-07`。
- MLX 日志显示第二轮 prompt 和工具调用均出现 `Prompt processing progress: 1/1`，说明 prompt cache 命中。

2026-06-06 02:49 天气工具链复测：

- 配置变更：在 `data/.config.yaml` 的 `Intent.function_call.functions` 中启用已有 `get_weather` 插件。
- 重启后真实工具列表包含 `get_weather`：
  - `当前支持的函数列表: [..., 'get_weather', ...]`
- 真实语音 E2E 命令：

```bash
venv/bin/python performance_tester/performance_tester_full_duplex_client.py \
  --case question_weather \
  --rounds 2 \
  --after-interrupt-wait-sec 20 \
  --report ../../codex-reports/2026-06-06_weather_tool_e2e_report.html
```

- 第一轮刚重启 MLX，工具 schema prompt cache 冷启动：
  - 主回答首 delta / 可播文本：`1303.1ms / 1303.2ms`
  - 插话天气工具选择首 delta / 首可播文本：`3226.8ms / 4584.7ms`
  - 真实调用：`get_weather {'location': '杭州', 'lang': 'zh_CN'}`
  - 真实 TTS 输出：`明天小雨`、`记得带伞哦`
- 第二轮 prompt cache 命中后达标：
  - 主回答首 delta / 可播文本：`536.5ms / 536.6ms`
  - 插话天气工具选择首 delta / 首可播文本：`750.7ms / 943.5ms`
  - 真实调用：`get_weather`
  - `interrupt_to_stop=1570.1ms`
  - stop 后旧音频包数：`0`
- 结论：天气这个更贴近日常的 function_call 链路在第二轮达成“语音结束到 LLM 首 delta < 1s”，首个可播文本也低于 1s；第一轮慢是重启后 MLX 工具 schema 冷 cache。

2026-06-06 03:05 启用天气工具后的全量复测：

- 配置基线：
  - ASR: `SherpaASRStream`
  - LLM: `MLXQwen3LLM`
  - Intent: `function_call`
  - TTS: `PaddleSpeechTTS`
  - `function_call.functions` 已包含 `get_weather`、`hass_get_state`、`hass_set_state`
- 真实语音全量 E2E 命令：

```bash
venv/bin/python performance_tester/performance_tester_full_duplex_client.py \
  --rounds 2 \
  --after-interrupt-wait-sec 20 \
  --report ../../codex-reports/2026-06-06_weather_enabled_full_duplex_e2e_report.html
```

- 报告：
  - `codex-reports/2026-06-06_weather_enabled_full_duplex_e2e_report.html`
- 结果：11 个 case x 2 轮全部 OK。
  - 硬打断 case 全部发送 stop：`explicit_stop`、`explicit_wait`、`correction_wrong_target`、`question_weather`、`question_rephrase`、`lunar_tool_query`
  - 非硬打断 case 均未 stop：`backchannel_um`、`backchannel_ok`、`hesitation_short_partial`、`ordinary_final_pause`、`ordinary_final_environment`
  - 所有 stop 后旧音频包数均为 `0`
- 第二轮关键耗时：
  - `explicit_stop`: prompt `527.6ms`，插话新 turn `280.2ms`
  - `explicit_wait`: prompt `547.5ms`，插话新 turn `169.0ms`
  - `correction_wrong_target`: prompt `524.8ms`，插话新 turn `181.4ms`
  - `question_weather`: prompt `452.5ms`，天气工具选择 `745.8ms`，首可播文本 `925.6ms`
  - `question_rephrase`: prompt `571.6ms`，插话新 turn `175.5ms`
  - `lunar_tool_query`: prompt `569.1ms`，工具选择 `941.6ms`，首可播文本 `1114.6ms`
- 解释：
  - 对真实工具链，`llm_first_delta_ms` 是“模型首次输出”，通常是 tool choice；`llm_first_token_ms` 是“首个可播文本”，需要等工具执行和工具结果后的二次 LLM。
  - 天气工具第二轮首 delta 和首可播均低于 1s，符合目标。
  - 黄历工具第二轮首 delta 低于 1s，首可播略高于 1s；这是两段 LLM 的真实工具链路成本，不是 prompt cache 未命中。MLX 日志显示最新全量多轮均为 `Prompt processing progress: 1/1`。
- 额外确认：
  - 最近 2500 行服务日志未匹配到 `emotion:`、`thinking]]`、`look_left]]`、`look_right]]`、`look_center]]` 进入 TTS 或发送音频。
  - 全量耗时较长，主要因为非硬打断 case 要等长 TTS 继续播放。后续回归建议拆成“硬中断快回归”和“非中断长播回归”，但本次验收保留了完整真实全量。

2026-06-06 01:40 后复测：

- `explicit_wait --rounds 2` 全部 OK。
  - 第一轮重启后 warmup：LLM 首 delta / 可播文本 1404.8ms。
  - 第二轮 cache 命中：LLM 首 delta / 可播文本 489.7ms。
  - 两轮均 hard interrupt，stop 后旧音频/新音频均为 0。
- `lunar_tool_query --rounds 2` 全部 OK。
  - 第一轮：LLM 首 delta / 可播文本 471.6ms / 471.7ms。
  - 第二轮：LLM 首 delta / 可播文本 524.4ms。
  - 两轮均触发真实 `get_lunar`，参数为 `{'date': '2026-06-07', 'query': '宜忌'}`。
- 最新日志中 TTS 字幕和日志不再出现 emotion 残片：
  - `发送第一段语音: 小智的全双工语音能力`
  - `语音生成成功: 小智的全双工语音能力，重试0次`

## 仍需继续查的问题

1. function_call 耗时要区分两类
   - 之前耗时报告中的 function_call 低于 1s，是“携带 tools schema，但模型直接 content 回复”的路径。
   - 真实工具调用是两次 LLM：第一次选择工具，执行工具后第二次基于工具结果回答；首轮会慢，cache 命中后明显变快。
   - 后续报告里应分别标记“prompt turn LLM 首 delta”和“工具 turn 首次 tool choice / 工具结果后首文本”，避免混在一起误读。

2. 客户端关闭后的音频发送错误日志
   - 现象：E2E 客户端关闭连接后偶发 `audio_play_priority_thread: None received 1000 (OK); then sent 1000 (OK)`。
   - 当前 E2E 指标中 stop 后旧音频/新音频均为 0，不影响本次打断结果。
   - 后续可单独确认是否需要把正常 websocket close 降级为 debug，或在发送前更早识别连接已关闭。

## 2026-06-06 direct_answer / GLM 对照

### 背景

MLX 在 `direct_answer_tool: true`、`adaptive_tools: false` 下，普通长回答会选择 `direct_answer` 虚拟工具。
服务端需要从 tool call 参数 JSON 的 `response` 字段里流式提取可播文本。

实测同一 `explicit_wait` case：

- MLX direct_answer：主问题首有效 delta / 首可播文本约 `3.8s`，即使 MLX 日志显示 `Prompt processing progress: 1/1` 仍然慢。
- GLM 切换后：主问题首有效 delta / 首可播文本为 `1304.2ms / 1304.3ms`、`840.7ms / 840.8ms`、`1131.1ms / 1131.1ms`。
- GLM 插话 turn：`1123.4ms`、`889.8ms`、`959.7ms`。

### 关键结论

GLM 这轮不是 direct_answer 工具链路，而是普通 `content` 流式文本链路：

- 服务端 metrics 中 `tool_names: []`。
- OpenAI provider 请求使用 `tool_choice: "auto"`，因此模型可以在携带 tools 的情况下直接返回 `content`。
- 当前 prompt 也允许两种格式：使用 `direct_answer` 时写 `response/emotion`；不调用工具直接回复时在文本开头加 `[[emotion:xxx]]`。

因此 GLM 结果只能说明“GLM 普通 content 流式回复比 MLX direct_answer 工具参数生成快”，不能证明“GLM direct_answer 工具参数生成也快”。

### 两条链路区别

普通 content 链路：

- LLM 直接流式返回 `content`。
- 服务端收到第一个非空文本即记录 `llm_first_delta_ms` 和 `llm_first_token_ms`。
- 文本直接进入 `TTSChunker`，分段送 TTS。
- emotion 由文本里的 `[[emotion:xxx]]` 或后续 `get_emotion()` 处理。

direct_answer 链路：

- LLM 返回 `tool_calls`，工具名为 `direct_answer`。
- 可播文本在工具参数 JSON 的 `response` 字段里。
- 服务端边收 `arguments` 边解析 `response`，解析到安全文本后才送 TTS。
- `emotion` 来自 direct_answer 参数，和 `response` 分离。
- 这条链路更结构化，但本地 MLX 在生成工具调用参数时延迟明显更高。

### 修复的真实配置问题

发现 `function_call.inject_fewshot: false` 原本没有被 `_inject_tool_call_fewshot()` 使用，导致配置写了 false 仍会注入 direct_answer / 工具调用 few-shot。

修改：

- 文件：`main/xiaozhi-server/core/connection.py`
- 新增 `_should_inject_tool_call_fewshot()`：
  - 当前 LLM 自己的 `function_call.inject_fewshot` 优先级最高。
  - 未配置时看全局 `Intent.function_call.inject_fewshot_by_llm`，支持按 LLM 配置名或 `model_name` 覆盖。
  - 最后回退到全局 `Intent.function_call.inject_fewshot`，默认仍为 true，保持向前兼容。
- `_inject_tool_call_fewshot()` 现在会尊重这个配置。

验证：

```bash
venv/bin/python -m py_compile core/connection.py tests/test_adaptive_tools.py
venv/bin/python -m unittest tests.test_adaptive_tools tests.test_turn_manager tests.test_tts_text_cleaning tests.test_server_plugin_schema
```

结果：13 个相关单测全部通过。

### 配置修复后 GLM 复测

重启主服务，让 `inject_fewshot: false` 生效后，跑 1 轮 `explicit_wait`：

```bash
venv/bin/python performance_tester/performance_tester_full_duplex_client.py \
  --case explicit_wait \
  --rounds 1 \
  --after-interrupt-wait-sec 20 \
  --report ../../codex-reports/2026-06-06_glm_no_fewshot_e2e_report.html
```

结果：

- 主问题：`llm_first_delta_ms = 2837.3ms`，`llm_first_token_ms = 2837.3ms`
- 插话 turn：`llm_first_delta_ms = 682.6ms`，`llm_first_token_ms = 682.6ms`
- `tool_names: []`，仍然是 GLM 普通 content 链路，不是 direct_answer。
- 中断逻辑 OK：`hard_interrupt:explicit_interrupt:等等`，stop 后旧音频包为 0。

结论：

- `inject_fewshot` 配置修复后链路未断。
- GLM 普通 content 链路延迟波动较大，不能作为“非首轮稳定 <1s”的可靠本地低延迟基线。
- 当前最符合目标的低延迟路径仍是 MLX + prompt cache + 普通聊天跳过 tools / 工具意图才带 tools；direct_answer 对 MLX 本地模型明显不适合作为默认低延迟路径。
