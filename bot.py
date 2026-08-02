#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот для гадания на Таро.
Версия 7.0 – полная монетизация с подпиской, вступительным сообщением,
фиксацией использования только после успешного расклада и предложением подписки.
"""

import asyncio
import os
import sys
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
    CallbackQuery, constants
)
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
PRICE_FULL = int(os.getenv("PRICE_FULL", 10000))                 # 100 руб в копейках
PRICE_SUBSCRIPTION_EXTRA = int(os.getenv("PRICE_SUBSCRIPTION_EXTRA", 5000))  # 50 руб
SUBSCRIPTION_PRICE = int(os.getenv("SUBSCRIPTION_PRICE", 49900))  # 499 руб в копейках
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

# ---------- ПРОМПТ ПО УМОЛЧАНИЮ ДЛЯ КАРТЫ ДНЯ ----------
DEFAULT_CARD_OF_DAY_PROMPT = """
Ты — профессиональный мудрый таролог. Человек вытягивает карту дня и ты её трактуешь. Сегодня выпала карта {card_name} ({card_eng}). Назови карту и трактуй карту так как принято в классическом таро, не бойся негативных карт, делай профессиональную трактовку, что эта карта говорит о сегодняшнем дне. 
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
                    subscription_type TEXT DEFAULT NULL,
                    subscription_until TIMESTAMPTZ DEFAULT NULL,
                    is_banned BOOLEAN DEFAULT FALSE,
                    ban_reason TEXT,
                    subscription_offer_shown BOOLEAN DEFAULT FALSE
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    question TEXT,
                    cards TEXT[],
                    answer TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
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
                'welcome_text': '🌟 Добро пожаловать в мир Таро! Я — ваш проводник. Здесь карты становятся зеркалом вашей души.\n\nЧто вы можете сделать:\n🃏 Получить Карту дня – бесплатный знак от Вселенной.\n🔮 Заказать полный расклад – глубинный ответ на любой вопрос.\n📖 Изучить примеры – чтобы увидеть, как я работаю.\n\nС чего начнём? Нажмите «Карта дня», чтобы получить ваш первый знак.',
                'subscription_offer_text': '💎 **Подписка «Таро-проводник»**\n\nОформите подписку и получите:\n• **1 бесплатный полный расклад каждый день** – без ограничений!\n• **Все последующие расклады в этот же день** – по сниженной цене **50 ₽** (вместо 100 ₽).\n• **Без подписки** – каждый расклад стоит **100 ₽**.\n\nСтоимость подписки: **499 ₽ в месяц**.\n\nВы всегда можете отменить подписку в любой момент в личном кабинете.\n\nХотите попробовать? Нажмите «Оформить подписку».',
                'help_text': '❓ Как пользоваться ботом:\n\n1. Нажмите «Карта дня» для получения знака на день.\n2. Нажмите «Полный расклад» для глубокого ответа на ваш вопрос.\n3. Первый расклад – бесплатный ознакомительный.\n4. Подписка даёт один бесплатный расклад в день и сниженную цену на дополнительные.\n5. Без подписки – платный расклад 100 ₽.\n\nЕсли у вас возникли вопросы, напишите нам: @support_username.',
                'price_full': str(PRICE_FULL),
                'price_subscription': str(PRICE_SUBSCRIPTION_EXTRA),
                'subscription_price': str(SUBSCRIPTION_PRICE),
                'full_reading_intro_text': '🔮 **Полный расклад Таро**\n\nЯ вытяну для вас три карты и сделаю глубокий разбор:\n• **Каждая карта** – её символы, значение, позитивные и теневые аспекты.\n• **Взаимодействие карт** – как они перекликаются и усиливают друг друга.\n• **Синтез** – целостный ответ на ваш вопрос.\n• **Квинтэссенция** – итоговый аркан-совет.\n• **Практический вывод** – как применить это в жизни.\n\nРасклад даст вам ясность и опору для принятия решений. Готовы? Нажмите «Начать расклад».',
                'card_of_day_intro_text': '✨ **Карта дня** – ваш знак от Вселенной.\n\nОдна карта, её энергия, аффирмация и простой шаг на сегодня. Это поможет вам настроиться на день, почувствовать его ритм и заметить важное.\n\nХотите узнать, что приготовил вам день? Нажмите «Достать карту дня».',
                'card_of_day_prompt': DEFAULT_CARD_OF_DAY_PROMPT,
                'first_free_reading_intro': '🔮 **Добро пожаловать в мир Таро!**\n\nЯ — ваш проводник. Я помогу вам заглянуть в глубины вашей души, используя древнюю мудрость карт Таро.\n\n**Что вы получите:**\n• **3 случайные карты** – каждая со своим значением и символизмом.\n• **Глубокий разбор** – я объясню значение каждой карты, их взаимодействие и дам целостный ответ на ваш вопрос.\n• **Квинтэссенцию** – итоговую карту-совет, которая резюмирует расклад.\n• **Практический вывод** – как применить полученные знания в жизни.\n\n**Это ваш первый бесплатный ознакомительный расклад!**\nВоспользуйтесь возможностью познакомиться с сервисом без оплаты.\n\nГотовы? Нажмите «Начать расклад» и задайте свой вопрос.'
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

    # ---------- Сессии ----------
    async def save_session(self, user_id: int, question: str, cards: list, answer: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sessions (user_id, question, cards, answer) VALUES ($1, $2, $3, $4)",
                user_id, question, cards, answer
            )

    async def get_user_sessions(self, user_id: int, limit: int = 10) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, question, cards, answer, created_at FROM sessions WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
                user_id, limit
            )
            return [dict(r) for r in rows]

    async def get_session_by_id(self, session_id: int, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE id = $1 AND user_id = $2",
                session_id, user_id
            )
            return dict(row) if row else None

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
            total_sessions = await conn.fetchval("SELECT COUNT(*) FROM sessions")
            total_payments = await conn.fetchval("SELECT COUNT(*) FROM payments")
            total_revenue = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payments")
            active_subscriptions = await conn.fetchval("SELECT COUNT(*) FROM users WHERE subscription_until > now()")
            return {
                "total_users": total_users,
                "total_sessions": total_sessions,
                "total_payments": total_payments,
                "total_revenue": total_revenue,
                "active_subscriptions": active_subscriptions
            }

    async def get_all_users(self) -> List[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE")
            return [r["user_id"] for r in rows]

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

    intro, cards_section, card_details, rest = parse_ai_response(answer)

    if intro is None:
        if original_cards:
            for card_eng in original_cards:
                rus_name = translations.get(card_eng, card_eng)
                file_id = card_images.get(card_eng)
                if file_id:
                    try:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=rus_name)
                    except Exception as e:
                        logger.error(f"Ошибка отправки фото для {card_eng}: {e}")
                        await update.message.reply_text(f"🃏 {rus_name}")
                else:
                    await update.message.reply_text(f"🃏 {rus_name}")
        translated_answer = translate_card_names(answer, translation_patterns)
        for i in range(0, len(translated_answer), 4096):
            await update.message.reply_text(translated_answer[i:i+4096])
        if original_cards and number_map and num_to_eng:
            quint_eng = calculate_quintessence(original_cards, number_map, num_to_eng)
            rus_quint = translations.get(quint_eng, quint_eng)
            file_id = card_images.get(quint_eng)
            if file_id:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=file_id,
                    caption=f"✨ **Квинтэссенция расклада (рассчитана ботом):** {rus_quint}",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(f"✨ **Квинтэссенция:** {rus_quint}")
        return

    intro_translated = translate_card_names(intro, translation_patterns)
    await update.message.reply_text(intro_translated)

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
        await update.message.reply_text(cards_msg, parse_mode='Markdown')

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
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=rus_name)
                except Exception as e:
                    logger.error(f"Ошибка отправки фото для {card_eng}: {e}")
                    await update.message.reply_text(f"🃏 {rus_name}")
            else:
                await update.message.reply_text(f"🃏 {rus_name}")
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
                    await update.message.reply_text(desc_translated[i:i+4096])
    else:
        for name_in_ai, desc in card_details:
            desc_translated = translate_card_names(desc, translation_patterns)
            for i in range(0, len(desc_translated), 4096):
                await update.message.reply_text(desc_translated[i:i+4096])

    if rest:
        sections = split_rest_sections(rest)
        if sections.get('synthesis'):
            await update.message.reply_text(translate_card_names(sections['synthesis'], translation_patterns))
        if sections.get('quintessence'):
            quint_text = sections['quintessence']
            await update.message.reply_text(translate_card_names(quint_text, translation_patterns))
            card_name = extract_quintessence_card_name(quint_text, translations)
            if card_name:
                canonical_quint = canonicalize_card_name(card_name, normalized_to_canonical)
                file_id = card_images.get(canonical_quint)
                if file_id:
                    rus_name = translations.get(canonical_quint, card_name)
                    try:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=rus_name)
                    except Exception as e:
                        logger.error(f"Ошибка отправки фото для квинтэссенции {canonical_quint}: {e}")
        if sections.get('general_answer'):
            await update.message.reply_text(translate_card_names(sections['general_answer'], translation_patterns))
        if sections.get('extra'):
            await update.message.reply_text(translate_card_names(sections['extra'], translation_patterns))

# ========== КЛАВИАТУРЫ ==========
def main_menu_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🃏 Карта дня"), KeyboardButton("🔮 Полный расклад")],
        [KeyboardButton("📄 Примеры"), KeyboardButton("💬 Отзывы")],
        [KeyboardButton("👤 Мой профиль"), KeyboardButton("❓ Помощь")]
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
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def subscription_offer_keyboard(back_callback: str = "back"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оформить подписку", callback_data="subscribe_confirm")],
        [InlineKeyboardButton("🔙 Назад", callback_data=back_callback)],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="home")]
    ])

def reading_actions_keyboard(has_subscription: bool = False):
    buttons = []
    if not has_subscription:
        buttons.append([InlineKeyboardButton("💎 Оформить подписку", callback_data="offer_subscription")])
    buttons.append([InlineKeyboardButton("📤 Поделиться", callback_data="share_reading")])
    buttons.append([InlineKeyboardButton("📜 Мои расклады", callback_data="my_history")])
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    buttons.append([InlineKeyboardButton("🏠 В главное меню", callback_data="home")])
    return InlineKeyboardMarkup(buttons)

def profile_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 История", callback_data="profile_history")],
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
PROFILE_HISTORY = 9
PROFILE_SUBSCRIPTION = 10
SUBSCRIPTION_OFFER = 11
SUBSCRIPTION_CANCEL = 12
PAYMENT = 13
EXAMPLES = 14
REVIEWS = 15
HELP = 16

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
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai.ChatCompletion.create,
                model="deepseek-chat",
                messages=[{"role": "user", "content": "Ты — таролог. Предложи пользователю задать вопрос. Объясни, что чем подробнее вопрос, тем точнее будет расклад. Не используй Markdown."}],
                max_tokens=300,
                temperature=0.9
            ),
            timeout=30
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Ошибка генерации приветствия: {e}")
        return "Задайте свой вопрос как можно подробнее. Я вытяну для вас карты и дам глубокий разбор."

async def ask_ai(question: str, history: list, cards: list = None) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history:
        messages.append(msg)
    
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
    await asyncio.sleep(2)
    for i in range(1, len(messages)):
        await msg.edit_text(messages[i])
        await asyncio.sleep(2)
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
                max_tokens=700,
                temperature=0.9
            ),
            timeout=40
        )
        text = response.choices[0].message.content.strip()
        return {"full_text": text}
    except Exception as e:
        logger.error(f"Ошибка генерации карты дня: {e}")
        return {"full_text": "Сегодняшняя карта приносит вам тихую мудрость. Прислушайтесь к себе и доверьтесь своему внутреннему голосу."}

# ---------- ФУНКЦИЯ ОТПРАВКИ ИНВОЙСА ----------
async def send_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int, payload: str, description: str = None) -> None:
    chat_id = update.effective_chat.id
    if not PAYMENT_PROVIDER_TOKEN or ':' not in PAYMENT_PROVIDER_TOKEN:
        await context.bot.send_message(chat_id, "❌ Платёжная система временно недоступна. Попробуйте позже.")
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

    # Устанавливаем состояние для вопроса
    context.user_data['state'] = AWAITING_QUESTION
    context.user_data['user_id'] = user_id
    # Если это была подписка – обновляем статус
    if payload == "subscription":
        # Определим дату окончания (1 месяц)
        until = datetime.now(timezone.utc) + timedelta(days=30)
        await db.set_subscription(user_id, "Таро-проводник", until)
        await update.message.reply_text(
            "✅ Оплата подписки прошла успешно! Теперь у вас есть 1 бесплатный расклад каждый день и сниженная цена на дополнительные.\n\n"
            "Задайте ваш вопрос, чтобы получить расклад."
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
        await update.message.reply_text("🚫 Ваш доступ к боту ограничен.")
        return MAIN

    # Сбрасываем состояние, если было прервано
    context.user_data.clear()
    context.user_data['state'] = MAIN

    welcome_text = await db.get_setting('welcome_text')
    if not welcome_text:
        welcome_text = "🌟 Добро пожаловать в мир Таро! Я — ваш проводник."

    await update.message.reply_text(welcome_text, reply_markup=main_menu_keyboard())
    return MAIN

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    help_text = await db.get_setting('help_text')
    if not help_text:
        help_text = "❓ Как пользоваться ботом:\n\n1. Нажмите «Карта дня» для получения знака на день.\n2. Нажмите «Полный расклад» для глубокого ответа.\n3. Первый расклад – бесплатный ознакомительный.\n4. Подписка даёт один бесплатный расклад в день и сниженную цену на дополнительные.\n5. Без подписки – платный расклад 100 ₽."
    await update.message.reply_text(help_text, reply_markup=help_keyboard())
    context.user_data['state'] = HELP
    return HELP

# ---------- Карта дня ----------
async def card_of_day_intro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    if await db.is_banned(user_id):
        await update.message.reply_text("🚫 Ваш доступ ограничен.")
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
    await update.message.reply_text(intro_text, parse_mode='Markdown', reply_markup=keyboard)
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

    # Проверяем, получал ли пользователь карту дня сегодня (используем last_free_reading_date)
    last_card = await db.get_last_free_reading_date(user_id)
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

    # Записываем дату
    await db.update_last_free_reading_date(user_id, today)

    card_images = context.bot_data.get('card_images', {})
    file_id = card_images.get(card_eng)

    if file_id:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=f"🌅 Ваша карта дня – **{card_rus}**",
            parse_mode='Markdown'
        )
        await context.bot.send_message(chat_id, full_text, reply_markup=card_of_day_keyboard())
    else:
        await context.bot.send_message(
            chat_id,
            f"🌅 Ваша карта дня – **{card_rus}**\n\n{full_text}",
            parse_mode='Markdown',
            reply_markup=card_of_day_keyboard()
        )

    context.user_data['state'] = CARD_OF_DAY
    return CARD_OF_DAY

# ---------- Полный расклад ----------
async def full_reading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    if await db.is_banned(user_id):
        await update.message.reply_text("🚫 Ваш доступ ограничен.")
        return MAIN

    user = await db.get_user(user_id)
    if not user:
        await update.message.reply_text("❌ Пользователь не найден.")
        return MAIN

    # Проверяем подписку
    subscription = await db.get_subscription(user_id)
    has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)

    # Получаем дату последнего бесплатного
    last_free_date = await db.get_last_free_reading_date(user_id)
    today = datetime.now(timezone.utc).date()
    free_trial_used = await db.get_free_trial_used(user_id)

    if has_subscription:
        if last_free_date != today:
            # Бесплатный расклад дня
            await db.update_last_free_reading_date(user_id, today)
            await update.message.reply_text(
                "🔮 Вы сегодня ещё не пользовались бесплатным раскладом по подписке. "
                "Задайте ваш вопрос – он будет бесплатным!"
            )
            context.user_data['state'] = AWAITING_QUESTION
            context.user_data['user_id'] = user_id
            context.user_data['is_free'] = True
            return AWAITING_QUESTION
        else:
            # Дополнительный платный по подписке
            price = int(await db.get_setting('price_subscription', str(PRICE_SUBSCRIPTION_EXTRA)))
            await update.message.reply_text(
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
            # Первый бесплатный – отправляем вступительное сообщение
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
                    "**Это ваш первый бесплатный ознакомительный расклад!**\n"
                    "Воспользуйтесь возможностью познакомиться с сервисом без оплаты.\n\n"
                    "Готовы? Нажмите «Начать расклад» и задайте свой вопрос."
                )
            keyboard = first_free_reading_intro_keyboard()
            await update.message.reply_text(intro_text, parse_mode='Markdown', reply_markup=keyboard)
            context.user_data['state'] = FULL_READING_INTRO
            return FULL_READING_INTRO
        else:
            # Платный расклад
            price = int(await db.get_setting('price_full', str(PRICE_FULL)))
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Оплатить расклад ({price//100} ₽)", callback_data="pay_full")],
                [InlineKeyboardButton("💎 Подробнее о подписке", callback_data="show_sub_details")]
            ])
            await update.message.reply_text(
                f"💳 Ваш бесплатный ознакомительный расклад уже использован.\n"
                f"Стоимость полного расклада – **{price//100} ₽**.\n\n"
                f"Оформите подписку и получайте **1 бесплатный расклад каждый день** и **скидку 50%** на все дополнительные расклады!",
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
        await update.message.reply_text("❌ Нет активной сессии. Нажмите «Полный расклад».")
        return MAIN

    if await db.is_banned(user_id):
        await update.message.reply_text("🚫 Ваш доступ ограничен.")
        return MAIN

    await show_animation(update, context)

    cards = await get_random_cards(context, 3)
    if not cards:
        await update.message.reply_text("❌ Колода карт не загружена. Попробуйте позже.")
        return MAIN

    answer = await ask_ai(question, [], cards)
    await db.save_session(user_id, question, cards, answer)
    await send_ai_response(update, context, answer, original_cards=cards)

    # Проверяем, был ли это бесплатный ознакомительный расклад
    is_free_trial = context.user_data.get('is_free_trial', False)
    if is_free_trial:
        # Обновляем флаг использования
        await db.update_free_trial_used(user_id)
        # Проверяем, показывали ли уже предложение подписки
        offer_shown = await db.get_subscription_offer_shown(user_id)
        if not offer_shown:
            await db.update_subscription_offer_shown(user_id, True)
            # Отправляем обычное меню после расклада
            subscription = await db.get_subscription(user_id)
            has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)
            await update.message.reply_text(
                "✨ Ваш расклад готов. Что дальше?",
                reply_markup=reading_actions_keyboard(has_subscription)
            )
            # Отправляем отдельное сообщение с подпиской
            await show_subscription_offer_separate(update, context)
        else:
            # Просто обычное меню
            subscription = await db.get_subscription(user_id)
            has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)
            await update.message.reply_text(
                "✨ Ваш расклад готов. Что дальше?",
                reply_markup=reading_actions_keyboard(has_subscription)
            )
        context.user_data.pop('is_free_trial', None)
    else:
        # Обычный расклад (платный или по подписке)
        subscription = await db.get_subscription(user_id)
        has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)
        await update.message.reply_text(
            "✨ Ваш расклад готов. Что дальше?",
            reply_markup=reading_actions_keyboard(has_subscription)
        )

    context.user_data['state'] = POST_READING
    return POST_READING

# ---------- Отдельное сообщение с предложением подписки ----------
async def show_subscription_offer_separate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = context.bot_data['db']
    price = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))
    text = await db.get_setting('subscription_offer_text')
    if not text:
        text = "💎 **Подписка «Таро-проводник»**\n\nОформите подписку и получите:\n• **1 бесплатный полный расклад каждый день** – без ограничений!\n• **Все последующие расклады в этот же день** – по сниженной цене **50 ₽** (вместо 100 ₽).\n• **Без подписки** – каждый расклад стоит **100 ₽**.\n\nСтоимость подписки: **499 ₽ в месяц**.\n\nВы всегда можете отменить подписку в любой момент в личном кабинете.\n\nХотите попробовать? Нажмите «Оформить подписку»."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Оформить подписку", callback_data="subscribe_confirm")],
        [InlineKeyboardButton("⏭️ Позже", callback_data="dismiss_subscription_offer")]
    ])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text.format(price=price//100),
        parse_mode='Markdown',
        reply_markup=keyboard
    )

# ---------- Обработчики callback ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    if data == "home":
        # Сброс всех флагов, возврат в главное меню
        context.user_data.clear()
        await query.message.reply_text("🏠 Главное меню:", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if data == "back":
        await query.message.reply_text("🔙 Возврат в главное меню:", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if data == "cancel":
        # Отмена действия, сброс флагов
        context.user_data.clear()
        await query.message.reply_text("❌ Действие отменено.", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if data == "get_card_of_day":
        await query.message.delete()
        return await card_of_day_execute(update, context, query=query)

    if data == "go_full_reading":
        return await full_reading(update, context)

    # ----- Начало бесплатного ознакомительного расклада -----
    if data == "start_free_reading":
        await query.message.delete()  # удаляем вступительное сообщение
        # Генерируем AI-приветствие
        welcome = await generate_welcome_message()
        if not welcome:
            welcome = "Задайте свой вопрос как можно подробнее. Чем больше деталей, тем точнее будет расклад."
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=welcome
        )
        context.user_data['state'] = AWAITING_QUESTION
        context.user_data['user_id'] = user_id
        context.user_data['is_free_trial'] = True
        return AWAITING_QUESTION

    # ----- Оплата полного расклада -----
    if data == "pay_full":
        price = int(await db.get_setting('price_full', str(PRICE_FULL)))
        await send_invoice(update, context, price, "full_paid", "Полный расклад Таро")
        context.user_data['state'] = PAYMENT
        return PAYMENT

    # ----- Показать подробности подписки -----
    if data == "show_sub_details":
        price = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))
        sub_text = await db.get_setting('subscription_offer_text')
        if not sub_text:
            sub_text = "💎 **Подписка «Таро-проводник»**\n\nВсего за {price} ₽ в месяц вы получаете:\n• **1 бесплатный полный расклад каждый день** – без ограничений!\n• **Все последующие расклады в этот же день** – по сниженной цене **50 ₽** (вместо 100 ₽).\n• **Без подписки** – каждый расклад стоит **100 ₽**.\n\nВы всегда можете отменить подписку в любой момент в личном кабинете.\n\nХотите попробовать? Нажмите «Оформить подписку»."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Оформить подписку ({price//100} ₽)", callback_data="subscribe_confirm")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_from_sub_details")]
        ])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=sub_text.format(price=price//100),
            parse_mode='Markdown',
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

    # ----- Скрыть предложение подписки (кнопка "Позже") -----
    if data == "dismiss_subscription_offer":
        await query.message.delete()
        await query.answer()
        return MAIN

    # ----- Подписка -----
    if data == "offer_subscription":
        return await show_subscription_offer(update, context)

    if data == "subscribe_confirm":
        price = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))
        await send_invoice(update, context, price, "subscription", "Подписка на месяц")
        context.user_data['state'] = PAYMENT
        context.user_data['payment_type'] = 'subscription'
        return PAYMENT

    if data == "cancel_subscription":
        return await cancel_subscription_start(update, context)

    if data == "confirm_cancel_subscription":
        await db.cancel_subscription(user_id)
        await query.message.edit_text("✅ Подписка отменена. Доступ к платным функциям сохранится до окончания оплаченного периода.")
        return await profile(update, context)

    if data == "back_to_subscription":
        return await profile_subscription(update, context)

    if data == "back_to_profile":
        return await profile(update, context)

    if data == "profile_history":
        return await profile_history(update, context)

    if data == "profile_subscription":
        return await profile_subscription(update, context)

    if data == "leave_review":
        await query.message.reply_text("✍️ Напишите ваш отзыв одним сообщением:")
        context.user_data['state'] = AWAITING_FEEDBACK
        return AWAITING_FEEDBACK

    if data == "examples":
        return await examples(update, context)

    if data == "help":
        return await help_command(update, context)

    if data == "share_reading":
        await query.message.reply_text(
            "📤 Скопируйте текст расклада и поделитесь им с друзьями:\n\n"
            "Я получил(а) глубокий расклад Таро от @YourBot. Хотите тоже попробовать? t.me/YourBot"
        )
        return POST_READING

    if data == "my_history":
        return await profile_history(update, context)

    if data.startswith("admin_"):
        return await admin_callback_handler(update, context)

    await query.message.reply_text("❌ Неизвестная команда. Попробуйте снова.")
    return MAIN

# ---------- Профиль ----------
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    user = await db.get_user(user_id)
    if not user:
        await update.message.reply_text("❌ Пользователь не найден.")
        return MAIN

    subscription = await db.get_subscription(user_id)
    has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)
    sub_status = f"✅ Активна до {subscription['subscription_until'].strftime('%d.%m.%Y')}" if has_subscription else "❌ Не активна"

    total_sessions = len(await db.get_user_sessions(user_id, limit=1000))

    text = (
        "👤 **Ваш профиль**\n\n"
        f"🔮 Всего раскладов: {total_sessions}\n"
        f"💎 Подписка: {sub_status}\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=profile_keyboard())
    context.user_data['state'] = PROFILE_MAIN
    return PROFILE_MAIN

async def profile_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    sessions = await db.get_user_sessions(user_id, limit=10)
    if not sessions:
        await update.message.reply_text("📭 У вас пока нет раскладов.")
        return PROFILE_MAIN

    text = "📜 **История ваших раскладов:**\n\n"
    for s in sessions:
        date = s['created_at'].strftime('%d.%m.%Y %H:%M')
        question = s['question'][:50] + "..." if len(s['question']) > 50 else s['question']
        text += f"• {date} — {question}\n"

    await update.message.reply_text(text, parse_mode='Markdown')
    await update.message.reply_text("Для просмотра полного ответа откройте конкретную сессию (пока не реализовано).")
    return PROFILE_MAIN

async def profile_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: Database = context.bot_data['db']

    subscription = await db.get_subscription(user_id)
    has_subscription = subscription and subscription['subscription_until'] > datetime.now(timezone.utc)

    if has_subscription:
        text = f"💎 **Ваша подписка**\n\nАктивна до {subscription['subscription_until'].strftime('%d.%m.%Y')}\nТип: {subscription['subscription_type'] or 'Таро-проводник'}"
    else:
        text = "💎 **Подписка не активна**\n\nОформите подписку для безлимитного доступа к раскладам и эксклюзивным модулям."

    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=subscription_status_keyboard(has_subscription))
    context.user_data['state'] = PROFILE_SUBSCRIPTION
    return PROFILE_SUBSCRIPTION

async def cancel_subscription_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "⚠️ Вы уверены, что хотите отменить подписку? После отмены доступ к функциям подписки сохранится до окончания оплаченного периода.",
        reply_markup=cancel_subscription_confirm_keyboard()
    )
    context.user_data['state'] = SUBSCRIPTION_CANCEL
    return SUBSCRIPTION_CANCEL

async def show_subscription_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    price = int(await db.get_setting('subscription_price', str(SUBSCRIPTION_PRICE)))
    text = await db.get_setting('subscription_offer_text')
    if not text:
        text = "💎 **Подписка «Таро-проводник»**\n\nОформите подписку и получите:\n• **1 бесплатный полный расклад каждый день** – без ограничений!\n• **Все последующие расклады в этот же день** – по сниженной цене **50 ₽** (вместо 100 ₽).\n• **Без подписки** – каждый расклад стоит **100 ₽**.\n\nСтоимость подписки: **499 ₽ в месяц**.\n\nВы всегда можете отменить подписку в любой момент в личном кабинете.\n\nХотите попробовать? Нажмите «Оформить подписку»."
    await update.message.reply_text(
        text.format(price=price//100),
        parse_mode='Markdown',
        reply_markup=subscription_offer_keyboard("back")
    )
    context.user_data['state'] = SUBSCRIPTION_OFFER
    return SUBSCRIPTION_OFFER

async def examples(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    examples_list = await db.get_examples()
    if not examples_list:
        await update.message.reply_text("📭 Примеров пока нет.")
        return MAIN

    for ex in examples_list[:3]:
        text = f"📄 **{ex['title']}**\n\n{ex['content'][:500]}..."
        await update.message.reply_text(text, parse_mode='Markdown')

    await update.message.reply_text("🔮 Хотите получить свой личный расклад?", reply_markup=examples_keyboard())
    context.user_data['state'] = EXAMPLES
    return EXAMPLES

async def reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.bot_data['db']
    reviews_list = await db.get_reviews(approved_only=True)
    if not reviews_list:
        await update.message.reply_text("💬 Отзывов пока нет. Станьте первым!")
    else:
        text = "💌 **Отзывы наших клиентов:**\n\n"
        for r in reviews_list[:5]:
            text += f"• {r['text'][:200]}...\n\n"
        await update.message.reply_text(text, parse_mode='Markdown')

    await update.message.reply_text("Хотите оставить свой отзыв?", reply_markup=reviews_keyboard())
    context.user_data['state'] = REVIEWS
    return REVIEWS

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text
    db: Database = context.bot_data['db']

    await db.add_review(user_id, text)
    await update.message.reply_text("✅ Спасибо за ваш отзыв! Он будет опубликован после модерации.")
    await update.message.reply_text("🏠 Главное меню:", reply_markup=main_menu_keyboard())
    context.user_data['state'] = MAIN
    return MAIN

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    state = context.user_data.get('state', MAIN)

    if state == AWAITING_QUESTION:
        return await handle_question(update, context)

    if state == AWAITING_FEEDBACK:
        return await handle_feedback(update, context)

    # -------- Админские состояния, требующие ввода данных --------
    if state in (ADMIN_TEXTS_EDIT, ADMIN_PRICES, ADMIN_PROMO_CREATE,
                 ADMIN_SETTINGS_EDIT, ADMIN_BROADCAST, ADMIN_USERS):
        return await admin_input_handler(update, context)

    # -------- Остальные состояния админ-панели (кнопки меню) --------
    if state >= ADMIN_MAIN:
        return await admin_text_handler(update, context)

    await update.message.reply_text("❓ Используйте кнопки меню для навигации.", reply_markup=main_menu_keyboard())
    return MAIN

# ---------- КОМАНДЫ АДМИНА ----------
async def resetdb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return MAIN

    context.user_data['confirm_reset'] = True
    await update.message.reply_text(
        "⚠️ ВНИМАНИЕ! Сброс базы данных удалит ВСЕХ пользователей, сессии, платежи, отзывы, настройки и примеры.\n"
        "Для подтверждения отправьте команду /resetdb_confirm\n"
        "Для отмены ничего не делайте."
    )
    return MAIN

async def resetdb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return MAIN

    if not context.user_data.get('confirm_reset'):
        await update.message.reply_text("Нет запроса на сброс. Используйте /resetdb для начала.")
        return MAIN

    db: Database = context.bot_data['db']
    await update.message.reply_text("⚠️ Начинаю сброс базы данных...")
    try:
        async with db.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS users CASCADE")
            await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
            await conn.execute("DROP TABLE IF EXISTS payments CASCADE")
            await conn.execute("DROP TABLE IF EXISTS reviews CASCADE")
            await conn.execute("DROP TABLE IF EXISTS settings CASCADE")
            await conn.execute("DROP TABLE IF EXISTS promocodes CASCADE")
            await conn.execute("DROP TABLE IF EXISTS examples CASCADE")
            await db.init_tables()
        await update.message.reply_text("✅ База данных успешно сброшена и пересоздана.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при сбросе базы: {e}")
    finally:
        context.user_data.pop('confirm_reset', None)
    return MAIN

# ========== АДМИН-ПАНЕЛЬ ==========

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ У вас нет доступа к админ-панели.")
        return MAIN

    await update.message.reply_text(
        "👑 Добро пожаловать в админ-панель!",
        reply_markup=admin_main_keyboard()
    )
    context.user_data['state'] = ADMIN_MAIN
    return ADMIN_MAIN

async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Нет доступа.")
        return MAIN

    text = update.message.text
    db: Database = context.bot_data['db']

    if text == "🔙 Выйти из админки":
        await update.message.reply_text("🔙 Выход из админ-панели.", reply_markup=main_menu_keyboard())
        context.user_data['state'] = MAIN
        return MAIN

    if text == "📊 Статистика":
        stats = await db.get_stats()
        msg = (
            "📊 **Статистика**\n\n"
            f"👥 Всего пользователей: {stats['total_users']}\n"
            f"🔮 Всего сессий: {stats['total_sessions']}\n"
            f"💳 Всего платежей: {stats['total_payments']}\n"
            f"💰 Доход: {stats['total_revenue']} {CURRENCY}\n"
            f"💎 Активных подписок: {stats['active_subscriptions']}"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')
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
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
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
        await update.message.reply_text("📝 Выберите текст для редактирования:", reply_markup=keyboard)
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
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        context.user_data['state'] = ADMIN_PROMO
        return ADMIN_PROMO

    if text == "📋 Отзывы (модерация)":
        pending = await db.get_pending_reviews()
        if not pending:
            await update.message.reply_text("📭 Нет отзывов на модерацию.")
            return ADMIN_MAIN
        for r in pending[:5]:
            user = await db.get_user(r['user_id'])
            name = user['first_name'] if user else "Неизвестно"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_review_{r['id']}")],
                [InlineKeyboardButton("🗑️ Удалить", callback_data=f"admin_delete_review_{r['id']}")]
            ])
            await update.message.reply_text(
                f"📋 Отзыв от {name}:\n\n{r['text']}",
                reply_markup=keyboard
            )
        await update.message.reply_text("📋 Все отзывы обработаны. Для возврата нажмите «Назад».")
        return ADMIN_REVIEWS

    if text == "📢 Рассылка":
        await update.message.reply_text("📢 Введите текст сообщения для рассылки всем пользователям:")
        context.user_data['state'] = ADMIN_BROADCAST
        return ADMIN_BROADCAST

    if text == "👥 Пользователи":
        await update.message.reply_text("👥 Введите ID пользователя для поиска или действия (например, 123456):")
        context.user_data['state'] = ADMIN_USERS
        return ADMIN_USERS

    if text == "💎 Подписки":
        active = await db.get_stats()
        await update.message.reply_text(f"💎 Активных подписок: {active['active_subscriptions']}")
        return ADMIN_MAIN

    if text == "⚙️ Общие настройки":
        await update.message.reply_text("⚙️ Общие настройки пока пусты.")
        return ADMIN_MAIN

    await update.message.reply_text("❌ Неизвестная команда.", reply_markup=admin_main_keyboard())
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

    if data == "admin_edit_price_full":
        await query.message.reply_text("💰 Введите новую полную цену расклада (в копейках, например 10000 за 100 руб):")
        context.user_data['admin_action'] = 'set_price_full'
        context.user_data['state'] = ADMIN_PRICES
        return ADMIN_PRICES

    if data == "admin_edit_price_sub":
        await query.message.reply_text("💰 Введите новую цену дополнительного расклада по подписке (в копейках):")
        context.user_data['admin_action'] = 'set_price_sub'
        context.user_data['state'] = ADMIN_PRICES
        return ADMIN_PRICES

    if data == "admin_edit_sub_price":
        await query.message.reply_text("💰 Введите новую цену подписки (в копейках):")
        context.user_data['admin_action'] = 'set_sub_price'
        context.user_data['state'] = ADMIN_PRICES
        return ADMIN_PRICES

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
        return ADMIN_MAIN

    if data.startswith("admin_unban_"):
        target_id = int(data.split('_')[2])
        await db.unban_user(target_id)
        await query.message.edit_text(f"✅ Пользователь {target_id} разбанен.")
        return ADMIN_MAIN

    await query.message.reply_text("❌ Неизвестная команда.")
    return ADMIN_MAIN

async def admin_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Нет доступа.")
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
        await update.message.reply_text(f"✅ Рассылка завершена. Отправлено: {success} пользователям.")
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_USERS:
        try:
            target_id = int(text)
            user = await db.get_user(target_id)
            if not user:
                await update.message.reply_text("❌ Пользователь не найден.")
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
                [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
            ])
            await update.message.reply_text(msg, reply_markup=keyboard)
        except ValueError:
            await update.message.reply_text("❌ Введите корректный числовой ID.")
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_TEXTS_EDIT:
        key = context.user_data.get('admin_edit_key')
        if key:
            await db.set_setting(key, text)
            await update.message.reply_text(f"✅ Текст для '{key}' успешно обновлён.")
            context.user_data.pop('admin_edit_key', None)
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_PRICES:
        try:
            value = int(text)
            if action == 'set_price_full':
                await db.set_setting('price_full', str(value))
                await update.message.reply_text(f"✅ Полная цена расклада изменена на {value//100} {CURRENCY}.")
            elif action == 'set_price_sub':
                await db.set_setting('price_subscription', str(value))
                await update.message.reply_text(f"✅ Цена дополнительного расклада по подписке изменена на {value//100} {CURRENCY}.")
            elif action == 'set_sub_price':
                await db.set_setting('subscription_price', str(value))
                await update.message.reply_text(f"✅ Цена подписки изменена на {value//100} {CURRENCY}.")
            else:
                await update.message.reply_text("❌ Неизвестное действие.")
        except ValueError:
            await update.message.reply_text("❌ Введите число.")
        context.user_data.pop('admin_action', None)
        context.user_data['state'] = ADMIN_MAIN
        return ADMIN_MAIN

    if state == ADMIN_PROMO_CREATE:
        if action == 'promo_code':
            context.user_data['promo_code'] = text.upper().strip()
            await update.message.reply_text("Введите процент скидки (только число, например 20):")
            context.user_data['admin_action'] = 'promo_discount'
            return ADMIN_PROMO_CREATE
        elif action == 'promo_discount':
            try:
                discount = int(text)
                context.user_data['promo_discount'] = discount
                await update.message.reply_text("Введите максимальное количество использований (например, 10):")
                context.user_data['admin_action'] = 'promo_max_uses'
                return ADMIN_PROMO_CREATE
            except ValueError:
                await update.message.reply_text("❌ Введите число.")
                return ADMIN_PROMO_CREATE
        elif action == 'promo_max_uses':
            try:
                max_uses = int(text)
                context.user_data['promo_max_uses'] = max_uses
                await update.message.reply_text("Введите дату окончания в формате ДД.ММ.ГГГГ (или 'бессрочно'):")
                context.user_data['admin_action'] = 'promo_expires'
                return ADMIN_PROMO_CREATE
            except ValueError:
                await update.message.reply_text("❌ Введите число.")
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
                    await update.message.reply_text("❌ Неверный формат. Используйте ДД.ММ.ГГГГ или 'бессрочно'.")
                    return ADMIN_PROMO_CREATE
            if code and discount is not None and max_uses is not None:
                success = await db.create_promocode(code, discount, expires_at, max_uses)
                if success:
                    await update.message.reply_text(f"✅ Промокод {code} успешно создан!")
                else:
                    await update.message.reply_text("❌ Промокод с таким кодом уже существует.")
                context.user_data.pop('promo_code', None)
                context.user_data.pop('promo_discount', None)
                context.user_data.pop('promo_max_uses', None)
                context.user_data.pop('admin_action', None)
                context.user_data['state'] = ADMIN_MAIN
                return ADMIN_MAIN
            else:
                await update.message.reply_text("❌ Ошибка: не все данные заполнены. Начните заново.")
                context.user_data['state'] = ADMIN_MAIN
                return ADMIN_MAIN

    await update.message.reply_text("❌ Неизвестная команда. Возврат в админ-панель.", reply_markup=admin_main_keyboard())
    context.user_data['state'] = ADMIN_MAIN
    return ADMIN_MAIN

# ========== ЗАПУСК ==========
async def main() -> None:
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("resetdb", resetdb))
    application.add_handler(CommandHandler("resetdb_confirm", resetdb_confirm))

    application.add_handler(MessageHandler(filters.Text("🃏 Карта дня"), card_of_day_intro))
    application.add_handler(MessageHandler(filters.Text("🔮 Полный расклад"), full_reading))
    application.add_handler(MessageHandler(filters.Text("📄 Примеры"), examples))
    application.add_handler(MessageHandler(filters.Text("💬 Отзывы"), reviews))
    application.add_handler(MessageHandler(filters.Text("👤 Мой профиль"), profile))
    application.add_handler(MessageHandler(filters.Text("❓ Помощь"), help_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_handler(CallbackQueryHandler(callback_handler, pattern="^(?!admin_)"))

    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_input_handler))

    await application.initialize()
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
    import signal
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
