from __future__ import annotations

from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import BroadcastAction, QwenKeyRecord, SearchMode, UserRecord
from app.pricing import (
    PACKAGES,
    package_base_price,
    package_discount_percent,
    package_list_price,
    package_price,
)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_keyboard(channel_url: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=channel_url)],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="sub:check")],
            [InlineKeyboardButton(text="💳 Купить запросы", callback_data="shop:root")],
        ]
    )


def main_keyboard(user: UserRecord, channel_url: str, is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💳 Купить запросы", callback_data="shop:root"),
            InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="promo:activate"),
        ],
        [
            InlineKeyboardButton(text="📊 Мой баланс", callback_data="user:balance"),
            InlineKeyboardButton(text="👥 Рефералы", callback_data="ref:root"),
        ],
        [InlineKeyboardButton(text="📢 Канал", url=channel_url)],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:root")])
    return _markup(rows)


def shop_root_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="🔎 Выбрать пакет", callback_data="shop:basic")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )


def shop_mode_keyboard(
    mode: SearchMode = "basic",
    penalty_prices: bool = False,
    promo_discount_percent: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key in ("b1", "b10", "b100"):
        package = PACKAGES[key]
        list_price = package_list_price(package, penalty_prices)
        package_price_stars = package_base_price(package, penalty_prices)
        stars = package_price(package, penalty_prices, promo_discount_percent)
        package_discount = package_discount_percent(package, penalty_prices)

        price_steps = [list_price]
        if package_price_stars != price_steps[-1]:
            price_steps.append(package_price_stars)
        if stars != price_steps[-1]:
            price_steps.append(stars)

        if len(price_steps) == 1:
            price_text = str(price_steps[0])
        else:
            price_text = " → ".join(str(value) for value in price_steps)
        label = f"{package.credits} запросов — {price_text} ⭐"

        discounts: list[str] = []
        if package_discount > 0:
            discounts.append(f"скидка {package_discount}%")
        if promo_discount_percent > 0 and not penalty_prices:
            discounts.append(f"доп. {promo_discount_percent}%")
        if discounts:
            label += " · " + " + ".join(discounts)

        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")])
    return _markup(rows)


def admin_root_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="👥 Количество пользователей", callback_data="admin:users")],
            [InlineKeyboardButton(text="🎁 Выдать запросы", callback_data="admin:grant")],
            [
                InlineKeyboardButton(text="⛔ Заблокировать", callback_data="admin:block"),
                InlineKeyboardButton(text="✅ Разблокировать", callback_data="admin:unblock"),
            ],
            [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin:promo")],
            [InlineKeyboardButton(text="🔑 Ключи Qwen", callback_data="admin:qwen")],
            [InlineKeyboardButton(text="⭐ Баланс бота", callback_data="admin:balance")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )


def qwen_keys_keyboard(keys: list[QwenKeyRecord]) -> InlineKeyboardMarkup:
    status_icons = {
        "active": "🟢",
        "exhausted": "🟠",
        "invalid": "🔴",
        "disabled": "⚪",
    }
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить ключ", callback_data="admin:qwen:add")]
    ]
    for record in keys[:30]:
        icon = status_icons.get(record.status, "❔")
        label = record.label.strip() or f"Ключ #{record.id}"
        if len(label) > 24:
            label = label[:21] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} #{record.id} · {label}",
                    callback_data=f"admin:qwen:view:{record.id}",
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:qwen")],
            [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:root")],
        ]
    )
    return _markup(rows)


def qwen_key_detail_keyboard(record: QwenKeyRecord) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if record.status == "active":
        rows.append(
            [InlineKeyboardButton(text="⏸ Отключить", callback_data=f"admin:qwen:disable:{record.id}")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="▶️ Активировать", callback_data=f"admin:qwen:enable:{record.id}")]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:qwen:delete:{record.id}")],
            [InlineKeyboardButton(text="⬅️ К списку ключей", callback_data="admin:qwen")],
        ]
    )
    return _markup(rows)


def qwen_key_delete_confirm_keyboard(key_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"admin:qwen:delete_confirm:{key_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:qwen:view:{key_id}")],
        ]
    )


def amount_choice_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="1", callback_data=f"{prefix}:1"),
                InlineKeyboardButton(text="10", callback_data=f"{prefix}:10"),
                InlineKeyboardButton(text="100", callback_data=f"{prefix}:100"),
            ],
            [InlineKeyboardButton(text="✍️ Свой вариант", callback_data=f"{prefix}:custom")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:root")],
        ]
    )


def promo_kind_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="🎁 Запросы", callback_data="promo_kind:credits")],
            [InlineKeyboardButton(text="🏷 Скидка на покупки", callback_data="promo_kind:discount")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:root")],
        ]
    )


def discount_choice_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="5%", callback_data="promo_discount:5"),
                InlineKeyboardButton(text="10%", callback_data="promo_discount:10"),
                InlineKeyboardButton(text="15%", callback_data="promo_discount:15"),
            ],
            [
                InlineKeyboardButton(text="20%", callback_data="promo_discount:20"),
                InlineKeyboardButton(text="25%", callback_data="promo_discount:25"),
            ],
            [InlineKeyboardButton(text="✍️ Свой процент", callback_data="promo_discount:custom")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:root")],
        ]
    )


def duration_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="1 день", callback_data="promo_duration:1"),
                InlineKeyboardButton(text="7 дней", callback_data="promo_duration:7"),
            ],
            [
                InlineKeyboardButton(text="30 дней", callback_data="promo_duration:30"),
                InlineKeyboardButton(text="Без срока", callback_data="promo_duration:0"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:root")],
        ]
    )


def activations_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                InlineKeyboardButton(text="1", callback_data="promo_uses:1"),
                InlineKeyboardButton(text="10", callback_data="promo_uses:10"),
                InlineKeyboardButton(text="100", callback_data="promo_uses:100"),
            ],
            [InlineKeyboardButton(text="✍️ Свой вариант", callback_data="promo_uses:custom")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:root")],
        ]
    )


def broadcast_action_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="📢 Быть подписанным на канал", callback_data="broadcast_action:channel")],
            [InlineKeyboardButton(text="🔎 Выполнить поиск", callback_data="broadcast_action:search")],
            [InlineKeyboardButton(text="💳 Совершить покупку", callback_data="broadcast_action:payment")],
            [InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="broadcast_action:promo")],
            [InlineKeyboardButton(text="🎁 Просто нажать кнопку", callback_data="broadcast_action:click")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:root")],
        ]
    )


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="✅ Начать рассылку", callback_data="broadcast:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:root")],
        ]
    )


def broadcast_claim_keyboard(broadcast_id: int, action: BroadcastAction) -> InlineKeyboardMarkup:
    text = "🎁 Получить бесплатные запросы" if action == "click" else "✅ Проверить и получить"
    return _markup(
        [[InlineKeyboardButton(text=text, callback_data=f"campaign:claim:{broadcast_id}")]]
    )


def moderation_keyboard(case_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="⛔ Заблокировать навсегда", callback_data=f"mod:block:{case_id}")],
            [InlineKeyboardButton(text="⚠️ Выдать предупреждение", callback_data=f"mod:warn:{case_id}")],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return _markup([[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]])


def referral_menu_keyboard(referral_link: str) -> InlineKeyboardMarkup:
    share_text = "Присоединяйся к AniScan — поиск аниме по кадру"
    share_url = (
        "https://t.me/share/url?url="
        f"{quote(referral_link, safe='')}"
        f"&text={quote(share_text, safe='')}"
    )
    return _markup(
        [
            [InlineKeyboardButton(text="📨 Пригласить друга", url=share_url)],
            [InlineKeyboardButton(text="👥 Список рефералов", callback_data="ref:list")],
            [InlineKeyboardButton(text="⭐ Потратить реферальный баланс", callback_data="ref:shop")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )


def referral_shop_root_keyboard() -> InlineKeyboardMarkup:
    return _markup(
        [
            [InlineKeyboardButton(text="🔎 Выбрать пакет", callback_data="refshop:basic")],
            [InlineKeyboardButton(text="⬅️ К рефералам", callback_data="ref:root")],
        ]
    )


def referral_shop_mode_keyboard(
    mode: SearchMode = "basic",
    penalty_prices: bool = False,
    discount_percent: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key in ("b1", "b10", "b100"):
        package = PACKAGES[key]
        list_price = package_list_price(package, penalty_prices)
        package_price_stars = package_base_price(package, penalty_prices)
        stars = package_price(package, penalty_prices, discount_percent)
        package_discount = package_discount_percent(package, penalty_prices)

        price_steps = [list_price]
        if package_price_stars != price_steps[-1]:
            price_steps.append(package_price_stars)
        if stars != price_steps[-1]:
            price_steps.append(stars)

        if len(price_steps) == 1:
            price_text = str(price_steps[0])
        else:
            price_text = " → ".join(str(value) for value in price_steps)
        label = f"{package.credits} запросов — {price_text} реф. ⭐"

        discounts: list[str] = []
        if package_discount > 0:
            discounts.append(f"скидка {package_discount}%")
        if discount_percent > 0 and not penalty_prices:
            discounts.append(f"доп. {discount_percent}%")
        if discounts:
            label += " · " + " + ".join(discounts)

        rows.append([InlineKeyboardButton(text=label, callback_data=f"refbuy:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ К рефералам", callback_data="ref:root")])
    return _markup(rows)
