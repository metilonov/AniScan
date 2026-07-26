from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
from io import BytesIO
from typing import Any

from aiogram import Bot
from openai import AsyncOpenAI
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Config
from app.db import Database
from app.models import AnimeAnalysis, QwenKeyRecord, VerificationResult


logger = logging.getLogger(__name__)

IDENTIFY_SYSTEM_PROMPT = (
    "Ты точный эксперт по распознаванию аниме по изображениям. "
    "Не выдумывай название или персонажа. Отвечай только коротким JSON."
)

IDENTIFY_PROMPT = """
Независимо определи аниме и главного видимого персонажа на изображении.

Верни ТОЛЬКО JSON:
{
  "content_class": "anime",
  "title": "название аниме на русском или английском",
  "character": "имя персонажа или Не удалось определить",
  "confidence": 94,
  "scene_description": "одно короткое предложение, максимум 12 слов"
}

Правила:
- content_class: anime или not_anime;
- confidence: целое число 0–100;
- не указывай серию, сезон, таймкод, жанры и ссылки;
- описание — максимум 12 слов;
- никаких Markdown и пояснений вне JSON.
""".strip()

NOT_ANIME_RECHECK_PROMPT = """
Первый анализ решил, что изображение может быть не аниме.
Проверь ещё раз максимально внимательно и независимо.
Если это кадр, арт или скриншот из аниме, определи название и персонажа.

Верни ТОЛЬКО JSON:
{
  "content_class": "anime",
  "title": "название аниме",
  "character": "имя персонажа или Не удалось определить",
  "confidence": 80,
  "scene_description": "одно короткое предложение, максимум 12 слов"
}

content_class: anime или not_anime.
""".strip()

RETRY_PROMPT_TEMPLATE = """
Предыдущая гипотеза не прошла независимую проверку Qwen.

Отклонённый вариант:
Название: {title}
Персонаж: {character}
Причина: {reason}

Не повторяй полностью тот же вариант. Проанализируй исходное изображение заново.
Предложи другой наиболее вероятный результат либо исправь персонажа, если название верное.

Верни ТОЛЬКО JSON:
{{
  "content_class": "anime",
  "title": "наиболее вероятное название аниме",
  "character": "имя персонажа или Не удалось определить",
  "confidence": 85,
  "scene_description": "одно короткое предложение, максимум 12 слов"
}}
""".strip()

VERIFY_SYSTEM_PROMPT = (
    "Ты независимый строгий проверяющий распознавания аниме. "
    "Сначала сам определи изображение, не доверяя предложенной гипотезе, "
    "и только затем сравни результаты. Верни только JSON."
)

VERIFY_PROMPT_TEMPLATE = """
Независимо проанализируй изображение, затем проверь предложенную гипотезу.
Не считай гипотезу правильной только потому, что она указана в тексте.

Проверяемая гипотеза:
Название аниме: {title}
Персонаж: {character}

Сравни видимый стиль, дизайн персонажа, лицо, волосы, одежду, окружение и сцену.

Верни ТОЛЬКО JSON:
{{
  "verified": true,
  "anime_match": true,
  "character_match": true,
  "scene_match": true,
  "score": 92,
  "reason": "краткая причина, максимум 12 слов",
  "independent_title": "твоё независимое название или Не удалось определить",
  "independent_character": "твой независимый персонаж или Не удалось определить"
}}

Правила:
- score: число от 0 до 100;
- verified=true только при достаточных видимых основаниях;
- если аниме совпало, но персонаж не подтверждён, character_match=false;
- никаких Markdown и текста вне JSON.
""".strip()

MODERATION_PROMPT = """
Проверь изображение на явно сексуальный, порнографический контент или сексуализацию несовершеннолетних.
Верни ТОЛЬКО JSON:
{
  "sexual": false,
  "score": 0.02
}
score — число от 0 до 1.
""".strip()


class InvalidImageError(ValueError):
    pass


class UnverifiedAnimeError(RuntimeError):
    pass


class QwenKeysUnavailableError(RuntimeError):
    """Нет ни одного ключа, которым можно продолжить работу."""


class QwenTemporaryUnavailableError(RuntimeError):
    """Все активные ключи временно ограничены по частоте/TPM."""


class AnimeAI:
    _HARD_QUOTA_MARKERS = (
        "allocationquota.freetieronly",
        "free allocated quota exceeded",
        "free quota exhausted",
        "free quota has been exhausted",
        "free tier only",
        "hour allocated quota exceeded",
        "week allocated quota exceeded",
        "month allocated quota exceeded",
        "plan quota depleted",
        "quota has been exhausted",
        "quota exhausted",
        "commoditynotpurchased",
        "commodity not purchased",
        "prepaidbilloverdue",
        "postpaidbilloverdue",
        "bill overdue",
        "account is in arrears",
        "arrearage",
        "insufficient balance",
    )
    _AUTH_MARKERS = (
        "invalid api key",
        "invalid_api_key",
        "invalid access token",
        "access token is invalid",
        "token expired",
        "authentication",
        "unauthorized",
    )
    _RATE_LIMIT_MARKERS = (
        "rate limit",
        "rate_limit",
        "throttling",
        "too many requests",
        "limit_requests",
        "allocated quota exceeded",
        "insufficient_quota",
        "you exceeded your current quota",
        "requests rate limit exceeded",
        "concurrency allocated quota exceeded",
        "usage allocated quota exceeded",
    )

    def __init__(self, config: Config, db: Database, bot: Bot) -> None:
        self.config = config
        self.db = db
        self.bot = bot
        self._clients: dict[int, tuple[str, str, AsyncOpenAI]] = {}
        self._all_unavailable_notified = False
        self._notification_lock = asyncio.Lock()

    async def close(self) -> None:
        clients = [entry[2] for entry in self._clients.values()]
        self._clients.clear()
        if clients:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)

    async def forget_key(self, key_id: int) -> None:
        entry = self._clients.pop(key_id, None)
        if entry is not None:
            await entry[2].close()

    def reset_quota_notification(self) -> None:
        self._all_unavailable_notified = False

    def prepare_image(self, raw: bytes, *, max_side: int | None = None) -> tuple[bytes, str]:
        side = max_side or self.config.image_max_side
        try:
            with Image.open(BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source)
                image.load()
                if image.mode == "RGBA":
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                image.thumbnail((side, side), Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(
                    output,
                    format="JPEG",
                    quality=self.config.image_jpeg_quality,
                    optimize=True,
                )
                return output.getvalue(), "image/jpeg"
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidImageError("Файл не является поддерживаемым изображением") from exc

    @staticmethod
    def _data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end <= start:
                raise RuntimeError("Qwen вернул ответ в неизвестном формате")
            value = json.loads(cleaned[start : end + 1])
        if not isinstance(value, dict):
            raise RuntimeError("Qwen вернул JSON не в виде объекта")
        return value

    def _client_for(self, record: QwenKeyRecord) -> AsyncOpenAI:
        cached = self._clients.get(record.id)
        if cached is not None and cached[0] == record.api_key and cached[1] == record.base_url:
            return cached[2]
        client = AsyncOpenAI(
            api_key=record.api_key,
            base_url=record.base_url,
            timeout=120.0,
            max_retries=0,
        )
        self._clients[record.id] = (record.api_key, record.base_url, client)
        return client

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        raw = getattr(exc, "status_code", None)
        if isinstance(raw, int):
            return raw
        response = getattr(exc, "response", None)
        raw = getattr(response, "status_code", None)
        return raw if isinstance(raw, int) else None

    @staticmethod
    def _exception_text(exc: Exception) -> str:
        parts = [str(exc)]
        body = getattr(exc, "body", None)
        if body:
            try:
                parts.append(json.dumps(body, ensure_ascii=False, default=str))
            except Exception:
                parts.append(str(body))
        return " | ".join(part for part in parts if part).strip()

    @staticmethod
    def _safe_error(error: str, api_key: str) -> str:
        cleaned = error.replace(api_key, "[API_KEY]") if api_key else error
        cleaned = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [API_KEY]", cleaned, flags=re.I)
        return cleaned[:1000]

    @classmethod
    def _is_hard_quota_error(cls, exc: Exception, text: str) -> bool:
        lowered = text.lower()
        status = cls._status_code(exc)
        if any(marker in lowered for marker in cls._HARD_QUOTA_MARKERS):
            return True
        # 403 insufficient_quota обычно означает, что продолжить без изменения тарифа нельзя.
        return status == 403 and "insufficient_quota" in lowered

    @classmethod
    def _is_auth_error(cls, exc: Exception, text: str) -> bool:
        lowered = text.lower()
        status = cls._status_code(exc)
        if status == 401:
            return True
        return any(marker in lowered for marker in cls._AUTH_MARKERS)

    @classmethod
    def _is_temporary_limit(cls, exc: Exception, text: str) -> bool:
        lowered = text.lower()
        status = cls._status_code(exc)
        return status == 429 or any(marker in lowered for marker in cls._RATE_LIMIT_MARKERS)

    @classmethod
    def _is_temporary_service_error(cls, exc: Exception) -> bool:
        status = cls._status_code(exc)
        class_name = type(exc).__name__.lower()
        return bool(
            (status is not None and status >= 500)
            or "connection" in class_name
            or "timeout" in class_name
        )

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.config.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception:
                logger.exception("Не удалось уведомить администратора %s о ключах Qwen", admin_id)

    async def _notify_key_exhausted(self, record: QwenKeyRecord, error: str) -> None:
        await self._notify_admins(
            "⚠️ <b>Квота ключа Qwen исчерпана</b>\n\n"
            f"Ключ: <b>#{record.id}</b> — {html.escape(record.label)}\n"
            f"Маска: <code>{html.escape(record.masked)}</code>\n"
            f"Причина: <code>{html.escape(error[:350])}</code>\n\n"
            "Ключ отключён автоматически. Бот переключается на следующий активный ключ."
        )

    async def _notify_key_invalid(self, record: QwenKeyRecord, error: str) -> None:
        await self._notify_admins(
            "🔴 <b>Ключ Qwen недействителен</b>\n\n"
            f"Ключ: <b>#{record.id}</b> — {html.escape(record.label)}\n"
            f"Маска: <code>{html.escape(record.masked)}</code>\n"
            f"Причина: <code>{html.escape(error[:350])}</code>\n\n"
            "Ключ отключён. Добавьте или активируйте другой ключ в админ-панели."
        )

    async def _notify_all_unavailable(self) -> None:
        async with self._notification_lock:
            if self._all_unavailable_notified:
                return
            self._all_unavailable_notified = True
        await self._notify_admins(
            "🚨 <b>Все ключи Qwen недоступны</b>\n\n"
            "Распознавание аниме временно остановлено. Откройте:\n"
            "<b>Админ-панель → 🔑 Ключи Qwen → ➕ Добавить ключ</b>."
        )

    async def _create_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> Any:
        keys = await self.db.available_qwen_keys()
        if not keys:
            counts = await self.db.qwen_key_counts()
            if counts["active"] > 0 and counts["cooling"] > 0:
                raise QwenTemporaryUnavailableError("Все ключи Qwen временно ограничены")
            await self._notify_all_unavailable()
            raise QwenKeysUnavailableError("Нет активных ключей Qwen")

        saw_temporary_error = False
        last_error: Exception | None = None

        for record in keys:
            client = self._client_for(record)
            try:
                response = await client.chat.completions.create(
                    model=self.config.qwen_model,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    extra_body={"enable_thinking": False},
                    extra_headers={"X-DashScope-Wait-Timeout": "30"},
                )
            except Exception as exc:
                last_error = exc
                raw_error = self._exception_text(exc)
                safe_error = self._safe_error(raw_error, record.api_key)

                if self._is_hard_quota_error(exc, raw_error):
                    newly_exhausted = await self.db.mark_qwen_key_exhausted(record.id, safe_error)
                    if newly_exhausted:
                        await self._notify_key_exhausted(record, safe_error)
                    continue

                if self._is_auth_error(exc, raw_error) or self._status_code(exc) == 403:
                    newly_invalid = await self.db.mark_qwen_key_invalid(record.id, safe_error)
                    if newly_invalid:
                        await self._notify_key_invalid(record, safe_error)
                    continue

                if self._is_temporary_limit(exc, raw_error):
                    saw_temporary_error = True
                    await self.db.cooldown_qwen_key(record.id, 60, safe_error)
                    continue

                if self._is_temporary_service_error(exc):
                    saw_temporary_error = True
                    await self.db.cooldown_qwen_key(record.id, 30, safe_error)
                    continue

                raise

            await self.db.mark_qwen_key_used(record.id)
            self._all_unavailable_notified = False
            return response

        counts = await self.db.qwen_key_counts()
        if counts["active"] > 0 and (counts["cooling"] > 0 or saw_temporary_error):
            raise QwenTemporaryUnavailableError("Qwen временно ограничил все активные ключи") from last_error

        await self._notify_all_unavailable()
        raise QwenKeysUnavailableError("Все ключи Qwen исчерпаны или недействительны") from last_error

    async def _request_json(
        self,
        *,
        system_prompt: str,
        prompt: str,
        images: list[bytes],
        max_tokens: int,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for image in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._data_url(image)},
                }
            )
        content.append({"type": "text", "text": prompt})

        response = await self._create_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            max_tokens=max_tokens,
        )
        if not response.choices:
            raise RuntimeError("Qwen вернул ответ без вариантов")
        text = response.choices[0].message.content
        if not text:
            raise RuntimeError("Qwen вернул пустой ответ")
        return self._extract_json(text)

    async def is_sexual(self, image_bytes: bytes, mime: str) -> tuple[bool, float]:
        del mime
        data = await self._request_json(
            system_prompt="Ты модуль безопасной классификации изображений.",
            prompt=MODERATION_PROMPT,
            images=[image_bytes],
            max_tokens=self.config.moderation_max_tokens,
        )
        sexual = bool(data.get("sexual", False))
        try:
            score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        return sexual or score >= self.config.sexual_score_threshold, score

    async def _identify(self, image_bytes: bytes, prompt: str) -> AnimeAnalysis:
        data = await self._request_json(
            system_prompt=IDENTIFY_SYSTEM_PROMPT,
            prompt=prompt,
            images=[image_bytes],
            max_tokens=self.config.identify_max_tokens,
        )
        return AnimeAnalysis.model_validate(data)

    async def _verify(self, image_bytes: bytes, candidate: AnimeAnalysis) -> VerificationResult:
        prompt = VERIFY_PROMPT_TEMPLATE.format(
            title=candidate.title,
            character=candidate.character,
        )
        data = await self._request_json(
            system_prompt=VERIFY_SYSTEM_PROMPT,
            prompt=prompt,
            images=[image_bytes],
            max_tokens=self.config.verify_max_tokens,
        )
        return VerificationResult.model_validate(data)

    def _accepted(self, candidate: AnimeAnalysis, verification: VerificationResult) -> bool:
        character_known = self._known_value(candidate.character)
        return (
            verification.verified
            and verification.anime_match
            and verification.score >= self.config.verify_min_score
            and (
                verification.character_match
                or verification.scene_match
                or not character_known
            )
        )

    @staticmethod
    def _known_value(value: str) -> bool:
        return value.strip().lower() not in {
            "",
            "не удалось определить",
            "не определён",
            "не определен",
            "неизвестно",
            "unknown",
        }

    @classmethod
    def _title_known(cls, title: str) -> bool:
        return cls._known_value(title)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-zа-я0-9]+", "", value.lower())

    @classmethod
    def _same_title(cls, first: str, second: str) -> bool:
        first_normalized = cls._normalize(first)
        second_normalized = cls._normalize(second)
        return bool(first_normalized) and first_normalized == second_normalized

    @classmethod
    def _same_candidate(cls, first: AnimeAnalysis, second: AnimeAnalysis) -> bool:
        return cls._same_title(first.title, second.title) and (
            cls._normalize(first.character) == cls._normalize(second.character)
        )

    @classmethod
    def _apply_verification(
        cls,
        candidate: AnimeAnalysis,
        verification: VerificationResult,
    ) -> AnimeAnalysis:
        candidate.confidence = int(
            round(min(float(candidate.confidence), float(verification.score)))
        )

        if not verification.character_match:
            if cls._known_value(verification.independent_character):
                candidate.character = verification.independent_character
            else:
                candidate.character = "Не удалось определить"

        candidate.note = verification.reason
        return candidate

    async def analyze_verified(self, image_bytes: bytes, mime: str) -> AnimeAnalysis:
        del mime

        # 1. Первичное независимое распознавание.
        first = await self._identify(image_bytes, IDENTIFY_PROMPT)
        if first.content_class == "not_anime":
            # 2. Отдельная повторная проверка решения not_anime.
            rechecked = await self._identify(image_bytes, NOT_ANIME_RECHECK_PROMPT)
            if rechecked.content_class == "not_anime":
                return first
            first = rechecked

        if not self._title_known(first.title):
            raise UnverifiedAnimeError("Первый анализ не определил название")

        # 3. Свежий запрос Qwen независимо проверяет первую гипотезу.
        first_check = await self._verify(image_bytes, first)
        if self._accepted(first, first_check):
            return self._apply_verification(first, first_check)

        # 4. Qwen получает отклонённый вариант и обязан сделать новый анализ.
        retry_prompt = RETRY_PROMPT_TEMPLATE.format(
            title=first.title,
            character=first.character,
            reason=first_check.reason or "результат не подтверждён",
        )
        second = await self._identify(image_bytes, retry_prompt)
        if second.content_class == "not_anime":
            raise UnverifiedAnimeError("Повторный анализ не подтвердил аниме")
        if not self._title_known(second.title):
            raise UnverifiedAnimeError("Повторный анализ не определил название")
        if self._same_candidate(first, second):
            raise UnverifiedAnimeError("Qwen полностью повторил отклонённый вариант")

        # 5. Ещё один свежий запрос независимо проверяет вторую гипотезу.
        second_check = await self._verify(image_bytes, second)
        if self._accepted(second, second_check):
            return self._apply_verification(second, second_check)

        raise UnverifiedAnimeError("Две независимые проверки Qwen не подтвердили результат")
