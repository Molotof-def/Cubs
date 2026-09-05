import os
import asyncio
import logging
import html
import uuid
import re
import random
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatPermissions,
    BotCommand,
    TelegramObject,
    ChatMemberOwner,
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberRestricted
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("❌ ОШИБКА: Токен бота не найден в переменных окружения (BOT_TOKEN)!")

DEV_ID = 5103088337       # Главный разработчик
CREATOR_IDS = {2053035323}  # Создатель бота

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    exit("❌ ОШИБКА: DATABASE_URL не найден! Добавьте подключение к PostgreSQL.")

REQUIRED_CHANNEL_RAW = os.getenv("REQUIRED_CHANNEL", "@DuelCubesChannel").strip()
if "t.me/" in REQUIRED_CHANNEL_RAW:
    REQUIRED_CHANNEL_RAW = "@" + REQUIRED_CHANNEL_RAW.split("t.me/")[-1].strip("/")
if REQUIRED_CHANNEL_RAW and not REQUIRED_CHANNEL_RAW.startswith("@") and not REQUIRED_CHANNEL_RAW.startswith("-100"):
    REQUIRED_CHANNEL_RAW = f"@{REQUIRED_CHANNEL_RAW}"
REQUIRED_CHANNEL = REQUIRED_CHANNEL_RAW

SPONSOR_CHANNEL_RAW = os.getenv("SPONSOR_CHANNEL", "@GrupaGoev").strip()
if "t.me/" in SPONSOR_CHANNEL_RAW:
    SPONSOR_CHANNEL_RAW = "@" + SPONSOR_CHANNEL_RAW.split("t.me/")[-1].strip("/")
if SPONSOR_CHANNEL_RAW and not SPONSOR_CHANNEL_RAW.startswith("@") and not SPONSOR_CHANNEL_RAW.startswith("-100"):
    SPONSOR_CHANNEL_RAW = f"@{SPONSOR_CHANNEL_RAW}"
SPONSOR_CHANNEL = SPONSOR_CHANNEL_RAW

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

IMG_WIN = "https://raw.githubusercontent.com/Molotof-def/Cubs/main/win.jpg"
IMG_LOSS = "https://raw.githubusercontent.com/Molotof-def/Cubs/main/lose.jpg"
IMG_DRAW = "https://raw.githubusercontent.com/Molotof-def/Cubs/main/draw.jpg"

MOTIVATIONAL_QUOTES = [
    "🔥 <i>«Тот, кто никогда не падал, никогда не поднимался. Сделай паузу и верни своё!»</i>",
    "💪 <i>«Серия неудач — это лишь разбег перед крупным триумфом. Главное — холодная голова.»</i>",
    "🛡 <i>«Опыт строится на ошибках. Кубики переменчивы, но мастерство остаётся!»</i>",
    "⚡ <i>«Фортуна любит терпеливых. Удача обязательно вернется в следующем раунде!»</i>",
    "🧠 <i>«Не поддавайся тильту! Поставь правильную цель, распредели банк и забери куш.»</i>"
]

WORK_TASKS = [
    "доставил секретный груз заказчику",
    "завершил крупный фриланс-проект",
    "выиграл престижный турнир по киберспорту",
    "нашёл золотой самородок в заброшенной шахте",
    "продал редкий 3D-ассет на маркетплейсе",
    "починил серверную стойку дата-центра",
    "провёл успешный стрим и собрал кучу донатов",
    "собрал кастомный игровой ПК под заказ"
]

QUIZ_WORDS = [
    "кубики", "джекпот", "дуэль", "победитель", "лесенка",
    "фортуна", "азарт", "баланс", "богач", "крипта", "монеты"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

active_ladders: Dict[int, dict] = {}
active_checks: Dict[str, dict] = {}
active_quizzes: Dict[int, dict] = {}
pending_confirmations: Dict[str, dict] = {}
known_groups: set = set()
chat_recent_users: Dict[int, List[int]] = {}
user_loss_streaks: Dict[int, int] = {}
user_last_action: Dict[int, float] = {}


def fmt_num(val) -> str:
    try:
        clean_int = int(round(float(val)))
        return f"{clean_int:,}".replace(",", " ")
    except Exception:
        return "0"


def get_mention(user_id: int, name: Optional[str]) -> str:
    safe_name = html.escape(name or "Игрок")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def parse_amount_string(val_str: Optional[str], current_balance: int = 0) -> Optional[int]:
    if not val_str:
        return None
    
    clean = val_str.strip().lower().replace(",", ".").replace(" ", "")
    allin_aliases = ["вабанк", "ва-банк", "все", "всё", "all", "full", "фулл", "фул", "макс", "max", "оллин", "all-in"]
    if clean in allin_aliases:
        return max(0, int(current_balance))

    match = re.match(r"^(\d+(?:\.\d+)?)\s*([a-zA-Zа-яА-Я]*)$", clean)
    if not match:
        return None

    num_part = float(match.group(1))
    suffix = match.group(2)

    if not suffix:
        return int(num_part)
    elif suffix in ["к", "k", "тыс", "тысяч", "тысячи", "тыща"]:
        return int(num_part * 1_000)
    elif suffix in ["кк", "kk", "м", "m", "млн", "миллион", "миллиона", "миллионов", "лям", "ляма", "лямов"]:
        return int(num_part * 1_000_000)
    elif suffix in ["ккк", "kkk", "b", "млрд", "миллиард", "миллиарда", "миллиардов"]:
        return int(num_part * 1_000_000_000)
    
    return None


def replay_keyboard(game_type: str, bet: int, user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔄 Реванш ({fmt_num(bet)} 💰)", callback_data=f"rep_{game_type}_{bet}_{user_id}")
    return builder.as_markup()


def confirm_bet_keyboard(conf_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить ставку", callback_data=f"conf_ok_{conf_id}")
    builder.button(text="❌ Отмена", callback_data=f"conf_no_{conf_id}")
    builder.adjust(2)
    return builder.as_markup()


def report_admin_keyboard(target_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔇 Мут 30 мин", callback_data=f"adm_mute_{target_id}_1800")
    builder.button(text="⚠️ Варн", callback_data=f"adm_warn_{target_id}")
    builder.button(text="🛑 Бан", callback_data=f"adm_ban_{target_id}")
    builder.adjust(3)
    return builder.as_markup()


def top_menu_keyboard(current_tab: str = "balance"):
    builder = InlineKeyboardBuilder()
    b_text = "💰 Баланс 🟢" if current_tab == "balance" else "💰 Баланс"
    t_text = "🔄 Оборот 🟢" if current_tab == "turnover" else "🔄 Оборот"
    w_text = "🏆 Победы 🟢" if current_tab == "wins" else "🏆 Победы"
    r_text = "🎯 Винрейт 🟢" if current_tab == "winrate" else "🎯 Винрейт"

    builder.button(text=b_text, callback_data="top_tab_balance")
    builder.button(text=t_text, callback_data="top_tab_turnover")
    builder.button(text=w_text, callback_data="top_tab_wins")
    builder.button(text=r_text, callback_data="top_tab_winrate")
    builder.adjust(2, 2)
    return builder.as_markup()


async def check_channel_member(user_id: int, channel_target: str) -> bool:
    if not channel_target or channel_target.lower() in ["none", "null", "", "@none", "@null"]:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel_target, user_id=user_id)
        if isinstance(member, (ChatMemberOwner, ChatMemberAdministrator, ChatMemberMember)):
            return True
        if isinstance(member, ChatMemberRestricted):
            return getattr(member, "is_member", False)
        status_str = str(getattr(member, "status", "")).lower()
        return status_str in ["creator", "administrator", "member"]
    except Exception as e:
        logging.warning(f"Ошибка проверки подписки {user_id} в {channel_target}: {e}")
        return False


async def check_subscription(user_id: int) -> bool:
    return await check_channel_member(user_id, REQUIRED_CHANNEL)


def sub_keyboard(target_user_id: int):
    builder = InlineKeyboardBuilder()
    clean_ch = REQUIRED_CHANNEL.replace("@", "")
    ch_url = f"https://t.me/{clean_ch}" if not REQUIRED_CHANNEL.startswith("-100") else "https://t.me/"
    
    builder.button(text="📢 Подписаться на канал", url=ch_url)
    builder.button(text="🔄 Проверить подписку", callback_data=f"sub_chk_{target_user_id}")
    builder.adjust(1)
    return builder.as_markup()


def sponsor_keyboard(target_user_id: int):
    builder = InlineKeyboardBuilder()
    clean_tag = SPONSOR_CHANNEL.replace("@", "")
    channel_url = f"https://t.me/{clean_tag}" if not SPONSOR_CHANNEL.startswith("-100") else "https://t.me/"
    builder.button(text=f"📢 Спонсор {SPONSOR_CHANNEL}", url=channel_url)
    builder.button(text="🎁 Забрать 50 000 💰", callback_data=f"sps_chk_{target_user_id}")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data.startswith("sub_chk_"))
async def cb_recheck_sub(call: CallbackQuery):
    target_id_str = call.data.replace("sub_chk_", "")
    if target_id_str.isdigit() and call.from_user.id != int(target_id_str):
        return await call.answer("❌ Это не ваша кнопка проверки подписки!", show_alert=True)

    if await check_subscription(call.from_user.id):
        try:
            await call.message.edit_text("✅ <b>Подписка подтверждена!</b> Теперь вам доступны все функции и игры.", parse_mode="HTML")
        except Exception:
            await call.answer("✅ Подписка подтверждена!", show_alert=True)
    else:
        await call.answer(f"❌ Вы ещё не подписались на {REQUIRED_CHANNEL}!", show_alert=True)


async def safe_reply(message: Message, text: str, reply_markup=None):
    try:
        return await message.reply(text=text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logging.warning(f"Ошибка HTML-парсера ({e}), отправка обычным текстом...")
        clean_text = re.sub(r'<[^>]+>', '', text)
        return await message.reply(text=clean_text, reply_markup=reply_markup)


async def send_game_result(message: Message, result_type: str, caption: str, user_id: Optional[int] = None, game_type: Optional[str] = None, bet: Optional[int] = None, reply_markup=None):
    banners = {
        "win": "🏆 <b>ПОБЕДА!</b>\n\n",
        "loss": "💀 <b>ПОРАЖЕНИЕ!</b>\n\n",
        "draw": "⚖️ <b>НИЧЬЯ!</b>\n\n"
    }
    
    quote_text = ""
    if user_id is not None:
        if result_type == "win":
            user_loss_streaks[user_id] = 0
        elif result_type == "loss":
            user_loss_streaks[user_id] = user_loss_streaks.get(user_id, 0) + 1
            if user_loss_streaks[user_id] >= 3:
                quote_text = f"\n\n💬 <b>Слова поддержки:</b>\n{random.choice(MOTIVATIONAL_QUOTES)}"

    full_caption = banners.get(result_type, "") + caption + quote_text
    img_map = {"win": IMG_WIN, "loss": IMG_LOSS, "draw": IMG_DRAW}
    photo_url = img_map.get(result_type)

    final_markup = reply_markup
    if not final_markup and game_type and bet and user_id:
        final_markup = replay_keyboard(game_type, bet, user_id)

    if photo_url:
        try:
            await message.reply_photo(
                photo=photo_url,
                caption=full_caption,
                parse_mode="HTML",
                reply_markup=final_markup
            )
            return
        except Exception as e:
            logging.warning(f"Ошибка отправки фото ({e}), переключение на текст.")

    await safe_reply(message, full_caption, reply_markup=final_markup)


def format_duration(seconds: int) -> str:
    if seconds >= 86400:
        days = seconds // 86400
        return f"{days} дн."
    elif seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} ч."
    elif seconds >= 60:
        mins = seconds // 60
        return f"{mins} мин."
    return f"{seconds} сек."


# ================= БАЗА ДАННЫХ =================
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
                    custom_nick TEXT DEFAULT NULL,
                    tg_username TEXT,
                    referrer_id BIGINT DEFAULT NULL,
                    balance BIGINT DEFAULT 10000 CHECK (balance >= 0),
                    turnover BIGINT DEFAULT 0,
                    wins INT DEFAULT 0,
                    losses INT DEFAULT 0,
                    draws INT DEFAULT 0,
                    warns INT DEFAULT 0,
                    last_work_time TIMESTAMP DEFAULT NULL,
                    sponsor_bonus_claimed BOOLEAN DEFAULT FALSE,
                    claimed_promos TEXT[] DEFAULT ARRAY[]::TEXT[],
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_nick TEXT DEFAULT NULL;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS claimed_promos TEXT[] DEFAULT ARRAY[]::TEXT[];

                CREATE TABLE IF NOT EXISTS chat_admins (
                    chat_id BIGINT,
                    user_id BIGINT,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id BIGINT,
                    user_id BIGINT,
                    msg_count BIGINT DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS chat_rules (
                    chat_id BIGINT PRIMARY KEY,
                    rules TEXT,
                    updated_by TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS active_duels (
                    duel_id TEXT PRIMARY KEY,
                    chat_id BIGINT,
                    challenger_id BIGINT,
                    challenger_name TEXT,
                    opponent_id BIGINT,
                    opponent_name TEXT,
                    bet BIGINT,
                    comment TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

    async def register_user(self, user_id: int, username: str, tg_username: Optional[str] = None, referrer_id: Optional[int] = None, chat_id: Optional[int] = None):
        clean_tag = tg_username.replace("@", "").lower() if tg_username else None
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, tg_username, referrer_id, balance) 
                VALUES ($1, $2, $3, $4, 10000)
                ON CONFLICT (user_id) DO UPDATE SET 
                    username = EXCLUDED.username,
                    tg_username = COALESCE(EXCLUDED.tg_username, users.tg_username)
            """, user_id, username, clean_tag, referrer_id)

            if chat_id:
                await conn.execute("""
                    INSERT INTO chat_members (chat_id, user_id, msg_count)
                    VALUES ($1, $2, 0)
                    ON CONFLICT (chat_id, user_id) DO NOTHING
                """, chat_id, user_id)

    async def set_custom_nick(self, user_id: int, nick: Optional[str]):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET custom_nick = $1 WHERE user_id = $2", nick, user_id)

    async def increment_message_count(self, chat_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO chat_members (chat_id, user_id, msg_count)
                VALUES ($1, $2, 1)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET 
                    msg_count = chat_members.msg_count + 1
            """, chat_id, user_id)

    async def get_top_messages(self, chat_id: int, limit: int = 10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT u.user_id, u.username, u.custom_nick, cm.msg_count 
                FROM chat_members cm
                JOIN users u ON cm.user_id = u.user_id
                WHERE cm.chat_id = $1 AND cm.msg_count > 0
                ORDER BY cm.msg_count DESC
                LIMIT $2
            """, chat_id, limit)

    async def get_user_id_by_username(self, tg_username: str):
        clean_tag = tg_username.replace("@", "").lower().strip()
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT user_id FROM users WHERE LOWER(tg_username) = $1", clean_tag)

    async def get_all_user_ids(self) -> List[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users")
            return [r["user_id"] for r in rows]

    async def get_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, username, balance, turnover, wins, losses, draws, warns, referrer_id, last_work_time, sponsor_bonus_claimed, created_at, tg_username, custom_nick, claimed_promos FROM users WHERE user_id = $1",
                user_id
            )
            return list(row) if row else None

    async def claim_promo(self, user_id: int, code: str, reward: int) -> bool:
        async with self.pool.acquire() as conn:
            claimed = await conn.fetchval("SELECT $1 = ANY(claimed_promos) FROM users WHERE user_id = $2", code, user_id)
            if claimed:
                return False
            await conn.execute("""
                UPDATE users 
                SET balance = balance + $1, 
                    claimed_promos = array_append(claimed_promos, $2) 
                WHERE user_id = $3
            """, reward, code, user_id)
            return True

    async def change_balance(self, user_id: int, amount: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", int(amount), user_id)

    async def update_work_time(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_work_time = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)

    async def set_sponsor_claimed(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET sponsor_bonus_claimed = TRUE WHERE user_id = $1", user_id)

    async def add_turnover(self, user_id: int, amount: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET turnover = turnover + $1 WHERE user_id = $2", abs(int(amount)), user_id)

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
                            text=f"🤝 <b>Реферальный бонус!</b>\nВаш реферал сыграл на <code>{fmt_num(lost_amount)} 💰</code>. Вам начислено 3%: <b>+{fmt_num(reward)} 💰</b>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Ошибка реферальных: {e}")

    async def get_referrals_count(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", user_id) or 0

    async def get_top_custom(self, column: str, limit=10):
        if column not in ["balance", "turnover", "wins"]:
            column = "balance"
        async with self.pool.acquire() as conn:
            return await conn.fetch(f"SELECT user_id, username, custom_nick, {column} FROM users WHERE {column} > 0 ORDER BY {column} DESC LIMIT $1", limit)

    async def get_top_winrate(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT user_id, username, custom_nick, wins, losses, draws 
                FROM users 
                WHERE (wins + losses + draws) >= 5 
                ORDER BY (CAST(wins AS FLOAT) / (wins + losses + draws)) DESC 
                LIMIT $1
            """, limit)

    async def get_chat_stats(self, chat_id: int):
        async with self.pool.acquire() as conn:
            stats = await conn.fetchrow("""
                SELECT 
                    COUNT(u.user_id) as total_players,
                    COALESCE(SUM(u.balance), 0) as total_balance,
                    COALESCE(SUM(u.turnover), 0) as total_turnover,
                    COALESCE(SUM(u.wins), 0) as total_wins,
                    COALESCE(SUM(u.losses), 0) as total_losses
                FROM chat_members cm
                JOIN users u ON cm.user_id = u.user_id
                WHERE cm.chat_id = $1
            """, chat_id)
            
            top_player = await conn.fetchrow("""
                SELECT u.user_id, COALESCE(u.custom_nick, u.username) as username, u.balance
                FROM chat_members cm
                JOIN users u ON cm.user_id = u.user_id
                WHERE cm.chat_id = $1
                ORDER BY u.balance DESC
                LIMIT 1
            """, chat_id)
            return stats, top_player

    async def is_developer(self, user_id: int) -> bool:
        return user_id == DEV_ID

    async def can_give_money(self, user_id: int) -> bool:
        return user_id == DEV_ID or user_id in CREATOR_IDS

    async def is_creator(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        if user_id == DEV_ID or user_id in CREATOR_IDS:
            return True
        if chat_id and chat_id < 0:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if isinstance(member, ChatMemberOwner):
                    return True
            except Exception:
                pass
        return False

    async def is_admin(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        if user_id == DEV_ID or user_id in CREATOR_IDS:
            return True
        if chat_id and chat_id < 0:
            async with self.pool.acquire() as conn:
                res = await conn.fetchval(
                    "SELECT 1 FROM chat_admins WHERE user_id = $1 AND chat_id = $2",
                    user_id, chat_id
                )
                if res is not None:
                    return True
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if isinstance(member, (ChatMemberOwner, ChatMemberAdministrator)):
                    return True
            except Exception:
                pass
        return False

    async def add_chat_admin(self, chat_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO chat_admins (chat_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", chat_id, user_id)

    async def remove_chat_admin(self, chat_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM chat_admins WHERE chat_id = $1 AND user_id = $2", chat_id, user_id)

    async def get_chat_assigned_admins(self, chat_id: int) -> List[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM chat_admins WHERE chat_id = $1", chat_id)
            return [r["user_id"] for r in rows]

    async def add_warn(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET warns = warns + 1 WHERE user_id = $1", user_id)
            res = await conn.fetchval("SELECT warns FROM users WHERE user_id = $1", user_id)
            return res if res is not None else 1

    async def reset_warns(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET warns = 0 WHERE user_id = $1", user_id)

    async def set_rules(self, chat_id: int, rules: str, updated_by: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO chat_rules (chat_id, rules, updated_by, updated_at)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                ON CONFLICT (chat_id) DO UPDATE SET 
                    rules = EXCLUDED.rules,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = CURRENT_TIMESTAMP
            """, chat_id, rules, updated_by)

    async def get_rules(self, chat_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT rules, updated_by, updated_at FROM chat_rules WHERE chat_id = $1", chat_id)

    async def delete_rules(self, chat_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM chat_rules WHERE chat_id = $1", chat_id)

    async def create_duel(self, duel_id: str, chat_id: int, challenger_id: int, challenger_name: str, opponent_id: int, opponent_name: str, bet: int, comment: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO active_duels (duel_id, chat_id, challenger_id, challenger_name, opponent_id, opponent_name, bet, comment, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
            """, duel_id, chat_id, challenger_id, challenger_name, opponent_id, opponent_name, bet, comment)

    async def get_duel(self, duel_id: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM active_duels WHERE duel_id = $1", duel_id)

    async def delete_duel(self, duel_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM active_duels WHERE duel_id = $1", duel_id)

    async def update_duel_status(self, duel_id: str, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE active_duels SET status = $1 WHERE duel_id = $2", status, duel_id)

    async def cleanup_expired_duels(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM active_duels 
                WHERE created_at < NOW() - INTERVAL '2 minutes' AND status = 'pending'
            """)


db = Database(DATABASE_URL)


# ================= АНТИСПАМ И ПРИВЕТСТВИЕ =================
class ChatActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            chat = event.chat
            user = event.from_user

            if chat and chat.type in ["group", "supergroup"]:
                known_groups.add(chat.id)

                if event.new_chat_members:
                    for new_member in event.new_chat_members:
                        if not new_member.is_bot:
                            mention = get_mention(new_member.id, new_member.full_name)
                            welcome_text = (
                                f"👋 Добро пожаловать в чат, {mention}!\n\n"
                                f"🎲 Играй в кубики, соревнуйся в дуэлях и побеждай.\n"
                                f"💡 Напиши <code>/start</code> для меню или <code>правила</code> для чтения правил."
                            )
                            try:
                                await event.answer(welcome_text, parse_mode="HTML")
                            except Exception:
                                pass
                    return

                if event.left_chat_member:
                    return

                if user and not user.is_bot:
                    asyncio.create_task(db.increment_message_count(chat.id, user.id))

        if isinstance(event, (Message, CallbackQuery)) and event.from_user and not event.from_user.is_bot:
            user_id = event.from_user.id
            now = time.time()
            last = user_last_action.get(user_id, 0.0)

            if now - last < 0.8:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Подождите секунду...", show_alert=False)
                return
            user_last_action[user_id] = now

        if isinstance(event, Message) and event.from_user and not event.from_user.is_bot:
            chat_id = event.chat.id if event.chat and event.chat.type in ["group", "supergroup"] else None
            if chat_id:
                if chat_id not in chat_recent_users:
                    chat_recent_users[chat_id] = []
                if event.from_user.id not in chat_recent_users[chat_id]:
                    chat_recent_users[chat_id].append(event.from_user.id)
                    if len(chat_recent_users[chat_id]) > 50:
                        chat_recent_users[chat_id].pop(0)

            if db.pool:
                try:
                    await db.register_user(
                        event.from_user.id,
                        event.from_user.full_name,
                        event.from_user.username,
                        chat_id=chat_id
                    )
                except Exception:
                    pass

        return await handler(event, data)


dp.message.outer_middleware(ChatActivityMiddleware())
dp.callback_query.outer_middleware(ChatActivityMiddleware())


# ================= КЛАВИАТУРЫ =================
def duel_keyboard(duel_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="⚔️ Принять вызов", callback_data=f"ac_{duel_id}")
    builder.button(text="❌ Отклонить", callback_data=f"dc_{duel_id}")
    builder.adjust(2)
    return builder.as_markup()


def check_keyboard(check_id: str, remaining: int, total: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🎁 Забрать чек ({remaining}/{total})", callback_data=f"take_chk_{check_id}")
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


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
async def resolve_target_user(message: Message, args: List[str]) -> Tuple[Optional[int], str, List[str]]:
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return None, target.full_name, args
        t_data = await db.get_user(target.id)
        display_name = (t_data[13] if t_data and t_data[13] else target.full_name)
        return target.id, display_name, args

    if not args:
        return None, "Пользователь", args

    first_arg = args[0].strip()
    remaining_args = args[1:]

    if first_arg.isdigit() and len(first_arg) >= 6:
        t_id = int(first_arg)
        u_data = await db.get_user(t_id)
        t_name = (u_data[13] or u_data[1]) if u_data else f"ID {t_id}"
        return t_id, t_name, remaining_args

    if first_arg.startswith("@"):
        clean_tag = first_arg.replace("@", "")
        t_id = await db.get_user_id_by_username(clean_tag)
        if t_id:
            u_data = await db.get_user(t_id)
            t_name = (u_data[13] or u_data[1]) if u_data else f"@{clean_tag}"
            return t_id, t_name, remaining_args
        return None, f"@{clean_tag}", remaining_args

    return None, "Пользователь", args


# ================= ПРОФИЛЬ =================
async def process_profile_cmd(message: Message, args: List[str]):
    if not await check_subscription(message.from_user.id):
        return await safe_reply(message, f"⚠️ <b>Для использования бота необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(message.from_user.id))

    req_user_id = message.from_user.id
    target_id, target_name, _ = await resolve_target_user(message, args)
    view_user_id = target_id if target_id else req_user_id

    me = await bot.get_me()
    if view_user_id == me.id:
        return

    user = await db.get_user(view_user_id)
    if not user:
        if view_user_id == req_user_id:
            await db.register_user(view_user_id, message.from_user.full_name, message.from_user.username)
            user = await db.get_user(view_user_id)
        else:
            return await safe_reply(message, "❌ Пользователь не найден в базе данных!")

    u_id, name, balance, turnover, wins, losses, draws, warns, _, last_work, sponsor_claimed, reg_date, tg_u, custom_nick, _ = user

    total_games = wins + losses + draws
    winrate = round((wins / total_games * 100), 1) if total_games > 0 else 0
    display_name = custom_nick or name

    role_badge = "Игрок"
    if view_user_id == DEV_ID:
        role_badge = "🛠 Главный Разработчик"
    elif view_user_id in CREATOR_IDS:
        role_badge = "👑 Создатель"
    elif await db.is_admin(view_user_id, message.chat.id):
        role_badge = "🛡 Администратор"

    text = (
        f"┏ 👤 <b>Профиль:</b> {get_mention(view_user_id, display_name)}\n"
        f"┣ 🎖 <b>Статус:</b> <code>{role_badge}</code>\n"
        f"┣ 🆔 <b>ID:</b> <code>{view_user_id}</code>\n"
        f"┣ 💰 <b>Баланс:</b> <code>{fmt_num(balance)} 💰</code>\n"
        f"┣ 🔄 <b>Оборот:</b> <code>{fmt_num(turnover)} 💰</code>\n"
        f"┣ 🎮 <b>Всего игр:</b> <code>{total_games}</code>\n"
        f"┣ 🏆 <b>Побед:</b> <code>{wins}</code> | 💀 <b>Поражений:</b> <code>{losses}</code> | ⚖️ <b>Ничьих:</b> <code>{draws}</code>\n"
        f"┣ 📈 <b>Винрейт:</b> <code>{winrate}%</code>\n"
        f"┗ ⚠️ <b>Варны:</b> <code>{warns}/3</code>"
    )
    await safe_reply(message, text)


# ================= СОЗДАНИЕ БЕССРОЧНЫХ ЧЕКОВ КЕМ УГОДНО =================
async def process_create_check_cmd(message: Message, args: List[str]):
    user_id = message.from_user.id
    if message.chat.type not in ["group", "supergroup"]:
        return await safe_reply(message, "❌ Создание чеков доступно только в группах!")

    if len(args) < 2:
        return await safe_reply(message, "❌ Формат: <code>чек [общая_сумма] [кол-во_человек]</code>\n<i>Пример:</i> <code>чек 50к 5</code>")

    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    total_amount = parse_amount_string(args[0], user_bal)
    if total_amount is None or total_amount <= 0:
        return await safe_reply(message, "❌ Укажите корректную сумму чека (например: <code>чек 10к 5</code>)!")

    try:
        activations = int(args[1])
    except ValueError:
        return await safe_reply(message, "❌ Количество человек должно быть числом!")

    if activations < 1 or activations > 50:
        return await safe_reply(message, "❌ Количество активаций от 1 до 50 человек!")

    if total_amount < activations * 100:
        return await safe_reply(message, "❌ Минимальная сумма на человека: <b>100 💰</b>!")

    if user_bal < total_amount:
        return await safe_reply(message, f"❌ У вас недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>")

    await db.change_balance(user_id, -total_amount)

    check_id = uuid.uuid4().hex[:8]
    amount_per_user = total_amount // activations
    display_name = user[13] or message.from_user.full_name

    active_checks[check_id] = {
        "creator_id": user_id,
        "creator_name": display_name,
        "total_amount": total_amount,
        "amount_per_user": amount_per_user,
        "total_activations": activations,
        "remaining_activations": activations,
        "claimed_users": set()
    }

    text = (
        f"🎁 <b>РАЗДАЧА ЧЕКА В ЧАТЕ!</b>\n\n"
        f"👤 Создатель: {get_mention(user_id, display_name)}\n"
        f"💰 Общий банк: <b>{fmt_num(total_amount)} 💰</b>\n"
        f"💵 Каждый получит: <b>+{fmt_num(amount_per_user)} 💰</b>\n"
        f"👥 Осталось мест: <b>{activations}/{activations}</b>\n\n"
        f"<i>Нажмите кнопку ниже, чтобы забрать монеты! (Чек бессрочный)</i>"
    )
    await safe_reply(message, text, reply_markup=check_keyboard(check_id, activations, activations))


@dp.callback_query(F.data.startswith("take_chk_"))
async def cb_take_check(call: CallbackQuery):
    check_id = call.data.replace("take_chk_", "")
    user_id = call.from_user.id

    if check_id not in active_checks:
        return await call.answer("❌ Этот чек уже полностью разобран!", show_alert=True)

    chk = active_checks[check_id]

    if user_id == chk["creator_id"]:
        return await call.answer("❌ Вы не можете забрать свой собственный чек!", show_alert=True)

    if user_id in chk["claimed_users"]:
        return await call.answer("❌ Вы уже активировали этот чек!", show_alert=True)

    if not await check_subscription(user_id):
        return await call.answer(f"⚠️ Сначала подпишитесь на канал {REQUIRED_CHANNEL}!", show_alert=True)

    chk["claimed_users"].add(user_id)
    chk["remaining_activations"] -= 1
    reward = chk["amount_per_user"]

    await db.register_user(user_id, call.from_user.full_name, call.from_user.username)
    await db.change_balance(user_id, reward)

    await call.answer(f"🎉 Вы получили +{fmt_num(reward)} монет!", show_alert=True)

    if chk["remaining_activations"] <= 0:
        del active_checks[check_id]
        await call.message.edit_text(
            f"🎁 <b>ЧЕК ПОЛНОСТЬЮ РАЗОБРАН!</b>\n\n"
            f"👤 Создатель: {get_mention(chk['creator_id'], chk['creator_name'])}\n"
            f"💰 Всего роздано: <b>{fmt_num(chk['total_amount'])} 💰</b> на <b>{chk['total_activations']}</b> чел.",
            parse_mode="HTML"
        )
    else:
        rem = chk["remaining_activations"]
        tot = chk["total_activations"]
        try:
            await call.message.edit_reply_markup(reply_markup=check_keyboard(check_id, rem, tot))
        except Exception:
            pass


# ================= ВОРК =================
async def process_work_cmd(message: Message):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await safe_reply(message, f"⚠️ <b>Для работы необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))

    user = await db.get_user(user_id)
    if not user:
        return

    last_work = user[9]
    now = datetime.now()

    if not last_work:
        diff_seconds = 7200
    else:
        diff_seconds = (now - last_work).total_seconds()

    display_name = user[13] or message.from_user.full_name

    if diff_seconds < 7200:
        remaining = int(7200 - diff_seconds)
        hours = remaining // 3600
        mins = (remaining % 3600) // 60
        secs = remaining % 60
        time_left_str = f"{hours} ч. {mins} мин." if hours > 0 else f"{mins} мин. {secs} сек."
        return await safe_reply(
            message,
            f"⏳ {get_mention(user_id, display_name)}, вы недавно закончили смену!\n"
            f"Следующая зарплата доступна через: <b>{time_left_str}</b>\n\n"
            f"💼 <i>Зарплата копится каждый час (вплоть до 24 часов). Чем дольше смена — тем выше куш!</i>"
        )

    capped_seconds = min(diff_seconds, 86400)
    accumulated_hours = capped_seconds / 3600.0

    rate_per_hour = random.randint(18000, 24000)
    earned = int(rate_per_hour * accumulated_hours) + random.randint(5000, 15000)
    earned = max(35000, min(earned, 580000))

    task = random.choice(WORK_TASKS)

    await db.change_balance(user_id, earned)
    await db.update_work_time(user_id)

    formatted_time = format_duration(int(diff_seconds))
    is_max = " <i>(Достигнут максимум смены 24ч)</i>" if diff_seconds >= 86400 else ""

    await safe_reply(
        message,
        f"💼 <b>ЗАРПЛАТА ПОЛУЧЕНА!</b>\n\n"
        f"👤 Работник: {get_mention(user_id, display_name)}\n"
        f"🛠 Вы успешно {task}!\n"
        f"⏱ <b>Отработано времени:</b> {formatted_time}{is_max}\n"
        f"💵 Ставка: <code>~{fmt_num(rate_per_hour)} 💰/час</code>\n"
        f"💰 <b>Итого начислено: +{fmt_num(earned)} монет!</b>\n\n"
        f"⏰ <i>Следующая смена доступна через 2 часа.</i>"
    )


# ================= ГЛАВНОЕ МЕНЮ =================
async def process_start_cmd(message: Message, ref_arg: Optional[str] = None):
    ref_id = None
    if ref_arg and ref_arg.startswith("ref_"):
        raw_id = ref_arg.replace("ref_", "")
        if raw_id.isdigit() and int(raw_id) != message.from_user.id:
            ref_id = int(raw_id)

    await db.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username, ref_id)
    user = await db.get_user(message.from_user.id)
    balance = user[2] if user else 10000
    display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name

    text = (
        f"👑 <b>DUEL CUBES | ИГРОВОЙ КЛУБ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Игрок:</b> {get_mention(message.from_user.id, display_name)}\n"
        f"💰 <b>Баланс:</b> <code>{fmt_num(balance)} 💰</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💼 <b>Заработок монет:</b>\n"
        f"├ 🛠 <code>ворк</code> — забрать зарплату (раз в 2ч, копится до 24ч)\n"
        f"├ 🎁 <code>промик MUSORDROP</code> — бонус <b>+50 000 💰</b>\n"
        f"└ 📢 <code>бонус спонсора</code> — подарок <b>+50 000 💰</b>\n\n"
        f"🎲 <b>Список игровых режимов:</b>\n"
        f"<blockquote expandable>"
        f"🎁 <code>чек [сумма] [кол-во]</code> — раздача чека (бессрочный)\n"
        f"⚔️ <code>дуэль [ставка] @username</code> — дуэль 1v1\n"
        f"🎲 <code>кубик [ставка/3кк/вабанк]</code> — 1 кубик против бота\n"
        f"🎲🎲 <code>кубы [ставка/3кк/вабанк]</code> — 2 кубика (х3.0 за дубль!)\n"
        f"🚀 <code>лесенка [ставка/вабанк]</code> — Лесенка (до x7.5)\n"
        f"📈 <code>больше [ставка/вабанк]</code> — Числа 4, 5, 6\n"
        f"📉 <code>меньше [ставка/вабанк]</code> — Числа 1, 2, 3\n"
        f"⚖️ <code>четное [ставка/вабанк]</code> — Чётный кубик\n"
        f"🎯 <code>нечетное [ставка/вабанк]</code> — Нечётный кубик"
        f"</blockquote>\n\n"
        f"📊 <b>Навигация и профиль:</b>\n"
        f"👤 <code>ник [имя]</code> — установить ник как в Iris\n"
        f"👤 <code>профиль</code> | 🏆 <code>топ</code> | 💬 <code>топ сообщений</code>\n"
        f"📜 <code>правила</code> | 👥 <code>список админов</code>\n"
        f"🤝 <code>реф</code> — партнерка 3% | 💸 <code>перевод [сумма] @username</code>"
    )
    await safe_reply(message, text)


async def process_chat_stats_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await safe_reply(message, "❌ Статистика чата доступна только в группах!")

    if not await check_subscription(message.from_user.id):
        return await safe_reply(message, f"⚠️ <b>Для использования бота необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(message.from_user.id))

    await db.register_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
        chat_id=message.chat.id
    )

    stats, top_player = await db.get_chat_stats(message.chat.id)
    if not stats or stats["total_players"] == 0:
        return await safe_reply(message, "📊 В этом чате пока нет зарегистрированных игроков.")

    total_games = int(stats["total_wins"]) + int(stats["total_losses"])
    winrate = round((stats["total_wins"] / total_games * 100), 1) if total_games > 0 else 0

    top_text = "<i>Пока нет</i>"
    if top_player and top_player["user_id"]:
        top_text = f"{get_mention(top_player['user_id'], top_player['username'])} (<code>{fmt_num(top_player['balance'])} 💰</code>)"

    text = (
        f"📊 <b>ИГРОВАЯ СТАТИСТИКА ЧАТА</b>\n"
        f"👥 Чат: <b>{html.escape(message.chat.title or 'Группа')}</b>\n\n"
        f"👤 Всего активных игроков: <b>{stats['total_players']}</b>\n"
        f"💰 Общий капитал игроков: <b>{fmt_num(stats['total_balance'])} 💰</b>\n"
        f"🔄 Суммарный оборот: <b>{fmt_num(stats['total_turnover'])} 💰</b>\n"
        f"🎮 Всего сыграно игр: <b>{total_games}</b>\n"
        f"🏆 Побед: <b>{stats['total_wins']}</b> | 💀 Поражений: <b>{stats['total_losses']}</b>\n"
        f"📈 Общий винрейт чата: <b>{winrate}%</b>\n\n"
        f"👑 <b>Богач чата:</b> {top_text}"
    )
    await safe_reply(message, text)


# ================= ДУЭЛЬ =================
async def process_duel_cmd(message: Message, args: List[str]):
    challenger = message.from_user
    await db.register_user(challenger.id, challenger.full_name, challenger.username)

    if not await check_subscription(challenger.id):
        return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(challenger.id))

    target_id, target_name = None, None
    bet_raw = None
    comment_parts = []

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return await safe_reply(message, "❌ Нельзя вызывать ботов на дуэль!")
        target_id = target.id
        t_data = await db.get_user(target.id)
        target_name = (t_data[13] if t_data and t_data[13] else target.full_name)
        if args:
            bet_raw = args[0]
            comment_parts = args[1:]
    else:
        if not args:
            return await safe_reply(message, "❌ Формат: <code>дуэль [ставка] @username</code> или ответом на сообщение.")
        
        remaining = []
        for arg in args:
            if not target_id and (arg.startswith("@") or (arg.isdigit() and len(arg) > 6 and int(arg) > 1000000)):
                clean_tag = arg.replace("@", "")
                if arg.isdigit():
                    t_id = int(arg)
                else:
                    t_id = await db.get_user_id_by_username(clean_tag)
                
                if t_id:
                    target_id = t_id
                    u_data = await db.get_user(t_id)
                    target_name = (u_data[13] or u_data[1]) if u_data else f"@{clean_tag}"
                else:
                    target_name = f"@{clean_tag}"
            elif not bet_raw and parse_amount_string(arg, 0) is not None:
                bet_raw = arg
            else:
                remaining.append(arg)
        comment_parts = remaining

    if not target_id:
        return await safe_reply(message, "❌ Укажите игрока через <code>@username</code> или ответьте на его сообщение!")

    if target_id == challenger.id:
        return await safe_reply(message, "❌ Нельзя играть с самим собой!")

    c_data = await db.get_user(challenger.id)
    c_bal = c_data[2] if c_data else 0
    c_name = c_data[13] or challenger.full_name

    bet = parse_amount_string(bet_raw, c_bal)
    if bet is None:
        return await safe_reply(message, "❌ Неверный формат ставки! Укажите число (например: <code>500</code>, <code>2кк</code>, <code>вабанк</code>).")

    if bet < 100:
        return await safe_reply(message, f"❌ Минимальная ставка для дуэли: <b>100 💰</b>! У вас: <code>{fmt_num(c_bal)} 💰</code>")

    o_data = await db.get_user(target_id)
    if not o_data:
        return await safe_reply(message, "❌ Выбранный игрок еще не зарегистрирован в боте!")

    if c_bal < bet:
        return await safe_reply(message, f"❌ У вас недостаточно монет! Баланс: <code>{fmt_num(c_bal)} 💰</code>")
    if o_data[2] < bet:
        return await safe_reply(message, f"❌ У оппонента недостаточно монет для этой ставки! Баланс оппонента: <code>{fmt_num(o_data[2])} 💰</code>")

    duel_id = uuid.uuid4().hex[:8]
    comment_str = " ".join(comment_parts).strip()

    await db.create_duel(
        duel_id=duel_id,
        chat_id=message.chat.id,
        challenger_id=challenger.id,
        challenger_name=c_name,
        opponent_id=target_id,
        opponent_name=target_name,
        bet=bet,
        comment=comment_str
    )

    is_allin = "🔥 <b>ALL-IN ВЫЗОВ (ВА-БАНК)!</b>\n" if bet == c_bal else ""
    comment_text = f"📝 <b>Комментарий:</b> <i>«{html.escape(comment_str)}»</i>\n" if comment_str else ""

    text = (
        f"⚔️ <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"{is_allin}"
        f"🔴 Вызывающий: {get_mention(challenger.id, c_name)}\n"
        f"🔵 Оппонент: {get_mention(target_id, target_name)}\n"
        f"💰 Ставка: <b>{fmt_num(bet)} 💰</b> (Приз: <b>+{fmt_num(int(bet * 1.9))} 💰</b>)\n"
        f"{comment_text}\n"
        f"⏱ <i>У оппонента 2 минуты на принятие (иначе авто-отмена).</i>"
    )

    duel_msg = await message.reply(text, reply_markup=duel_keyboard(duel_id), parse_mode="HTML")
    asyncio.create_task(duel_timeout_watcher(duel_id, duel_msg))


# ================= ПЕРЕВОД МОНЕТ =================
async def process_pay_cmd(message: Message, args: List[str]):
    sender = message.from_user
    if not await check_subscription(sender.id):
        return await safe_reply(message, f"⚠️ <b>Для переводов необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(sender.id))

    sender_data = await db.get_user(sender.id)
    sender_bal = sender_data[2] if sender_data else 0
    s_name = sender_data[13] or sender.full_name

    recipient_id, recipient_name = None, None
    amount_raw = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return await safe_reply(message, "❌ Нельзя переводить монеты ботам!")
        recipient_id = target.id
        t_data = await db.get_user(target.id)
        recipient_name = (t_data[13] if t_data and t_data[13] else target.full_name)
        amount_raw = args[0] if args else None
    else:
        if not args:
            return await safe_reply(message, "❌ Формат: <code>перевод [сумма/3кк] @username</code> или ответом на сообщение.")
        for arg in args:
            if arg.startswith("@") or (arg.isdigit() and len(arg) > 6 and int(arg) > 1000000):
                clean_tag = arg.replace("@", "")
                t_id = int(arg) if arg.isdigit() else await db.get_user_id_by_username(clean_tag)
                if t_id:
                    recipient_id = t_id
                    u_data = await db.get_user(t_id)
                    recipient_name = (u_data[13] or u_data[1]) if u_data else f"@{clean_tag}"
            else:
                amount_raw = arg

    if not recipient_id:
        return await safe_reply(message, "❌ Укажите получателя: <code>перевод 500 @username</code>")

    if recipient_id == sender.id:
        return await safe_reply(message, "❌ Нельзя переводить монеты самому себе!")

    amount = parse_amount_string(amount_raw, sender_bal)
    if amount is None or amount < 100:
        return await safe_reply(message, "❌ Минимальная сумма перевода: <b>100 💰</b>! (Пример: <code>перевод 50к</code>, <code>перевод 3кк</code>)")

    if sender_bal < amount:
        return await safe_reply(message, f"❌ Недостаточно монет для перевода! Ваш баланс: <code>{fmt_num(sender_bal)} 💰</code>")

    fee = max(1, int(amount * 0.05))
    received_amount = amount - fee

    await db.register_user(sender.id, sender.full_name, sender.username)
    await db.register_user(recipient_id, recipient_name)

    await db.change_balance(sender.id, -amount)
    await db.change_balance(recipient_id, received_amount)

    await safe_reply(
        message,
        f"💸 {get_mention(sender.id, s_name)} перевёл монеты игроку {get_mention(recipient_id, recipient_name)}!\n\n"
        f"💵 <b>Сумма перевода:</b> <code>{fmt_num(amount)} 💰</code>\n"
        f"🔥 <b>Комиссия банка (5% сгорело):</b> <code>-{fmt_num(fee)} 💰</code>\n"
        f"💰 <b>Получатель зачислил:</b> <b>+{fmt_num(received_amount)} 💰</b>"
    )


# ================= ИГРОВЫЕ РЕЖИМЫ =================
async def run_dice_game(message: Message, user_id: int, user_name: str, bet: int):
    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    if user_bal < bet:
        return await safe_reply(message, f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    display_name = user[13] or user_name
    is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if bet == user_bal else ""
    await safe_reply(message, f"{is_allin}🎲 Бросок {get_mention(user_id, display_name)} (Ставка: <b>{fmt_num(bet)} 💰</b>):")
    p_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    p_val = int(p_msg.dice.value)

    await message.answer("🤖 Бросок Бота:", parse_mode="HTML")
    b_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    b_val = int(b_msg.dice.value)

    if p_val > b_val:
        win = int(bet * 1.9)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        text = (
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"👤 {get_mention(user_id, display_name)}\n"
            f"💰 Коэффициент: <b>x1.9</b>\n"
            f"💵 Выигрыш: <b>+{fmt_num(win)} 💰</b>"
        )
        await send_game_result(message, "win", text, user_id=user_id, game_type="dice", bet=bet)
    elif p_val < b_val:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        text = (
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"👤 {get_mention(user_id, display_name)}\n"
            f"📉 Потеряно: <b>-{fmt_num(bet)} 💰</b>"
        )
        await send_game_result(message, "loss", text, user_id=user_id, game_type="dice", bet=bet)
    else:
        await db.change_balance(user_id, bet)
        await db.record_game(user_id, "draw")
        text = (
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"👤 {get_mention(user_id, display_name)}\n"
            f"💰 <b>Возврат ставки:</b> <code>+{fmt_num(bet)} 💰</code>"
        )
        await send_game_result(message, "draw", text, user_id=user_id, game_type="dice", bet=bet)


async def run_doubledice_game(message: Message, user_id: int, user_name: str, bet: int):
    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    if user_bal < bet:
        return await safe_reply(message, f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    display_name = user[13] or user_name
    is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if bet == user_bal else ""
    await safe_reply(message, f"{is_allin}🎲🎲 <b>Бросок двух кубиков {get_mention(user_id, display_name)} (Ставка: {fmt_num(bet)} 💰):</b>")
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
        mult = 3.0 if is_double else 1.9
        win = int(bet * mult)

        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")

        bonus_title = "🔥 <b>РЕДКИЙ МЕГА-ДУБЛЬ (x3.0)!</b>\n" if is_double else f"Коэффициент: <b>x{mult}</b>\n"
        res = (
            f"👤 {get_mention(user_id, display_name)}\n"
            f"🎲 Твои очки: {p1} + {p2} = <b>{p_sum}</b>\n"
            f"🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n"
            f"{bonus_title}💵 Выигрыш: <b>+{fmt_num(win)} 💰</b>"
        )
        await send_game_result(message, "win", res, user_id=user_id, game_type="doubledice", bet=bet)
    elif p_sum < b_sum:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        res = (
            f"👤 {get_mention(user_id, display_name)}\n"
            f"🎲 Твои очки: {p1} + {p2} = <b>{p_sum}</b>\n"
            f"🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n"
            f"📉 Потеряно: <b>-{fmt_num(bet)} 💰</b>"
        )
        await send_game_result(message, "loss", res, user_id=user_id, game_type="doubledice", bet=bet)
    else:
        await db.change_balance(user_id, bet)
        await db.record_game(user_id, "draw")
        res = (
            f"👤 {get_mention(user_id, display_name)}\n"
            f"🎲 Твои очки: {p1} + {p2} = <b>{p_sum}</b>\n"
            f"🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n"
            f"💰 <b>Ничья! Ставка {fmt_num(bet)} 💰 возвращена на баланс.</b>"
        )
        await send_game_result(message, "draw", res, user_id=user_id, game_type="doubledice", bet=bet)


async def run_simple_bet_game(message: Message, user_id: int, user_name: str, bet: int, game_type: str):
    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    if user_bal < bet:
        return await safe_reply(message, f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    type_titles = {
        "over": "БОЛЬШЕ (4-6)",
        "under": "МЕНЬШЕ (1-3)",
        "even": "ЧЁТНОЕ (2, 4, 6)",
        "odd": "НЕЧЁТНОЕ (1, 3, 5)"
    }

    display_name = user[13] or user_name
    is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if bet == user_bal else ""
    await safe_reply(message, f"{is_allin}🎲 {get_mention(user_id, display_name)} поставил <b>{fmt_num(bet)} 💰</b> на <b>{type_titles[game_type]}</b>:")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = int(dice_msg.dice.value)

    win_cond = False
    if game_type == "over" and val in [4, 5, 6]:
        win_cond = True
    elif game_type == "under" and val in [1, 2, 3]:
        win_cond = True
    elif game_type == "even" and (val % 2 == 0):
        win_cond = True
    elif game_type == "odd" and (val % 2 != 0):
        win_cond = True

    if win_cond:
        win = int(bet * 1.9)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🎲 Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, display_name)}\n💰 Множитель: <b>x1.9</b>\n💵 Выигрыш: <b>+{fmt_num(win)} 💰</b>"
        await send_game_result(message, "win", res, user_id=user_id, game_type=game_type, bet=bet)
    else:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        res = f"🎲 Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, display_name)}\n📉 Потеряно: <b>-{fmt_num(bet)} 💰</b>"
        await send_game_result(message, "loss", res, user_id=user_id, game_type=game_type, bet=bet)


# ================= ВСЕ ФОНОВЫЕ ВОРКЕРЫ (ОБЪЯВЛЕНЫ ДО STARTUP) =================
async def quiz_background_worker():
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.sleep(random.randint(1500, 2400))
            if not known_groups:
                continue

            target_chat_id = random.choice(list(known_groups))

            try:
                member_count = await bot.get_chat_member_count(target_chat_id)
                if member_count < 50:
                    continue
            except Exception as e:
                logging.warning(f"Не удалось получить участников {target_chat_id} ({e}), пропуск.")
                known_groups.discard(target_chat_id)
                continue

            reward = random.randint(25000, 60000)

            if random.random() < 0.5:
                target_word = random.choice(QUIZ_WORDS)
                active_quizzes[target_chat_id] = {"answer": target_word.lower(), "reward": reward}
                text = (
                    f"⚡️ <b>ВИКТОРИНА: БЫСТРЫЕ ПАЛЬЦЫ!</b>\n\n"
                    f"Напишите первым слово: <code>{target_word}</code>\n"
                    f"💰 Награда: <b>+{fmt_num(reward)} 💰</b> на баланс!"
                )
            else:
                a, b = random.randint(15, 99), random.randint(12, 88)
                active_quizzes[target_chat_id] = {"answer": str(a + b), "reward": reward}
                text = (
                    f"⚡️ <b>ВИКТОРИНА: МАТЕМАТИКА!</b>\n\n"
                    f"Решите первым пример: <b>{a} + {b} = ?</b>\n"
                    f"💰 Награда: <b>+{fmt_num(reward)} 💰</b> на баланс!"
                )

            try:
                await bot.send_message(chat_id=target_chat_id, text=text, parse_mode="HTML")
            except Exception:
                active_quizzes.pop(target_chat_id, None)

        except Exception as e:
            logging.error(f"Ошибка quiz worker: {e}")
            await asyncio.sleep(60)


async def db_cleanup_background_worker():
    await asyncio.sleep(120)
    while True:
        try:
            await db.cleanup_expired_duels()
            pending_confirmations.clear()
        except Exception as e:
            logging.error(f"Ошибка очистки базы данных: {e}")
        await asyncio.sleep(7200)


async def ladder_timeout_watcher(user_id: int, message_obj: Message):
    try:
        await asyncio.sleep(180)
        if user_id in active_ladders:
            game = active_ladders[user_id]
            step = game.get("step", 0)
            bet = game.get("bet", 100)
            del active_ladders[user_id]

            if step > 0:
                mult = LADDER_STEPS[step]
                win = int(bet * mult)
                await db.change_balance(user_id, win)
                await db.record_game(user_id, "win")
                try:
                    await message_obj.reply(
                        f"⏰ <b>Время игры в Лесенку истекло (3 мин)!</b>\n"
                        f"💰 Автоматически зафиксирован выигрыш на <b>Ступени {step}</b> (x{mult}): <b>+{fmt_num(win)} 💰</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            else:
                await db.change_balance(user_id, bet)
                try:
                    await message_obj.reply(
                        f"⏰ <b>Время игры в Лесенку истекло (3 мин)!</b>\n"
                        f"💰 Несыгранная ставка <b>{fmt_num(bet)} 💰</b> возвращена на ваш баланс.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except asyncio.CancelledError:
        pass


async def duel_timeout_watcher(duel_id: str, duel_msg: Message):
    try:
        await asyncio.sleep(120)
        duel = await db.get_duel(duel_id)
        if duel and duel["status"] == "pending":
            await db.delete_duel(duel_id)
            try:
                await duel_msg.edit_text("⌛ <b>Время ожидания дуэли истекло (2 мин). Игра автоматически отменена, деньги на месте!</b>", parse_mode="HTML")
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


# ================= СТРОГАЯ КОМАНДА РАССЫЛКИ =================
@dp.message(F.text.startswith("/broadcast") | F.text.startswith("/рассылка"))
async def cmd_broadcast_strict(message: Message):
    if not await db.can_give_money(message.from_user.id):
        return await safe_reply(message, "❌ Глобальная рассылка доступна только <b>Создателю и Разработчику</b>!")

    parts = message.text.strip().split(maxsplit=1)
    args_text = parts[1] if len(parts) > 1 else None

    broadcast_text = None
    if message.reply_to_message:
        broadcast_text = message.reply_to_message.text or message.reply_to_message.caption
    elif args_text:
        broadcast_text = args_text

    if not broadcast_text:
        return await safe_reply(message, "❌ Формат: <code>/рассылка [текст]</code> или ответом на сообщение.")

    user_ids = await db.get_all_user_ids()
    status_msg = await message.reply(f"📢 Начинаю рассылку для <b>{len(user_ids)}</b> игроков...", parse_mode="HTML")

    success = 0
    blocked = 0

    for u_id in user_ids:
        try:
            await bot.send_message(chat_id=u_id, text=broadcast_text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.04)
        except Exception:
            blocked += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📬 Доставлено: <b>{success}</b>\n"
        f"🚫 Заблокировали бота / Ошибка: <b>{blocked}</b>",
        parse_mode="HTML"
    )


# ================= ОБРАБОТЧИК ВСЕХ ТЕКСТОВЫХ КОМАНД =================
@dp.message(F.text)
async def handle_all_text_commands(message: Message):
    raw_text = message.text.strip()
    if not raw_text:
        return

    full_lower = raw_text.lower()
    chat_id = message.chat.id

    if chat_id in active_quizzes:
        quiz = active_quizzes[chat_id]
        if full_lower == quiz["answer"]:
            reward = quiz["reward"]
            del active_quizzes[chat_id]

            await db.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
            await db.change_balance(message.from_user.id, reward)
            user_data = await db.get_user(message.from_user.id)
            display_name = (user_data[13] or message.from_user.full_name) if user_data else message.from_user.full_name

            return await safe_reply(
                message,
                f"🎉 <b>ПОБЕДА В ВИКТОРИНЕ!</b>\n\n"
                f"👤 {get_mention(message.from_user.id, display_name)} ответил первым!\n"
                f"💰 Награда: <b>+{fmt_num(reward)} монет</b> зачислена на баланс."
            )

    parts = raw_text.split()
    if not parts:
        return

    first_word = parts[0].lower()
    cmd = first_word.lstrip("/").split("@")[0]
    args = parts[1:]

    # Ник
    if first_word == "ник":
        nick_val = raw_text[len(parts[0]):].strip()
        return await process_nick_cmd(message, nick_val)

    if full_lower in ["удалить ник", "сбросить ник", "снять ник"]:
        return await process_reset_nick_cmd(message)

    # Промокод
    if cmd in ["промо", "промик", "промокод", "promocode", "promo"]:
        code_val = args[0] if args else ""
        return await process_promo_cmd(message, code_val)

    # Топ сообщений
    if full_lower in ["топ сообщений", "топ соо", "топ сообщений чата", "топ_соо"]:
        return await process_top_messages_cmd(message)

    if full_lower.startswith("установить правила") or full_lower.startswith("поставить правила"):
        rules_content = raw_text.split(maxsplit=2)[2] if len(raw_text.split(maxsplit=2)) > 2 else ""
        if message.chat.type not in ["group", "supergroup"]:
            return await safe_reply(message, "❌ Правила можно устанавливать только в группах!")
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора для установки правил!")
        await db.set_rules(message.chat.id, rules_content.strip(), message.from_user.full_name)
        return await safe_reply(message, "✅ <b>Правила чата успешно установлены и сохранены!</b>")

    if full_lower in ["правила", "rules", "правила чата", "правила группы"]:
        if message.chat.type not in ["group", "supergroup"]:
            return await safe_reply(message, "❌ Команда доступна только в группах!")
        data = await db.get_rules(message.chat.id)
        if not data or not data["rules"]:
            return await safe_reply(message, "📜 <b>В этом чате ещё не установлены правила!</b>")
        return await safe_reply(message, f"📜 <b>ПРАВИЛА ЧАТА:</b>\n━━━━━━━━━━━━━━━━━━━━\n{html.escape(data['rules'])}\n━━━━━━━━━━━━━━━━━━━━")

    if full_lower in ["удалить правила", "сбросить правила"]:
        if message.chat.type not in ["group", "supergroup"]:
            return await safe_reply(message, "❌ Команда доступна только в группах!")
        if not await db.is_creator(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ Только Владелец чата или Разработчик может удалить правила!")
        await db.delete_rules(message.chat.id)
        return await safe_reply(message, "🗑 <b>Правила чата были полностью удалены.</b>")

    if full_lower in ["список админов", "админы", "состав", "список_админов", "admins", "администрация"]:
        return await process_admins_list_cmd(message)

    if full_lower in ["стата чата", "статистика чата", "стата_чата", "чат стата", "чат статистика", "топ чата"]:
        return await process_chat_stats_cmd(message)

    if full_lower in ["бонус спонсора", "спонсор бонус", "бонус_спонсора", "спонсор"]:
        return await process_sponsor_cmd(message)

    if cmd in ["start", "старт", "меню", "menu", "помощь", "help", "инфо"]:
        ref_arg = args[0] if args else None
        return await process_start_cmd(message, ref_arg)
    
    if cmd in ["бот", "bot"] and len(parts) <= 2:
        return await process_start_cmd(message)

    elif cmd in ["topmsg", "topmessages"]:
        return await process_top_messages_cmd(message)

    elif cmd in ["admins", "списокадминов"]:
        return await process_admins_list_cmd(message)

    elif cmd in ["setrules", "установитьправила"]:
        rules_content = " ".join(args)
        if message.chat.type not in ["group", "supergroup"]:
            return await safe_reply(message, "❌ Правила доступны только в группах!")
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        await db.set_rules(message.chat.id, rules_content.strip(), message.from_user.full_name)
        return await safe_reply(message, "✅ <b>Правила успешно установлены!</b>")

    elif cmd in ["rules"]:
        data = await db.get_rules(message.chat.id)
        if not data or not data["rules"]:
            return await safe_reply(message, "📜 <b>В этом чате ещё не установлены правила!</b>")
        return await safe_reply(message, f"📜 <b>ПРАВИЛА ЧАТА:</b>\n━━━━━━━━━━━━━━━━━━━━\n{html.escape(data['rules'])}\n━━━━━━━━━━━━━━━━━━━━")

    elif cmd in ["delrules", "удалитьправила"]:
        if not await db.is_creator(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ Только Владелец чата или Разработчик может удалить правила!")
        await db.delete_rules(message.chat.id)
        return await safe_reply(message, "🗑 <b>Правила удалены.</b>")

    elif cmd in ["addadmin", "добавитьадмина", "датьадмина"]:
        if message.chat.type not in ["group", "supergroup"]:
            return await safe_reply(message, "❌ Назначение администраторов доступно только в группах!")
        if not await db.is_creator(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ Назначать администраторов может только <b>Владелец чата</b> или <b>Разработчик</b>!")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Укажите игрока: <code>/addadmin @username</code>")
        await db.add_chat_admin(message.chat.id, target_id)
        return await safe_reply(message, f"👑 {get_mention(target_id, target_name)} назначен <b>Администратором этого чата</b>!")

    elif cmd in ["deladmin", "удалитьадмина", "снятадмина"]:
        if message.chat.type not in ["group", "supergroup"]:
            return await safe_reply(message, "❌ Снятие администраторов доступно только в группах!")
        if not await db.is_creator(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ Снимать администраторов может только <b>Владелец чата</b> или <b>Разработчик</b>!")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Укажите игрока: <code>/deladmin @username</code>")
        if target_id == DEV_ID or target_id in CREATOR_IDS:
            return await safe_reply(message, "❌ Нельзя снять Создателя или Разработчика!")
        await db.remove_chat_admin(message.chat.id, target_id)
        return await safe_reply(message, f"🚫 {get_mention(target_id, target_name)} снят с должности Администратора этого чата.")

    elif cmd in ["report", "репорт", "жалоба", "спам"]:
        return await process_report_cmd(message, args)

    elif cmd in ["кубы", "кубики", "кубсы", "дабл", "doubledice", "2dice", "дубль", "2кубика"]:
        user_id = message.from_user.id
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        if not await check_subscription(user_id):
            return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))
        user = await db.get_user(user_id)
        user_bal = user[2] if user else 0
        display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name
        bet = parse_amount_string(args[0] if args else None, user_bal)
        if bet is None:
            return await safe_reply(message, "❌ <b>Укажите корректную ставку!</b>\nФормат: <code>кубы 500</code>, <code>кубы 3кк</code> или <code>кубы вабанк</code>")
        if bet < 100:
            return await safe_reply(message, f"❌ Минимальная ставка: <b>100 💰</b>! Ваш баланс: <code>{fmt_num(user_bal)} 💰</code>")
        if await check_bet_confirmation(message, user_id, display_name, bet, "doubledice", run_doubledice_game):
            return await run_doubledice_game(message, user_id, display_name, bet)

    elif cmd in ["check", "чек", "чеки", "раздача"]:
        return await process_create_check_cmd(message, args)

    elif cmd in ["sponsor", "sub_bonus", "subbonus", "спонсор"]:
        return await process_sponsor_cmd(message)

    elif cmd in ["work", "работа", "ворк", "заработать", "зарплата", "смена"]:
        return await process_work_cmd(message)

    elif cmd in ["chatstats", "статачата", "чатстата", "чат"]:
        return await process_chat_stats_cmd(message)

    elif cmd in ["clear", "очистить", "удалить", "clean", "purge"]:
        if message.chat.type not in ["group", "supergroup"]:
            return await safe_reply(message, "❌ Очистка доступна только в группах!")
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        count = 10
        if args and args[0].isdigit():
            count = min(100, max(1, int(args[0])))
        deleted = 0
        start_msg_id = message.message_id
        for msg_id in range(start_msg_id, max(1, start_msg_id - count - 1), -1):
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=msg_id)
                deleted += 1
            except Exception:
                pass
        info_msg = await message.answer(f"🧹 Удалено сообщений: <b>{deleted}</b>", parse_mode="HTML")
        await asyncio.sleep(4)
        try:
            await info_msg.delete()
        except Exception:
            pass
        return

    elif cmd in ["dice", "кубик", "кость", "кости", "куб"]:
        user_id = message.from_user.id
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        if not await check_subscription(user_id):
            return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))
        user = await db.get_user(user_id)
        user_bal = user[2] if user else 0
        display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name
        bet = parse_amount_string(args[0] if args else None, user_bal)
        if bet is None:
            return await safe_reply(message, "❌ <b>Укажите корректную ставку!</b>\nФормат: <code>кубик 500</code>, <code>кубик 2кк</code> или <code>кубик вабанк</code>")
        if bet < 100:
            return await safe_reply(message, f"❌ Минимальная ставка: <b>100 💰</b>! Ваш баланс: <code>{fmt_num(user_bal)} 💰</code>")
        if await check_bet_confirmation(message, user_id, display_name, bet, "dice", run_dice_game):
            return await run_dice_game(message, user_id, display_name, bet)

    elif cmd in ["ladder", "лесенка", "лестница", "ступень"]:
        user_id = message.from_user.id
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        if not await check_subscription(user_id):
            return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))
        if user_id in active_ladders:
            return await safe_reply(message, "❌ У вас уже начата игра в Лесенку! Завершите её.")
        user = await db.get_user(user_id)
        user_bal = user[2] if user else 0
        display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name
        bet = parse_amount_string(args[0] if args else None, user_bal)
        if bet is None:
            return await safe_reply(message, "❌ <b>Укажите корректную ставку!</b>\nФормат: <code>лесенка 500</code>, <code>лесенка 1кк</code> или <code>лесенка вабанк</code>")
        if bet < 100:
            return await safe_reply(message, f"❌ Минимальная ставка в Лесенке: <b>100 💰</b>! Ваш баланс: <code>{fmt_num(user_bal)} 💰</code>")
        if user_bal < bet:
            return await safe_reply(message, f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!")

        async def start_ladder_direct(msg, uid, uname, b):
            await db.change_balance(uid, -b)
            await db.add_turnover(uid, b)
            timeout_task = asyncio.create_task(ladder_timeout_watcher(uid, msg))
            active_ladders[uid] = {"user_id": uid, "bet": b, "step": 0, "is_rolling": False, "task": timeout_task}
            is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if b == user_bal else ""
            text = (
                f"🚀 <b>КУБИЧЕСКАЯ ЛЕСЕНКА</b>\n\n"
                f"{is_allin}"
                f"👤 Игрок: {get_mention(uid, uname)}\n"
                f"💰 Ставка: <b>{fmt_num(b)} 💰</b>\n"
                f"⏱ <i>Таймаут неактивности: 3 минуты</i>\n\n"
                f"{render_ladder(0)}\n\n"
                f"🎲 <i>Правила: кубик 3, 4, 5, 6 — подъём наверх (+множитель). 1 или 2 — падение и сгорание ставки!</i>"
            )
            await safe_reply(msg, text, reply_markup=ladder_keyboard(uid, 0))

        if await check_bet_confirmation(message, user_id, display_name, bet, "ladder", start_ladder_direct):
            return await start_ladder_direct(message, user_id, display_name, bet)

    elif cmd in ["duel", "дуэль", "вызов", "дуели", "дуель"]:
        return await process_duel_cmd(message, args)

    elif cmd in ["over", "больше", "бол", "хай", "high"]:
        user_id = message.from_user.id
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        if not await check_subscription(user_id):
            return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))
        user = await db.get_user(user_id)
        user_bal = user[2] if user else 0
        display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name
        bet = parse_amount_string(args[0] if args else None, user_bal)
        if bet is None or bet < 100:
            return await safe_reply(message, f"❌ Укажите ставку от 100 💰!")
        async def simple_wrapper(msg, uid, uname, b):
            await run_simple_bet_game(msg, uid, uname, b, "over")
        if await check_bet_confirmation(message, user_id, display_name, bet, "over", simple_wrapper):
            return await run_simple_bet_game(message, user_id, display_name, bet, "over")

    elif cmd in ["under", "меньше", "мен", "лоу", "low"]:
        user_id = message.from_user.id
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        if not await check_subscription(user_id):
            return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))
        user = await db.get_user(user_id)
        user_bal = user[2] if user else 0
        display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name
        bet = parse_amount_string(args[0] if args else None, user_bal)
        if bet is None or bet < 100:
            return await safe_reply(message, f"❌ Укажите ставку от 100 💰!")
        async def simple_wrapper(msg, uid, uname, b):
            await run_simple_bet_game(msg, uid, uname, b, "under")
        if await check_bet_confirmation(message, user_id, display_name, bet, "under", simple_wrapper):
            return await run_simple_bet_game(message, user_id, display_name, bet, "under")

    elif cmd in ["even", "чет", "четное", "чёт", "чётное"]:
        user_id = message.from_user.id
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        if not await check_subscription(user_id):
            return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))
        user = await db.get_user(user_id)
        user_bal = user[2] if user else 0
        display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name
        bet = parse_amount_string(args[0] if args else None, user_bal)
        if bet is None or bet < 100:
            return await safe_reply(message, f"❌ Укажите ставку от 100 💰!")
        async def simple_wrapper(msg, uid, uname, b):
            await run_simple_bet_game(msg, uid, uname, b, "even")
        if await check_bet_confirmation(message, user_id, display_name, bet, "even", simple_wrapper):
            return await run_simple_bet_game(message, user_id, display_name, bet, "even")

    elif cmd in ["odd", "нечет", "нечетное", "нечёт", "нечётное"]:
        user_id = message.from_user.id
        await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
        if not await check_subscription(user_id):
            return await safe_reply(message, f"⚠️ <b>Для игры необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(user_id))
        user = await db.get_user(user_id)
        user_bal = user[2] if user else 0
        display_name = (user[13] or message.from_user.full_name) if user else message.from_user.full_name
        bet = parse_amount_string(args[0] if args else None, user_bal)
        if bet is None or bet < 100:
            return await safe_reply(message, f"❌ Укажите ставку от 100 💰!")
        async def simple_wrapper(msg, uid, uname, b):
            await run_simple_bet_game(msg, uid, uname, b, "odd")
        if await check_bet_confirmation(message, user_id, display_name, bet, "odd", simple_wrapper):
            return await run_simple_bet_game(message, user_id, display_name, bet, "odd")
    
    elif cmd in ["profile", "профиль", "баланс", "balance", "stats", "стата"]:
        return await process_profile_cmd(message, args)
    elif cmd in ["ref", "реф", "рефералы", "друзья", "партнерка"]:
        if not await check_subscription(message.from_user.id):
            return await safe_reply(message, f"⚠️ <b>Необходимо подписаться на наш канал {REQUIRED_CHANNEL}!</b>", reply_markup=sub_keyboard(message.from_user.id))
        me = await bot.get_me()
        ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
        ref_count = await db.get_referrals_count(message.from_user.id)
        text_ref = (
            f"🤝 <b>Реферальная программа</b>\n\n"
            f"Приглашай друзей и получай <b>3% от каждой их ставки</b> во всех режимах кубиков!\n\n"
            f"👥 Твоих рефералов: <b>{ref_count}</b>\n"
            f"🔗 Ссылка для приглашения:\n<code>{ref_link}</code>"
        )
        return await safe_reply(message, text_ref)
    elif cmd in ["top", "топ", "лидеры", "богачи"]:
        return await process_top_cmd(message)
    elif cmd in ["pay", "передать", "перевод"]:
        return await process_pay_cmd(message, args)

    # Выдача монет
    elif cmd in ["give", "выдать", "начислить", "сет", "set"]:
        if not await db.can_give_money(message.from_user.id):
            return await safe_reply(message, "❌ Функция выдачи монет доступна только <b>Создателю и Разработчику</b> бота!")
        target_id, target_name = None, None
        amount_raw = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
            target_id = target.id
            t_data = await db.get_user(target.id)
            target_name = (t_data[13] if t_data and t_data[13] else target.full_name)
            if args:
                amount_raw = args[0]
        else:
            if len(args) >= 2:
                for arg in args:
                    if parse_amount_string(arg, 0) is not None:
                        amount_raw = arg
                    else:
                        t_id, t_name, _ = await resolve_target_user(message, [arg])
                        if t_id:
                            target_id, target_name = t_id, t_name

        if not target_id or not amount_raw:
            return await safe_reply(message, "❌ Формат: <code>/give 3кк @username</code> или <code>выдать 100к</code> ответом на сообщение.")

        is_negative = amount_raw.startswith("-")
        clean_amount_str = amount_raw.lstrip("-")
        amount = parse_amount_string(clean_amount_str, 0)
        if amount is None:
            return await safe_reply(message, "❌ Неверный формат суммы!")
        if is_negative:
            amount = -amount

        await db.register_user(target_id, target_name)
        await db.change_balance(target_id, amount)
        verb = "выдал" if amount >= 0 else "забрал"
        return await safe_reply(message, f"👑 Администратор {verb} <b>{fmt_num(abs(amount))} 💰</b> у {get_mention(target_id, target_name)}!")

    elif cmd in ["mute", "мут", "завалить", "замутить"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        target_id, target_name, rest = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Укажите игрока: <code>/mute 10м @username Спам</code> или ответом на сообщение.")
        if target_id == DEV_ID or target_id in CREATOR_IDS:
            return await safe_reply(message, "❌ Нельзя замутить Создателя или Разработчика!")
        duration_sec = 600
        reason = "Без причины"
        if rest:
            match = re.match(r"^(\d+)\s*([a-zA-Zа-яА-Я]*)$", rest[0].strip().lower())
            if match:
                val = int(match.group(1))
                unit = match.group(2)
                if not unit or unit in ["м", "m", "мин", "min"]:
                    duration_sec = val * 60
                elif unit in ["с", "s", "сек"]:
                    duration_sec = val
                elif unit in ["ч", "h", "час", "часа"]:
                    duration_sec = val * 3600
                elif unit in ["д", "d", "день", "дня"]:
                    duration_sec = val * 86400
                if len(rest) > 1:
                    reason = " ".join(rest[1:])
            else:
                reason = " ".join(rest)
        try:
            until = datetime.now() + timedelta(seconds=duration_sec)
            await message.chat.restrict(user_id=target_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
            return await safe_reply(message, f"🔇 {get_mention(target_id, target_name)} отправлен в мут на <b>{format_duration(duration_sec)}</b>\n📝 Причина: {html.escape(reason)}")
        except Exception as e:
            return await safe_reply(message, f"❌ Ошибка: {e}")

    elif cmd in ["unmute", "размут", "снятьмут"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Укажите пользователя: <code>/unmute @username</code> или ответом на сообщение.")
        try:
            await message.chat.restrict(
                user_id=target_id,
                permissions=ChatPermissions(
                    can_send_messages=True, can_send_audios=True, can_send_documents=True,
                    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            )
            return await safe_reply(message, f"🔊 {get_mention(target_id, target_name)} размучен.")
        except Exception as e:
            return await safe_reply(message, f"❌ Ошибка: {e}")

    elif cmd in ["ban", "бан", "забанить", "кик", "kick"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Использование: <code>/ban @username</code> или ответом на сообщение.")
        if target_id == DEV_ID or target_id in CREATOR_IDS:
            return await safe_reply(message, "❌ Нельзя наказать Создателя или Разработчика!")
        try:
            await message.chat.ban(user_id=target_id)
            return await safe_reply(message, f"🛑 {get_mention(target_id, target_name)} забанен.")
        except Exception as e:
            return await safe_reply(message, f"❌ Ошибка при бане: {e}")

    elif cmd in ["unban", "разбан", "снятьбан"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Использование: <code>/unban @username</code> или <code>/unban 12345678</code>")
        try:
            await message.chat.unban(user_id=target_id, only_if_banned=True)
            return await safe_reply(message, f"✅ Пользователь {get_mention(target_id, target_name)} успешно разбанен в чате!")
        except Exception as e:
            return await safe_reply(message, f"❌ Ошибка разбана: {e}")

    elif cmd in ["warn", "варн", "пред", "предупреждение"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Укажите игрока: <code>/warn @username</code> или ответом на сообщение.")
        if target_id == DEV_ID or target_id in CREATOR_IDS:
            return await safe_reply(message, "❌ Нельзя выдать варн Создателю или Разработчику!")
        await db.register_user(target_id, target_name)
        warns = await db.add_warn(target_id)
        if warns >= 3:
            try:
                await message.chat.ban(user_id=target_id)
                await db.reset_warns(target_id)
                return await safe_reply(message, f"🛑 {get_mention(target_id, target_name)} набрал <b>3/3 варнов</b> и получил бан!")
            except Exception as e:
                return await safe_reply(message, f"❌ Ошибка при бане: {e}")
        else:
            return await safe_reply(message, f"⚠️ {get_mention(target_id, target_name)} получил варн (<b>{warns}/3</b>)!")

    elif cmd in ["unwarn", "снятьварн", "разварн", "снятьпред"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await safe_reply(message, "❌ У вас нет прав администратора!")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await safe_reply(message, "❌ Укажите игрока: <code>/unwarn @username</code> или ответом на сообщение.")
        await db.reset_warns(target_id)
        return await safe_reply(message, f"✅ Предупреждения игрока {get_mention(target_id, target_name)} аннулированы.")


# ================= ЗАПУСК =================
async def handle_ping(request):
    return web.Response(text="Duel Cubes Bot is alive! 🎲", status=200)


async def on_startup(bot: Bot):
    await db.init()
    
    commands = [
        BotCommand(command="start", description="Главное меню 🎲"),
        BotCommand(command="work", description="Работа (сбор раз в 2ч, до 24ч) 💼"),
        BotCommand(command="sponsor", description="Бонус спонсора (+50k) 📢"),
        BotCommand(command="rules", description="Правила чата 📜"),
        BotCommand(command="admins", description="Администрация этого чата 👥"),
        BotCommand(command="check", description="Создать чек-раздачу в чате 🎁"),
        BotCommand(command="report", description="Жалоба на спамера 🚨"),
        BotCommand(command="clear", description="Очистить сообщения в чате 🧹"),
        BotCommand(command="dice", description="1 кубик против бота 🤖"),
        BotCommand(command="ladder", description="Кубическая лесенка до x7.5 🚀"),
        BotCommand(command="duel", description="Дуэль 1v1 в чате ⚔️"),
        BotCommand(command="chatstats", description="Статистика чата 📊"),
        BotCommand(command="over", description="Больше (4-6) 📈"),
        BotCommand(command="under", description="Меньше (1-3) 📉"),
        BotCommand(command="even", description="Чётное число ⚖️"),
        BotCommand(command="odd", description="Нечётное число 🎲"),
        BotCommand(command="profile", description="Мой профиль и баланс 👤"),
        BotCommand(command="ref", description="Рефералка (+3%) 🤝"),
        BotCommand(command="pay", description="Передать монеты 💸"),
        BotCommand(command="top", description="Топ игроков 🏆"),
        BotCommand(command="topmsg", description="Топ по сообщениям 💬"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logging.warning(f"Ошибка регистрации команд: {e}")

    asyncio.create_task(quiz_background_worker())
    asyncio.create_task(db_cleanup_background_worker())

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        logging.info(f"Установка Webhook: {webhook_url}")
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
    else:
        logging.info("RENDER_EXTERNAL_URL не задан, запуск в локальном режиме.")


def main():
    if RENDER_EXTERNAL_URL:
        app = web.Application()
        app.router.add_get("/", handle_ping)
        
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