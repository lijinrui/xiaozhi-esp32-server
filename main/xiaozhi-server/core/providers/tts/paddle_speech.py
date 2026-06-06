import io
import re
import wave
import json
import base64
import asyncio
import threading
import websockets
import numpy as np
from datetime import datetime
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase



TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 3, 1.0, lambda v: round(float(v), 1)),
        ("ttsRate", "speed", 0, 3, 1.0, lambda v: round(float(v), 1)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url", "ws://192.168.1.10:8092/paddlespeech/tts/streaming")
        self.protocol = config.get("protocol", "websocket")
        self.timeout_seconds = int(config.get("tts_timeout", 60) or 60)
        self._synthesis_lock = threading.Lock()
        
        if config.get("private_voice"):
            self.spk_id = int(config.get("private_voice"))
        else:
            self.spk_id = int(config.get("spk_id", "0"))

        speed = config.get("speed", 1.0)
        self.speed = float(speed) if speed else 1.0
        
        volume = config.get("volume", 1.0)
        self.volume = float(volume) if volume else 1.0
        self.sample_rate = int(config.get("sample_rate", 24000) or 24000)
        self.fade_ms = float(config.get("fade_ms", 5) or 0)
        self.fade_in_ms = float(config.get("fade_in_ms", self.fade_ms) or 0)
        self.fade_out_ms = float(config.get("fade_out_ms", self.fade_ms) or 0)
        self.target_peak = float(config.get("target_peak", 0.92) or 0.92)
        self.first_chunk_leading_silence_ms = int(
            config.get("first_chunk_leading_silence_ms", 60) or 0
        )
        self.first_chunk_onset_gain = float(
            config.get("first_chunk_onset_gain", 1.2) or 1.0
        )
        self.first_chunk_onset_ms = int(config.get("first_chunk_onset_ms", 180) or 0)
        
        self.delete_audio_file = config.get("delete_audio", True)

        # 应用百分比调整（如果存在），否则使用公有化配置
        self._apply_percentage_params(config)

        if not self.delete_audio_file:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = config.get("save_path")
            if save_path:
                if not save_path.endswith('.wav'):
                    save_path = f"{save_path}_{timestamp}.wav"
                else:
                    other_path = save_path[:-4]
                    save_path = f"{other_path}_{timestamp}.wav"
                self.save_path = save_path
            else:
                self.save_path = f"./streaming_tts_{timestamp}.wav"
        else:
            self.save_path = None

    async def pcm_to_wav(self, pcm_data: bytes, sample_rate: int = 24000, num_channels: int = 1,
                         bits_per_sample: int = 16) -> bytes:
        """
        将 PCM 数据转换为 WAV 文件并返回字节数据
        :param pcm_data: PCM 数据（原始字节流）
        :param sample_rate: 音频采样率，默认为24000
        :param num_channels: 声道数，默认为单声道
        :param bits_per_sample: 每个样本的位数，默认为16
        :return: WAV 格式的字节数据
        """
        byte_data = self._condition_pcm(pcm_data, sample_rate)
        wav_io = io.BytesIO()

        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(num_channels)
            wav_file.setsampwidth(bits_per_sample // 8)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(byte_data.tobytes())

        return wav_io.getvalue()

    def _condition_pcm(self, pcm_data: bytes, sample_rate: int) -> np.ndarray:
        pcm = np.frombuffer(pcm_data, dtype=np.int16).copy()
        if pcm.size == 0:
            return pcm

        max_abs = int(np.max(np.abs(pcm.astype(np.int32))))
        target_abs = int(32767 * max(0.1, min(self.target_peak, 1.0)))
        if max_abs > target_abs:
            pcm = (pcm.astype(np.float32) * (target_abs / max_abs)).astype(np.int16)

        fade_in_samples = int(sample_rate * self.fade_in_ms / 1000)
        fade_out_samples = int(sample_rate * self.fade_out_ms / 1000)
        if fade_in_samples > 0 and pcm.size > fade_in_samples * 2:
            fade_in = np.linspace(0.0, 1.0, fade_in_samples, dtype=np.float32)
            pcm[:fade_in_samples] = (
                pcm[:fade_in_samples].astype(np.float32) * fade_in
            ).astype(np.int16)
        if fade_out_samples > 0 and pcm.size > fade_out_samples * 2:
            fade_out = np.linspace(1.0, 0.0, fade_out_samples, dtype=np.float32)
            pcm[-fade_out_samples:] = (
                pcm[-fade_out_samples:].astype(np.float32) * fade_out
            ).astype(np.int16)

        return pcm

    async def text_to_speak(self, text, output_file):
        text = self._normalize_text(text)
        if self.protocol == "websocket":
            with self._synthesis_lock:
                return await self.text_streaming(text, output_file)
        else:
            raise ValueError("Unsupported protocol. Please use 'websocket' or 'http'.")

    def _prepare_first_tts_audio(self, audio_bytes: bytes) -> bytes:
        if self.first_chunk_leading_silence_ms <= 0 or not audio_bytes:
            return audio_bytes

        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as source:
                params = source.getparams()
                pcm = source.readframes(source.getnframes())

            silence_frames = int(params.framerate * self.first_chunk_leading_silence_ms / 1000)
            silence = b"\x00" * silence_frames * params.nchannels * params.sampwidth
            pcm = self._boost_first_chunk_onset(pcm, params)

            wav_io = io.BytesIO()
            with wave.open(wav_io, "wb") as target:
                target.setparams(params)
                target.writeframes(silence + pcm)
            return wav_io.getvalue()
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"添加首段TTS前导静音失败: {exc}")
            return audio_bytes

    def _boost_first_chunk_onset(self, pcm_bytes: bytes, params) -> bytes:
        if self.first_chunk_onset_ms <= 0 or self.first_chunk_onset_gain <= 1.0:
            return pcm_bytes
        if params.sampwidth != 2:
            return pcm_bytes

        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).copy()
        if pcm.size == 0:
            return pcm_bytes

        onset_samples = int(params.framerate * self.first_chunk_onset_ms / 1000)
        onset_values = onset_samples * params.nchannels
        if onset_values <= 0:
            return pcm_bytes

        onset_values = min(onset_values, pcm.size)
        target_abs = int(32767 * max(0.1, min(self.target_peak, 1.0)))
        segment = pcm[:onset_values].astype(np.float32) * self.first_chunk_onset_gain
        segment = np.clip(segment, -target_abs, target_abs)
        pcm[:onset_values] = segment.astype(np.int16)
        return pcm.tobytes()

    @staticmethod
    def _normalize_text(text):
        if not text:
            return text
        text = re.sub(r"(\d)\s*[~～]\s*(\d)", r"\1到\2", text)
        text = re.sub(r"\.{2,}|…+", "，", text)
        text = re.sub(r"[~～]+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    async def text_streaming(self, text, output_file):
        try:
            # 使用 websockets 异步连接到 WebSocket 服务器
            async with websockets.connect(self.url) as ws:
                # 发送开始请求
                start_request = {
                    "task": "tts",
                    "signal": "start"
                }
                await ws.send(json.dumps(start_request))

                # 接收开始响应并提取 session_id
                start_response = await ws.recv()
                start_response = json.loads(start_response)  # 解析 JSON 响应
                if start_response.get("status") != 0:
                    raise Exception(f"连接失败: {start_response.get('signal')}")

                session_id = start_response.get("session")

                # 发送待合成的文本数据
                data_request = {
                    "text": text,
                    "spk_id": self.spk_id,
                }
                await ws.send(json.dumps(data_request))

                audio_chunks = b""
                try:
                    while True:
                        response = await asyncio.wait_for(ws.recv(), timeout=self.timeout_seconds)
                        response = json.loads(response)  # 解析 JSON 响应
                        status = response.get("status")

                        if status == 2:  # 最后一个数据包
                            break
                        else:
                            # 拼接音频数据（base64 编码的 PCM 数据）
                            audio_chunks += base64.b64decode(response.get("audio"))
                except asyncio.TimeoutError:
                    raise Exception(f"WebSocket 超时：等待音频数据超过 {self.timeout_seconds} 秒")

                # 将拼接后的 PCM 数据转换为 WAV 格式
                wav_data = await self.pcm_to_wav(audio_chunks, sample_rate=self.sample_rate)

                # 结束请求
                end_request = {
                    "task": "tts",
                    "signal": "end",
                    "session": session_id  # 会话 ID 必须与开始请求中的一致
                }
                await ws.send(json.dumps(end_request))

                # 接收结束响应避免服务抛出异常
                await ws.recv()

                # 根据配置决定是否保存文件
                if not self.delete_audio_file and self.save_path:
                    with open(self.save_path, "wb") as f:
                        f.write(wav_data)
                    logger.bind(tag=TAG).info(f"音频文件已保存到: {self.save_path}")
                
                # 返回或保存音频数据
                if output_file:
                    with open(output_file, "wb") as file_to_save:
                        file_to_save.write(wav_data)
                else:
                    return wav_data

        except Exception as e:
            raise Exception(f"Error during TTS WebSocket request: {e} while processing text: {text}")
