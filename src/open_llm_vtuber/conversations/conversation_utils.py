import asyncio
import re
from typing import Optional, Union, Any, List, Dict
import numpy as np
import json
from loguru import logger
import yaml
import os
from dotenv import load_dotenv
from ..message_handler import message_handler
from .types import WebSocketSend, BroadcastContext
from .tts_manager import TTSTaskManager
from ..agent.output_types import SentenceOutput, AudioOutput
from ..agent.input_types import BatchInput, TextData, ImageData, TextSource, ImageSource
from ..agent.stateless_llm_factory import LLMFactory as StatelessLLMFactory
from ..AITUBER.youtube_group_talk import YouTubeGroupTalk, YouTubeCommentAdapter
from ..asr.asr_interface import ASRInterface
from ..live2d_model import Live2dModel
from ..tts.tts_interface import TTSInterface
from ..utils.stream_audio import prepare_audio_payload
def create_batch_input(input_text: str, images: Optional[List[Dict[str, Any]]], from_name: str) -> BatchInput:
    """Create batch input for agent processing"""
    return BatchInput(
        texts=[TextData(source=TextSource.INPUT, content=input_text, from_name=from_name)],
        images=[ImageData(source=ImageSource(img["source"]), data=img["data"], mime_type=img["mime_type"]) for img in (images or [])] if images else None,
    )

# === AITUBER Agent/YouTubeコメント連携 ===
def load_aituber_agent_config(conf_path="conf.yaml"):
    """
    conf.yamlからAITUBER_agentまたはAITUBER_apiの設定を取得
    """
    with open(conf_path, "r", encoding="utf-8") as f:
        conf = yaml.safe_load(f)
    if "AITUBER_agent" in conf:
        return conf["AITUBER_agent"]
    elif "AITUBER_api" in conf:
        return conf["AITUBER_api"]
    else:
        raise ValueError("AITUBER_agent or AITUBER_api config not found in conf.yaml")

def get_aituber_llm_instance():
    """
    conf.yamlの設定に基づきLLMインスタンスを生成
    """
    config = load_aituber_agent_config()
    llm_provider = config.get("llm_provider") or ("aituber_api" if config.get("base_url") else None)
    if not llm_provider:
        raise ValueError("llm_provider or base_url must be set in conf.yaml")
    return StatelessLLMFactory.create_llm(llm_provider, **config)

# YouTubeコメントを会話フローに流し込む
async def inject_youtube_comment_to_conversation(
    comment,
    client_uid,
    context,
    websocket,
    client_contexts,
    client_connections,
    chat_group_manager,
    received_data_buffers,
    current_conversation_tasks,
    broadcast_to_group,
):
    """
    YouTubeコメントを通常の会話トリガーとして流し込む
    """
    # regular_labelがあればAI入力に反映
    label = comment.get("regular_label", "")
    author = comment.get("author", "YouTubeUser")
    message = comment.get("message", "")
    if label == "常連":
        prefix = f"{author}さん（常連）"
    elif label == "また来てくれてありがとう":
        prefix = f"{author}さん、また来てくれてありがとう！"
    else:
        prefix = f"{author}さん"
    data = {
        "text": f"{prefix} のコメント: {message}"
    }
    # グループ会話時はmetadataでauthor名を渡す
    metadata = {"yt_comment_author": author}
    await handle_conversation_trigger(
        msg_type="text-input",
        data=data,
        client_uid=client_uid,
        context=context,
        websocket=websocket,
        client_contexts=client_contexts,
        client_connections=client_connections,
        chat_group_manager=chat_group_manager,
        received_data_buffers=received_data_buffers,
        current_conversation_tasks=current_conversation_tasks,
        broadcast_to_group=broadcast_to_group,
        metadata=metadata,
    )
    """
    YouTubeコメントを定期的に監視し、会話フローに割り込ませる
    """
    load_dotenv()
    api_key = os.getenv("YOUTUBE_API_KEY")
    channel_id = os.getenv("YOUTUBE_CHANNEL_ID")
    if not api_key or not channel_id:
        logger.warning("YOUTUBE_API_KEYまたはYOUTUBE_CHANNEL_IDが未設定です")
        return
    yt_adapter = YouTubeCommentAdapter(api_key=api_key, channel_id=channel_id)
    if not yt_adapter.live_chat_id:
        logger.warning("❌ live_chat_id取得失敗。配信が無いかコメント無効")
        return
    group_talk = YouTubeGroupTalk(yt_adapter)
    while True:
        try:
            new_comments = group_talk.poll_comments()
            for comment in new_comments:
                await inject_youtube_comment_to_conversation(
                    comment,
                    client_uid,
                    context,
                    websocket,
                    client_contexts,
                    client_connections,
                    chat_group_manager,
                    received_data_buffers,
                    current_conversation_tasks,
                    broadcast_to_group,
                )
        except Exception as e:
            logger.error(f"💥 YouTubeコメント監視エラー: {e}")
        await asyncio.sleep(5)


async def process_agent_output(
    output: Union[AudioOutput, SentenceOutput],
    character_config: Any,
    live2d_model: Live2dModel,
    tts_engine: TTSInterface,
    websocket_send: WebSocketSend,
    tts_manager: TTSTaskManager,
    translate_engine: Optional[Any] = None,
# YouTubeコメントを会話フローに流し込む
) -> str:
    """Process agent output with character information and optional translation"""
    output.display_text.name = character_config.character_name
    output.display_text.avatar = character_config.avatar

    full_response = ""
    try:
        if isinstance(output, SentenceOutput):
            full_response = await handle_sentence_output(
                output,
                live2d_model,
                tts_engine,
                websocket_send,
                tts_manager,
                translate_engine,
            )
        elif isinstance(output, AudioOutput):
            full_response = await handle_audio_output(output, websocket_send)
# === YouTubeコメント監視・割り込み起動例 ===
        else:
            logger.warning(f"Unknown output type: {type(output)}")
    except Exception as e:
        logger.error(f"Error processing agent output: {e}")
        await websocket_send(
            json.dumps(
                {"type": "error", "message": f"Error processing response: {str(e)}"}
            )
        )

    return full_response


async def handle_sentence_output(
    output: SentenceOutput,
    live2d_model: Live2dModel,
    tts_engine: TTSInterface,
    websocket_send: WebSocketSend,
    tts_manager: TTSTaskManager,
    translate_engine: Optional[Any] = None,
) -> str:
    """Handle sentence output type with optional translation support"""
    full_response = ""
    async for display_text, tts_text, actions in output:
        logger.debug(f"🏃 Processing output: '''{tts_text}'''...")

        if translate_engine:
            if len(re.sub(r'[\s.,!?，。！？\'"』」）】\s]+', "", tts_text)):
                tts_text = translate_engine.translate(tts_text)
            logger.info(f"🏃 Text after translation: '''{tts_text}'''...")
        else:
            logger.debug("🚫 No translation engine available. Skipping translation.")

        full_response += display_text.text
        await tts_manager.speak(
            tts_text=tts_text,
            display_text=display_text,
            actions=actions,
            live2d_model=live2d_model,
            tts_engine=tts_engine,
            websocket_send=websocket_send,
        )
    return full_response


async def handle_audio_output(
    output: AudioOutput,
    websocket_send: WebSocketSend,
) -> str:
    """Process and send AudioOutput directly to the client"""
    full_response = ""
    async for audio_path, display_text, transcript, actions in output:
        full_response += transcript
        audio_payload = prepare_audio_payload(
            audio_path=audio_path,
            display_text=display_text,
            actions=actions.to_dict() if actions else None,
        )
        await websocket_send(json.dumps(audio_payload))
    return full_response


async def send_conversation_start_signals(websocket_send: WebSocketSend) -> None:
    """Send initial conversation signals"""
    await websocket_send(
        json.dumps(
            {
                "type": "control",
                "text": "conversation-chain-start",
            }
        )
    )
    await websocket_send(json.dumps({"type": "full-text", "text": "Thinking..."}))


async def process_user_input(
    user_input: Union[str, np.ndarray],
    asr_engine: ASRInterface,
    websocket_send: WebSocketSend,
) -> str:
    """Process user input, converting audio to text if needed"""
    if isinstance(user_input, np.ndarray):
        logger.info("Transcribing audio input...")
        input_text = await asr_engine.async_transcribe_np(user_input)
        await websocket_send(
            json.dumps({"type": "user-input-transcription", "text": input_text})
        )
        return input_text
    return user_input


async def finalize_conversation_turn(
    tts_manager: TTSTaskManager,
    websocket_send: WebSocketSend,
    client_uid: str,
    broadcast_ctx: Optional[BroadcastContext] = None,
) -> None:
    """Finalize a conversation turn"""
    if tts_manager.task_list:
        await asyncio.gather(*tts_manager.task_list)
        await websocket_send(json.dumps({"type": "backend-synth-complete"}))

        response = await message_handler.wait_for_response(
            client_uid, "frontend-playback-complete"
        )

        if not response:
            logger.warning(f"No playback completion response from {client_uid}")
            return

    await websocket_send(json.dumps({"type": "force-new-message"}))

    if broadcast_ctx and broadcast_ctx.broadcast_func:
        await broadcast_ctx.broadcast_func(
            broadcast_ctx.group_members,
            {"type": "force-new-message"},
            broadcast_ctx.current_client_uid,
        )

    await send_conversation_end_signal(websocket_send, broadcast_ctx)


async def send_conversation_end_signal(
    websocket_send: WebSocketSend,
    broadcast_ctx: Optional[BroadcastContext],
    session_emoji: str = "😊",
) -> None:
    """Send conversation chain end signal"""
    chain_end_msg = {
        "type": "control",
        "text": "conversation-chain-end",
    }

    await websocket_send(json.dumps(chain_end_msg))

    if broadcast_ctx and broadcast_ctx.broadcast_func and broadcast_ctx.group_members:
        await broadcast_ctx.broadcast_func(
            broadcast_ctx.group_members,
            chain_end_msg,
        )

    logger.info(f"😎👍✅ Conversation Chain {session_emoji} completed!")


def cleanup_conversation(tts_manager: TTSTaskManager, session_emoji: str) -> None:
    """Clean up conversation resources"""
    tts_manager.clear()
    logger.debug(f"🧹 Clearing up conversation {session_emoji}.")


EMOJI_LIST = [
    "🐶",
    "🐱",
    "🐭",
    "🐹",
    "🐰",
    "🦊",
    "🐻",
    "🐼",
    "🐨",
    "🐯",
    "🦁",
    "🐮",
    "🐷",
    "🐸",
    "🐵",
    "🐔",
    "🐧",
    "🐦",
    "🐤",
    "🐣",
    "🐥",
    "🦆",
    "🦅",
    "🦉",
    "🦇",
    "🐺",
    "🐗",
    "🐴",
    "🦄",
    "🐝",
    "🌵",
    "🎄",
    "🌲",
    "🌳",
    "🌴",
    "🌱",
    "🌿",
    "☘️",
    "🍀",
    "🍂",
    "🍁",
    "🍄",
    "🌾",
    "💐",
    "🌹",
    "🌸",
    "🌛",
    "🌍",
    "⭐️",
    "🔥",
    "🌈",
    "🌩",
    "⛄️",
    "🎃",
    "🎄",
    "🎉",
    "🎏",
    "🎗",
    "🀄️",
    "🎭",
    "🎨",
    "🧵",
    "🪡",
    "🧶",
    "🥽",
    "🥼",
    "🦺",
    "👔",
    "👕",
    "👜",
    "👑",
]
