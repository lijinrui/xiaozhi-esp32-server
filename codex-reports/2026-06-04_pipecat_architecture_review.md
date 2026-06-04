# 2026-06-04 Pipecat 架构分析与 xiaozhi-server 可借鉴点

目标：分析 `pipecat-ai/pipecat` 对当前 `xiaozhi-esp32-server` 全双工语音架构的参考价值，包括架构、模块边界、turn/interruption 设计、transport、provider 选型和落地建议。

主要来源：

- Pipecat GitHub: https://github.com/pipecat-ai/pipecat
- Pipecat Overview: https://docs.pipecat.ai/pipecat/learn/overview
- Speech Input & Turn Detection: https://docs.pipecat.ai/pipecat/learn/speech-input
- User Turn Strategies: https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-turn-strategies
- System Frames: https://docs.pipecat.ai/api-reference/server/frames/system-frames
- Supported Services: https://docs.pipecat.ai/api-reference/server/services/supported-services
- Transports: https://docs.pipecat.ai/pipecat/learn/transports
- Metrics: https://docs.pipecat.ai/pipecat/fundamentals/metrics

## 1. 总结判断

Pipecat 最值得参考的不是某一个 STT/TTS provider，而是它把实时语音系统抽象成：

```text
Frame
-> FrameProcessor
-> Pipeline
-> Worker
-> Transport
-> Observer/Metrics
```

这套模型很适合解决我们现在面临的几个问题：

```text
1. ASR partial / final / interrupt / LLM token / TTS audio / tool result 缺少统一事件语义。
2. turn/cancel 逻辑正在变复杂，需要从 ConnectionHandler 里收敛出来。
3. interrupt 不应只是 abort，而应是高优先级系统事件。
4. provider 越来越多，需要统一 adapter 层。
5. 离线测试需要可注入 frame，而不是必须启动真实模型。
```

但不建议把 xiaozhi-server 直接迁移成 Pipecat：

```text
1. ESP32 固件协议已经是 WebSocket + JSON control + Opus binary，直接替换 transport 成本高。
2. Pipecat 默认更偏 Python agent server / WebRTC / telephony / cloud provider 生态。
3. 当前 server 已经有较多业务逻辑、配置、插件、IoT 工具和管理后台耦合。
4. 直接引入会扩大依赖和迁移面，短期风险高于收益。
```

最务实路线：

```text
不直接引入 Pipecat runtime。
借鉴 Pipecat 的 frame/pipeline/turn/interruption/metrics 设计，
在 xiaozhi-server 内部做轻量版 VoicePipeline。
```

## 2. Pipecat 的关键架构

### 2.1 Frame 是系统内统一事件

Pipecat 把音频、转写、LLM 输出、TTS 音频、控制信号都做成 frame。

对 xiaozhi 的启发：

```text
当前我们有：
  websocket json
  binary opus packet
  ASR text
  LLM token
  TTS queue DTO
  tool future result
  abort message

但它们没有统一生命周期和优先级。
```

建议新增内部 frame，不改变外部 ESP32 协议：

```text
AudioInputFrame
ASRPartialFrame
ASRFinalFrame
UserStartedSpeakingFrame
UserStoppedSpeakingFrame
InterruptDecisionFrame
TurnStartFrame
TurnCancelFrame
LLMTokenFrame
TTSChunkFrame
TTSAudioFrame
ToolCallFrame
ToolResultFrame
PlaybackProgressFrame
MetricsFrame
ErrorFrame
```

每个 frame 必须带：

```text
session_id
turn_id
timestamp
source
sequence
```

### 2.2 SystemFrame / ControlFrame / DataFrame 的优先级值得借鉴

Pipecat 的 System Frames 有高优先级语义，例如 `CancelFrame`、`InterruptionFrame`、`UserStartedSpeakingFrame` 等。文档明确说 SystemFrames 不会被普通 interruption 取消，且承载必须送达的生命周期信号。

对 xiaozhi 的启发：

```text
abort / tts stop / interruption / error / metrics
不应该和普通 TTS 文本、LLM token、audio packet 混在同一个普通队列语义里。
```

建议：

```text
1. TurnCancelFrame 是高优先级系统事件。
2. 收到 TurnCancelFrame 后：
   - 清 TTS pending text
   - 停止音频发送
   - cancel LLM/tool future
   - 发 tts stop 给设备
   - 标记旧 turn stale
3. 普通 DataFrame 如果 turn_id 已 stale，必须 drop 并计数。
```

### 2.3 InterruptionFrame 比普通 abort 更精细

Pipecat 的 interruption 不是简单“断开 pipeline”，而是：

```text
用户开始说话
-> start strategy 可发 interruption
-> pending audio/text 被清理
-> pipeline 准备接收新用户输入
```

对 xiaozhi 的启发：

```text
当前 abort 主要是客户端发来的事实事件。
下一步应该拆成：
  UserSpeechStarted
  InterruptCandidate
  InterruptDecision
  TurnCancel
```

这样可以支持：

```text
ignore:
  嗯、好、对、短噪声，不 cancel

soft_interrupt:
  先观察，不立刻污染 history

hard_interrupt:
  立即 cancel old turn

resume_previous:
  false interruption 后恢复上一句
```

### 2.4 Turn strategies 是最值得直接抄作业的部分

Pipecat 把 turn start / turn stop 拆成策略：

```text
Start:
  VADUserTurnStartStrategy
  TranscriptionUserTurnStartStrategy
  MinWordsUserTurnStartStrategy
  KrispVivaIPUserTurnStartStrategy
  ExternalUserTurnStartStrategy

Stop:
  SpeechTimeoutUserTurnStopStrategy
  TurnAnalyzerUserTurnStopStrategy
  ExternalUserTurnStopStrategy
```

对 xiaozhi 的直接落地：

```text
core/voice/turn_start.py
  VADStartStrategy
  ASRPartialStartStrategy
  MinWordsStartStrategy
  WakeWordStartStrategy
  RuleInterruptStartStrategy

core/voice/turn_stop.py
  SpeechTimeoutStopStrategy
  ASRFinalStopStrategy
  SemanticCompleteStopStrategy
```

这能把“播放中插话”和“用户说完没说完”从一堆 if 里解救出来。

### 2.5 UserTurnInferenceCompletedFrame 对中文很重要

Pipecat 有 `UserTurnInferenceCompletedFrame`：由 STT、LLM 标记、专门 end-of-turn classifier 等组件发出，表示语义上真的说完了。

对 xiaozhi 的启发：

```text
不要只靠 VAD silence 判断用户说完。
中文里“我想问一下那个...”中间停顿很常见。
```

建议：

```text
ASR final + 标点 + pause timeout + semantic complete classifier
共同决定是否 commit 到 LLM。
```

短期：

```text
SpeechTimeoutStopStrategy:
  vad silence 600-900ms
  且至少有 ASR final
```

中期：

```text
SemanticCompleteStopStrategy:
  小模型/规则判断是否语义完整
```

### 2.6 UserMute 机制适合处理“不要误打断”

Pipecat 有 user mute：bot 说话时暂时抑制用户输入，避免 interruption。

我们不能完全 mute，因为目标是播放中插话。但可以借鉴成：

```text
SelectiveUserMute / InterruptGate
```

规则：

```text
bot speaking 时：
  VAD/audio 继续进 ASR
  ASR partial 进入 InterruptClassifier
  backchannel/noise 被 gate 掉
  hard interrupt 才进入 TurnCancel
```

这比直接关麦或直接 abort 都好。

## 3. Provider 选型参考

Pipecat 支持很多 provider，说明它的价值在于 provider adapter，而不是押注单个 provider。

### 3.1 STT

Pipecat 官方支持：

```text
Deepgram
AssemblyAI
AWS Transcribe
Azure
Google
Groq Whisper
Speechmatics
Soniox
Whisper
```

对我们：

```text
云端 benchmark:
  Deepgram / Speechmatics / Soniox / AssemblyAI

本地主线:
  FunASR streaming
  Qwen ASR benchmark
  faster-whisper fallback
```

但由于用户主要中文桌面机器人，Pipecat provider 列表不能直接决定最终选型。中文本地优先还是 FunASR/Qwen ASR。

### 3.2 LLM

Pipecat 支持：

```text
OpenAI
Anthropic
Google Gemini
DeepSeek
Mistral
Ollama
Qwen
OpenRouter
Together
Groq
```

对我们：

```text
继续保持 OpenAI-compatible adapter。
Kimi / MiniMax / LM Studio / Qwen 本地都走统一 streaming LLM adapter。
```

关键不是换 LLM，而是：

```text
token frame 带 turn_id
late token drop
tool call cancel
TTFT metrics
```

### 3.3 TTS

Pipecat 支持：

```text
Cartesia
ElevenLabs
Deepgram
Fish
Kokoro
MiniMax
OpenAI
Piper
XTTS
Rime
LMNT
```

对我们：

```text
低延迟云端 benchmark:
  Cartesia
  ElevenLabs
  MiniMax TTS
  OpenAI TTS/realtime

本地免费候选:
  CosyVoice2-0.5B（Pipecat 未必内置，但适合中文）
  Kokoro-ONNX（Pipecat 已有 Kokoro provider，可作 baseline）
  Fish Speech/Fish Audio S2（Pipecat 支持 Fish，值得 benchmark）
  Qwen3-TTS（单独 benchmark）
```

如果参考 Pipecat 生态，当前最有价值的是：

```text
Kokoro:
  轻量本地 baseline

Fish:
  开源效果/低延迟候选

Cartesia/ElevenLabs:
  低延迟商业体验上限 benchmark
```

中文本地最终仍优先测 CosyVoice2。

## 4. Transport 参考

Pipecat 支持：

```text
DailyTransport
FastAPIWebsocketTransport
LiveKitTransport
SmallWebRTCTransport
```

对 xiaozhi：

```text
ESP32:
  继续 WebSocket + JSON control + Opus binary。

PC/browser validator:
  可参考 FastAPIWebsocketTransport 或 SmallWebRTCTransport。

未来 App:
  WebRTC/LiveKit 更合适。
```

不要现在把 ESP32 切 WebRTC。Pipecat 的 transport 抽象值得借鉴：

```text
TransportInput:
  incoming audio/json

TransportOutput:
  outgoing audio/json

同一 pipeline 可挂 ESP32 WebSocket、PC fake client、WebRTC browser。
```

## 5. Metrics/Observer 参考

Pipecat 有 Metrics/Observer，例如 `MetricsLogObserver`，支持 LLM/TTS/processing/TTFB 等指标。

对 xiaozhi：

建议新增：

```text
VoiceMetricsObserver
```

指标：

```text
turn_id
asr_partial_at
asr_final_at
llm_first_token_ms
tts_first_chunk_ms
tts_first_audio_ms
abort_to_stop_ms
turn2_first_audio_ms
dropped_llm_tokens
dropped_tts_chunks
dropped_audio_packets
dropped_tool_results
interrupt_decision
interrupt_reason
spoken_commit_chars
history_truncated
```

这比现在零散日志更适合后面调体验。

## 6. 推荐落地路线

### P0：不要引入 Pipecat，先做轻量内部 frame 化

新增：

```text
main/xiaozhi-server/core/voice/frames.py
main/xiaozhi-server/core/voice/turn_manager.py
main/xiaozhi-server/core/voice/interrupt_classifier.py
main/xiaozhi-server/core/voice/tts_chunker.py
main/xiaozhi-server/core/voice/metrics_observer.py
```

不要第一步就重构所有 provider。先把现有 `ConnectionHandler` 的输出点包一层 frame/guard。

### P1：把当前 abort flow 改成 frame flow

当前：

```text
client abort json
-> handleAbortMessage
-> cancel_current_turn
-> clear_queues
-> websocket send tts stop
```

目标：

```text
AbortMessage
-> TurnCancelFrame(reason=client_abort)
-> TurnManager.cancel()
-> TTSQueue.clear(turn_id)
-> AudioSender.stop(turn_id)
-> ToolManager.cancel(turn_id)
-> TransportOutputFrame(tts stop)
```

### P2：引入 turn strategies

把“播放中是否打断”拆成：

```text
StartStrategies:
  wake word
  VAD
  ASR partial
  min words / backchannel filter

StopStrategies:
  ASR final
  silence timeout
  semantic complete
```

### P3：做 PC fake pipeline validator

参考 Pipecat 的可注入 frame 思路，离线测试不要靠真实模型。

测试脚本输入：

```text
AudioInputFrame / ASRPartialFrame / ASRFinalFrame / LLMTokenFrame / ToolResultFrame
```

验证：

```text
hard interrupt cancel
weak backchannel ignore
late output drop
history truncation
turn2 first audio
```

## 7. 和当前计划的关系

这份 Pipecat 分析强化了之前计划中的判断：

```text
TurnManager:
  必须做，而且最好从 ConnectionHandler 收敛出去。

TTSChunker:
  必须做，相当于 LLM text frame -> TTS text/audio frame 的 processor。

InterruptClassifier:
  必须做，相当于 Pipecat start strategies + interruption prediction 的轻量版。

Spoken Commit:
  必须做，对应 bot speaking/audio output 的状态反馈和 history 管理。

Metrics:
  必须做，参考 Pipecat observer。
```

## 8. 最终建议

```text
不要把 Pipecat 当成要替换 xiaozhi-server 的框架。
把 Pipecat 当成一份成熟 voice-agent 架构蓝图。
```

最值得照抄的四个设计：

```text
1. Frame 化内部事件。
2. SystemFrame 高优先级 cancel/interruption。
3. Turn start/stop strategies。
4. Metrics observer。
```

最值得参考的模块选型：

```text
STT:
  云端 benchmark 看 Deepgram/Speechmatics/Soniox。
  本地中文仍优先 FunASR/Qwen ASR。

LLM:
  统一 OpenAI-compatible/Qwen/Kimi/MiniMax/LM Studio streaming adapter。

TTS:
  本地 baseline 看 Kokoro。
  开源高质量看 Fish。
  中文主线继续测 CosyVoice2/Qwen3-TTS。
  商业低延迟 benchmark 看 Cartesia/ElevenLabs/MiniMax/OpenAI。

Transport:
  ESP32 保持 WebSocket。
  PC/browser validator 可参考 WebRTC/SmallWebRTC/LiveKit。
```

