from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

from app.config import Config
from app.db import Database
from app.models import WarningResult


async def is_subscribed(bot: Bot, config: Config, user_id: int) -> bool | None:
    if config.is_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=config.channel_id, user_id=user_id)
    except TelegramAPIError:
        # None означает, что Telegram не позволил надёжно проверить статус.
        # В этом случае нельзя выдавать штраф за отписку.
        return None

    if member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True
    return bool(getattr(member, "is_member", False))


async def apply_unsubscribe_if_needed(
    db: Database, user_id: int, subscribed: bool | None
) -> WarningResult:
    if subscribed is not False:
        return WarningResult(0, False, False)
    return await db.apply_unsubscribe_penalty(user_id)
