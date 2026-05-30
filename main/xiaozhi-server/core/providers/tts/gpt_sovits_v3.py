import audioop
import os
import tempfile
import requests
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import InterfaceType, SentenceType
from core.utils.util import audio_bytes_to_data_stream, parse_string_to_list

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url")
        self.api_version = config.get("api_version", "v2")
        self.refer_wav_path = config.get('ref_audio')if config.get('ref_audio') else config.get("refer_wav_path")
        self.prompt_text = config.get('ref_text')if config.get('ref_text') else config.get("prompt_text")
        self.prompt_language = config.get("prompt_language")
        self.text_language = config.get("text_language", "auto")

        # 处理空字符串的情况
        top_k = config.get("top_k", "15")
        top_p = config.get("top_p", "1.0")
        temperature = config.get("temperature", "1.0")
        sample_steps = config.get("sample_steps", "32")
        speed = config.get("speed", "1.0")

        self.top_k = int(top_k) if top_k else 15
        self.top_p = float(top_p) if top_p else 1.0
        self.temperature = float(temperature) if temperature else 1.0
        self.sample_steps = int(sample_steps) if sample_steps else 32
        self.speed = float(speed) if speed else 1.0

        self.cut_punc = config.get("cut_punc", "")
        self.inp_refs = parse_string_to_list(config.get("inp_refs"))
        self.if_sr = str(config.get("if_sr", False)).lower() in ("true", "1", "yes")
        self.audio_file_type = config.get("format", "wav")
        self.streaming_mode = config.get("streaming_mode", 2)
        self.media_type = config.get("media_type", "wav")
        self.text_split_method = config.get("text_split_method", "cut0")
        self.batch_size = int(config.get("batch_size", 1))
        self.batch_threshold = float(config.get("batch_threshold", 0.75))
        self.split_bucket = str(config.get("split_bucket", True)).lower() in (
            "true",
            "1",
            "yes",
        )
        self.fragment_interval = float(config.get("fragment_interval", 0.3))
        self.seed = int(config.get("seed", -1))
        self.parallel_infer = str(config.get("parallel_infer", True)).lower() in (
            "true",
            "1",
            "yes",
        )
        self.repetition_penalty = float(config.get("repetition_penalty", 1.35))
        self.overlap_length = int(config.get("overlap_length", 2))
        self.min_chunk_length = int(config.get("min_chunk_length", 16))

        if self.api_version == "v2" and self.streaming_mode:
            self.interface_type = InterfaceType.SINGLE_STREAM

    async def text_to_speak(self, text, output_file):
        if self.api_version == "v2":
            return await self._text_to_speak_v2(text, output_file)

        request_params = {
            "refer_wav_path": self.refer_wav_path,
            "prompt_text": self.prompt_text,
            "prompt_language": self.prompt_language,
            "text": text,
            "text_language": self.text_language,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "cut_punc": self.cut_punc,
            "speed": self.speed,
            "inp_refs": self.inp_refs,
            "sample_steps": self.sample_steps,
            "if_sr": self.if_sr,
        }

        resp = requests.get(self.url, params=request_params)
        if resp.status_code == 200:
            if output_file:
                with open(output_file, "wb") as file:
                    file.write(resp.content)
            else:
                return resp.content
        else:
            error_msg = f"GPT_SoVITS_V3 TTS请求失败: {resp.status_code} - {resp.text}"
            logger.bind(tag=TAG).error(error_msg)
            raise Exception(error_msg)

    async def _text_to_speak_v2(self, text, output_file):
        request_params = {
            "text": text,
            "text_lang": self.text_language,
            "ref_audio_path": self.refer_wav_path,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_language,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "text_split_method": self.text_split_method,
            "batch_size": self.batch_size,
            "batch_threshold": self.batch_threshold,
            "split_bucket": self.split_bucket,
            "speed_factor": self.speed,
            "fragment_interval": self.fragment_interval,
            "seed": self.seed,
            "media_type": self.media_type,
            "streaming_mode": self.streaming_mode,
            "parallel_infer": self.parallel_infer,
            "repetition_penalty": self.repetition_penalty,
            "sample_steps": self.sample_steps,
            "super_sampling": self.if_sr,
            "overlap_length": self.overlap_length,
            "min_chunk_length": self.min_chunk_length,
        }

        resp = requests.get(self.url, params=request_params, stream=bool(self.streaming_mode))
        if resp.status_code != 200:
            error_msg = f"GPT_SoVITS_V3 TTS请求失败: {resp.status_code} - {resp.text}"
            logger.bind(tag=TAG).error(error_msg)
            raise Exception(error_msg)

        if output_file:
            with open(output_file, "wb") as file:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        file.write(chunk)
            return None

        return b"".join(chunk for chunk in resp.iter_content(chunk_size=8192) if chunk)

    def to_tts_stream(self, text, opus_handler=None) -> None:
        if self.api_version != "v2" or not self.streaming_mode:
            return super().to_tts_stream(text, opus_handler=opus_handler)

        text = self._prepare_text(text)
        if not text:
            return None

        request_params = {
            "text": text,
            "text_lang": self.text_language,
            "ref_audio_path": self.refer_wav_path,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_language,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "temperature": self.temperature,
            "text_split_method": self.text_split_method,
            "batch_size": self.batch_size,
            "batch_threshold": self.batch_threshold,
            "split_bucket": self.split_bucket,
            "speed_factor": self.speed,
            "fragment_interval": self.fragment_interval,
            "seed": self.seed,
            "media_type": self.media_type,
            "streaming_mode": self.streaming_mode,
            "parallel_infer": self.parallel_infer,
            "repetition_penalty": self.repetition_penalty,
            "sample_steps": self.sample_steps,
            "super_sampling": self.if_sr,
            "overlap_length": self.overlap_length,
            "min_chunk_length": self.min_chunk_length,
        }

        try:
            resp = requests.get(
                self.url,
                params=request_params,
                stream=True,
                timeout=self.tts_timeout,
            )
            if resp.status_code != 200:
                error_msg = f"GPT_SoVITS_V3流式TTS请求失败: {resp.status_code} - {resp.text}"
                logger.bind(tag=TAG).error(error_msg)
                raise Exception(error_msg)

            self.tts_audio_queue.put(
                (SentenceType.FIRST, None, text, getattr(self, "current_sentence_id", None))
            )
            self._stream_response_to_opus(resp, opus_handler)
            logger.bind(tag=TAG).info(f"流式语音生成完成: {text}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"流式语音生成失败: {text}，错误: {e}")

    def _prepare_text(self, text):
        from core.utils.tts import MarkdownCleaner

        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(
                lambda m: self.correct_words[m.group(0)], text
            )
        return text.strip()

    def _stream_response_to_opus(self, resp, opus_handler):
        source_sample_rate = self.conn.sample_rate
        source_channels = 1
        source_sample_width = 2
        target_sample_rate = self.conn.sample_rate
        chunk_size = int(source_sample_rate * 0.3 * source_sample_width)
        buffer = bytearray()
        started = False
        resample_state = None
        emitted_chunks = 0

        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                continue
            buffer.extend(chunk)
            if not started and self.media_type == "wav":
                wav_info = self._parse_wav_header(buffer)
                header_end = wav_info["data_offset"] if wav_info else None
                if header_end is None:
                    continue
                source_sample_rate = wav_info["sample_rate"]
                source_channels = wav_info["channels"]
                source_sample_width = wav_info["sample_width"]
                chunk_size = int(source_sample_rate * 0.3 * source_channels * source_sample_width)
                del buffer[:header_end]
                started = True
                logger.bind(tag=TAG).info(
                    "GPT_SoVITS_V3流式音频参数: "
                    f"{source_sample_rate}Hz/{source_channels}ch/{source_sample_width * 8}bit -> "
                    f"{target_sample_rate}Hz"
                )
            elif self.media_type != "wav":
                started = True

            while len(buffer) >= chunk_size:
                pcm_chunk = bytes(buffer[:chunk_size])
                del buffer[:chunk_size]
                pcm_chunk, resample_state = self._normalize_pcm_chunk(
                    pcm_chunk,
                    source_sample_rate,
                    target_sample_rate,
                    source_channels,
                    source_sample_width,
                    resample_state,
                )
                self._emit_audio_chunk(pcm_chunk, False, opus_handler)
                emitted_chunks += 1

        if buffer:
            pcm_chunk, resample_state = self._normalize_pcm_chunk(
                bytes(buffer),
                source_sample_rate,
                target_sample_rate,
                source_channels,
                source_sample_width,
                resample_state,
            )
            self._emit_audio_chunk(pcm_chunk, True, opus_handler)
            emitted_chunks += 1
        else:
            self.opus_encoder.encode_pcm_to_opus_stream(b"", True, opus_handler)
        logger.bind(tag=TAG).info(f"GPT_SoVITS_V3流式音频输出chunk数: {emitted_chunks}")

    def _emit_audio_chunk(self, chunk, end_of_stream, opus_handler):
        if self.conn.audio_format == "pcm":
            if opus_handler:
                opus_handler(chunk)
            return

        if self.media_type in ("raw", "wav"):
            self.opus_encoder.encode_pcm_to_opus_stream(
                chunk,
                end_of_stream=end_of_stream,
                callback=opus_handler,
            )
            return

        with tempfile.NamedTemporaryFile(suffix=f".{self.media_type}", delete=False) as tmp:
            tmp.write(chunk)
            tmp_path = tmp.name
        try:
            with open(tmp_path, "rb") as file:
                audio_bytes_to_data_stream(
                    file.read(),
                    file_type=self.media_type,
                    is_opus=True,
                    callback=opus_handler,
                    sample_rate=self.conn.sample_rate,
                    opus_encoder=self.opus_encoder,
                )
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _wav_data_offset(self, data):
        if len(data) < 44:
            return None
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return 0
        idx = data.find(b"data", 12)
        if idx == -1 or len(data) < idx + 8:
            return None
        return idx + 8

    def _parse_wav_header(self, data):
        if len(data) < 44:
            return None
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return {
                "data_offset": 0,
                "sample_rate": self.conn.sample_rate,
                "channels": 1,
                "sample_width": 2,
            }

        fmt_idx = data.find(b"fmt ", 12)
        data_idx = data.find(b"data", 12)
        if fmt_idx == -1 or data_idx == -1 or len(data) < data_idx + 8:
            return None

        channels = int.from_bytes(data[fmt_idx + 10 : fmt_idx + 12], "little")
        sample_rate = int.from_bytes(data[fmt_idx + 12 : fmt_idx + 16], "little")
        bits_per_sample = int.from_bytes(data[fmt_idx + 22 : fmt_idx + 24], "little")
        sample_width = max(1, bits_per_sample // 8)
        return {
            "data_offset": data_idx + 8,
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
        }

    def _normalize_pcm_chunk(
        self,
        pcm_chunk,
        source_sample_rate,
        target_sample_rate,
        source_channels,
        source_sample_width,
        resample_state,
    ):
        if not pcm_chunk:
            return pcm_chunk, resample_state

        if source_channels != 1:
            pcm_chunk = audioop.tomono(
                pcm_chunk, source_sample_width, 1.0 / source_channels, 1.0 / source_channels
            )

        if source_sample_width != 2:
            pcm_chunk = audioop.lin2lin(pcm_chunk, source_sample_width, 2)

        if source_sample_rate != target_sample_rate:
            pcm_chunk, resample_state = audioop.ratecv(
                pcm_chunk,
                2,
                1,
                source_sample_rate,
                target_sample_rate,
                resample_state,
            )

        return pcm_chunk, resample_state
