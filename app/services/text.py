from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.models import AnimeAnalysis, SearchMode, UserRecord


_TAG_RE = re.compile(r"<[^>]+>")
_PROMO_CODE_RE = re.compile(r"(?<![A-Z0-9])ANISCAN(?:-[A-Z0-9]{3,12}){2,4}(?![A-Z0-9])")


def clean_ai_text(
    value: str | None,
    fallback: str = "Не удалось определить",
    *,
    limit: int = 500,
) -> str:
    if not value:
        value = fallback
    value = _TAG_RE.sub("", value).strip()
    value = re.sub(r"\s+", " ", value)
    if len(value) > limit:
        value = value[: max(1, limit - 1)].rstrip() + "…"
    return html.escape(value)


def clean_list(
    values: list[str],
    fallback: str = "Не удалось определить",
    *,
    max_items: int = 8,
    item_limit: int = 100,
    total_limit: int = 450,
) -> str:
    cleaned = [
        clean_ai_text(value, "", limit=item_limit)
        for value in values[:max_items]
        if value and value.strip()
    ]
    result = ", ".join(cleaned) if cleaned else html.escape(fallback)
    if len(result) > total_limit:
        result = result[: max(1, total_limit - 1)].rstrip() + "…"
    return result


def format_utc(value: str | None) -> str:
    if not value:
        return "без срока"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return "срок не определён"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def format_balance(user: UserRecord, is_admin: bool = False) -> str:
    admin_note = "\n👑 <b>Администратор:</b> запросы не списываются." if is_admin else ""
    price_note = (
        "\n⚠️ <b>Пакетные скидки отключены</b> из-за отписки от обязательного канала."
        if user.penalty_prices
        else ""
    )
    discount_note = ""
    if user.active_discount_percent > 0:
        discount_note = (
            f"\n🏷 Промо-скидка: <b>{user.active_discount_percent}%</b> "
            f"(до: <b>{html.escape(format_utc(user.active_discount_until))}</b>)"
        )
    status = "заблокирован" if user.blocked else "на проверке" if user.restricted else "активен"
    return (
        "<b>📊 Баланс AniScan</b>\n\n"
        f"🔎 Запросы: <b>{user.basic_credits}</b>\n"
        f"💎 Реферальный баланс: <b>{user.referral_balance_stars:g} ⭐</b> "
        f"(доступно целых: <b>{user.referral_spendable_stars}</b>)\n"
        f"⚠️ Предупреждения: <b>{user.warnings}/3</b>\n"
        f"Статус: <b>{status}</b>"
        f"{price_note}{discount_note}{admin_note}"
    )


def format_analysis(result: AnimeAnalysis, mode: SearchMode, remaining: int | None) -> str:
    description = clean_ai_text(
        result.scene_description,
        "Краткое описание не определено.",
        limit=180,
    )
    text = (
        "🎬 <b>Аниме найдено!</b>\n"
        f"🏷 <b>Название:</b> {clean_ai_text(result.title, limit=180)}\n"
        f"👤 <b>Персонаж:</b> {clean_ai_text(result.character, limit=160)}\n"
        f"🎯 <b>Точность:</b> {result.confidence}%\n"
        f"📝 <b>Краткое описание:</b> {description}"
    )
    if remaining is not None:
        text += f"\n\nОсталось запросов: <b>{remaining}</b>"
    return text


def format_broadcast_text(raw_text: str, *, limit: int = 3600) -> str:
    text = raw_text.strip()
    if not text:
        raise ValueError("Текст рассылки пуст")
    if len(text) > limit:
        raise ValueError(f"Текст рассылки длиннее {limit} символов")

    escaped = html.escape(text)
    return _PROMO_CODE_RE.sub(lambda match: f"<code>{match.group(0)}</code>", escaped)


async def remove_inline_keyboard(callback: CallbackQuery) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def safe_edit_or_answer(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except (TelegramBadRequest, AttributeError):
        await message.answer(text, reply_markup=reply_markup)
