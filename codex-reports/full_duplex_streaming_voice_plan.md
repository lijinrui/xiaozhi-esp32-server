# xiaozhi 全双工流式语音体验升级研究与实施计划

生成时间：2026-06-02 12:15 CST

## 1. 目标

本报告面向 `xiaozhi-esp32-server` 与 `xiaozhi-esp32` 的下一阶段体验升级，目标不是完整商用智能音箱级全双工，而是先做成：

```text
final ASR -> LLM streaming -> TTS streaming
播放中监听 -> 用户插话 -> cancel
```

用户体验目标：

```text
1. 用户说完后，机器人尽快开始回答。
2. LLM 不等完整回复，边生成边送 TTS。
3. TTS 不等完整文本，边合成边发给设备播放。
4. 机器人播放时仍持续监听用户。
5. 用户插话时，机器人 200-500ms 内停止当前播放。
6. 被打断的 LLM/TTS/tool 结果不会污染下一轮对话。
```

这是一条务实路线：先做好“桌面机器人级可打断、播放时还能听见人插话”，再逐步升级 ASR partial 预热、AEC、Protocol v4、Device Shadow。

## 2. 当前架构可行性判断

现有 `xiaozhi-esp32-server` 已具备良好基础：

- WebSocket 长连接。
- Opus 音频帧。
- VAD / ASR / LLM / TTS Provider 模式。
- Intent / Function call / MCP。
- 客户端 abort 机制。
- 部分流式 ASR / TTS Provider。
- ESP32 端具备音频采集、播放、Opus 编解码和状态上报能力。

但当前体验更接近：

```text
听一段 -> ASR -> LLM -> TTS -> 播放
```

要升级成可打断流式体验，需要显式引入：

```text
TurnManager / RealtimeVoiceSession
turn_id
可取消任务组
LLM token chunker
TTS audio egress queue
设备播放队列 clear/flush
播放中 VAD/AEC barge-in
被打断历史截断
```

## 3. 行业最佳实践摘要

### 3.1 Streaming + Pipelining

语音 Agent 主流生产架构仍然是级联流式管线：

```text
Streaming STT -> Streaming LLM -> Streaming TTS
```

关键不是单个模型最强，而是每个组件都可流式、可取消、可观测。LiveKit 的 voice agent 实践认为，流式化后延迟不再简单等于 `VAD + STT + LLM + TTS` 串行相加，而更接近各阶段瓶颈的重叠。

### 3.2 Barge-in

优秀 voice agent 必须支持用户在机器人说话时插话：

```text
agent speaking
  -> VAD detects user speech
  -> interruption event
  -> cancel active TTS playback
  -> cancel or discard active LLM stream
  -> start new listening turn
```

### 3.3 Endpointing

流式 ASR 常见能力：

- interim result / partial transcript
- final transcript
- speech_final / endpointing

本计划第一阶段不依赖 partial 直接回答，仍以 final ASR 为正式触发点，降低抢话和误判风险。

### 3.4 AEC

ESP32-S3 可使用 Espressif ESP-SR AFE/AEC。它适合全双工人机交互、语音唤醒、播放时识别等场景。

当前硬件路线已经更新：优先使用立创实战派 ESP32-S3 / SZPI-ESP32S3，而不是继续围绕 DeskEmoji 单麦方案做主验证。这块板有双麦、ES7210 多通道输入、ES8311 输出反馈到 ES7210 的 MIC3，结构上更适合做播放中插话和 AEC reference。

## 4. 关键设计原则

### 4.1 partial 不等于 commit

ASR partial 可以用于后续预热，但不应直接驱动机器人开口。

第一版策略：

```text
ASR final 才启动正式 LLM 回复。
播放时插话进入新 turn。
```

后续增强：

```text
ASR partial -> 只做 intent/slot 预测与低风险预取
semantic stable -> 可提前 commit
```

### 4.2 一切结果必须绑定 turn_id

每轮用户请求创建一个唯一 `turn_id`：

```text
turn_id = 102
```

所有结果都必须带 `turn_id`：

```text
ASR final
LLM token
TTS text chunk
TTS audio chunk
tool call
audio egress
chat history
```

用户插话后：

```text
current_turn_id = 103
```

旧 turn 的任何迟到结果必须丢弃。

### 4.3 cancel 优先于等待

用户插话时不能等 LLM/TTS 正常结束。必须立即：

```text
cancel llm_task
cancel tts_task
clear audio_egress_queue
send stop/abort to device
ignore stale turn output
```

### 4.4 设备端必须能本地停播

只在服务端停止发音频不够。设备端可能已经缓存了音频。收到 stop 后必须：

```text
停止 I2S 播放
清空 Opus 解码缓存
清空 speaker PCM buffer
清空播放队列
返回 stopped ack
```

### 4.5 历史只记录用户实际听到的内容

机器人被打断后，不能把完整未播完答案写入对话历史。应记录：

```json
{
  "role": "assistant",
  "content": "用户实际听到的前半段...",
  "interrupted": true
}
```

第一版可通过已发送音频时长估算用户听到的文本。

## 5. 服务端目标结构

建议逐步从 `ConnectionHandler` 拆出实时会话模块。

```text
core/
  session/
    realtime_voice_session.py
    turn_manager.py
    turn_pipeline.py
    cancellation.py
    metrics.py
  audio/
    audio_egress_queue.py
    sentence_chunker.py
  device/
    playback_control.py
```

职责建议：

```text
ConnectionHandler
  - websocket 生命周期
  - 消息路由
  - binary audio 收发入口

RealtimeVoiceSession
  - session state
  - current_turn_id
  - current pipeline
  - barge-in handling
  - metrics

TurnPipeline
  - final ASR -> LLM stream -> text chunks -> TTS stream -> audio chunks
  - cancel / cleanup

SentenceChunker
  - LLM token 切成适合 TTS 的短文本

AudioEgressQueue
  - TTS audio chunk 排队
  - clear / discard stale turn
```

## 6. 流式回答管线

### 6.1 基本流程

```text
ASR final text
  -> create turn_id
  -> start LLM streaming
  -> LLM token enters SentenceChunker
  -> completed text chunk enters TTS
  -> TTS audio chunk enters audio_egress_queue
  -> websocket sends audio to device
  -> device plays audio
```

### 6.2 SentenceChunker 策略

推荐初始策略：

```text
遇到 。！？； 立即切句
遇到 ， 且累计字数 >= 12 可切句
超过 700ms 没有标点，但累计字数 >= 18 可切句
每个 chunk 最短 6-8 个中文字符
每个 chunk 最长 30-40 个中文字符
避免每几个字就 TTS，防止声音断裂
```

### 6.3 TTS chunk 注意点

过短：

```text
首包快，但语音一顿一顿，语气不自然。
```

过长：

```text
语音自然，但首包慢。
```

桌面机器人建议偏短，优先反应速度。

## 7. 播放中监听与插话取消

### 7.1 推荐两级策略

设备侧快速触发：

```text
Speaking 状态下继续 mic capture
本地 VAD/AEC 判断用户说话持续 300-500ms
设备立即 stop playback
设备发送 barge_in/abort 到服务端
```

服务端确认：

```text
收到 barge_in
cancel current turn
clear server audio queue
切换 Listening
后续音频进入 ASR
```

### 7.2 防误触发条件

建议：

```text
TTS 开始后前 300ms 不允许触发插话
VAD 命中至少 300ms
播放中 VAD 阈值高于普通监听
若有 AEC，使用 AEC output 判断
若无 AEC，结合音量动态阈值与能量突增
```

### 7.3 soft / hard interruption

第一版可不区分。后续可优化：

```text
soft interruption:
  嗯、哦、好、继续
  可不打断或低优先级

hard interruption:
  停、等一下、不是、算了、换一个
  必须立即打断
```

## 8. 设备端改造要点

### 8.1 播放队列 stop/clear

设备必须支持：

```text
stop current TTS playback
clear Opus decoder buffer
clear PCM speaker buffer
clear queued audio chunks
send stopped ack
```

### 8.2 播放中采音

Speaking 状态下仍保持：

```text
mic capture active
local VAD active
optional AEC active
```

第一版可只上传/检测插话，不必把播放中所有音频正式送 ASR。

### 8.3 AEC

目标硬件：

```text
立创实战派 ESP32-S3 / SZPI-ESP32S3
ESP32-S3-WROOM-1-N16R8
ES7210 ADC
  MIC1: 用户语音输入
  MIC2: 用户语音输入
  MIC3: ES8311 输出反馈，用作 AEC reference
ES8311 DAC
NS4150B 功放
ZTS6216 双麦
1W 喇叭
```

判断：

```text
这块板具备做“桌面机器人级可打断”的关键硬件条件。
它不是为了研究 AEC 底层最标准的板，而是更适合把完整产品体验搭起来。
双麦 + 播放反馈通道让后续 ESP-SR AFE/AEC 更有落点。
```

建议：

```text
第一阶段仍然先做 server 离线验证，不依赖硬件闭环。
第二阶段固件先实现播放队列 stop/clear 和播放时 mic capture active。
第三阶段用高阈值 VAD 验证播放中插话。
第四阶段接 ESP-SR AFE/AEC：
  MIC1/MIC2 作为 near-end speech input
  MIC3/ES8311 feedback 作为 echo reference
  AEC output 先给 VAD/barge-in 判断
  稳定后再考虑把 AEC output 送完整 ASR
```

硬件结构建议：

```text
麦克风远离喇叭
喇叭不要正对麦克风
控制最大播放音量
外壳避免强反射腔
利用双麦输入
调试 MIC3 播放反馈链路的时序和增益
不要在 server cancel path 未稳定前深度调 AEC
```

## 9. Provider 推荐

### 9.1 ASR

优先级：

```text
1. XunfeiStreamASR
2. AliyunStream / DoubaoStreamASR
3. FunASRServer streaming
4. FunASR local
```

理由：

```text
流式 ASR 对 endpointing、final、barge-in 体验很关键。
中文场景下讯飞实时转写成熟，WebSocket 长连接实时返回文本流。
本地 FunASR 适合免费和私有化，但低延迟、并发、端点判断需要更多工程调优。
```

第一版建议：

```text
如果追求最快做出好体验，先用 XunfeiStreamASR 建立基线。
如果必须免费/本地，使用 FunASRServer streaming，并重点调 endpointing。
```

### 9.2 LLM

优先选择：

```text
qwen-flash / qwen-turbo 类低延迟模型
Doubao 低延迟且 function_call 稳定的模型
```

关键指标：

```text
streaming first token 快
短回复自然
function_call 稳定
支持取消/断开 stream
成本可控
```

不建议桌面机器人每轮都用最强慢模型。语音体验中，慢模型会明显破坏自然感。

### 9.3 TTS

优先级：

```text
1. HuoshanDoubleStreamTTS
2. Aliyun realtime TTS / CosyVoice / Qwen-TTS
3. Minimax HTTP streaming
4. EdgeTTS
```

理由：

```text
本目标最依赖 TTS 首包和可取消性。
双向流式 TTS 能更好承接 LLM text chunks。
EdgeTTS 免费好用，但不适合作为低延迟可打断的主力方案，适合兜底。
```

第一版建议：

```text
如果你已有火山双流 TTS 配置，优先使用 HuoshanDoubleStreamTTS。
若偏阿里生态，使用 Aliyun realtime TTS / CosyVoice / Qwen-TTS。
```

### 9.4 VAD / AEC

服务端：

```text
SileroVAD 兜底
```

设备端：

```text
ESP-SR AFE/VAD/AEC 优先
```

## 10. 指标与验收

每个 turn 必须记录：

```text
turn_id
asr_final_ms
llm_first_token_ms
first_text_chunk_ms
tts_first_audio_ms
first_audio_sent_ms
device_play_start_ms
barge_in_detect_ms
barge_in_stop_ms
cancel_complete_ms
audio_queue_remaining_ms
interrupted
stale_output_dropped_count
```

初始体验目标：

```text
LLM first token < 800ms
TTS first audio < 700ms
ASR final 后到首个音频发出 < 1.2s
用户插话到设备停播 < 300ms，最多不超过 500ms
被打断后旧 turn 不再继续说
被打断后对话历史不记录未听完内容
```

## 11. 分阶段实施计划

### 阶段 1：服务端流式回答

目标：

```text
final ASR -> LLM streaming -> TTS streaming
```

任务：

```text
1. 新增 turn_id。
2. 新增 TurnPipeline。
3. 新增 SentenceChunker。
4. 新增 audio_egress_queue。
5. LLM streaming token 接入 chunker。
6. TTS 支持 text chunk 输入。
7. 旧 turn 输出自动丢弃。
8. 输出 turn summary metrics。
```

验收：

```text
长回答能边生成边播放。
不等待完整 LLM 回复。
取消后旧音频不会继续发。
```

### 阶段 2：设备端停播能力

目标：

```text
服务端发 stop/abort，设备立即静音。
```

任务：

```text
1. 设备播放队列支持 clear。
2. Opus decoder buffer 支持 flush。
3. I2S speaker buffer 支持 stop/reset。
4. 服务端发送 stop_speaking/abort。
5. 设备返回 stopped ack。
```

验收：

```text
收到 stop 后 200ms 内静音。
不会残留半句旧音频。
```

### 阶段 3：播放中监听

目标：

```text
Speaking 状态下继续采音并检测插话。
```

任务：

```text
1. 设备 TTS 播放时保持 mic capture。
2. 播放中启用高阈值 VAD。
3. 检测到用户说话后本地停播。
4. 发送 barge_in/abort 到服务端。
5. 服务端 cancel 当前 turn。
```

验收：

```text
机器人说话时，用户说“停一下”，机器人能停。
```

### 阶段 4：AEC / 抗误触发

目标：

```text
降低机器人自己的声音触发打断的概率。
```

任务：

```text
1. 接入 ESP-SR AFE/AEC。
2. 使用播放 PCM 作为 reference。
3. 使用 mic PCM 作为 input。
4. AEC output 给 VAD。
5. 调整播放中 VAD 阈值。
```

验收：

```text
机器人正常说话时不频繁打断自己。
用户正常插话能触发。
```

### 阶段 5：历史截断

目标：

```text
被打断后上下文不乱。
```

任务：

```text
1. 记录已发送文本 chunk。
2. 估算已播放文本。
3. assistant message 标记 interrupted。
4. 历史只保存用户听到的部分。
5. 新 turn 不引用旧 turn 未播完内容。
```

验收：

```text
用户打断后，下一轮不会接着旧答案胡说。
```

## 12. 第一版推荐组合

```text
ASR: M3 Ultra 优先 Qwen3ASRLocal；需要更低延迟时对比 SherpaASRStream
LLM: LMStudioLLM 本地优先；Kimi / MiniMax 云端作效果或兜底对比
TTS: 第一阶段 EdgeTTS 跑通链路；再评估 MiniMax / IndexStreamTTS / Paddle 等低延迟方案
VAD: 设备端 ESP-SR VAD/AEC + 服务端 SileroVAD 兜底
协议: 继续 WebSocket
设备: lckfb-esp32-s3；先实现播放队列 stop/clear，再做 AEC
```

## 13. 暂不优先做的事项

第一阶段不建议优先做：

```text
ASR partial -> LLM 预热
WebRTC
复杂 turn-taking 模型
Device Shadow
Protocol v4
完整商用 AEC
```

原因：

```text
当前最大体验收益来自 turn_id、streaming pipeline、设备停播、播放中 VAD barge-in。
先把这四个做扎实，再做更激进的 partial/semantic trigger。
```

## 14. 后续可继续研究的方向

```text
1. ASR partial 只做 intent/slot 预测与低风险工具预取。
2. semantic trigger 提前 commit。
3. MCP 工具权限分级，防止 partial 阶段误执行硬件动作。
4. Device Shadow + Telemetry，用于动态下发 audio profile。
5. WebRTC / QUIC Gateway，用于浏览器和移动端数字人。
6. 围绕 lckfb-esp32-s3 调 ESP-SR AFE/AEC，重点验证 MIC3 playback feedback 的参考信号质量。
```
