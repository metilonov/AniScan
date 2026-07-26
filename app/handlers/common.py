from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, User

from app.config import Config
from app.db import Database
from app.keyboards import back_to_menu_keyboard, main_keyboard, subscription_keyboard
from app.services.promotions import process_due_milestones
from app.services.subscription import apply_unsubscribe_if_needed, is_subscribed
from app.services.text import format_balance, format_utc, remove_inline_keyboard


router = Router(name="common")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class PromoActivationState(StatesGroup):
    waiting_code = State()


async def _send_home(
    message: Message,
    actor: User,
    bot: Bot,
    db: Database,
    config: Config,
    *,
    prefix: str | None = None,
    referrer_id: int | None = None,
) -> None:
    created = await db.upsert_user(actor.id, actor.username, actor.first_name)
    if created and referrer_id is not None and referrer_id != actor.id:
        linked = await db.set_referrer(actor.id, referrer_id)
        if linked:
            referral_prefix = "👥 Вы зарегистрированы по реферальной ссылке."
            prefix = referral_prefix if not prefix else prefix + "\n" + referral_prefix
    if created:
        asyncio.create_task(process_due_milestones(bot, db, config))
    user = await db.get_user(actor.id)
    if user is None:
        raise RuntimeError("Не удалось создать пользователя")

    if user.blocked and not config.is_admin(user.user_id):
        await message.answer(
            "⛔ <b>Доступ к боту заблокирован навсегда.</b>\n"
            "Причина: получено 3 предупреждения или блокировка администратором."
        )
        return
    if user.restricted and not config.is_admin(user.user_id):
        await message.answer(
            "⏳ <b>Доступ временно ограничен.</b>\n"
            "Изображение отправлено администратору на проверку."
        )
        return

    subscribed = await is_subscribed(bot, config, user.user_id)
    if subscribed is None:
        await message.answer(
            "⚠️ Не удалось проверить подписку на канал. "
            "Убедитесь, что бот добавлен администратором канала, и повторите позже."
        )
        return

    penalty = await apply_unsubscribe_if_needed(db, user.user_id, subscribed)
    if penalty.newly_applied:
        if penalty.blocked:
            await message.answer("⛔ После третьего предупреждения доступ заблокирован навсегда.")
            return
        await message.answer(
            "⚠️ <b>Зафиксирована отписка от обязательного канала.</b>\n"
            f"Предупреждений: <b>{penalty.warnings}/3</b>.\n"
            "Повторный пробный запрос не выдаётся, скидки на пакеты отключены."
        )

    if not subscribed:
        await message.answer(
            "<b>Для работы с AniScan нужна подписка на канал.</b>\n\n"
            "После первой подписки вы получите 1 бесплатный поиск.\n"
            "После проверки подписки отправьте кадр из аниме.",
            reply_markup=subscription_keyboard(config.channel_url),
        )
        return

    trial_granted = False
    if not config.is_admin(user.user_id):
        trial_granted = await db.grant_trial_if_eligible(user.user_id)
    user = await db.get_user(user.user_id)
    assert user is not None

    intro = (
        "<b>🎌 AniScan</b>\n\n"
        "Отправьте один кадр из аниме. Бот определит название и персонажа, "
        "а затем независимо перепроверит результат через Qwen.\n\n"
        "Ответ будет кратким: название, персонаж, точность и описание сцены."
    )
    if trial_granted:
        intro = "🎁 <b>Начислен 1 пробный запрос.</b>\n\n" + intro
    if prefix:
        intro = prefix + "\n\n" + intro

    await message.answer(
        intro,
        reply_markup=main_keyboard(user, config.channel_url, config.is_admin(user.user_id)),
    )


@router.message(CommandStart())
async def start_handler(message: Message, bot: Bot, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    referrer_id: int | None = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            raw = parts[1][4:]
            if raw.isdigit():
                referrer_id = int(raw)
    await _send_home(
        message,
        message.from_user,
        bot,
        db,
        config,
        referrer_id=referrer_id,
    )


@router.message(Command("menu"))
async def menu_handler(message: Message, bot: Bot, db: Database, config: Config, state: FSMContext) -> None:
    await state.clear()
    await _send_home(message, message.from_user, bot, db, config)


@router.message(Command("paysupport"))
async def payment_support(message: Message, config: Config) -> None:
    if config.support_username:
        await message.answer(
            "По вопросам оплаты напишите: "
            f"<a href=\"https://t.me/{config.support_username}\">@{config.support_username}</a>"
        )
    else:
        await message.answer("Контакт поддержки пока не настроен.")


@router.callback_query(F.data == "sub:check")
async def check_subscription(callback: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.from_user or not callback.message:
        return
    await db.upsert_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.first_name,
    )
    subscribed = await is_subscribed(bot, config, callback.from_user.id)
    if subscribed is None:
        await callback.message.answer(
            "⚠️ Не удалось проверить подписку. Проверьте права бота в канале и повторите позже.",
            reply_markup=subscription_keyboard(config.channel_url),
        )
        return
    if not subscribed:
        penalty = await apply_unsubscribe_if_needed(db, callback.from_user.id, subscribed)
        text = "❌ Подписка не найдена. Подпишитесь на канал и повторите проверку."
        if penalty.newly_applied:
            text += (
                f"\n\n⚠️ Выдано предупреждение: <b>{penalty.warnings}/3</b>. "
                "Скидки отключены."
            )
        await callback.message.answer(text, reply_markup=subscription_keyboard(config.channel_url))
        return

    trial = False
    if not config.is_admin(callback.from_user.id):
        trial = await db.grant_trial_if_eligible(callback.from_user.id)
    user = await db.get_user(callback.from_user.id)
    assert user is not None
    text = "✅ Подписка подтверждена."
    if trial:
        text += " Начислен 1 пробный запрос."
    await callback.message.answer(
        text,
        reply_markup=main_keyboard(user, config.channel_url, config.is_admin(user.user_id)),
    )


@router.callback_query(F.data == "menu:main")
async def menu_callback(callback: CallbackQuery, bot: Bot, db: Database, config: Config, state: FSMContext) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    await state.clear()
    if callback.message:
        await _send_home(callback.message, callback.from_user, bot, db, config)


@router.callback_query(F.data.startswith("mode:"))
async def legacy_mode_handler(callback: CallbackQuery, db: Database, config: Config) -> None:
    """Старые кнопки из предыдущих сообщений переводят пользователя на единый режим."""
    await callback.answer("В AniScan теперь один режим поиска")
    await remove_inline_keyboard(callback)
    if not callback.from_user or not callback.message:
        return
    await db.upsert_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    await db.set_mode(callback.from_user.id, "basic")
    user = await db.get_user(callback.from_user.id)
    if user is None:
        return
    await callback.message.answer(
        "✅ Теперь используется единый поиск с двойной проверкой. Отправьте кадр из аниме.",
        reply_markup=main_keyboard(user, config.channel_url, config.is_admin(user.user_id)),
    )


@router.callback_query(F.data == "user:balance")
async def balance_handler(callback: CallbackQuery, db: Database, config: Config) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.from_user or not callback.message:
        return
    await db.upsert_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    user = await db.get_user(callback.from_user.id)
    assert user is not None
    await callback.message.answer(
        format_balance(user, config.is_admin(user.user_id)),
        reply_markup=back_to_menu_keyboard(),
    )


@router.callback_query(F.data == "promo:activate")
async def promo_activate_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return
    await state.set_state(PromoActivationState.waiting_code)
    await callback.message.answer(
        "Введите промокод одним сообщением. Пример: <code>ANISCAN-AB12-CD34</code>",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(PromoActivationState.waiting_code, F.text)
async def promo_activate_finish(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    result = await db.activate_promo(message.from_user.id, message.text)
    await state.clear()
    user = await db.get_user(message.from_user.id)
    assert user is not None
    if result.ok and result.kind == "credits":
        text = f"✅ {result.message}\nНачислено: <b>{result.credits}</b> запросов."
    elif result.ok and result.kind == "discount":
        penalty_note = (
            "\n⚠️ Из-за отписки у вас действуют цены без скидок; промо-скидка сохранена, "
            "но к покупкам не применяется."
            if user.penalty_prices
            else ""
        )
        text = (
            f"✅ {result.message}\n"
            f"Активирована скидка: <b>{result.discount_percent}%</b> на покупку пакетов.\n"
            f"Действует до: <b>{format_utc(result.discount_until)}</b>."
            f"{penalty_note}"
        )
    else:
        text = f"❌ {result.message}"
    await message.answer(
        text,
        reply_markup=main_keyboard(user, config.channel_url, config.is_admin(user.user_id)),
    )
