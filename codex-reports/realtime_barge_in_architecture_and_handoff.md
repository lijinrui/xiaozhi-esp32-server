# xiaozhi 实时流式可打断语音架构改造计划与交接文档

生成时间：2026-06-02

## 0. 可检测 Goal

长任务目标：

```text
在 xiaozhi-esp32-server 上实现并验证一条可取消、可观测、可重复测试的实时语音 turn 链路：

final ASR -> LLM streaming -> TTS streaming
播放中监听 -> 用户插话 -> cancel current turn -> start next turn
```

验收方式：

```text
使用 codex-tools/offline_barge_in_test/offline_barge_in_test.py 或其扩展版，在没有真实硬件的电脑环境中完成可重复验证。
```

必须通过的行为验收：

```text
1. server 能完成第一轮用户输入 -> LLM/TTS 开始输出。
2. 测试客户端在检测到第一轮 TTS start 或 audio packet 后发送 abort。
3. server 在收到 abort 后发送 tts stop。
4. abort 后第一轮的 LLM token、TTS 文本、TTS 音频、tool result 不再继续下发给客户端。
5. 第二轮用户输入可以立即进入新的 turn。
6. 第二轮回复内容不混入第一轮被打断的残留内容。
7. 对话历史只记录已确认应该保留的内容，被打断且未完成播放的 assistant 内容不能污染下一轮。
```

量化验收指标：

```text
abort_to_tts_stop_ms <= 500ms
abort 后到第二轮开始前 stale audio packet <= 2
第二轮 listen/detect 到 tts start <= 2500ms，本地模型冷启动除外
连续运行 10 次 offline barge-in test，成功率 >= 90%
server 日志中每个 turn 都有 turn_id/session_id/cancel_reason/metrics summary
```

需要交付的代码能力：

```text
1. TurnManager 或等价机制：每轮都有唯一 turn_id。
2. cancel path：abort 能取消当前 LLM/TTS/tool 任务。
3. stale output filter：旧 turn 的迟到输出会被丢弃。
4. audio queue clear：abort 后清空待发送音频。
5. metrics：输出 asr_final_ms、llm_first_token_ms、tts_first_audio_ms、abort_to_stop_ms、stale_packets。
6. offline test：至少支持 text-only barge-in；扩展目标是 PC wav/Opus 注入。
```

失败判定：

```text
只要出现以下任一情况，就不能算完成：

1. abort 后旧回答继续播放或继续下发明显音频。
2. 第二轮回复混入第一轮残留内容。
3. 只能靠真实硬件验证，无法在电脑上离线复现。
4. 没有 metrics，无法判断延迟和 stale packet 数量。
5. 需要大规模重构但没有保留现有 provider/config 兼容性。
```

非目标：

```text
第一阶段不要求完成硬件 AEC 调参。
第一阶段不要求 ASR partial 直接驱动 LLM 正式回复。
第一阶段不要求 Protocol v4、WebRTC、Device Shadow。
第一阶段不要求商用智能音箱级远场全双工。
```

## 1. 背景与目标

当前目标不是一步到位做商用智能音箱级全双工，而是先做出“桌面机器人级可打断、播放时还能听见用户插话”的体验。

核心链路：

```text
final ASR -> LLM streaming -> TTS streaming
播放中监听 -> 用户插话 -> cancel
```

目标体验：

```text
1. 用户说完后，服务端尽快拿到 final ASR。
2. LLM 以 streaming 方式输出，不等完整回答。
3. TTS 以 streaming/chunk 方式合成，不等完整文本。
4. TTS 音频边生成边下发给设备播放。
5. 机器人播放时仍然监听用户插话。
6. 用户插话后，设备立即停播，server 取消当前 LLM/TTS/tool。
7. 旧 turn 的迟到 token、音频、工具结果都被丢弃。
8. 第二轮用户输入能立刻进入新的 turn。
```

## 2. 总体架构

建议架构分为 4 层：

```text
[Device / Virtual Client]
  - ESP32-S3 / DeskEmoji / PC offline client
  - mic capture
  - speaker playback
  - local VAD/AEC barge-in
  - stop playback / clear buffers

        |
        | WebSocket, text detect, binary Opus
        v

[Realtime Session Layer]
  - ConnectionHandler
  - RealtimeVoiceSession
  - TurnManager
  - cancellation
  - metrics
  - stale output filtering

        |
        v

[AI Pipeline Layer]
  - ASR final / streaming ASR
  - LLM streaming
  - SentenceChunker
  - TTS streaming
  - audio egress queue

        |
        v

[Provider / Tool Layer]
  - Qwen3ASRLocal / SherpaASRStream / FunASR
  - LMStudio / Kimi / MiniMax / GPT fallback
  - EdgeTTS / MiniMaxTTS / IndexStreamTTS / local TTS
  - MCP / plugins / tools
```

## 3. 核心判断

Server 是第一阶段主战场。

硬件决定“播放时能不能听清人声、误触发多不多”，但 server 决定：

```text
是否能正确取消当前回答
是否能清空旧音频队列
是否能丢弃旧 turn 的迟到结果
是否能开启第二轮
对话历史是否不被未播完内容污染
```

如果 server 不稳，再好的麦克风/AEC 也救不了体验。

因此优先级：

```text
1. Server 离线打断闭环
2. Server turn_id / cancellation / metrics
3. PC 虚拟设备实时测试
4. 固件 stop/clear + 播放中监听
5. AEC / 硬件选型
```

## 4. 目标状态机

建议引入显式状态机：

```text
Idle
  -> Listening
  -> Thinking
  -> Speaking
  -> Interrupted
  -> Listening
  -> Closing
```

当前 `ConnectionHandler` 里已经有部分状态：

```text
client_abort
client_is_speaking
client_listen_mode
tts_start_sent
sentence_id
close_after_chat
```

但这些状态散落在连接、ASR、TTS、handle 函数里。后续应集中到 `RealtimeVoiceSession / TurnManager`。

## 5. Turn ID 设计

每轮用户输入创建一个 `turn_id`：

```text
turn_id = 102
```

所有异步结果都带上这个 ID：

```text
ASR final
LLM token
TTS text chunk
TTS audio chunk
tool call
audio egress
history update
```

用户插话后：

```text
current_turn_id = 103
```

所有 `turn_id=102` 的迟到结果直接丢弃。

这比单纯依赖 `client_abort` 更可靠。

## 6. Server 改造范围

### 6.1 当前代码现状

关键文件：

```text
main/xiaozhi-server/core/connection.py
main/xiaozhi-server/core/handle/abortHandle.py
main/xiaozhi-server/core/handle/receiveAudioHandle.py
main/xiaozhi-server/core/handle/sendAudioHandle.py
main/xiaozhi-server/core/providers/tts/base.py
main/xiaozhi-server/core/providers/asr/qwen3_asr_local.py
main/xiaozhi-server/core/providers/asr/sherpa_onnx_stream.py
```

已有可利用能力：

```text
abortHandle:
  client_abort = True
  clear_queues()
  send {"type":"tts","state":"stop"}
  clearSpeakStatus()

sendAudioHandle:
  sendAudioMessage()
  sendAudio()
  AudioRateController
  sentence_id stale filtering

receiveAudioHandle:
  startToChat()
  播放中收到 ASR 分段时可 handleAbortMessage()

ASR:
  Qwen3ASRLocal 是 InterfaceType.STREAM
  SherpaASRStream 是 InterfaceType.STREAM
```

### 6.2 第一阶段 server 目标

不先大规模重构，先让现有结构可测、可取消、可观测。

目标：

```text
1. offline_barge_in_test 能稳定 PASS。
2. abort 后旧音频包数量可控。
3. 第二轮 turn 不被第一轮残留污染。
4. 输出明确 metrics。
```

### 6.3 第二阶段 server 目标

引入最小 TurnManager：

```text
core/session/turn_manager.py
core/session/metrics.py
core/audio/sentence_chunker.py
```

职责：

```text
TurnManager:
  - current_turn_id
  - next_turn()
  - abort_current_turn()
  - is_current(turn_id)

Metrics:
  - turn start
  - first token
  - first tts audio
  - abort sent
  - stale audio dropped

SentenceChunker:
  - LLM streaming token -> TTS text chunk
```

先不要求完全拆出 `RealtimeVoiceSession`，避免一次改动过大。

### 6.4 第三阶段 server 目标

将实时会话从 `ConnectionHandler` 拆出：

```text
core/session/realtime_voice_session.py
core/session/turn_pipeline.py
core/audio/audio_egress_queue.py
```

`ConnectionHandler` 逐步退化成：

```text
WebSocket lifecycle
message routing
device metadata
```

## 7. 离线验证策略

### 7.1 为什么可以离线验证

可打断体验中，很多风险不依赖硬件：

```text
server 是否处理 abort
LLM stream 是否停止
TTS queue 是否清空
audio rate controller 是否停止
旧音频是否继续发送
第二轮是否能开始
```

这些都可以用 PC client 测。

### 7.2 已新增测试工具

已新增：

```text
codex-tools/offline_barge_in_test/offline_barge_in_test.py
codex-tools/offline_barge_in_test/README.md
```

它模拟 ESP32：

```text
1. 连接 ws://127.0.0.1:8000/xiaozhi/v1/
2. 发送 hello
3. 用 listen/detect 直接发第一轮文本
4. 等待 TTS start 或音频包
5. 发送 abort
6. 等待 tts stop
7. 发送第二轮文本
8. 输出 metrics
```

它先用文本模式而不是音频模式，是为了优先验证 server cancel 主路径。

### 7.3 运行方式

先启动 server：

```bash
cd /Users/wangjinyuan1/Proj/xiaozhi/xiaozhi-esp32-server/main/xiaozhi-server
python app.py
```

再运行测试：

```bash
cd /Users/wangjinyuan1/Proj/xiaozhi
python codex-tools/offline_barge_in_test/offline_barge_in_test.py \
  --url ws://127.0.0.1:8000/xiaozhi/v1/
```

注意：需要使用安装了 `websockets` 的 Python 环境，通常就是 `xiaozhi-server` 的运行环境。

### 7.4 当前测试指标

输出：

```text
hello latency
turn1 -> first STT
turn1 -> TTS start
turn1 -> sentence_start
turn1 -> first audio
abort -> tts stop
turn2 -> STT
turn2 -> TTS start
turn2 -> first audio
binary packets before abort
binary packets after abort before turn2
binary packets after turn2
```

关键指标：

```text
binary packets after abort before turn2
```

如果这个数字很高，说明 abort 后仍有旧音频继续发送。

默认容忍值：

```text
allowed_stale_packets = 2
```

## 8. M3 Ultra 验证建议

你的 M3 Ultra 96G 适合跑本地 ASR/LLM。

推荐第一轮：

```text
ASR: Qwen3ASRLocal
LLM: LMStudioLLM
TTS: EdgeTTS
```

原因：

```text
Qwen3ASRLocal:
  当前项目已有 MLX streaming Provider。
  M3 Ultra 适合跑 Qwen3-ASR-1.7B-8bit。

LMStudioLLM:
  本地 OpenAI-compatible server。
  适合低延迟主链路。

EdgeTTS:
  先用稳定低成本 TTS 跑通架构。
  GPT-SoVITS 此前卡顿，不建议第一轮阻塞在它上面。
```

第二轮再试：

```text
ASR: Qwen3ASRLocal / SherpaASRStream A/B
LLM: LMStudioLLM + Kimi fallback
TTS: MiniMaxTTS / IndexStreamTTS
```

## 9. Provider 选型

### 9.1 ASR

当前推荐：

```text
1. Qwen3ASRLocal
2. SherpaASRStream
3. FunASRServer
4. FunASR local
```

关键原因：

```text
Qwen3ASRLocal:
  准确率潜力高。
  MLX 适配 Apple Silicon。
  项目中已经是 InterfaceType.STREAM。

SherpaASRStream:
  端点检测确定性强。
  适合作为低延迟对照组。
```

应实测：

```text
final ASR latency
是否漏句首
短命令识别
噪声环境
barge-in 场景
```

### 9.2 LLM

本地主链路：

```text
LM Studio:
  qwen2.5-7b-instruct
  qwen2.5-14b-instruct
  qwen3-8b / qwen3-14b with no_think
```

云端兜底：

```text
Kimi:
  复杂问题、长上下文、规划、代码。

MiniMax:
  可用于聊天或 TTS，如果已有额度。

GPT:
  高质量兜底，不建议放每轮语音主链路。
```

### 9.3 TTS

第一轮：

```text
EdgeTTS
```

后续：

```text
MiniMax HTTP stream
IndexStreamTTS
PaddleSpeechTTS
```

暂不优先：

```text
GPT-SoVITS
```

原因：

```text
之前跑起来卡顿。
实时对话优先稳定首包和可取消，不优先追求音色克隆。
```

## 10. 固件改造计划

固件不是第一阶段主战场，但最终必须改。

### 10.1 固件 v1：无 AEC 可打断

目标：

```text
机器人说话时，用户明显插话，设备立即闭嘴。
```

改动：

```text
1. 播放队列支持 clear。
2. 收到 server tts stop/abort 时 flush 播放缓存。
3. TTS 播放中不要完全关闭 mic。
4. 播放中跑高阈值 VAD。
5. VAD 命中后本地 stop playback。
6. 发 abort JSON 给 server。
```

### 10.2 固件 v2：ESP-SR AEC

目标：

```text
减少机器人自己的声音触发插话。
```

改动：

```text
1. 接 ESP-SR AFE/AEC。
2. speaker playback PCM 作为 far-end reference。
3. mic PCM 作为 near-end input。
4. AEC output 给 VAD。
5. 调整 NLP level、VAD threshold。
```

### 10.3 固件 v3：更完整全双工

目标：

```text
播放时也能稳定识别完整用户语音。
```

需要：

```text
双麦阵列
更好的 AEC reference
音频帧同步
声学结构优化
更严谨设备状态同步
```

## 11. 目标硬件：立创实战派 ESP32-S3

当前已经确定主验证硬件：

```text
立创实战派 ESP32-S3 / SZPI-ESP32S3
内部代号建议：lckfb-esp32-s3
```

关键配置：

```text
ESP32-S3-WROOM-1-N16R8
PSRAM 8MB / Flash 16MB
ES7210 四通道 ADC，开发板使用三路
  MIC1: 用户语音输入
  MIC2: 用户语音输入
  MIC3: ES8311 输出反馈，用作 AEC reference
ES8311 音频 DAC
NS4150B 功放
ZTS6216 双麦
1W 喇叭
ST7789 屏幕 / FT6336 触摸
```

结论：

```text
这块板比 BOX-3 / Korvo-2 更适合作为当前项目主硬件。
原因不是它 AEC 底层研究能力最强，而是它已经具备“搭出可用桌面机器人”的关键链路：

1. 双麦输入。
2. ES7210 多通道采集。
3. ES8311 播放输出反馈到 ES7210 MIC3，可作为 AEC reference。
4. 有屏幕、触摸、喇叭、电池/Type-C 等完整产品形态。
5. 用户已经有明确资料和购买路径。
```

当前策略：

```text
不要再优先购买 BOX-3 或 Korvo-2。
先把 server 和离线打断验证跑稳。
再围绕 lckfb-esp32-s3 做固件适配。
```

固件适配顺序：

```text
1. 跑通 ES7210 三路采集和 ES8311 播放。
2. 跑通小智 WebSocket 协议。
3. 实现播放队列 stop/clear。
4. 播放时保持 mic capture active。
5. 先用高阈值 VAD 做可打断。
6. 再接 ESP-SR AFE/AEC，把 MIC3 playback feedback 作为 echo reference。
7. AEC output 先服务于 barge-in 判断，再考虑送完整 ASR。
```

硬件资料：

```text
https://docs.gtai-tech.com/node/0198944f-25cd-7af2-b6bb-d2b6ad5dd60a
https://wiki.lckfb.com/zh-hans/szpi-esp32s3/beginner/introduction.html
https://wiki.lckfb.com/zh-hans/szpi-esp32s3/beginner/audio-input-es7210.html
```

## 12. 当前已完成改动

新增报告：

```text
codex-reports/full_duplex_streaming_voice_plan.md
codex-reports/local_runtime_stack_plan.md
codex-reports/realtime_barge_in_architecture_and_handoff.md
```

新增工具：

```text
codex-tools/offline_barge_in_test/offline_barge_in_test.py
codex-tools/offline_barge_in_test/README.md
```

当前未改 server 主逻辑。

## 13. 下一步代码任务

### Task A：在 M3 Ultra 上跑离线测试

```text
1. 配好 data/.config.yaml。
2. 启动 xiaozhi-server。
3. 运行 offline_barge_in_test。
4. 记录 metrics。
```

如果失败，优先看：

```text
server 是否需要设备绑定
websocket 是否认证
TTS 是否能正常出音频
abort 后是否 tts stop
stale audio packet 是否过多
```

### Task B：修 abort 后残留音频

可能修改点：

```text
ConnectionHandler.clear_queues()
AudioRateController.reset()/stop_sending()
sendAudioHandle._send_audio_with_rate_control()
sendAudioHandle._start_background_sender()
sentence_id 更新时机
```

目标：

```text
binary packets after abort before turn2 <= 2
```

### Task C：加入 turn_id

最小实现：

```text
ConnectionHandler.current_turn_id
ConnectionHandler.next_turn_id()
ConnectionHandler.abort_turn()
sendAudioMessage 检查 turn_id 或 sentence_id
LLM/TTS 回调带 turn_id
```

### Task D：加入 metrics summary

每轮结束输出：

```text
turn_id
asr_final_ms
llm_first_token_ms
tts_first_audio_ms
abort_to_stop_ms
stale_packets
interrupted
```

### Task E：PC wav/Opus 注入测试

在 text detect 测通后，扩展 offline client：

```text
wav -> pcm -> opus frames -> websocket binary
listen start / stop
interrupt.wav 自动注入
```

这样可以测试 ASR 与更真实的音频链路。

## 14. 交接给 M3 Ultra Agent 的重点

请从这里开始：

```text
1. 阅读本文件。
2. 阅读 codex-tools/offline_barge_in_test/README.md。
3. 配置本地 xiaozhi-server 环境。
4. 使用 Qwen3ASRLocal + LMStudioLLM + EdgeTTS 跑 offline_barge_in_test。
5. 根据 metrics 修改 server cancel path。
6. 确认 abort 后 stale audio packet 是否稳定 <= 2。
7. 再扩展 PC wav/Opus 注入测试。
8. 硬件按 lckfb-esp32-s3 做后续固件适配，不从 BOX-3 / Korvo-2 起步。
9. 每次修改后更新本报告的“当前已完成改动”和“下一步代码任务”。
```

不要一开始做：

```text
硬件 AEC 深度调参
Protocol v4
ASR partial 预热
Device Shadow
大规模重构 ConnectionHandler
```

先把：

```text
server abort/cancel/stale-output
text-only offline barge-in
PC wav/Opus offline barge-in
```

做到可测、可重复、可量化。
