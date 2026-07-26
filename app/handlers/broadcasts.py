from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import Config
from app.db import Database
from app.keyboards import (
    admin_root_keyboard,
    amount_choice_keyboard,
    back_to_menu_keyboard,
    broadcast_action_keyboard,
    broadcast_claim_keyboard,
    broadcast_confirm_keyboard,
    main_keyboard,
)
from app.models import BroadcastAction
from app.services.subscription import is_subscribed
from app.services.text import format_broadcast_text, remove_inline_keyboard


logger = logging.getLogger(__name__)

router = Router(name="broadcasts")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class BroadcastStates(StatesGroup):
    waiting_text = State()
    waiting_custom_amount = State()


ACTION_LABELS: dict[BroadcastAction, str] = {
    "channel": "быть подписанным на обязательный канал",
    "search": "выполнить хотя бы один успешный поиск после начала рассылки",
    "payment": "совершить хотя бы одну покупку после начала рассылки",
    "promo": "активировать любой промокод после начала рассылки",
    "click": "нажать кнопку получения подарка",
}


def _is_admin(config: Config, user_id: int) -> bool:
    return config.is_admin(user_id)


async def _deny(callback: CallbackQuery) -> None:
    await callback.answer("Недостаточно прав", show_alert=True)


def _campaign_text(text_html: str, action: BroadcastAction, credits: int) -> str:
    return (
        f"{text_html}\n\n"
        f"<b>Условие:</b> {ACTION_LABELS[action]}.\n"
        f"<b>Награда:</b> {credits} запросов."
    )

async def _start_broadcast(message: Message, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _is_admin(config, message.from_user.id):
        return
    await state.clear()
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer(
        "<b>📣 Новая рассылка</b>\n\n"
        "Отправьте текст сообщения одним сообщением. Если в тексте будет промокод формата "
        "<code>ANISCAN-XXXX-XXXX</code>, бот автоматически выделит его моноширинным шрифтом.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(Command("broadcast"))
async def broadcast_command(message: Message, config: Config, state: FSMContext) -> None:
    await _start_broadcast(message, config, state)


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start_callback(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _is_admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if callback.message:
        await _start_broadcast(callback.message, config, state)


@router.message(BroadcastStates.waiting_text, F.text)
async def broadcast_text(message: Message, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _is_admin(config, message.from_user.id) or not message.text:
        return
    try:
        text_html = format_broadcast_text(message.text)
    except ValueError as exc:
        await message.answer(f"❌ {exc}", reply_markup=back_to_menu_keyboard())
        return
    await state.update_data(text_html=text_html)
    await state.set_state(None)
    await message.answer(
        "<b>Предпросмотр текста:</b>\n\n" + text_html + "\n\nВыберите проверяемое действие:",
        reply_markup=broadcast_action_keyboard(),
    )


@router.callback_query(F.data.startswith("broadcast_action:"))
async def broadcast_action(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _is_admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    action = callback.data.split(":", 1)[1]
    if action not in ACTION_LABELS:
        return
    await state.update_data(action=action, mode="basic")
    await callback.message.answer(
        "Сколько бесплатных запросов выдавать одному пользователю?",
        reply_markup=amount_choice_keyboard("broadcast_amount"),
    )


@router.callback_query(F.data.startswith("broadcast_mode:"))
async def legacy_broadcast_mode(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _is_admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return
    await state.update_data(mode="basic")
    await callback.message.answer(
        "Сколько бесплатных запросов выдавать одному пользователю?",
        reply_markup=amount_choice_keyboard("broadcast_amount"),
    )


async def _show_confirmation(message: Message, state: FSMContext, amount: int) -> None:
    data = await state.get_data()
    text_html = data.get("text_html")
    action = data.get("action")
    if not isinstance(text_html, str) or action not in ACTION_LABELS:
        await state.clear()
        await message.answer("Сценарий рассылки устарел. Начните заново.", reply_markup=admin_root_keyboard())
        return
    await state.update_data(amount=amount)
    preview = _campaign_text(text_html, action, amount)
    await message.answer(
        "<b>Подтверждение рассылки</b>\n\n" + preview + "\n\nОтправить всем незаблокированным пользователям?",
        reply_markup=broadcast_confirm_keyboard(),
    )


@router.callback_query(F.data.startswith("broadcast_amount:"))
async def broadcast_amount(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _is_admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await state.set_state(BroadcastStates.waiting_custom_amount)
        await callback.message.answer(
            "Введите количество бесплатных запросов от 1 до 1 000 000.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if value.isdigit() and 1 <= int(value) <= 1_000_000:
        await _show_confirmation(callback.message, state, int(value))


@router.message(BroadcastStates.waiting_custom_amount, F.text)
async def broadcast_custom_amount(message: Message, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _is_admin(config, message.from_user.id) or not message.text:
        return
    try:
        amount = int(message.text.strip())
    except ValueError:
        amount = 0
    if not 1 <= amount <= 1_000_000:
        await message.answer("Введите целое число от 1 до 1 000 000.")
        return
    await state.set_state(None)
    await _show_confirmation(message, state, amount)


async def _send_with_retry(
    bot: Bot,
    user_id: int,
    text: str,
    broadcast_id: int,
    action: BroadcastAction,
) -> tuple[bool, str | None]:
    try:
        await bot.send_message(
            user_id,
            text,
            reply_markup=broadcast_claim_keyboard(broadcast_id, action),
        )
        return True, None
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.2)
        try:
            await bot.send_message(
                user_id,
                text,
                reply_markup=broadcast_claim_keyboard(broadcast_id, action),
            )
            return True, None
        except Exception as retry_exc:
            return False, type(retry_exc).__name__
    except Exception as exc:
        return False, type(exc).__name__


@router.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not _is_admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return

    data = await state.get_data()
    await state.clear()
    text_html = data.get("text_html")
    action = data.get("action")
    amount = data.get("amount")
    if (
        not isinstance(text_html, str)
        or action not in ACTION_LABELS
        or not isinstance(amount, int)
        or amount <= 0
    ):
        await callback.message.answer("Данные рассылки устарели. Начните заново.", reply_markup=admin_root_keyboard())
        return

    broadcast_id = await db.create_broadcast(
        created_by=callback.from_user.id,
        text_html=text_html,
        action_type=action,
        reward_mode="basic",
        reward_credits=amount,
    )
    recipients = await db.get_broadcast_recipients()
    final_text = _campaign_text(text_html, action, amount)
    progress = await callback.message.answer(
        f"⏳ Рассылка <code>#{broadcast_id}</code> запущена. Получателей: <b>{len(recipients)}</b>."
    )

    sent = 0
    failed = 0
    delivery_batch: list[tuple[int, bool, str | None]] = []
    for user_id in recipients:
        delivered, error = await _send_with_retry(bot, user_id, final_text, broadcast_id, action)
        sent += int(delivered)
        failed += int(not delivered)
        delivery_batch.append((user_id, delivered, error))
        if len(delivery_batch) >= 100:
            await db.record_broadcast_deliveries(broadcast_id, delivery_batch)
            delivery_batch.clear()
        await asyncio.sleep(0.055)

    if delivery_batch:
        await db.record_broadcast_deliveries(broadcast_id, delivery_batch)
    await db.finish_broadcast(broadcast_id, sent, failed)

    result_text = (
        f"✅ <b>Рассылка #{broadcast_id} завершена</b>\n\n"
        f"Получили сообщение: <b>{sent}</b>\n"
        f"Не доставлено: <b>{failed}</b>\n"
        f"Всего в выборке: <b>{len(recipients)}</b>"
    )
    try:
        await progress.edit_text(result_text, reply_markup=admin_root_keyboard())
    except Exception:
        await callback.message.answer(result_text, reply_markup=admin_root_keyboard())


@router.callback_query(F.data.startswith("campaign:claim:"))
async def campaign_claim(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    config: Config,
) -> None:
    if not callback.message or not callback.data:
        return
    try:
        broadcast_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная рассылка", show_alert=True)
        return

    await db.upsert_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.first_name,
    )
    broadcast = await db.get_broadcast(broadcast_id)
    if broadcast is None:
        await callback.answer("Эта акция больше недоступна", show_alert=True)
        await remove_inline_keyboard(callback)
        return

    external_verified: bool | None = None
    if broadcast["action_type"] == "channel":
        external_verified = await is_subscribed(bot, config, callback.from_user.id)
        if external_verified is None:
            await callback.answer("Не удалось проверить подписку. Попробуйте позже.", show_alert=True)
            return

    result = await db.claim_broadcast_reward(
        broadcast_id,
        callback.from_user.id,
        external_action_verified=external_verified,
    )
    if result.ok:
        await callback.answer("Награда начислена")
        await remove_inline_keyboard(callback)
        user = await db.get_user(callback.from_user.id)
        if user is None:
            return
        await callback.message.answer(
            f"🎁 Начислено <b>{result.credits}</b> запросов.",
            reply_markup=main_keyboard(user, config.channel_url, config.is_admin(user.user_id)),
        )
        return

    if result.reason == "already_claimed":
        await callback.answer("Вы уже получили эту награду", show_alert=True)
        await remove_inline_keyboard(callback)
    elif result.reason == "not_completed":
        await callback.answer("Условие ещё не выполнено. Выполните его и нажмите снова.", show_alert=True)
    elif result.reason == "blocked":
        await callback.answer("Ваш доступ к боту заблокирован", show_alert=True)
        await remove_inline_keyboard(callback)
    else:
        await callback.answer("Акция недоступна", show_alert=True)
        await remove_inline_keyboard(callback)
