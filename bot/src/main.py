import os
import asyncio
import logging
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO)

BOT_RATE_WINDOW_SEC = float(os.getenv("BOT_RATE_WINDOW_SEC", "0.5"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
REDIRECT_BASE = os.getenv("REDIRECT_BASE", "http://localhost:8000").rstrip("/")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Офферы (одинаковые для RU/KZ)
OFFERS = [
    ("boostra", "BOOSTRA"),
    ("privet-sosed", "Привет, сосед!"),
    ("one-click-money", "One Click Money"),
    ("vivus", "Vivus"),
    ("podbor-0", "Подбор займа без процентов"),
]

COUNTRIES = [
    ("RU", "🇷🇺 Россия"),
    ("KZ", "🇰🇿 Казахстан"),
]

# Ключи в Redis
R_USER_COUNTRY = "u:{uid}:country"
R_USER_THROTTLE = "u:{uid}:thr"

async def anti_flood(redis: aioredis.Redis, user_id: int) -> bool:
    """Возвращает True, если можно пускать запрос; False — если слишком часто."""
    key = R_USER_THROTTLE.format(uid=user_id)
    allowed = await redis.set(key, "1", ex=max(int(BOT_RATE_WINDOW_SEC * 2), 1), nx=True)
    return bool(allowed)

async def start_cmd(message: Message, redis: aioredis.Redis):
    if not await anti_flood(redis, message.from_user.id):
        return

    kb = [
        [InlineKeyboardButton(text=title, callback_data=f"country:{code}")]
        for code, title in COUNTRIES
    ]
    await message.answer(
        "👋 Привет! Я помогу найти выгодные предложения по займам.\n\n"
        "📍 Выберите вашу страну:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )

async def on_country(cb: CallbackQuery, redis: aioredis.Redis):
    if not await anti_flood(redis, cb.from_user.id):
        await cb.answer()
        return

    code = cb.data.split(":", 1)[1]
    await redis.set(R_USER_COUNTRY.format(uid=cb.from_user.id), code, ex=60*60*24*30)

    # Показ офферов (одинаковый список)
    kb = []
    for slug, title in OFFERS:
        params = urlencode({
            "u": cb.from_user.id,
            "c": code,
        })
        url = f"{REDIRECT_BASE}/r/{slug}?{params}"
        kb.append([InlineKeyboardButton(text=title, url=url)])

    await cb.message.edit_text(
        f"Страна: {dict(COUNTRIES)[code]}\n\nВыберите оффер:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer("Готово")

async def stats_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    # MVP: пока без БД-отчёта — просто заглушка
    await message.answer("Статистика будет добавлена после подключения БД/API веб-сервиса.")

async def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    bot = Bot(token=token)
    dp = Dispatcher()

    # Хэндлеры
    dp.message.register(lambda m: start_cmd(m, redis), CommandStart())
    dp.message.register(lambda m: stats_cmd(m), F.text == "/stats")
    dp.callback_query.register(lambda c: on_country(c, redis), F.data.startswith("country:"))

    logging.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())