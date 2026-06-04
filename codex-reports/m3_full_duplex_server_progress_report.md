# M3 Full Duplex Server Progress Report

生成时间：2026-06-04 12:45 CST

分支：`feat/server-full-duplex-experience`

## 1. 本次完成范围

基于 `origin/feat/server-offline-barge-in-turns` 的 M3 plan，完成 server 全双工状态机第一批可测核心：

```text
TurnManager 独立状态管理
TTSChunker 流式 token 切分
InterruptClassifier 轻量插话分类
spoken commit / interrupted history truncation 基础路径
offline_full_duplex_test 集成测试
```

## 2. 主要代码变更

新增模块：

```text
core/voice/turn_manager.py
core/voice/tts_chunker.py
core/voice/interrupt_classifier.py
```

新增测试：

```text
tests/test_turn_manager.py
tests/test_tts_chunker.py
tests/test_interrupt_classifier.py
tests/test_spoken_commit_history.py
codex-tools/offline_full_duplex_test/offline_full_duplex_test.py
```

接入点：

```text
core/connection.py
core/handle/receiveAudioHandle.py
core/handle/sendAudioHandle.py
core/handle/abortHandle.py
config.yaml
```

## 3. 行为变化

### 3.1 TurnManager

`ConnectionHandler` 原有 turn API 保持不变，但内部委托给 `TurnManager`：

```text
begin_turn
cancel_current_turn
is_current_turn
register_sentence_turn
register_tool_future
wait_turn_future
log_turn_metrics
```

新增 metrics：

```text
stale_audio_packets
dropped_llm_tokens
dropped_tts_chunks
dropped_tool_results
spoken_commit_chars
history_truncated
interrupt_decision
interrupt_reason
```

为了兼容已有测试，`turn_metrics` 仍保留 `stale_packets` 字段。

### 3.2 TTSChunker

LLM token 不再每个 delta 直接进入 TTS queue，而是先经过 chunker：

```text
首包：8-20 字或 max_wait_ms=250
后续：优先标点切分，最大 60 字
cancel：pending chunk 计入 dropped_tts_chunks，不进入 TTS queue
```

这会牺牲一小段 fake provider 首包速度，换取更自然的 TTS 输入。

### 3.3 InterruptClassifier

播放中收到 ASR 文本时先分类：

```text
hard_interrupt: 停一下 / 等等 / 不是 / 不对 / 我说的是 / wake word / 明显新问题
ignore: 嗯 / 好 / 对 / 哦 / 可以 / 行
soft_interrupt: 其他短文本，先不取消旧 turn
```

当前接入策略：

```text
hard_interrupt -> handleAbortMessage
ignore -> return，不创建新 turn
soft_interrupt -> return，不创建新 turn
```

### 3.4 Spoken Commit

server 在 `sentence_start` 对应音频路径开始时记录 spoken text。abort 后：

```text
已大概率播放的文本 -> 写入 assistant history
完整未播放回答 -> 不写入 history
history_truncated -> true
```

这仍是 server 侧估算，不依赖设备 ack。

## 4. 验证结果

单元测试：

```bash
PYTHONPATH=main/xiaozhi-server python3 -m unittest discover -s main/xiaozhi-server/tests -p 'test_*.py'
```

结果：

```text
Ran 11 tests
OK
```

语法检查：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/xiaozhi-pycache python3 -m py_compile ...
```

结果：PASS。

offline full-duplex 集成测试：

```bash
main/xiaozhi-server/venv/bin/python codex-tools/offline_full_duplex_test/offline_full_duplex_test.py \
  --url ws://127.0.0.1:8010/xiaozhi/v1/
```

结果：

```text
hard_interrupt: PASS
weak_backchannel: PASS
failures: 0
PASS
```

连续 10 次 offline full-duplex 回归：

```text
PASS: 10/10
hard_interrupt: 10/10 PASS
weak_backchannel: 10/10 PASS
```

旧 offline barge-in 回归：

```text
abort -> tts stop: 1.0ms
server abort_to_stop_ms: 0.3
server stale_packets: 0
binary packets after abort before turn2: 0
PASS
```

真实 provider smoke：

```text
provider: MiniMaxLLM + EdgeTTS + Qwen3ASRLocal + function_call intent
abort -> tts stop: 1.6ms
server abort_to_stop_ms: 0.3
server stale_packets: 0
binary packets after abort before turn2: 0
turn2 -> first audio: 4411.1ms
PASS
```

## 5. 未完成项

仍未覆盖：

```text
PC wav/Opus 注入测试
真实 ASR binary audio -> final text 链路
真机 tts stop 后停播/清 buffer/继续采音
设备端播放 ack 驱动的精确 spoken commit
```

当前 spoken commit 是 server 估算版，适合作为下一阶段真机 ack 前的中间实现。

## 6. 注意事项

1. `TTSChunker` 会让 fake provider 首音频延迟从约 190ms 上升到约 360ms，仍满足 fake provider P95 <= 500ms 的目标。
2. `enable_turn_guard: false` 仍是兼容回退开关。
3. 工作区中仍有两个与本次任务无关的既有本地改动未纳入本次提交：

```text
main/xiaozhi-server/config/assets/wakeup_words/ed76d459636c2481aec828516c1b4f54.wav
main/xiaozhi-server/core/providers/tts/GPT-SoVITS-V3
```

