import asyncio
import os
import numpy as np
import opuslib_next
from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from typing import Optional, Tuple, List, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    """Sherpa-ONNX 流式（Online / Streaming）ASR Provider

    支持模型类型：
      - paraformer:   encoder + decoder
      - transducer / zipformer: encoder + decoder + joiner

    需要流式模型（streaming model），不能用 sense_voice 等 Offline 模型。
    """

    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.output_dir = config.get("output_dir", "tmp/")
        self.model_type = config.get("model_type", "paraformer")
        self.delete_audio_file = delete_audio_file

        # 模型文件配置（支持绝对路径或相对于 model_dir）
        self.model_dir = config.get("model_dir")
        self.encoder = config.get("encoder")
        self.decoder = config.get("decoder")
        self.joiner = config.get("joiner")
        self.tokens = config.get("tokens")

        # 端点检测（endpoint detection）配置
        self.rule1_min_trailing_silence = config.get(
            "rule1_min_trailing_silence", 2.4
        )
        self.rule2_min_trailing_silence = config.get(
            "rule2_min_trailing_silence", 1.2
        )
        self.rule3_min_utterance_length = config.get(
            "rule3_min_utterance_length", 300
        )

        # 运行时状态
        self.recognizer = None
        self.stream = None
        self.is_processing = False
        self._is_stopping = False
        self.forward_task = None
        self.text = ""
        self.decoder_opus = opuslib_next.Decoder(16000, 1)
        self._conn = None

        self._init_recognizer()

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #

    def _resolve_path(self, filename: str) -> str:
        if not filename:
            return ""
        if os.path.isabs(filename):
            return filename
        if self.model_dir:
            return os.path.join(self.model_dir, filename)
        return filename

    def _init_recognizer(self):
        import sherpa_onnx

        tokens_path = self._resolve_path(self.tokens)
        if not os.path.isfile(tokens_path):
            raise FileNotFoundError(f"tokens 文件不存在: {tokens_path}")

        common_kwargs = {
            "tokens": tokens_path,
            "num_threads": 2,
            "sample_rate": 16000,
            "feature_dim": 80,
            "decoding_method": "greedy_search",
            "provider": "cpu",
        }
        # 端点检测参数仅 transducer / zipformer 支持
        endpoint_kwargs = {
            "rule1_min_trailing_silence": self.rule1_min_trailing_silence,
            "rule2_min_trailing_silence": self.rule2_min_trailing_silence,
            "rule3_min_utterance_length": self.rule3_min_utterance_length,
        }

        if self.model_type == "paraformer":
            encoder_path = self._resolve_path(self.encoder)
            decoder_path = self._resolve_path(self.decoder)
            if not os.path.isfile(encoder_path):
                raise FileNotFoundError(f"encoder 不存在: {encoder_path}")
            if not os.path.isfile(decoder_path):
                raise FileNotFoundError(f"decoder 不存在: {decoder_path}")

            self.recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
                encoder=encoder_path,
                decoder=decoder_path,
                **common_kwargs,
            )
        elif self.model_type in ("transducer", "zipformer"):
            encoder_path = self._resolve_path(self.encoder)
            decoder_path = self._resolve_path(self.decoder)
            joiner_path = self._resolve_path(self.joiner)
            if not os.path.isfile(encoder_path):
                raise FileNotFoundError(f"encoder 不存在: {encoder_path}")
            if not os.path.isfile(decoder_path):
                raise FileNotFoundError(f"decoder 不存在: {decoder_path}")
            if not os.path.isfile(joiner_path):
                raise FileNotFoundError(f"joiner 不存在: {joiner_path}")

            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                encoder=encoder_path,
                decoder=decoder_path,
                joiner=joiner_path,
                **common_kwargs,
                **endpoint_kwargs,
            )
        else:
            raise ValueError(
                f"不支持的流式模型类型: {self.model_type}，"
                f"支持 paraformer / transducer / zipformer"
            )

        logger.bind(tag=TAG).info(
            f"Sherpa-ONNX 流式识别器初始化完成: {self.model_type}"
        )

    # ------------------------------------------------------------------ #
    # 音频接收（实时喂给 OnlineRecognizer）
    # ------------------------------------------------------------------ #

    async def receive_audio(self, conn: "ConnectionHandler", audio, audio_have_voice):
        # 先调用父类缓存音频（供 handle_voice_stop 中的声纹识别等使用）
        await super().receive_audio(conn, audio, audio_have_voice)
        self._conn = conn

        # 语音开始且没有活跃 stream -> 创建新 stream
        if audio_have_voice and not self.is_processing and not self._is_stopping:
            try:
                self.stream = self.recognizer.create_stream()
                self.is_processing = True
                self.text = ""
                self.forward_task = asyncio.create_task(
                    self._recognize_loop(conn)
                )
                logger.bind(tag=TAG).debug("创建新的流式识别 stream")
            except Exception as e:
                logger.bind(tag=TAG).error(f"创建识别 stream 失败: {e}")
                self.is_processing = False
                return

        # 有活跃 stream 且未停止 -> 实时喂 opus 音频
        if self.stream and self.is_processing and not self._is_stopping:
            try:
                pcm = self.decoder_opus.decode(audio, 960)
                if pcm:
                    samples = (
                        np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                        / 32768.0
                    )
                    self.stream.accept_waveform(16000, samples)
            except Exception as e:
                logger.bind(tag=TAG).debug(f"喂音频失败: {e}")

        # VAD 停止信号兜底：流式模式下当 VAD 检测到语音结束时触发停止
        if (
            self.is_processing
            and conn.client_voice_stop
            and not self._is_stopping
        ):
            logger.bind(tag=TAG).debug("VAD 检测到语音结束，触发流式识别停止")
            asyncio.create_task(self._send_stop_request())

    # ------------------------------------------------------------------ #
    # 后台识别循环
    # ------------------------------------------------------------------ #

    async def _recognize_loop(self, conn: "ConnectionHandler"):
        try:
            while (
                self.stream
                and self.is_processing
                and not conn.stop_event.is_set()
            ):
                # 持续解码直到没有足够特征
                while self.recognizer.is_ready(self.stream):
                    self.recognizer.decode_stream(self.stream)

                # 获取当前识别结果（不同版本 sherpa_onnx 返回类型不同：str 或对象）
                result = self.recognizer.get_result(self.stream)
                current_text = result if isinstance(result, str) else getattr(result, "text", "")
                if current_text and current_text != self.text:
                    self.text = current_text
                    logger.bind(tag=TAG).debug(
                        f"流式中间结果: {self.text}"
                    )

                # 端点检测触发（用户说完一句话）或手动停止
                is_endpoint = False
                try:
                    is_endpoint = self.recognizer.is_endpoint(self.stream)
                except Exception:
                    pass  # paraformer 模型可能不支持 is_endpoint

                if is_endpoint or self._is_stopping:
                    final_text = self.text

                    if self._is_stopping:
                        logger.bind(tag=TAG).info(
                            f"手动停止，最终结果: {final_text}"
                        )
                    else:
                        logger.bind(tag=TAG).info(
                            f"端点检测触发，识别结果: {final_text}"
                        )
                        # 重置 stream，准备下一句（paraformer 可能不支持）
                        try:
                            self.recognizer.reset(self.stream)
                        except Exception:
                            pass

                    # 触发后续处理（声纹识别 + LLM）
                    # 注意：self.text 由 speech_to_text 负责清空
                    if (
                        final_text
                        and conn.asr_audio
                        and len(conn.asr_audio) > 0
                    ):
                        await self.handle_voice_stop(conn, conn.asr_audio)
                    break

                await asyncio.sleep(0.05)

        except Exception as e:
            logger.bind(tag=TAG).error(f"识别循环出错: {e}", exc_info=True)
        finally:
            self.is_processing = False
            self.stream = None
            self._is_stopping = False
            # 重置连接音频状态，清理 asr_audio 缓存和 VAD 标志
            try:
                conn.reset_audio_states()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 手动停止（listen stop / 打断）
    # ------------------------------------------------------------------ #

    async def _send_stop_request(self):
        """添加 tail padding，通知识别循环结束并获取最终结果。"""
        if not self.stream or not self.is_processing:
            self._is_stopping = False
            return

        self._is_stopping = True
        try:
            tail_padding = np.zeros(
                int(0.66 * 16000), dtype=np.float32
            )
            self.stream.accept_waveform(16000, tail_padding)
            logger.bind(tag=TAG).debug(
                "已发送 tail padding，等待识别循环结束"
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"发送 tail padding 失败: {e}")
            self._is_stopping = False

    def reset_stream_state(self):
        self.is_processing = False
        self._is_stopping = False
        self.stream = None
        self.text = ""

    # ------------------------------------------------------------------ #
    # 接口适配
    # ------------------------------------------------------------------ #

    async def speech_to_text(
        self, opus_data, session_id, audio_format, artifacts=None
    ):
        """流式模式下结果已在 _recognize_loop 中处理。
        这里返回缓存文本，供 handle_voice_stop -> speech_to_text_wrapper 调用。"""
        result = self.text
        self.text = ""
        return result, None

    def stop_ws_connection(self):
        if self.is_processing:
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
        self.stream = None

        if hasattr(self, "decoder_opus") and self.decoder_opus:
            try:
                del self.decoder_opus
                self.decoder_opus = None
            except Exception:
                pass

        logger.bind(tag=TAG).debug(
            "Sherpa-ONNX 流式识别器资源已释放"
        )
