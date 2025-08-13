import os
import time
import asyncio
import hashlib
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, List

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BufferedInputFile
)
import httpx

# --- env ---
load_dotenv()  # текущая рабочая директория
bot_dir_env = Path(__file__).resolve().parent / ".env"
if bot_dir_env.exists():
    load_dotenv(dotenv_path=bot_dir_env)

# --- settings ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")

REDIRECT_BASE = os.getenv("REDIRECT_BASE", "http://127.0.0.1:8000").rstrip("/")
STATS_BASE = os.getenv("STATS_BASE", REDIRECT_BASE).rstrip("/")
USER_HASH_SALT = os.getenv("USER_HASH_SALT", "change_me_salt")

# ====== утилиты ======

def _parse_admin_ids(val: Optional[str]) -> List[int]:
    if not val:
        return []
    ids = []
    for part in val.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids

ADMIN_IDS = set(_parse_admin_ids(os.getenv("ADMIN_IDS", "")))

# антифлуд
BOT_RATE_WINDOW_SEC = float(os.getenv("BOT_RATE_WINDOW_SEC", "0.5"))

# файлы
BASE_DIR = Path(__file__).resolve().parent
BANNER_LOCAL_PATH = BASE_DIR / "banner.png"   # если есть — отправим
BANNER_URL = None                              # можно задать URL баннера

WELCOME_TEXT = (
    "👋 Привет! Я помогу быстро найти выгодные предложения по займам.\n\n"
    "📍 Сначала выбери свою страну, чтобы показать актуальные офферы.\n"
    "⚡ Оформление происходит на сайтах партнёров."
)

COUNTRIES = [
    ("RU", "🇷🇺 Россия"),
    ("KZ", "🇰🇿 Казахстан"),
]

# каталог офферов: slug, title (основные)
OFFERS = [
    ("boostra",         "🚀 BOOSTRA"),
    ("privet-sosed",    "🏠 Привет, сосед!"),
    ("one-click-money", "⚡ One Click Money"),
    ("vivus",           "💚 Vivus"),
    ("podbor-0",        "🎯 Подбор займа без %"),
]

# дополнительные офферы, которые ведут на внешние ссылки (через /r/ → считаем клики)
EXTRA_OFFERS = [
    ("calc-potential", "📈 Рассчитать потенциал"),  # https://clck.ru/3NbeMg
    ("best-terms",     "⭐ Получить лучшие условия"),  # https://clck.ru/3NbeU8
]

# ====== простейший антифлуд и память выбора ======
_last_action_ts: dict[int, float] = {}
_user_country: dict[int, str] = {}

def allowed(user_id: int) -> bool:
    now = time.monotonic()
    prev = _last_action_ts.get(user_id, 0.0)
    if now - prev < BOT_RATE_WINDOW_SEC:
        return False
    _last_action_ts[user_id] = now
    return True

def uid_hash(telegram_user_id: int) -> str:
    # sha256(str(user_id) + ':' + salt)
    raw = f"{telegram_user_id}:{USER_HASH_SALT}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def build_redirect(slug: str, country: str, uid_h: str) -> str:
    return f"{REDIRECT_BASE}/r/{slug}?c={country}&u={uid_h}"

# ====== клавиатуры ======

def kb_countries() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=title, callback_data=f"country:{code}")]
            for code, title in COUNTRIES]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_offers(country: str, user_id: int) -> InlineKeyboardMarkup:
    uid_h = uid_hash(user_id)
    pairs = OFFERS + EXTRA_OFFERS
    slugs = [s for s, _ in pairs]

    async with httpx.AsyncClient(timeout=10.0) as client:
        async def make(slug: str):
            try:
                r = await client.post(
                    f"{STATS_BASE}/s/new",
                    json={"slug": slug, "c": country, "u": uid_h},
                )
                r.raise_for_status()
                path = r.json().get("path")
                return slug, (f"{REDIRECT_BASE}{path}" if path else None)
            except Exception as e:
                print("short link error:", slug, e)
                return slug, None

        short_pairs = await asyncio.gather(*(make(slug) for slug in slugs))

    short = dict(short_pairs)
    rows = []
    for slug, title in pairs:
        url = short.get(slug) or build_redirect(slug, country, uid_h)
        rows.append([InlineKeyboardButton(text=title, url=url)])

    rows.append([InlineKeyboardButton(text="« Назад", callback_data="back:countries")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ====== хэндлеры ======
async def cmd_start(message: Message):
    if not allowed(message.from_user.id):
        return

    markup = kb_countries()
    sent = False

    # 1) баннер из файла
    try:
        if BANNER_LOCAL_PATH.exists():
            photo = FSInputFile(str(BANNER_LOCAL_PATH))
            await message.answer_photo(photo=photo, caption=WELCOME_TEXT, reply_markup=markup)
            sent = True
    except Exception as e:
        print("Ошибка отправки локального баннера:", e)

    # 2) баннер по URL
    if not sent and BANNER_URL:
        try:
            await message.answer_photo(photo=BANNER_URL, caption=WELCOME_TEXT, reply_markup=markup)
            sent = True
        except Exception as e:
            print("Ошибка отправки баннера по URL:", e)

    # 3) только текст
    if not sent:
        await message.answer(WELCOME_TEXT, reply_markup=markup)

async def on_country(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        await cb.answer()
        return

    code = cb.data.split(":", 1)[1]
    _user_country[cb.from_user.id] = code

    markup = await kb_offers(code, cb.from_user.id)
    text = f"Страна: {dict(COUNTRIES)[code]}\n\nВыберите оффер:"

    try:
        if cb.message.photo:
            await cb.message.edit_caption(text, reply_markup=markup)
        else:
            await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)

    await cb.answer("Готово")

async def on_back(cb: CallbackQuery):
    """Возврат к выбору страны."""
    if not allowed(cb.from_user.id):
        await cb.answer()
        return
    markup = kb_countries()
    try:
        if cb.message.photo:
            await cb.message.edit_caption(WELCOME_TEXT, reply_markup=markup)
        else:
            await cb.message.edit_text(WELCOME_TEXT, reply_markup=markup)
    except Exception:
        await cb.message.answer(WELCOME_TEXT, reply_markup=markup)
    await cb.answer("Назад")

# --- админ ---

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS if ADMIN_IDS else False

async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("Нет доступа.")
    rows = [
        [
            InlineKeyboardButton(text="1 день",   callback_data="stats:1"),
            InlineKeyboardButton(text="7 дней",   callback_data="stats:7"),
            InlineKeyboardButton(text="30 дней",  callback_data="stats:30"),
            InlineKeyboardButton(text="Всё время",callback_data="stats:all"),
        ]
    ]
    await message.answer("Выбери период статистики посещения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def on_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    key = cb.data.split(":", 1)[1]  # '1' | '7' | '30' | 'all'
    today = date.today()

    if key == "1":
        frm, to = today, today
        title = "За 1 день"
    elif key == "7":
        frm, to = today - timedelta(days=6), today
        title = "За 7 дней"
    elif key == "30":
        frm, to = today - timedelta(days=29), today
        title = "За 30 дней"
    else:
        frm, to = date(2000, 1, 1), today
        title = "За всё время"

    url = f"{STATS_BASE}/stats/plot?from_date={frm.isoformat()}&to_date={to.isoformat()}&top=10"

    await cb.answer("Собираю статистику…")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            img_bytes = r.content
    except Exception as e:
        return await cb.message.answer(f"Не смог получить график: {e}")

    png = BufferedInputFile(img_bytes, filename="stats.png")
    caption = f"{title}\n{frm.isoformat()} — {to.isoformat()}"
    await cb.message.answer_photo(photo=png, caption=caption)

# ====== запуск ======
async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(cmd_start, CommandStart())
    dp.callback_query.register(on_country, F.data.startswith("country:"))
    dp.callback_query.register(on_back, F.data == "back:countries")

    dp.message.register(cmd_admin, Command("admin"))
    dp.callback_query.register(on_stats, F.data.startswith("stats:"))

    print("Bot started. Команды: /start, /admin (для админов)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
