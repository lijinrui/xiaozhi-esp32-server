# Server Offline Barge-in Turn Completion Report

生成时间：2026-06-02 19:50 CST

## 1. 目标

本次分支 `feat/server-offline-barge-in-turns` 只改动 server 侧，目标是让 text-only offline barge-in 可重复验收：

```text
第一轮 listen/detect -> LLM streaming -> TTS/audio output
abort -> 500ms 内 tts stop
旧 turn 的 LLM/TTS/tool/audio 迟到输出被丢弃
第二轮 listen/detect 立即进入新 turn
server/test 输出 turn metrics
```

## 2. 主要变更

### 2.1 Turn 管理与指标

在 `core/connection.py` 增加轻量 turn 状态：

```text
turn_id
current_turn_id
cancelled_turn_ids
sentence_turn_map
turn_metrics
active_tool_futures
```

新增能力：

```text
begin_turn()
cancel_current_turn()
is_current_turn()
is_current_sentence()
mark_llm_first_token()
mark_tts_first_audio()
mark_tts_stop_sent()
mark_stale_audio_packet()
```

日志输出包含：

```text
turn_id
session_id
cancel_reason
llm_first_token_ms
tts_first_audio_ms
abort_to_stop_ms
stale_packets
```

临时验证模式还会向客户端发送 `turn_metrics` JSON，便于测试脚本自动校验。

### 2.2 Abort 路径

`core/handle/abortHandle.py` 改为：

```text
cancel current turn
clear queues
send tts stop with turn_id
record abort_to_stop_ms
clear speaking status
```

### 2.3 LLM/TTS/audio stale output 过滤

`core/handle/receiveAudioHandle.py` 在正常聊天入口先创建 turn，再发送 STT 与提交 chat。

`core/connection.py` 的 chat 流程将 LLM token、direct_answer、tool result、history write、LAST TTS 都绑定 turn，并在旧 turn 时丢弃。

`core/handle/sendAudioHandle.py` 在 TTS 状态消息、音频流控、二进制发送前检查当前 turn，旧 turn 音频会被计入 stale packet 并丢弃。

### 2.4 TTS Provider 兼容

`TTSMessageDTO` 增加可选 `turn_id`。

`TTSProviderBase` 新增通用 helper：

```text
prepare_tts_message()
queue_audio()
wait_tts_future()
```

同时补齐以下重写 `tts_text_priority_thread` 的流式 provider：

```text
alibl_stream
aliyun_stream
xunfei_stream
huoshan_double_stream
minimax_httpstream
index_stream
```

这些 provider 现在会检查 turn 状态，旧 turn 的文本任务不再继续合成；外部 TTS future 也能在 turn cancel 后快速取消。

### 2.5 Tool future cancel

工具调用 future 按 turn 注册。abort 后会取消当前 turn 的 active tool futures。

等待工具结果时改为小步轮询：

```text
wait_turn_future()
```

如果 turn 已取消，立即 cancel future 并退出旧 turn，避免长期占住 chat executor worker。

Reviewer follow-up：`core/handle/intentHandler.py` 的 intent/function-call 旁路也已接入同一套 turn/cancel 体系。intent 命中时会创建 turn，`speak_txt()` 写入 TTS 队列时携带 `turn_id`，单工具与批量工具的 coroutine future 会注册到 `active_tool_futures`，abort 后旧 turn 的 intent 工具结果不会再进入 TTS。

### 2.6 Offline 验证工具

扩展 `codex-tools/offline_barge_in_test/offline_barge_in_test.py`：

```text
等待 sentence_start/audio 后再 abort
校验 abort_to_stop <= 500ms
校验 stale audio packet <= 2
校验 turn1_id / turn2_id 不同
校验 stop_turn_id 匹配第一轮
校验 server turn_metrics 字段
```

新增 `codex-tools/run_temp_server.py`：

```text
在 127.0.0.1:8010/8013 启动临时验证 server
不影响产品 server 的 8000/8003
可启用 deterministic offline-test providers
```

新增 deterministic providers：

```text
core/providers/llm/offline_test/offline_test.py
core/providers/tts/offline_test.py
```

它们用于离线测试，不依赖真实 LLM、真实 TTS、ffmpeg 或硬件。

## 3. 验收结果

使用命令：

```bash
main/xiaozhi-server/venv/bin/python codex-tools/offline_barge_in_test/offline_barge_in_test.py \
  --url ws://127.0.0.1:8010/xiaozhi/v1/ \
  --tts-start-timeout 10 \
  --observe-after-turn2 3
```

通过结果：

```text
turn1 -> sentence_start: 273.3ms
turn1 -> first audio: 293.7ms
abort -> tts stop: 1.2ms
turn2 -> TTS start: 173.6ms
turn2 -> first audio: 197.1ms
binary packets after abort before turn2: 0
server cancel_reason: client_abort
server llm_first_token_ms: 86.0
server tts_first_audio_ms: 191.9
server abort_to_stop_ms: 0.4
server stale_packets: 0
PASS
```

语法检查通过：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/xiaozhi-pycache python3 -m py_compile ...
```

## 4. 当前注意事项

1. 本次只验证 server text-only offline barge-in，不包含固件播放中拾音、AEC、真实硬件 stop/clear buffer。
2. `send_turn_metrics_to_client` 只在临时验证脚本中开启，默认产品配置不会向客户端下发 metrics。
3. 工作区中仍有两个与本次任务无关的既有本地改动未纳入本次提交：

```text
main/xiaozhi-server/config/assets/wakeup_words/ed76d459636c2481aec828516c1b4f54.wav
main/xiaozhi-server/core/providers/tts/GPT-SoVITS-V3
```

提交时应继续排除它们。
