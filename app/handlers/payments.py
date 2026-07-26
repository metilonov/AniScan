from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from app.config import Config
from app.db import Database
from app.keyboards import main_keyboard, shop_mode_keyboard
from app.pricing import (
    PACKAGES,
    make_payload,
    package_base_price,
    package_discount_percent,
    package_list_price,
    package_price,
    parse_payload,
)
from app.services.pricing import build_offer, offer_lines
from app.services.text import remove_inline_keyboard


router = Router(name="payments")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def _format_millistars(value: int) -> str:
    whole, fraction = divmod(max(0, value), 1000)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:03d}".rstrip("0")


async def _show_packages(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    await remove_inline_keyboard(callback)
    if not callback.message or not callback.from_user:
        return
    await db.upsert_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.first_name,
    )
    user = await db.get_user(callback.from_user.id)
    if user is None:
        return
    offer = await build_offer(db, user, PACKAGES["b1"])
    lines = [
        "📦 Пакетные скидки:\n"
        "• 10 запросов: <s>100 ⭐</s> → <b>90 ⭐</b> (−10%)\n"
        "• 100 запросов: <s>1000 ⭐</s> → <b>800 ⭐</b> (−20%)"
    ]
    lines.extend(offer_lines(offer))
    lines.append(
        "ℹ️ Дополнительные скидки применяются поверх цены пакета. "
        "Суммарная дополнительная скидка ограничена 25%."
    )
    await callback.message.answer(
        "<b>💳 Пакеты запросов AniScan</b>\n\n" + "\n".join(lines),
        reply_markup=shop_mode_keyboard(
            "basic",
            user.penalty_prices,
            offer.total_extra_percent,
        ),
    )


@router.callback_query(F.data == "shop:root")
async def shop_root(callback: CallbackQuery, db: Database) -> None:
    await _show_packages(callback, db)


@router.callback_query(F.data.in_({"shop:basic", "shop:extended"}))
async def legacy_shop_mode(callback: CallbackQuery, db: Database) -> None:
    await _show_packages(callback, db)


@router.callback_query(F.data.startswith("buy:"))
async def create_invoice(callback: CallbackQuery, bot: Bot, db: Database) -> None:
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

    await db.upsert_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    user = await db.get_user(callback.from_user.id)
    if user is None:
        return

    offer = await build_offer(db, user, package)
    payload = make_payload(offer)
    discount_parts: list[str] = []
    package_discount = package_discount_percent(package, offer.penalty_prices)
    if package_discount > 0:
        list_price = package_list_price(package, offer.penalty_prices)
        package_price_stars = package_base_price(package, offer.penalty_prices)
        discount_parts.append(
            f"Исходная цена: {list_price} ⭐. "
            f"Цена пакета: {package_price_stars} ⭐. "
            f"Пакетная скидка: {package_discount}%."
        )
    if offer.total_extra_percent > 0:
        discount_parts.append(f"Дополнительная скидка: {offer.total_extra_percent}%.")
    discount_note = " " + " ".join(discount_parts) if discount_parts else ""

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=package.title,
        description=(
            "После оплаты запросы автоматически начисляются на баланс AniScan. "
            "Оплата цифровой услуги производится в Telegram Stars."
            + discount_note
        ),
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=package.title, amount=offer.final_stars)],
        start_parameter=f"aniscan-{package.key}",
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    parsed = parse_payload(query.invoice_payload)
    if parsed is None:
        await query.answer(ok=False, error_message="Некорректный платёжный пакет.")
        return

    package, payload_penalty, payload_discount, payload_milestone, payload_new = parsed
    user = await db.get_user(query.from_user.id)
    if user is None:
        await query.answer(ok=False, error_message="Сначала откройте бота и нажмите /start.")
        return

    offer = await build_offer(db, user, package)
    valid_snapshot = (
        payload_penalty == offer.penalty_prices
        and payload_discount == offer.total_extra_percent
        and payload_milestone == offer.milestone_threshold
        and payload_new == (offer.first_purchase_percent > 0)
    )
    if not valid_snapshot or query.currency != "XTR" or query.total_amount != offer.final_stars:
        await query.answer(
            ok=False,
            error_message="Цена или скидка изменилась. Откройте магазин и создайте новый счёт.",
        )
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot, db: Database, config: Config) -> None:
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return

    parsed = parse_payload(payment.invoice_payload)
    if parsed is None:
        await message.answer("Платёж получен, но пакет не распознан. Обратитесь в /paysupport.")
        return
    package, payload_penalty, payload_discount, payload_milestone, _payload_new = parsed
    expected = package_price(package, payload_penalty, payload_discount)
    if payment.currency != "XTR" or payment.total_amount != expected:
        await message.answer("Платёж получен с неожиданной суммой. Обратитесь в /paysupport.")
        return

    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    result = await db.record_payment(
        telegram_charge_id=payment.telegram_payment_charge_id,
        provider_charge_id=payment.provider_payment_charge_id,
        user_id=message.from_user.id,
        payload=payment.invoice_payload,
        stars=payment.total_amount,
        mode="basic",
        credits=package.credits,
        package_key=package.key,
        milestone_threshold=payload_milestone,
    )
    user = await db.get_user(message.from_user.id)
    if user is None:
        return

    if result.added:
        package_discount = package_discount_percent(package, payload_penalty)
        discount_lines: list[str] = []
        if package_discount > 0:
            list_price = package_list_price(package, payload_penalty)
            package_price_stars = package_base_price(package, payload_penalty)
            discount_lines.append(
                f"Исходная цена: <s>{list_price} ⭐</s>"
            )
            discount_lines.append(
                f"Цена пакета: <b>{package_price_stars} ⭐</b>"
            )
            discount_lines.append(
                f"Пакетная скидка: <b>{package_discount}%</b>"
            )
        if payload_discount > 0:
            discount_lines.append(f"Дополнительная скидка: <b>{payload_discount}%</b>")
        discount_text = ""
        if discount_lines:
            discount_text = "\n" + "\n".join(discount_lines)

        text = (
            "✅ <b>Оплата успешно получена.</b>\n\n"
            f"Начислено запросов: <b>{package.credits}</b>\n"
            f"Списано звёзд: <b>{payment.total_amount} ⭐</b>"
            f"{discount_text}"
        )
        if result.referrer_id and result.referral_reward_millistars > 0:
            reward_text = _format_millistars(result.referral_reward_millistars)
            try:
                await bot.send_message(
                    result.referrer_id,
                    "💎 <b>Реферальное начисление</b>\n\n"
                    f"Ваш реферал совершил покупку на <b>{payment.total_amount} ⭐</b>.\n"
                    f"На внутренний реферальный баланс начислено <b>{reward_text} ⭐</b> (15%).",
                )
            except Exception:
                pass
    else:
        text = "ℹ️ Этот платёж уже был обработан ранее."

    await message.answer(
        text,
        reply_markup=main_keyboard(user, config.channel_url, config.is_admin(user.user_id)),
    )
