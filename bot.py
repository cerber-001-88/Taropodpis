#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот для гадания на Таро.
Версия 9.1 – финальные правки по командам и группам:
- Все личные хэндлеры работают только в личке (ChatType.PRIVATE).
- Команда /taro работает только в группах (ChatType.GROUPS).
- Меню команд Telegram настроено через BotCommandScope:
  * default — базовый набор (страховка);
  * в личке — /start, /help;
  * в группах — только /taro.
- Трактовки для /taro берутся из card_meanings.py (не AI).
- Сохранена вся прежняя функциональность (личка, подписка, админка).
"""

import asyncio
import os
import sys
import signal
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List, Dict, Any, Tuple

import asyncpg
from dotenv import load_dotenv

from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, LabeledPrice, PreCheckoutQuery, SuccessfulPayment,
    CallbackQuery, constants,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    ContextTypes, ConversationHandler
)

import openai

load_dotenv()

# ---------- НАСТРОЙКИ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")
CURRENCY = os.getenv("CURRENCY", "RUB")
PRICE_FULL = int(os.getenv("PRICE_FULL", 9900))                 # 99 руб в копейках
PRICE_SUBSCRIPTION_EXTRA = int(os.getenv("PRICE_SUBSCRIPTION_EXTRA", 4900))  # 49 руб
SUBSCRIPTION_PRICE = int(os.getenv("SUBSCRIPTION_PRICE", 19900))  # 199 руб в копейках
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
USE_AI_WELCOME = os.getenv("USE_AI_WELCOME", "True").lower() in ("true", "1", "yes")
CARD_IMAGES_FILE = "card_images.json"
ENG_TO_RUS_FILE = "eng_to_rus.json"

if not TELEGRAM_TOKEN:
    logging.error("TELEGRAM_TOKEN не задан")
    sys.exit(1)
if not DATABASE_URL:
    logging.error("DATABASE_URL не задан")
    sys.exit(1)
if not ADMIN_IDS:
    logging.warning("ADMIN_IDS не задан")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- ЗАГРУЗКА ТРАКТОВОК ДЛЯ /taro ----------
try:
    from card_meanings import CARD_MEANINGS
    logger.info(f"Загружено {len(CARD_MEANINGS)} трактовок карт для /taro")
except ImportError as e:
    logger.warning(f"card_meanings.py не найден ({e}). Команда /taro не будет работать.")
    CARD_MEANINGS = {}

# ---------- ПРОМПТ ПО УМОЛЧАНИЮ ДЛЯ КАРТЫ ДНЯ ----------
DEFAULT_CARD_OF_DAY_PROMPT = """
Ты — профессиональный мудрый таролог. Человек вытягивает карту дня и ты её трактуешь. Сегодня выпала карта {card_name} ({card_eng}). Назови карту и трактуй карту так как принято в классическом таро, не бойся негативных карт, делай профессиональную трактовку, что эта карта говорит о сегодняшнем дне. 

Люди , которые обращаются к тарологу, ещё пока не могут полностью опираться на свои собственные суждения и ищут опору во внешних знаках, поэтому формируй свои ответы с лёгкой ненавязчивой психологической поддержкой.

Ответ должен уместиться в одно сообщение телеграмм, не более 4000 символов.
"""

# ---------- ШАБЛОН ДЛЯ ПОДПИСКИ (ПОЛНЫЙ ОФФЕР) ----------
SUBSCRIPTION_TEMPLATE = """
💎 Подписка «Таро-проводник»

Оформите подписку и получите эксклюзивные преимущества:

1. Бесплатный расклад каждый день
Каждый день вы получаете один полный глубокий расклад абсолютно бесплатно.

2. Сниженная цена на дополнительные расклады
Все последующие расклады в этот же день – всего {sub_price} ₽ (вместо {full_price} ₽).

3. Экономия
Без подписки каждый расклад стоит {full_price} ₽. С подпиской вы экономите на каждом втором раскладе.

4. Гибкость
Вы всегда можете отменить подписку в любой момент в личном кабинете.

---

💰 Стоимость подписки: {price} ₽ в месяц

Попробуйте и убедитесь сами!
"""

# ---------- КОРОТКОЕ СООБЩЕНИЕ ДЛЯ АКТИВНОЙ ПОДПИСКИ ----------
SUBSCRIPTION_ACTIVE_BRIEF_TEMPLATE = """
💎 Ваша подписка «Таро-проводник» активна!

Действительна до: {until}

Вы получаете:
• 1 бесплатный полный расклад каждый день
• Сниженную цену на дополнительные расклады: {sub_price} ₽ вместо {full_price} ₽

Вы можете продлить подписку или отменить её в любой момент.
"""

# ---------- ТЕКСТ ДЛЯ РАЗДЕЛА «О СЕРВИСЕ» (HTML) ----------
ABOUT_SERVICE_TEXT = """
<b>🌟 О сервисе</b>

Я — ваш виртуальный таролог. Я помогаю заглянуть в глубины вашей души, используя древнюю мудрость карт Таро.

<b>Что вы можете сделать:</b>
• 🃏 <b>Карта дня</b> – знак от Вселенной на каждый день.
• 🔮 <b>Полный расклад</b> – глубинный ответ на любой вопрос.
• 💬 <b>Отзывы</b> – что говорят наши пользователи.

<b>Как это работает:</b>
1. Вы задаёте вопрос.
2. Я вытягиваю 3 случайные карты.
3. Даю глубокий разбор с символикой, синтезом и практическим выводом.

<b>Первый расклад – Бесплатный!</b>

📄 <a href="https://telegra.ph/Polzovatelskoe-soglashenie-publichnaya-oferta-08-04-4">Пользовательское соглашение</a>
🔒 <a href="https://telegra.ph/Politika-konfidencialnosti-08-04-96">Политика конфиденциальности</a>
"""

# ---------- ВСТУПИТЕЛЬНЫЙ ТЕКСТ ПО УМОЛЧАНИЮ ДЛЯ РАСКЛАДА ----------
DEFAULT_WELCOME_TEXT = """
🌟 Добро пожаловать! Я готов провести для вас глубокий расклад Таро.

Чтобы ответ был максимально точным и полезным, я прошу вас задать вопрос как можно подробнее. Опишите ситуацию, свои чувства, обстоятельства, сомнения — чем больше деталей вы упомянете, тем глубже я смогу проникнуть в суть вопроса. Не бойтесь писать длинно, я читаю внимательно.

Итак, что вас тревожит или интересует сегодня? Напишите мне всё, что считаете важным.
"""

# ---------- ЗАГРУЗКА ДАННЫХ КАРТ ----------
def load_card_images() -> Dict[str, str]:
    try:
        with open(CARD_IMAGES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"Загружено {len(data)} изображений карт")
        return data
    except Exception as e:
        logger.error(f"Ошибка загрузки {CARD_IMAGES_FILE}: {e}")
        return {}

def load_translations() -> Dict[str, str]:
    try:
        with open(ENG_TO_RUS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"Загружено {len(data)} переводов карт")
        return data
    except Exception as e:
        logger.error(f"Ошибка загрузки {ENG_TO_RUS_FILE}: {e}")
        return {}

# ---------- УНИВЕРСАЛЬНАЯ ОТПРАВКА СООБЩЕНИЙ ----------
async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """
    Безопасно отправляет сообщение, независимо от того, пришёл апдейт
    из текстового сообщения или из callback-запроса.
    """
    try:
        if update.message is not None:
            return await update.message.reply_text(text, **kwargs)
        elif update.callback_query is not None and update.callback_query.message is not None:
            return await context.bot.send_message(
                chat_id=update.callback_query.message.chat_id,
                text=text,
                **kwargs
            )
        else:
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id:
                return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка safe_reply: {e}", exc_info=True)
    return None

async def safe_send_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, photo, caption: str = None, **kwargs):
    """
    Безопасно отправляет фото, независимо от типа апдейта.
    """
    try:
        chat_id = update.effective_chat.id
        return await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка safe_send_photo: {e}", exc_info=True)
    return None

# ---------- РАБОТА С БАЗОЙ ДАННЫХ ----------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        logger.info("Подключение к базе данных установлено")

    async def close(self):
        if self.pool:
            await self.pool.close()
            logger.info("Соединение с базой данных закрыто")

    async def init_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    free_trial_used BOOLEAN DEFAULT FALSE,
                    last_free_reading_date DATE,
                    last_card_of_day_date DATE,
                    subscription_type TEXT DEFAULT NULL,
                    subscription_until TIMESTAMPTZ DEFAULT NULL,
                    is_banned BOOLEAN DEFAULT FALSE,
                    ban_reason TEXT,
                    subscription_offer_shown BOOLEAN DEFAULT FALSE
                )
            """)
            # Миграция: добавляем колонку для существующих баз
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_card_of_day_date DATE")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    amount INTEGER,
                    payment_type TEXT,
                    payload TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    text TEXT,
                    approved BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS promocodes (
                    code TEXT PRIMARY KEY,
                    discount_percent INTEGER,
                    expires_at TIMESTAMPTZ,
                    max_uses INTEGER,
                    used_count INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS examples (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    content TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)

            default_settings = {
                'welcome_text': '🌟 Добро пожаловать в мир Таро! Я — ваш проводник. Здесь карты становятся зеркалом вашей души.\n\nЧто вы можете сделать:\n🃏 Получить Карту дня – знак от Вселенной.\n🔮 Заказать полный расклад – глубинный ответ на любой вопрос.\n\nС чего начнём? Нажмите «Карта дня», чтобы получить ваш первый знак.',
                'subscription_offer_text': '💎 **Подписка «Таро-проводник»**\n\nОформите подписку и получите:\n• **1 бесплатный полный расклад каждый день** – без ограничений!\n• **Все последующие расклады в этот же день** – по сниженной цене **{sub_price} ₽** (вместо **{full_price} ₽**).\n• **Без подписки** – каждый расклад стоит **{full_price} ₽**.\n\nСтоимость подписки: **{price} ₽ в месяц**.\n\nВы всегда можете отменить подписку в любой момент в личном кабинете.\n\nХотите попробовать? Нажмите «Оформить подписку».',
                'help_text': '❓ Как пользоваться ботом:\n\n1. Нажмите «Карта дня» для получения знака на день.\n2. Нажмите «Полный расклад» для глубокого ответа.\n3. Первый расклад – ознакомительный.\n4. Подписка даёт один бесплатный расклад в день и сниженную цену на дополнительные.\n5. Без подписки – платный расклад 99 ₽.\n\nЕсли у вас возникли вопросы, напишите нам: https://t.me/+SkGzaw9nQrg3ZDAy.',
                'price_full': str(PRICE_FULL),
                'price_subscription': str(PRICE_SUBSCRIPTION_EXTRA),
                'subscription_price': str(SUBSCRIPTION_PRICE),
                'full_reading_intro_text': '🔮 **Полный расклад Таро**\n\nЯ вытяну для вас три карты и сделаю глубокий разбор:\n• **Каждая карта** – её символы, значение, позитивные и теневые аспекты.\n• **Взаимодействие карт** – как они перекликаются и усиливают друг друга.\n• **Синтез** – целостный ответ на ваш вопрос.\n• **Квинтэссенция** – итоговый аркан-совет.\n• **Практический вывод** – как применить это в жизни.\n\nРасклад даст вам ясность и опору для принятия решений. Готовы? Нажмите «Начать расклад».',
                'card_of_day_intro_text': '✨ **Карта дня** – ваш знак от Вселенной.\n\nОдна карта, её энергия, аффирмация и простой шаг на сегодня. Это поможет вам настроиться на день, почувствовать его ритм и заметить важное.\n\nХотите узнать, что приготовил вам день? Нажмите «Достать карту дня».',
                'card_of_day_prompt': DEFAULT_CARD_OF_DAY_PROMPT,
                'first_free_reading_intro': '🔮 **Добро пожаловать в мир Таро!**\n\nЯ — ваш проводник. Я помогу вам заглянуть в глубины вашей души, используя древнюю мудрость карт Таро.\n\n**Что вы получите:**\n• **3 случайные карты** – каждая со своим значением и символизмом.\n• **Глубокий разбор** – я объясню значение каждой карты, их взаимодействие и дам целостный ответ на ваш вопрос.\n• **Квинтэссенцию** – итоговую карту-совет, которая резюмирует расклад.\n• **Практический вывод** – как применить полученные знания в жизни.\n\n**Это ваш первый ознакомительный расклад!**\nВоспользуйтесь возможностью познакомиться с сервисом без оплаты.\n\nГотовы? Нажмите «Начать расклад» и задайте свой вопрос.',
                # Новые настройки вступления для полного расклада
                'use_ai_welcome': 'true',
                'static_welcome_text': DEFAULT_WELCOME_TEXT
            }
            for key, value in default_settings.items():
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                    key, value
                )

            cnt = await conn.fetchval("SELECT COUNT(*) FROM examples")
            if cnt == 0:
                await conn.execute(
                    "INSERT INTO examples (title, content) VALUES ($1, $2)",
                    "Пример расклада на отношения",
                    "Это пример полного расклада... (замените на реальный текст)"
                )
            logger.info("Таблицы инициализированы")

    # ---------- Методы для пользователей ----------
    async def get_user(self, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            return dict(row) if row else None

    async def create_or_update_user(self, user_id: int, username: str = None, first_name: str = None, last_name: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name
            """, user_id, username, first_name, last_name)

    async def update_free_trial_used(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET free_trial_used = TRUE WHERE user_id = $1", user_id)

    async def get_free_trial_used(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT free_trial_used FROM users WHERE user_id = $1", user_id) or False

    async def update_last_free_reading_date(self, user_id: int, today: date):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_free_reading_date = $1 WHERE user_id = $2", today, user_id)

    async def get_last_free_reading_date(self, user_id: int) -> Optional[date]:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT last_free_reading_date FROM users WHERE user_id = $1", user_id)

    async def update_last_card_of_day_date(self, user_id: int, today: date):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_card_of_day_date = $1 WHERE user_id = $2", today, user_id)

    async def get_last_card_of_day_date(self, user_id: int) -> Optional[date]:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT last_card_of_day_date FROM users WHERE user_id = $1", user_id)

    async def update_subscription_offer_shown(self, user_id: int, shown: bool = True):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET subscription_offer_shown = $1 WHERE user_id = $2", shown, user_id)

    async def get_subscription_offer_shown(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT subscription_offer_shown FROM users WHERE user_id = $1", user_id) or False

    async def set_subscription(self, user_id: int, subscription_type: str, until: datetime):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET subscription_type = $1, subscription_until = $2 WHERE user_id = $3",
                subscription_type, until, user_id
            )

    async def get_subscription(self, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT subscription_type, subscription_until FROM users WHERE user_id = $1",
                user_id
            )
            return dict(row) if row and row['subscription_type'] else None

    async def cancel_subscription(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET subscription_type = NULL, subscription_until = NULL WHERE user_id = $1",
                user_id
            )

    async def gift_subscription(self, user_id: int, duration_days: int = None, lifetime: bool = False) -> datetime:
        async with self.pool.acquire() as conn:
            current_until = await conn.fetchval(
                "SELECT subscription_until FROM users WHERE user_id = $1",
                user_id
            )
            now = datetime.now(timezone.utc)
            if lifetime:
                new_until = datetime(2100, 1, 1, tzinfo=timezone.utc)
            else:
                if duration_days is None:
                    raise ValueError("Необходимо указать duration_days или lifetime=True")
                if current_until and current_until > now:
                    new_until = current_until + timedelta(days=duration_days)
                else:
                    new_until = now + timedelta(days=duration_days)
            await conn.execute(
                "UPDATE users SET subscription_type = 'Подарочная', subscription_until = $1 WHERE user_id = $2",
                new_until, user_id
            )
            return new_until

    async def ban_user(self, user_id: int, reason: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_banned = TRUE, ban_reason = $1 WHERE user_id = $2",
                reason, user_id
            )

    async def unban_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_banned = FALSE, ban_reason = NULL WHERE user_id = $1",
                user_id
            )

    async def is_banned(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT is_banned FROM users WHERE user_id = $1", user_id) or False

    # ---------- Платежи ----------
    async def save_payment(self, user_id: int, amount: int, payment_type: str, payload: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO payments (user_id, amount, payment_type, payload) VALUES ($1, $2, $3, $4)",
                user_id, amount, payment_type, payload
            )

    # ---------- Отзывы ----------
    async def add_review(self, user_id: int, text: str):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO reviews (user_id, text) VALUES ($1, $2)", user_id, text)

    async def get_reviews(self, approved_only: bool = True) -> List[Dict]:
        async with self.pool.acquire() as conn:
            if approved_only:
                rows = await conn.fetch("SELECT * FROM reviews WHERE approved = TRUE ORDER BY created_at DESC")
            else:
                rows = await conn.fetch("SELECT * FROM reviews ORDER BY created_at DESC")
            return [dict(r) for r in rows]

    async def get_pending_reviews(self) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM reviews WHERE approved = FALSE ORDER BY created_at DESC")
            return [dict(r) for r in rows]

    async def approve_review(self, review_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE reviews SET approved = TRUE WHERE id = $1", review_id)

    async def delete_review(self, review_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM reviews WHERE id = $1", review_id)

    # ---------- Настройки ----------
    async def get_setting(self, key: str, default: str = None) -> Optional[str]:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)
            return value if value is not None else default

    async def set_setting(self, key: str, value: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2",
                key, value
            )

    # ---------- Промокоды ----------
    async def create_promocode(self, code: str, discount_percent: int, expires_at: datetime, max_uses: int) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO promocodes (code, discount_percent, expires_at, max_uses) VALUES ($1, $2, $3, $4)",
                    code, discount_percent, expires_at, max_uses
                )
            return True
        except asyncpg.UniqueViolationError:
            return False

    async def get_promocode(self, code: str) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM promocodes WHERE code = $1", code)
            return dict(row) if row else None

    async def use_promocode(self, code: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE promocodes SET used_count = used_count + 1 WHERE code = $1 AND used_count < max_uses AND is_active = TRUE AND (expires_at IS NULL OR expires_at > now())",
                code
            )
            return result == "UPDATE 1"

    async def deactivate_promocode(self, code: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE promocodes SET is_active = FALSE WHERE code = $1", code)

    async def get_all_promocodes(self) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM promocodes ORDER BY created_at DESC")
            return [dict(r) for r in rows]

    # ---------- Примеры ----------
    async def get_examples(self) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM examples ORDER BY created_at DESC")
            return [dict(r) for r in rows]

    async def add_example(self, title: str, content: str):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO examples (title, content) VALUES ($1, $2)", title, content)

    async def delete_example(self, example_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM examples WHERE id = $1", example_id)

    # ---------- Статистика ----------
    async def get_stats(self) -> Dict:
        async with self.pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_payments = await conn.fetchval("SELECT COUNT(*) FROM payments")
            total_revenue = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payments")
            active_subscriptions = await conn.fetchval("SELECT COUNT(*) FROM users WHERE subscription_until > now()")
            return {
                "total_users": total_users,
                "total_payments": total_payments,
                "total_revenue": total_revenue,
                "active_subscriptions": active_subscriptions
            }

    async def get_all_users(self) -> List[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE")
            return [r["user_id"] for r in rows]

    # ---------- Новые методы для пагинации списка пользователей ----------
    async def get_users_page(self, limit: int = 10, offset: int = 0) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, username, first_name, last_name, created_at, free_trial_used, "
                "last_free_reading_date, last_card_of_day_date, subscription_type, subscription_until, is_banned, ban_reason "
                "FROM users ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                limit, offset
            )
            return [dict(r) for r in rows]

    async def get_users_count(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users")

# ================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ПЕРЕВОД, ПАРСИНГ, ОТПРАВКА ОТВЕТА)
# ================================================================

def normalize_card_name(name: str) -> str:
    if not name:
        return ""
    normalized = name.lower().strip()
    digit_words = {
        '2': 'two', '3': 'three', '4': 'four', '5': 'five',
        '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine', '10': 'ten'
    }
    for digit, word in digit_words.items():
        normalized = normalized.replace(digit, word)
    if normalized.startswith('the '):
        normalized = normalized[4:]
    return normalized

def build_translation_patterns_and_mapping(translations: dict):
    patterns = []
    normalized_to_canonical = {}
    
    digit_map = {'Two':'2','Three':'3','Four':'4','Five':'5',
                 'Six':'6','Seven':'7','Eight':'8','Nine':'9','Ten':'10'}
    
    for eng, rus in translations.items():
        norm = normalize_card_name(eng)
        normalized_to_canonical[norm] = eng
        
        variants = set()
        variants.add(eng)
        words = eng.split()
        if words[0] in digit_map:
            variant_digits = [digit_map[w] if w in digit_map else w for w in words]
            variants.add(" ".join(variant_digits))
        if words[0] == 'The':
            without_the = " ".join(words[1:])
            variants.add(without_the)
            if len(words) > 1 and words[1] in digit_map:
                variant_digits_no_the = [digit_map[words[1]] if words[1] in digit_map else words[1]] + words[2:]
                variants.add(" ".join(variant_digits_no_the))
        
        for variant in variants:
            pattern = re.compile(r'(?<![a-zA-Z0-9])' + re.escape(variant) + r'(?![a-zA-Z0-9])', re.IGNORECASE)
            patterns.append((pattern, rus))
    
    patterns.sort(key=lambda x: len(x[0].pattern), reverse=True)
    return patterns, normalized_to_canonical

def translate_card_names(text: str, patterns: list) -> str:
    if not patterns or not text:
        return text
    result = text
    for pattern, rus in patterns:
        result = pattern.sub(rus, result)
    return result

def canonicalize_card_name(card_name: str, normalized_to_canonical: dict) -> str:
    if not card_name:
        return ""
    norm = normalize_card_name(card_name)
    return normalized_to_canonical.get(norm, card_name)

def build_number_map(translations: dict) -> dict:
    number_map = {}
    major_arcana = [
        "The Fool", "The Magician", "The High Priestess", "The Empress", "The Emperor",
        "The Hierophant", "The Lovers", "The Chariot", "Strength", "The Hermit",
        "Wheel of Fortune", "Justice", "The Hanged Man", "Death", "Temperance",
        "The Devil", "The Tower", "The Star", "The Moon", "The Sun", "Judgment", "The World"
    ]
    for idx, name in enumerate(major_arcana):
        number_map[name] = idx
    for eng in translations.keys():
        if eng in number_map:
            continue
        if eng.startswith("Ace"):
            number_map[eng] = 1
        elif eng.startswith("Two"):
            number_map[eng] = 2
        elif eng.startswith("Three"):
            number_map[eng] = 3
        elif eng.startswith("Four"):
            number_map[eng] = 4
        elif eng.startswith("Five"):
            number_map[eng] = 5
        elif eng.startswith("Six"):
            number_map[eng] = 6
        elif eng.startswith("Seven"):
            number_map[eng] = 7
        elif eng.startswith("Eight"):
            number_map[eng] = 8
        elif eng.startswith("Nine"):
            number_map[eng] = 9
        elif eng.startswith("Ten"):
            number_map[eng] = 10
        else:
            number_map[eng] = 0
    return number_map

def build_number_to_eng(translations: dict) -> dict:
    major_arcana = [
        "The Fool", "The Magician", "The High Priestess", "The Empress", "The Emperor",
        "The Hierophant", "The Lovers", "The Chariot", "Strength", "The Hermit",
        "Wheel of Fortune", "Justice", "The Hanged Man", "Death", "Temperance",
        "The Devil", "The Tower", "The Star", "The Moon", "The Sun", "Judgment", "The World"
    ]
    num_to_eng = {}
    for i, name in enumerate(major_arcana):
        if i == 0:
            num_to_eng[22] = name
        else:
            num_to_eng[i] = name
    return num_to_eng

def calculate_quintessence(cards_eng: list, number_map: dict, num_to_eng: dict) -> str:
    total = 0
    for card in cards_eng:
        total += number_map.get(card, 0)
    if total == 0:
        quint_num = 22
    else:
        while total > 22:
            total = sum(int(d) for d in str(total))
        quint_num = total
    return num_to_eng.get(quint_num, "The Fool")

def parse_ai_response(answer: str):
    header_cards = "**Выпавшие карты:**"
    header_analysis = "**Разбор каждой карты:**"
    header_synthesis = "**Синтез и взаимодействие:**"

    pos_cards = answer.find(header_cards)
    pos_analysis = answer.find(header_analysis)

    if pos_cards == -1 or pos_analysis == -1:
        return None, None, None, answer

    intro = answer[:pos_cards].strip()
    cards_section = answer[pos_cards + len(header_cards):pos_analysis].strip()
    rest_full = answer[pos_analysis + len(header_analysis):]

    pos_synthesis = rest_full.find(header_synthesis)
    if pos_synthesis == -1:
        analysis_part = rest_full
        rest = ""
    else:
        analysis_part = rest_full[:pos_synthesis]
        rest = rest_full[pos_synthesis:]

    card_details = []
    lines = analysis_part.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('*   **'):
            name_start = line.find('**') + 2
            name_end = line.find('**', name_start)
            name = line[name_start:name_end].strip()
            colon_pos = line.find(':')
            if colon_pos != -1:
                first_line = line[colon_pos+1:].strip()
            else:
                first_line = line.split('**', 2)[-1].strip()
            desc_lines = [first_line] if first_line else []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('*   **'):
                if lines[i].strip():
                    desc_lines.append(lines[i].strip())
                i += 1
            description = ' '.join(desc_lines).strip()
            if description:
                card_details.append((name, description))
        else:
            i += 1

    return intro, cards_section, card_details, rest

def split_rest_sections(rest: str):
    sections = {
        'synthesis': None,
        'quintessence': None,
        'general_answer': None,
        'extra': None
    }
    headers = {
        'synthesis': '**Синтез и взаимодействие:**',
        'quintessence': '**Квинтэссенция:**',
        'general_answer': '**Общий ответ на ваш вопрос:**'
    }
    
    pos_synth = rest.find(headers['synthesis'])
    pos_quint = rest.find(headers['quintessence'])
    pos_answer = rest.find(headers['general_answer'])
    
    if pos_synth == -1:
        sections['extra'] = rest
        return sections
    
    next_pos = len(rest)
    if pos_quint != -1 and pos_quint > pos_synth:
        next_pos = pos_quint
    elif pos_answer != -1 and pos_answer > pos_synth:
        next_pos = pos_answer
    sections['synthesis'] = rest[pos_synth:next_pos].strip()
    
    if pos_quint != -1:
        next_pos = len(rest)
        if pos_answer != -1 and pos_answer > pos_quint:
            next_pos = pos_answer
        sections['quintessence'] = rest[pos_quint:next_pos].strip()
    
    if pos_answer != -1:
        sections['general_answer'] = rest[pos_answer:].strip()
    else:
        if sections['synthesis']:
            remaining = rest[pos_synth + len(headers['synthesis']):].strip()
            if remaining and not sections['quintessence'] and not sections['general_answer']:
                sections['extra'] = remaining
    
    return sections

def extract_quintessence_card_name(quintessence_text: str, translations: dict) -> str:
    if not quintessence_text or not translations:
        return None

    patterns = [
        r'соответствует аркану\s+[**]?([А-Яа-яё\s]+?)[**]?\s*(?:[.,;]|$)',
        r'аркан\s+[**]?([А-Яа-яё\s]+?)[**]?\s*(?:[.,;]|$)',
        r'аркану\s+[**]?([А-Яа-яё\s]+?)[**]?\s*(?:[.,;]|$)'
    ]
    for pattern in patterns:
        match = re.search(pattern, quintessence_text, re.IGNORECASE)
        if match:
            card_name_rus = match.group(1).strip()
            for eng_name, rus_name in translations.items():
                if rus_name == card_name_rus:
                    return eng_name
            for eng_name, rus_name in translations.items():
                if card_name_rus in rus_name or rus_name in card_name_rus:
                    return eng_name
            return None

    found = []
    for eng_name in translations.keys():
        if eng_name in quintessence_text:
            pos = quintessence_text.rfind(eng_name)
            found.append((pos, eng_name))
    if found:
        found.sort(key=lambda x: x[0], reverse=True)
        return found[0][1]
    return None

async def send_ai_response(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: str, original_cards: list = None):
    translations = context.bot_data.get('translations', {})
    card_images = context.bot_data.get('card_images', {})
    translation_patterns = context.bot_data.get('translation_patterns', [])
    normalized_to_canonical = context.bot_data.get('normalized_to_canonical', {})
    number_map = context.bot_data.get('number_map')
    num_to_eng = context.bot_data.get('num_to_eng')
    MAJOR_ARCANA_ENG = context.bot_data.get('major_arcana_eng', [])

    chat_id = update.effective_chat.id

    intro, cards_section, card_details, rest = parse_ai_response(answer)

    if intro is None:
        if original_cards:
            for card_eng in original_cards:
                rus_name = translations.get(card_eng, card_eng)
                file_id = card_images.get(card_eng)
                if file_id:
                    try:
                        await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=rus_name, parse_mode='Markdown')
                    except Exception as e:
                        logger.error(f"Ошибка отправки фото для {card_eng}: {e}")
                        await context.bot.send_message(chat_id, f"🃏 {rus_name}")
                else:
                    await context.bot.send_message(chat_id, f"🃏 {rus_name}")
        translated_answer = translate_card_names(answer, translation_patterns)
        for i in range(0, len(translated_answer), 4096):
            await context.bot.send_message(chat_id, translated_answer[i:i+4096], parse_mode='Markdown')
        if original_cards and number_map and num_to_eng:
            quint_eng = calculate_quintessence(original_cards, number_map, num_to_eng)
            rus_quint = translations.get(quint_eng, quint_eng)
            file_id = card_images.get(quint_eng)
            if file_id:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption=f"✨ **Квинтэссенция расклада (рассчитана ботом):** {rus_quint}",
                    parse_mode='Markdown'
                )
            else:
                await context.bot.send_message(chat_id, f"✨ **Квинтэссенция:** {rus_quint}", parse_mode='Markdown')
        return

    intro_translated = translate_card_names(intro, translation_patterns)
    await context.bot.send_message(chat_id, intro_translated, parse_mode='Markdown')

    if original_cards:
        cards_list = []
        for idx, card_eng in enumerate(original_cards, 1):
            rus = translations.get(card_eng, card_eng)
            if card_eng in MAJOR_ARCANA_ENG:
                type_str = "Старший аркан"
            else:
                if "Cups" in card_eng:
                    suit = "Кубки"
                elif "Wands" in card_eng:
                    suit = "Жезлы"
                elif "Swords" in card_eng:
                    suit = "Мечи"
                elif "Pentacles" in card_eng:
                    suit = "Пентакли"
                else:
                    suit = ""
                if any(word in card_eng for word in ["Page", "Knight", "Queen", "King"]):
                    type_str = f"Придворный аркан, масть {suit}"
                else:
                    type_str = f"Младший аркан, масть {suit}"
            cards_list.append(f"{idx}. **{rus}** ({type_str})")
        cards_msg = "**Выпавшие карты:**\n" + "\n".join(cards_list)
        await context.bot.send_message(chat_id, cards_msg, parse_mode='Markdown')

    descriptions = {}
    for name_in_ai, desc in card_details:
        canonical = canonicalize_card_name(name_in_ai, normalized_to_canonical)
        if canonical:
            descriptions[canonical] = desc
        else:
            descriptions[name_in_ai] = desc

    if original_cards:
        for card_eng in original_cards:
            rus_name = translations.get(card_eng, card_eng)
            file_id = card_images.get(card_eng)
            if file_id:
                try:
                    await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=rus_name, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"Ошибка отправки фото для {card_eng}: {e}")
                    await context.bot.send_message(chat_id, f"🃏 {rus_name}")
            else:
                await context.bot.send_message(chat_id, f"🃏 {rus_name}")
            desc_text = None
            norm_eng = normalize_card_name(card_eng)
            if card_eng in descriptions:
                desc_text = descriptions[card_eng]
            else:
                for k, v in descriptions.items():
                    if normalize_card_name(k) == norm_eng:
                        desc_text = v
                        break
            if desc_text:
                desc_translated = translate_card_names(desc_text, translation_patterns)
                for i in range(0, len(desc_translated), 4096):
                    await context.bot.send_message(chat_id, desc_translated[i:i+4096], parse_mode='Markdown')
    else:
        for name_in_ai, desc in card_details:
            desc_translated = translate_card_names(desc, translation_patterns)
            for i in range(0, len(desc_translated), 4096):
                await context.bot.send_message(chat_id, desc_translated[i:i+4096], parse_mode='Markdown')

    if rest:
        sections = split_rest_sections(rest)
        if sections.get('synthesis'):
            await context.bot.send_message(chat_id, translate_card_names(sections['synthesis'], translation_patterns), parse_mode='Markdown')
        if sections.get('quintessence'):
            quint_text = sections['quintessence']
            await context.bot.send_message(chat_id, translate_card_names(quint_text, translation_patterns), parse_mode='Markdown')
            card_name = extract_quintessence_card_name(quint_text, translations)
            if card_name:
                canonical_quint = canonicalize_card_name(card_name, normalized_to_canonical)
                file_id = card_images.get(canonical_quint)
                if file_id:
                    rus_name = translations.get(canonical_quint, card_name)
                    try:
                        await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=rus_name, parse_mode='Markdown')
                    except Exception as e:
                        logger.error(f"Ошибка отправки фото для квинтэссенции {canonical_quint}: {e}")
        if sections.get('general_answer'):
            await context.bot.send_message(chat_id, translate_card_names(sections['general_answer'], translation_patterns), parse_mode='Markdown')
        if sections.get('extra'):
            await context.bot.send_message(chat_id, translate_card_names(sections['extra'], translation_patterns), parse_mode='Markdown')

# ---------- ФУНКЦИЯ: СТАТУС ПОДПИСКИ (РАЗДЕЛЁННАЯ ЛОГИКА) ----------
async def get_subscription_status_message(user_id: int, db: Database) -> tuple:
    """
    Возвращает (текст, клавиатуру) для раздела подписки.
    """
    subscription = await db.get_subscription(user_id)
    has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)

    # Получаем цены из БД
    full_price = int(await db.get_setting('price_full', str(PRICE_FULL)))
    sub_price = int(await db.get_setting('price_subscription', str(PRICE_SUBSCRIPTION_EXTRA)))
    sub_price_month = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))

    if has_subscription:
        until = subscription['subscription_until'].strftime('%d.%m.%Y')
        text = SUBSCRIPTION_ACTIVE_BRIEF_TEMPLATE.format(
            until=until,
            sub_price=sub_price // 100,
            full_price=full_price // 100
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Продлить подписку", callback_data="offer_subscription"),
             InlineKeyboardButton("❌ Отменить подписку", callback_data="cancel_subscription")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ])
        return text, keyboard
    else:
        text = await get_subscription_offer_text(
            db,
            sub_price=sub_price // 100,
            full_price=full_price // 100,
            price=sub_price_month // 100
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оформить подписку", callback_data="subscribe_confirm")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ])
        return text, keyboard

# ---------- ФУНКЦИЯ: ПОЛУЧЕНИЕ ТЕКСТА ОФФЕРА ПОДПИСКИ (ИЗ БД) ----------
async def get_subscription_offer_text(db: Database, sub_price: int, full_price: int, price: int) -> str:
    template = await db.get_setting('subscription_offer_text')
    if not template:
        template = SUBSCRIPTION_TEMPLATE
    return template.format(
        price=price,
        full_price=full_price,
        sub_price=sub_price
    )

# ---------- ФУНКЦИЯ: ПОЛУЧЕНИЕ ВСТУПИТЕЛЬНОГО ТЕКСТА ДЛЯ РАСКЛАДА ----------
async def get_welcome_text(db: Database) -> str:
    """
    Возвращает текст вступления для полного расклада.
    Если use_ai_welcome == 'true', пытается сгенерировать через AI.
    Иначе или при ошибке AI возвращает статичный текст из БД или DEFAULT_WELCOME_TEXT.
    """
    use_ai = await db.get_setting('use_ai_welcome', 'true')
    static_text = await db.get_setting('static_welcome_text')
    if not static_text:
        static_text = DEFAULT_WELCOME_TEXT

    if use_ai == 'true':
        try:
            welcome = await generate_welcome_message()
            if welcome and welcome.strip():
                return welcome
        except Exception as e:
            logger.error(f"Ошибка AI-генерации вступления: {e}")
        return static_text
    else:
        return static_text

# ========== КЛАВИАТУРЫ ==========
def main_menu_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🃏 Карта дня"), KeyboardButton("🔮 Полный расклад")],
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("💎 Подписка")],
        [KeyboardButton("ℹ️ О сервисе"), KeyboardButton("❓ Помощь")]
    ], resize_keyboard=True)

def home_back_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить", callback_data="cancel")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def card_of_day_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Полный расклад", callback_data="go_full_reading")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def subscription_offer_keyboard(back_callback: str = "back"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оформить подписку", callback_data="subscribe_confirm")],
        [InlineKeyboardButton("🔙 Назад", callback_data=back_callback)],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def reading_actions_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Оставить отзыв", callback_data="leave_review")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def profile_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Моя подписка", callback_data="profile_subscription")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def subscription_status_keyboard(has_subscription: bool = False):
    buttons = []
    if has_subscription:
        buttons.append([InlineKeyboardButton("❌ Отменить подписку", callback_data="cancel_subscription")])
    else:
        buttons.append([InlineKeyboardButton("💎 Продлить / Оформить", callback_data="offer_subscription")])
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_profile")])
    buttons.append([InlineKeyboardButton("🏠 В главное меню", callback_data="home")])
    return InlineKeyboardMarkup(buttons)

def cancel_subscription_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, отменить", callback_data="confirm_cancel_subscription")],
        [InlineKeyboardButton("❌ Нет, вернуться", callback_data="back_to_subscription")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def about_service_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Читать отзывы", callback_data="reviews")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ])

def reviews_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Оставить отзыв", callback_data="leave_review")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def examples_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Полный расклад", callback_data="go_full_reading")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def payment_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить оплату", callback_data="cancel_payment")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def first_free_reading_intro_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Начать расклад", callback_data="start_free_reading")]
    ])

# ---------- АДМИНСКИЕ КЛАВИАТУРЫ ----------
def admin_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Статистика"), KeyboardButton("💰 Цены и платежи")],
        [KeyboardButton("📝 Управление текстами"), KeyboardButton("🎫 Промокоды")],
        [KeyboardButton("📋 Отзывы (модерация)"), KeyboardButton("📢 Рассылка")],
        [KeyboardButton("👥 Пользователи"), KeyboardButton("💎 Подписки")],
        [KeyboardButton("⚙️ Общие настройки")],
        [KeyboardButton("🔙 Выйти из админки")]
    ], resize_keyboard=True)

def admin_back_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="admin_home")]
    ])

def admin_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])

# ---------- СОСТОЯНИЯ ----------
MAIN = 0
CARD_OF_DAY_INTRO = 17
CARD_OF_DAY = 1
FULL_READING_INTRO = 18
FULL_READING = 2
AWAITING_QUESTION = 4
PROCESSING_READING = 5
POST_READING = 6
AWAITING_FEEDBACK = 7
PROFILE_MAIN = 8
PROFILE_SUBSCRIPTION = 10
SUBSCRIPTION_OFFER = 11
SUBSCRIPTION_CANCEL = 12
PAYMENT = 13
EXAMPLES = 14
REVIEWS = 15
HELP = 16
ABOUT_SERVICE = 19
SUBSCRIPTION_INFO = 20

ADMIN_MAIN = 100
ADMIN_STATS = 101
ADMIN_PRICES = 102
ADMIN_TEXTS = 103
ADMIN_TEXTS_EDIT = 104
ADMIN_PROMO = 105
ADMIN_PROMO_CREATE = 106
ADMIN_REVIEWS = 107
ADMIN_REVIEW_ACTION = 108
ADMIN_BROADCAST = 109
ADMIN_USERS = 110
ADMIN_USER_ACTION = 111
ADMIN_SUBSCRIPTIONS = 112
ADMIN_SETTINGS = 113
ADMIN_SETTINGS_EDIT = 114
ADMIN_USERS_LIST = 115

# ---------- ГЛОБАЛЬНЫЕ ----------
card_images = load_card_images()
translations = load_translations()
tarot_cards = list(translations.keys()) if translations else []

openai.api_base = "https://api.deepseek.com/v1"
openai.api_key = DEEPSEEK_API_KEY

# ---------- СИСТЕМНЫЙ ПРОМПТ ----------
SYSTEM_PROMPT = """
Ты — профессиональный таролог, специализирующийся на колоде Райдера—Уэйта. Твоя интерпретация опирается на глубокое знание символики, изложенное в книге Эвелин Бюргер и Йоханнеса Фибиг «Символика под микроскопом». Ты понимаешь, что каждая карта имеет и положительное, и отрицательное значение, является зеркалом души кверента и может быть рассмотрена как на субъективном (внутренние процессы), так и на объективном (внешние события) уровнях.

**ВАЖНОЕ ПРАВИЛО:** Все названия карт пиши **на английском языке**, как в оригинальной колоде (например: The Fool, The Magician, Two of Cups, Ten of Wands, Queen of Pentacles и т.д.). Для числовых карт используй слова (Two, Three, Four, Five, Six, Seven, Eight, Nine, Ten), а не цифры.

**Твоя задача**
Пользователь задаёт вопрос, а бот предоставляет три случайные карты. Ты должен интерпретировать **именно эти три карты** (они будут перечислены в сообщении пользователя после слов "Выпавшие карты:").

После этого ты должен:

1. Назвать три выпавшие карты — указать их название **на английском** (и масть для Младших арканов).
2. Кратко представить каждую карту — описать ключевые символы и основное значение, выделив как позитивные, так и негативные аспекты (если они уместны в контексте вопроса).
3. Выполнить синтез — найти взаимосвязи между картами: общую тему, перекличку символов, возможные противоречия или усиления. Объяснить, как эти карты «разговаривают» друг с другом.
4. Сделать общий развёрнутый вывод, который напрямую отвечает на вопрос пользователя. Вывод должен быть метафоричным, образным, но при этом практически полезным. Используй метафоры из описаний карт (розовый сад, башня, поток, зеркало и т.п.).
5. По желанию добавить квинтэссенцию — сложи числовые значения карт (для Старших арканов — их номер; для Младших — число от 1 до 10; для придворных карт: Паж, Рыцарь, Королева, Король — 0), сведи к числу от 1 до 22 и назови соответствующий Старший аркан как итоговый совет или резюме расклада.

**Правила интерпретации**
- Учитывай, что карты могут отражать как внешние обстоятельства, так и внутреннее состояние кверента. Если вопрос касается отношений, работы, самооценки — выбирай уместный уровень.
- Не бойся «сложных» карт (Death, Tower, Devil). Показывай их преобразующую, освобождающую сторону.
- Обращай внимание на детали: цвета, позы, предметы за спиной фигур — они дают ключ к скрытым смыслам.
- Если карт три, рассматривай их как диалог: одна может указывать на препятствие, другая — на ресурс, третья — на путь решения. Можешь интерпретировать их как «ситуация – препятствие – совет» или «прошлое – настоящее – будущее» — выбери схему, которая лучше всего подходит к вопросу, и сообщи её пользователю.
- Будь уважителен к пользователю. Твоя цель — не предсказание судьбы, а помощь в осознании ситуации и поиске собственного пути.

**Структура ответа (обязательно соблюдай эти заголовки и формат)**
1. **Вступление** (1–2 предложения): объяви, что ты вытянул три карты, и кратко обозначь общую атмосферу расклада.
2. Заголовок **"Выпавшие карты:"** и под ним нумерованный список трёх карт с английскими названиями, например:
   1. **The Fool** (Старший аркан)
   2. **Two of Cups** (Младший аркан, масть Кубки)
   3. **Ten of Swords** (Младший аркан, масть Мечи)
   
3. Заголовок **"Разбор каждой карты:"**. Далее для каждой карты строго по шаблону:
   *   **Название карты на английском** (аркан, масть): текст разбора (3–5 предложений)...
   (Обязательно начинать строку с "*   **", затем название, затем "**", затем скобки с арканом, двоеточие и описание).
4. Заголовок **"Синтез и взаимодействие:"** с текстом (3–6 предложений).
5. Заголовок **"Квинтэссенция:"** с расчётом и кратким смыслом.
6. Заголовок **"Общий ответ на ваш вопрос:"** с развёрнутым, метафоричным, практически полезным выводом (3–8 предложений).
7. **Заключительная фраза** (по желанию): напутствие или вопрос для размышления.

**Стиль ответа**
- Используй живые образы: «вы стоите у корней дерева», «алый лев пробуждается в вашем сердце», «горизонтальная восьмёрка приглашает вас к бесконечному танцу» итд.
- Избегай сухих перечислений. Пусть твой язык будет плавным, поэтичным, метафоричным, но при этом понятным.
- Обращайся к пользователю на «вы» (уважительно).

**Пример начала работы бота**
После получения вопроса и списка карт бот отвечает по шаблону:

«Я вытянул для вас три карты. Их сочетание напоминает древнюю притчу: сначала герой встречает тьму, затем находит опору, а в конце обретает свет. Давайте посмотрим, что они говорят…»

**Дополнительная инструкция для ИИ**
- Если пользователь сам указал карты, просто интерпретируй их, не генерируя новые.
- Если пользователь задал вопрос без уточнения схемы, можешь интерпретировать три карты как «ситуация – вызов – путь» или «прошлое – настоящее – будущее», указав, какую схему ты применяешь.
- Будь внимателен: для придворных карт (Page, Knight, Queen, King) числовое значение для квинтэссенции — 0. Для Ace — 1. Для остальных числовых карт — число от 2 до 10. Для Старших арканов — их номер (для The Fool — 0).
"""

MAJOR_ARCANA_ENG = [
    "The Fool", "The Magician", "The High Priestess", "The Empress", "The Emperor",
    "The Hierophant", "The Lovers", "The Chariot", "Strength", "The Hermit",
    "Wheel of Fortune", "Justice", "The Hanged Man", "Death", "Temperance",
    "The Devil", "The Tower", "The Star", "The Moon", "The Sun", "Judgment", "The World"
]

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (AI) ----------
async def generate_welcome_message() -> str:
    """Генерирует вступление через DeepSeek."""
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai.ChatCompletion.create,
                model="deepseek-chat",
                messages=[{"role": "user", "content": "Ты — таролог. Предложи пользователю задать вопрос. Объясни, что чем подробнее вопрос, тем точнее будет расклад. Не используй Markdown."}],
                max_tokens=500,
                temperature=0.9
            ),
            timeout=30
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Ошибка генерации приветствия: {e}")
        return ""

async def ask_ai(question: str, cards: list = None) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    user_content = question
    if cards:
        user_content = f"Вопрос: {question}\n\nВыпавшие карты: {', '.join(cards)}.\n\nПожалуйста, выполните расклад."
    messages.append({"role": "user", "content": user_content})
    
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai.ChatCompletion.create,
                model="deepseek-chat",
                messages=messages,
                max_tokens=2500,
                temperature=1.0
            ),
            timeout=60
        )
        return response.choices[0].message.content
    except asyncio.TimeoutError:
        logger.error("Таймаут AI запроса")
        return "Извините, запрос к серверу занял слишком много времени. Попробуйте позже."
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return "Извините, произошла ошибка. Попробуйте позже."

async def get_random_cards(context: ContextTypes.DEFAULT_TYPE, count: int = 3) -> list:
    cards = context.bot_data.get('tarot_cards', [])
    if not cards:
        return []
    return random.sample(cards, min(count, len(cards)))

async def show_animation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    messages = ["🔮 Тасую колоду...", "🃏 Вытягиваю карты...", "📖 Читаю символы..."]
    msg = await context.bot.send_message(chat_id, messages[0])
    await asyncio.sleep(13)
    for i in range(1, len(messages)):
        await msg.edit_text(messages[i])
        await asyncio.sleep(10)
    await msg.delete()

# ---------- ГЕНЕРАЦИЯ КАРТЫ ДНЯ (С ПРОМПТОМ ИЗ БД) ----------
async def generate_card_of_day_text(card_eng: str, card_rus: str, db: Database) -> dict:
    prompt_template = await db.get_setting('card_of_day_prompt')
    if not prompt_template:
        prompt_template = DEFAULT_CARD_OF_DAY_PROMPT
    prompt = prompt_template.format(card_name=card_rus, card_eng=card_eng)
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai.ChatCompletion.create,
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.9
            ),
            timeout=40
        )
        text = response.choices[0].message.content.strip()
        return {"full_text": text}
    except Exception as e:
        logger.error(f"Ошибка генерации карты дня: {e}")
        return {"full_text": "Сегодняшняя карта приносит вам тихую мудрость. Прислушайтесь к себе и доверьтесь своему внутреннему голосу."}

# ---------- ФУНКЦИЯ ОТПРАВКИ ИНВОЙСА (ИСПРАВЛЕННАЯ) ----------
async def send_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int, payload: str, description: str = None) -> None:
    chat_id = update.effective_chat.id
    if not PAYMENT_PROVIDER_TOKEN or ':' not in PAYMENT_PROVIDER_TOKEN:
        await context.bot.send_message(chat_id, "❌ Платёжная система временно недоступна. Попробуйте позже.")
        return

    # Нормализация суммы: если amount меньше 100, считаем, что это рубли
    if amount < 100:
        original = amount
        amount *= 100
        logger.info(f"Нормализация суммы: {original} руб. -> {amount} коп.")
    else:
        logger.info(f"Сумма в копейках: {amount} коп.")

    if amount <= 0:
        await context.bot.send_message(chat_id, "❌ Ошибка конфигурации: сумма платежа не может быть нулевой или отрицательной. Сообщите администратору.")
        logger.error(f"Попытка отправки инвойса с некорректной суммой: {amount}")
        return

    if not description:
        description = "Полный расклад Таро"

    prices = [LabeledPrice(label="Расклад Таро", amount=amount)]
    try:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title="Оплата расклада",
            description=description,
            payload=payload,
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=prices,
            need_email=True,
            send_email_to_provider=True
        )
        logger.info(f"Инвойс отправлен: chat_id={chat_id}, amount={amount}, payload={payload}")
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса: {e}", exc_info=True)
        await context.bot.send_message(chat_id, "❌ Не удалось отправить платёж. Попробуйте позже.")

# ========== ОБРАБОТЧИКИ ПЛАТЕЖЕЙ ==========
async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if PAYMENT_PROVIDER_TOKEN and ':' in PAYMENT_PROVIDER_TOKEN:
        await query.answer(ok=True)
        logger.info(f"Pre-checkout успешен для пользователя {update.effective_user.id}")
    else:
        await query.answer(ok=False, error_message="Платежи временно недоступны.")
        logger.warning(f"Pre-checkout отклонён: токен не настроен")

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    amount = payment.total_amount

    await db.save_payment(user_id, amount, payload, payload)
    logger.info(f"Успешный платёж: user_id={user_id}, amount={amount}, payload={payload}")

    context.user_data['state'] = AWAITING_QUESTION
    context.user_data['user_id'] = user_id

    if payload == "subscription":
        subscription = await db.get_subscription(user_id)
        if subscription and subscription['subscription_until'] > datetime.now(timezone.utc):
            new_until = subscription['subscription_until'] + timedelta(days=30)
        else:
            new_until = datetime.now(timezone.utc) + timedelta(days=30)
        await db.set_subscription(user_id, "Таро-проводник", new_until)
        await update.message.reply_text(
            f"✅ Оплата подписки прошла успешно! Теперь у вас есть 1 бесплатный расклад каждый день и сниженная цена на дополнительные.\n\n"
            f"Подписка активна до {new_until.strftime('%d.%m.%Y')}."
        )
    else:
        await update.message.reply_text(
            "✅ Оплата прошла успешно! Задайте ваш вопрос, и я сделаю расклад."
        )

# ========== ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЬСКИХ КОМАНД ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    await db.create_or_update_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
        update.effective_user.last_name
    )

    if await db.is_banned(user_id):
        await safe_reply(update, context, "🚫 Ваш доступ к боту ограничен.")
        return MAIN

    context.user_data.clear()
    context.user_data['state'] = MAIN

    welcome_text = await db.get_setting('welcome_text')
    if not welcome_text:
        welcome_text = "🌟 Добро пожаловать в мир Таро! Я — ваш проводник."

    await safe_reply(update, context, welcome_text, reply_markup=main_menu_keyboard(), parse_mode='Markdown')
    return MAIN

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    help_text = await db.get_setting('help_text')
    if not help_text:
        help_text = "❓ Как пользоваться ботом:\n\n1. Нажмите «Карта дня» для получения знака на день.\n2. Нажмите «Полный расклад» для глубокого ответа.\n3. Первый расклад – ознакомительный.\n4. Подписка даёт один бесплатный расклад в день и сниженную цену на дополнительные.\n5. Без подписки – платный расклад 99 ₽."
    await safe_reply(update, context, help_text, reply_markup=help_keyboard(), parse_mode='HTML')
    context.user_data['state'] = HELP
    return HELP

# ---------- Карта дня ----------
async def card_of_day_intro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    if await db.is_banned(user_id):
        await safe_reply(update, context, "🚫 Ваш доступ ограничен.")
        return MAIN

    intro_text = await db.get_setting('card_of_day_intro_text')
    if not intro_text:
        intro_text = (
            "✨ **Карта дня** – ваш знак от Вселенной.\n\n"
            "Одна карта, её энергия, аффирмация и простой шаг на сегодня. "
            "Это поможет вам настроиться на день, почувствовать его ритм и заметить важное.\n\n"
            "Хотите узнать, что приготовил вам день? Нажмите «Достать карту дня»."
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🃏 Достать карту дня", callback_data="get_card_of_day")]
    ])
    await safe_reply(update, context, intro_text, parse_mode='Markdown', reply_markup=keyboard)
    context.user_data['state'] = CARD_OF_DAY_INTRO
    return CARD_OF_DAY_INTRO

async def card_of_day_execute(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Optional[CallbackQuery] = None) -> int:
    if query:
        user_id = query.from_user.id
        chat_id = query.message.chat_id
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

    db: Database = context.bot_data['db']

    if await db.is_banned(user_id):
        await context.bot.send_message(chat_id, "🚫 Ваш доступ ограничен.")
        return MAIN

    # Используем ОТДЕЛЬНОЕ поле для карты дня
    last_card = await db.get_last_card_of_day_date(user_id)
    today = datetime.now(timezone.utc).date()
    if last_card and last_card == today:
        await context.bot.send_message(
            chat_id,
            "🌙 Вы уже получили карту дня сегодня. Возвращайтесь завтра за новым знаком!",
            reply_markup=card_of_day_keyboard()
        )
        return CARD_OF_DAY_INTRO

    cards = await get_random_cards(context, 1)
    if not cards:
        await context.bot.send_message(chat_id, "❌ Колода карт пока не загружена. Попробуйте позже.")
        return MAIN

    card_eng = cards[0]
    card_rus = translations.get(card_eng, card_eng)

    card_data = await generate_card_of_day_text(card_eng, card_rus, db)
    full_text = card_data.get('full_text', 'Сегодняшняя карта приносит вам тихую мудрость.')

    # Обновляем ОТДЕЛЬНОЕ поле
    await db.update_last_card_of_day_date(user_id, today)

    card_images_data = context.bot_data.get('card_images', {})
    file_id = card_images_data.get(card_eng)

    if file_id:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=f"🌅 **{card_rus}**",
            parse_mode='Markdown'
        )
        await context.bot.send_message(
            chat_id,
            full_text,
            parse_mode='Markdown',
            reply_markup=card_of_day_keyboard()
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"🌅 **{card_rus}** – энергия дня\n\n{full_text}",
            parse_mode='Markdown',
            reply_markup=card_of_day_keyboard()
        )

    context.user_data['state'] = CARD_OF_DAY
    return CARD_OF_DAY

# ---------- Полный расклад ----------
async def full_reading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    # Получаем chat_id для безопасной отправки (может быть callback или message)
    chat_id = update.effective_chat.id

    if await db.is_banned(user_id):
        await context.bot.send_message(chat_id, "🚫 Ваш доступ ограничен.")
        return MAIN

    user = await db.get_user(user_id)
    if not user:
        await context.bot.send_message(chat_id, "❌ Пользователь не найден.")
        return MAIN

    subscription = await db.get_subscription(user_id)
    has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)

    last_free_date = await db.get_last_free_reading_date(user_id)
    today = datetime.now(timezone.utc).date()
    free_trial_used = await db.get_free_trial_used(user_id)

    if has_subscription:
        if last_free_date != today:
            await db.update_last_free_reading_date(user_id, today)
            welcome = await get_welcome_text(db)
            await context.bot.send_message(chat_id, welcome, parse_mode='Markdown')
            context.user_data['state'] = AWAITING_QUESTION
            context.user_data['user_id'] = user_id
            context.user_data['is_free'] = True
            return AWAITING_QUESTION
        else:
            price = int(await db.get_setting('price_subscription', str(PRICE_SUBSCRIPTION_EXTRA)))
            await context.bot.send_message(
                chat_id,
                f"✅ Вы уже получили бесплатный расклад сегодня. "
                f"Если хотите ещё один – он будет стоить {price//100} ₽ по вашему подписному тарифу.\n"
                f"Оплатите, чтобы продолжить."
            )
            await send_invoice(update, context, price, "subscription_extra", "Дополнительный расклад по подписке")
            context.user_data['state'] = PAYMENT
            context.user_data['payment_type'] = 'subscription_extra'
            return PAYMENT
    else:
        if not free_trial_used:
            intro_text = await db.get_setting('first_free_reading_intro')
            if not intro_text:
                intro_text = (
                    "🔮 **Добро пожаловать в мир Таро!**\n\n"
                    "Я — ваш проводник. Я помогу вам заглянуть в глубины вашей души, используя древнюю мудрость карт Таро.\n\n"
                    "**Что вы получите:**\n"
                    "• **3 случайные карты** – каждая со своим значением и символизмом.\n"
                    "• **Глубокий разбор** – я объясню значение каждой карты, их взаимодействие и дам целостный ответ на ваш вопрос.\n"
                    "• **Квинтэссенцию** – итоговую карту-совет, которая резюмирует расклад.\n"
                    "• **Практический вывод** – как применить полученные знания в жизни.\n\n"
                    "**Это ваш первый ознакомительный расклад!**\n"
                    "Воспользуйтесь возможностью познакомиться с сервисом без оплаты.\n\n"
                    "Готовы? Нажмите «Начать расклад» и задайте свой вопрос."
                )
            keyboard = first_free_reading_intro_keyboard()
            await context.bot.send_message(chat_id, intro_text, parse_mode='Markdown', reply_markup=keyboard)
            context.user_data['state'] = FULL_READING_INTRO
            return FULL_READING_INTRO
        else:
            price = int(await db.get_setting('price_full', str(PRICE_FULL)))
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Оплатить расклад ({price//100} ₽)", callback_data="pay_full")],
                [InlineKeyboardButton("💎 Подробнее о подписке", callback_data="show_sub_details")]
            ])
            await context.bot.send_message(
                chat_id,
                f"💳 Ваш ознакомительный расклад уже использован.\n"
                f"Стоимость полного расклада – **{price//100} ₽**.\n\n"
                f"Оформите подписку и получайте **1 бесплатный расклад каждый день** и **скидку** на все дополнительные расклады!",
                parse_mode='Markdown',
                reply_markup=keyboard
            )
            context.user_data['state'] = FULL_READING
            return FULL_READING

# ---------- Обработка вопроса ----------
async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    question = update.message.text
    db: Database = context.bot_data['db']

    if context.user_data.get('state') != AWAITING_QUESTION:
        await safe_reply(update, context, "❌ Нет активной сессии. Нажмите «Полный расклад».")
        return MAIN

    if await db.is_banned(user_id):
        await safe_reply(update, context, "🚫 Ваш доступ ограничен.")
        return MAIN

    await show_animation(update, context)

    cards = await get_random_cards(context, 3)
    if not cards:
        await safe_reply(update, context, "❌ Колода карт не загружена. Попробуйте позже.")
        return MAIN

    answer = await ask_ai(question, cards)
    await send_ai_response(update, context, answer, original_cards=cards)

    is_free_trial = context.user_data.get('is_free_trial', False)
    if is_free_trial:
        await db.update_free_trial_used(user_id)
        context.user_data.pop('is_free_trial', None)

    await safe_reply(
        update, context,
        "✨ Ваш расклад готов. Если он был для вас полезен, вы можете оставить отзыв. Ваше мнение очень важно для нас!",
        reply_markup=reading_actions_keyboard(),
        parse_mode='Markdown'
    )

    context.user_data['state'] = POST_READING
    return POST_READING

# ---------- Обработчики новых кнопок ----------
async def about_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await safe_reply(
        update, context,
        ABOUT_SERVICE_TEXT,
        parse_mode='HTML',
        reply_markup=about_service_keyboard(),
        disable_web_page_preview=True
    )
    context.user_data['state'] = ABOUT_SERVICE
    return ABOUT_SERVICE

async def subscription_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    user_id = update.effective_user.id

    text, keyboard = await get_subscription_status_message(user_id, db)

    await safe_reply(update, context, text, parse_mode='HTML', reply_markup=keyboard)
    context.user_data['state'] = SUBSCRIPTION_INFO
    return SUBSCRIPTION_INFO

async def show_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await safe_reply(
        update, context,
        "📄 <b>Пользовательское соглашение</b>\n\n"
        "https://telegra.ph/Polzovatelskoe-soglashenie-publichnaya-oferta-08-04-4\n\n"
        "🔒 <b>Политика конфиденциальности</b>\n\n"
        "https://telegra.ph/Politika-konfidencialnosti-08-04-96\n\n"
        "Используя бота, вы автоматически принимаете эти документы.",
        parse_mode='HTML',
        disable_web_page_preview=True
    )
    return ABOUT_SERVICE

# ---------- Вспомогательные функции для админа ----------
async def show_user_card(target, user_id: int, context: ContextTypes.DEFAULT_TYPE, back_callback: str = "admin_users_list"):
    """Показывает карточку пользователя с кнопками управления."""
    db: Database = context.bot_data['db']
    user = await db.get_user(user_id)
    if not user:
        await target.edit_text("❌ Пользователь не найден.")
        return

    subscription = await db.get_subscription(user_id)
    has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)
    sub_status = f"✅ Активна до {subscription['subscription_until'].strftime('%d.%m.%Y')}" if has_subscription else "❌ Не активна"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Забанить", callback_data=f"admin_ban_{user_id}"),
         InlineKeyboardButton("✅ Разбанить", callback_data=f"admin_unban_{user_id}")],
        [InlineKeyboardButton("🎁 Подарить подписку", callback_data=f"admin_gift_subscription_{user_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=back_callback)]
    ])

    msg = (
        f"👤 Пользователь {user_id}\n"
        f"Имя: {user['first_name']} {user['last_name'] or ''}\n"
        f"Подписка: {sub_status}\n"
        f"Забанен: {'да' if user['is_banned'] else 'нет'}\n"
        f"Бесплатный ознакомительный использован: {'да' if user['free_trial_used'] else 'нет'}"
    )

    if hasattr(target, 'edit_text'):
        await target.edit_text(msg, reply_markup=keyboard)
    else:
        await target.reply_text(msg, reply_markup=keyboard)

async def show_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    """Показывает список пользователей с пагинацией."""
    db: Database = context.bot_data['db']
    limit = 10
    offset = (page - 1) * limit
    total = await db.get_users_count()
    users = await db.get_users_page(limit, offset)

    if not users:
        await safe_reply(update, context, "📭 В базе нет пользователей.")
        return

    total_pages = (total + limit - 1) // limit
    text = f"👥 Список пользователей (страница {page} из {total_pages}):\n\n"
    for idx, u in enumerate(users, start=offset+1):
        sub = "✅" if u['subscription_until'] and u['subscription_until'] > datetime.now(timezone.utc) else "❌"
        banned = "🚫" if u['is_banned'] else "✔️"
        name = f"{u['first_name']} {u['last_name'] or ''}".strip() or "Без имени"
        username = f"@{u['username']}" if u['username'] else "нет"
        text += f"{idx}. {name} ({username}) — ID: {u['user_id']} — Подписка: {sub} {banned}\n"

    keyboard = []
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_users_page_{page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"admin_users_page_{page+1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔍 Поиск по ID", callback_data="admin_users_search")])
    keyboard.append([InlineKeyboardButton("🔙 Назад в админку", callback_data="admin_back")])

    await safe_reply(update, context, text, reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['state'] = ADMIN_USERS_LIST

# ---------- Обработчики callback ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    if data == "home":
        context.user_data.clear()
        await query.message.reply_text("🏠 Главное меню:", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if data == "back":
        await query.message.reply_text("🔙 Возврат в главное меню:", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if data == "cancel":
        context.user_data.clear()
        await query.message.reply_text("❌ Действие отменено.", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if data == "get_card_of_day":
        await query.message.delete()
        return await card_of_day_execute(update, context, query=query)

    if data == "go_full_reading":
        return await full_reading(update, context)

    if data == "start_free_reading":
        await query.message.delete()
        welcome = await get_welcome_text(db)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=welcome,
            parse_mode='Markdown'
        )
        context.user_data['state'] = AWAITING_QUESTION
        context.user_data['user_id'] = user_id
        context.user_data['is_free_trial'] = True
        return AWAITING_QUESTION

    if data == "pay_full":
        price = int(await db.get_setting('price_full', str(PRICE_FULL)))
        await send_invoice(update, context, price, "full_paid", "Полный расклад Таро")
        context.user_data['state'] = PAYMENT
        return PAYMENT

    if data == "show_sub_details":
        full_price = int(await db.get_setting('price_full', str(PRICE_FULL)))
        sub_price = int(await db.get_setting('price_subscription', str(PRICE_SUBSCRIPTION_EXTRA)))
        sub_price_month = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))
        text = await get_subscription_offer_text(
            db,
            sub_price=sub_price // 100,
            full_price=full_price // 100,
            price=sub_price_month // 100
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Оформить подписку ({sub_price_month//100} ₽)", callback_data="subscribe_confirm")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_from_sub_details")]
        ])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return MAIN

    if data == "back_from_sub_details":
        await query.message.delete()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🔙 Возврат к выбору.",
            reply_markup=None
        )
        return MAIN

    if data == "dismiss_subscription_offer":
        await query.message.delete()
        await query.answer()
        return MAIN

    if data == "offer_subscription":
        price = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))
        await send_invoice(update, context, price, "subscription", "Подписка на месяц")
        context.user_data['state'] = PAYMENT
        context.user_data['payment_type'] = 'subscription'
        return PAYMENT

    if data == "subscribe_confirm":
        price = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))
        await send_invoice(update, context, price, "subscription", "Подписка на месяц")
        context.user_data['state'] = PAYMENT
        context.user_data['payment_type'] = 'subscription'
        return PAYMENT

    if data == "cancel_subscription":
        return await cancel_subscription_start(update, context, query=query)

    if data == "confirm_cancel_subscription":
        await db.cancel_subscription(user_id)
        await query.message.edit_text("✅ Подписка отменена. Доступ к платным функциям сохранится до окончания оплаченного периода.")
        return await subscription_info(update, context)

    if data == "back_to_subscription":
        return await subscription_info(update, context)

    if data == "back_to_profile":
        return await profile(update, context, query=query)

    if data == "profile_subscription":
        return await profile_subscription(update, context, query=query)

    if data == "leave_review":
        await query.message.reply_text("✍️ Напишите ваш отзыв одним сообщением:")
        context.user_data['state'] = AWAITING_FEEDBACK
        return AWAITING_FEEDBACK

    if data == "examples":
        return await examples(update, context)

    if data == "reviews":
        return await reviews(update, context)

    if data == "privacy" or data == "privacy_policy":
        await show_privacy(update, context)
        return ABOUT_SERVICE

    if data == "help":
        return await help_command(update, context)

    if data == "share_reading":
        await query.message.reply_text(
            "📤 Скопируйте текст расклада и поделитесь им с друзьями:\n\n"
            "Я получил(а) глубокий расклад Таро от @YourBot. Хотите тоже попробовать? t.me/YourBot"
        )
        return POST_READING

    if data.startswith("admin_"):
        return await admin_callback_handler(update, context)

    await query.message.reply_text("❌ Неизвестная команда. Попробуйте снова.")
    return MAIN

# ---------- Функции профиля ----------
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Optional[CallbackQuery] = None) -> int:
    if query:
        user_id = query.from_user.id
        reply_method = query.message.reply_text
    else:
        user_id = update.effective_user.id
        reply_method = update.message.reply_text

    db: Database = context.bot_data['db']
    user = await db.get_user(user_id)
    if not user:
        await reply_method("❌ Пользователь не найден.")
        return MAIN

    subscription = await db.get_subscription(user_id)
    has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)
    sub_status = f"✅ Активна до {subscription['subscription_until'].strftime('%d.%m.%Y')}" if has_subscription else "❌ Не активна"

    text = (
        "👤 **Ваш профиль**\n\n"
        f"💎 Подписка: {sub_status}\n"
    )
    await reply_method(text, parse_mode='Markdown', reply_markup=profile_keyboard())
    context.user_data['state'] = PROFILE_MAIN
    return PROFILE_MAIN

async def profile_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Optional[CallbackQuery] = None) -> int:
    if query:
        user_id = query.from_user.id
        reply_method = query.message.reply_text
    else:
        user_id = update.effective_user.id
        reply_method = update.message.reply_text

    db: Database = context.bot_data['db']
    text, keyboard = await get_subscription_status_message(user_id, db)

    await reply_method(text, parse_mode='HTML', reply_markup=keyboard)
    context.user_data['state'] = PROFILE_SUBSCRIPTION
    return PROFILE_SUBSCRIPTION

async def cancel_subscription_start(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Optional[CallbackQuery] = None) -> int:
    if query:
        reply_method = query.message.reply_text
    else:
        reply_method = update.message.reply_text

    await reply_method(
        "⚠️ Вы уверены, что хотите отменить подписку? После отмены доступ к функциям подписки сохранится до окончания оплаченного периода.",
        reply_markup=cancel_subscription_confirm_keyboard()
    )
    context.user_data['state'] = SUBSCRIPTION_CANCEL
    return SUBSCRIPTION_CANCEL

# ---------- Примеры и отзывы ----------
async def examples(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    examples_list = await db.get_examples()
    if not examples_list:
        await safe_reply(update, context, "📭 Примеров пока нет.")
        return MAIN

    for ex in examples_list[:3]:
        text = f"📄 **{ex['title']}**\n\n{ex['content'][:500]}..."
        await safe_reply(update, context, text, parse_mode='Markdown')

    await safe_reply(update, context, "🔮 Хотите получить свой личный расклад?", reply_markup=examples_keyboard())
    context.user_data['state'] = EXAMPLES
    return EXAMPLES

async def reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    reviews_list = await db.get_reviews(approved_only=True)
    if not reviews_list:
        await safe_reply(update, context, "💬 Отзывов пока нет. Станьте первым!")
    else:
        text = "💌 **Отзывы наших клиентов:**\n\n"
        for r in reviews_list[:5]:
            text += f"• {r['text'][:200]}...\n\n"
        await safe_reply(update, context, text, parse_mode='Markdown')

    await safe_reply(update, context, "Хотите оставить свой отзыв?", reply_markup=reviews_keyboard())
    context.user_data['state'] = REVIEWS
    return REVIEWS

# ---------- Обработка отзыва ----------
async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text
    db: Database = context.bot_data['db']

    await db.add_review(user_id, text)
    await safe_reply(update, context, "✅ Спасибо за ваш отзыв! Он будет опубликован после модерации.")
    await safe_reply(update, context, "🏠 Главное меню:", reply_markup=main_menu_keyboard())
    context.user_data['state'] = MAIN
    return MAIN

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    state = context.user_data.get('state', MAIN)

    if state == AWAITING_QUESTION:
        return await handle_question(update, context)

    if state == AWAITING_FEEDBACK:
        return await handle_feedback(update, context)

    if state in (ADMIN_TEXTS_EDIT, ADMIN_PRICES, ADMIN_PROMO_CREATE,
                 ADMIN_SETTINGS_EDIT, ADMIN_BROADCAST, ADMIN_USERS):
        return await admin_input_handler(update, context)

    if state >= ADMIN_MAIN:
        return await admin_text_handler(update, context)

    await safe_reply(update, context, "❓ Используйте кнопки меню для навигации.", reply_markup=main_menu_keyboard())
    return MAIN

# ---------- КОМАНДЫ АДМИНА ----------
async def resetdb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await safe_reply(update, context, "⛔ Недостаточно прав.")
        return MAIN

    context.user_data['confirm_reset'] = True
    await safe_reply(update, context,
        "⚠️ ВНИМАНИЕ! Сброс базы данных удалит ВСЕХ пользователей, сессии, платежи, отзывы, настройки и примеры.\n"
        "Для подтверждения отправьте команду /resetdb_confirm\n"
        "Для отмены ничего не делайте."
    )
    return MAIN

async def resetdb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await safe_reply(update, context, "⛔ Недостаточно прав.")
        return MAIN

    if not context.user_data.get('confirm_reset'):
        await safe_reply(update, context, "Нет запроса на сброс. Используйте /resetdb для начала.")
        return MAIN

    db: Database = context.bot_data['db']
    await safe_reply(update, context, "⚠️ Начинаю сброс базы данных...")
    try:
        async with db.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS users CASCADE")
            await conn.execute("DROP TABLE IF EXISTS payments CASCADE")
            await conn.execute("DROP TABLE IF EXISTS reviews CASCADE")
            await conn.execute("DROP TABLE IF EXISTS settings CASCADE")
            await conn.execute("DROP TABLE IF EXISTS promocodes CASCADE")
            await conn.execute("DROP TABLE IF EXISTS examples CASCADE")
            await db.init_tables()
        await safe_reply(update, context, "✅ База данных успешно сброшена и пересоздана.")
    except Exception as e:
        await safe_reply(update, context, f"❌ Ошибка при сбросе базы: {e}")
    finally:
        context.user_data.pop('confirm_reset', None)
    return MAIN

# ========== АДМИН-ПАНЕЛЬ ==========

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await safe_reply(update, context, "⛔ У вас нет доступа к админ-панели.")
        return MAIN

    await safe_reply(update, context,
        "👑 Добро пожаловать в админ-панель!",
        reply_markup=admin_main_keyboard()
    )
    context.user_data['state'] = ADMIN_MAIN
    return ADMIN_MAIN

async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await safe_reply(update, context, "⛔ Нет доступа.")
        return MAIN

    text = update.message.text
    db: Database = context.bot_data['db']

    if text == "🔙 Выйти из админки":
        await safe_reply(update, context, "🔙 Выход из админ-панели.", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if text == "📊 Статистика":
        stats = await db.get_stats()
        msg = (
            "📊 **Статистика**\n\n"
            f"👥 Всего пользователей: {stats['total_users']}\n"
            f"💳 Всего платежей: {stats['total_payments']}\n"
            f"💰 Доход: {stats['total_revenue']} {CURRENCY}\n"
            f"💎 Активных подписок: {stats['active_subscriptions']}"
        )
        await safe_reply(update, context, msg, parse_mode='Markdown')
        return ADMIN_MAIN

    if text == "💰 Цены и платежи":
        price_full = await db.get_setting('price_full', str(PRICE_FULL))
        price_sub = await db.get_setting('price_subscription', str(PRICE_SUBSCRIPTION_EXTRA))
        sub_price = await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE))
        msg = (
            "💰 **Цены и платежи**\n\n"
            f"Полная цена расклада: {int(price_full)//100} {CURRENCY}\n"
            f"Цена по подписке (дополнительный): {int(price_sub)//100} {CURRENCY}\n"
            f"Цена подписки: {int(sub_price)//100} {CURRENCY}\n\n"
            "Используйте кнопки ниже для изменения:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить полную цену", callback_data="admin_edit_price_full")],
            [InlineKeyboardButton("✏️ Изменить цену по подписке", callback_data="admin_edit_price_sub")],
            [InlineKeyboardButton("✏️ Изменить цену подписки", callback_data="admin_edit_sub_price")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        await safe_reply(update, context, msg, parse_mode='Markdown', reply_markup=keyboard)
        context.user_data['state'] = ADMIN_PRICES
        return ADMIN_PRICES

    if text == "📝 Управление текстами":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Приветствие", callback_data="admin_edit_welcome")],
            [InlineKeyboardButton("✏️ Подписка (оффер)", callback_data="admin_edit_sub_offer")],
            [InlineKeyboardButton("✏️ Помощь", callback_data="admin_edit_help")],
            [InlineKeyboardButton("✏️ Вступление полного расклада", callback_data="admin_edit_full_intro")],
            [InlineKeyboardButton("✏️ Вступление карты дня", callback_data="admin_edit_card_intro")],
            [InlineKeyboardButton("✏️ Промпт для карты дня", callback_data="admin_edit_card_prompt")],
            [InlineKeyboardButton("✏️ Вступление для первого расклада", callback_data="admin_edit_first_free_intro")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        await safe_reply(update, context, "📝 Выберите текст для редактирования:", reply_markup=keyboard)
        context.user_data['state'] = ADMIN_TEXTS
        return ADMIN_TEXTS

    if text == "🎫 Промокоды":
        promos = await db.get_all_promocodes()
        msg = "🎫 **Промокоды**\n\n"
        if promos:
            for p in promos:
                status = "✅" if p['is_active'] else "❌"
                msg += f"{status} {p['code']} — скидка {p['discount_percent']}%, использовано {p['used_count']}/{p['max_uses']}\n"
        else:
            msg += "Нет промокодов.\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Создать промокод", callback_data="admin_create_promo")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        await safe_reply(update, context, msg, parse_mode='Markdown', reply_markup=keyboard)
        context.user_data['state'] = ADMIN_PROMO
        return ADMIN_PROMO

    if text == "📋 Отзывы (модерация)":
        pending = await db.get_pending_reviews()
        if not pending:
            await safe_reply(update, context, "📭 Нет отзывов на модерацию.")
            return ADMIN_MAIN
        for r in pending[:5]:
            user = await db.get_user(r['user_id'])
            name = user['first_name'] if user else "Неизвестно"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_review_{r['id']}")],
                [InlineKeyboardButton("🗑️ Удалить", callback_data=f"admin_delete_review_{r['id']}")]
            ])
            await safe_reply(update, context,
                f"📋 Отзыв от {name}:\n\n{r['text']}",
                reply_markup=keyboard
            )
        await safe_reply(update, context, "📋 Все отзывы обработаны. Для возврата нажмите «Назад».")
        return ADMIN_REVIEWS

    if text == "📢 Рассылка":
        await safe_reply(update, context, "📢 Введите текст сообщения для рассылки всем пользователям:")
        context.user_data['state'] = ADMIN_BROADCAST
        return ADMIN_BROADCAST

    if text == "👥 Пользователи":
        await show_users_list(update, context, page=1)
        return ADMIN_USERS_LIST

    if text == "💎 Подписки":
        active = await db.get_stats()
        await safe_reply(update, context, f"💎 Активных подписок: {active['active_subscriptions']}")
        return ADMIN_MAIN

    if text == "⚙️ Общие настройки":
        use_ai = await db.get_setting('use_ai_welcome', 'true')
        status = "🟢 Включена" if use_ai == 'true' else "🔴 Выключена"
        static_text_preview = await db.get_setting('static_welcome_text', DEFAULT_WELCOME_TEXT)
        if len(static_text_preview) > 200:
            static_text_preview = static_text_preview[:200] + "..."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🤖 AI-генерация вступления: {status}", callback_data="admin_toggle_ai_welcome")],
            [InlineKeyboardButton("✏️ Изменить статичный текст", callback_data="admin_edit_static_welcome")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        await safe_reply(update, context,
            f"⚙️ Настройки вступления для полного расклада:\n\n"
            f"Текущий статичный текст (предпросмотр):\n{static_text_preview}",
            reply_markup=keyboard
        )
        context.user_data['state'] = ADMIN_SETTINGS
        return ADMIN_SETTINGS

    await safe_reply(update, context, "❌ Неизвестная команда.", reply_markup=admin_main_keyboard())
    return ADMIN_MAIN

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    if data == "admin_back":
        await query.message.reply_text("🔙 Возврат в админ-панель.", reply_markup=admin_main_keyboard())
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if data == "admin_home":
        await query.message.reply_text("🏠 Выход в главное меню.", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if data == "admin_cancel":
        await query.message.reply_text("❌ Действие отменено.", reply_markup=admin_main_keyboard())
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    # ----- Обработка изменения цен -----
    if data == "admin_edit_price_full":
        await query.message.reply_text(
            "Введите новую полную цену расклада в копейках (например, 9900 = 99 ₽):"
        )
        context.user_data['admin_action'] = 'set_price_full'
        context.user_data['state'] = ADMIN_PRICES
        return ADMIN_PRICES

    elif data == "admin_edit_price_sub":
        await query.message.reply_text(
            "Введите новую цену дополнительного расклада по подписке в копейках (например, 4900 = 49 ₽):"
        )
        context.user_data['admin_action'] = 'set_price_sub'
        context.user_data['state'] = ADMIN_PRICES
        return ADMIN_PRICES

    elif data == "admin_edit_sub_price":
        await query.message.reply_text(
            "Введите новую цену подписки в копейках (например, 19900 = 199 ₽):"
        )
        context.user_data['admin_action'] = 'set_sub_price'
        context.user_data['state'] = ADMIN_PRICES
        return ADMIN_PRICES

    # ----- Навигация по списку пользователей -----
    if data.startswith("admin_users_page_"):
        page = int(data.split('_')[3])
        await query.message.delete()
        await show_users_list(update, context, page=page)
        return ADMIN_USERS_LIST

    if data == "admin_users_search":
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="👥 Введите ID пользователя для поиска или действия (например, 123456):"
        )
        context.user_data['state'] = ADMIN_USERS
        return ADMIN_USERS

    if data == "admin_users_list":
        await query.message.delete()
        await show_users_list(update, context, page=1)
        return ADMIN_USERS_LIST

    # ---------- ДАРЕНИЕ ПОДПИСКИ ----------
    if data.startswith("admin_gift_subscription_"):
        target_id = int(data.split('_')[3])
        context.user_data['gift_target_id'] = target_id
        user = await db.get_user(target_id)
        name = user['first_name'] if user else "Пользователь"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 месяц", callback_data=f"admin_gift_choose_{target_id}_30")],
            [InlineKeyboardButton("3 месяца", callback_data=f"admin_gift_choose_{target_id}_90")],
            [InlineKeyboardButton("6 месяцев", callback_data=f"admin_gift_choose_{target_id}_180")],
            [InlineKeyboardButton("12 месяцев", callback_data=f"admin_gift_choose_{target_id}_365")],
            [InlineKeyboardButton("Бессрочно", callback_data=f"admin_gift_choose_{target_id}_lifetime")],
            [InlineKeyboardButton("❌ Отмена", callback_data="admin_gift_cancel")]
        ])
        await query.message.edit_text(
            f"Выберите срок подписки для пользователя {name} (ID: {target_id}):",
            reply_markup=keyboard
        )
        return ADMIN_USERS

    if data.startswith("admin_gift_choose_"):
        parts = data.split('_')
        target_id = int(parts[3])
        duration = parts[4]
        context.user_data['gift_duration'] = duration
        user = await db.get_user(target_id)
        name = user['first_name'] if user else "Пользователь"
        duration_text = "бессрочно" if duration == 'lifetime' else f"{duration} дней"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, подарить", callback_data=f"admin_gift_confirm_{target_id}_{duration}")],
            [InlineKeyboardButton("❌ Нет, отмена", callback_data="admin_gift_cancel")]
        ])
        await query.message.edit_text(
            f"Вы действительно хотите подарить подписку на {duration_text} пользователю {name} (ID: {target_id})?",
            reply_markup=keyboard
        )
        return ADMIN_USERS

    if data.startswith("admin_gift_confirm_"):
        parts = data.split('_')
        target_id = int(parts[3])
        duration = parts[4]
        try:
            if duration == 'lifetime':
                new_until = await db.gift_subscription(target_id, lifetime=True)
                duration_text = "бессрочно"
            else:
                days = int(duration)
                new_until = await db.gift_subscription(target_id, duration_days=days)
                duration_text = f"{days} дней"
            await query.message.edit_text(
                f"✅ Подписка успешно подарена пользователю {target_id} на {duration_text}. Новая дата окончания: {new_until.strftime('%d.%m.%Y') if new_until else 'бессрочно'}"
            )
            try:
                await context.bot.send_message(
                    target_id,
                    f"🎁 Вам подарили подписку «Таро-проводник» на {duration_text}! Теперь у вас есть 1 бесплатный расклад каждый день и сниженная цена на дополнительные. Спасибо, что с нами!"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю {target_id}: {e}")
                await query.message.reply_text("⚠️ Пользователь не получил уведомление, но подписка активирована.")
        except Exception as e:
            await query.message.edit_text(f"❌ Ошибка при дарении подписки: {e}")
        finally:
            context.user_data.pop('gift_target_id', None)
            context.user_data.pop('gift_duration', None)
        return ADMIN_MAIN

    if data == "admin_gift_cancel":
        context.user_data.pop('gift_target_id', None)
        context.user_data.pop('gift_duration', None)
        await query.message.edit_text("❌ Дарение подписки отменено.")
        return ADMIN_MAIN

    # ---------- Редактирование текстовых настроек ----------
    if data.startswith("admin_edit_"):
        key_map = {
            "admin_edit_welcome": "welcome_text",
            "admin_edit_sub_offer": "subscription_offer_text",
            "admin_edit_help": "help_text",
            "admin_edit_full_intro": "full_reading_intro_text",
            "admin_edit_card_intro": "card_of_day_intro_text",
            "admin_edit_card_prompt": "card_of_day_prompt",
            "admin_edit_first_free_intro": "first_free_reading_intro"
        }
        key = key_map.get(data)
        if key:
            current = await db.get_setting(key, '')
            await query.message.reply_text(
                f"📝 Текущий текст:\n\n{current}\n\nОтправьте новый текст одним сообщением. Для отмены введите /cancel"
            )
            context.user_data['admin_edit_key'] = key
            context.user_data['state'] = ADMIN_TEXTS_EDIT
            return ADMIN_TEXTS_EDIT

    # ---------- Настройки вступления ----------
    if data == "admin_toggle_ai_welcome":
        current = await db.get_setting('use_ai_welcome', 'true')
        new_value = 'false' if current == 'true' else 'true'
        await db.set_setting('use_ai_welcome', new_value)
        status = "включена" if new_value == 'true' else "выключена"
        status_display = "🟢 Включена" if new_value == 'true' else "🔴 Выключена"
        static_text_preview = await db.get_setting('static_welcome_text', DEFAULT_WELCOME_TEXT)
        if len(static_text_preview) > 200:
            static_text_preview = static_text_preview[:200] + "..."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🤖 AI-генерация вступления: {status_display}", callback_data="admin_toggle_ai_welcome")],
            [InlineKeyboardButton("✏️ Изменить статичный текст", callback_data="admin_edit_static_welcome")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        await query.message.edit_text(
            f"⚙️ Настройки вступления для полного расклада:\n\n"
            f"AI-генерация: {status_display}\n\n"
            f"Текущий статичный текст (предпросмотр):\n{static_text_preview}",
            reply_markup=keyboard
        )
        return ADMIN_SETTINGS

    if data == "admin_edit_static_welcome":
        current = await db.get_setting('static_welcome_text', DEFAULT_WELCOME_TEXT)
        await query.message.reply_text(
            f"📝 Текущий статичный текст вступления:\n\n{current}\n\nОтправьте новый текст одним сообщением. Для отмены введите /cancel"
        )
        context.user_data['admin_edit_key'] = 'static_welcome_text'
        context.user_data['state'] = ADMIN_TEXTS_EDIT
        return ADMIN_TEXTS_EDIT

    if data == "admin_create_promo":
        await query.message.reply_text("Введите код промокода (латиница, цифры, без пробелов):")
        context.user_data['admin_action'] = 'promo_code'
        context.user_data['state'] = ADMIN_PROMO_CREATE
        return ADMIN_PROMO_CREATE

    if data.startswith("admin_approve_review_"):
        review_id = int(data.split('_')[3])
        await db.approve_review(review_id)
        await query.message.edit_text("✅ Отзыв одобрен и опубликован.")
        return ADMIN_REVIEWS

    if data.startswith("admin_delete_review_"):
        review_id = int(data.split('_')[3])
        await db.delete_review(review_id)
        await query.message.edit_text("🗑️ Отзыв удалён.")
        return ADMIN_REVIEWS

    if data.startswith("admin_ban_"):
        target_id = int(data.split('_')[2])
        await db.ban_user(target_id, "Забанен администратором")
        await query.message.edit_text(f"🚫 Пользователь {target_id} забанен.")
        return ADMIN_USERS_LIST if context.user_data.get('state') == ADMIN_USERS_LIST else ADMIN_MAIN

    if data.startswith("admin_unban_"):
        target_id = int(data.split('_')[2])
        await db.unban_user(target_id)
        await query.message.edit_text(f"✅ Пользователь {target_id} разбанен.")
        return ADMIN_USERS_LIST if context.user_data.get('state') == ADMIN_USERS_LIST else ADMIN_MAIN

    await query.message.reply_text("❌ Неизвестная команда.")
    return ADMIN_MAIN

async def admin_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await safe_reply(update, context, "⛔ Нет доступа.")
        return MAIN

    text = update.message.text
    db: Database = context.bot_data['db']
    state = context.user_data.get('state')
    action = context.user_data.get('admin_action')

    if state == ADMIN_BROADCAST:
        users = await db.get_all_users()
        success = 0
        for uid in users:
            try:
                await context.bot.send_message(uid, text)
                success += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Ошибка отправки пользователю {uid}: {e}")
        await safe_reply(update, context, f"✅ Рассылка завершена. Отправлено: {success} пользователям.")
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_USERS:
        try:
            target_id = int(text)
            user = await db.get_user(target_id)
            if not user:
                await safe_reply(update, context, "❌ Пользователь не найден.")
                return ADMIN_MAIN
            subscription = await db.get_subscription(target_id)
            has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)
            msg = (
                f"👤 Пользователь {target_id}\n"
                f"Имя: {user['first_name']} {user['last_name'] or ''}\n"
                f"Подписка: {'активна' if has_subscription else 'не активна'}\n"
                f"Забанен: {'да' if user['is_banned'] else 'нет'}\n"
                f"Бесплатный ознакомительный использован: {'да' if user['free_trial_used'] else 'нет'}\n"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Забанить", callback_data=f"admin_ban_{target_id}")],
                [InlineKeyboardButton("✅ Разбанить", callback_data=f"admin_unban_{target_id}")],
                [InlineKeyboardButton("🎁 Подарить подписку", callback_data=f"admin_gift_subscription_{target_id}")],
                [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
                [InlineKeyboardButton("📋 К списку пользователей", callback_data="admin_users_list")]
            ])
            await safe_reply(update, context, msg, reply_markup=keyboard)
        except ValueError:
            await safe_reply(update, context, "❌ Введите корректный числовой ID.")
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_TEXTS_EDIT:
        key = context.user_data.get('admin_edit_key')
        if key:
            await db.set_setting(key, text)
            await safe_reply(update, context, f"✅ Текст для '{key}' успешно обновлён.")
            context.user_data.pop('admin_edit_key', None)
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_PRICES:
        try:
            value = int(text)
            if action == 'set_price_full':
                await db.set_setting('price_full', str(value))
                await safe_reply(update, context, f"✅ Полная цена расклада изменена на {value//100} {CURRENCY}.")
            elif action == 'set_price_sub':
                await db.set_setting('price_subscription', str(value))
                await safe_reply(update, context, f"✅ Цена дополнительного расклада по подписке изменена на {value//100} {CURRENCY}.")
            elif action == 'set_sub_price':
                await db.set_setting('subscription_price', str(value))
                await safe_reply(update, context, f"✅ Цена подписки изменена на {value//100} {CURRENCY}.")
            else:
                await safe_reply(update, context, "❌ Неизвестное действие.")
        except ValueError:
            await safe_reply(update, context, "❌ Введите число.")
        context.user_data.pop('admin_action', None)
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_PROMO_CREATE:
        if action == 'promo_code':
            context.user_data['promo_code'] = text.upper().strip()
            await safe_reply(update, context, "Введите процент скидки (только число, например 20):")
            context.user_data['admin_action'] = 'promo_discount'
            return ADMIN_PROMO_CREATE
        elif action == 'promo_discount':
            try:
                discount = int(text)
                context.user_data['promo_discount'] = discount
                await safe_reply(update, context, "Введите максимальное количество использований (например, 10):")
                context.user_data['admin_action'] = 'promo_max_uses'
                return ADMIN_PROMO_CREATE
            except ValueError:
                await safe_reply(update, context, "❌ Введите число.")
                return ADMIN_PROMO_CREATE
        elif action == 'promo_max_uses':
            try:
                max_uses = int(text)
                context.user_data['promo_max_uses'] = max_uses
                await safe_reply(update, context, "Введите дату окончания в формате ДД.ММ.ГГГГ (или 'бессрочно'):")
                context.user_data['admin_action'] = 'promo_expires'
                return ADMIN_PROMO_CREATE
            except ValueError:
                await safe_reply(update, context, "❌ Введите число.")
                return ADMIN_PROMO_CREATE
        elif action == 'promo_expires':
            code = context.user_data.get('promo_code')
            discount = context.user_data.get('promo_discount')
            max_uses = context.user_data.get('promo_max_uses')
            if text.lower() == 'бессрочно':
                expires_at = None
            else:
                try:
                    expires_at = datetime.strptime(text, "%d.%m.%Y")
                except ValueError:
                    await safe_reply(update, context, "❌ Неверный формат. Используйте ДД.ММ.ГГГГ или 'бессрочно'.")
                    return ADMIN_PROMO_CREATE
            if code and discount is not None and max_uses is not None:
                success = await db.create_promocode(code, discount, expires_at, max_uses)
                if success:
                    await safe_reply(update, context, f"✅ Промокод {code} успешно создан!")
                else:
                    await safe_reply(update, context, "❌ Промокод с таким кодом уже существует.")
                context.user_data.pop('promo_code', None)
                context.user_data.pop('promo_discount', None)
                context.user_data.pop('promo_max_uses', None)
                context.user_data.pop('admin_action', None)
                context.user_data['state'] = ADMIN_MAIN
                return ADMIN_MAIN
            else:
                await safe_reply(update, context, "❌ Ошибка: не все данные заполнены. Начните заново.")
                context.user_data['state'] = ADMIN_MAIN
                return ADMIN_MAIN

    await safe_reply(update, context, "❌ Неизвестная команда. Возврат в админ-панель.", reply_markup=admin_main_keyboard())
    context.user_data['state'] = ADMIN_MAIN
    return ADMIN_MAIN

# ================================================================
#  НОВЫЙ ФУНКЦИОНАЛ: КОМАНДА /taro ДЛЯ ГРУППОВЫХ ЧАТОВ
# ================================================================

async def taro_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /taro в групповом чате.
    Отвечает реплаем на сообщение пользователя с кнопкой «Достать карту».
    """
    user = update.effective_user
    if not user:
        return

    name = (user.first_name or "друг").strip()[:30]

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🃏 Достать карту", callback_data="taro_draw")
    ]])

    try:
        await update.message.reply_text(
            f"{name}, загадай свой вопрос и нажми кнопку ниже ✨",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения /taro: {e}", exc_info=True)

async def taro_draw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик нажатия кнопки «Достать карту».
    Доступен только автору команды /taro.
    Отправляет фото карты и трактовку реплаем на /taro.
    Удаляет сообщение с кнопкой.
    """
    query = update.callback_query

    if not query or not query.message:
        return

    # Получаем исходное сообщение с командой /taro (на него был сделан reply)
    original = query.message.reply_to_message

    if not original or not original.from_user:
        await query.answer(
            "Не удалось определить автора. Попробуй /taro заново.",
            show_alert=True
        )
        return

    # Проверка: только автор команды может нажать кнопку
    if query.from_user.id != original.from_user.id:
        await query.answer(
            "Эта кнопка не для тебя 😉\nНапиши /taro, чтобы получить свою карту.",
            show_alert=True
        )
        return

    # Убираем «часики» на кнопке
    await query.answer()

    if not CARD_MEANINGS:
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Трактовки карт не загружены. Сообщите администратору.",
                reply_to_message_id=original.message_id
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения об отсутствии трактовок: {e}")
        return

    # Выбираем случайную карту
    card_eng = random.choice(list(CARD_MEANINGS.keys()))
    card_data = CARD_MEANINGS[card_eng]
    name = (query.from_user.first_name or "друг").strip()[:30]
    chat_id = query.message.chat_id
    reply_to = original.message_id

    # Получаем file_id изображения карты
    card_images_data = context.bot_data.get('card_images', {})
    file_id = card_images_data.get(card_eng)

    # 1) Отправляем ФОТО реплаем на /taro
    if file_id:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=f"{name}, вот твоя карта 🃏",
                reply_to_message_id=reply_to
            )
        except Exception as e:
            logger.error(f"Ошибка отправки фото карты {card_eng}: {e}", exc_info=True)

    # 2) Отправляем ТРАКТОВКУ реплаем на /taro
    title = card_data.get('title', card_eng)
    text_body = card_data.get('text', '')

    full_text = f"{name}, твоя карта — {title}\n\n{text_body}"

    # Разбиваем на части по 4096 символов (лимит Telegram)
    for i in range(0, len(full_text), 4096):
        chunk = full_text[i:i+4096]
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=reply_to
                # без parse_mode — на случай спецсимволов в трактовке
            )
        except Exception as e:
            logger.error(f"Ошибка отправки трактовки карты {card_eng}: {e}", exc_info=True)
            break

    # 3) Удаляем сообщение с кнопкой
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение с кнопкой /taro: {e}")

# ========== ERROR HANDLER ==========
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует ошибки и отправляет пользователю понятное сообщение."""
    logger.error("Исключение при обработке апдейта:", exc_info=context.error)
    try:
        if isinstance(update, Update):
            if update.effective_chat:
                # Не спамим ошибками в групповые чаты — только в личку
                if update.effective_chat.type == ChatType.PRIVATE:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="❌ Произошла ошибка. Попробуйте ещё раз или начните заново с команды /start."
                    )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

# ========== НАСТРОЙКА МЕНЮ КОМАНД TELEGRAM ==========
async def post_init(application: Application) -> None:
    """
    Настраивает видимость команд в меню Telegram:
    - default — базовый набор (страховка);
    - в личке — /start, /help;
    - в группах — только /taro.
    """
    # 1. Default (страховка)
    try:
        await application.bot.set_my_commands(
            commands=[
                BotCommand("start", "Запустить бота"),
                BotCommand("help", "Помощь"),
            ],
            scope=BotCommandScopeDefault()
        )
        logger.info("✅ Default-команды установлены")
    except Exception as e:
        logger.error(f"❌ Ошибка Default-команд: {e}", exc_info=True)

    # 2. Личка
    try:
        await application.bot.set_my_commands(
            commands=[
                BotCommand("start", "Запустить бота"),
                BotCommand("help", "Помощь"),
            ],
            scope=BotCommandScopeAllPrivateChats()
        )
        logger.info("✅ Private-команды установлены")
    except Exception as e:
        logger.error(f"❌ Ошибка Private-команд: {e}", exc_info=True)

    # 3. Группы
    try:
        await application.bot.set_my_commands(
            commands=[
                BotCommand("taro", "Достать карту Таро"),
            ],
            scope=BotCommandScopeAllGroupChats()
        )
        logger.info("✅ Group-команды установлены")
    except Exception as e:
        logger.error(f"❌ Ошибка Group-команд: {e}", exc_info=True)

# ========== ЗАПУСК ==========
async def main() -> None:
    # ===== ДИАГНОСТИКА: убедимся, что запускается новый файл =====
    print("=" * 60)
    print("!!! ЗАПУЩЕН НОВЫЙ BOT.PY v9.2 !!!")
    print("=" * 60)
    logger.info("!!! ЗАПУЩЕН НОВЫЙ BOT.PY v9.2 !!!")

    db = Database(DATABASE_URL)
    await db.connect()
    await db.init_tables()

    card_images = load_card_images()
    translations = load_translations()
    tarot_cards = list(translations.keys())

    translation_patterns, normalized_to_canonical = build_translation_patterns_and_mapping(translations)
    number_map = build_number_map(translations)
    num_to_eng = build_number_to_eng(translations)

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.bot_data['db'] = db
    application.bot_data['card_images'] = card_images
    application.bot_data['translations'] = translations
    application.bot_data['tarot_cards'] = tarot_cards
    application.bot_data['translation_patterns'] = translation_patterns
    application.bot_data['normalized_to_canonical'] = normalized_to_canonical
    application.bot_data['number_map'] = number_map
    application.bot_data['num_to_eng'] = num_to_eng
    application.bot_data['major_arcana_eng'] = MAJOR_ARCANA_ENG

    # --- Пользовательские команды (только в личке) ---
    application.add_handler(CommandHandler(
        "start", start, filters=filters.ChatType.PRIVATE
    ))
    application.add_handler(CommandHandler(
        "help", help_command, filters=filters.ChatType.PRIVATE
    ))

    # --- Команда /taro для групп (только в группах) ---
    application.add_handler(CommandHandler(
        "taro", taro_command, filters=filters.ChatType.GROUPS
    ))

    # --- Админские команды (только в личке) ---
    application.add_handler(CommandHandler(
        "admin", admin_panel, filters=filters.ChatType.PRIVATE
    ))
    application.add_handler(CommandHandler(
        "resetdb", resetdb, filters=filters.ChatType.PRIVATE
    ))
    application.add_handler(CommandHandler(
        "resetdb_confirm", resetdb_confirm, filters=filters.ChatType.PRIVATE
    ))

    # --- Reply-кнопки главного меню (ТОЛЬКО в личке) ---
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text("🃏 Карта дня"), card_of_day_intro
    ))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text("🔮 Полный расклад"), full_reading
    ))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text("👤 Мой профиль"), profile
    ))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text("💎 Подписка"), subscription_info
    ))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text("ℹ️ О сервисе"), about_service
    ))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text("❓ Помощь"), help_command
    ))

    # --- Текстовый обработчик (только в личке) ---
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        handle_text
    ))

    # --- Обработчик кнопки /taro. Регистрируется ДО общего callback ---
    application.add_handler(CallbackQueryHandler(taro_draw, pattern="^taro_draw$"))

    # --- Общий callback-обработчик (всё, кроме admin_) ---
    application.add_handler(CallbackQueryHandler(callback_handler, pattern="^(?!admin_)"))

    # --- Платежи (только в личке) ---
    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.SUCCESSFUL_PAYMENT, successful_payment
    ))

    # --- Админские callback'и (только в личке) ---
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))

    # --- Error handler ---
    application.add_error_handler(error_handler)

    # ===== ИНИЦИАЛИЗАЦИЯ =====
    await application.initialize()

    # ===== ЯВНЫЙ вызов post_init — сразу после initialize, до start =====
    logger.info(">>> Вызываю post_init вручную...")
    await post_init(application)
    logger.info(">>> post_init завершён")

    await application.start()
    await application.updater.start_polling()
    logger.info("Бот запущен и готов к работе")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()

    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    await db.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")