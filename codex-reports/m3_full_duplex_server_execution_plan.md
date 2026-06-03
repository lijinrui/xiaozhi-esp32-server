# M3 执行计划：Server 全双工打断体验功能优化与测试

生成时间：2026-06-03

适用仓库：`xiaozhi-esp32-server`

建议工作分支：基于 `origin/feat/server-offline-barge-in-turns` 新开分支，例如：

```bash
git checkout -b feat/server-full-duplex-experience origin/feat/server-offline-barge-in-turns
```

## 1. 总目标

把当前已经跑通的 server abort/turn guard，从“能停住旧 TTS”升级为“桌面机器人级全双工语音交互状态机”。

核心体验目标：

```text
final ASR -> LLM streaming -> TTS streaming
播放中监听 -> 用户插话 -> 判断该不该打断
真打断 -> cancel old turn -> stop old TTS -> start new turn
弱反馈/噪声 -> 不打断
被打断后 -> 对话历史只保留用户实际听到的 assistant 内容
```

## 2. 非目标

本阶段不要做这些：

```text
1. 不切换 ESP32 到 WebRTC。
2. 不依赖真机。
3. 不依赖付费 ASR/TTS/LLM。
4. 不把纯 speech-to-speech 大模型作为主链路。
5. 不深挖 AEC 参数。
6. 不合并到 main，先在 feature 分支闭环。
```

## 3. 当前已完成基础

`origin/feat/server-offline-barge-in-turns` 已经完成：

```text
1. turn_id 基础机制。
2. client abort 后 server 发送 tts stop。
3. TTS control message 携带 turn_id。
4. sendAudio 路径过滤 stale turn。
5. LLM streaming 循环检查 current turn。
6. tool future cancel 基础机制。
7. offline barge-in 10/10 PASS。
8. real provider smoke PASS。
```

所以本阶段不是重写 cancel 基础链路，而是补功能体验和测试覆盖。

## 4. 必须交付的代码模块

### 4.1 TurnManager 收敛

现有 turn 逻辑在 `ConnectionHandler` 中已经能工作，但建议收敛为独立模块或至少独立类。

建议能力：

```text
begin_turn(query, source)
cancel_turn(turn_id, reason)
is_current(turn_id)
bind_sentence(sentence_id, turn_id)
bind_tool_future(turn_id, future)
mark_llm_token_dropped(turn_id)
mark_tts_chunk_dropped(turn_id)
mark_audio_packet_dropped(turn_id)
mark_tool_result_dropped(turn_id)
mark_spoken_text(turn_id, text, estimated_played_ms)
get_turn_metrics(turn_id)
```

验收：

```text
旧 turn cancel 后，LLM token、TTS chunk、audio packet、tool result 的 late output 都有明确 drop 计数。
```

### 4.2 TTSChunker

不要让 LLM token 直接无脑进入 TTS。

建议新增：

```text
core/voice/tts_chunker.py
```

第一版规则：

```text
first_chunk:
  中文 8-20 字，或 max_wait_ms 150-300ms

middle_chunk:
  优先按 。！？；，、 换行 切分
  最大 40-80 中文字

cancel:
  turn cancel 后 pending chunk 全部丢弃
```

验收：

```text
1. 半句 token 不会过早播出，除非达到首包最大等待。
2. 长句会被切成合理 chunk。
3. cancel 后 pending chunk 不进入 TTS queue。
```

### 4.3 InterruptClassifier

新增轻量规则分类器：

```text
core/voice/interrupt_classifier.py
```

输入：

```text
playback_state
vad_event
asr_partial
asr_final
wake_word
current_turn_id
```

输出：

```text
ignore
soft_interrupt
hard_interrupt
resume_previous
```

第一版规则：

```text
hard_interrupt:
  唤醒词
  "停一下"
  "等等"
  "等一下"
  "别说了"
  "暂停"
  "不是"
  "不对"
  "我说的是"
  明显新问题，例如 "那天气呢"、"换一个问题"

ignore:
  "嗯"
  "好"
  "对"
  "哦"
  "可以"
  "行"
  短笑声/咳嗽占位文本

soft_interrupt:
  内容长度较短但不像 backchannel，等待 ASR final 再决定。
```

验收：

```text
弱反馈不能触发 cancel。
明确打断必须触发 cancel。
soft interrupt 不得立刻污染 history。
```

### 4.4 Spoken Commit / History Truncation

目标：用户打断后，history 里不能记录 assistant 未实际播放的完整回答。

第一版可以估算，不需要设备 ack：

```text
spoken_text = 已发送给 TTS 并大概率播放的文本
safety_margin_ms = 300-800ms
```

建议接口：

```text
mark_tts_chunk_sent(turn_id, sentence_id, text, estimated_audio_ms)
mark_audio_packet_sent(turn_id, packet_duration_ms)
get_spoken_commit(turn_id)
truncate_assistant_history(turn_id)
```

验收：

```text
长回答说到一半被打断后，dialogue/history 只保留 spoken_commit 文本。
未播放文本不能进入 assistant history。
```

## 5. 测试分层

### Layer 1：纯单元测试，不启动 server

必须新增：

```text
tests/test_tts_chunker.py
tests/test_interrupt_classifier.py
tests/test_turn_manager.py
tests/test_spoken_commit_history.py
```

这些测试不允许依赖：

```text
真实 ASR
真实 TTS
真实 LLM
网络
音频设备
ESP32
```

### Layer 2：fake provider server 离线集成测试

建议新增：

```text
codex-tools/offline_full_duplex_test/offline_full_duplex_test.py
```

它应该使用 fake LLM / fake TTS / text listen，专测 server 状态机。

### Layer 3：real provider smoke

保留已有 real smoke：

```text
MiniMax/Kimi + EdgeTTS + text listen
```

目标只是证明真实 provider 没被架构改动破坏，不作为核心功能正确性的唯一依据。

## 6. 详细测试 Case

### TC01：hard interrupt 停止旧 turn

输入：

```text
turn1: "请用十句话介绍小智机器人。"
server 开始 TTS/audio
用户插话: "停一下，改成一句话。"
```

期望：

```text
1. InterruptClassifier 输出 hard_interrupt。
2. server cancel old turn。
3. server 发送 {"type":"tts","state":"stop","turn_id": old_turn_id}。
4. 新 turn_id != old_turn_id。
5. 新 turn 正常产生 TTS。
```

指标：

```text
abort_to_stop_ms P95 <= 50ms
old_turn_audio_packets_after_abort = 0
old_turn_tts_chunks_after_abort = 0
old_turn_llm_tokens_after_abort = 0
```

### TC02：weak backchannel 不打断

输入：

```text
turn1: 长回答播放中
用户声音/asr_partial: "嗯"
```

期望：

```text
1. InterruptClassifier 输出 ignore。
2. 不发送 abort。
3. 不发送 tts stop。
4. old turn 继续播放。
5. history 不新增用户 turn。
```

覆盖词：

```text
嗯
好
对
哦
可以
行
```

### TC03：否定型插话必须打断

输入：

```text
播放中用户说:
"不是"
"不对"
"我说的是另一个"
"等等"
```

期望：

```text
全部 hard_interrupt。
全部 cancel old turn。
```

### TC04：soft interrupt 等 final 再决定

输入：

```text
asr_partial: "那个"
700ms 内没有 final
```

期望：

```text
1. 不立刻 hard cancel。
2. 可进入 soft_interrupt 状态。
3. 超时后没有 final，则恢复或继续 old turn。
4. history 不污染。
```

### TC05：false interruption recovery

输入：

```text
播放中检测到短噪声，误进入 soft_interrupt。
700-1200ms 内没有 ASR final。
```

期望：

```text
1. 不产生新 user message。
2. 不记录 assistant 完整未播放文本。
3. old turn 可继续，或至少不进入错误状态。
```

### TC06：TTSChunker 首包速度

输入 token：

```text
"好"
"的"
"，"
"我"
"来"
"简单"
"说"
"一下"
```

期望：

```text
在 150-300ms max_wait_ms 或达到 8-20 中文字后生成 first chunk。
first chunk 文本自然，不包含 JSON/tool 残留。
```

### TC07：TTSChunker 长句切分

输入：

```text
"你可以先打开设置然后找到网络选项接着选择你的 Wi-Fi 并输入密码最后点击连接。"
```

期望：

```text
1. 不等完整长句结束才开始 TTS。
2. chunk 按语义/标点/长度切分。
3. 单 chunk 不超过配置 max_chars。
```

### TC08：cancel 丢弃 pending TTS chunk

输入：

```text
LLM 已产生 3 个 chunk。
第 1 个已进入 TTS。
第 2、3 个仍 pending。
此时 hard interrupt。
```

期望：

```text
1. 第 2、3 个 pending chunk 被 drop。
2. drop_tts_chunks >= 2。
3. pending chunk 不进入 TTS queue。
```

### TC09：late LLM token 丢弃

输入：

```text
old turn cancel 后，fake LLM 继续 yield token。
```

期望：

```text
1. token 不进入 TTSChunker。
2. token 不进入 TTS queue。
3. dropped_llm_tokens > 0。
```

### TC10：late tool result 丢弃

输入：

```text
old turn 发起 slow tool。
用户 hard interrupt。
tool 在 cancel 后返回。
```

期望：

```text
1. tool future 被 cancel 或结果被 drop。
2. tool result 不进入 dialogue。
3. tool result 不触发 TTS。
4. dropped_tool_results >= 1。
```

### TC11：被打断后的 history truncation

输入：

```text
assistant planned text:
"第一步打开设置。第二步进入网络。第三步选择 Wi-Fi。"

已播放:
"第一步打开设置。"

用户 hard interrupt。
```

期望：

```text
dialogue assistant history 只包含:
"第一步打开设置。"

不能包含:
"第二步进入网络。第三步选择 Wi-Fi。"
```

### TC12：第二轮首音频延迟

输入：

```text
turn1 长回答
hard interrupt
turn2: "改成一句话"
```

期望：

```text
turn2_first_audio_ms 记录出来。
fake provider 下 P95 <= 500ms。
real provider 下记录实际值，不强行卡阈值。
```

### TC13：兼容旧客户端

配置：

```text
enable_turn_guard: false
```

期望：

```text
1. 普通对话仍能播放。
2. JSON control message 不强依赖 turn_id。
3. 老固件不崩。
```

### TC14：turn_id 覆盖率

期望：

```text
1. tts start 带 turn_id。
2. tts sentence_start 带 turn_id。
3. tts stop 带 turn_id。
4. stt message 带 current turn_id。
5. turn_metrics 带 turn_id。
```

注意：

```text
二进制 audio packet 当前不带 turn_id，短期接受。
server 端必须保证 stale audio 不发送。
```

### TC15：10 次稳定性回归

命令建议：

```bash
for i in {1..10}; do
  python codex-tools/offline_full_duplex_test/offline_full_duplex_test.py || exit 1
done
```

期望：

```text
10/10 PASS
stale audio = 0
old turn late output = 0
hard interrupt pass = 10/10
weak backchannel ignore pass = 10/10
```

## 7. 指标字段建议

每个 turn 输出 metrics：

```json
{
  "turn_id": "...",
  "cancel_reason": "hard_interrupt",
  "llm_first_token_ms": 123,
  "tts_first_audio_ms": 456,
  "abort_to_stop_ms": 12,
  "stale_audio_packets": 0,
  "dropped_llm_tokens": 0,
  "dropped_tts_chunks": 0,
  "dropped_tool_results": 0,
  "spoken_commit_chars": 18,
  "history_truncated": true,
  "interrupt_decision": "hard_interrupt",
  "interrupt_reason": "explicit_stop_phrase"
}
```

## 8. 完成定义

本阶段完成必须同时满足：

```text
1. Layer 1 单元测试全部 PASS。
2. Layer 2 fake provider 离线集成测试 10/10 PASS。
3. Layer 3 real provider smoke 至少 PASS 一组。
4. hard interrupt 能停旧 turn。
5. weak backchannel 不误打断。
6. late LLM/TTS/tool/audio output 不进入设备输出。
7. history truncation 有测试覆盖。
8. 报告更新到 codex-reports。
```

不满足以下任一条，都不算完成：

```text
只发 tts stop，但旧 turn late output 没测。
只测真实 provider，不测 fake deterministic case。
弱反馈会误打断。
history 仍记录完整未播放 assistant 回复。
测试依赖付费模型才能跑。
```

## 9. 推荐执行顺序

```text
1. 先写 TTSChunker 单元测试和实现。
2. 再写 InterruptClassifier 单元测试和实现。
3. 收敛 TurnManager metrics/drop counters。
4. 增加 spoken commit/history truncation 测试。
5. 扩展 offline_full_duplex_test.py。
6. 跑 10 次 fake provider 回归。
7. 跑 1 次 real provider smoke。
8. 更新 completion report。
9. push feature branch，不合 main。
```

