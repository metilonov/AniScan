from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.config import Config
from app.db import Database
from app.keyboards import (
    back_to_menu_keyboard,
    main_keyboard,
    referral_menu_keyboard,
    referral_shop_mode_keyboard,
)
from app.pricing import PACKAGES
from app.services.pricing import build_offer, offer_lines
from app.services.text import remove_inline_keyboard


router = Router(name="referrals")
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def _format_millistars(value: int) -> str:
    whole, fraction = divmod(max(0, value), 1000)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:03d}".rstrip("0")


async def _referral_link(bot: Bot, user_id: int) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start=ref_{user_id}"


@router.message(Command("referrals"), F.chat.type == ChatType.PRIVATE)
async def referral_command(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    stats = await db.referral_stats(message.from_user.id)
    link = await _referral_link(bot, message.from_user.id)
    await message.answer(
        "<b>👥 Реферальная система AniScan</b>\n\n"
        "За каждую оплату приглашённого друга начисляется <b>15%</b> "
        "на внутренний реферальный баланс.\n\n"
        f"Рефералов: <b>{stats['count']}</b>\n"
        f"Заработано: <b>{_format_millistars(stats['earned_millistars'])} ⭐</b>\n"
        f"Доступно: <b>{_format_millistars(stats['balance_millistars'])} ⭐</b>\n\n"
        f"Ваша ссылка:\n<code>{escape(link)}</code>",
        reply_markup=referral_menu_keyboard(link),
    )


@router.callback_query(F.data == "ref:root")
async def referral_root(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.from_user:
        return
    await db.upsert_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    stats = await db.referral_stats(callback.from_user.id)
    link = await _referral_link(bot, callback.from_user.id)
    await callback.message.answer(
        "<b>👥 Реферальная система AniScan</b>\n\n"
        "За каждую оплату приглашённого друга вам начисляется <b>15%</b> "
        "на внутренний реферальный баланс. Начисления повторяются при каждой его покупке.\n\n"
        f"Рефералов: <b>{stats['count']}</b>\n"
        f"Заработано всего: <b>{_format_millistars(stats['earned_millistars'])} ⭐</b>\n"
        f"Доступный баланс: <b>{_format_millistars(stats['balance_millistars'])} ⭐</b>\n"
        f"Можно потратить сейчас: <b>{stats['balance_millistars'] // 1000} целых ⭐</b>\n\n"
        f"Ваша ссылка:\n<code>{escape(link)}</code>",
        reply_markup=referral_menu_keyboard(link),
    )


@router.callback_query(F.data == "ref:list")
async def referral_list(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.from_user:
        return
    stats = await db.referral_stats(callback.from_user.id, limit=30)
    items: list[dict] = stats["items"]
    if not items:
        await callback.message.answer(
            "У вас пока нет рефералов. Отправьте другу персональную ссылку из раздела «Рефералы».",
            reply_markup=back_to_menu_keyboard(),
        )
        return
    lines = ["<b>👥 Ваши рефералы</b>", ""]
    for index, item in enumerate(items, start=1):
        username = f"@{escape(item['username'])}" if item.get("username") else escape(item.get("first_name") or "Без username")
        reward = _format_millistars(int(item.get("reward_millistars") or 0))
        lines.append(
            f"{index}. {username} — оплат: <b>{int(item.get('payments_count') or 0)}</b>, "
            f"принёс: <b>{reward} ⭐</b>"
        )
    await callback.message.answer("\n".join(lines), reply_markup=back_to_menu_keyboard())


async def _show_referral_packages(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.from_user:
        return
    user = await db.get_user(callback.from_user.id)
    if user is None:
        return
    offer = await build_offer(db, user, PACKAGES["b1"])
    lines = offer_lines(offer)
    suffix = "\n\n" + "\n".join(lines) if lines else ""
    await callback.message.answer(
        "<b>⭐ Покупка за реферальный баланс</b>\n\n"
        f"Доступно: <b>{_format_millistars(user.referral_balance_millistars)} ⭐</b>."
        f"{suffix}",
        reply_markup=referral_shop_mode_keyboard(
            "basic",
            user.penalty_prices,
            offer.total_extra_percent,
        ),
    )


@router.callback_query(F.data == "ref:shop")
async def referral_shop(callback: CallbackQuery, db: Database) -> None:
    await _show_referral_packages(callback, db)


@router.callback_query(F.data.in_({"refshop:basic", "refshop:extended"}))
async def referral_shop_mode(callback: CallbackQuery, db: Database) -> None:
    await _show_referral_packages(callback, db)


@router.callback_query(F.data.startswith("refbuy:"))
async def referral_buy(callback: CallbackQuery, db: Database, config: Config) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.from_user or not callback.data:
        return
    key = callback.data.split(":", 1)[1]
    if key.startswith("e"):
        key = "b" + key[1:]
    package = PACKAGES.get(key)
    if package is None or key not in {"b1", "b10", "b100"}:
        await callback.message.answer("Пакет не найден.")
        return
    user = await db.get_user(callback.from_user.id)
    if user is None:
        return
    offer = await build_offer(db, user, package)
    result = await db.redeem_referral_package(
        user_id=user.user_id,
        package_key=package.key,
        mode="basic",
        credits=package.credits,
        stars=offer.final_stars,
        milestone_threshold=offer.milestone_threshold,
    )
    user = await db.get_user(user.user_id)
    assert user is not None
    if not result.ok:
        if result.reason == "insufficient":
            text = (
                "❌ Недостаточно реферальных звёзд.\n\n"
                f"Нужно: <b>{offer.final_stars} ⭐</b>\n"
                f"Доступно: <b>{_format_millistars(result.remaining_millistars)} ⭐</b>"
            )
        else:
            text = "❌ Не удалось выполнить покупку за реферальный баланс."
    else:
        text = (
            "✅ <b>Покупка за реферальный баланс выполнена.</b>\n\n"
            f"Начислено: <b>{package.credits}</b> запросов\n"
            f"Списано: <b>{offer.final_stars} реф. ⭐</b>\n"
            f"Осталось: <b>{_format_millistars(result.remaining_millistars)} ⭐</b>"
        )
    await callback.message.answer(
        text,
        reply_markup=main_keyboard(user, config.channel_url, config.is_admin(user.user_id)),
    )
