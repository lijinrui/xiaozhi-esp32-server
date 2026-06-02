# xiaozhi 本地运行语音链路方案

生成时间：2026-06-02

目标：尽量不依赖讯飞、豆包、阿里等付费 API，先跑通：

```text
本地/局域网 ASR -> 本地 LLM streaming -> 本地/局域网 TTS -> ESP32 播放
```

并为后续“播放中监听 -> 用户插话 -> cancel”打基础。

## 1. 结论

本地方案可以跑起来，但体验分层明显：

```text
最快跑通：本地 ASR + 本地 LLM + EdgeTTS
完全本地：本地 ASR + 本地 LLM + GPT-SoVITS_V3 / IndexStreamTTS / PaddleSpeechTTS
低延迟较好：FunASRServer/SherpaASRStream + Ollama/LMStudio + GPT-SoVITS_V3 或 IndexStreamTTS
```

完全本地的最大瓶颈通常不是 ASR，也不是 LLM，而是 TTS 的首包延迟、自然度和流式输出。

## 2. 当前仓库已支持的本地选项

### 2.1 ASR

已支持：

```text
FunASR
FunASRServer
SherpaASR
SherpaParaformerASR
SherpaASRStream
VoskASR
Qwen3ASRLocal
```

推荐优先级：

```text
1. Qwen3ASRLocal（Apple Silicon / M3 Ultra 优先测试）
2. SherpaASRStream
3. FunASRServer
4. FunASR local
5. VoskASR
```

说明：

```text
Qwen3ASRLocal:
  Qwen3-ASR 官方开源模型支持 streaming inference、async serving、vLLM 后端等能力。
  当前项目 Provider 已按 InterfaceType.STREAM 接入，并通过 MLX 在 Apple Silicon 上运行。
  适合 M3 Ultra 96G 作为高准确率本地 ASR 主线优先测试。
  需要重点实测 chunk_size_sec、final 修正耗时、barge-in 时延。

SherpaASRStream:
  真流式，适合后续 barge-in 和 interim display。
  工程确定性强，端点检测成熟，适合做低延迟对照组。

FunASRServer:
  独立部署，成熟，中文好，适合 CPU/GPU 服务器。

FunASR local:
  容易跑，适合先验证，但不是最佳低延迟流式。

VoskASR:
  完全离线，资源要求低，但中文效果和标点较弱。
```

## 3. LLM

已支持：

```text
OllamaLLM
LMStudioLLM
XinferenceLLM
XinferenceSmallLLM
```

推荐优先级：

```text
1. OllamaLLM
2. LMStudioLLM
3. XinferenceLLM
```

建议模型：

```text
低资源/快速响应：
  qwen2.5:3b
  qwen3:4b / qwen3:8b，注意使用 /no_think

平衡质量和速度：
  qwen2.5:7b
  qwen2.5:14b，如果机器足够强

不建议第一版使用：
  大型推理模型
  长思考模型
  72B 本地模型
```

原因：

```text
语音体验优先看 first token latency，而不是最强推理能力。
本项目 Ollama Provider 已使用 stream=True，适合 LLM streaming。
```

## 4. TTS

已支持的本地/局域网候选：

```text
GPT_SOVITS_V2
GPT_SOVITS_V3
FishSpeech
PaddleSpeechTTS
IndexStreamTTS
EdgeTTS
```

推荐优先级：

```text
1. GPT_SOVITS_V3
2. IndexStreamTTS
3. PaddleSpeechTTS
4. EdgeTTS
5. FishSpeech
```

说明：

```text
GPT_SOVITS_V3:
  当前 Provider 有 to_tts_stream 实现，支持流式响应转 Opus，适合本地流式体验。

IndexStreamTTS:
  当前 Provider 是 SINGLE_STREAM，按 PCM 流式处理，适合低延迟尝试，但依赖外部 index-tts-vllm 服务。

PaddleSpeechTTS:
  配置支持 WebSocket streaming，但当前 Provider 更偏“收到全部音频后返回”，不是最佳首包方案。
  如果想用它做低延迟，需要改 Provider，把服务端 chunk 边收边转 Opus 发给设备。

EdgeTTS:
  免费、最容易跑、声音自然，但不是本地，依赖微软服务；可作为免付费过渡方案。

FishSpeech:
  声音效果可好，但部署和首包延迟通常更重，第一版不优先。
```

## 5. 推荐本地组合

### 5.1 最快免付费跑通

```text
ASR: FunASR local
LLM: OllamaLLM / qwen2.5:7b 或 qwen3:8b
TTS: EdgeTTS
```

优点：

```text
最容易启动。
成本低。
适合先验证 final ASR -> LLM streaming -> TTS playback。
```

缺点：

```text
EdgeTTS 不是本地。
TTS 首包和可取消性不如真正本地流式 TTS。
```

### 5.2 完全本地可打断基线

```text
ASR: Qwen3ASRLocal 或 SherpaASRStream
LLM: OllamaLLM / qwen2.5:7b
TTS: GPT_SOVITS_V3
```

优点：

```text
全链路本地/局域网。
Qwen3ASRLocal 准确率潜力高，SherpaASRStream 端点/实时性确定性强。
LLM 真流式。
TTS Provider 已有流式转 Opus 实现。
```

缺点：

```text
GPT-SoVITS 部署较重。
需要调 chunk、采样率、首包延迟。
```

### 5.3 本地低延迟探索组合

```text
ASR: FunASRServer 或 SherpaASRStream
LLM: LMStudioLLM / OllamaLLM
TTS: IndexStreamTTS
```

优点：

```text
IndexStreamTTS 当前 Provider 按 PCM streaming 处理，理论上适合低延迟。
```

缺点：

```text
需要额外部署 index-tts-vllm 服务。
实际效果取决于本地 GPU/服务实现。
```

## 6. 配置建议

在 `main/xiaozhi-server/data/.config.yaml` 中覆盖：

```yaml
selected_module:
  VAD: SileroVAD
  ASR: SherpaASRStream
  LLM: OllamaLLM
  VLLM: ChatGLMVLLM
  TTS: GPT_SOVITS_V3
  Memory: nomem
  Intent: function_call
```

Ollama：

```yaml
LLM:
  OllamaLLM:
    type: ollama
    model_name: qwen2.5:7b
    base_url: http://localhost:11434
```

SherpaASRStream：

```yaml
ASR:
  Qwen3ASRLocal:
    type: qwen3_asr_local
    model_path: /path/to/Qwen3-ASR-1.7B-8bit
    output_dir: tmp/
    language: Chinese
    chunk_size_sec: 1.0
    max_context_sec: 30.0
    enable_interim_results: true
    interim_result_interval: 0.15

  SherpaASRStream:
    type: sherpa_onnx_stream
    model_dir: models/sherpa-onnx-streaming-paraformer-bilingual-zh-en-2023-06-21
    model_type: paraformer
    encoder: encoder.int8.onnx
    decoder: decoder.int8.onnx
    tokens: tokens.txt
    output_dir: tmp/
    enable_interim_results: true
    enable_early_llm: false
    interrupt_tts_on_segment: false
    defer_tts_start_until_audio: true
    enable_endpoint_detection: true
```

GPT-SoVITS V3：

```yaml
TTS:
  GPT_SOVITS_V3:
    type: gpt_sovits_v3
    api_version: v2
    url: "http://127.0.0.1:9880/tts"
    output_dir: tmp/
    text_language: "auto"
    prompt_language: "zh"
    refer_wav_path: "demo.wav"
    prompt_text: ""
    streaming_mode: 2
    media_type: "wav"
    min_chunk_length: 16
```

## 7. 机器资源建议

轻量验证：

```text
MacBook / PC CPU
ASR: FunASR local 或 SherpaASRStream
LLM: qwen2.5:3b / qwen3:4b
TTS: EdgeTTS 或 PaddleSpeechTTS
```

较好体验：

```text
Apple Silicon 16GB+ 或 NVIDIA GPU
ASR: SherpaASRStream / FunASRServer
LLM: qwen2.5:7b / qwen3:8b
TTS: GPT_SOVITS_V3 / IndexStreamTTS
```

更好体验：

```text
NVIDIA GPU 12GB+
ASR 服务独立
LLM 服务独立
TTS 服务独立
```

## 8. 本地方案对全双工目标的影响

本地方案能跑，但要注意：

```text
ASR 本地可行。
LLM 本地可行。
TTS 本地是主要瓶颈。
```

第一阶段建议优先做到：

```text
final ASR -> 本地 LLM streaming -> 本地/免费 TTS
设备端 stop/clear
播放中 VAD 打断
```

如果本地 TTS 首包太慢，可以先用 EdgeTTS 过渡，把架构打通。架构稳定后再换 GPT-SoVITS_V3 或 IndexStreamTTS。

## 9. 参考资料

- Ollama OpenAI-compatible API: https://docs.ollama.com/api/openai-compatibility
- sherpa-onnx: https://github.com/k2-fsa/sherpa-onnx
- FunASR: https://github.com/modelscope/FunASR
- FunASR documentation: https://modelscope.github.io/FunASR/index.html
- PaddleSpeech: https://github.com/PaddlePaddle/PaddleSpeech
