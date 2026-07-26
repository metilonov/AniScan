from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


# В базе старый режим extended сохраняется только для совместимости с предыдущими версиями.
# Новая версия AniScan всегда использует basic.
SearchMode = Literal["basic", "extended"]
ContentClass = Literal["anime", "not_anime"]
PromoKind = Literal["credits", "discount"]
BroadcastAction = Literal["channel", "search", "payment", "promo", "click"]


class AnimeAnalysis(BaseModel):
    content_class: ContentClass
    title: str = "Не удалось определить"
    original_title: str = "Не удалось определить"
    character: str = "Не удалось определить"
    confidence: int = Field(default=0, ge=0, le=100)
    scene_description: str = ""
    note: str = ""

    # Поля сохранены для совместимости со старым кодом/базой и не выводятся пользователю.
    episode: str = "Не удалось определить"
    approximate_timestamp: str = "Не удалось определить"
    alternative_titles: list[str] = Field(default_factory=list)
    year: str = "Не удалось определить"
    season: str = "Не удалось определить"
    studio: str = "Не удалось определить"
    genres: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class ContentClassification(BaseModel):
    content_class: ContentClass
    confidence: int = Field(default=0, ge=0, le=100)
    reason: str = ""


class VerificationResult(BaseModel):
    content_class: ContentClass = "not_anime"
    verified: bool = False
    anime_match: bool = False
    character_match: bool = False
    scene_match: bool = False
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    reason: str = ""
    independent_title: str = "Не удалось определить"
    independent_original_title: str = "Не удалось определить"
    independent_character: str = "Не удалось определить"


@dataclass(slots=True)
class QwenKeyRecord:
    id: int
    api_key: str
    label: str
    base_url: str
    status: str
    last_error: str | None
    created_at: str
    updated_at: str
    exhausted_at: str | None
    last_used_at: str | None
    cooldown_until: str | None
    usage_count: int

    @property
    def masked(self) -> str:
        value = self.api_key.strip()
        if len(value) <= 10:
            return value[:2] + "••••"
        return f"{value[:5]}••••{value[-4:]}"


@dataclass(slots=True)
class UserRecord:
    user_id: int
    username: str | None
    first_name: str | None
    basic_credits: int
    extended_credits: int
    trial_granted: bool
    penalty_prices: bool
    unsubscribe_warned: bool
    warnings: int
    blocked: bool
    restricted: bool
    preferred_mode: SearchMode
    active_discount_percent: int = 0
    active_discount_until: str | None = None
    referral_balance_millistars: int = 0

    @property
    def referral_balance_stars(self) -> float:
        return self.referral_balance_millistars / 1000

    @property
    def referral_spendable_stars(self) -> int:
        return self.referral_balance_millistars // 1000


@dataclass(slots=True)
class ConsumeResult:
    ok: bool
    reason: str
    remaining: int


@dataclass(slots=True)
class WarningResult:
    warnings: int
    blocked: bool
    newly_applied: bool = True


@dataclass(slots=True)
class PromoActivationResult:
    ok: bool
    message: str
    kind: PromoKind | None = None
    mode: SearchMode | None = None
    credits: int = 0
    discount_percent: int = 0
    discount_until: str | None = None


@dataclass(slots=True)
class BroadcastClaimResult:
    ok: bool
    reason: str
    mode: SearchMode | None = None
    credits: int = 0


@dataclass(slots=True)
class MilestoneReward:
    threshold: int
    discount_percent: int
    created_at: str


@dataclass(slots=True)
class PaymentRecordResult:
    added: bool
    referrer_id: int | None = None
    referral_reward_millistars: int = 0


@dataclass(slots=True)
class ReferralPurchaseResult:
    ok: bool
    reason: str
    remaining_millistars: int = 0
