from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Переменная {name} не указана в .env")
    upper = value.upper()
    if "PASTE_" in upper or "YOUR_" in upper:
        raise RuntimeError(f"Переменная {name} содержит шаблон вместо реального значения")
    return value


def _optional_secret(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    upper = value.upper()
    if "PASTE_" in upper or "YOUR_" in upper:
        return None
    return value


def _parse_admin_ids(raw: str) -> frozenset[int]:
    result: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        result.add(int(part))
    if not result:
        raise RuntimeError("ADMIN_IDS должен содержать хотя бы один Telegram user ID")
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    dashscope_api_key: str | None
    qwen_base_url: str
    qwen_model: str
    admin_ids: frozenset[int]
    channel_id: int
    channel_url: str
    support_username: str
    database_path: Path
    image_max_side: int
    image_jpeg_quality: int
    identify_max_tokens: int
    verify_max_tokens: int
    moderation_max_tokens: int
    verify_min_score: float
    sexual_score_threshold: float

    @classmethod
    def from_env(cls) -> "Config":
        db_raw = os.getenv("DATABASE_PATH", "data/aniscan.sqlite3").strip()
        db_path = Path(db_raw)
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path

        support = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")

        qwen_base_url = _require("QWEN_BASE_URL").rstrip("/")
        if not qwen_base_url.endswith("/v1"):
            raise RuntimeError("QWEN_BASE_URL должен заканчиваться на /compatible-mode/v1")

        return cls(
            bot_token=_require("BOT_TOKEN"),
            # Начальный ключ необязателен: дополнительные ключи хранятся в SQLite
            # и добавляются через админ-панель.
            dashscope_api_key=_optional_secret("DASHSCOPE_API_KEY"),
            qwen_base_url=qwen_base_url,
            qwen_model=os.getenv("QWEN_MODEL", "qwen3.7-plus").strip(),
            admin_ids=_parse_admin_ids(_require("ADMIN_IDS")),
            channel_id=int(_require("CHANNEL_ID")),
            channel_url=_require("CHANNEL_URL"),
            support_username=support,
            database_path=db_path,
            image_max_side=max(512, int(os.getenv("IMAGE_MAX_SIDE", "768"))),
            image_jpeg_quality=max(55, min(95, int(os.getenv("IMAGE_JPEG_QUALITY", "82")))),
            identify_max_tokens=max(100, int(os.getenv("IDENTIFY_MAX_TOKENS", "150"))),
            verify_max_tokens=max(90, int(os.getenv("VERIFY_MAX_TOKENS", "140"))),
            moderation_max_tokens=max(50, int(os.getenv("MODERATION_MAX_TOKENS", "80"))),
            verify_min_score=max(1.0, min(100.0, float(os.getenv("VERIFY_MIN_SCORE", "80")))),
            sexual_score_threshold=max(
                0.0, min(1.0, float(os.getenv("SEXUAL_SCORE_THRESHOLD", "0.65")))
            ),
        )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids
