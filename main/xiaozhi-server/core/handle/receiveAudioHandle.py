import time
import json
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent
from core.voice.interrupt_classifier import (
    InterruptDecision,
    RuleBasedInterruptClassifier,
)
from core.voice.barge_in_config import (
    BARGE_IN_MODE_NORMAL,
    resolve_barge_in_config,
)
from plugins_func.functions.recording_mode import (
    append_recording,
    exit_recording_mode,
)

# 录音模式退出兜底关键词：当 intent_llm 没识别出退出意图时，子串匹配触发
RECORDING_EXIT_FALLBACK_KEYWORDS = (
    "退出录音",
    "结束录音",
    "停止录音",
    "关闭录音",
)
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_display_message, send_stt_message, SentenceType

TAG = __name__
INTERRUPT_CLASSIFIER = RuleBasedInterruptClassifier()


async def handleAudioMessage(conn: "ConnectionHandler", audio):
    # 当前片段是否有人说话
    have_voice = conn.vad.is_vad(conn, audio)
    # 如果设备刚刚被唤醒，短暂忽略VAD检测
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # 设置一个短暂延迟后恢复VAD检测
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        return
    # 设备长时间空闲检测，用于say goodbye
    await no_voice_close_connect(conn, have_voice)
    # 接收音频
    await conn.asr.receive_audio(conn, audio, have_voice)


async def resume_vad_detection(conn: "ConnectionHandler"):
    # 等待2秒后恢复VAD检测
    await asyncio.sleep(2)
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text):
    # 检查输入是否是JSON格式（包含说话人信息）
    speaker_name = None
    language_tag = None
    actual_text = text
    speech_content = text  # 仅供录音模式落盘使用（不含 JSON 包装）

    try:
        # 尝试解析JSON格式的输入
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                language_tag = data.get("language")
                actual_text = data["content"]
                speech_content = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")

                # 当前文本已经是 data["content"]（纯文本），保留 speaker 信息到 conn
    except (json.JSONDecodeError, KeyError):
        # 如果解析失败，继续使用原始文本
        pass

    # 保存说话人信息到连接对象
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None
    conn.last_user_text = actual_text

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # 如果当日的输出字数大于限定的字数
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return

    # 手动监听模式下不打断正在播放的内容。
    # 播放中插话模式：
    #   off: 播放中不打断
    #   normal: 只有明确插话才打断
    #   sensitive: 可疑插话也打断
    barge_in_mode = BARGE_IN_MODE_NORMAL
    should_interrupt_playback = True
    interrupt_soft_as_hard = False
    try:
        selected_asr = conn.config.get("selected_module", {}).get("ASR")
        asr_config = conn.config.get("ASR", {}).get(selected_asr, {})
        (
            barge_in_mode,
            should_interrupt_playback,
            interrupt_soft_as_hard,
        ) = resolve_barge_in_config(asr_config)
    except Exception:
        barge_in_mode = BARGE_IN_MODE_NORMAL
        should_interrupt_playback = True
        interrupt_soft_as_hard = False

    if (
        conn.client_is_speaking
        and conn.client_listen_mode != "manual"
        and should_interrupt_playback
    ):
        wake_words = conn.config.get("wakeup_words", [])
        interrupt = INTERRUPT_CLASSIFIER.classify(
            is_speaking=True,
            text=actual_text,
            wake_word=any(word in actual_text for word in wake_words),
            is_final=True,
        )
        conn.logger.bind(tag=TAG).info(
            f"播放中ASR分段打断判断: mode={barge_in_mode}, decision={interrupt.decision.value}, "
            f"reason={interrupt.reason}, text={actual_text}"
        )
        if interrupt.decision == InterruptDecision.IGNORE:
            return
        if (
            interrupt.decision == InterruptDecision.SOFT_INTERRUPT
            and not interrupt_soft_as_hard
        ):
            return
        await handleAbortMessage(
            conn,
            reason=f"{interrupt.decision.value}:{interrupt.reason}",
        )
    elif conn.client_is_speaking and conn.client_listen_mode != "manual":
        conn.logger.bind(tag=TAG).info("播放中收到新的ASR分段，继续送入LLM，不打断当前TTS")

    # 首先进行意图分析，使用实际文本内容
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        return

    # 录音模式：意图未触发退出/其他 plugin 时，子串兜底；否则落盘 + 跳过主 LLM
    if getattr(conn, "recording_session", None):
        # 先检查是否是唤醒词（子串匹配，支持"你好小智，今天天气"这类包含唤醒词的表达）
        wakeup_words = conn.config.get("wakeup_words", [])
        if wakeup_words and any(w in actual_text for w in wakeup_words):
            conn.logger.bind(tag=TAG).info(f"录音模式检测到唤醒词，退出录音模式: {actual_text}")
            exit_recording_mode(conn)
            # 继续走正常流程，让 checkWakeupWords 处理唤醒回复
            # 注意：不走 return，继续下面的意图处理
        elif any(kw in actual_text for kw in RECORDING_EXIT_FALLBACK_KEYWORDS):
            conn.logger.bind(tag=TAG).info(f"录音模式子串兜底退出: {actual_text}")
            result = exit_recording_mode(conn)
            await send_stt_message(conn, actual_text)
            if result and result.response:
                from core.handle.intentHandler import speak_txt
                speak_txt(conn, result.response)
            return
        else:
            # 正常录音：写一行 JSONL，前端显示 STT，不调主 LLM、不 TTS
            append_recording(conn, speech_content)
            await send_display_message(conn, actual_text)
            return

    # 意图未被处理，继续常规聊天流程，使用实际文本内容
    turn_id = conn.begin_turn(actual_text)
    await send_stt_message(conn, actual_text)

    conn.executor.submit(conn.chat, actual_text, turn_id=turn_id)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    # 只有在已经初始化过时间戳的情况下才进行超时检查
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        if getattr(conn, "recording_session", None):
            # 录音模式下长时间静默是正常场景，使用录音模式专用超时（默认 60 分钟）
            close_connection_no_voice_time = 3600
        else:
            close_connection_no_voice_time = int(
                conn.config.get("close_connection_no_voice_time", 120)
            )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt)


async def max_out_size(conn: "ConnectionHandler"):
    # 播放超出最大输出字数的提示
    conn.client_abort = False
    text = "不好意思，我现在有点事情要忙，明天这个时候我们再聊，约好了哦！明天不见不散，拜拜！"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        # 确保bind_code是6位数字
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return

        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
        await send_stt_message(conn, text)

        # 播放提示音
        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # 逐个播放数字
        for i in range(6):  # 确保只播放6位数字
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # 播放未绑定提示
        conn.client_abort = False
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
