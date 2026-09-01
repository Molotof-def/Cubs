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

OWNER_ID_RAW = os.getenv("OWNER_ID", "0").strip()
try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    OWNER_ID = 0

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    exit("❌ ОШИБКА: DATABASE_URL не найден! Добавьте подключение к PostgreSQL.")

REQUIRED_CHANNEL_RAW = os.getenv("REQUIRED_CHANNEL", "@chat_giveaways").strip()
if "t.me/" in REQUIRED_CHANNEL_RAW:
    REQUIRED_CHANNEL_RAW = "@" + REQUIRED_CHANNEL_RAW.split("t.me/")[-1].strip("/")
if REQUIRED_CHANNEL_RAW and not REQUIRED_CHANNEL_RAW.startswith("@") and not REQUIRED_CHANNEL_RAW.startswith("-100"):
    REQUIRED_CHANNEL_RAW = f"@{REQUIRED_CHANNEL_RAW}"

REQUIRED_CHANNEL = REQUIRED_CHANNEL_RAW

# Канал спонсора для бонуса
SPONSOR_CHANNEL_RAW = os.getenv("SPONSOR_CHANNEL", "@DuelCubesChannel").strip()
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
    "доставил секретный груз на спорткаре",
    "взломал защищённую базу данных корпорации",
    "выиграл турнир по киберспорту",
    "нашёл золотой самородок в заброшенной шахте",
    "продал редкий 3D-ассет на маркетплейсе",
    "починил серверную стойку дата-центра",
    "провёл успешный стрим и собрал кучу донатов",
    "собрал кастомный игровой ПК под заказ"
]

TG_FACTS = [
    "Первый клиент Telegram был запущен 14 августа 2013 года исключительно для устройств на iOS.",
    "Протокол безопасности MTProto, на котором работает весь Telegram, был полностью спроектирован Николаем Дуровым — братом Павла Дурова и кандидатом физико-математических наук.",
    "Идея создания защищенного мессенджера пришла Павлу Дурову, когда к нему в дверь стучался спецназ, а у него не было безопасного канала связи даже для звонка брату.",
    "Telegram Bot API был запущен в июне 2015 года и стал первым в мире массовым стандартом для создания ботов внутри мессенджеров.",
    "Telegram стал первым мессенджером, который ввел векторные анимированные стикеры в формате .TGS (на базе Lottie), которые весят в среднем менее 30 КБ.",
    "Все анимированные эмодзи в Telegram с играми (🎲 кубик, 🎯 дартс, 🎰 слоты, 🏀 баскетбол) имеют встроенный детерминированный генератор случайных чисел на серверах Telegram.",
    "Самый дорогой юзернейм в истории платформы Fragment — <b>@news</b>, он был продан на аукционе за 994 000 TON (~2.4 миллиона долларов на момент сделки).",
    "Telegram официально не потратил ни одного доллара на платную рекламу самого приложения за всю историю своего существования — весь миллиардный рост произошёл благодаря вирусному эффекту.",
    "В Telegram можно отправлять файлы объемом до 2 ГБ бесплатно и до 4 ГБ с подпиской Telegram Premium — это крупнейший лимит среди всех популярных мессенджеров мира.",
    "Серверная инфраструктура Telegram распределена по независимым дата-центрам в разных точках планеты, а ключи шифрования разделены на части и никогда не хранятся в одном месте."
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

active_duels: Dict[str, dict] = {}
active_ladders: Dict[int, dict] = {}
active_checks: Dict[str, dict] = {}
chat_recent_users: Dict[int, List[int]] = {}
user_loss_streaks: Dict[int, int] = {}
user_last_action: Dict[int, float] = {}
resolved_channel_id: Optional[int] = None
resolved_sponsor_id: Optional[int] = None


def fmt_num(val: int) -> str:
    return f"{val:,}".replace(",", " ")


def get_mention(user_id: int, name: Optional[str]) -> str:
    safe_name = html.escape(name or "Игрок")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def has_cubes_tag(user_full_name: str) -> bool:
    name_lower = (user_full_name or "").lower()
    return "cubes" in name_lower or "кубы" in name_lower or "cube" in name_lower or "куб" in name_lower


def replay_keyboard(game_type: str, bet: int, user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔄 Реванш ({fmt_num(bet)} 💰)", callback_data=f"rep_{game_type}_{bet}_{user_id}")
    return builder.as_markup()


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

    try:
        await message.reply(text=full_caption, parse_mode="HTML", reply_markup=final_markup)
    except Exception as e:
        logging.error(f"HTML error: {e}")
        clean_text = re.sub(r'<[^>]+>', '', full_caption)
        await message.reply(text=clean_text, reply_markup=final_markup)


def parse_time_string(time_str: str) -> Optional[int]:
    match = re.match(r"^(\d+)\s*([a-zA-Zа-яА-Я]*)$", time_str.strip().lower())
    if not match:
        return None
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if not unit or unit in ["м", "m", "мин", "min", "минут", "минуты", "минута"]:
        return value * 60
    elif unit in ["с", "s", "сек", "sec", "секунд", "секунды", "секунда"]:
        return value
    elif unit in ["ч", "h", "час", "часа", "часов", "hour", "hours"]:
        return value * 3600
    elif unit in ["д", "d", "день", "дня", "дней", "day", "days"]:
        return value * 86400
    elif unit in ["н", "w", "нед", "неделя", "недели", "недель", "week", "weeks"]:
        return value * 604800
    
    return None


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


# ================= ПРОВЕРКА ПОДПИСКИ =================
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
        if any(s in status_str for s in ["member", "administrator", "creator"]):
            return True

        return False
    except Exception as e:
        err_msg = str(e).lower()
        logging.warning(f"Ошибка проверки подписки {user_id} в {channel_target}: {e}")
        if "user not found" in err_msg or "participant_id_invalid" in err_msg:
            return False
        if "chat not found" in err_msg or "bot is not a member" in err_msg:
            return True
        return False


async def check_subscription(user_id: int) -> bool:
    return await check_channel_member(user_id, REQUIRED_CHANNEL)


def sub_keyboard():
    builder = InlineKeyboardBuilder()
    clean_tag = REQUIRED_CHANNEL.replace("@", "")
    channel_url = f"https://t.me/{clean_tag}" if not REQUIRED_CHANNEL.startswith("-100") else "https://t.me/"
    builder.button(text="📢 Подписаться на канал", url=channel_url)
    builder.button(text="🔄 Проверить подписку", callback_data="sub_check_recheck")
    builder.adjust(1)
    return builder.as_markup()


def sponsor_keyboard():
    builder = InlineKeyboardBuilder()
    clean_tag = SPONSOR_CHANNEL.replace("@", "")
    channel_url = f"https://t.me/{clean_tag}" if not SPONSOR_CHANNEL.startswith("-100") else "https://t.me/"
    builder.button(text="📢 Канал Спонсора", url=channel_url)
    builder.button(text="🎁 Проверить и забрать 50k", callback_data="chk_sponsor_bonus")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data == "sub_check_recheck")
async def cb_recheck_sub(call: CallbackQuery):
    if await check_subscription(call.from_user.id):
        try:
            await call.message.edit_text("✅ <b>Подписка подтверждена!</b> Теперь вам доступны все функции и игры.", parse_mode="HTML")
        except Exception:
            await call.answer("✅ Подписка подтверждена!", show_alert=True)
    else:
        await call.answer("❌ Вы ещё не подписались на наш канал!", show_alert=True)


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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_work_time TIMESTAMP DEFAULT NULL;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS sponsor_bonus_claimed BOOLEAN DEFAULT FALSE;

                CREATE TABLE IF NOT EXISTS bot_admins (
                    user_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id BIGINT,
                    user_id BIGINT,
                    PRIMARY KEY (chat_id, user_id)
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
                    INSERT INTO chat_members (chat_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT (chat_id, user_id) DO NOTHING
                """, chat_id, user_id)

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
                "SELECT user_id, username, balance, turnover, wins, losses, draws, warns, referrer_id, last_work_time, sponsor_bonus_claimed, created_at, tg_username FROM users WHERE user_id = $1",
                user_id
            )
            return list(row) if row else None

    async def change_balance(self, user_id: int, amount: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

    async def update_work_time(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_work_time = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)

    async def set_sponsor_claimed(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET sponsor_bonus_claimed = TRUE WHERE user_id = $1", user_id)

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
                            text=f"🤝 <b>Реферальный бонус!</b>\nВаш реферал сыграл на <code>{fmt_num(lost_amount)} 💰</code>. Вам начислено 3%: <b>+{fmt_num(reward)} 💰</b>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Ошибка начисления реферальных: {e}")

    async def get_referrals_count(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", user_id) or 0

    async def get_top(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT $1", limit)

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
                SELECT u.user_id, u.username, u.balance
                FROM chat_members cm
                JOIN users u ON cm.user_id = u.user_id
                WHERE cm.chat_id = $1
                ORDER BY u.balance DESC
                LIMIT 1
            """, chat_id)
            
            return stats, top_player

    async def is_admin(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        if OWNER_ID and user_id == OWNER_ID:
            return True
        
        async with self.pool.acquire() as conn:
            res = await conn.fetchval("SELECT 1 FROM bot_admins WHERE user_id = $1", user_id)
            if res is not None:
                return True

        if chat_id and chat_id < 0:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if isinstance(member, (ChatMemberOwner, ChatMemberAdministrator)):
                    return True
            except Exception:
                pass

        return False

    async def add_admin(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

    async def remove_admin(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM bot_admins WHERE user_id = $1", user_id)

    async def add_warn(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET warns = warns + 1 WHERE user_id = $1", user_id)
            res = await conn.fetchval("SELECT warns FROM users WHERE user_id = $1", user_id)
            return res if res is not None else 1

    async def reset_warns(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET warns = 0 WHERE user_id = $1", user_id)


db = Database(DATABASE_URL)


# ================= АНТИСПАМ И РЕГИСТРАЦИЯ (MIDDLEWARE) =================
class ThrottlingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
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


dp.message.outer_middleware(ThrottlingMiddleware())
dp.callback_query.outer_middleware(ThrottlingMiddleware())


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
def resolve_bet_amount(arg_val: Optional[str], current_balance: int) -> Optional[int]:
    if not arg_val:
        return 100
    
    val_lower = arg_val.lower().strip()
    allin_aliases = ["вабанк", "ва-банк", "все", "всё", "all", "full", "фулл", "фул", "макс", "max", "оллин", "all-in"]
    if val_lower in allin_aliases:
        return max(0, current_balance)

    if val_lower.isdigit():
        return int(val_lower)
        
    return None


async def resolve_target_user(message: Message, args: List[str]) -> Tuple[Optional[int], str, List[str]]:
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return None, target.full_name, args
        return target.id, target.full_name, args

    if not args:
        return None, "Пользователь", args

    first_arg = args[0].strip()
    remaining_args = args[1:]

    if first_arg.isdigit() and len(first_arg) >= 6:
        t_id = int(first_arg)
        u_data = await db.get_user(t_id)
        t_name = u_data[1] if u_data else f"ID {t_id}"
        return t_id, t_name, remaining_args

    if first_arg.startswith("@"):
        clean_tag = first_arg.replace("@", "")
        t_id = await db.get_user_id_by_username(clean_tag)
        if t_id:
            u_data = await db.get_user(t_id)
            t_name = u_data[1] if u_data else f"@{clean_tag}"
            return t_id, t_name, remaining_args
        return None, f"@{clean_tag}", remaining_args

    return None, "Пользователь", args


# ================= АВТОМАТИЧЕСКИЙ ТАЙМАУТ ЛЕСЕНКИ =================
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


async def run_dice_game(message: Message, user_id: int, user_name: str, bet: int):
    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    if user_bal < bet:
        return await message.reply(f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if bet == user_bal else ""
    await message.reply(f"{is_allin}🎲 Бросок {get_mention(user_id, user_name)} (Ставка: <b>{fmt_num(bet)} 💰</b>):", parse_mode="HTML")
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
        try:
            await db.record_game(user_id, "win")
        except Exception:
            pass
        text = (
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"👤 {get_mention(user_id, user_name)}\n"
            f"💰 Коэффициент: <b>x1.9</b>\n"
            f"💵 Выигрыш: <b>+{fmt_num(win)} 💰</b>"
        )
        await send_game_result(message, "win", text, user_id=user_id, game_type="dice", bet=bet)
    elif p_val < b_val:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception as e:
            logging.error(f"Ошибка фиксации поражения: {e}")
        text = (
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"👤 {get_mention(user_id, user_name)}\n"
            f"📉 Потеряно: <b>-{fmt_num(bet)} 💰</b>"
        )
        await send_game_result(message, "loss", text, user_id=user_id, game_type="dice", bet=bet)
    else:
        await db.change_balance(user_id, bet)
        try:
            await db.record_game(user_id, "draw")
        except Exception:
            pass
        text = (
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"💰 <b>Возврат ставки:</b> <code>+{fmt_num(bet)} 💰</code>"
        )
        await send_game_result(message, "draw", text, user_id=user_id, game_type="dice", bet=bet)


async def run_doubledice_game(message: Message, user_id: int, user_name: str, bet: int):
    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    if user_bal < bet:
        return await message.reply(f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if bet == user_bal else ""
    await message.reply(f"{is_allin}🎲🎲 <b>Бросок двух кубиков {get_mention(user_id, user_name)} (Ставка: {fmt_num(bet)} 💰):</b>", parse_mode="HTML")
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
        try:
            await db.record_game(user_id, "win")
        except Exception:
            pass

        bonus_title = "🔥 <b>МЕГА-ДУБЛЬ (x3.0)!</b>\n" if is_double else f"Коэффициент: <b>x{mult}</b>\n"
        res = f"👤 {get_mention(user_id, user_name)}\nТвои очки: {p1} + {p2} = <b>{p_sum}</b>\n🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n{bonus_title}💵 Выигрыш: <b>+{fmt_num(win)} 💰</b>"
        await send_game_result(message, "win", res, user_id=user_id, game_type="doubledice", bet=bet)
    elif p_sum < b_sum:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception as e:
            logging.error(f"Ошибка фиксации поражения: {e}")
        res = f"👤 {get_mention(user_id, user_name)}\nТвои очки: {p1} + {p2} = <b>{p_sum}</b>\n🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n📉 Потеряно: <b>-{fmt_num(bet)} 💰</b>"
        await send_game_result(message, "loss", res, user_id=user_id, game_type="doubledice", bet=bet)
    else:
        await db.change_balance(user_id, bet)
        try:
            await db.record_game(user_id, "draw")
        except Exception:
            pass
        res = f"🎲 Очки: <b>{p_sum} = {b_sum}</b>\n💰 Ставка <b>{fmt_num(bet)} 💰</b> возвращена!"
        await send_game_result(message, "draw", user_id=user_id, game_type="doubledice", bet=bet)


async def run_simple_bet_game(message: Message, user_id: int, user_name: str, bet: int, game_type: str):
    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    if user_bal < bet:
        return await message.reply(f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    type_titles = {
        "over": "БОЛЬШЕ (4-6)",
        "under": "МЕНЬШЕ (1-3)",
        "even": "ЧЁТНОЕ (2, 4, 6)",
        "odd": "НЕЧЁТНОЕ (1, 3, 5)"
    }

    is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if bet == user_bal else ""
    await message.reply(f"{is_allin}🎲 {get_mention(user_id, user_name)} поставил <b>{fmt_num(bet)} 💰</b> на <b>{type_titles[game_type]}</b>:", parse_mode="HTML")
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
        try:
            await db.record_game(user_id, "win")
        except Exception:
            pass
        res = f"🎲 Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, user_name)}\n💰 Множитель: <b>x1.9</b>\n💵 Выигрыш: <b>+{fmt_num(win)} 💰</b>"
        await send_game_result(message, "win", res, user_id=user_id, game_type=game_type, bet=bet)
    else:
        try:
            await db.record_game(user_id, "loss")
            await db.process_referral_loss(user_id, bet)
        except Exception as e:
            logging.error(f"Ошибка фиксации проигрыша: {e}")
        res = f"🎲 Выпало: [ <b>{val}</b> ]\n👤 {get_mention(user_id, user_name)}\n📉 Потеряно: <b>-{fmt_num(bet)} 💰</b>"
        await send_game_result(message, "loss", res, user_id=user_id, game_type=game_type, bet=bet)


# ================= СИСТЕМА ЧЕКОВ В ЧАТАХ =================
async def process_create_check_cmd(message: Message, args: List[str]):
    user_id = message.from_user.id
    if message.chat.type not in ["group", "supergroup"]:
        return await message.reply("❌ Раздачи чеков работают только в группах!", parse_mode="HTML")

    if not await check_subscription(user_id):
        return await message.reply("⚠️ <b>Необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    if len(args) < 2:
        return await message.reply("❌ Формат: <code>чек [общая_сумма] [кол-во_человек]</code>\n<i>Пример:</i> <code>чек 50000 5</code>", parse_mode="HTML")

    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    try:
        total_amount = int(args[0])
        activations = int(args[1])
    except ValueError:
        return await message.reply("❌ Сумма и количество человек должны быть числами!", parse_mode="HTML")

    if activations < 1 or activations > 50:
        return await message.reply("❌ Количество активаций от 1 до 50!", parse_mode="HTML")

    if total_amount < activations * 100:
        return await message.reply(f"❌ Минимальная сумма на одного человека: <b>100 💰</b>!", parse_mode="HTML")

    if user_bal < total_amount:
        return await message.reply(f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -total_amount)

    check_id = uuid.uuid4().hex[:8]
    amount_per_user = total_amount // activations

    active_checks[check_id] = {
        "creator_id": user_id,
        "creator_name": message.from_user.full_name,
        "total_amount": total_amount,
        "amount_per_user": amount_per_user,
        "total_activations": activations,
        "remaining_activations": activations,
        "claimed_users": set()
    }

    text = (
        f"🎁 <b>РАЗДАЧА ЧЕКА В ЧАТЕ!</b>\n\n"
        f"👤 Создатель: {get_mention(user_id, message.from_user.full_name)}\n"
        f"💰 Общий банк: <b>{fmt_num(total_amount)} 💰</b>\n"
        f"💵 Каждый получит: <b>+{fmt_num(amount_per_user)} 💰</b>\n"
        f"👥 Осталось мест: <b>{activations}/{activations}</b>\n\n"
        f"<i>Нажмите кнопку ниже, чтобы забрать монеты!</i>"
    )
    await message.reply(text, reply_markup=check_keyboard(check_id, activations, activations), parse_mode="HTML")


@dp.callback_query(F.data.startswith("take_chk_"))
async def cb_take_check(call: CallbackQuery):
    check_id = call.data.replace("take_chk_", "")
    user_id = call.from_user.id

    if check_id not in active_checks:
        return await call.answer("❌ Этот чек уже закончился или недействителен!", show_alert=True)

    chk = active_checks[check_id]

    if user_id == chk["creator_id"]:
        return await call.answer("❌ Вы не можете забрать свой собственный чек!", show_alert=True)

    if user_id in chk["claimed_users"]:
        return await call.answer("❌ Вы уже активировали этот чек!", show_alert=True)

    if not await check_subscription(user_id):
        return await call.answer("⚠️ Сначала подпишитесь на канал проекта!", show_alert=True)

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


# ================= БОНУС СПОНСОРА =================
async def process_sponsor_cmd(message: Message):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    user = await db.get_user(user_id)
    if not user:
        return

    claimed = user[10]  # sponsor_bonus_claimed
    if claimed:
        return await message.reply("✅ <b>Вы уже получали бонус за подписку на спонсора (+50 000 💰)!</b>", parse_mode="HTML")

    text = (
        f"📢 <b>БОНУС ЗА ПОДПИСКУ НА СПОНСОРА!</b>\n\n"
        f"Подпишитесь на канал спонсора <b>{SPONSOR_CHANNEL}</b> и получите разовый бонус:\n"
        f"💰 <b>+50 000 монет на баланс!</b>\n\n"
        f"<i>После подписки нажмите кнопку проверки ниже.</i>"
    )
    await message.reply(text, reply_markup=sponsor_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "chk_sponsor_bonus")
async def cb_sponsor_bonus_claim(call: CallbackQuery):
    user_id = call.from_user.id
    user = await db.get_user(user_id)
    if not user:
        return await call.answer("❌ Ошибка профиля!", show_alert=True)

    if user[10]:
        return await call.answer("❌ Вы уже забрали этот бонус ранее!", show_alert=True)

    is_sponsor_member = await check_channel_member(user_id, SPONSOR_CHANNEL)
    if not is_sponsor_member:
        return await call.answer(f"❌ Вы ещё не подписались на {SPONSOR_CHANNEL}!", show_alert=True)

    reward = 50000
    await db.change_balance(user_id, reward)
    await db.set_sponsor_claimed(user_id)

    await call.answer("🎉 Бонус +50 000 💰 успешно зачислен на баланс!", show_alert=True)
    try:
        await call.message.edit_text(
            f"🎉 <b>БОНУС СПОНСОРА ПОЛУЧЕН!</b>\n\n"
            f"👤 Игрок: {get_mention(user_id, call.from_user.full_name)}\n"
            f"💰 Начислено: <b>+{fmt_num(reward)} 💰</b> на баланс.",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ================= СИСТЕМА НАКОПИТЕЛЬНОГО ВОРКА С БОНУСОМ ЗА НИК (+10%) =================
async def process_work_cmd(message: Message):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.reply("⚠️ <b>Для работы необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    user = await db.get_user(user_id)
    if not user:
        return

    last_work = user[9]  # last_work_time
    now = datetime.now()

    if not last_work:
        diff_seconds = 3600 * 4
    else:
        diff_seconds = (now - last_work).total_seconds()

    # Минимальный интервал — 10 минут
    if diff_seconds < 600:
        remaining = int(600 - diff_seconds)
        mins = remaining // 60
        secs = remaining % 60
        return await message.reply(
            f"⏳ {get_mention(user_id, message.from_user.full_name)}, вы только что закончили смену!\n"
            f"Зарплата копится. Минимальный перерыв: <b>{mins} мин. {secs} сек.</b>\n\n"
            f"💡 <i>Чем дольше вы не забираете ворк — тем больше сумма накопится (вплоть до 24 часов)!</i>",
            parse_mode="HTML"
        )

    capped_seconds = min(diff_seconds, 86400)
    hours = capped_seconds / 3600.0

    base_per_hour = random.randint(11000, 15000)
    raw_earned = int(base_per_hour * hours) + random.randint(2000, 8000)
    raw_earned = max(5000, min(raw_earned, 380000))

    # Проверка тега в нике (+10%)
    has_tag = has_cubes_tag(message.from_user.full_name)
    tag_bonus_text = ""
    if has_tag:
        tag_bonus = int(raw_earned * 0.10)
        final_earned = raw_earned + tag_bonus
        tag_bonus_text = f"\n🏷 <b>Бонус за тег «Cubes» в нике (+10%):</b> <code>+{fmt_num(tag_bonus)} 💰</code>"
    else:
        final_earned = raw_earned
        tag_bonus_text = "\n💡 <i>Добавьте «Cubes» в имя Telegram и получайте +10% к каждой зарплате!</i>"

    task = random.choice(WORK_TASKS)

    await db.change_balance(user_id, final_earned)
    await db.update_work_time(user_id)

    formatted_time = format_duration(int(diff_seconds))
    is_max = " <i>(Достигнут максимум 24ч)</i>" if diff_seconds >= 86400 else ""

    await message.reply(
        f"💼 {get_mention(user_id, message.from_user.full_name)} {task}!\n\n"
        f"⏱ <b>Время накопления:</b> {formatted_time}{is_max}\n"
        f"💰 <b>Заработано монет:</b> <b>+{fmt_num(final_earned)} 💰</b>{tag_bonus_text}",
        parse_mode="HTML"
    )


# ================= КОМАНДА «ФАКТ ДНЯ» =================
async def process_fact_cmd(message: Message):
    fact = random.choice(TG_FACTS)
    text = (
        f"💡 <b>ИНТЕРЕСНЫЙ ФАКТ О TELEGRAM:</b>\n\n"
        f"<i>«{fact}»</i>"
    )
    await message.reply(text, parse_mode="HTML")


async def process_start_cmd(message: Message, ref_arg: Optional[str] = None):
    ref_id = None
    if ref_arg and ref_arg.startswith("ref_"):
        raw_id = ref_arg.replace("ref_", "")
        if raw_id.isdigit() and int(raw_id) != message.from_user.id:
            ref_id = int(raw_id)

    await db.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username, ref_id)
    user = await db.get_user(message.from_user.id)
    balance = user[2] if user else 10000

    text = (
        f"🎲 <b>Добро пожаловать в игровой клуб Duel Cubes!</b>\n\n"
        f"👤 Игрок: {get_mention(message.from_user.id, message.from_user.full_name)}\n"
        f"💰 Баланс: <b>{fmt_num(balance)} монет</b>\n\n"
        f"💼 <b>Заработок монет:</b>\n"
        f"🛠 <code>ворк</code> / <code>/work</code> — накопительный доход (до 24 часов)\n"
        f"📢 <code>бонус спонсора</code> — разовые <b>+50 000 💰</b> за канал\n"
        f"🏷 Добавь «Cubes» в ник Telegram $\\to$ <b>+10% к каждому ворку</b>\n\n"
        f"📜 <b>Режимы игр:</b>\n"
        f"🎁 <code>чек [сумма] [кол-во]</code> — создать раздачу чека в чате\n"
        f"⚔️ <code>дуэль [ставка] @username</code> — дуэль 1v1 с игроком\n"
        f"🎲 <code>кубик [ставка/вабанк]</code> — бросок против бота\n"
        f"🎲🎲 <code>дабл [ставка/вабанк]</code> — 2 кубика (х3 за дубль)\n"
        f"🚀 <code>лесенка [ставка/вабанк]</code> — Кубическая Лесенка (до x7.5)\n"
        f"📈 <code>больше [ставка/вабанк]</code> — Больше (4, 5, 6)\n"
        f"📉 <code>меньше [ставка/вабанк]</code> — Меньше (1, 2, 3)\n"
        f"⚖️ <code>четное [ставка/вабанк]</code> — Чётное число\n"
        f"🎲 <code>нечетное [ставка/вабанк]</code> — Нечётное число\n\n"
        f"📊 <b>Статистика и другое:</b>\n"
        f"👤 <code>профиль</code> | 🏆 <code>топ</code> | 👥 <code>стата чата</code>\n"
        f"💡 <code>факт дня</code> — интересные факты о Telegram\n"
        f"🤝 <code>реф</code> — рефералка (3% от ставок друзей)\n"
        f"💸 <code>перевод [сумма] @username</code> — передать монеты"
    )
    await message.reply(text, parse_mode="HTML")


async def process_chat_stats_cmd(message: Message):
    if message.chat.type not in ["group", "supergroup"]:
        return await message.reply("❌ Статистика чата доступна только в группах!", parse_mode="HTML")

    if not await check_subscription(message.from_user.id):
        return await message.reply("⚠️ <b>Для использования бота необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    await db.register_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
        chat_id=message.chat.id
    )

    stats, top_player = await db.get_chat_stats(message.chat.id)
    if not stats or stats["total_players"] == 0:
        return await message.reply("📊 В этом чате пока нет зарегистрированных игроков.", parse_mode="HTML")

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
    await message.reply(text, parse_mode="HTML")


async def process_dice_cmd(message: Message, args: List[str]):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.reply("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    bet = resolve_bet_amount(args[0] if args else None, user_bal)
    if bet is None:
        return await message.reply("❌ <b>Укажите корректную ставку!</b>\nФормат: <code>кубик 500</code> или <code>кубик вабанк</code>", parse_mode="HTML")

    if bet < 100:
        return await message.reply(f"❌ Минимальная ставка: <b>100 💰</b>! Ваш баланс: <code>{fmt_num(user_bal)} 💰</code>", parse_mode="HTML")

    await run_dice_game(message, user_id, message.from_user.full_name, bet)


async def process_doubledice_cmd(message: Message, args: List[str]):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.reply("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    bet = resolve_bet_amount(args[0] if args else None, user_bal)
    if bet is None:
        return await message.reply("❌ <b>Укажите корректную ставку!</b>\nФормат: <code>дабл 500</code> или <code>дабл вабанк</code>", parse_mode="HTML")

    if bet < 100:
        return await message.reply(f"❌ Минимальная ставка: <b>100 💰</b>! Ваш баланс: <code>{fmt_num(user_bal)} 💰</code>", parse_mode="HTML")

    await run_doubledice_game(message, user_id, message.from_user.full_name, bet)


async def process_simple_bet(message: Message, args: List[str], game_type: str):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.reply("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    bet = resolve_bet_amount(args[0] if args else None, user_bal)
    if bet is None:
        return await message.reply("❌ <b>Укажите корректную ставку!</b>\nФормат: <code>больше 500</code> или <code>больше вабанк</code>", parse_mode="HTML")

    if bet < 100:
        return await message.reply(f"❌ Минимальная ставка: <b>100 💰</b>! Ваш баланс: <code>{fmt_num(user_bal)} 💰</code>", parse_mode="HTML")

    await run_simple_bet_game(message, user_id, message.from_user.full_name, bet, game_type)


async def process_ladder_cmd(message: Message, args: List[str]):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.reply("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    if user_id in active_ladders:
        return await message.reply("❌ У вас уже начата игра в Лесенку! Завершите её.", parse_mode="HTML")

    user = await db.get_user(user_id)
    user_bal = user[2] if user else 0

    bet = resolve_bet_amount(args[0] if args else None, user_bal)
    if bet is None:
        return await message.reply("❌ <b>Укажите корректную ставку!</b>\nФормат: <code>лесенка 500</code> или <code>лесенка вабанк</code>", parse_mode="HTML")

    if bet < 100:
        return await message.reply(f"❌ Минимальная ставка в Лесенке: <b>100 💰</b>! Ваш баланс: <code>{fmt_num(user_bal)} 💰</code>", parse_mode="HTML")

    if user_bal < bet:
        return await message.reply(f"❌ Недостаточно монет! Баланс: <b>{fmt_num(user_bal)} 💰</b>\n💡 Напишите <code>ворк</code> чтобы заработать!", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    timeout_task = asyncio.create_task(ladder_timeout_watcher(user_id, message))

    active_ladders[user_id] = {
        "user_id": user_id,
        "bet": bet,
        "step": 0,
        "is_rolling": False,
        "task": timeout_task
    }

    is_allin = "🔥 <b>ALL-IN (ВА-БАНК)!</b>\n" if bet == user_bal else ""
    text = (
        f"🚀 <b>КУБИЧЕСКАЯ ЛЕСЕНКА</b>\n\n"
        f"{is_allin}"
        f"👤 Игрок: {get_mention(user_id, message.from_user.full_name)}\n"
        f"💰 Ставка: <b>{fmt_num(bet)} 💰</b>\n"
        f"⏱ <i>Таймаут неактивности: 3 минуты</i>\n\n"
        f"{render_ladder(0)}\n\n"
        f"🎲 <i>Правила: кубик 3, 4, 5, 6 — подъём наверх (+множитель). 1 или 2 — падение и сгорание ставки!</i>"
    )
    await message.reply(text, reply_markup=ladder_keyboard(user_id, 0), parse_mode="HTML")


async def process_duel_cmd(message: Message, args: List[str]):
    challenger = message.from_user
    await db.register_user(challenger.id, challenger.full_name, challenger.username)

    if not await check_subscription(challenger.id):
        return await message.reply("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    target_id, target_name = None, None
    bet_raw = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return await message.reply("❌ Нельзя вызывать ботов на дуэль!", parse_mode="HTML")
        target_id = target.id
        target_name = target.full_name
        bet_raw = args[0] if args else None
    else:
        if not args:
            return await message.reply("❌ Формат: <code>дуэль [ставка] @username</code> или ответом на сообщение.", parse_mode="HTML")
        
        for arg in args:
            if arg.startswith("@") or (arg.isdigit() and len(arg) > 6 and int(arg) > 1000000):
                clean_tag = arg.replace("@", "")
                if arg.isdigit():
                    t_id = int(arg)
                else:
                    t_id = await db.get_user_id_by_username(clean_tag)
                
                if t_id:
                    target_id = t_id
                    u_data = await db.get_user(t_id)
                    target_name = u_data[1] if u_data else f"@{clean_tag}"
            else:
                bet_raw = arg

    if not target_id:
        return await message.reply("❌ Укажите игрока через <code>@username</code> или ответьте на его сообщение!", parse_mode="HTML")

    if target_id == challenger.id:
        return await message.reply("❌ Нельзя играть с самим собой!", parse_mode="HTML")

    c_data = await db.get_user(challenger.id)
    c_bal = c_data[2] if c_data else 0

    bet = resolve_bet_amount(bet_raw, c_bal)
    if bet is None:
        return await message.reply("❌ Неверный формат ставки! Укажите число от 100 или <code>вабанк</code>.", parse_mode="HTML")

    if bet < 100:
        return await message.reply(f"❌ Минимальная ставка для дуэли: <b>100 💰</b>! У вас: <code>{fmt_num(c_bal)} 💰</code>", parse_mode="HTML")

    o_data = await db.get_user(target_id)
    if not o_data:
        return await message.reply("❌ Выбранный игрок еще не зарегистрирован в боте!", parse_mode="HTML")

    if c_bal < bet:
        return await message.reply(f"❌ У вас недостаточно монет! Баланс: <code>{fmt_num(c_bal)} 💰</code>", parse_mode="HTML")
    if o_data[2] < bet:
        return await message.reply(f"❌ У оппонента недостаточно монет для этой ставки! Баланс оппонента: <code>{fmt_num(o_data[2])} 💰</code>", parse_mode="HTML")

    duel_id = uuid.uuid4().hex[:8]

    active_duels[duel_id] = {
        "chat_id": message.chat.id,
        "challenger_id": challenger.id,
        "challenger_name": challenger.full_name,
        "opponent_id": target_id,
        "opponent_name": target_name,
        "bet": bet,
        "status": "pending"
    }

    is_allin = "🔥 <b>ALL-IN ВЫЗОВ (ВА-БАНК)!</b>\n" if bet == c_bal else ""
    text = (
        f"⚔️ <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"{is_allin}"
        f"🔴 Вызывающий: {get_mention(challenger.id, challenger.full_name)}\n"
        f"🔵 Оппонент: {get_mention(target_id, target_name)}\n"
        f"💰 Ставка: <b>{fmt_num(bet)} 💰</b> (Приз: <b>+{fmt_num(int(bet * 1.9))} 💰</b>)\n\n"
        f"<i>У оппонента 60 секунд на принятие.</i>"
    )

    duel_msg = await message.reply(text, reply_markup=duel_keyboard(duel_id), parse_mode="HTML")

    await asyncio.sleep(60)
    if duel_id in active_duels and active_duels[duel_id]["status"] == "pending":
        del active_duels[duel_id]
        try:
            await duel_msg.edit_text("⌛ <b>Время вызова истекло. Дуэль отменена.</b>", parse_mode="HTML")
        except Exception:
            pass


async def process_profile_cmd(message: Message, args: List[str]):
    if not await check_subscription(message.from_user.id):
        return await message.reply("⚠️ <b>Для использования бота необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    if len(args) > 1:
        return
    
    if len(args) == 1:
        arg = args[0].strip()
        if not arg.startswith("@") and not (arg.isdigit() and len(arg) >= 6):
            return

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
            return await message.reply("❌ Пользователь не найден в базе данных!", parse_mode="HTML")

    _, name, balance, turnover, wins, losses, draws, warns, _, last_work, sponsor_claimed, reg_date, tg_u = user

    total_games = wins + losses + draws
    winrate = round((wins / total_games * 100), 1) if total_games > 0 else 0
    tag_badge = "✅ Активен (+10% к ворку)" if has_cubes_tag(name) else "❌ Не установлен"

    text = (
        f"┏ 👤 <b>Профиль:</b> {get_mention(view_user_id, name)}\n"
        f"┣ 🆔 <b>ID:</b> <code>{view_user_id}</code>\n"
        f"┣ 💰 <b>Баланс:</b> <code>{fmt_num(balance)} 💰</code>\n"
        f"┣ 🏷 <b>Бонус за ник:</b> {tag_badge}\n"
        f"┣ 🔄 <b>Оборот:</b> <code>{fmt_num(turnover)} 💰</code>\n"
        f"┣ 🎮 <b>Всего игр:</b> <code>{total_games}</code>\n"
        f"┣ 🏆 <b>Побед:</b> <code>{wins}</code> | 💀 <b>Поражений:</b> <code>{losses}</code> | ⚖️ <b>Ничьих:</b> <code>{draws}</code>\n"
        f"┣ 📈 <b>Винрейт:</b> <code>{winrate}%</code>\n"
        f"┗ ⚠️ <b>Варны:</b> <code>{warns}/3</code>"
    )
    await message.reply(text, parse_mode="HTML")


async def process_top_cmd(message: Message):
    if not await check_subscription(message.from_user.id):
        return await message.reply("⚠️ <b>Для использования бота необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    top = await db.get_top(10)
    if not top:
        return await message.reply("🏆 Таблица лидеров пока пуста.", parse_mode="HTML")

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = "🏆 <b>ТОП-10 БОГАЧЕЙ БОТА:</b>\n\n"
    for i, row in enumerate(top, 1):
        place = medals.get(i, f"<b>{i}.</b>")
        name = row["username"]
        val = row["balance"]
        safe_name = html.escape(name or "Аноним")
        text += f"{place} {safe_name} — <code>{fmt_num(val)} 💰</code>\n"

    await message.reply(text, parse_mode="HTML")


async def process_pay_cmd(message: Message, args: List[str]):
    sender = message.from_user
    if not await check_subscription(sender.id):
        return await message.reply("⚠️ <b>Для выполнения переводов необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    sender_data = await db.get_user(sender.id)
    sender_bal = sender_data[2] if sender_data else 0

    recipient_id, recipient_name = None, None
    amount_raw = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return await message.reply("❌ Нельзя переводить монеты ботам!", parse_mode="HTML")
        recipient_id = target.id
        recipient_name = target.full_name
        amount_raw = args[0] if args else None
    else:
        if not args:
            return await message.reply("❌ Формат: <code>перевод [сумма] @username</code> или ответом на сообщение.", parse_mode="HTML")
        for arg in args:
            if arg.startswith("@") or (arg.isdigit() and len(arg) > 6 and int(arg) > 1000000):
                clean_tag = arg.replace("@", "")
                t_id = int(arg) if arg.isdigit() else await db.get_user_id_by_username(clean_tag)
                if t_id:
                    recipient_id = t_id
                    u_data = await db.get_user(t_id)
                    recipient_name = u_data[1] if u_data else f"@{clean_tag}"
            else:
                amount_raw = arg

    if not recipient_id:
        return await message.reply("❌ Укажите получателя: <code>перевод 500 @username</code>", parse_mode="HTML")

    if recipient_id == sender.id:
        return await message.reply("❌ Нельзя переводить монеты самому себе!", parse_mode="HTML")

    amount = resolve_bet_amount(amount_raw, sender_bal)
    if amount is None or amount <= 0:
        return await message.reply("❌ Укажите корректную сумму: <code>перевод 100</code> или <code>перевод вабанк</code>", parse_mode="HTML")

    if sender_bal < amount:
        return await message.reply(f"❌ Недостаточно монет для перевода! Ваш баланс: <code>{fmt_num(sender_bal)} 💰</code>", parse_mode="HTML")

    await db.register_user(sender.id, sender.full_name, sender.username)
    await db.register_user(recipient_id, recipient_name)

    await db.change_balance(sender.id, -amount)
    await db.change_balance(recipient_id, amount)

    await message.reply(
        f"💸 {get_mention(sender.id, sender.full_name)} перевел <b>{fmt_num(amount)} 💰</b> "
        f"игроку {get_mention(recipient_id, recipient_name)}!",
        parse_mode="HTML"
    )


# ================= СТРОГАЯ КОМАНДА РАССЫЛКИ =================
@dp.message(F.text.startswith("/broadcast") | F.text.startswith("/рассылка"))
async def cmd_broadcast_strict(message: Message):
    if not await db.is_admin(message.from_user.id, message.chat.id):
        return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")

    if not await check_subscription(message.from_user.id):
        return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    parts = message.text.strip().split(maxsplit=1)
    args_text = parts[1] if len(parts) > 1 else None

    broadcast_text = None
    if message.reply_to_message:
        broadcast_text = message.reply_to_message.text or message.reply_to_message.caption
    elif args_text:
        broadcast_text = args_text

    if not broadcast_text:
        return await message.reply("❌ Формат: <code>/рассылка [текст]</code> или ответом на сообщение.", parse_mode="HTML")

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


# ================= МАРШРУТИЗАТОР ВСЕХ ОСТАЛЬНЫХ ТЕКСТОВЫХ КОМАНД =================
@dp.message(F.text)
async def handle_all_text_commands(message: Message):
    full_text = message.text.strip().lower()
    if not full_text:
        return

    # Фразовые команды
    if full_text in ["стата чата", "статистика чата", "стата_чата", "чат стата", "чат статистика", "топ чата"]:
        return await process_chat_stats_cmd(message)

    if full_text in ["факт дня", "факт_дня", "фактдня", "факты"]:
        return await process_fact_cmd(message)

    if full_text in ["бонус спонсора", "спонсор бонус", "бонус_спонсора", "спонсор"]:
        return await process_sponsor_cmd(message)

    parts = message.text.strip().split()
    cmd_raw = parts[0].lower()
    cmd = cmd_raw.lstrip("/").split("@")[0]
    args = parts[1:]

    # Старт и кубы
    if cmd in ["start", "старт", "кубы", "куби", "кубсы", "меню", "menu", "помощь", "help", "инфо"]:
        ref_arg = args[0] if args else None
        await process_start_cmd(message, ref_arg)

    # Чеки
    elif cmd in ["check", "чек", "чеки", "раздача"]:
        await process_create_check_cmd(message, args)

    # Бонус спонсора
    elif cmd in ["sponsor", "sub_bonus", "subbonus", "спонсор"]:
        await process_sponsor_cmd(message)

    # Работа / Заработок (ворк)
    elif cmd in ["work", "работа", "ворк", "заработать", "зарплата", "смена"]:
        await process_work_cmd(message)

    # Факт
    elif cmd in ["fact", "факт"]:
        await process_fact_cmd(message)

    elif cmd in ["chatstats", "статачата", "чатстата", "чат"]:
        await process_chat_stats_cmd(message)

    # Игры
    elif cmd in ["dice", "кубик", "кость", "кости", "куб"]:
        await process_dice_cmd(message, args)
    elif cmd in ["doubledice", "2dice", "дабл", "дубль", "2кубика"]:
        await process_doubledice_cmd(message, args)
    elif cmd in ["ladder", "лесенка", "лестница", "ступень"]:
        await process_ladder_cmd(message, args)
    elif cmd in ["duel", "дуэль", "вызов", "дуели", "дуель"]:
        await process_duel_cmd(message, args)
    elif cmd in ["over", "больше", "бол", "хай", "high"]:
        await process_simple_bet(message, args, "over")
    elif cmd in ["under", "меньше", "мен", "лоу", "low"]:
        await process_simple_bet(message, args, "under")
    elif cmd in ["even", "чет", "четное", "чёт", "чётное"]:
        await process_simple_bet(message, args, "even")
    elif cmd in ["odd", "нечет", "нечетное", "нечёт", "нечётное"]:
        await process_simple_bet(message, args, "odd")
    
    # Личный кабинет
    elif cmd in ["profile", "профиль", "баланс", "balance", "stats", "стата"]:
        await process_profile_cmd(message, args)
    elif cmd in ["ref", "реф", "рефералы", "друзья", "партнерка"]:
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")
        me = await bot.get_me()
        ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
        ref_count = await db.get_referrals_count(message.from_user.id)
        text_ref = (
            f"🤝 <b>Реферальная программа</b>\n\n"
            f"Приглашай друзей и получай <b>3% от каждой их ставки</b> во всех режимах кубиков!\n\n"
            f"👥 Твоих рефералов: <b>{ref_count}</b>\n"
            f"🔗 Ссылка для приглашения:\n<code>{ref_link}</code>"
        )
        await message.reply(text_ref, parse_mode="HTML")
    elif cmd in ["top", "топ", "лидеры", "богачи"]:
        await process_top_cmd(message)
    elif cmd in ["pay", "передать", "перевод"]:
        await process_pay_cmd(message, args)

    # Админка: Выдача / Списание
    elif cmd in ["give", "выдать", "начислить", "сет", "set"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")
        
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

        target_id, target_name = None, None
        amount_raw = None

        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
            target_id, target_name = target.id, target.full_name
            if args:
                amount_raw = args[0]
        else:
            if len(args) >= 2:
                for arg in args:
                    if arg.lstrip("-").isdigit():
                        amount_raw = arg
                    else:
                        t_id, t_name, _ = await resolve_target_user(message, [arg])
                        if t_id:
                            target_id, target_name = t_id, t_name

        if not target_id or not amount_raw:
            return await message.reply("❌ Формат: <code>/give 1000 @username</code> или <code>выдать 1000</code> ответом на сообщение.", parse_mode="HTML")

        try:
            amount = int(amount_raw)
        except ValueError:
            return await message.reply("❌ Сумма должна быть числом!", parse_mode="HTML")

        await db.register_user(target_id, target_name)
        await db.change_balance(target_id, amount)
        verb = "выдал" if amount >= 0 else "забрал"
        await message.reply(f"👑 Администратор {verb} <b>{fmt_num(abs(amount))} 💰</b> у {get_mention(target_id, target_name)}!", parse_mode="HTML")

    elif cmd in ["setadmin", "админ", "датьадмина"]:
        if message.from_user.id != OWNER_ID:
            return await message.reply("❌ Только владелец бота может назначать администраторов!", parse_mode="HTML")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Укажите игрока: <code>/setadmin @username</code>", parse_mode="HTML")
        await db.add_admin(target_id)
        await message.reply(f"👑 {get_mention(target_id, target_name)} назначен администратором бота!", parse_mode="HTML")

    elif cmd in ["deladmin", "снятадмина"]:
        if message.from_user.id != OWNER_ID:
            return await message.reply("❌ Только владелец бота может снимать администраторов!", parse_mode="HTML")
        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Укажите игрока: <code>/deladmin @username</code>", parse_mode="HTML")
        await db.remove_admin(target_id)
        await message.reply(f"🚫 {get_mention(target_id, target_name)} снят с поста администратора.", parse_mode="HTML")

    elif cmd in ["mute", "мут", "завалить", "замутить"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")
        
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

        target_id, target_name, rest = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Укажите игрока: <code>/mute 10м @username Спам</code> или ответом на сообщение.", parse_mode="HTML")

        if target_id == OWNER_ID or await db.is_admin(target_id, message.chat.id):
            return await message.reply("❌ Нельзя замутить администратора!", parse_mode="HTML")
        
        duration_sec = 600
        reason = "Без причины"
        
        if rest:
            parsed = parse_time_string(rest[0])
            if parsed is not None:
                duration_sec = max(30, parsed)
                if len(rest) > 1:
                    reason = " ".join(rest[1:])
            else:
                reason = " ".join(rest)
        
        try:
            until = datetime.now() + timedelta(seconds=duration_sec)
            await message.chat.restrict(
                user_id=target_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
            await message.reply(f"🔇 {get_mention(target_id, target_name)} отправлен в мут на <b>{format_duration(duration_sec)}</b>\n📝 Причина: {html.escape(reason)}", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}", parse_mode="HTML")

    elif cmd in ["unmute", "размут", "снятьмут"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")
        
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Укажите пользователя: <code>/unmute @username</code> или ответом на сообщение.", parse_mode="HTML")

        try:
            await message.chat.restrict(
                user_id=target_id,
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
            await message.reply(f"🔊 {get_mention(target_id, target_name)} размучен.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}", parse_mode="HTML")

    elif cmd in ["ban", "бан", "забанить", "кик", "kick"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")
        
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Использование: <code>/ban @username</code> или ответом на сообщение.", parse_mode="HTML")

        if target_id == OWNER_ID or await db.is_admin(target_id, message.chat.id):
            return await message.reply("❌ Нельзя наказать администратора!", parse_mode="HTML")

        try:
            await message.chat.ban(user_id=target_id)
            await message.reply(f"🛑 {get_mention(target_id, target_name)} забанен.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ Ошибка при бане: {e}", parse_mode="HTML")

    elif cmd in ["unban", "разбан", "снятьбан"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")
        
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Использование: <code>/unban @username</code> или <code>/unban 12345678</code>", parse_mode="HTML")

        try:
            await message.chat.unban(user_id=target_id, only_if_banned=True)
            await message.reply(f"✅ Пользователь {get_mention(target_id, target_name)} успешно разбанен в чате!", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ Ошибка разбана: {e}", parse_mode="HTML")

    elif cmd in ["warn", "варн", "пред", "предупреждение"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")
        
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Укажите игрока: <code>/warn @username</code> или ответом на сообщение.", parse_mode="HTML")

        if target_id == OWNER_ID or await db.is_admin(target_id, message.chat.id):
            return await message.reply("❌ Нельзя выдать варн администратору!", parse_mode="HTML")

        await db.register_user(target_id, target_name)
        warns = await db.add_warn(target_id)
        if warns >= 3:
            try:
                await message.chat.ban(user_id=target_id)
                await db.reset_warns(target_id)
                await message.reply(f"🛑 {get_mention(target_id, target_name)} набрал <b>3/3 варнов</b> и получил бан!", parse_mode="HTML")
            except Exception as e:
                await message.reply(f"❌ Ошибка при бане: {e}")
        else:
            await message.reply(f"⚠️ {get_mention(target_id, target_name)} получил варн (<b>{warns}/3</b>)!", parse_mode="HTML")

    elif cmd in ["unwarn", "снятьварн", "разварн", "снятьпред"]:
        if not await db.is_admin(message.from_user.id, message.chat.id):
            return await message.reply("❌ У вас нет прав администратора!", parse_mode="HTML")
        
        if not await check_subscription(message.from_user.id):
            return await message.reply("⚠️ <b>Администраторам также необходимо подписаться на канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

        target_id, target_name, _ = await resolve_target_user(message, args)
        if not target_id:
            return await message.reply("❌ Укажите игрока: <code>/unwarn @username</code> или ответом на сообщение.", parse_mode="HTML")

        await db.reset_warns(target_id)
        await message.reply(f"✅ Предупреждения игрока {get_mention(target_id, target_name)} аннулированы.", parse_mode="HTML")


# ================= ЗАПУСК =================
async def handle_ping(request):
    return web.Response(text="Duel Cubes Bot is alive! 🎲", status=200)


async def on_startup(bot: Bot):
    await db.init()
    
    commands = [
        BotCommand(command="start", description="Главное меню 🎲"),
        BotCommand(command="work", description="Забрать накопленную зарплату 💼"),
        BotCommand(command="sponsor", description="Бонус за спонсора (+50k) 📢"),
        BotCommand(command="check", description="Создать чек-раздачу в чате 🎁"),
        BotCommand(command="fact", description="Интересный факт о Telegram 💡"),
        BotCommand(command="dice", description="Кубик против бота 🤖"),
        BotCommand(command="doubledice", description="2 кубика (x3 за дубль) 🎲🎲"),
        BotCommand(command="ladder", description="Кубическая лесенка до x7.5 🚀"),
        BotCommand(command="duel", description="Дуэль 1v1 в чате ⚔️"),
        BotCommand(command="chatstats", description="Статистика чата 📊"),
        BotCommand(command="over", description="Больше (4-6) 📈"),
        BotCommand(command="under", description="Меньше (1-3) 📉"),
        BotCommand(command="even", description="Чётное число ⚖️"),
        BotCommand(command="odd", description="Нечётное число 🎲"),
        BotCommand(command="profile", description="Мой профиль и баланс 👤"),
        BotCommand(command="ref", description="Реферальная ссылка (+3%) 🤝"),
        BotCommand(command="pay", description="Передать монеты 💸"),
        BotCommand(command="top", description="Топ богачей 🏆"),
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