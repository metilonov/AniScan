from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import ChatMemberUpdated, Message

from app.config import Config
from app.db import Database


router = Router(name="system")
LOGGER = logging.getLogger(__name__)
ANISCAN_WORD = re.compile(r"(?<![A-Za-z0-9_])aniscan(?![A-Za-z0-9_])", re.IGNORECASE)


@router.my_chat_member()
async def auto_leave_unauthorized_chats(
    update: ChatMemberUpdated,
    bot: Bot,
    config: Config,
) -> None:
    if update.chat.type == ChatType.PRIVATE:
        return

    joined_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
    }
    left_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    if update.new_chat_member.status not in joined_statuses:
        return
    if update.old_chat_member.status not in left_statuses:
        return
    if config.is_admin(update.from_user.id):
        return

    # Бот не обслуживает группы/каналы, куда его пригласил не владелец проекта.
    if update.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        try:
            await bot.send_message(
                update.chat.id,
                "AniScan работает только в личных сообщениях. "
                "Добавлять бота в чаты могут только администраторы проекта.",
            )
        except Exception:
            pass
    try:
        await bot.leave_chat(update.chat.id)
    except Exception:
        LOGGER.exception("Не удалось выйти из чата %s", update.chat.id)


async def _forward_with_retry(bot: Bot, user_id: int, source_chat_id: int, message_id: int) -> bool:
    try:
        await bot.forward_message(
            chat_id=user_id,
            from_chat_id=source_chat_id,
            message_id=message_id,
        )
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        try:
            await bot.forward_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=message_id,
            )
            return True
        except Exception:
            return False
    except Exception:
        return False


async def _broadcast_channel_post(
    post: Message,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    recipients = await db.get_broadcast_recipients()
    sent = 0
    failed = 0
    for index in range(0, len(recipients), 25):
        batch = recipients[index : index + 25]
        results = await asyncio.gather(
            *(
                _forward_with_retry(bot, user_id, post.chat.id, post.message_id)
                for user_id in batch
            )
        )
        sent += sum(1 for ok in results if ok)
        failed += sum(1 for ok in results if not ok)
        if index + 25 < len(recipients):
            await asyncio.sleep(1.0)

    await db.finish_channel_post(post.chat.id, post.message_id, sent, failed)
    report = (
        "📨 <b>Автоматическая рассылка поста завершена</b>\n\n"
        f"Пост: <code>{post.message_id}</code>\n"
        f"Получили: <b>{sent}</b>\n"
        f"Не доставлено: <b>{failed}</b>"
    )
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, report)
        except Exception:
            pass


@router.channel_post()
async def automatic_channel_post_broadcast(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if message.chat.id != config.channel_id:
        return
    content = message.text or message.caption or ""
    if not ANISCAN_WORD.search(content):
        return
    claimed = await db.claim_channel_post(message.chat.id, message.message_id)
    if not claimed:
        return
    asyncio.create_task(_broadcast_channel_post(message, bot, db, config))
