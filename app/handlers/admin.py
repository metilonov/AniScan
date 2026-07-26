from __future__ import annotations

import html
import secrets
import string
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import Config
from app.db import Database
from app.keyboards import (
    activations_keyboard,
    admin_root_keyboard,
    amount_choice_keyboard,
    back_to_menu_keyboard,
    discount_choice_keyboard,
    duration_keyboard,
    promo_kind_keyboard,
    qwen_key_delete_confirm_keyboard,
    qwen_key_detail_keyboard,
    qwen_keys_keyboard,
)
from app.models import PromoKind
from app.services.ai import AnimeAI
from app.services.text import remove_inline_keyboard


router = Router(name="admin")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class GrantStates(StatesGroup):
    waiting_target = State()
    waiting_custom_amount = State()


class BlockStates(StatesGroup):
    waiting_block_target = State()
    waiting_unblock_target = State()


class PromoStates(StatesGroup):
    waiting_custom_credits = State()
    waiting_custom_discount = State()
    waiting_custom_uses = State()


class QwenKeyStates(StatesGroup):
    waiting_key = State()


def _admin(config: Config, user_id: int) -> bool:
    return config.is_admin(user_id)


async def _deny(callback: CallbackQuery) -> None:
    await callback.answer("Недостаточно прав", show_alert=True)


async def _notify_user(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass


async def _send_user_stats(message: Message, db: Database) -> None:
    stats = await db.stats()
    await message.answer(
        "<b>👥 Пользователи AniScan</b>\n\n"
        f"Всего: <b>{stats['total_users']}</b>\n"
        f"Доступ активен: <b>{stats['active_users']}</b>\n"
        f"Заблокировано: <b>{stats['blocked_users']}</b>\n"
        f"На модерации: <b>{stats['restricted_users']}</b>\n\n"
        f"Новых за 24 часа: <b>{stats['new_day']}</b>\n"
        f"Новых за 7 дней: <b>{stats['new_week']}</b>\n"
        f"Новых за 30 дней: <b>{stats['new_month']}</b>",
        reply_markup=admin_root_keyboard(),
    )


@router.message(Command("admin"))
async def admin_command(message: Message, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _admin(config, message.from_user.id):
        return
    await state.clear()
    await message.answer("<b>🛠 Админ-панель AniScan</b>", reply_markup=admin_root_keyboard())


@router.message(Command("users"))
async def users_command(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _admin(config, message.from_user.id):
        return
    await state.clear()
    await _send_user_stats(message, db)


@router.callback_query(F.data == "admin:root")
async def admin_root(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    await state.clear()
    if callback.message:
        await callback.message.answer("<b>🛠 Админ-панель AniScan</b>", reply_markup=admin_root_keyboard())


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery, db: Database, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    await state.clear()
    if callback.message:
        await _send_user_stats(callback.message, db)


def _qwen_key_status(record) -> str:
    now = datetime.now(timezone.utc)
    if record.status == "active" and record.cooldown_until:
        try:
            until = datetime.fromisoformat(record.cooldown_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until > now:
                return "🟡 Временный лимит"
        except ValueError:
            pass
    return {
        "active": "🟢 Активен",
        "exhausted": "🟠 Квота исчерпана",
        "invalid": "🔴 Недействителен",
        "disabled": "⚪ Отключён",
    }.get(record.status, "❔ Неизвестно")


def _qwen_key_text(record) -> str:
    last_error = "нет"
    if record.last_error:
        last_error = html.escape(record.last_error[:500])
    last_used = html.escape(record.last_used_at or "ещё не использовался")
    return (
        f"<b>🔑 Ключ Qwen #{record.id}</b>\n\n"
        f"Название: <b>{html.escape(record.label)}</b>\n"
        f"Статус: <b>{_qwen_key_status(record)}</b>\n"
        f"Ключ: <code>{html.escape(record.masked)}</code>\n"
        f"Base URL: <code>{html.escape(record.base_url)}</code>\n"
        f"Успешных вызовов: <b>{record.usage_count}</b>\n"
        f"Последнее использование: <code>{last_used}</code>\n\n"
        f"Последняя ошибка: <code>{last_error}</code>"
    )


async def _show_qwen_keys(message: Message, db: Database) -> None:
    keys = await db.list_qwen_keys()
    counts = await db.qwen_key_counts()
    text = (
        "<b>🔑 Ключи Qwen</b>\n\n"
        f"Всего: <b>{len(keys)}</b>\n"
        f"Активных: <b>{counts['active']}</b>\n"
        f"Временный лимит: <b>{counts['cooling']}</b>\n"
        f"С исчерпанной квотой: <b>{counts['exhausted']}</b>\n"
        f"Недействительных: <b>{counts['invalid']}</b>\n"
        f"Отключённых вручную: <b>{counts['disabled']}</b>\n\n"
        "Бот автоматически переключается на следующий активный ключ. "
        "Полные значения ключей в панели не показываются."
    )
    await message.answer(text, reply_markup=qwen_keys_keyboard(keys))


@router.callback_query(F.data == "admin:qwen")
async def admin_qwen_keys(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    await state.clear()
    if callback.message:
        await _show_qwen_keys(callback.message, db)


@router.callback_query(F.data == "admin:qwen:add")
async def admin_qwen_add(
    callback: CallbackQuery,
    config: Config,
    state: FSMContext,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return
    await state.set_state(QwenKeyStates.waiting_key)
    await callback.message.answer(
        "<b>➕ Добавление ключа Qwen</b>\n\n"
        "Отправьте одним сообщением один из вариантов:\n\n"
        "<code>sk-ваш_ключ</code>\n"
        "<code>Название | sk-ваш_ключ</code>\n"
        "<code>Название | sk-ваш_ключ | https://WORKSPACE.../compatible-mode/v1</code>\n\n"
        "Если Base URL не указан, используется адрес из <code>.env</code>. "
        "Сообщение с ключом будет удалено после сохранения.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(QwenKeyStates.waiting_key, F.text)
async def admin_qwen_add_value(
    message: Message,
    db: Database,
    config: Config,
    anime_ai: AnimeAI,
    state: FSMContext,
) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return

    raw = message.text.strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) == 1:
        label = "Резервный ключ"
        api_key = parts[0]
        base_url = config.qwen_base_url
    elif len(parts) == 2:
        label, api_key = parts
        base_url = config.qwen_base_url
    elif len(parts) == 3:
        label, api_key, base_url = parts
    else:
        await message.answer(
            "Неверный формат. Используйте: <code>Название | API-ключ | Base URL</code>."
        )
        return

    if len(api_key) < 16 or any(char.isspace() for char in api_key):
        await message.answer("API-ключ выглядит некорректно. Проверьте его и отправьте снова.")
        return
    base_url = base_url.rstrip("/")
    if not base_url.startswith("https://") or not base_url.endswith("/v1"):
        await message.answer(
            "Base URL должен начинаться с <code>https://</code> и заканчиваться на <code>/v1</code>."
        )
        return
    if not label:
        label = "Резервный ключ"
    label = label[:60]

    record, created = await db.add_qwen_key(
        api_key=api_key,
        base_url=base_url,
        label=label,
        added_by=message.from_user.id,
    )
    await anime_ai.forget_key(record.id)
    anime_ai.reset_quota_notification()
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass

    action = "добавлен" if created else "обновлён и активирован"
    await message.answer(
        f"✅ Ключ <b>#{record.id}</b> {action}.\n"
        f"Название: <b>{html.escape(record.label)}</b>\n"
        f"Маска: <code>{html.escape(record.masked)}</code>",
        reply_markup=qwen_key_detail_keyboard(record),
    )


@router.callback_query(F.data.startswith("admin:qwen:view:"))
async def admin_qwen_view(
    callback: CallbackQuery,
    db: Database,
    config: Config,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    try:
        key_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    record = await db.get_qwen_key(key_id)
    if record is None:
        await callback.message.answer("Ключ не найден.", reply_markup=admin_root_keyboard())
        return
    await callback.message.answer(_qwen_key_text(record), reply_markup=qwen_key_detail_keyboard(record))


@router.callback_query(F.data.startswith("admin:qwen:enable:"))
async def admin_qwen_enable(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    anime_ai: AnimeAI,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    try:
        key_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    if not await db.set_qwen_key_enabled(key_id, True):
        await callback.message.answer("Ключ не найден.", reply_markup=admin_root_keyboard())
        return
    await anime_ai.forget_key(key_id)
    anime_ai.reset_quota_notification()
    record = await db.get_qwen_key(key_id)
    if record:
        await callback.message.answer(
            "✅ Ключ активирован.",
            reply_markup=qwen_key_detail_keyboard(record),
        )


@router.callback_query(F.data.startswith("admin:qwen:disable:"))
async def admin_qwen_disable(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    anime_ai: AnimeAI,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    try:
        key_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    if not await db.set_qwen_key_enabled(key_id, False):
        await callback.message.answer("Ключ не найден.", reply_markup=admin_root_keyboard())
        return
    await anime_ai.forget_key(key_id)
    record = await db.get_qwen_key(key_id)
    if record:
        await callback.message.answer(
            "⏸ Ключ отключён вручную.",
            reply_markup=qwen_key_detail_keyboard(record),
        )


@router.callback_query(F.data.startswith("admin:qwen:delete:"))
async def admin_qwen_delete_request(
    callback: CallbackQuery,
    db: Database,
    config: Config,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    try:
        key_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    record = await db.get_qwen_key(key_id)
    if record is None:
        await callback.message.answer("Ключ не найден.", reply_markup=admin_root_keyboard())
        return
    await callback.message.answer(
        f"Удалить ключ <b>#{record.id}</b> — {html.escape(record.label)}?",
        reply_markup=qwen_key_delete_confirm_keyboard(record.id),
    )


@router.callback_query(F.data.startswith("admin:qwen:delete_confirm:"))
async def admin_qwen_delete_confirm(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    anime_ai: AnimeAI,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    try:
        key_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    deleted = await db.delete_qwen_key(key_id)
    await anime_ai.forget_key(key_id)
    if deleted:
        await callback.message.answer("🗑 Ключ удалён.")
    else:
        await callback.message.answer("Ключ уже удалён или не найден.")
    await _show_qwen_keys(callback.message, db)


@router.callback_query(F.data == "admin:balance")
async def admin_balance(callback: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return

    stats = await db.stats()
    try:
        balance = await bot.get_my_star_balance()
        amount = int(getattr(balance, "amount", 0) or 0)
        nanostars = int(getattr(balance, "nanostar_amount", 0) or 0)
        star_text = f"{amount} ⭐"
        if nanostars:
            star_text += f" + {nanostars} нанозвёзд"
    except Exception as exc:
        star_text = f"не удалось получить ({type(exc).__name__})"

    text = (
        "<b>⭐ Баланс и статистика бота</b>\n\n"
        f"Баланс Telegram: <b>{star_text}</b>\n"
        f"Получено по базе: <b>{stats['stars_received_db']} ⭐</b>\n"
        f"Успешных оплат: <b>{stats['payments_count']}</b>\n"
        f"Обработанных поисков: <b>{stats['searches_count']}</b>\n\n"
        f"Пользователей: <b>{stats['total_users']}</b>\n"
        f"Заблокировано: <b>{stats['blocked_users']}</b>\n"
        f"На модерации: <b>{stats['restricted_users']}</b>\n"
        f"Запросов на балансах: <b>{stats['basic_credits']}</b>"
    )
    await callback.message.answer(text, reply_markup=admin_root_keyboard())


async def _resolve_target(message: Message, bot: Bot, db: Database, raw: str):
    target = await db.find_user(raw)
    if target is not None:
        return target
    try:
        chat = await bot.get_chat(raw.strip())
        if chat.type == ChatType.PRIVATE:
            await db.upsert_user(
                chat.id,
                getattr(chat, "username", None),
                getattr(chat, "first_name", None),
            )
            return await db.get_user(chat.id)
    except Exception:
        return None
    return None


async def _apply_block_action(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    raw_target: str,
    *,
    unblock: bool,
) -> None:
    target = await _resolve_target(message, bot, db, raw_target)
    if target is None:
        await message.answer(
            "Пользователь не найден. Он должен хотя бы один раз открыть бота, "
            "после чего его можно найти по @username или ID.",
            reply_markup=admin_root_keyboard(),
        )
        return
    if config.is_admin(target.user_id):
        await message.answer("Администратора нельзя заблокировать через эту команду.", reply_markup=admin_root_keyboard())
        return
    if unblock:
        ok = await db.unblock_user(target.user_id, f"Разблокирован администратором {message.from_user.id}")
        if ok:
            await message.answer(
                f"✅ Пользователь <code>{target.user_id}</code> разблокирован. Предупреждения сброшены.",
                reply_markup=admin_root_keyboard(),
            )
            await _notify_user(bot, target.user_id, "✅ Администратор разблокировал ваш доступ к AniScan.")
        else:
            await message.answer("Не удалось разблокировать пользователя.", reply_markup=admin_root_keyboard())
    else:
        await db.block_user(target.user_id, f"Заблокирован администратором {message.from_user.id}")
        await message.answer(
            f"⛔ Пользователь <code>{target.user_id}</code> заблокирован навсегда.",
            reply_markup=admin_root_keyboard(),
        )
        await _notify_user(bot, target.user_id, "⛔ Администратор заблокировал ваш доступ к AniScan.")


@router.message(Command("block"))
async def block_command(
    message: Message, bot: Bot, db: Database, config: Config, state: FSMContext
) -> None:
    if not message.from_user or not _admin(config, message.from_user.id):
        return
    await state.clear()
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        await state.set_state(BlockStates.waiting_block_target)
        await message.answer("Введите @username или Telegram user ID для блокировки.", reply_markup=back_to_menu_keyboard())
        return
    await _apply_block_action(message, bot, db, config, raw[1], unblock=False)


@router.message(Command("unblock"))
async def unblock_command(
    message: Message, bot: Bot, db: Database, config: Config, state: FSMContext
) -> None:
    if not message.from_user or not _admin(config, message.from_user.id):
        return
    await state.clear()
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        await state.set_state(BlockStates.waiting_unblock_target)
        await message.answer("Введите @username или Telegram user ID для разблокировки.", reply_markup=back_to_menu_keyboard())
        return
    await _apply_block_action(message, bot, db, config, raw[1], unblock=True)


@router.callback_query(F.data.in_({"admin:block", "admin:unblock"}))
async def block_start(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    await state.clear()
    unblock = callback.data == "admin:unblock"
    await state.set_state(
        BlockStates.waiting_unblock_target if unblock else BlockStates.waiting_block_target
    )
    action = "разблокировки" if unblock else "блокировки"
    await callback.message.answer(
        f"Введите @username или Telegram user ID для {action}.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(BlockStates.waiting_block_target, F.text)
async def block_target_state(
    message: Message, bot: Bot, db: Database, config: Config, state: FSMContext
) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return
    await state.clear()
    await _apply_block_action(message, bot, db, config, message.text, unblock=False)


@router.message(BlockStates.waiting_unblock_target, F.text)
async def unblock_target_state(
    message: Message, bot: Bot, db: Database, config: Config, state: FSMContext
) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return
    await state.clear()
    await _apply_block_action(message, bot, db, config, message.text, unblock=True)


@router.callback_query(F.data == "admin:grant")
async def grant_start(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return
    await state.clear()
    await state.set_state(GrantStates.waiting_target)
    await callback.message.answer(
        "Введите Telegram user ID или @username пользователя.\n"
        "Пользователь должен хотя бы один раз открыть бота.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(GrantStates.waiting_target, F.text)
async def grant_target(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return

    target = await _resolve_target(message, bot, db, message.text)

    if target is None:
        await message.answer(
            "Пользователь не найден. Проверьте ID/@username и попробуйте ещё раз.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await state.update_data(target_id=target.user_id, mode="basic")
    await message.answer(
        f"Пользователь найден: <code>{target.user_id}</code>.\nСколько запросов выдать?",
        reply_markup=amount_choice_keyboard("grant_amount"),
    )


@router.callback_query(F.data.startswith("grant_mode:"))
async def legacy_grant_mode(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return
    await state.update_data(mode="basic")
    await callback.message.answer("Сколько запросов выдать?", reply_markup=amount_choice_keyboard("grant_amount"))


async def _finish_grant(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
    amount: int,
) -> None:
    data = await state.get_data()
    target_id = int(data["target_id"])
    ok = await db.add_credits(target_id, "basic", amount)
    await state.clear()
    if not ok:
        await message.answer("Не удалось начислить запросы.", reply_markup=admin_root_keyboard())
        return
    await message.answer(
        f"✅ Пользователю <code>{target_id}</code> выдано <b>{amount}</b> запросов.",
        reply_markup=admin_root_keyboard(),
    )
    await _notify_user(bot, target_id, f"🎁 Администратор начислил вам <b>{amount}</b> запросов.")


@router.callback_query(F.data.startswith("grant_amount:"))
async def grant_amount(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await state.set_state(GrantStates.waiting_custom_amount)
        await callback.message.answer(
            "Введите количество запросов числом от 1 до 1 000 000.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if value.isdigit():
        await _finish_grant(callback.message, bot, db, state, int(value))


@router.message(GrantStates.waiting_custom_amount, F.text)
async def grant_custom_amount(
    message: Message,
    bot: Bot,
    db: Database,
    config: Config,
    state: FSMContext,
) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return
    try:
        amount = int(message.text.strip())
    except ValueError:
        amount = 0
    if not 1 <= amount <= 1_000_000:
        await message.answer("Введите целое число от 1 до 1 000 000.")
        return
    await _finish_grant(message, bot, db, state, amount)


@router.callback_query(F.data == "admin:promo")
async def promo_start(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return
    await state.clear()
    await callback.message.answer(
        "Что будет выдавать промокод?",
        reply_markup=promo_kind_keyboard(),
    )


@router.callback_query(F.data.startswith("promo_kind:"))
async def promo_kind(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    kind = callback.data.split(":", 1)[1]
    if kind not in {"credits", "discount"}:
        return
    await state.update_data(kind=kind)
    if kind == "credits":
        await state.update_data(mode="basic")
        await callback.message.answer(
            "Сколько запросов начисляет одна активация?",
            reply_markup=amount_choice_keyboard("promo_credits"),
        )
    else:
        await callback.message.answer(
            "Выберите размер скидки на покупку пакетов:",
            reply_markup=discount_choice_keyboard(),
        )


@router.callback_query(F.data.startswith("promo_mode:"))
async def legacy_promo_mode(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message:
        return
    await state.update_data(mode="basic")
    await callback.message.answer(
        "Сколько запросов начисляет одна активация?",
        reply_markup=amount_choice_keyboard("promo_credits"),
    )


@router.callback_query(F.data.startswith("promo_credits:"))
async def promo_credits(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await state.set_state(PromoStates.waiting_custom_credits)
        await callback.message.answer(
            "Введите количество запросов на одну активацию (1–1 000 000).",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if value.isdigit():
        await state.update_data(credits=int(value))
        await callback.message.answer("Выберите срок действия промокода:", reply_markup=duration_keyboard())


@router.message(PromoStates.waiting_custom_credits, F.text)
async def promo_custom_credits(message: Message, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return
    try:
        credits = int(message.text.strip())
    except ValueError:
        credits = 0
    if not 1 <= credits <= 1_000_000:
        await message.answer("Введите целое число от 1 до 1 000 000.")
        return
    await state.update_data(credits=credits)
    await state.set_state(None)
    await message.answer("Выберите срок действия промокода:", reply_markup=duration_keyboard())


@router.callback_query(F.data.startswith("promo_discount:"))
async def promo_discount(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await state.set_state(PromoStates.waiting_custom_discount)
        await callback.message.answer(
            "Введите скидку целым числом от 1 до 25 процентов.",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if value.isdigit() and 1 <= int(value) <= 25:
        await state.update_data(discount_percent=int(value))
        await callback.message.answer("Выберите срок действия промокода:", reply_markup=duration_keyboard())


@router.message(PromoStates.waiting_custom_discount, F.text)
async def promo_custom_discount(message: Message, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return
    try:
        percent = int(message.text.strip().rstrip("%"))
    except ValueError:
        percent = 0
    if not 1 <= percent <= 25:
        await message.answer("Введите целое число от 1 до 25.")
        return
    await state.update_data(discount_percent=percent)
    await state.set_state(None)
    await message.answer("Выберите срок действия промокода:", reply_markup=duration_keyboard())


@router.callback_query(F.data.startswith("promo_duration:"))
async def promo_duration(callback: CallbackQuery, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    raw = callback.data.split(":", 1)[1]
    if not raw.isdigit():
        return
    days = int(raw)
    expires_at = None
    if days > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    await state.update_data(expires_at=expires_at, duration_days=days)
    await callback.message.answer(
        "Выберите максимальное количество активаций:",
        reply_markup=activations_keyboard(),
    )


def _generate_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    part1 = "".join(secrets.choice(alphabet) for _ in range(4))
    part2 = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"ANISCAN-{part1}-{part2}"


async def _finish_promo(
    message: Message,
    db: Database,
    config: Config,
    state: FSMContext,
    max_activations: int,
) -> None:
    data = await state.get_data()
    kind: PromoKind = data.get("kind", "credits")
    expires_at = data.get("expires_at")
    duration_days = int(data.get("duration_days", 0))
    creator_id = message.from_user.id if message.from_user else next(iter(config.admin_ids))

    code = ""
    for _ in range(20):
        code = _generate_code()
        if await db.promo_code_exists(code):
            continue
        try:
            if kind == "credits":
                await db.create_promo(
                    code=code,
                    mode="basic",
                    credits=int(data["credits"]),
                    max_activations=max_activations,
                    expires_at=expires_at,
                    created_by=creator_id,
                )
            else:
                await db.create_discount_promo(
                    code=code,
                    discount_percent=int(data["discount_percent"]),
                    max_activations=max_activations,
                    expires_at=expires_at,
                    created_by=creator_id,
                )
            break
        except aiosqlite.IntegrityError:
            continue
    else:
        await state.clear()
        await message.answer("Не удалось сгенерировать уникальный промокод.", reply_markup=admin_root_keyboard())
        return

    await state.clear()
    duration_label = "без срока" if duration_days == 0 else f"{duration_days} дн."
    if kind == "credits":
        credits = int(data["credits"])
        reward = f"<b>{credits}</b> запросов"
    else:
        reward = f"скидка <b>{int(data['discount_percent'])}%</b> на покупки"

    await message.answer(
        "✅ <b>Промокод создан</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Награда: {reward}\n"
        f"Срок: <b>{duration_label}</b>\n"
        f"Активаций: <b>{max_activations}</b>",
        reply_markup=admin_root_keyboard(),
    )


@router.callback_query(F.data.startswith("promo_uses:"))
async def promo_uses(callback: CallbackQuery, db: Database, config: Config, state: FSMContext) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await state.set_state(PromoStates.waiting_custom_uses)
        await callback.message.answer(
            "Введите максимальное количество активаций (1–1 000 000).",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    if value.isdigit():
        await _finish_promo(callback.message, db, config, state, int(value))


@router.message(PromoStates.waiting_custom_uses, F.text)
async def promo_custom_uses(message: Message, db: Database, config: Config, state: FSMContext) -> None:
    if not message.from_user or not _admin(config, message.from_user.id) or not message.text:
        return
    try:
        uses = int(message.text.strip())
    except ValueError:
        uses = 0
    if not 1 <= uses <= 1_000_000:
        await message.answer("Введите целое число от 1 до 1 000 000.")
        return
    await _finish_promo(message, db, config, state, uses)


@router.callback_query(F.data.startswith("mod:block:"))
async def moderation_block(callback: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    try:
        case_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    user_id = await db.resolve_moderation_case(case_id, "blocked", callback.from_user.id)
    if user_id is None:
        await callback.message.answer("ℹ️ Это дело уже обработано другим администратором.")
        return
    await db.block_user(user_id, f"Заблокирован администратором по делу #{case_id}")
    await callback.message.answer(f"⛔ Пользователь <code>{user_id}</code> заблокирован навсегда.")
    await _notify_user(bot, user_id, "⛔ Администратор заблокировал ваш доступ к AniScan навсегда.")


@router.callback_query(F.data.startswith("mod:warn:"))
async def moderation_warn(callback: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if not _admin(config, callback.from_user.id):
        await _deny(callback)
        return
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.data:
        return
    try:
        case_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    user_id = await db.resolve_moderation_case(case_id, "warning", callback.from_user.id)
    if user_id is None:
        await callback.message.answer("ℹ️ Это дело уже обработано другим администратором.")
        return
    result = await db.add_warning(user_id, f"Предупреждение администратора по делу #{case_id}")
    if result.blocked:
        await callback.message.answer(
            f"⛔ Пользователь <code>{user_id}</code> получил третье предупреждение и заблокирован."
        )
        await _notify_user(
            bot,
            user_id,
            "⛔ Вы получили третье предупреждение. Доступ к AniScan заблокирован навсегда.",
        )
    else:
        await callback.message.answer(
            f"⚠️ Пользователю <code>{user_id}</code> выдано предупреждение "
            f"(<b>{result.warnings}/3</b>), ограничение снято."
        )
        await _notify_user(
            bot,
            user_id,
            f"⚠️ Администратор выдал предупреждение: <b>{result.warnings}/3</b>. "
            "Доступ к боту восстановлен.",
        )
