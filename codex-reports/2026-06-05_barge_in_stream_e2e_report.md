# 播放中插话打断流式 E2E 测试报告

测试时间：2026-06-05 17:00  
ASR：SherpaASRStream  
模式：流式，每 60ms 送入一帧音频  
音频目录：`main/xiaozhi-server/tmp/barge_in_audio`

## 结论

本轮共 12 个用例，11 个符合预期，1 个失败。

失败用例是 `explicit_stop`：测试音频文本为“停一下”，但流式 ASR 识别成“停一”。因为当前规则只把“停一下/停下/停止”等完整短语作为明确打断，`normal` 模式没有触发打断；`sensitive` 模式会打断。

这个结果说明短打断词在流式 ASR 下存在被截短的风险。后续建议用真人录音复测，并考虑对“停一”这类高置信截短结果加规则容错。

## 耗时说明

`asr_ms`：离线跑完整条音频的 ASR 计算耗时，不是用户感知等待时间。  
`rule_us`：打断规则判定耗时，微秒级，可忽略。  
`cancel_ms`：音频时间轴上首次触发打断的位置，更接近真实用户说话后多久会触发打断。

## 结果明细

| case | 识别文本 | 判定 | 原因 | off | normal | sensitive | ASR计算耗时 | 规则耗时 | 首次打断位置 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- |
| backchannel_um | 嗯 | ignore | backchannel | 否 | 否 | 否 | 34.6ms | 160.88us | - | OK |
| backchannel_ok | 好的 | ignore | backchannel | 否 | 否 | 否 | 35.6ms | 19.58us | - | OK |
| noise_placeholder | 咳嗽 | ignore | noise_placeholder | 否 | 否 | 否 | 34.9ms | 20.08us | - | OK |
| explicit_stop | 停一 | soft_interrupt | short_candidate | 否 | 否 | 是 | 32.1ms | 23.54us | sensitive:565ms | FAIL |
| explicit_wait | 等等我重新问 | hard_interrupt | explicit_interrupt:等等 | 否 | 是 | 是 | 70.3ms | 9.40us | normal:660ms, sensitive:660ms | OK |
| correction_wrong_target | 不是这个我问的是天气 | hard_interrupt | explicit_interrupt:不是 | 否 | 是 | 是 | 87.4ms | 9.09us | normal:660ms, sensitive:660ms | OK |
| question_weather | 那明天天气怎么样 | hard_interrupt | question_like | 否 | 是 | 是 | 70.6ms | 9.76us | normal:1319ms, sensitive:660ms | OK |
| question_rephrase | 能不能换一种说法 | hard_interrupt | question_like | 否 | 是 | 是 | 67.2ms | 9.39us | normal:1260ms, sensitive:660ms | OK |
| wake_word | 小智小智 | hard_interrupt | wake_word | 否 | 是 | 是 | 49.2ms | 9.58us | normal:660ms, sensitive:660ms | OK |
| hesitation_short_partial | 那个 | soft_interrupt | short_candidate | 否 | 否 | 是 | 32.2ms | 20.46us | sensitive:419ms | OK |
| ordinary_final_pause | 我觉得这个回答还可以 | soft_interrupt | final_enough | 否 | 否 | 是 | 85.4ms | 7.03us | sensitive:660ms | OK |
| ordinary_final_environment | 今天外面声音有点大 | soft_interrupt | final_enough | 否 | 否 | 是 | 70.3ms | 9.14us | sensitive:660ms | OK |

## 复现命令

```bash
main/xiaozhi-server/venv/bin/python main/xiaozhi-server/performance_tester/performance_tester_barge_in_e2e.py \
  --asr SherpaASRStream \
  --stream \
  --frame-ms 60 \
  --repeat 1
```

重新生成测试音频：

```bash
main/xiaozhi-server/venv/bin/python main/xiaozhi-server/performance_tester/performance_tester_barge_in_e2e.py \
  --generate-audio \
  --asr SherpaASRStream \
  --stream \
  --frame-ms 60 \
  --repeat 1
```
