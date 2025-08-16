import traceback
import yaml
import os
from dotenv import load_dotenv


from ..agent.stateless_llm_factory import LLMFactory as StatelessLLMFactory
from ..AITUBER.youtube_group_talk import YouTubeGroupTalk, YouTubeCommentAdapter

# === AITUBER Agent/YouTubeコメント連携 ===
def load_aituber_agent_config(conf_path="conf.yaml"):
    """
    conf.yamlからAITUBER_agentまたはAITUBER_apiの設定を取得
    """
    with open(conf_path, "r", encoding="utf-8") as f:
        conf = yaml.safe_load(f)
    logger.debug(f"[DEBUG] conf.yaml loaded: keys={list(conf.keys())}")
    if "AITUBER_agent" in conf:
        logger.debug("[DEBUG] Using AITUBER_agent config")
        return conf["AITUBER_agent"]
    elif "AITUBER_api" in conf:
        logger.debug("[DEBUG] Using AITUBER_api config")
        return conf["AITUBER_api"]
    else:
        logger.error("[DEBUG] No AITUBER_agent or AITUBER_api found in conf.yaml")
        raise ValueError("AITUBER_agent or AITUBER_api config not found in conf.yaml")


def get_aituber_llm_instance():
    """
    conf.yamlの設定に基づきLLMインスタンスを生成
    """
    logger.debug("[DEBUG] get_aituber_llm_instance called")
    config = load_aituber_agent_config()
    logger.debug(f"[DEBUG] LLM config: {config}")
    llm_provider = config.get("llm_provider") or ("aituber_api" if config.get("base_url") else None)
    logger.debug(f"[DEBUG] llm_provider resolved: {llm_provider}")
    if not llm_provider:
        logger.error("[DEBUG] llm_provider or base_url missing in config")
        raise ValueError("llm_provider or base_url must be set in conf.yaml")
    try:
        config_wo_provider = {k: v for k, v in config.items() if k != "llm_provider"}
        llm = StatelessLLMFactory.create_llm(llm_provider, **config_wo_provider)
        logger.debug(f"[DEBUG] LLM instance created: {llm}")
        return llm
    except Exception as e:
        logger.error(f"[DEBUG] LLMFactory.create_llm error: {e}\n{traceback.format_exc()}")
        raise

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
    logger.debug(f"[DEBUG] inject_youtube_comment_to_conversation: comment={comment}, client_uid={client_uid}")
    # YouTubeコメントをAI応答のprefixに反映させるため、contextに一時的にauthor名をセット
    author = comment.get('author', 'YouTubeUser')
    message = comment.get('message', '')
    # contextがNoneならclient_contextsから取得
    if context is None and client_contexts is not None:
        context = client_contexts.get(client_uid)
    if context is not None:
        setattr(context, "_yt_comment_author", author)
    else:
        logger.error(f"[ERROR] inject_youtube_comment_to_conversation: contextが取得できません client_uid={client_uid}")
    # AIへの入力としてYouTubeコメントを会話トリガーに流し込む
    data = {
        "text": message
    }
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
    )

# === YouTubeコメント監視・割り込み起動例 ===
async def start_youtube_comment_listener(
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
    YouTubeコメントを定期的に監視し、会話フローに割り込ませる
    """
    logger.debug(f"[DEBUG] start_youtube_comment_listener: client_uid={client_uid}")
    load_dotenv()
    api_key = os.getenv("YOUTUBE_API_KEY")
    channel_id = os.getenv("YOUTUBE_CHANNEL_ID")
    logger.debug(f"[DEBUG] YOUTUBE_API_KEY={api_key}, YOUTUBE_CHANNEL_ID={channel_id}")
    if not api_key or not channel_id:
        logger.error("[DEBUG] YOUTUBE_API_KEYまたはYOUTUBE_CHANNEL_IDが未設定です")
        logger.warning("YOUTUBE_API_KEYまたはYOUTUBE_CHANNEL_IDが未設定です")
        return


    # --- YouTubeコメントリスナー用ServiceContext初期化 ---
    if client_contexts is None:
        client_contexts = {}
    if client_uid not in client_contexts:
        try:
            from ..service_context import ServiceContext, Config, read_yaml
            service_context = ServiceContext()
            # conf.yamlからConfigをロードし、ServiceContextに反映
            config_dict = read_yaml("conf.yaml")
            config_obj = Config.model_validate(config_dict)
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                coro = service_context.load_from_config(config_obj)
                task = asyncio.create_task(coro)
                loop.run_until_complete(asyncio.sleep(0))  # すぐに初期化
            else:
                loop.run_until_complete(service_context.load_from_config(config_obj))
            service_context.client_uid = client_uid
            client_contexts[client_uid] = service_context
            logger.info(f"[DEBUG] ServiceContext for {client_uid} initialized and registered.")
        except Exception as e:
            logger.error(f"[ERROR] ServiceContext初期化失敗: {e}\n{traceback.format_exc()}")
            return

    yt_adapter = YouTubeCommentAdapter(api_key=api_key, channel_id=channel_id)
    logger.debug(f"[DEBUG] YouTubeCommentAdapter created: {yt_adapter}")
    if not yt_adapter.live_chat_id:
        logger.error("[DEBUG] live_chat_id取得失敗。配信が無いかコメント無効")
        logger.warning("❌ live_chat_id取得失敗。配信が無いかコメント無効")
        return
    group_talk = YouTubeGroupTalk(yt_adapter)
    logger.debug(f"[DEBUG] YouTubeGroupTalk created: {group_talk}")
    while True:
        try:
            logger.debug("[DEBUG] Polling YouTube comments...")
            new_comments = group_talk.poll_comments()
            logger.debug(f"[DEBUG] new_comments: {new_comments}")
            for comment in new_comments:
                logger.debug(f"[DEBUG] New YouTube comment: {comment}")
                # client_contextsは必ずdictとして渡す
                await inject_youtube_comment_to_conversation(
                    comment,
                    client_uid,
                    client_contexts.get(client_uid),
                    websocket,
                    client_contexts,
                    client_connections,
                    chat_group_manager,
                    received_data_buffers,
                    current_conversation_tasks,
                    broadcast_to_group,
                )
        except Exception as e:
            logger.error(f"💥 YouTubeコメント監視エラー: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(5)
import asyncio
import json
from typing import Dict, Optional, Callable

import numpy as np
from fastapi import WebSocket
from loguru import logger

from ..chat_group import ChatGroupManager
from ..chat_history_manager import store_message
from ..service_context import ServiceContext
from .group_conversation import process_group_conversation
from .single_conversation import process_single_conversation
from .conversation_utils import EMOJI_LIST
from .types import GroupConversationState
from prompts import prompt_loader


async def handle_conversation_trigger(
    msg_type: str,
    data: dict,
    client_uid: str,
    context: ServiceContext,
    websocket: WebSocket,
    client_contexts: Dict[str, ServiceContext],
    client_connections: Dict[str, WebSocket],
    chat_group_manager: ChatGroupManager,
    received_data_buffers: Dict[str, np.ndarray],
    current_conversation_tasks: Dict[str, Optional[asyncio.Task]],
    broadcast_to_group: Callable,
) -> None:
    if current_conversation_tasks is None:
        current_conversation_tasks = {}
    metadata = None
    """Handle triggers that start a conversation"""
    metadata = None

    if msg_type == "ai-speak-signal":
        try:
            # Get proactive speak prompt from config
            prompt_name = "proactive_speak_prompt"
            prompt_file = context.system_config.tool_prompts.get(prompt_name)
            if prompt_file:
                user_input = prompt_loader.load_util(prompt_file)
            else:
                logger.warning(f"Proactive speak prompt not configured, using default")
                user_input = "Please say something."
        except Exception as e:
            logger.error(f"Error loading proactive speak prompt: {e}")
            user_input = "Please say something."

        # Add metadata to indicate this is a proactive speak request
        # that should be skipped in both memory and history
        metadata = {
            "proactive_speak": True,
            "skip_memory": True,  # Skip storing in AI's internal memory
            "skip_history": True,  # Skip storing in local conversation history
        }

        await websocket.send_text(
            json.dumps(
                {
                    "type": "full-text",
                    "text": "AI wants to speak something...",
                }
            )
        )
    elif msg_type == "text-input":
        user_input = data.get("text", "")
    else:  # mic-audio-end
        user_input = received_data_buffers[client_uid]
        received_data_buffers[client_uid] = np.array([])

    images = data.get("images")
    session_emoji = np.random.choice(EMOJI_LIST)

    if chat_group_manager is not None:
        group = chat_group_manager.get_client_group(client_uid)
    else:
        group = None

    if group and len(group.members) > 1:
        # Use group_id as task key for group conversations
        task_key = group.group_id
        if (
            task_key not in current_conversation_tasks
            or current_conversation_tasks[task_key].done()
        ):
            logger.info(f"Starting new group conversation for {task_key}")

            current_conversation_tasks[task_key] = asyncio.create_task(
                process_group_conversation(
                    client_contexts=client_contexts,
                    client_connections=client_connections,
                    broadcast_func=broadcast_to_group,
                    group_members=group.members,
                    initiator_client_uid=client_uid,
                    user_input=user_input,
                    images=images,
                    session_emoji=session_emoji,
                    metadata=metadata,
                )
            )
    else:
        # Use client_uid as task key for individual conversations
        if websocket is not None:
            ws_send = websocket.send_text
        else:
            async def ws_send(*args, **kwargs):
                return None
        current_conversation_tasks[client_uid] = asyncio.create_task(
            process_single_conversation(
                context=context,
                websocket_send=ws_send,
                client_uid=client_uid,
                user_input=user_input,
                images=images,
                session_emoji=session_emoji,
                metadata=metadata,
            )
        )


async def handle_individual_interrupt(
    client_uid: str,
    current_conversation_tasks: Dict[str, Optional[asyncio.Task]],
    context: ServiceContext,
    heard_response: str,
):
    if client_uid in current_conversation_tasks:
        task = current_conversation_tasks[client_uid]
        if task and not task.done():
            task.cancel()
            logger.info("🛑 Conversation task was successfully interrupted")

        try:
            context.agent_engine.handle_interrupt(heard_response)
        except Exception as e:
            logger.error(f"Error handling interrupt: {e}")

        if context.history_uid:
            store_message(
                conf_uid=context.character_config.conf_uid,
                history_uid=context.history_uid,
                role="ai",
                content=heard_response,
                name=context.character_config.character_name,
                avatar=context.character_config.avatar,
            )
            store_message(
                conf_uid=context.character_config.conf_uid,
                history_uid=context.history_uid,
                role="system",
                content="[Interrupted by user]",
            )


async def handle_group_interrupt(
    group_id: str,
    heard_response: str,
    current_conversation_tasks: Dict[str, Optional[asyncio.Task]],
    chat_group_manager: ChatGroupManager,
    client_contexts: Dict[str, ServiceContext],
    broadcast_to_group: Callable,
) -> None:
    """Handles interruption for a group conversation"""
    task = current_conversation_tasks.get(group_id)
    if not task or task.done():
        return

    # Get state and speaker info before cancellation
    state = GroupConversationState.get_state(group_id)
    current_speaker_uid = state.current_speaker_uid if state else None

    # Get context from current speaker
    context = None
    group = chat_group_manager.get_group_by_id(group_id)
    if current_speaker_uid:
        context = client_contexts.get(current_speaker_uid)
        logger.info(f"Found current speaker context for {current_speaker_uid}")
    if not context and group and group.members:
        logger.warning(f"No context found for group {group_id}, using first member")
        context = client_contexts.get(next(iter(group.members)))

    # Now cancel the task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        logger.info(f"🛑 Group conversation {group_id} cancelled successfully.")

    current_conversation_tasks.pop(group_id, None)
    GroupConversationState.remove_state(group_id)  # Clean up state after we've used it

    # Store messages with speaker info
    if context and group:
        for member_uid in group.members:
            if member_uid in client_contexts:
                try:
                    member_ctx = client_contexts[member_uid]
                    member_ctx.agent_engine.handle_interrupt(heard_response)
                    store_message(
                        conf_uid=member_ctx.character_config.conf_uid,
                        history_uid=member_ctx.history_uid,
                        role="ai",
                        content=heard_response,
                        name=context.character_config.character_name,
                        avatar=context.character_config.avatar,
                    )
                    store_message(
                        conf_uid=member_ctx.character_config.conf_uid,
                        history_uid=member_ctx.history_uid,
                        role="system",
                        content="[Interrupted by user]",
                    )
                except Exception as e:
                    logger.error(f"Error handling interrupt for {member_uid}: {e}")

    await broadcast_to_group(
        list(group.members),
        {
            "type": "interrupt-signal",
            "text": "conversation-interrupted",
        },
    )
