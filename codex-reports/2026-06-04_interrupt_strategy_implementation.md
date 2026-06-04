# 2026-06-04 Server Interrupt Strategy 第一阶段实现记录

分支：`review/server-offline-barge-in-turns`

推送目标：`origin/feat/server-offline-barge-in-turns`

## 1. 本次目标

把播放中 ASR 分段从“收到就直接 abort”升级为轻量规则判断：

```text
backchannel / 噪声:
  ignore，不打断，不进入 LLM

短而不完整的 partial:
  soft_interrupt，不立刻打断，不进入 LLM

明确打断 / 否定 / 纠正 / 新问题:
  hard_interrupt，复用原有 abort/cancel/tts stop 链路
```

本次不改变 ESP32 外部协议，不改变 TTS/ASR/LLM provider 接口。

## 2. 新增模块

```text
main/xiaozhi-server/core/voice/frames.py
main/xiaozhi-server/core/voice/turn_strategies.py
main/xiaozhi-server/core/voice/interrupt_classifier.py
main/xiaozhi-server/core/voice/__init__.py
```

### 2.1 frames.py

新增 server 内部 frame 类型：

```text
VoiceFrame
UserStartedSpeakingFrame
InterruptCandidateFrame
InterruptionFrame
TurnCancelFrame
```

这些 frame 只用于 server 内部，不下发给固件。

### 2.2 turn_strategies.py

新增：

```text
RuleBasedTurnStartStrategy
TurnStartDecision
TurnStartResult
```

第一版规则：

```text
“嗯/好/对/哦/可以/行” => IGNORE
“停一下/等等/别说了/不是/不对/我问的是” => START
短且不完整 => CANDIDATE
问题型或足够长 final => START
```

### 2.3 interrupt_classifier.py

新增：

```text
RuleBasedInterruptClassifier
InterruptDecision
InterruptResult
```

输出：

```text
IGNORE
SOFT_INTERRUPT
HARD_INTERRUPT
RESUME_PREVIOUS（预留）
```

## 3. 接入点

修改：

```text
main/xiaozhi-server/core/handle/receiveAudioHandle.py
```

播放中收到 ASR 分段时：

```text
enable_interrupt_classifier=true:
  IGNORE/SOFT_INTERRUPT => return，不打断，不进入 LLM
  HARD_INTERRUPT => handleAbortMessage(reason="hard_interrupt:...")

enable_interrupt_classifier=false:
  保持旧行为，收到分段即 handleAbortMessage()
```

保留原有：

```text
interrupt_tts_on_segment=false:
  不打断，继续走旧配置路径
```

修改：

```text
main/xiaozhi-server/core/handle/abortHandle.py
```

`handleAbortMessage(conn, reason="client_abort")` 增加可选 reason，旧调用不受影响。

## 4. 配置

修改：

```text
main/xiaozhi-server/config.yaml
```

在 ASR 配置中新增：

```yaml
enable_interrupt_classifier: true
```

说明：

```text
false 时退回“播放中收到 ASR 分段即打断”的旧行为。
```

## 5. 测试

新增：

```text
tests/test_voice_interrupts.py
```

覆盖：

```text
1. backchannel 不触发 turn start。
2. 噪声占位文本不触发 turn start。
3. 明确打断词触发 START。
4. 问题型文本触发 START。
5. 短 partial 触发 CANDIDATE。
6. 非 speaking 时不产生 interruption。
7. speaking + “嗯” => IGNORE。
8. speaking + “停一下” => HARD_INTERRUPT。
9. speaking + “不是，我问的是天气” => HARD_INTERRUPT。
10. speaking + 短 partial => SOFT_INTERRUPT。
11. wake word => HARD_INTERRUPT。
```

验证命令：

```bash
python3 -m unittest tests/test_voice_interrupts.py
PYTHONPYCACHEPREFIX=/private/tmp/xiaozhi-pycache python3 -m py_compile \
  main/xiaozhi-server/core/voice/frames.py \
  main/xiaozhi-server/core/voice/turn_strategies.py \
  main/xiaozhi-server/core/voice/interrupt_classifier.py \
  main/xiaozhi-server/core/handle/receiveAudioHandle.py \
  main/xiaozhi-server/core/handle/abortHandle.py
git diff --check
```

结果：

```text
12 tests PASS
py_compile PASS
git diff --check PASS
```

## 6. 兼容性

兼容项：

```text
1. ESP32 websocket 协议未改变。
2. 旧固件 abort 仍走 handleAbortMessage(conn)。
3. server 下发 tts stop 逻辑未改变。
4. turn_id 仍是可选字段。
5. ASR/TTS/LLM provider 接口未改变。
6. enable_interrupt_classifier=false 可退回旧打断行为。
7. interrupt_tts_on_segment=false 继续保持不打断。
```

## 7. 本次未做

```text
1. 没有实现 spoken commit / history truncation。
2. 没有实现 TTSChunker。
3. 没有把 TurnManager 从 ConnectionHandler 完全抽离。
4. 没有接入 ASR partial 的实时流入口；当前接的是 startToChat 的 ASR 分段入口。
5. 没有做真实 provider smoke。
6. 没有做真机 AEC/播放中插话验证。
```

## 8. 下一步建议

```text
1. 增加 fake integration test：
   speaking + “嗯” 不发 tts stop；
   speaking + “停一下” 发 tts stop。

2. 接入真正 ASR partial 入口：
   partial 只进 InterruptClassifier；
   final 才进入 LLM turn。

3. 做 TTSChunker：
   LLM token -> chunk -> TTS queue。

4. 做 spoken commit：
   被打断后 history 只保留已播放文本。
```

