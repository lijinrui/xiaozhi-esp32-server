# Offline Barge-in Test

This tool verifies the server-side interruption path without ESP32 hardware.

It uses text-only `listen/detect` messages, so it does not need a microphone,
Opus encoder, or ASR audio injection. It validates:

- first turn triggers STT/LLM/TTS
- TTS/audio begins
- client sends `abort`
- server sends `tts stop`
- stale audio after abort stays below a configured threshold
- second turn can start after interruption

## Prerequisites

Run it with the same Python environment used by `main/xiaozhi-server`, because
it needs the `websockets` package.

The xiaozhi server must already be running:

```bash
cd /Users/wangjinyuan1/Proj/xiaozhi/xiaozhi-esp32-server/main/xiaozhi-server
python app.py
```

## Run

From `/Users/wangjinyuan1/Proj/xiaozhi`:

```bash
python codex-tools/offline_barge_in_test/offline_barge_in_test.py \
  --url ws://127.0.0.1:8000/xiaozhi/v1/
```

Custom turns:

```bash
python codex-tools/offline_barge_in_test/offline_barge_in_test.py \
  --turn1 "请用三句话介绍一下小智机器人，并且慢慢说。" \
  --turn2 "停一下，改成只说一句话。"
```

## Result

The script prints timing metrics:

```text
turn1 -> TTS start
turn1 -> first audio
abort -> tts stop
turn2 -> TTS start
binary packets after abort before turn2
```

The most important value is:

```text
binary packets after abort before turn2
```

If this number is high, the server is still sending stale audio after the client
interrupts. The default tolerance is `2`.

## Limitations

This is a server-side cancel-path test. It does not verify:

- ASR quality
- microphone capture
- device playback buffer clearing
- AEC
- real barge-in VAD

Those should be tested later with a wav/Opus injection client and real hardware.

