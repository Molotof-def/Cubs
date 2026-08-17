import os
import asyncio
import logging
import random
import html
import uuid
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatPermissions,
    PreCheckoutQuery,
    LabeledPrice,
    BotCommand
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("❌ ОШИБКА: Токен бота не найден в переменных окружения (BOT_TOKEN)!")

OWNER_ID_RAW = os.getenv("OWNER_ID", "0")
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else 0

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    exit("❌ ОШИБКА: DATABASE_URL не найден! Добавьте подключение к PostgreSQL.")

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@DuelCubesChannel").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

# Кошелек для приёма GRAM / TON
GRAM_WALLET = os.getenv("GRAM_WALLET", "EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N")

# Прямые CDN-картинки высокой доступности для Telegram
IMG_WIN = "https://i.imgur.com/v8tXgU2.png"
IMG_LOSS = "https://i.imgur.com/K3ZJ3vE.png"
IMG_DRAW = "https://i.imgur.com/2Uf3U9F.png"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Сессии в памяти
active_duels: Dict[str, dict] = {}
active_checks: Dict[str, dict] = {}
active_ladders: Dict[int, dict] = {}
active_clan_invites: Dict[str, dict] = {}
active_withdraws: Dict[str, dict] = {}
active_gram_invoices: Dict[str, dict] = {}
chat_recent_users: Dict[int, List[int]] = {}


def get_mention(user_id: int, name: Optional[str]) -> str:
    safe_name = html.escape(name or "Игрок")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


async def send_game_result(message: Message, photo_url: Optional[str], caption: str, reply_markup=None):
    """Отказоустойчивая отправка результата игры"""
    if photo_url:
        try:
            await message.answer_photo(
                photo=photo_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            return
        except Exception as e:
            logging.warning(f"Ошибка загрузки фото ({e}), переключение на текстовый режим.")

    try:
        await message.answer(text=caption, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Ошибка HTML-парсинга: {e}. Отправка в чистом тексте.")
        clean_text = re.sub(r'<[^>]+>', '', caption)
        await message.answer(text=clean_text, reply_markup=reply_markup)


# ================= ПРОВЕРКА ПОДПИСКИ =================
async def check_subscription(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True

    if not REQUIRED_CHANNEL or REQUIRED_CHANNEL in ["@твой_канал", "none", "", "None"]:
        return True

    chat_target = REQUIRED_CHANNEL
    if not chat_target.startswith("@") and not chat_target.startswith("-100") and not chat_target.startswith("-"):
        chat_target = f"@{chat_target}"

    try:
        member = await bot.get_chat_member(chat_id=chat_target, user_id=user_id)
        status_val = str(member.status).lower().replace("chatmemberstatus.", "")
        
        if status_val in ["creator", "administrator", "member", "owner"]:
            return True
        if status_val == "restricted":
            return getattr(member, "is_member", False)
        return False
    except Exception as e:
        err_msg = str(e).lower()
        if "chat not found" in err_msg or "admin" in err_msg or "not a member" in err_msg or "member list is inaccessible" in err_msg:
            logging.error(f"⚠️ Бот должен быть АДМИНИСТРАТОРОМ канала {chat_target}! Пропуск проверки.")
            return True
        logging.warning(f"Ошибка проверки подписки {user_id}: {e}")
        return False


def sub_keyboard():
    builder = InlineKeyboardBuilder()
    clean_tag = REQUIRED_CHANNEL.replace("@", "")
    channel_url = f"https://t.me/{clean_tag}" if not REQUIRED_CHANNEL.startswith("-100") else "https://t.me/"
    builder.button(text="📢 Подписаться на канал", url=channel_url)
    builder.button(text="🔄 Проверить подписку", callback_data="sub_check_recheck")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data == "sub_check_recheck")
async def cb_recheck_sub(call: CallbackQuery):
    if await check_subscription(call.from_user.id):
        await call.message.edit_text("✅ <b>Подписка подтверждена!</b> Теперь вам доступны все функции.", parse_mode="HTML")
    else:
        await call.answer("❌ Вы ещё не подписались на наш канал!", show_alert=True)


# ================= БАЗА ДАННЫХ (POSTGRESQL) =================
class Database:
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.pool = None

    async def init(self):
        clean_url = self.db_url.replace("postgres://", "postgresql://", 1)
        self.pool = await asyncpg.create_pool(dsn=clean_url)

        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    tg_username TEXT,
                    referrer_id BIGINT DEFAULT NULL,
                    clan_id INT DEFAULT NULL,
                    balance BIGINT DEFAULT 0,
                    turnover BIGINT DEFAULT 0,
                    wins INT DEFAULT 0,
                    losses INT DEFAULT 0,
                    draws INT DEFAULT 0,
                    warns INT DEFAULT 0,
                    has_deposited BOOLEAN DEFAULT FALSE,
                    last_stars_deposit TIMESTAMP DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS clans (
                    clan_id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE,
                    owner_id BIGINT,
                    treasury BIGINT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS withdraw_requests (
                    req_id TEXT PRIMARY KEY,
                    user_id BIGINT,
                    amount BIGINT,
                    stars_amount INT,
                    target_username TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS gram_deposits (
                    invoice_id TEXT PRIMARY KEY,
                    user_id BIGINT,
                    gram_amount NUMERIC,
                    coins_amount BIGINT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS bot_admins (
                    user_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    reward INT,
                    uses_left INT
                );
                CREATE TABLE IF NOT EXISTS promo_history (
                    user_id BIGINT,
                    code TEXT,
                    PRIMARY KEY (user_id, code)
                );
            """)
            await conn.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT DEFAULT NULL;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_username TEXT DEFAULT NULL;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS warns INT DEFAULT 0;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS clan_id INT DEFAULT NULL;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS has_deposited BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_stars_deposit TIMESTAMP DEFAULT NULL;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
                ALTER TABLE withdraw_requests ADD COLUMN IF NOT EXISTS stars_amount INT DEFAULT 0;
                ALTER TABLE withdraw_requests ADD COLUMN IF NOT EXISTS target_username TEXT DEFAULT '';
            """)
            await conn.execute("""
                INSERT INTO promo_codes (code, reward, uses_left) 
                VALUES ('START', 100, 10) 
                ON CONFLICT (code) DO NOTHING;
            """)

    async def register_user(self, user_id: int, username: str, tg_username: Optional[str] = None, referrer_id: Optional[int] = None):
        clean_tag = tg_username.replace("@", "").lower() if tg_username else None
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, tg_username, referrer_id, balance) 
                VALUES ($1, $2, $3, $4, 0)
                ON CONFLICT (user_id) DO UPDATE SET 
                    username = EXCLUDED.username,
                    tg_username = COALESCE(EXCLUDED.tg_username, users.tg_username)
            """, user_id, username, clean_tag, referrer_id)

    async def get_user_id_by_username(self, tg_username: str):
        clean_tag = tg_username.replace("@", "").lower().strip()
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT user_id FROM users WHERE LOWER(tg_username) = $1", clean_tag)

    async def get_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, username, balance, turnover, wins, losses, draws, warns, referrer_id, clan_id, has_deposited, last_stars_deposit, created_at FROM users WHERE user_id = $1",
                user_id
            )
            return list(row) if row else None

    async def change_balance(self, user_id: int, amount: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

    async def add_turnover(self, user_id: int, amount: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET turnover = turnover + $1 WHERE user_id = $2", abs(amount), user_id)

    async def record_game(self, user_id: int, status: str):
        col = "wins" if status == "win" else ("losses" if status == "loss" else "draws")
        async with self.pool.acquire() as conn:
            await conn.execute(f"UPDATE users SET {col} = {col} + 1 WHERE user_id = $1", user_id)

    async def process_referral_loss(self, loser_id: int, lost_amount: int):
        if lost_amount <= 0:
            return
        try:
            async with self.pool.acquire() as conn:
                ref_id = await conn.fetchval("SELECT referrer_id FROM users WHERE user_id = $1", loser_id)
                if ref_id and ref_id != loser_id:
                    reward = max(1, int(lost_amount * 0.03))
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", reward, ref_id)
                    try:
                        await bot.send_message(
                            chat_id=ref_id,
                            text=f"🤝 <b>Реферальный бонус!</b>\nВаш реферал проиграл <code>{lost_amount} 💰</code>. Вам начислено 3%: <b>+{reward} 💰</b>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Ошибка начисления реферальных: {e}")

    # ================= КЛАНЫ =================
    async def create_clan(self, owner_id: int, name: str) -> Optional[int]:
        async with self.pool.acquire() as conn:
            try:
                clan_id = await conn.fetchval(
                    "INSERT INTO clans (name, owner_id, treasury) VALUES ($1, $2, 0) RETURNING clan_id",
                    name, owner_id
                )
                await conn.execute("UPDATE users SET clan_id = $1 WHERE user_id = $2", clan_id, owner_id)
                return clan_id
            except Exception:
                return None

    async def get_clan(self, clan_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT clan_id, name, owner_id, treasury FROM clans WHERE clan_id = $1", clan_id)

    async def get_clan_members_count(self, clan_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users WHERE clan_id = $1", clan_id) or 0

    async def add_to_clan_treasury(self, clan_id: int, amount: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE clans SET treasury = treasury + $1 WHERE clan_id = $2", amount, clan_id)

    async def set_user_clan(self, user_id: int, clan_id: Optional[int]):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET clan_id = $1 WHERE user_id = $2", clan_id, user_id)

    async def delete_clan(self, clan_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET clan_id = NULL WHERE clan_id = $1", clan_id)
            await conn.execute("DELETE FROM clans WHERE clan_id = $1", clan_id)

    async def get_top_clans(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT name, treasury FROM clans ORDER BY treasury DESC LIMIT $1", limit)

    async def get_referrals_count(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", user_id) or 0

    async def get_top(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT username, balance FROM users ORDER BY balance DESC LIMIT $1", limit)

    async def is_admin(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        async with self.pool.acquire() as conn:
            res = await conn.fetchval("SELECT 1 FROM bot_admins WHERE user_id = $1", user_id)
            return res is not None

    async def add_admin(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

    async def add_warn(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET warns = warns + 1 WHERE user_id = $1", user_id)
            res = await conn.fetchval("SELECT warns FROM users WHERE user_id = $1", user_id)
            return res if res is not None else 1

    async def reset_warns(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET warns = 0 WHERE user_id = $1", user_id)

    async def create_promo(self, code: str, reward: int, uses: int) -> bool:
        async with self.pool.acquire() as conn:
            try:
                await conn.execute("INSERT INTO promo_codes (code, reward, uses_left) VALUES ($1, $2, $3)", code.upper(), reward, uses)
                return True
            except Exception:
                return False

    async def activate_promo(self, user_id: int, code: str) -> tuple[bool, str]:
        code = code.upper()
        async with self.pool.acquire() as conn:
            used = await conn.fetchval("SELECT 1 FROM promo_history WHERE user_id = $1 AND code = $2", user_id, code)
            if used:
                return False, "❌ Вы уже активировали этот промокод!"

            promo = await conn.fetchrow("SELECT reward, uses_left FROM promo_codes WHERE code = $1", code)
            if not promo:
                return False, "❌ Промокод не найден!"

            reward, uses_left = promo["reward"], promo["uses_left"]
            if uses_left <= 0:
                return False, "❌ У этого промокода закончились активации!"

            async with conn.transaction():
                await conn.execute("UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = $1", code)
                await conn.execute("INSERT INTO promo_history (user_id, code) VALUES ($1, $2)", user_id, code)
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", reward, user_id)

            return True, f"🎉 Промокод активирован! Получено: <b>+{reward} 💰</b>"


db = Database(DATABASE_URL)


@dp.message.outer_middleware()
async def auto_register_middleware(handler, event: Message, data: dict):
    if event.from_user and not event.from_user.is_bot:
        if event.chat and event.chat.type in ["group", "supergroup"]:
            c_id = event.chat.id
            if c_id not in chat_recent_users:
                chat_recent_users[c_id] = []
            if event.from_user.id not in chat_recent_users[c_id]:
                chat_recent_users[c_id].append(event.from_user.id)
                if len(chat_recent_users[c_id]) > 50:
                    chat_recent_users[c_id].pop(0)

        if db.pool:
            try:
                await db.register_user(event.from_user.id, event.from_user.full_name, event.from_user.username)
            except Exception:
                pass
    return await handler(event, data)


# ================= КЛАВИАТУРЫ =================
def duel_keyboard(duel_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="⚔️ Принять вызов", callback_data=f"ac_{duel_id}")
    builder.button(text="❌ Отклонить", callback_data=f"dc_{duel_id}")
    builder.adjust(2)
    return builder.as_markup()


def check_keyboard(check_id: str, claimed: int, total: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💰 Забрать ({claimed}/{total})", callback_data=f"ck_{check_id}")
    return builder.as_markup()


def clan_invite_keyboard(invite_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Вступить", callback_data=f"ci_ac_{invite_id}")
    builder.button(text="❌ Отклонить", callback_data=f"ci_dc_{invite_id}")
    builder.adjust(2)
    return builder.as_markup()


def withdraw_admin_keyboard(req_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Звёзды отправлены", callback_data=f"wd_ok_{req_id}")
    builder.button(text="❌ Отклонить (Возврат)", callback_data=f"wd_no_{req_id}")
    builder.adjust(2)
    return builder.as_markup()


def gram_admin_keyboard(inv_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить зачисление", callback_data=f"g_ok_{inv_id}")
    builder.button(text="❌ Отклонить", callback_data=f"g_no_{inv_id}")
    builder.adjust(2)
    return builder.as_markup()


LADDER_STEPS = {0: 1.0, 1: 1.3, 2: 1.8, 3: 2.5, 4: 4.0, 5: 7.5}


def render_ladder(current_step: int) -> str:
    lines = []
    for step in range(5, 0, -1):
        mult = LADDER_STEPS[step]
        if step == current_step:
            lines.append(f"🧗 <b>[ Ступень {step} ] ➔ x{mult}</b> 🔥 <i>(Вы здесь)</i>")
        elif step < current_step:
            lines.append(f"✅ <s>[ Ступень {step} ] ➔ x{mult}</s>")
        else:
            lines.append(f"▫️ [ Ступень {step} ] ➔ x{mult}")
    return "\n".join(lines)


def ladder_keyboard(user_id: int, step: int):
    builder = InlineKeyboardBuilder()
    if step < 5:
        builder.button(text=f"🎲 Шаг вверх (след. x{LADDER_STEPS[step+1]})", callback_data=f"ld_step_{user_id}")
    if step > 0:
        builder.button(text=f"💰 Забрать куш (x{LADDER_STEPS[step]})", callback_data=f"ld_cash_{user_id}")
    builder.adjust(1)
    return builder.as_markup()


# ================= ОСНОВНЫЕ КОМАНДЫ =================
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    ref_id = None
    if command.args and command.args.startswith("ref_"):
        raw_id = command.args.replace("ref_", "")
        if raw_id.isdigit() and int(raw_id) != message.from_user.id:
            ref_id = int(raw_id)

    await db.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username, ref_id)
    user = await db.get_user(message.from_user.id)
    balance = user[2] if user else 0

    text = (
        f"🎲 <b>Добро пожаловать в Dice Club!</b>\n\n"
        f"👤 Игрок: {get_mention(message.from_user.id, message.from_user.full_name)}\n"
        f"💰 Баланс: <b>{balance} монет</b>\n\n"
        f"📜 <b>Режимы игр (мин. ставка 100 💰):</b>\n"
        f"⚔️ <code>/duel [ставка]</code> — дуэль 1 на 1 в чате\n"
        f"🎲 <code>/dice [ставка]</code> — бросок против бота\n"
        f"🎲🎲 <code>/doubledice [ставка]</code> — 2 кубика (х3 за дубль)\n"
        f"🚀 <code>/ladder [ставка]</code> — Кубическая Лесенка (до x7.5)\n"
        f"📈 <code>/over [ставка]</code> — Больше (4, 5, 6)\n"
        f"📉 <code>/under [ставка]</code> — Меньше (1, 2, 3)\n"
        f"⚖️ <code>/even [ставка]</code> — Чётное число\n"
        f"🎲 <code>/odd [ставка]</code> — Нечётное число\n\n"
        f"🏰 <b>Кланы и Социалка:</b>\n"
        f"🛡 <code>/clan</code> — карточка клана\n"
        f"🚩 <code>/create_clan [имя]</code> — создать клан (2500 💰)\n"
        f"🏆 <code>/clan_top</code> — топ кланов по казне\n\n"
        f"💳 <b>Пополнение и Вывод:</b>\n"
        f"💎 <code>/gram [кол-во]</code> — пополнить через Gram / TON (без холда)\n"
        f"⭐ <code>/stars [кол-во]</code> — пополнить за Stars (холд 21д на вывод)\n"
        f"📤 <code>/withdraw [монеты]</code> — вывод в Stars (курс 10:1, от 1000 💰)\n"
        f"🧧 <code>/check [сумма] [людей]</code> — раздать чек в чат\n"
        f"🌧 <code>/rain [сумма] [людей]</code> — денежный дождь\n"
        f"🤝 <code>/ref</code> — реферальная ссылка (3%)\n"
        f"💸 <code>/pay [сумма]</code> (ответом) — передать монеты\n"
        f"👤 <code>/profile</code> | 🏆 <code>/top</code> | 🎟 <code>/promo [код]</code>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    ref_count = await db.get_referrals_count(message.from_user.id)

    text = (
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей и получай <b>3% от каждого их проигрыша</b> во всех режимах кубиков!\n\n"
        f"👥 Твоих рефералов: <b>{ref_count}</b>\n"
        f"🔗 Ссылка для приглашения:\n<code>{ref_link}</code>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user:
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        user = await db.get_user(user_id)

    _, name, balance, turnover, wins, losses, draws, warns, _, clan_id, has_dep, last_stars, reg_date = user
    total_games = wins + losses + draws
    winrate = round((wins / total_games * 100), 1) if total_games > 0 else 0

    clan_tag = "<i>Без клана</i>"
    if clan_id:
        clan = await db.get_clan(clan_id)
        if clan:
            clan_tag = f"<b>[{html.escape(clan['name'])}]</b>"

    dep_status = "⭐ Депозитор" if has_dep else "⏳ Без депозита"

    text = (
        f"┏ 👤 <b>Профиль:</b> {get_mention(user_id, name)}\n"
        f"┣ 🏰 <b>Клан:</b> {clan_tag}\n"
        f"┣ 🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"┣ 💰 <b>Баланс:</b> <code>{balance} 💰</code>\n"
        f"┣ 🔄 <b>Оборот:</b> <code>{turnover} 💰</code>\n"
        f"┣ 🎮 <b>Всего игр:</b> <code>{total_games}</code>\n"
        f"┣ 🏆 <b>Побед:</b> <code>{wins}</code> | 💀 <b>Поражений:</b> <code>{losses}</code> | ⚖️ <b>Ничьих:</b> <code>{draws}</code>\n"
        f"┣ 📈 <b>Винрейт:</b> <code>{winrate}%</code>\n"
        f"┣ 💳 <b>Статус:</b> {dep_status}\n"
        f"┗ ⚠️ <b>Варны:</b> <code>{warns}/3</code>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("pay"))
async def cmd_pay(message: Message, command: CommandObject):
    if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
        return await message.answer("❌ Ответьте этой командой на сообщение игрока!")

    recipient = message.reply_to_message.from_user
    sender = message.from_user

    if recipient.id == sender.id:
        return await message.answer("❌ Нельзя переводить монеты самому себе!")

    if not command.args or not command.args.isdigit():
        return await message.answer("❌ Формат: <code>/pay 100</code>", parse_mode="HTML")

    amount = int(command.args)
    if amount <= 0:
        return await message.answer("❌ Сумма перевода должна быть больше 0!")

    await db.register_user(sender.id, sender.full_name, sender.username)
    await db.register_user(recipient.id, recipient.full_name, recipient.username)

    sender_data = await db.get_user(sender.id)
    if not sender_data or sender_data[2] < amount:
        return await message.answer("❌ Недостаточно монет для перевода!")

    await db.change_balance(sender.id, -amount)
    await db.change_balance(recipient.id, amount)

    await message.answer(
        f"💸 {get_mention(sender.id, sender.full_name)} перевел <b>{amount} 💰</b> "
        f"игроку {get_mention(recipient.id, recipient.full_name)}!",
        parse_mode="HTML"
    )


# ================= 💎 ПОПОЛНЕНИЕ GRAM / TON =================
@dp.message(Command("gram"))
@dp.message(Command("ton"))
async def cmd_gram(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    gram_amount = 1.0
    if command.args:
        try:
            gram_amount = float(command.args.replace(",", "."))
        except ValueError:
            return await message.answer("❌ Укажите количество Gram: <code>/gram 5</code>", parse_mode="HTML")

    if gram_amount < 0.1:
        return await message.answer("❌ Минимальная сумма пополнения: 0.1 Gram!")

    coins_amount = int(gram_amount * 1000)
    invoice_id = f"G_{uuid.uuid4().hex[:6]}"

    active_gram_invoices[invoice_id] = {
        "user_id": user_id,
        "gram_amount": gram_amount,
        "coins_amount": coins_amount
    }

    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO gram_deposits (invoice_id, user_id, gram_amount, coins_amount, status) VALUES ($1, $2, $3, $4, 'pending')",
            invoice_id, user_id, gram_amount, coins_amount
        )

    ton_link = f"ton://transfer/{GRAM_WALLET}?amount={int(gram_amount * 1e9)}&text={invoice_id}"

    builder = InlineKeyboardBuilder()
    builder.button(text=f"💎 Оплатить {gram_amount} GRAM", url=ton_link)
    builder.button(text="✅ Я оплатил", callback_data=f"g_check_{invoice_id}")
    builder.adjust(1)

    text = (
        f"💎 <b>ПОПОЛНЕНИЕ ЧЕРЕЗ GRAM / TON</b>\n\n"
        f"💰 Вы получите: <b>+{coins_amount} монет</b> (без холда!)\n"
        f"💵 К оплате: <code>{gram_amount} GRAM</code> (или TON)\n\n"
        f"📍 <b>Адрес кошелька:</b>\n<code>{GRAM_WALLET}</code>\n\n"
        f"📝 <b>ОБЯЗАТЕЛЬНЫЙ комментарий (MEMO):</b>\n<code>{invoice_id}</code>\n\n"
        f"⚠️ <i>Обязательно укажите комментарий <code>{invoice_id}</code> при переводе! После перевода нажмите кнопку «Я оплатил».</i>"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("g_check_"))
async def cb_gram_check(call: CallbackQuery):
    inv_id = call.data.replace("g_check_", "")
    async with db.pool.acquire() as conn:
        dep = await conn.fetchrow("SELECT * FROM gram_deposits WHERE invoice_id = $1", inv_id)
        if not dep:
            return await call.answer("❌ Заявка не найдена!", show_alert=True)

        if dep["status"] == "completed":
            return await call.answer("✅ Этот платеж уже зачислен!", show_alert=True)

    await call.answer("⏳ Запрос на проверку отправлен администраторам!", show_alert=True)
    await call.message.edit_text("⏳ <b>Платеж отправлен на проверку!</b> Монеты будут зачислены после проверки перевода.", parse_mode="HTML")

    if OWNER_ID:
        admin_text = (
            f"💎 <b>НОВОЕ ПОПОЛНЕНИЕ GRAM/TON!</b>\n\n"
            f"👤 Игрок: {get_mention(dep['user_id'], call.from_user.full_name)} (ID: <code>{dep['user_id']}</code>)\n"
            f"💵 Сумма: <b>{dep['gram_amount']} GRAM</b>\n"
            f"💰 К начислению: <b>{dep['coins_amount']} 💰</b>\n"
            f"📝 Memo/Инвойс: <code>{inv_id}</code>"
        )
        try:
            await bot.send_message(
                chat_id=OWNER_ID,
                text=admin_text,
                reply_markup=gram_admin_keyboard(inv_id),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления админу: {e}")


@dp.callback_query(F.data.startswith("g_ok_"))
async def cb_gram_approve(call: CallbackQuery):
    if not await db.is_admin(call.from_user.id):
        return await call.answer("❌ Нет прав!", show_alert=True)

    inv_id = call.data.replace("g_ok_", "")
    async with db.pool.acquire() as conn:
        dep = await conn.fetchrow("SELECT * FROM gram_deposits WHERE invoice_id = $1", inv_id)
        if not dep or dep["status"] != "pending":
            return await call.answer("❌ Заявка уже обработана!", show_alert=True)

        await conn.execute("UPDATE gram_deposits SET status = 'completed' WHERE invoice_id = $1", inv_id)
        await db.change_balance(dep["user_id"], int(dep["coins_amount"]))
        await conn.execute("UPDATE users SET has_deposited = TRUE WHERE user_id = $1", dep["user_id"])

    try:
        await bot.send_message(
            chat_id=dep["user_id"],
            text=f"🎉 <b>Платеж GRAM подтвержден!</b>\n💰 Начислено: <b>+{dep['coins_amount']} 💰</b> (без холда)",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await call.message.edit_text(f"{call.message.text}\n\n<b>✅ ЗАЧИСЛЕНО админом {get_mention(call.from_user.id, call.from_user.full_name)}</b>", parse_mode="HTML")
    await call.answer("✅ Монеты зачислены!")


@dp.callback_query(F.data.startswith("g_no_"))
async def cb_gram_reject(call: CallbackQuery):
    if not await db.is_admin(call.from_user.id):
        return await call.answer("❌ Нет прав!", show_alert=True)

    inv_id = call.data.replace("g_no_", "")
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE gram_deposits SET status = 'rejected' WHERE invoice_id = $1", inv_id)

    await call.message.edit_text(f"{call.message.text}\n\n<b>❌ ОТКЛОНЕНО</b>", parse_mode="HTML")
    await call.answer("❌ Заявка отклонена.")


# ================= ⭐ ВЫВОД STARS (КУРС 10:1, ЗАЩИТА 21 ДЕНЬ) =================
@dp.message(Command("withdraw"))
@dp.message(Command("out"))
async def cmd_withdraw(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not command.args:
        return await message.answer(
            "⭐ <b>Вывод в Telegram Stars</b>\n\n"
            "Курс конвертации: <b>10 монет = 1 ⭐ Star</b>\n"
            "Минимум для вывода: <b>1000 💰 (= 100 ⭐)</b>\n\n"
            "🔒 <b>Защита от Refund:</b> Вывод доступен спустя <b>21 день</b> после последнего пополнения через Stars (на пополнения через <code>/gram</code> холд не действует).\n\n"
            "Формат: <code>/withdraw [монеты] [твой_тег]</code>\n"
            "<i>Пример:</i> <code>/withdraw 2000 @durov</code> (получите 200 ⭐)",
            parse_mode="HTML"
        )

    parts = command.args.split()
    if not parts[0].isdigit():
        return await message.answer("❌ Сумма должна быть числом!")

    amount = int(parts[0])
    target_username = parts[1].strip() if len(parts) > 1 else (f"@{message.from_user.username}" if message.from_user.username else str(user_id))

    if amount < 1000:
        return await message.answer(
            f"❌ <b>Минимальная сумма вывода: 1000 💰 (= 100 ⭐)!</b>\nВаша сумма: <code>{amount} 💰</code>",
            parse_mode="HTML"
        )

    user = await db.get_user(user_id)
    if not user or user[2] < amount:
        bal = user[2] if user else 0
        return await message.answer(
            f"❌ <b>Недостаточно средств!</b>\nВаш баланс: <code>{bal} 💰</code>",
            parse_mode="HTML"
        )

    last_stars_dep = user[11]
    created_at = user[12] or datetime.now()

    check_date = last_stars_dep if last_stars_dep else created_at
    days_passed = (datetime.now() - check_date).days

    if days_passed < 21:
        days_left = 21 - days_passed
        reason = "последнего депозита Stars (защита от рефанда Telegram)" if last_stars_dep else "регистрации"
        return await message.answer(
            f"🔒 <b>Холд безопасности активен!</b>\n\n"
            f"В связи с правилами Telegram Stars Refund вывод средств заморожен на 21 день с момента {reason}.\n\n"
            f"⏳ Осталось дней холда: <b>{days_left} дн.</b>\n"
            f"💡 <i>Пополняйте баланс через <code>/gram</code> без каких-либо холдов и задержек!</i>",
            parse_mode="HTML"
        )

    stars_to_receive = amount // 10

    await db.change_balance(user_id, -amount)
    req_id = uuid.uuid4().hex[:8]

    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO withdraw_requests (req_id, user_id, amount, stars_amount, target_username, status) VALUES ($1, $2, $3, $4, $5, 'pending')",
            req_id, user_id, amount, stars_to_receive, target_username
        )

    await message.answer(
        f"⏳ <b>Заявка на вывод Stars #{req_id} создана!</b>\n\n"
        f"💰 Списано: <b>{amount} 💰</b>\n"
        f"⭐ К получению: <b>{stars_to_receive} Telegram Stars</b>\n"
        f"👤 Получатель: <code>{html.escape(target_username)}</code>\n\n"
        f"<i>Администратор отправит звёзды подарком на указанный аккаунт в ближайшее время.</i>",
        parse_mode="HTML"
    )

    if OWNER_ID:
        admin_text = (
            f"🚨 <b>ЗАЯВКА НА ВЫВОД STARS #{req_id}</b>\n\n"
            f"👤 Игрок: {get_mention(user_id, message.from_user.full_name)} (ID: <code>{user_id}</code>)\n"
            f"💰 Списано монет: <b>{amount} 💰</b>\n"
            f"⭐ <b>Отправить звёзд: {stars_to_receive} ⭐</b>\n"
            f"🔗 Получатель: {html.escape(target_username)}"
        )
        try:
            await bot.send_message(
                chat_id=OWNER_ID,
                text=admin_text,
                reply_markup=withdraw_admin_keyboard(req_id),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу о выводе: {e}")


@dp.callback_query(F.data.startswith("wd_ok_"))
async def cb_withdraw_approve(call: CallbackQuery):
    if not await db.is_admin(call.from_user.id):
        return await call.answer("❌ Нет прав!", show_alert=True)

    req_id = call.data.replace("wd_ok_", "")
    
    async with db.pool.acquire() as conn:
        req = await conn.fetchrow("SELECT user_id, amount, stars_amount, target_username, status FROM withdraw_requests WHERE req_id = $1", req_id)
        if not req or req["status"] != "pending":
            return await call.answer("❌ Заявка уже обработана или не найдена!", show_alert=True)

        await conn.execute("UPDATE withdraw_requests SET status = 'approved' WHERE req_id = $1", req_id)

    stars = req["stars_amount"] if req["stars_amount"] else req["amount"] // 10

    try:
        await bot.send_message(
            chat_id=req["user_id"],
            text=(
                f"✅ <b>Заявка #{req_id} выплачена!</b>\n\n"
                f"⭐ Вам отправлено: <b>+{stars} Telegram Stars</b>\n"
                f"👤 На аккаунт: <code>{html.escape(req['target_username'])}</code>\n\n"
                f"<i>Спасибо за игру в Dice Club!</i>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await call.message.edit_text(
        f"{call.message.text}\n\n<b>✅ СТАТУС: Звёзды отправлены админом {get_mention(call.from_user.id, call.from_user.full_name)}</b>",
        parse_mode="HTML"
    )
    await call.answer("✅ Выплата Stars подтверждена!")


@dp.callback_query(F.data.startswith("wd_no_"))
async def cb_withdraw_reject(call: CallbackQuery):
    if not await db.is_admin(call.from_user.id):
        return await call.answer("❌ Нет прав!", show_alert=True)

    req_id = call.data.replace("wd_no_", "")

    async with db.pool.acquire() as conn:
        req = await conn.fetchrow("SELECT user_id, amount, details, status FROM withdraw_requests WHERE req_id = $1", req_id)
        if not req or req["status"] != "pending":
            return await call.answer("❌ Заявка уже обработана или не найдена!", show_alert=True)

        await conn.execute("UPDATE withdraw_requests SET status = 'rejected' WHERE req_id = $1", req_id)
        await db.change_balance(req["user_id"], req["amount"])

    try:
        await bot.send_message(
            chat_id=req["user_id"],
            text=(
                f"❌ <b>Заявка на вывод #{req_id} отклонена!</b>\n\n"
                f"💰 Сумма <b>{req['amount']} 💰</b> возвращена на ваш игровой баланс.\n"
                f"<i>Убедитесь в правильности указанного @username и повторите попытку.</i>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await call.message.edit_text(
        f"{call.message.text}\n\n<b>❌ СТАТУС: Отклонено (монеты возвращены игроку)</b>",
        parse_mode="HTML"
    )
    await call.answer("❌ Заявка отклонена, средства возвращены.")


# ================= 🚀 КУБИЧЕСКАЯ ЛЕСЕНКА (/ladder) =================
@dp.message(Command("ladder"))
async def cmd_ladder(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    if user_id in active_ladders:
        return await message.answer("❌ У вас уже начата игра в Лесенку! Завершите её.")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка в Лесенке: <b>100 💰</b>!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    active_ladders[user_id] = {
        "user_id": user_id,
        "bet": bet,
        "step": 0,
        "is_rolling": False
    }

    text = (
        f"🚀 <b>КУБИЧЕСКАЯ ЛЕСЕНКА</b>\n\n"
        f"👤 Игрок: {get_mention(user_id, message.from_user.full_name)}\n"
        f"💰 Ставка: <b>{bet} 💰</b>\n\n"
        f"{render_ladder(0)}\n\n"
        f"🎲 <i>Правила: кубик 3, 4, 5, 6 — подъём наверх (+множитель). 1 или 2 — падение и сгорание ставки!</i>"
    )
    await message.answer(text, reply_markup=ladder_keyboard(user_id, 0), parse_mode="HTML")


@dp.callback_query(F.data.startswith("ld_step_"))
async def cb_ladder_step(call: CallbackQuery):
    try:
        user_id = int(call.data.replace("ld_step_", ""))
    except ValueError:
        return

    if call.from_user.id != user_id:
        return await call.answer("❌ Это не ваша игра в лесенку!", show_alert=True)

    if user_id not in active_ladders:
        return await call.answer("❌ Игра уже завершена!", show_alert=True)

    game = active_ladders[user_id]
    if game.get("is_rolling"):
        return await call.answer("⏳ Кубик уже брошен, подождите!", show_alert=False)

    game["is_rolling"] = True
    await call.message.edit_reply_markup(reply_markup=None)

    await call.message.answer(f"🎲 Бросок кубика для подъема {get_mention(user_id, call.from_user.full_name)}:", parse_mode="HTML")
    dice_msg = await call.message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = int(dice_msg.dice.value)

    # Падение (1 или 2)
    if val in [1, 2]:
        bet = game["bet"]
        del active_ladders[user_id]
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception:
            pass

        res = (
            f"💀 <b>ПАДЕНИЕ С ЛЕСНИЦЫ!</b>\n\n"
            f"👤 {get_mention(user_id, call.from_user.full_name)}\n"
            f"🎲 Выпало число: [ <b>{val}</b> ] (Осечка 1-2)\n"
            f"📉 Ставка сгорела: <b>-{bet} 💰</b>"
        )
        return await send_game_result(call.message, IMG_LOSS, res)

    # Успешный шаг (3, 4, 5, 6)
    game["step"] += 1
    game["is_rolling"] = False
    step = game["step"]
    mult = LADDER_STEPS[step]

    # Достигнута вершина (5 ступень x7.5)
    if step >= 5:
        win = int(game["bet"] * mult)
        del active_ladders[user_id]
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")

        res = (
            f"👑 <b>ВЕРШИНА ПОКОРЕНА! ВЫ ПРОШЛИ ВСЮ ЛЕСЕНКУ!</b>\n\n"
            f"👤 {get_mention(user_id, call.from_user.full_name)}\n"
            f"🎲 Выпало: [ <b>{val}</b> ]\n"
            f"{render_ladder(5)}\n\n"
            f"🔥 Максимальный множитель: <b>x{mult}</b>\n"
            f"💵 Выигрыш: <b>+{win} 💰</b>"
        )
        return await send_game_result(call.message, IMG_WIN, res)

    current_win = int(game["bet"] * mult)
    res = (
        f"🧗 <b>УСПЕШНЫЙ ШАГ ВВЕРХ!</b>\n\n"
        f"👤 {get_mention(user_id, call.from_user.full_name)}\n"
        f"🎲 Выпало: [ <b>{val}</b> ]\n\n"
        f"{render_ladder(step)}\n\n"
        f"📈 Множитель: <b>x{mult}</b> (Куш: <b>{current_win} 💰</b>)"
    )
    await call.message.answer(res, reply_markup=ladder_keyboard(user_id, step), parse_mode="HTML")


@dp.callback_query(F.data.startswith("ld_cash_"))
async def cb_ladder_cash(call: CallbackQuery):
    try:
        user_id = int(call.data.replace("ld_cash_", ""))
    except ValueError:
        return

    if call.from_user.id != user_id:
        return await call.answer("❌ Это не ваша игра!", show_alert=True)

    if user_id not in active_ladders:
        return await call.answer("❌ Игра уже завершена!", show_alert=True)

    game = active_ladders[user_id]
    mult = LADDER_STEPS[game["step"]]
    win = int(game["bet"] * mult)

    del active_ladders[user_id]
    await db.change_balance(user_id, win)
    await db.record_game(user_id, "win")

    await call.message.edit_reply_markup(reply_markup=None)

    res = (
        f"💰 <b>ВЫ УСПЕШНО ЗАБРАЛИ КУШ В ЛЕСЕНКЕ!</b>\n\n"
        f"👤 {get_mention(user_id, call.from_user.full_name)}\n"
        f"🧗 Остановлено на: <b>Ступень {game['step']}</b>\n"
        f"📈 Зафиксирован множитель: <b>x{mult}</b>\n"
        f"💵 На баланс зачислено: <b>+{win} 💰</b>"
    )
    await send_game_result(call.message, IMG_WIN, res)


# ================= 🌧 ДЕНЕЖНЫЙ ДОЖДЬ (/rain) =================
@dp.message(Command("rain"))
async def cmd_rain(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if message.chat.type not in ["group", "supergroup"]:
        return await message.answer("❌ Денежный дождь доступен только в групповых чатах!")

    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Использование: <code>/rain [сумма] [кол-во_игроков]</code>\nПример: <code>/rain 500 5</code>", parse_mode="HTML")

    args = command.args.split()
    if not args[0].isdigit() or not args[1].isdigit():
        return await message.answer("❌ Сумма и количество игроков должны быть числами!")

    total_amount = int(args[0])
    people_count = int(args[1])

    if total_amount < people_count or people_count <= 0 or total_amount < 100:
        return await message.answer("❌ Минимальная сумма дождя: 100 💰!")

    user = await db.get_user(user_id)
    if not user or user[2] < total_amount:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    chat_id = message.chat.id
    candidates = [u for u in chat_recent_users.get(chat_id, []) if u != user_id]

    if not candidates:
        return await message.answer("❌ В чате пока недостаточно активных участников для дождя!")

    selected_count = min(people_count, len(candidates))
    lucky_users = random.sample(candidates, selected_count)
    per_person = total_amount // selected_count

    await db.change_balance(user_id, -total_amount)

    mentions_list = []
    for uid in lucky_users:
        u_data = await db.get_user(uid)
        u_name = u_data[1] if u_data else f"ID {uid}"
        await db.change_balance(uid, per_person)
        mentions_list.append(f"• {get_mention(uid, u_name)} ➔ <b>+{per_person} 💰</b>")

    text = (
        f"🌧 <b>ДЕНЕЖНЫЙ ДОЖДЬ В ЧАТЕ!</b> 🌧\n\n"
        f"👤 Спонсор: {get_mention(user_id, message.from_user.full_name)}\n"
        f"💰 Раздано: <b>{total_amount} 💰</b> на <b>{selected_count}</b> активных участников!\n\n"
        f"🎁 <b>Счастливчики:</b>\n" + "\n".join(mentions_list)
    )
    await message.answer(text, parse_mode="HTML")


# ================= КЛАНОВАЯ СИСТЕМА (2500 💰) =================
@dp.message(Command("create_clan"))
async def cmd_create_clan(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not command.args:
        return await message.answer("❌ Укажите название клана: <code>/create_clan [Название]</code>", parse_mode="HTML")

    clan_name = command.args.strip()
    if len(clan_name) < 3 or len(clan_name) > 16:
        return await message.answer("❌ Название клана должно быть от 3 до 16 символов!")

    user = await db.get_user(user_id)
    if user[9]:
        return await message.answer("❌ Вы уже состоите в клане! Сначала покиньте его через /leave_clan.")

    creation_cost = 2500
    if user[2] < creation_cost:
        return await message.answer(f"❌ Создание клана стоит <b>{creation_cost} 💰</b>. У вас: {user[2]} 💰", parse_mode="HTML")

    await db.change_balance(user_id, -creation_cost)
    clan_id = await db.create_clan(user_id, clan_name)

    if not clan_id:
        await db.change_balance(user_id, creation_cost)
        return await message.answer("❌ Клан с таким названием уже существует! Выберите другое имя.")

    await message.answer(f"🏰 <b>Клан [{html.escape(clan_name)}] успешно создан!</b>\n👑 Лидер: {get_mention(user_id, message.from_user.full_name)}", parse_mode="HTML")


@dp.message(Command("clan"))
async def cmd_clan(message: Message):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user or not user[9]:
        return await message.answer("🛡 Вы не состоите в клане. Создайте свой: <code>/create_clan [Название]</code> (2500 💰)", parse_mode="HTML")

    clan = await db.get_clan(user[9])
    if not clan:
        await db.set_user_clan(user_id, None)
        return await message.answer("❌ Ваш клан был расформирован.")

    members_count = await db.get_clan_members_count(clan["clan_id"])
    owner_data = await db.get_user(clan["owner_id"])
    owner_name = owner_data[1] if owner_data else "Неизвестно"

    text = (
        f"🏰 <b>Информация о клане: [{html.escape(clan['name'])}]</b>\n\n"
        f"👑 <b>Глава:</b> {get_mention(clan['owner_id'], owner_name)}\n"
        f"👥 <b>Участников:</b> <code>{members_count} чел.</code>\n"
        f"💰 <b>Казна клана:</b> <code>{clan['treasury']} 💰</code>\n\n"
        f"📌 <i>Пополнить казну: <code>/clan_deposit [сумма]</code></i>\n"
        f"👥 <i>Пригласить игрока: <code>/invite_clan</code> (ответом)</i>\n"
        f"🚪 <i>Покинуть клан: <code>/leave_clan</code></i>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("clan_deposit"))
async def cmd_clan_deposit(message: Message, command: CommandObject):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user or not user[9]:
        return await message.answer("❌ Вы не состоите в клане!")

    if not command.args or not command.args.isdigit():
        return await message.answer("Формат: <code>/clan_deposit 100</code>", parse_mode="HTML")

    amount = int(command.args)
    if amount <= 0:
        return await message.answer("❌ Сумма должна быть больше 0!")

    if user[2] < amount:
        return await message.answer(f"❌ Недостаточно средств! Ваш баланс: <b>{user[2]} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -amount)
    await db.add_to_clan_treasury(user[9], amount)

    clan = await db.get_clan(user[9])
    await message.answer(f"💰 {get_mention(user_id, message.from_user.full_name)} внёс <b>+{amount} 💰</b> в казну клана [{html.escape(clan['name'])}]!", parse_mode="HTML")


@dp.message(Command("invite_clan"))
async def cmd_invite_clan(message: Message):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user or not user[9]:
        return await message.answer("❌ Вы не состоите в клане!")

    clan = await db.get_clan(user[9])
    if clan["owner_id"] != user_id:
        return await message.answer("❌ Только лидер клана может приглашать участников!")

    if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
        return await message.answer("❌ Ответьте этой командой на сообщение игрока, которого хотите пригласить!")

    target = message.reply_to_message.from_user
    if target.id == user_id:
        return await message.answer("❌ Вы не можете пригласить сами себя!")

    target_user = await db.get_user(target.id)
    if target_user and target_user[9]:
        return await message.answer("❌ Этот игрок уже состоит в клане!")

    invite_id = uuid.uuid4().hex[:8]
    active_clan_invites[invite_id] = {
        "clan_id": clan["clan_id"],
        "clan_name": clan["name"],
        "target_id": target.id
    }

    text = (
        f"🏰 <b>ПРИГЛАШЕНИЕ В КЛАН!</b>\n\n"
        f"Игрок {get_mention(target.id, target.full_name)}, вас приглашают в клан <b>[{html.escape(clan['name'])}]</b>!\n"
        f"Лидер: {get_mention(user_id, message.from_user.full_name)}"
    )
    await message.answer(text, reply_markup=clan_invite_keyboard(invite_id), parse_mode="HTML")


@dp.callback_query(F.data.startswith("ci_ac_"))
async def cb_accept_clan_invite(call: CallbackQuery):
    invite_id = call.data.replace("ci_ac_", "")
    if invite_id not in active_clan_invites:
        return await call.answer("❌ Приглашение устарело!", show_alert=True)

    invite = active_clan_invites[invite_id]
    if call.from_user.id != invite["target_id"]:
        return await call.answer("❌ Это приглашение не для вас!", show_alert=True)

    user = await db.get_user(call.from_user.id)
    if user and user[9]:
        del active_clan_invites[invite_id]
        return await call.answer("❌ Вы уже состоите в клане!", show_alert=True)

    await db.set_user_clan(call.from_user.id, invite["clan_id"])
    del active_clan_invites[invite_id]

    await call.message.edit_text(f"🎉 {get_mention(call.from_user.id, call.from_user.full_name)} вступил в клан <b>[{html.escape(invite['clan_name'])}]</b>!", parse_mode="HTML")


@dp.callback_query(F.data.startswith("ci_dc_"))
async def cb_decline_clan_invite(call: CallbackQuery):
    invite_id = call.data.replace("ci_dc_", "")
    if invite_id in active_clan_invites:
        if call.from_user.id == active_clan_invites[invite_id]["target_id"]:
            del active_clan_invites[invite_id]
            await call.message.edit_text("❌ Приглашение в клан отклонено.", parse_mode="HTML")
            return
    await call.answer()


@dp.message(Command("leave_clan"))
async def cmd_leave_clan(message: Message):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user or not user[9]:
        return await message.answer("❌ Вы не состоите в клане!")

    clan = await db.get_clan(user[9])
    if clan["owner_id"] == user_id:
        await db.delete_clan(clan["clan_id"])
        await message.answer(f"💥 Лидер распустил клан <b>[{html.escape(clan['name'])}]</b>!", parse_mode="HTML")
    else:
        await db.set_user_clan(user_id, None)
        await message.answer(f"🚪 Вы покинули клан <b>[{html.escape(clan['name'])}]</b>.", parse_mode="HTML")


@dp.message(Command("clan_top"))
async def cmd_clan_top(message: Message):
    top_clans = await db.get_top_clans(10)
    if not top_clans:
        return await message.answer("Таблица кланов пуста.")

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = "🏰 <b>ТОП-10 СИЛЬНЕЙШИХ КЛАНОВ (ПО КАЗНЕ):</b>\n\n"
    for i, row in enumerate(top_clans, 1):
        place = medals.get(i, f"<b>{i}.</b>")
        name = html.escape(row["name"])
        treasury = row["treasury"]
        text += f"{place} [{name}] — <code>{treasury} 💰</code> в казне\n"

    await message.answer(text, parse_mode="HTML")


# ================= РАЗДАЧИ ЧЕКОВ (/check, /drop) =================
@dp.message(Command("check"))
@dp.message(Command("drop"))
async def cmd_check(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Использование: <code>/check [сумма] [кол-во_человек]</code>", parse_mode="HTML")

    args = command.args.split()
    if not args[0].isdigit() or not args[1].isdigit():
        return await message.answer("❌ Сумма и количество человек должны быть числами!", parse_mode="HTML")

    total_amount, people_count = int(args[0]), int(args[1])
    if total_amount < people_count or people_count <= 0 or total_amount < 100:
        return await message.answer("❌ Минимальная сумма чека: 100 💰!", parse_mode="HTML")

    user = await db.get_user(user_id)
    if not user or user[2] < total_amount:
        bal = user[2] if user else 0
        return await message.answer(f"❌ Недостаточно средств! Ваш баланс: <b>{bal} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -total_amount)
    check_id = uuid.uuid4().hex[:8]
    per_person = total_amount // people_count

    active_checks[check_id] = {
        "creator_id": user_id,
        "creator_name": message.from_user.full_name,
        "total_amount": total_amount,
        "per_person": per_person,
        "total_people": people_count,
        "claimed_users": []
    }

    text = (
        f"🧧 <b>ДЕНЕЖНЫЙ ЧЕК В ЧАТЕ!</b>\n\n"
        f"👤 Создатель: {get_mention(user_id, message.from_user.full_name)}\n"
        f"💰 Общая сумма: <b>{total_amount} 💰</b>\n"
        f"👥 Количество активаций: <b>{people_count}</b> (по <b>{per_person} 💰</b> каждому)\n\n"
        f"<i>Жми на кнопку ниже, чтобы забрать!</i>"
    )
    await message.answer(text, reply_markup=check_keyboard(check_id, 0, people_count), parse_mode="HTML")


@dp.callback_query(F.data.startswith("ck_"))
async def cb_claim_check(call: CallbackQuery):
    check_id = call.data.replace("ck_", "")
    user_id = call.from_user.id

    if check_id not in active_checks:
        return await call.answer("❌ Этот чек уже полностью разобран!", show_alert=True)

    check = active_checks[check_id]
    if user_id in check["claimed_users"]:
        return await call.answer("❌ Вы уже забрали эту раздачу!", show_alert=True)

    if not await check_subscription(user_id):
        return await call.answer("⚠️ Подпишитесь на наш канал, чтобы забирать чеки!", show_alert=True)

    await db.register_user(user_id, call.from_user.full_name, call.from_user.username)
    check["claimed_users"].append(user_id)
    reward = check["per_person"]
    await db.change_balance(user_id, reward)

    claimed_count = len(check["claimed_users"])
    total_people = check["total_people"]

    await call.answer(f"🎉 Вы забрали +{reward} 💰!", show_alert=True)

    if claimed_count >= total_people:
        del active_checks[check_id]
        await call.message.edit_text(
            f"🧧 <b>ЧЕК ПОЛНОСТЬЮ РАЗОБРАН!</b>\n\n"
            f"👤 Создатель: {get_mention(check['creator_id'], check['creator_name'])}\n"
            f"💰 Раздал: <b>{check['total_amount']} 💰</b> на <b>{total_people}</b> человек!",
            parse_mode="HTML"
        )
    else:
        await call.message.edit_reply_markup(reply_markup=check_keyboard(check_id, claimed_count, total_people))


# ================= ОДИНОЧНЫЕ ИГРЫ (100% ГАРАНТИЯ ВЫВОДА) =================
@dp.message(Command("dice"))
async def cmd_dice(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка: <b>100 💰</b>!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 Бросок {get_mention(user_id, message.from_user.full_name)}:", parse_mode="HTML")
    p_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    p_val = int(p_msg.dice.value)

    await message.answer("🤖 Бросок Бота:", parse_mode="HTML")
    b_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    b_val = int(b_msg.dice.value)

    if p_val > b_val:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        try:
            await db.record_game(user_id, "win")
        except Exception:
            pass
        text = (
            f"🏆 <b>ПОБЕДА!</b>\n\n"
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"👤 {get_mention(user_id, message.from_user.full_name)}\n"
            f"💰 Коэффициент: <b>x1.95</b>\n"
            f"💵 Выигрыш: <b>+{win} 💰</b>"
        )
        await send_game_result(message, IMG_WIN, text)
    elif p_val < b_val:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception as e:
            logging.error(f"Ошибка фиксации поражения: {e}")
        text = (
            f"💀 <b>ПОРАЖЕНИЕ!</b>\n\n"
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"👤 {get_mention(user_id, message.from_user.full_name)}\n"
            f"📉 Потеряно: <b>-{bet} 💰</b>"
        )
        await send_game_result(message, IMG_LOSS, text)
    else:
        await db.change_balance(user_id, bet)
        try:
            await db.record_game(user_id, "draw")
        except Exception:
            pass
        text = (
            f"╔════════════════════╗\n      ⚖️ <b>БОЕВАЯ НИЧЬЯ!</b> ⚖️\n╚════════════════════╝\n\n"
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"💰 <b>Возврат:</b> <code>+{bet} 💰</code>"
        )
        await send_game_result(message, IMG_DRAW, text)


@dp.message(Command("doubledice"))
async def cmd_double_dice(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка: <b>100 💰</b>!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲🎲 <b>Бросок двух кубиков {get_mention(user_id, message.from_user.full_name)}:</b>", parse_mode="HTML")
    p_d1 = await message.answer_dice(emoji="🎲")
    p_d2 = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    p1, p2 = int(p_d1.dice.value), int(p_d2.dice.value)
    p_sum = p1 + p2

    await message.answer("🤖 <b>Бросок двух кубиков Бота:</b>", parse_mode="HTML")
    b_d1 = await message.answer_dice(emoji="🎲")
    b_d2 = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    b1, b2 = int(b_d1.dice.value), int(b_d2.dice.value)
    b_sum = b1 + b2

    if p_sum > b_sum:
        is_double = (p1 == p2)
        mult = 3.0 if is_double else 1.95
        win = int(bet * mult)

        await db.change_balance(user_id, win)
        try:
            await db.record_game(user_id, "win")
        except Exception:
            pass

        bonus_title = "🔥 <b>МЕГА-ДУБЛЬ (x3.0)!</b>\n" if is_double else f"Коэффициент: <b>x{mult}</b>\n"
        res = f"🎉 <b>ПОБЕДА!</b>\n\n👤 {get_mention(user_id, message.from_user.full_name)}\nТвои очки: {p1} + {p2} = <b>{p_sum}</b>\n🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n{bonus_title}💵 Выигрыш: <b>+{win} 💰</b>"
        await send_game_result(message, IMG_WIN, res)
    elif p_sum < b_sum:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception as e:
            logging.error(f"Ошибка фиксации поражения: {e}")
        res = f"💀 <b>ПОРАЖЕНИЕ!</b>\n\n👤 {get_mention(user_id, message.from_user.full_name)}\nТвои очки: {p1} + {p2} = <b>{p_sum}</b>\n🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n📉 Потеряно: <b>-{bet} 💰</b>"
        await send_game_result(message, IMG_LOSS, res)
    else:
        await db.change_balance(user_id, bet)
        try:
            await db.record_game(user_id, "draw")
        except Exception:
            pass
        res = f"╔════════════════════╗\n    ⚖️ <b>DOUBLE НИЧЬЯ! ({p_sum} = {b_sum})</b> ⚖️\n╚════════════════════╝\n\n💰 Ставка <b>{bet} 💰</b> возвращена!"
        await send_game_result(message, IMG_DRAW, res)


@dp.message(Command("over"))
async def cmd_over(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка: <b>100 💰</b>!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>БОЛЬШЕ (4-6)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = int(dice_msg.dice.value)

    if val in [4, 5, 6]:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, message.from_user.full_name)}\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
        await send_game_result(message, IMG_WIN, res)
    else:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception:
            pass
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, message.from_user.full_name)}\n📉 Потеряно: <b>-{bet} 💰</b>"
        await send_game_result(message, IMG_LOSS, res)


@dp.message(Command("under"))
async def cmd_under(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка: <b>100 💰</b>!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>МЕНЬШЕ (1-3)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = int(dice_msg.dice.value)

    if val in [1, 2, 3]:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, message.from_user.full_name)}\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
        await send_game_result(message, IMG_WIN, res)
    else:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception:
            pass
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, message.from_user.full_name)}\n📉 Потеряно: <b>-{bet} 💰</b>"
        await send_game_result(message, IMG_LOSS, res)


@dp.message(Command("even"))
async def cmd_even(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка: <b>100 💰</b>!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>ЧЁТНОЕ (2, 4, 6)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = int(dice_msg.dice.value)

    if val % 2 == 0:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ] (Чётное)\n👤 {get_mention(user_id, message.from_user.full_name)}\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
        await send_game_result(message, IMG_WIN, res)
    else:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception:
            pass
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ] (Нечётное)\n👤 {get_mention(user_id, message.from_user.full_name)}\n📉 Потеряно: <b>-{bet} 💰</b>"
        await send_game_result(message, IMG_LOSS, res)


@dp.message(Command("odd"))
async def cmd_odd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка: <b>100 💰</b>!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>НЕЧЁТНОЕ (1, 3, 5)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = int(dice_msg.dice.value)

    if val % 2 != 0:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ] (Нечётное)\n👤 {get_mention(user_id, message.from_user.full_name)}\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
        await send_game_result(message, IMG_WIN, res)
    else:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception:
            pass
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ] (Чётное)\n👤 {get_mention(user_id, message.from_user.full_name)}\n📉 Потеряно: <b>-{bet} 💰</b>"
        await send_game_result(message, IMG_LOSS, res)


# ================= ДУЭЛИ МЕЖДУ ИГРОКАМИ =================
@dp.message(Command("duel"))
async def cmd_duel(message: Message, command: CommandObject):
    await db.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(message.from_user.id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
        return await message.answer("❌ Ответьте этой командой на сообщение оппонента!")

    opponent = message.reply_to_message.from_user
    challenger = message.from_user

    if opponent.id == challenger.id:
        return await message.answer("❌ Нельзя играть с самим собой!")

    bet = 100
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet < 100:
        return await message.answer("❌ Минимальная ставка для дуэли: <b>100 💰</b>!")

    await db.register_user(opponent.id, opponent.full_name, opponent.username)

    c_data = await db.get_user(challenger.id)
    o_data = await db.get_user(opponent.id)

    if not c_data or c_data[2] < bet:
        return await message.answer("❌ У вас недостаточно монет!")
    if not o_data or o_data[2] < bet:
        return await message.answer("❌ У оппонента недостаточно монет!")

    duel_id = uuid.uuid4().hex[:8]

    active_duels[duel_id] = {
        "chat_id": message.chat.id,
        "challenger_id": challenger.id,
        "challenger_name": challenger.full_name,
        "opponent_id": opponent.id,
        "opponent_name": opponent.full_name,
        "bet": bet,
        "status": "pending"
    }

    text = (
        f"⚔️ <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"🔴 Вызывающий: {get_mention(challenger.id, challenger.full_name)}\n"
        f"🔵 Оппонент: {get_mention(opponent.id, opponent.full_name)}\n"
        f"💰 Ставка: <b>{bet} 💰</b> (Приз: <b>+{int(bet * 1.95)} 💰</b>)\n\n"
        f"<i>У оппонента 60 секунд на принятие.</i>"
    )

    duel_msg = await message.answer(text, reply_markup=duel_keyboard(duel_id), parse_mode="HTML")

    await asyncio.sleep(60)
    if duel_id in active_duels and active_duels[duel_id]["status"] == "pending":
        del active_duels[duel_id]
        try:
            await duel_msg.edit_text("⌛ <b>Время вызова истекло. Дуэль отменена.</b>", parse_mode="HTML")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("ac_"))
async def cb_accept_duel(call: CallbackQuery):
    duel_id = call.data.replace("ac_", "")
    if duel_id not in active_duels:
        return await call.answer("❌ Дуэль не найдена или уже завершилась!", show_alert=True)

    duel = active_duels[duel_id]
    if call.from_user.id != duel["opponent_id"]:
        return await call.answer("❌ Этот вызов брошен не вам!", show_alert=True)

    if not await check_subscription(call.from_user.id):
        return await call.answer("⚠️ Подпишитесь на наш канал, чтобы принять вызов!", show_alert=True)

    if duel["status"] != "pending":
        return await call.answer("Дуэль уже началась!", show_alert=True)

    duel["status"] = "in_progress"
    c_id, o_id, bet = duel["challenger_id"], duel["opponent_id"], duel["bet"]

    c_data = await db.get_user(c_id)
    o_data = await db.get_user(o_id)

    if not c_data or not o_data or c_data[2] < bet or o_data[2] < bet:
        del active_duels[duel_id]
        await call.message.edit_text("❌ Дуэль отменена: недостаточно средств!")
        return

    await db.change_balance(c_id, -bet)
    await db.change_balance(o_id, -bet)
    await db.add_turnover(c_id, bet)
    await db.add_turnover(o_id, bet)

    await call.message.edit_text(f"⚔️ <b>Дуэль началась!</b> Ставка: <b>{bet} 💰</b>", parse_mode="HTML")

    await call.message.answer(f"🔴 Бросает {get_mention(c_id, duel['challenger_name'])}:", parse_mode="HTML")
    c_dice = await call.message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    c_val = int(c_dice.dice.value)

    await call.message.answer(f"🔵 Бросает {get_mention(o_id, duel['opponent_name'])}:", parse_mode="HTML")
    o_dice = await call.message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    o_val = int(o_dice.dice.value)

    win_sum = int(bet * 1.95)

    if c_val > o_val:
        await db.change_balance(c_id, win_sum)
        await db.record_game(c_id, "win")
        await db.record_game(o_id, "loss")
        await db.process_referral_loss(o_id, bet)
        res = f"🏆 <b>ПОБЕДИТЕЛЬ:</b> {get_mention(c_id, duel['challenger_name'])} ({c_val})\n💀 <b>ПРОИГРАВШИЙ:</b> {get_mention(o_id, duel['opponent_name'])} ({o_val}) [ -{bet} 💰 ]\n\n💵 Выигрыш: <b>+{win_sum} 💰</b>"
        await send_game_result(call.message, IMG_WIN, res)
    elif o_val > c_val:
        await db.change_balance(o_id, win_sum)
        await db.record_game(o_id, "win")
        await db.record_game(c_id, "loss")
        await db.process_referral_loss(c_id, bet)
        res = f"🏆 <b>ПОБЕДИТЕЛЬ:</b> {get_mention(o_id, duel['opponent_name'])} ({o_val})\n💀 <b>ПРОИГРАВШИЙ:</b> {get_mention(c_id, duel['challenger_name'])} ({c_val}) [ -{bet} 💰 ]\n\n💵 Выигрыш: <b>+{win_sum} 💰</b>"
        await send_game_result(call.message, IMG_WIN, res)
    else:
        await db.change_balance(c_id, bet)
        await db.change_balance(o_id, bet)
        await db.record_game(c_id, "draw")
        await db.record_game(o_id, "draw")
        res = f"╔════════════════════╗\n      ⚖️ <b>БОЕВАЯ НИЧЬЯ!</b> ⚖️\n╚════════════════════╝\n\n💰 Ставки возвращены (+{bet} 💰 каждому)."
        await send_game_result(call.message, IMG_DRAW, res)

    del active_duels[duel_id]
    await call.answer()


@dp.callback_query(F.data.startswith("dc_"))
async def cb_decline_duel(call: CallbackQuery):
    duel_id = call.data.replace("dc_", "")
    if duel_id not in active_duels:
        return await call.answer("❌ Дуэль уже неактивна!", show_alert=True)

    duel = active_duels[duel_id]
    if call.from_user.id not in [duel["opponent_id"], duel["challenger_id"]]:
        return await call.answer("❌ Вы не участвуете в этой дуэли!", show_alert=True)

    del active_duels[duel_id]
    await call.message.edit_text("❌ <b>Дуэль была отклонена.</b>", parse_mode="HTML")
    await call.answer()


# ================= STARS ПОПОЛНЕНИЕ =================
@dp.message(Command("stars"))
@dp.message(Command("donate"))
async def cmd_stars(message: Message, command: CommandObject):
    stars_amount = 15
    if command.args and command.args.isdigit():
        stars_amount = int(command.args)

    coins_to_get = stars_amount * 10
    prices = [LabeledPrice(label=f"Пакет: {coins_to_get} монет", amount=stars_amount)]

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="⭐ Покупка монет",
        description=f"Приобретение {coins_to_get} игровых монет за {stars_amount} Telegram Stars.",
        payload=f"stars_deposit_{coins_to_get}",
        currency="XTR",
        prices=prices,
        start_parameter="stars-buy-coins"
    )


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("stars_deposit_"):
        coins = int(payload.replace("stars_deposit_", ""))
        await db.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
        await db.change_balance(message.from_user.id, coins)
        
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET has_deposited = TRUE, last_stars_deposit = CURRENT_TIMESTAMP WHERE user_id = $1",
                message.from_user.id
            )

        await message.answer(
            f"🎉 <b>Оплата успешна!</b>\n"
            f"⭐ Списано: <code>{message.successful_payment.total_amount} Stars</code>\n"
            f"💰 Зачислено: <b>+{coins} монет</b>\n"
            f"🔒 <i>Вывод доступен через 21 день (защита Telegram Stars Refund). Пополнения через <code>/gram</code> выводятся без ожидания.</i>",
            parse_mode="HTML"
        )


# ================= ТОПЫ И ПРОМОКОДЫ =================
@dp.message(Command("top"))
async def cmd_top(message: Message):
    top = await db.get_top(10)
    if not top:
        return await message.answer("Таблица лидеров пуста.")

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = "🏆 <b>ТОП-10 БОГАЧЕЙ БОТА:</b>\n\n"
    for i, row in enumerate(top, 1):
        place = medals.get(i, f"<b>{i}.</b>")
        name = row["username"]
        val = row["balance"]
        safe_name = html.escape(name or "Аноним")
        text += f"{place} {safe_name} — <code>{val} 💰</code>\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command("add_promo"))
async def cmd_add_promo(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return

    if not command.args or len(command.args.split()) < 3:
        return await message.answer("Формат: <code>/add_promo [КОД] [НАГРАДА] [КОЛ-ВО]</code>", parse_mode="HTML")

    code, reward_str, uses_str = command.args.split()
    if not reward_str.isdigit() or not uses_str.isdigit():
        return await message.answer("❌ Награда и количество должны быть числами!")

    ok = await db.create_promo(code, int(reward_str), int(uses_str))
    if ok:
        await message.answer(f"✅ Промокод <code>{code.upper()}</code> на <b>{reward_str} 💰</b> создан!", parse_mode="HTML")
    else:
        await message.answer("❌ Такой промокод уже существует!")


@dp.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer("Формат: <code>/promo [КОД]</code> (например: <code>/promo START</code>)", parse_mode="HTML")

    await db.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    _, msg = await db.activate_promo(message.from_user.id, command.args.strip())
    await message.answer(msg, parse_mode="HTML")


# ================= АДМИНИСТРИРОВАНИЕ И МОДЕРАЦИЯ =================
@dp.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return
    if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
        return await message.answer("❌ Ответьте на сообщение игрока для выдачи!")
    if not command.args:
        return await message.answer("❌ Укажите сумму: <code>/give 1000</code>", parse_mode="HTML")

    try:
        amount = int(command.args)
    except ValueError:
        return await message.answer("❌ Сумма должна быть числом!")

    target = message.reply_to_message.from_user
    await db.register_user(target.id, target.full_name, target.username)
    await db.change_balance(target.id, amount)

    verb = "выдал" if amount >= 0 else "забрал"
    await message.answer(f"👑 Администратор {verb} <b>{abs(amount)} 💰</b> у {get_mention(target.id, target.full_name)}!", parse_mode="HTML")


@dp.message(Command("add_admin"))
async def cmd_add_admin(message: Message):
    if not await db.is_admin(message.from_user.id):
        return
    if not message.reply_to_message:
        return await message.answer("❌ Ответьте этой командой на сообщение нового администратора.")

    target = message.reply_to_message.from_user
    await db.add_admin(target.id)
    await message.answer(f"✅ {get_mention(target.id, target.full_name)} назначен администратором бота!", parse_mode="HTML")


@dp.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return

    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target:
        return await message.answer("❌ Ответьте на сообщение нарушителя!")

    if target.id == OWNER_ID or await db.is_admin(target.id):
        return await message.answer("❌ Нельзя замутить администратора!")

    args = command.args.split(maxsplit=1) if command.args else []
    mins = 10
    reason = "Без причины"

    if args:
        if args[0].isdigit():
            mins = int(args[0])
            if len(args) > 1:
                reason = args[1]
        else:
            reason = command.args

    try:
        until = datetime.now() + timedelta(minutes=mins)
        await message.chat.restrict(
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await message.answer(f"🔇 {get_mention(target.id, target.full_name)} отправлен в мут на {mins} мин.\n📝 Причина: {reason}", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message):
    if not await db.is_admin(message.from_user.id) or not message.reply_to_message:
        return

    target = message.reply_to_message.from_user
    try:
        await message.chat.restrict(
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        await message.answer(f"🔊 {get_mention(target.id, target.full_name)} размучен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return

    target_id = None
    target_name = "Пользователь"

    if message.reply_to_message:
        target = message.reply_to_message.from_user
        target_id = target.id
        target_name = target.full_name
    elif command.args:
        arg = command.args.strip()
        if arg.isdigit():
            target_id = int(arg)
        else:
            target_id = await db.get_user_id_by_username(arg)
            if not target_id:
                return await message.answer(f"❌ Пользователь <code>{arg}</code> не найден в БД!", parse_mode="HTML")

    if not target_id:
        return await message.answer("Использование: <code>/ban @username</code> или ответом на сообщение.", parse_mode="HTML")

    if target_id == OWNER_ID or await db.is_admin(target_id):
        return await message.answer("❌ Нельзя наказать администратора!")

    try:
        await message.chat.ban(user_id=target_id)
        await message.answer(f"🛑 {get_mention(target_id, target_name)} забанен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка при бане: {e}")


@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return

    target_id = None
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
    elif command.args:
        arg = command.args.strip()
        if arg.isdigit():
            target_id = int(arg)
        else:
            target_id = await db.get_user_id_by_username(arg)
            if not target_id:
                return await message.answer(f"❌ Пользователь с тегом <code>{arg}</code> не найден в базе данных!", parse_mode="HTML")

    if not target_id:
        return await message.answer("Использование: <code>/unban @username</code> или <code>/unban 12345678</code>", parse_mode="HTML")

    try:
        await message.chat.unban(user_id=target_id, only_if_banned=True)
        await message.answer(f"✅ Пользователь (ID: <code>{target_id}</code>) успешно разбанен в чате!", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка разбана (проверьте права бота): {e}")


@dp.message(Command("warn"))
async def cmd_warn(message: Message):
    if not await db.is_admin(message.from_user.id):
        return

    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target:
        return await message.answer("❌ Ответьте на сообщение нарушителя!")

    if target.id == OWNER_ID or await db.is_admin(target.id):
        return await message.answer("❌ Нельзя выдать варн администратору!")

    await db.register_user(target.id, target.full_name, target.username)
    warns = await db.add_warn(target.id)

    if warns >= 3:
        try:
            await message.chat.ban(user_id=target.id)
            await db.reset_warns(target.id)
            await message.answer(f"🛑 {get_mention(target.id, target.full_name)} набрал <b>3/3 варнов</b> и получил бан!", parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка при бане: {e}")
    else:
        await message.answer(f"⚠️ {get_mention(target.id, target.full_name)} получил варн (<b>{warns}/3</b>)!", parse_mode="HTML")


@dp.message(Command("unwarn"))
async def cmd_unwarn(message: Message):
    if not await db.is_admin(message.from_user.id) or not message.reply_to_message:
        return
    target = message.reply_to_message.from_user
    await db.reset_warns(target.id)
    await message.answer(f"✅ Предупреждения игрока {get_mention(target.id, target.full_name)} аннулированы.", parse_mode="HTML")


# ================= ЗАПУСК =================
async def on_startup(bot: Bot):
    await db.init()
    
    commands = [
        BotCommand(command="start", description="Главное меню 🎲"),
        BotCommand(command="dice", description="Кубик против бота (от 100 💰) 🤖"),
        BotCommand(command="doubledice", description="2 кубика (x3 за дубль) 🎲🎲"),
        BotCommand(command="ladder", description="Кубическая лесенка до x7.5 🚀"),
        BotCommand(command="duel", description="Дуэль 1v1 в чате ⚔️"),
        BotCommand(command="clan", description="Мой клан 🛡"),
        BotCommand(command="create_clan", description="Создать клан (2500 💰) 🚩"),
        BotCommand(command="clan_top", description="Топ кланов 🏰"),
        BotCommand(command="over", description="Больше (4-6) 📈"),
        BotCommand(command="under", description="Меньше (1-3) 📉"),
        BotCommand(command="even", description="Чётное число ⚖️"),
        BotCommand(command="odd", description="Нечётное число 🎲"),
        BotCommand(command="gram", description="Пополнить через Gram / TON 💎"),
        BotCommand(command="stars", description="Пополнить за Stars ⭐"),
        BotCommand(command="withdraw", description="Вывод в Telegram Stars ⭐"),
        BotCommand(command="check", description="Раздать чек в чат 🧧"),
        BotCommand(command="rain", description="Денежный дождь в чат 🌧"),
        BotCommand(command="profile", description="Мой профиль и баланс 👤"),
        BotCommand(command="ref", description="Реферальная ссылка (+3%) 🤝"),
        BotCommand(command="pay", description="Передать монеты 💸"),
        BotCommand(command="top", description="Топ богачей 🏆"),
        BotCommand(command="promo", description="Активировать промокод 🎟"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logging.warning(f"Ошибка регистрации команд: {e}")

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        logging.info(f"Установка Webhook: {webhook_url}")
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
    else:
        logging.info("RENDER_EXTERNAL_URL не задан, запуск в локальном режиме.")


def main():
    if RENDER_EXTERNAL_URL:
        app = web.Application()
        webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_handler.register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        app.on_startup.append(lambda app: on_startup(bot))
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:
        async def run_polling():
            await db.init()
            await bot.delete_webhook(drop_pending_updates=True)
            logging.info("🚀 Запуск в режиме Polling...")
            await dp.start_polling(bot)

        asyncio.run(run_polling())


if __name__ == "__main__":
    main()