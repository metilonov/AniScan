from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from app.config import Config
from app.db import Database


LOGGER = logging.getLogger(__name__)
MILESTONES: tuple[tuple[int, int], ...] = (
    (500, 10),
    (1_000, 15),
    (5_000, 20),
    (10_000, 25),
    (50_000, 25),
)


async def _send_milestone_message(bot: Bot, user_id: int, threshold: int, percent: int) -> bool:
    text = (
        "🎉 <b>Новая акция AniScan!</b>\n\n"
        f"В боте уже <b>{threshold:,}</b> пользователей. Вам выдана разовая скидка "
        f"<b>{percent}%</b> на следующую покупку.\n\n"
        "Скидка доступна только пользователям, которые уже совершали покупки, "
        "складывается с активной промо-скидкой и ограничивается общим максимумом 25%."
    ).replace(",", " ")
    try:
        await bot.send_message(user_id, text)
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        try:
            await bot.send_message(user_id, text)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def process_due_milestones(bot: Bot, db: Database, config: Config) -> None:
    events = await db.trigger_due_milestones(MILESTONES)
    for event in events:
        threshold = int(event["threshold"])
        percent = int(event["percent"])
        recipients: list[int] = list(event["recipients"])
        sent = 0
        failed = 0
        for index in range(0, len(recipients), 25):
            batch = recipients[index : index + 25]
            results = await asyncio.gather(
                *(_send_milestone_message(bot, user_id, threshold, percent) for user_id in batch)
            )
            sent += sum(1 for ok in results if ok)
            failed += sum(1 for ok in results if not ok)
            if index + 25 < len(recipients):
                await asyncio.sleep(1.0)

        report = (
            "🎉 <b>Автоматическая акция запущена</b>\n\n"
            f"Порог: <b>{threshold:,}</b> пользователей\n"
            f"Скидка: <b>{percent}%</b>\n"
            f"Получили право на скидку: <b>{len(recipients)}</b>\n"
            f"Уведомлено: <b>{sent}</b>\n"
            f"Не доставлено: <b>{failed}</b>"
        ).replace(",", " ")
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(admin_id, report)
            except Exception:
                LOGGER.exception("Не удалось отправить отчёт об акции администратору %s", admin_id)
