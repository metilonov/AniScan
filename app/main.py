from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from app.config import Config
from app.db import Database
from app.handlers import admin, broadcasts, common, payments, referrals, search, system
from app.services.ai import AnimeAI


async def configure_commands(bot: Bot, config: Config) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить AniScan"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="paysupport", description="Поддержка по оплате"),
            BotCommand(command="referrals", description="Рефералы и бонусы"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    admin_commands = [
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="broadcast", description="Создать рассылку"),
        BotCommand(command="users", description="Количество пользователей"),
        BotCommand(command="block", description="Заблокировать пользователя"),
        BotCommand(command="unblock", description="Разблокировать пользователя"),
        BotCommand(command="menu", description="Главное меню"),
    ]
    for admin_id in config.admin_ids:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logging.getLogger(__name__).exception(
                "Не удалось установить команды администратора для %s", admin_id
            )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = Config.from_env()
    db = Database(config.database_path)
    await db.init()
    if config.dashscope_api_key:
        await db.seed_qwen_key(
            config.dashscope_api_key,
            config.qwen_base_url,
            label="Основной ключ из .env",
        )

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    anime_ai = AnimeAI(config, db, bot)
    dp = Dispatcher()

    # Системные обновления и более специфичные админские/FSM-обработчики регистрируются первыми.
    dp.include_router(system.router)
    dp.include_router(admin.router)
    dp.include_router(broadcasts.router)
    dp.include_router(payments.router)
    dp.include_router(referrals.router)
    dp.include_router(common.router)
    dp.include_router(search.router)

    await configure_commands(bot, config)
    await bot.delete_webhook(drop_pending_updates=False)

    from app.services.promotions import process_due_milestones

    # Рассылка уже наступивших акций не задерживает запуск long polling.
    milestone_task = asyncio.create_task(process_due_milestones(bot, db, config))
    try:
        await dp.start_polling(
            bot,
            config=config,
            db=db,
            anime_ai=anime_ai,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        if not milestone_task.done():
            milestone_task.cancel()
        await asyncio.gather(milestone_task, return_exceptions=True)
        await anime_ai.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
