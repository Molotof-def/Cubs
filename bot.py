import os
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from aiogram.types import BotCommand
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatPermissions,
    PreCheckoutQuery,
    LabeledPrice
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
    exit("❌ ОШИБКА: DATABASE_URL не найден! Добавьте подключение к PostgreSQL в Render.")

# Имя канала с собачкой, например: @DuelCubesChannel
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@DuelCubesChannel").strip()

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

active_duels: Dict[str, dict] = {}
# Хранилище активных чеков: {check_id: {...}}
active_checks: Dict[str, dict] = {}
active_miners: Dict[int, dict] = {}
async def on_startup(bot: Bot):
    await db.init()
    
    # Регистрация всплывающих команд при вводе '/'
    commands = [
        BotCommand(command="start", description="Главное меню 🎲"),
        BotCommand(command="dice", description="Кубик против бота 🤖"),
        BotCommand(command="doubledice", description="Бросок 2 кубиков (x3 за дубль) 🎲🎲"),
        BotCommand(command="duel", description="Вызвать игрока на дуэль ⚔️"),
        BotCommand(command="profile", description="Мой профиль и баланс 👤"),
        BotCommand(command="ref", description="Реферальная ссылка (+3%) 🤝"),
        BotCommand(command="pay", description="Передать монеты игроку 💸"),
        BotCommand(command="stars", description="Пополнить баланс за Stars ⭐"),
        BotCommand(command="top", description="Топ богачей 🏆"),
        BotCommand(command="promo", description="Активировать промокод 🎟"),
    ]
    await bot.set_my_commands(commands)
    
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        logging.info(f"Установка Webhook: {webhook_url}")
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
    else:
        logging.info("RENDER_EXTERNAL_URL не задан, сервер ожидает локального запуска.")
def get_mention(user_id: int, name: str) -> str:
    safe_name = (name or "Игрок").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


async def check_subscription(user_id: int) -> bool:
    if not REQUIRED_CHANNEL or REQUIRED_CHANNEL == "@твой_канал":
        return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ["creator", "administrator", "member", "restricted"]
    except Exception as e:
        logging.warning(f"⚠️ Ошибка проверки подписки: {e}. Проверьте права бота в канале!")
        return True


def sub_keyboard():
    builder = InlineKeyboardBuilder()
    clean_tag = REQUIRED_CHANNEL.replace("@", "")
    channel_url = f"https://t.me/{clean_tag}"
    builder.button(text="📢 Подписаться на канал", url=channel_url)
    return builder.as_markup()


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
                    balance BIGINT DEFAULT 0,
                    turnover BIGINT DEFAULT 0,
                    wins INT DEFAULT 0,
                    losses INT DEFAULT 0,
                    draws INT DEFAULT 0,
                    warns INT DEFAULT 0
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
                "SELECT user_id, username, balance, turnover, wins, losses, draws, warns, referrer_id FROM users WHERE user_id = $1",
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
        async with self.pool.acquire() as conn:
            ref_id = await conn.fetchval("SELECT referrer_id FROM users WHERE user_id = $1", loser_id)
            if ref_id and ref_id != loser_id:
                reward = max(1, int(lost_amount * 0.03))  # 3% от проигрыша
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", reward, ref_id)
                try:
                    await bot.send_message(
                        chat_id=ref_id,
                        text=f"🤝 <b>Реферальный бонус!</b>\nВаш реферал проиграл <code>{lost_amount} 💰</code> в кубиках. Вам начислено 3%: <b>+{reward} 💰</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

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


# ================= КЛАВИАТУРЫ =================
def duel_keyboard(duel_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="⚔️ Принять вызов", callback_data=f"accept_{duel_id}")
    builder.button(text="❌ Отклонить", callback_data=f"decline_{duel_id}")
    builder.adjust(2)
    return builder.as_markup()


def check_keyboard(check_id: str, claimed: int, total: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💰 Забрать куш ({claimed}/{total})", callback_data=f"claim_check_{check_id}")
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
        f"💰 Твой баланс: <b>{balance} монет</b>\n\n"
        f"📜 <b>Игровые команды:</b>\n"
        f"⚔️ <code>/duel [ставка]</code> (ответом) — дуэль 1 vs 1\n"
        f"🎲 <code>/dice [ставка]</code> — бросить кубик против бота\n"
        f"🎲🎲 <code>/doubledice [ставка]</code> — 2 кубика (x3 за дубль!)\n\n"
        f"💳 <b>Финансы и Профиль:</b>\n"
        f"🤝 <code>/ref</code> — реферальная система (3% с проигрышей друзей)\n"
        f"⭐ <code>/stars [кол-во]</code> — купить монеты за Stars\n"
        f"💸 <code>/pay [сумма]</code> (ответом) — передать монеты\n"
        f"👤 <code>/profile</code> — профиль и статистика\n"
        f"🏆 <code>/top</code> — богатейшие игроки\n"
        f"🎟 <code>/promo [код]</code> — активировать промокод (попробуй: <code>/promo START</code>)"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    ref_count = await db.get_referrals_count(message.from_user.id)

    text = (
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей и получай <b>3% от каждого их проигрыша</b> в любых режимах кубиков пожизненно!\n\n"
        f"👥 Приглашено рефералов: <b>{ref_count}</b>\n"
        f"🔗 Твоя ссылка для приглашения:\n<code>{ref_link}</code>"
    )
    await message.answer(text, parse_mode="HTML")

# 📈 БОЛЬШЕ (4, 5, 6)
@dp.message(Command("over"))
async def cmd_over(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 15
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>БОЛЬШЕ (4-6)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = dice_msg.dice.value

    if val in [4, 5, 6]:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ]\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
    else:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ]\n📉 Потеряно: <b>-{bet} 💰</b>"

    await message.answer(res, parse_mode="HTML")


# 📉 МЕНЬШЕ (1, 2, 3)
@dp.message(Command("under"))
async def cmd_under(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 15
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>МЕНЬШЕ (1-3)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = dice_msg.dice.value

    if val in [1, 2, 3]:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ]\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
    else:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ]\n📉 Потеряно: <b>-{bet} 💰</b>"

    await message.answer(res, parse_mode="HTML")
# ⚖️ ЧЁТНОЕ (2, 4, 6)
@dp.message(Command("even"))
async def cmd_even(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 15
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>ЧЁТНОЕ (2, 4, 6)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = dice_msg.dice.value

    if val % 2 == 0:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ] (Чётное)\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
    else:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ] (Нечётное)\n📉 Потеряно: <b>-{bet} 💰</b>"

    await message.answer(res, parse_mode="HTML")


# 🎲 НЕЧЁТНОЕ (1, 3, 5)
@dp.message(Command("odd"))
async def cmd_odd(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 15
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 {get_mention(user_id, message.from_user.full_name)} поставил на <b>НЕЧЁТНОЕ (1, 3, 5)</b>:", parse_mode="HTML")
    dice_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    val = dice_msg.dice.value

    if val % 2 != 0:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = f"🏆 <b>ПОБЕДА!</b> Выпало: [ <b>{val}</b> ] (Нечётное)\n💰 Множитель: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
    else:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        res = f"💀 <b>ПОРАЖЕНИЕ!</b> Выпало: [ <b>{val}</b> ] (Чётное)\n📉 Потеряно: <b>-{bet} 💰</b>"

    await message.answer(res, parse_mode="HTML")
MINER_MULTS = {
    0: 0.95,
    1: 1.2,
    2: 1.5,
    3: 2.0,
    4: 2.8,
    5: 3.8
}

def render_miner_field(opened_cells: set, mine_index: int, show_mine: bool = False) -> str:
    field = []
    for i in range(1, 7):
        if i in opened_cells:
            field.append("💎")
        elif show_mine and i == mine_index:
            field.append("💣")
        else:
            field.append("⬛️")
    return "".join(field)

def miner_keyboard(user_id: int, current_mult: float, next_mult: Optional[float]):
    builder = InlineKeyboardBuilder()
    if next_mult is not None:
        builder.button(text=f"🎲 Кинуть куб (след. x{next_mult})", callback_data=f"miner_roll_{user_id}")
    builder.button(text=f"💰 Забрать (x{current_mult})", callback_data=f"miner_cash_{user_id}")
    builder.adjust(1)
    return builder.as_markup()

# Запуск игры Сапёр
@dp.message(Command("miner"))
async def cmd_miner(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    if user_id in active_miners:
        return await message.answer("❌ У вас уже запущена игра в Сапёра! Завершите её.")

    bet = 20
    if command.args and command.args.isdigit():
        bet = int(command.args)
    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    mine_pos = random.randint(1, 6)
    active_miners[user_id] = {
        "bet": bet,
        "mine": mine_pos,
        "opened": set(),
        "step": 0
    }

    text = (
        f"💣 <b>КУБИЧЕСКИЙ САПЁР (1x6)</b>\n\n"
        f"Поле: {render_miner_field(set(), mine_pos)}\n"
        f"💰 Ставка: <b>{bet} 💰</b>\n"
        f"💎 Открыто клеток: <b>0/5</b>\n"
        f"📈 Текущий множитель: <b>x0.95</b> (забрать: <b>{int(bet * 0.95)} 💰</b>)\n\n"
        f"<i>Бросайте кубик, чтобы открывать клетки (1-6). На поле спрятана 1 мина!</i>"
    )
    await message.answer(text, reply_markup=miner_keyboard(user_id, 0.95, 1.2), parse_mode="HTML")

# Нажатие «Кинуть куб»
@dp.callback_query(F.data.startswith("miner_roll_"))
async def cb_miner_roll(call: CallbackQuery):
    user_id = int(call.data.replace("miner_roll_", ""))
    if call.from_user.id != user_id:
        return await call.answer("❌ Это не ваша игра!", show_alert=True)

    if user_id not in active_miners:
        return await call.answer("❌ Игра уже завершена!", show_alert=True)

    game = active_miners[user_id]
    await call.message.edit_reply_markup(reply_markup=None)

    await call.message.answer(f"🎲 Бросок кубика для {get_mention(user_id, call.from_user.full_name)}:", parse_mode="HTML")
    dice_msg = await call.message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    rolled_cell = dice_msg.dice.value

    # Проверка на мину
    if rolled_cell == game["mine"]:
        bet = game["bet"]
        del active_miners[user_id]
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)

        field_view = render_miner_field(game["opened"], game["mine"], show_mine=True)
        res = (
            f"💥 <b>БАБАХ! Выпало число [{rolled_cell}] — ТАМ МИНА!</b>\n\n"
            f"Поле: {field_view}\n"
            f"💀 Вы подорвались и потеряли: <b>-{bet} 💰</b>"
        )
        return await call.message.answer(res, parse_mode="HTML")

    # Если клетка уже была открыта ранее
    if rolled_cell in game["opened"]:
        current_mult = MINER_MULTS[game["step"]]
        next_step = game["step"] + 1
        next_mult = MINER_MULTS.get(next_step)
        field_view = render_miner_field(game["opened"], game["mine"])
        res = (
            f"🔄 Выпало число [ <b>{rolled_cell}</b> ], но эта клетка уже была открыта!\n"
            f"Множитель остался прежним.\n\n"
            f"Поле: {field_view}\n"
            f"💎 Открыто: <b>{game['step']}/5</b> | Множитель: <b>x{current_mult}</b>"
        )
        return await call.message.answer(res, reply_markup=miner_keyboard(user_id, current_mult, next_mult), parse_mode="HTML")

    # Открытие новой безопасной клетки
    game["opened"].add(rolled_cell)
    game["step"] += 1
    step = game["step"]
    current_mult = MINER_MULTS[step]
    field_view = render_miner_field(game["opened"], game["mine"])

    if step >= 5:  # Открыты все 5 безопасных клеток
        win = int(game["bet"] * current_mult)
        del active_miners[user_id]
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        res = (
            f"👑 <b>ФАНТАСТИКА! ВСЕ БЕЗОПАСНЫЕ КЛЕТКИ ОТКРЫТЫ!</b>\n\n"
            f"Поле: {field_view}\n"
            f"🔥 Максимальный множитель: <b>x{current_mult}</b>\n"
            f"💵 Выигрыш: <b>+{win} 💰</b>"
        )
        return await call.message.answer(res, parse_mode="HTML")

    next_mult = MINER_MULTS.get(step + 1)
    res = (
        f"💎 <b>УСПЕХ! Открыта чистая клетка [{rolled_cell}]!</b>\n\n"
        f"Поле: {field_view}\n"
        f"💎 Открыто клеток: <b>{step}/5</b>\n"
        f"📈 Текущий множитель: <b>x{current_mult}</b> (забрать: <b>{int(game['bet'] * current_mult)} 💰</b>)"
    )
    await call.message.answer(res, reply_markup=miner_keyboard(user_id, current_mult, next_mult), parse_mode="HTML")


# Нажатие «Забрать»
@dp.callback_query(F.data.startswith("miner_cash_"))
async def cb_miner_cash(call: CallbackQuery):
    user_id = int(call.data.replace("miner_cash_", ""))
    if call.from_user.id != user_id:
        return await call.answer("❌ Это не ваша игра!", show_alert=True)

    if user_id not in active_miners:
        return await call.answer("❌ Игра уже завершена!", show_alert=True)

    game = active_miners[user_id]
    mult = MINER_MULTS[game["step"]]
    win = int(game["bet"] * mult)

    del active_miners[user_id]
    await db.change_balance(user_id, win)
    await db.record_game(user_id, "win" if mult >= 1.0 else "loss")

    field_view = render_miner_field(game["opened"], game["mine"], show_mine=True)
    await call.message.edit_reply_markup(reply_markup=None)

    res = (
        f"💰 <b>ВЫ УСПЕШНО ЗАБРАЛИ КУШ!</b>\n\n"
        f"Поле: {field_view}\n"
        f"💎 Открыто клеток: <b>{game['step']}/5</b>\n"
        f"📈 Итоговый множитель: <b>x{mult}</b>\n"
        f"💵 На баланс зачислено: <b>+{win} 💰</b>"
    )
    await call.message.answer(res, parse_mode="HTML")
@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)
    user = await db.get_user(user_id)

    _, name, balance, turnover, wins, losses, draws, warns, _ = user
    total_games = wins + losses + draws
    winrate = round((wins / total_games * 100), 1) if total_games > 0 else 0

    text = (
        f"┏ 👤 <b>Профиль игрока:</b> {get_mention(user_id, name)}\n"
        f"┣ 🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"┣ 💰 <b>Баланс:</b> <code>{balance} 💰</code>\n"
        f"┣ 🔄 <b>Оборот:</b> <code>{turnover} 💰</code>\n"
        f"┣ 🎮 <b>Всего игр:</b> <code>{total_games}</code>\n"
        f"┣ 🏆 <b>Побед:</b> <code>{wins}</code> | 💀 <b>Поражений:</b> <code>{losses}</code> | ⚖️ <b>Ничьих:</b> <code>{draws}</code>\n"
        f"┣ 📈 <b>Винрейт:</b> <code>{winrate}%</code>\n"
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
        return await message.answer("❌ Формат: <code>/pay 50</code>", parse_mode="HTML")

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


# ================= 🎁 ЧЕКИ И РАЗДАЧИ В ЧАТ (/check, /drop) =================
def check_keyboard(check_id: str, claimed: int, total: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💰 Забрать куш ({claimed}/{total})", callback_data=f"claim_check_{check_id}")
    return builder.as_markup()

# Команда создания чека (/check 500 5)
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
    if total_amount < people_count or people_count <= 0:
        return await message.answer("❌ Некорректная сумма или количество!", parse_mode="HTML")

    user = await db.get_user(user_id)
    if not user or user[2] < total_amount:
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{user[2] if user else 0} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -total_amount)
    check_id = f"chk_{message.chat.id}_{random.randint(10000, 99999)}"
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
        f"👥 Активаций: <b>{people_count}</b> (по <b>{per_person} 💰</b> каждому)\n\n"
        f"<i>Жми на кнопку ниже, чтобы забрать!</i>"
    )
    await message.answer(text, reply_markup=check_keyboard(check_id, 0, people_count), parse_mode="HTML")

# Обработка нажатия на кнопку чека
@dp.callback_query(F.data.startswith("claim_check_"))
async def cb_claim_check(call: CallbackQuery):
    check_id = call.data.replace("claim_check_", "")
    user_id = call.from_user.id

    if check_id not in active_checks:
        return await call.answer("❌ Чек уже закончился!", show_alert=True)

    check = active_checks[check_id]
    if user_id in check["claimed_users"]:
        return await call.answer("❌ Вы уже забирали этот чек!", show_alert=True)

    if not await check_subscription(user_id):
        return await call.answer("⚠️ Подпишитесь на наш канал, чтобы забирать чеки!", show_alert=True)

    await db.register_user(user_id, call.from_user.full_name, call.from_user.username)
    check["claimed_users"].append(user_id)
    await db.change_balance(user_id, check["per_person"])

    claimed_count = len(check["claimed_users"])
    await call.answer(f"🎉 Вы забрали +{check['per_person']} 💰!", show_alert=True)

    if claimed_count >= check["total_people"]:
        del active_checks[check_id]
        await call.message.edit_text(
            f"🧧 <b>ЧЕК ПОЛНОСТЬЮ РАЗОБРАН!</b>\n"
            f"👤 Создатель: {get_mention(check['creator_id'], check['creator_name'])}\n"
            f"💰 Раздал: <b>{check['total_amount']} 💰</b> на <b>{check['total_people']}</b> человек!",
            parse_mode="HTML"
        )
    else:
        await call.message.edit_reply_markup(reply_markup=check_keyboard(check_id, claimed_count, check["total_people"]))

@dp.callback_query(F.data.startswith("claim_check_"))
async def cb_claim_check(call: CallbackQuery):
    check_id = call.data.replace("claim_check_", "")
    user_id = call.from_user.id

    if check_id not in active_checks:
        return await call.answer("❌ Этот чек уже полностью разобран или недействителен!", show_alert=True)

    check = active_checks[check_id]

    if user_id in check["claimed_users"]:
        return await call.answer("❌ Вы уже активировали этот чек!", show_alert=True)

    if not await check_subscription(user_id):
        return await call.answer("⚠️ Подпишитесь на наш канал, чтобы забирать чеки!", show_alert=True)

    await db.register_user(user_id, call.from_user.full_name, call.from_user.username)

    check["claimed_users"].append(user_id)
    reward = check["per_person"]
    await db.change_balance(user_id, reward)

    claimed_count = len(check["claimed_users"])
    total_people = check["total_people"]

    await call.answer(f"🎉 Вы успешно забрали +{reward} 💰!", show_alert=True)

    if claimed_count >= total_people:
        # Чек закончился
        del active_checks[check_id]
        await call.message.edit_text(
            f"🧧 <b>ДЕНЕЖНЫЙ ЧЕК ЗАВЕРШЁН!</b>\n\n"
            f"👤 Создатель: {get_mention(check['creator_id'], check['creator_name'])}\n"
            f"💰 Раздал: <b>{check['total_amount']} 💰</b> на <b>{total_people}</b> человек!\n"
            f"✅ Все доли успешно получены игроками.",
            parse_mode="HTML"
        )
    else:
        # Обновляем счетчик на кнопке
        await call.message.edit_reply_markup(reply_markup=check_keyboard(check_id, claimed_count, total_people))


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
        await message.answer(
            f"🎉 <b>Оплата прошла успешно!</b>\n\n"
            f"⭐ Списано: <code>{message.successful_payment.total_amount} Stars</code>\n"
            f"💰 Зачислено: <b>+{coins} монет</b>",
            parse_mode="HTML"
        )


# ================= КУБИК ПРОТИВ БОТА (/dice) [ИСПРАВЛЕНО] =================
@dp.message(Command("dice"))
async def cmd_dice(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 15
    if command.args and command.args.isdigit():
        bet = int(command.args)

    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        bal = user[2] if user else 0
        return await message.answer(f"❌ Недостаточно средств! Твой баланс: <b>{bal} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 Бросок {get_mention(user_id, message.from_user.full_name)}:", parse_mode="HTML")
    p_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    p_val = p_msg.dice.value

    await message.answer("🤖 Бросок Бота:", parse_mode="HTML")
    b_msg = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    b_val = b_msg.dice.value

    if p_val > b_val:
        win = int(bet * 1.95)
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        text = f"🏆 <b>ПОБЕДА!</b> ({p_val} > {b_val})\n\n💰 Коэффициент: <b>x1.95</b>\n💵 Выигрыш: <b>+{win} 💰</b>"
    elif p_val < b_val:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        text = f"💀 <b>ПОРАЖЕНИЕ!</b> ({p_val} < {b_val})\n\n📉 Потеряно: <b>-{bet} 💰</b>"
    else:
        await db.change_balance(user_id, bet)
        await db.record_game(user_id, "draw")
        text = (
            f"╔════════════════════╗\n"
            f"      ⚖️ <b>БОЕВАЯ НИЧЬЯ!</b> ⚖️\n"
            f"╚════════════════════╝\n\n"
            f"🎲 Игрок: [ <b>{p_val}</b> ] ⚡ Бот: [ <b>{b_val}</b> ]\n"
            f"💰 <b>Возврат:</b> <code>+{bet} 💰</code>"
        )

    await message.answer(text, parse_mode="HTML")

# ================= 2 КУБИКА (/doubledice) [ИСПРАВЛЕНО] =================
@dp.message(Command("doubledice"))
async def cmd_double_dice(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name, message.from_user.username)

    if not await check_subscription(user_id):
        return await message.answer("⚠️ <b>Для игры необходимо подписаться на наш канал!</b>", reply_markup=sub_keyboard(), parse_mode="HTML")

    bet = 20
    if command.args and command.args.isdigit():
        bet = int(command.args)

    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if not user or user[2] < bet:
        bal = user[2] if user else 0
        return await message.answer(f"❌ Недостаточно средств! Баланс: <b>{bal} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲🎲 <b>Бросок двух кубиков {get_mention(user_id, message.from_user.full_name)}:</b>", parse_mode="HTML")
    p_d1 = await message.answer_dice(emoji="🎲")
    p_d2 = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    p1 = p_d1.dice.value
    p2 = p_d2.dice.value
    p_sum = p1 + p2

    await message.answer("🤖 <b>Бросок двух кубиков Бота:</b>", parse_mode="HTML")
    b_d1 = await message.answer_dice(emoji="🎲")
    b_d2 = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    b1 = b_d1.dice.value
    b2 = b_d2.dice.value
    b_sum = b1 + b2

    if p_sum > b_sum:
        is_double = (p1 == p2)
        mult = 3.0 if is_double else 1.95
        win = int(bet * mult)

        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")

        bonus_title = "🔥 <b>МЕГА-ДУБЛЬ (x3.0)!</b>\n" if is_double else f"Коэффициент: <b>x{mult}</b>\n"
        res = (
            f"🎉 <b>ПОБЕДА!</b>\n\n"
            f"👤 Твои очки: {p1} + {p2} = <b>{p_sum}</b>\n"
            f"🤖 Очки бота: {b1} + {b2} = <b>{b_sum}</b>\n\n"
            f"{bonus_title}💵 Выигрыш: <b>+{win} 💰</b>"
        )
    elif p_sum < b_sum:
        await db.record_game(user_id, "loss")
        await db.process_referral_loss(user_id, bet)
        res = f"💀 <b>ПОРАЖЕНИЕ!</b>\n\n👤 {p1} + {p2} = <b>{p_sum}</b>\n🤖 {b1} + {b2} = <b>{b_sum}</b>\n\n📉 Потеряно: <b>-{bet} 💰</b>"
    else:
        await db.change_balance(user_id, bet)
        await db.record_game(user_id, "draw")
        res = f"╔════════════════════╗\n    ⚖️ <b>DOUBLE НИЧЬЯ! ({p_sum} = {b_sum})</b> ⚖️\n╚════════════════════╝\n\n💰 Ставка <b>{bet} 💰</b> возвращена!"

    await message.answer(res, parse_mode="HTML")


# ================= ДУЭЛИ (/duel) =================
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

    bet = 15
    if command.args and command.args.isdigit():
        bet = int(command.args)

    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    await db.register_user(opponent.id, opponent.full_name, opponent.username)

    c_data = await db.get_user(challenger.id)
    o_data = await db.get_user(opponent.id)

    if not c_data or c_data[2] < bet:
        return await message.answer("❌ У вас недостаточно монет!")
    if not o_data or o_data[2] < bet:
        return await message.answer("❌ У оппонента недостаточно монет!")

    duel_id = f"{message.chat.id}_{challenger.id}_{opponent.id}_{random.randint(1000, 9999)}"

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


@dp.callback_query(F.data.startswith("accept_"))
async def cb_accept_duel(call: CallbackQuery):
    duel_id = call.data.replace("accept_", "")
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
    c_val = c_dice.dice.value

    await call.message.answer(f"🔵 Бросает {get_mention(o_id, duel['opponent_name'])}:", parse_mode="HTML")
    o_dice = await call.message.answer_dice(emoji="🎲")
    await asyncio.sleep(4.0)
    o_val = o_dice.dice.value

    win_sum = int(bet * 1.95)

    if c_val > o_val:
        await db.change_balance(c_id, win_sum)
        await db.record_game(c_id, "win")
        await db.record_game(o_id, "loss")
        await db.process_referral_loss(o_id, bet)
        res = (
            f"🏆 <b>ПОБЕДИТЕЛЬ:</b> {get_mention(c_id, duel['challenger_name'])} ({c_val})\n"
            f"💀 <b>ПРОИГРАВШИЙ:</b> {get_mention(o_id, duel['opponent_name'])} ({o_val}) [ -{bet} 💰 ]\n\n"
            f"💵 Выигрыш победителя: <b>+{win_sum} 💰</b>"
        )
    elif o_val > c_val:
        await db.change_balance(o_id, win_sum)
        await db.record_game(o_id, "win")
        await db.record_game(c_id, "loss")
        await db.process_referral_loss(c_id, bet)
        res = (
            f"🏆 <b>ПОБЕДИТЕЛЬ:</b> {get_mention(o_id, duel['opponent_name'])} ({o_val})\n"
            f"💀 <b>ПРОИГРАВШИЙ:</b> {get_mention(c_id, duel['challenger_name'])} ({c_val}) [ -{bet} 💰 ]\n\n"
            f"💵 Выигрыш победителя: <b>+{win_sum} 💰</b>"
        )
    else:
        await db.change_balance(c_id, bet)
        await db.change_balance(o_id, bet)
        await db.record_game(c_id, "draw")
        await db.record_game(o_id, "draw")
        res = f"╔════════════════════╗\n      ⚖️ <b>БОЕВАЯ НИЧЬЯ!</b> ⚖️\n╚════════════════════╝\n\n💰 Ставки возвращены (+{bet} 💰 каждому)."

    del active_duels[duel_id]
    await call.message.answer(res, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("decline_"))
async def cb_decline_duel(call: CallbackQuery):
    duel_id = call.data.replace("decline_", "")
    if duel_id not in active_duels:
        return await call.answer("❌ Дуэль уже неактивна!", show_alert=True)

    duel = active_duels[duel_id]
    if call.from_user.id not in [duel["opponent_id"], duel["challenger_id"]]:
        return await call.answer("❌ Вы не участвуете в этой дуэли!", show_alert=True)
    del active_duels[duel_id]
    await call.message.edit_text("❌ <b>Дуэль была отклонена.</b>", parse_mode="HTML")
    await call.answer()


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
        safe_name = name.replace("<", "&lt;").replace(">", "&gt;") if name else "Аноним"
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


# ================= АДМИНКА И МОДЕРАЦИЯ =================
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

    target = message.reply_to_message.from_user
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
                return await message.answer("❌ Пользователь с таким @username не найден в БД!")

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
    if not await db.is_admin(message.from_user.id) or not message.reply_to_message:
        return

    target = message.reply_to_message.from_user
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
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        logging.info(f"Установка Webhook: {webhook_url}")
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
    else:
        logging.info("RENDER_EXTERNAL_URL не задан, сервер ожидает локального запуска.")


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