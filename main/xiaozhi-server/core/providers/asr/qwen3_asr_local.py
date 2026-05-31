import os
import time
import asyncio
import numpy as np
import opuslib_next

from typing import Optional, Tuple, List
from config.logger import setup_logging
from core.handle.sendAudioHandle import send_display_message
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    """Qwen3-ASR 本地模型流式 ASR Provider（MLX 版本，仅 Apple Silicon Mac）

    参照 doubao_stream / xunfei_stream 的实现：
    每次收到 opus 音频包（60ms）立即 decode 并 feed 给模型，不累积到固定 chunk。
    """

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.model_path = config.get("model_path")
        if not self.model_path:
            raise ValueError("Qwen3-ASR-Local 需要配置 model_path")

        self.language = config.get("language", None)
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

        # 流式参数
        self.chunk_size_sec = float(config.get("chunk_size_sec", 0.3))
        self.max_context_sec = float(config.get("max_context_sec", 30.0))
        self.sample_rate = 16000
        self.chunk_samples = int(self.chunk_size_sec * self.sample_rate)

        # 中间结果推送
        self.enable_interim_results = config.get("enable_interim_results", True)
        self.interim_result_interval = float(config.get("interim_result_interval", 0.15))

        # 运行时状态
        self.stream_state = None
        self.is_processing = False
        self._is_stopping = False
        self._finalizing = False
        self.text = ""
        self.last_interim_text = ""
        self.last_interim_time = 0.0
        self.forward_task = None
        self.audio_buffer = np.array([], dtype=np.float32)
        self.decoder_opus = opuslib_next.Decoder(self.sample_rate, 1)
        self._conn = None

        os.makedirs(self.output_dir, exist_ok=True)
        logger.bind(tag=TAG).info(
            f"Qwen3-ASR 流式识别器初始化完成: {self.model_path}"
        )

    # ------------------------------------------------------------------ #
    # 音频接收（累积到 chunk_size 再 feed）
    # ------------------------------------------------------------------ #

    async def receive_audio(self, conn: "ConnectionHandler", audio, audio_have_voice):
        # 先调用父类缓存音频（供声纹识别等使用）
        await super().receive_audio(conn, audio, audio_have_voice)
        self._conn = conn

        # 语音开始且没有活跃 stream -> 创建新 stream
        if audio_have_voice and not self.is_processing and not self._is_stopping:
            try:
                from mlx_qwen3_asr.streaming import init_streaming

                kwargs = {}
                if self.language:
                    kwargs["language"] = self.language

                self.stream_state = await asyncio.to_thread(
                    init_streaming,
                    model=self.model_path,
                    chunk_size_sec=self.chunk_size_sec,
                    max_context_sec=self.max_context_sec,
                    sample_rate=self.sample_rate,
                    **kwargs,
                )
                self.is_processing = True
                self.text = ""
                self.last_interim_text = ""
                self.last_interim_time = 0.0
                self._finalizing = False
                self.audio_buffer = np.array([], dtype=np.float32)
                self.forward_task = asyncio.create_task(self._recognize_loop(conn))
                logger.bind(tag=TAG).debug("创建新的 Qwen3-ASR 流式识别 stream")
            except Exception as e:
                logger.bind(tag=TAG).error(f"创建流式识别 stream 失败: {e}")
                self.is_processing = False
                return

        # 有活跃 stream 且未停止 -> 实时喂 opus 音频（累积到 chunk_size 再 feed）
        if self.stream_state and self.is_processing and not self._is_stopping:
            try:
                pcm = self.decoder_opus.decode(audio, 960)
                if pcm:
                    samples = (
                        np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                        / 32768.0
                    )
                    self.audio_buffer = np.concatenate([self.audio_buffer, samples])

                    # 累积到 chunk 大小就 feed
                    while len(self.audio_buffer) >= self.chunk_samples:
                        chunk = self.audio_buffer[:self.chunk_samples]
                        self.audio_buffer = self.audio_buffer[self.chunk_samples:]
                        self.stream_state = await asyncio.to_thread(
                            self._feed_audio, chunk, self.stream_state
                        )
            except Exception as e:
                logger.bind(tag=TAG).debug(f"喂音频失败: {e}")

        # VAD 停止信号兜底
        if (
            self.is_processing
            and conn.client_voice_stop
            and not self._is_stopping
        ):
            logger.bind(tag=TAG).debug("VAD 检测到语音结束，触发流式识别停止")
            asyncio.create_task(self._send_stop_request())

    def _feed_audio(self, chunk: np.ndarray, state):
        from mlx_qwen3_asr.streaming import feed_audio
        return feed_audio(chunk, state)

    # ------------------------------------------------------------------ #
    # 后台识别循环
    # ------------------------------------------------------------------ #

    async def _recognize_loop(self, conn: "ConnectionHandler"):
        try:
            while (
                self.stream_state
                and self.is_processing
                and not conn.stop_event.is_set()
            ):
                current_text = self.stream_state.text if self.stream_state else ""
                if current_text and current_text != self.text:
                    self.text = current_text
                    logger.bind(tag=TAG).debug(f"流式中间结果: {self.text}")
                    await self._send_interim_result(conn, self.text)

                # 停止信号已发出且 buffer 已清空 -> finalize
                if self._is_stopping and len(self.audio_buffer) < self.chunk_samples:
                    final_text = await self._finalize()
                    logger.bind(tag=TAG).info(f"流式识别结束: {final_text}")

                    if final_text and conn.asr_audio and len(conn.asr_audio) > 0:
                        self._finalizing = True
                        await self.handle_voice_stop(conn, conn.asr_audio)
                    break

                await asyncio.sleep(0.05)  # 50ms 检查一次

        except Exception as e:
            logger.bind(tag=TAG).error(f"识别循环出错: {e}", exc_info=True)
        finally:
            self.is_processing = False
            self.stream_state = None
            self._is_stopping = False
            self._finalizing = False
            self.audio_buffer = np.array([], dtype=np.float32)
            try:
                conn.reset_audio_states()
            except Exception:
                pass

    async def _finalize(self) -> str:
        """结束流式识别，返回最终文本"""
        from mlx_qwen3_asr.streaming import finish_streaming

        # 先把剩余 buffer feed 进去
        if len(self.audio_buffer) > 0 and self.stream_state:
            self.stream_state = await asyncio.to_thread(
                self._feed_audio, self.audio_buffer, self.stream_state
            )
            self.audio_buffer = np.array([], dtype=np.float32)

        if self.stream_state:
            self.stream_state = await asyncio.to_thread(
                finish_streaming, self.stream_state
            )
            self.text = self.stream_state.text if self.stream_state else ""
            return self.text
        return ""

    # ------------------------------------------------------------------ #
    # 停止与清理
    # ------------------------------------------------------------------ #

    async def _send_stop_request(self):
        if not self.stream_state or not self.is_processing:
            self._is_stopping = False
            return
        self._is_stopping = True
        logger.bind(tag=TAG).debug("收到停止请求，等待识别循环结束")

    async def _send_interim_result(self, conn: "ConnectionHandler", text: str):
        if not self.enable_interim_results or not text:
            return
        now = time.monotonic()
        if text == self.last_interim_text:
            return
        if now - self.last_interim_time < self.interim_result_interval:
            return
        self.last_interim_text = text
        self.last_interim_time = now
        try:
            await send_display_message(conn, text)
        except Exception as e:
            logger.bind(tag=TAG).debug(f"发送流式中间结果失败: {e}")

    def reset_stream_state(self):
        self.is_processing = False
        self._is_stopping = False
        self.stream_state = None
        self.text = ""
        self.last_interim_text = ""
        self.last_interim_time = 0.0
        self._finalizing = False
        self.audio_buffer = np.array([], dtype=np.float32)

    # ------------------------------------------------------------------ #
    # 文件与非流式修正
    # ------------------------------------------------------------------ #

    def requires_file(self) -> bool:
        return True

    def prefers_temp_file(self) -> bool:
        return True

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """流式模式下：
        - 前端看到的是 _recognize_loop 推送的中间结果（低延迟）
        - LLM 收到的是非流式 transcribe 重新识别的结果（高质量，修复断句）
        """
        try:
            # 优先用完整音频走非流式 transcribe 修正
            if artifacts and artifacts.temp_path:
                from mlx_qwen3_asr import transcribe as asr_transcribe

                kwargs = {}
                if self.language:
                    kwargs["language"] = self.language

                start = time.time()
                result = await asyncio.to_thread(
                    asr_transcribe,
                    artifacts.temp_path,
                    model=self.model_path,
                    **kwargs,
                )
                final_text = result.text
                logger.bind(tag=TAG).info(
                    f"非流式修正耗时: {time.time()-start:.3f}s | 结果: {final_text}"
                )
                return final_text, artifacts.file_path

            # 兜底：返回 streaming 结果
            result = self.text
            self.text = ""
            return result, None

        except Exception as e:
            logger.bind(tag=TAG).error(f"非流式修正失败: {e}", exc_info=True)
            # 降级：返回 streaming 结果
            result = self.text
            self.text = ""
            return result, None

    def stop_ws_connection(self):
        if self.is_processing and not self._finalizing:
            asyncio.create_task(self._send_stop_request())

    async def close(self):
        if self.forward_task:
            self.forward_task.cancel()
            try:
                await self.forward_task
            except asyncio.CancelledError:
                pass
            self.forward_task = None

        self.is_processing = False
        self._is_stopping = False
        self._finalizing = False
        self.stream_state = None
        self.audio_buffer = np.array([], dtype=np.float32)

        if hasattr(self, "decoder_opus") and self.decoder_opus:
            try:
                del self.decoder_opus
                self.decoder_opus = None
            except Exception:
                pass

        logger.bind(tag=TAG).debug("Qwen3-ASR 流式识别器资源已释放")
