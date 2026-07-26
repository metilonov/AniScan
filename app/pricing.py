from __future__ import annotations

import math
from dataclasses import dataclass

from app.models import SearchMode


MAX_EXTRA_DISCOUNT = 25
NEW_USER_DISCOUNT = 20
REFERRAL_COMMISSION_PERCENT = 15


@dataclass(frozen=True, slots=True)
class Package:
    key: str
    mode: SearchMode
    credits: int
    normal_stars: int
    penalty_stars: int
    title: str


@dataclass(frozen=True, slots=True)
class Offer:
    package: Package
    penalty_prices: bool
    promo_percent: int
    first_purchase_percent: int
    milestone_threshold: int
    milestone_percent: int
    total_extra_percent: int
    base_stars: int
    final_stars: int


PACKAGES: dict[str, Package] = {
    "b1": Package("b1", "basic", 1, 10, 10, "1 запрос AniScan"),
    "b10": Package("b10", "basic", 10, 90, 100, "10 запросов AniScan"),
    "b100": Package("b100", "basic", 100, 800, 1000, "100 запросов AniScan"),
    # Старые ключи оставлены, чтобы уже созданные счета предыдущей версии могли оплатиться.
    # Они также начисляют единые запросы AniScan.
    "e1": Package("e1", "basic", 1, 15, 15, "1 запрос AniScan (старый счёт)"),
    "e10": Package("e10", "basic", 10, 135, 150, "10 запросов AniScan (старый счёт)"),
    "e100": Package("e100", "basic", 100, 1200, 1500, "100 запросов AniScan (старый счёт)"),
}


def normalize_discount(percent: int) -> int:
    return max(0, min(MAX_EXTRA_DISCOUNT, int(percent)))


def combine_discounts(*percents: int) -> int:
    return normalize_discount(sum(max(0, int(value)) for value in percents))


def package_base_price(package: Package, penalty_prices: bool) -> int:
    """Цена пакета до промокодов и других дополнительных скидок."""
    return package.penalty_stars if penalty_prices else package.normal_stars


def package_list_price(package: Package, penalty_prices: bool = False) -> int:
    """Исходная цена без пакетной скидки: количество запросов × 10 ⭐."""
    if penalty_prices or package.key not in {"b1", "b10", "b100"}:
        return package_base_price(package, penalty_prices)

    single_price = PACKAGES["b1"].normal_stars
    return package.credits * single_price


def package_discount_percent(package: Package, penalty_prices: bool = False) -> int:
    """Пакетная скидка между исходной и уже сниженной ценой пакета."""
    full_price = package_list_price(package, penalty_prices)
    package_price_stars = package_base_price(package, penalty_prices)
    if full_price <= 0 or package_price_stars >= full_price:
        return 0

    return round((full_price - package_price_stars) * 100 / full_price)


def package_price(package: Package, penalty_prices: bool, discount_percent: int = 0) -> int:
    base = package_base_price(package, penalty_prices)
    percent = 0 if penalty_prices else normalize_discount(discount_percent)
    if percent <= 0:
        return base
    return max(1, math.ceil(base * (100 - percent) / 100))


def make_payload(offer: Offer) -> str:
    tier = "f" if offer.penalty_prices else "d"
    return (
        f"aniscan:{offer.package.key}:{tier}:d{offer.total_extra_percent}:"
        f"m{offer.milestone_threshold}:n{int(offer.first_purchase_percent > 0)}:v3"
    )


def parse_payload(payload: str) -> tuple[Package, bool, int, int, bool] | None:
    parts = payload.split(":")

    if len(parts) == 4 and parts[0] == "aniscan" and parts[3] == "v1":
        package = PACKAGES.get(parts[1])
        if not package or parts[2] not in {"discount", "full"}:
            return None
        return package, parts[2] == "full", 0, 0, False

    if len(parts) == 5 and parts[0] == "aniscan" and parts[4] == "v2":
        package = PACKAGES.get(parts[1])
        if not package or parts[2] not in {"discount", "full"}:
            return None
        discount_raw = parts[3]
        if not discount_raw.startswith("d") or not discount_raw[1:].isdigit():
            return None
        return package, parts[2] == "full", normalize_discount(int(discount_raw[1:])), 0, False

    if len(parts) != 7 or parts[0] != "aniscan" or parts[6] != "v3":
        return None
    package = PACKAGES.get(parts[1])
    if not package or parts[2] not in {"d", "f"}:
        return None
    if not parts[3].startswith("d") or not parts[3][1:].isdigit():
        return None
    if not parts[4].startswith("m") or not parts[4][1:].isdigit():
        return None
    if parts[5] not in {"n0", "n1"}:
        return None
    return (
        package,
        parts[2] == "f",
        normalize_discount(int(parts[3][1:])),
        int(parts[4][1:]),
        parts[5] == "n1",
    )
