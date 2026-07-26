from __future__ import annotations

from app.db import Database
from app.models import UserRecord
from app.pricing import (
    NEW_USER_DISCOUNT,
    Offer,
    Package,
    combine_discounts,
    normalize_discount,
    package_base_price,
    package_price,
)


async def build_offer(db: Database, user: UserRecord, package: Package) -> Offer:
    if user.penalty_prices:
        base = package_base_price(package, True)
        return Offer(
            package=package,
            penalty_prices=True,
            promo_percent=0,
            first_purchase_percent=0,
            milestone_threshold=0,
            milestone_percent=0,
            total_extra_percent=0,
            base_stars=base,
            final_stars=base,
        )

    has_purchase = await db.has_purchase(user.user_id)
    milestone = await db.get_available_milestone_reward(user.user_id)
    promo = normalize_discount(user.active_discount_percent)
    first = 0 if has_purchase else NEW_USER_DISCOUNT
    milestone_threshold = milestone.threshold if milestone else 0
    milestone_percent = milestone.discount_percent if milestone else 0
    total = combine_discounts(promo, first, milestone_percent)
    base = package_base_price(package, False)
    return Offer(
        package=package,
        penalty_prices=False,
        promo_percent=promo,
        first_purchase_percent=first,
        milestone_threshold=milestone_threshold,
        milestone_percent=milestone_percent,
        total_extra_percent=total,
        base_stars=base,
        final_stars=package_price(package, False, total),
    )


def offer_lines(offer: Offer) -> list[str]:
    lines: list[str] = []
    if offer.penalty_prices:
        return ["⚠️ Для аккаунта действуют цены без скидок из-за отписки."]
    if offer.first_purchase_percent:
        lines.append(f"🆕 Скидка нового пользователя: <b>{offer.first_purchase_percent}%</b>.")
    if offer.promo_percent:
        lines.append(f"🎟 Скидка по промокоду: <b>{offer.promo_percent}%</b>.")
    if offer.milestone_percent:
        lines.append(
            f"🎉 Разовая акция за {offer.milestone_threshold} пользователей: "
            f"<b>{offer.milestone_percent}%</b>."
        )
    if offer.total_extra_percent:
        lines.append(
            f"🏷 Итоговая дополнительная скидка: <b>{offer.total_extra_percent}%</b> "
            "(максимум 25%)."
        )
    return lines
