from __future__ import annotations

import asyncio
import html
import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.config import Config
from app.db import Database
from app.keyboards import main_keyboard, moderation_keyboard, shop_root_keyboard, subscription_keyboard
from app.services.ai import (
    AnimeAI,
    InvalidImageError,
    QwenKeysUnavailableError,
    QwenTemporaryUnavailableError,
    UnverifiedAnimeError,
)
from app.services.subscription import apply_unsubscribe_if_needed, is_subscribed
from app.services.text import format_analysis


logger = logging.getLogger(__name__)

router = Router(name="search")
router.message.filter(F.chat.type == ChatType.PRIVATE)

_user_locks: dict[int, asyncio.Lock] = {}


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


@router.message(F.photo)
async def photo_search(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    anime_ai: AnimeAI,
) -> None:
    if message.from_user is None:
        return
    lock = _user_lock(message.from_user.id)
    if lock.locked():
        await message.answer("⏳ Дождитесь завершения предыдущего поиска.")
        return
    async with lock:
        await _photo_search_impl(message, bot, db, config, anime_ai)


async def _photo_search_impl(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    anime_ai: AnimeAI,
) -> None:
    if message.from_user is None or not message.photo:
        return

    user_id = message.from_user.id
    mode = "basic"
    is_admin = config.is_admin(user_id)
    await db.upsert_user(user_id, message.from_user.username, message.from_user.first_name)
    user = await db.get_user(user_id)
    if user is None:
        return

    if user.blocked and not is_admin:
        await message.answer("⛔ Доступ к боту заблокирован навсегда.")
        return
    if user.restricted and not is_admin:
        await message.answer("⏳ Доступ временно ограничен до решения администратора.")
        return

    subscribed = await is_subscribed(bot, config, user_id)
    if subscribed is None:
        await message.answer(
            "⚠️ Не удалось проверить подписку на канал. "
            "Запрос не списан; повторите попытку позже."
        )
        return
    penalty = await apply_unsubscribe_if_needed(db, user_id, subscribed)
    if penalty.blocked and not is_admin:
        await message.answer("⛔ После третьего предупреждения доступ заблокирован навсегда.")
        return
    if not subscribed:
        extra = ""
        if penalty.newly_applied:
            extra = (
                f"\n\n⚠️ Выдано предупреждение: <b>{penalty.warnings}/3</b>. "
                "Скидки отключены."
            )
        await message.answer(
            "Для поиска нужна действующая подписка на обязательный канал." + extra,
            reply_markup=subscription_keyboard(config.channel_url),
        )
        return

    if not is_admin:
        await db.grant_trial_if_eligible(user_id)

    raw_buffer = BytesIO()
    try:
        telegram_file = await bot.get_file(message.photo[-1].file_id)
        if not telegram_file.file_path:
            raise InvalidImageError("Telegram не вернул путь к файлу")
        await bot.download_file(telegram_file.file_path, destination=raw_buffer)
        image_bytes, mime = anime_ai.prepare_image(raw_buffer.getvalue())
    except (InvalidImageError, OSError, ValueError):
        await message.answer("Не удалось прочитать изображение. Отправьте обычное фото JPG/PNG.")
        return

    remaining: int | None = None
    charged = False
    processing = await message.answer("Поиск аниме..")

    try:
        sexual, score = await anime_ai.is_sexual(image_bytes, mime)
        if sexual:
            if is_admin:
                await processing.edit_text(
                    "⚠️ Изображение отмечено модерацией как сексуальный контент. "
                    "Для администратора ограничение не применяется."
                )
                return

            await db.set_restricted(user_id, True)
            case_id = await db.create_moderation_case(user_id, message.photo[-1].file_id, score)
            await db.log_search(user_id, mode, "sexual", "sexual", None)

            username = f"@{html.escape(message.from_user.username)}" if message.from_user.username else "нет"
            caption = (
                "🚨 <b>Изображение отправлено на модерацию</b>\n\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"Username: {username}\n"
                f"Оценка sexual: <b>{score:.3f}</b>\n"
                f"Дело: <code>#{case_id}</code>\n\n"
                "До решения администратора доступ пользователя ограничен."
            )
            for admin_id in config.admin_ids:
                try:
                    await bot.send_photo(
                        chat_id=admin_id,
                        photo=message.photo[-1].file_id,
                        caption=caption,
                        reply_markup=moderation_keyboard(case_id),
                    )
                except Exception:
                    logger.exception("Не удалось отправить уведомление администратору %s", admin_id)

            await processing.edit_text(
                "⛔ <b>Изображение нарушает правила бота.</b>\n"
                "Доступ временно ограничен, материал отправлен администратору на проверку."
            )
            return

        if not is_admin:
            consume = await db.consume_credit(user_id, mode)
            if not consume.ok:
                if consume.reason == "no_credits":
                    await processing.edit_text(
                        "Недостаточно запросов. Откройте магазин и пополните баланс.",
                        reply_markup=shop_root_keyboard(),
                    )
                elif consume.reason == "restricted":
                    await processing.edit_text(
                        "⏳ Доступ временно ограничен до решения администратора."
                    )
                else:
                    await processing.edit_text("⛔ Доступ к боту ограничен.")
                return
            charged = True
            remaining = consume.remaining

        result = await anime_ai.analyze_verified(image_bytes, mime)
        if result.content_class == "not_anime":
            if is_admin:
                await processing.edit_text(
                    "⚠️ <b>AniScan работает исключительно с аниме.</b>\n"
                    "Отправьте кадр или изображение из аниме."
                )
                return

            warning = await db.add_warning(
                user_id,
                "Отправлено изображение, которое AI не распознал как аниме",
            )
            await db.log_search(user_id, mode, "not_anime", "not_anime", None)
            if warning.blocked:
                await processing.edit_text(
                    "⛔ <b>AniScan работает исключительно с аниме.</b>\n"
                    "Игры, фильмы, сериалы, манга, манхва и другие изображения не поддерживаются.\n\n"
                    "Запрос списан. Получено третье предупреждение — доступ заблокирован навсегда."
                )
            else:
                await processing.edit_text(
                    "⚠️ <b>AniScan работает исключительно с аниме.</b>\n"
                    "Игры, фильмы, сериалы, манга, манхва и другие изображения не поддерживаются.\n\n"
                    "Запрос списан согласно правилам.\n"
                    f"Предупреждений: <b>{warning.warnings}/3</b>.\n"
                    f"Осталось запросов: <b>{remaining}</b>"
                )
            return

        await db.log_search(user_id, mode, "success", "anime", result.title)
        refreshed = await db.get_user(user_id)
        assert refreshed is not None
        await processing.edit_text(
            format_analysis(result, mode, remaining),
            reply_markup=main_keyboard(refreshed, config.channel_url, is_admin),
        )

    except QwenKeysUnavailableError:
        if charged:
            await db.refund_credit(user_id, mode, 1)
        await db.log_search(user_id, mode, "qwen_quota_exhausted", None, None)
        await processing.edit_text(
            "⚠️ <b>Квота нейросети временно закончилась.</b>\n"
            "Администратор уже уведомлён. Запрос не списан или возвращён на баланс. "
            "Повторите попытку после добавления нового ключа Qwen."
        )
    except QwenTemporaryUnavailableError:
        if charged:
            await db.refund_credit(user_id, mode, 1)
        await db.log_search(user_id, mode, "qwen_rate_limit", None, None)
        await processing.edit_text(
            "⏳ Qwen временно ограничил частоту запросов. "
            "Запрос не списан или возвращён на баланс. Попробуйте ещё раз через минуту."
        )
    except UnverifiedAnimeError:
        if charged:
            await db.refund_credit(user_id, mode, 1)
        await db.log_search(user_id, mode, "unverified", "anime", None)
        await processing.edit_text(
            "⚠️ Не удалось надёжно подтвердить аниме после двух проверок.\n"
            "Запрос возвращён на баланс. Попробуйте отправить другой или более полный кадр."
        )
    except Exception as exc:
        logger.exception("Ошибка AI-поиска пользователя %s: %s", user_id, exc)
        if charged:
            await db.refund_credit(user_id, mode, 1)
        await db.log_search(user_id, mode, "error", None, None)
        await processing.edit_text(
            "❌ Произошла техническая ошибка при анализе изображения. "
            "Запрос возвращён на баланс. Попробуйте ещё раз позже."
        )
