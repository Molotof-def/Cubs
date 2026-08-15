import os
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Dict

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatPermissions
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ================= НАСТРОЙКИ СЕРВЕРА (БЕЗОПАСНЫЕ) =================
# Бот берет данные ТОЛЬКО из переменных окружения сервера
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("❌ ОШИБКА: Токен бота не найден! Добавьте BOT_TOKEN в Environment Variables на Render.")

OWNER_ID_STR = os.getenv("OWNER_ID")
if not OWNER_ID_STR:
    exit("❌ ОШИБКА: Ваш ID не найден! Добавьте OWNER_ID в Environment Variables на Render.")
OWNER_ID = int(OWNER_ID_STR)

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render подставляет сам
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"
DB_FILE = "dice_server.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Активные вызовы дуэлей: {duel_id: {...}}
active_duels: Dict[str, dict] = {}


def get_mention(user_id: int, name: str) -> str:
    safe_name = name.replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


# ================= БАЗА ДАННЫХ (AIOSQLITE) =================
class Database:
    def __init__(self, db_file: str):
        self.db_file = db_file

    async def init(self):
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    balance INTEGER DEFAULT 100,
                    turnover INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    draws INTEGER DEFAULT 0,
                    last_bonus TEXT DEFAULT NULL,
                    warns INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_admins (
                    user_id INTEGER PRIMARY KEY
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    reward INTEGER,
                    uses_left INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS promo_history (
                    user_id INTEGER,
                    code TEXT,
                    PRIMARY KEY (user_id, code)
                )
            """)
            await db.commit()

    async def register_user(self, user_id: int, username: str):
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("""
                INSERT INTO users (user_id, username) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """, (user_id, username))
            await db.commit()

    async def get_user(self, user_id: int):
        async with aiosqlite.connect(self.db_file) as db:
            async with db.execute(
                "SELECT user_id, username, balance, turnover, wins, losses, draws, last_bonus, warns FROM users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                return await cursor.fetchone()

    async def change_balance(self, user_id: int, amount: int):
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            await db.commit()

    async def add_turnover(self, user_id: int, amount: int):
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("UPDATE users SET turnover = turnover + ? WHERE user_id = ?", (abs(amount), user_id))
            await db.commit()

    async def record_game(self, user_id: int, status: str):
        col = "wins" if status == "win" else ("losses" if status == "loss" else "draws")
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute(f"UPDATE users SET {col} = {col} + 1 WHERE user_id = ?", (user_id,))
            await db.commit()

    async def claim_daily_bonus(self, user_id: int) -> tuple[bool, int, str]:
        user = await self.get_user(user_id)
        if not user:
            return False, 0, "Сначала напишите /start"

        last_bonus_str = user[7]
        now = datetime.now()

        if last_bonus_str:
            last_bonus = datetime.fromisoformat(last_bonus_str)
            if now - last_bonus < timedelta(hours=24):
                remaining = timedelta(hours=24) - (now - last_bonus)
                hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                return False, 0, f"⏳ Бонус уже получен! Приходи через <b>{hours}ч {minutes}м</b>."

        bonus = random.randint(25, 100)
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute(
                "UPDATE users SET balance = balance + ?, last_bonus = ? WHERE user_id = ?",
                (bonus, now.isoformat(), user_id)
            )
            await db.commit()
        return True, bonus, f"🎁 Ты забрал ежедневный бонус: <b>+{bonus} 💰</b>!"

    async def get_top(self, order_by="balance", limit=10):
        async with aiosqlite.connect(self.db_file) as db:
            async with db.execute(f"SELECT username, {order_by} FROM users ORDER BY {order_by} DESC LIMIT ?", (limit,)) as cursor:
                return await cursor.fetchall()

    async def is_admin(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        async with aiosqlite.connect(self.db_file) as db:
            async with db.execute("SELECT 1 FROM bot_admins WHERE user_id = ?", (user_id,)) as cursor:
                return (await cursor.fetchone()) is not None

    async def add_admin(self, user_id: int):
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def add_warn(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("UPDATE users SET warns = warns + 1 WHERE user_id = ?", (user_id,))
            await db.commit()
            async with db.execute("SELECT warns FROM users WHERE user_id = ?", (user_id,)) as cursor:
                res = await cursor.fetchone()
                return res[0] if res else 1

    async def reset_warns(self, user_id: int):
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("UPDATE users SET warns = 0 WHERE user_id = ?", (user_id,))
            await db.commit()

    async def create_promo(self, code: str, reward: int, uses: int) -> bool:
        async with aiosqlite.connect(self.db_file) as db:
            try:
                await db.execute("INSERT INTO promo_codes VALUES (?, ?, ?)", (code.upper(), reward, uses))
                await db.commit()
                return True
            except:
                return False

    async def activate_promo(self, user_id: int, code: str) -> tuple[bool, str]:
        code = code.upper()
        async with aiosqlite.connect(self.db_file) as db:
            async with db.execute("SELECT 1 FROM promo_history WHERE user_id = ? AND code = ?", (user_id, code)) as c1:
                if await c1.fetchone():
                    return False, "❌ Вы уже активировали этот промокод!"

            async with db.execute("SELECT reward, uses_left FROM promo_codes WHERE code = ?", (code,)) as c2:
                row = await c2.fetchone()
                if not row:
                    return False, "❌ Промокод не найден!"

            reward, uses_left = row
            if uses_left <= 0:
                return False, "❌ У этого промокода закончились активации!"

            await db.execute("UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = ?", (code,))
            await db.execute("INSERT INTO promo_history VALUES (?, ?)", (user_id, code))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, user_id))
            await db.commit()
            return True, f"🎉 Промокод активирован! Получено: <b>+{reward} 💰</b>"


db = Database(DB_FILE)


# ================= КЛАВИАТУРА ДУЭЛИ =================
def duel_keyboard(duel_id: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="⚔️ Принять дуэль", callback_data=f"accept_{duel_id}")
    builder.button(text="❌ Отклонить", callback_data=f"decline_{duel_id}")
    builder.adjust(2)
    return builder.as_markup()


# ================= ОСНОВНЫЕ КОМАНДЫ =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await db.register_user(message.from_user.id, message.from_user.full_name)
    user = await db.get_user(message.from_user.id)

    text = (
        f"🎲 <b>Добро пожаловать в Бот Кубиков!</b>\n\n"
        f"👤 Игрок: {get_mention(message.from_user.id, message.from_user.full_name)}\n"
        f"💰 Баланс: <b>{user[2]} монет</b>\n\n"
        f"📜 <b>Команды:</b>\n"
        f"⚔️ <code>/duel [ставка]</code> (ответом на сообщение) — бросить вызов игроку\n"
        f"🤖 <code>/dice [ставка]</code> — бросить кубик против бота\n"
        f"🎁 <code>/bonus</code> — ежедневный бонус монет\n"
        f"👤 <code>/profile</code> — твоя статистика\n"
        f"💸 <code>/pay [сумма]</code> (ответом) — передать монеты другу\n"
        f"🏆 <code>/top</code> — таблица лидеров\n"
        f"🎟 <code>/promo [код]</code> — активировать промокод"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name)
    user = await db.get_user(user_id)

    _, name, balance, turnover, wins, losses, draws, _, warns = user
    total_games = wins + losses + draws
    winrate = round((wins / total_games * 100), 1) if total_games > 0 else 0

    text = (
        f"┏ 👤 <b>Профиль игрока:</b> {get_mention(user_id, name)}\n"
        f"┣ 🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"┣ 💰 <b>Баланс:</b> <code>{balance} 💰</code>\n"
        f"┣ 🔄 <b>Оборот:</b> <code>{turnover} 💰</code>\n"
        f"┣ 🎮 <b>Всего игр:</b> <code>{total_games}</code>\n"
        f"┣ 🏆 <b>Побед:</b> <code>{wins}</code> | ❌ <b>Поражений:</b> <code>{losses}</code> | 🤝 <b>Ничьих:</b> <code>{draws}</code>\n"
        f"┣ 📈 <b>Винрейт:</b> <code>{winrate}%</code>\n"
        f"┗ ⚠️ <b>Варны:</b> <code>{warns}/3</code>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("bonus"))
async def cmd_bonus(message: Message):
    await db.register_user(message.from_user.id, message.from_user.full_name)
    _, _, msg = await db.claim_daily_bonus(message.from_user.id)
    await message.answer(msg, parse_mode="HTML")


@dp.message(Command("pay"))
async def cmd_pay(message: Message, command: CommandObject):
    if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
        return await message.answer("❌ Используйте команду в ответ на сообщение игрока!")

    recipient = message.reply_to_message.from_user
    sender = message.from_user

    if recipient.id == sender.id:
        return await message.answer("❌ Нельзя переводить монеты самому себе!")

    if not command.args or not command.args.isdigit():
        return await message.answer("❌ Формат: <code>/pay 50</code>", parse_mode="HTML")

    amount = int(command.args)
    if amount <= 0:
        return await message.answer("❌ Сумма перевода должна быть больше 0!")

    await db.register_user(sender.id, sender.full_name)
    await db.register_user(recipient.id, recipient.full_name)

    sender_data = await db.get_user(sender.id)
    if sender_data[2] < amount:
        return await message.answer("❌ Недостаточно монет для перевода!")

    await db.change_balance(sender.id, -amount)
    await db.change_balance(recipient.id, amount)

    await message.answer(
        f"💸 {get_mention(sender.id, sender.full_name)} перевел <b>{amount} 💰</b> "
        f"игроку {get_mention(recipient.id, recipient.full_name)}!",
        parse_mode="HTML"
    )


# ================= КУБИК ПРОТИВ БОТА (/dice) =================
@dp.message(Command("dice"))
async def cmd_dice(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await db.register_user(user_id, message.from_user.full_name)

    bet = 15
    if command.args and command.args.isdigit():
        bet = int(command.args)

    if bet <= 0:
        return await message.answer("❌ Ставка должна быть больше 0!")

    user = await db.get_user(user_id)
    if user[2] < bet:
        return await message.answer(f"❌ Недостаточно средств! Твой баланс: <b>{user[2]} 💰</b>", parse_mode="HTML")

    await db.change_balance(user_id, -bet)
    await db.add_turnover(user_id, bet)

    await message.answer(f"🎲 Бросок {get_mention(user_id, message.from_user.full_name)}:", parse_mode="HTML")
    p_msg = await message.answer_dice(emoji="🎲")
    p_val = p_msg.dice.value
    await asyncio.sleep(4.0)

    await message.answer("🤖 Бросок Бота:", parse_mode="HTML")
    b_msg = await message.answer_dice(emoji="🎲")
    b_val = b_msg.dice.value
    await asyncio.sleep(4.0)

    if p_val > b_val:
        win = bet * 2
        await db.change_balance(user_id, win)
        await db.record_game(user_id, "win")
        text = f"🎉 <b>ПОБЕДА!</b> ({p_val} > {b_val})\nВыигрыш: <b>+{win} 💰</b>"
    elif p_val < b_val:
        await db.record_game(user_id, "loss")
        text = f"💀 <b>ПОРАЖЕНИЕ!</b> ({p_val} < {b_val})\nПотеряно: <b>-{bet} 💰</b>"
    else:
        await db.change_balance(user_id, bet)
        await db.record_game(user_id, "draw")
        text = f"🤝 <b>НИЧЬЯ!</b> ({p_val} = {b_val})\nСтавка возвращена: <b>{bet} 💰</b>"

    await message.answer(text, parse_mode="HTML")


# ================= КУБИК ДУЭЛИ МЕЖДУ ИГРОКАМИ (/duel) =================
@dp.message(Command("duel"))
async def cmd_duel(message: Message, command: CommandObject):
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

    await db.register_user(challenger.id, challenger.full_name)
    await db.register_user(opponent.id, opponent.full_name)

    c_data = await db.get_user(challenger.id)
    o_data = await db.get_user(opponent.id)

    if c_data[2] < bet:
        return await message.answer(f"❌ У вас недостаточно монет! Баланс: <b>{c_data[2]} 💰</b>", parse_mode="HTML")
    if o_data[2] < bet:
        return await message.answer(f"❌ У оппонента недостаточно монет! Баланс: <b>{o_data[2]} 💰</b>", parse_mode="HTML")

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
        f"💰 Ставка: <b>{bet} 💰</b>\n\n"
        f"<i>У оппонента есть 60 секунд на принятие.</i>"
    )

    duel_msg = await message.answer(text, reply_markup=duel_keyboard(duel_id), parse_mode="HTML")

    await asyncio.sleep(60)
    if duel_id in active_duels and active_duels[duel_id]["status"] == "pending":
        del active_duels[duel_id]
        try:
            await duel_msg.edit_text("⌛ <b>Время вызова истекло. Дуэль отменена.</b>", parse_mode="HTML")
        except:
            pass


@dp.callback_query(F.data.startswith("accept_"))
async def cb_accept_duel(call: CallbackQuery):
    duel_id = call.data.replace("accept_", "")
    if duel_id not in active_duels:
        return await call.answer("❌ Дуэль не найдена или уже завершилась!", show_alert=True)

    duel = active_duels[duel_id]
    if call.from_user.id != duel["opponent_id"]:
        return await call.answer("❌ Этот вызов брошен не вам!", show_alert=True)

    if duel["status"] != "pending":
        return await call.answer("Дуэль уже началась!", show_alert=True)

    duel["status"] = "in_progress"
    c_id, o_id, bet = duel["challenger_id"], duel["opponent_id"], duel["bet"]

    c_data = await db.get_user(c_id)
    o_data = await db.get_user(o_id)

    if c_data[2] < bet or o_data[2] < bet:
        del active_duels[duel_id]
        await call.message.edit_text("❌ Дуэль отменена: у одного из игроков недостаточно средств!")
        return

    await db.change_balance(c_id, -bet)
    await db.change_balance(o_id, -bet)
    await db.add_turnover(c_id, bet)
    await db.add_turnover(o_id, bet)

    await call.message.edit_text(f"⚔️ <b>Дуэль началась! Банк: {bet * 2} 💰</b>", parse_mode="HTML")

    await call.message.answer(f"🔴 Бросает {get_mention(c_id, duel['challenger_name'])}:", parse_mode="HTML")
    c_dice = await call.message.answer_dice(emoji="🎲")
    c_val = c_dice.dice.value
    await asyncio.sleep(4.0)

    await call.message.answer(f"🔵 Бросает {get_mention(o_id, duel['opponent_name'])}:", parse_mode="HTML")
    o_dice = await call.message.answer_dice(emoji="🎲")
    o_val = o_dice.dice.value
    await asyncio.sleep(4.0)

    if c_val > o_val:
        win_sum = bet * 2
        await db.change_balance(c_id, win_sum)
        await db.record_game(c_id, "win")
        await db.record_game(o_id, "loss")
        res = (
            f"🏆 <b>ПОБЕДИТЕЛЬ:</b> {get_mention(c_id, duel['challenger_name'])} ({c_val})\n"
            f"💀 <b>ПРОИГРАВШИЙ:</b> {get_mention(o_id, duel['opponent_name'])} ({o_val}) [ -{bet} 💰 ]\n\n"
            f"💸 Победитель забирает банк: <b>+{win_sum} 💰</b>"
        )
    elif o_val > c_val:
        win_sum = bet * 2
        await db.change_balance(o_id, win_sum)
        await db.record_game(o_id, "win")
        await db.record_game(c_id, "loss")
        res = (
            f"🏆 <b>ПОБЕДИТЕЛЬ:</b> {get_mention(o_id, duel['opponent_name'])} ({o_val})\n"
            f"💀 <b>ПРОИГРАВШИЙ:</b> {get_mention(c_id, duel['challenger_name'])} ({c_val}) [ -{bet} 💰 ]\n\n"
            f"💸 Победитель забирает банк: <b>+{win_sum} 💰</b>"
        )
    else:
        await db.change_balance(c_id, bet)
        await db.change_balance(o_id, bet)
        await db.record_game(c_id, "draw")
        await db.record_game(o_id, "draw")
        res = f"🤝 <b>Ничья ({c_val} = {o_val})!</b> Ставки возвращены."

    del active_duels[duel_id]
    await call.message.answer(res, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("decline_"))
async def cb_decline_duel(call: CallbackQuery):
    duel_id = call.data.replace("decline_", "")
    if duel_id not in active_duels:
        return await call.answer("❌ Дуэль уже неактивна!", show_alert=True)

    duel = active_duels[duel_id]
    if call.from_user.id != duel["opponent_id"] and call.from_user.id != duel["challenger_id"]:
        return await call.answer("❌ Вы не участвуете в этой дуэли!", show_alert=True)

    del active_duels[duel_id]
    await call.message.edit_text("❌ <b>Дуэль была отклонена.</b>", parse_mode="HTML")
    await call.answer()


# ================= ТОПЫ И ПРОМОКОДЫ =================
@dp.message(Command("top"))
async def cmd_top(message: Message):
    top = await db.get_top("balance", 10)
    if not top:
        return await message.answer("Таблица лидеров пуста.")

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = "🏆 <b>ТОП-10 БОГАЧЕЙ БОТА:</b>\n\n"
    for i, (name, val) in enumerate(top, 1):
        place = medals.get(i, f"<b>{i}.</b>")
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
        return await message.answer("Формат: <code>/promo [КОД]</code>", parse_mode="HTML")

    await db.register_user(message.from_user.id, message.from_user.full_name)
    _, msg = await db.activate_promo(message.from_user.id, command.args.strip())
    await message.answer(msg, parse_mode="HTML")


# ================= АДМИНКА И МОДЕРАЦИЯ =================
@dp.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return
    if not message.reply_to_message or message.reply_to_message.from_user.is_bot:
        return await message.answer("❌ Используйте команду ответом на сообщение пользователя!")
    if not command.args:
        return await message.answer("❌ Укажите сумму: <code>/give 1000</code>", parse_mode="HTML")

    try:
        amount = int(command.args)
    except ValueError:
        return await message.answer("❌ Сумма должна быть числом!")

    target = message.reply_to_message.from_user
    await db.register_user(target.id, target.full_name)
    await db.change_balance(target.id, amount)

    verb = "выдал" if amount >= 0 else "забрал"
    await message.answer(f"👑 Администратор {verb} <b>{abs(amount)} 💰</b> у {get_mention(target.id, target.full_name)}!", parse_mode="HTML")


@dp.message(Command("add_admin"))
async def cmd_add_admin(message: Message):
    if not await db.is_admin(message.from_user.id):
        return
    if not message.reply_to_message:
        return await message.answer("❌ Ответьте этой командой на сообщение пользователя.")

    target = message.reply_to_message.from_user
    await db.add_admin(target.id)
    await message.answer(f"✅ {get_mention(target.id, target.full_name)} назначен администратором бота!", parse_mode="HTML")


@dp.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id) or not message.reply_to_message:
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
        await message.answer(
            f"🔇 {get_mention(target.id, target.full_name)} отправлен в мут!\n"
            f"⏱ <b>Время:</b> {mins} мин.\n"
            f"📝 <b>Причина:</b> {reason}",
            parse_mode="HTML"
        )
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


@dp.message(Command("warn"))
async def cmd_warn(message: Message):
    if not await db.is_admin(message.from_user.id) or not message.reply_to_message:
        return

    target = message.reply_to_message.from_user
    if target.id == OWNER_ID or await db.is_admin(target.id):
        return await message.answer("❌ Нельзя выдать варн администратору!")

    await db.register_user(target.id, target.full_name)
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
@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if not await db.is_admin(message.from_user.id) or not message.reply_to_message:
        return
    target = message.reply_to_message.from_user
    try:
        await message.chat.ban(user_id=target.id)
        await message.answer(f"🛑 Пользователь забанен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id) or not command.args:
        return await message.answer("❌ Укажите ID: /unban ID")
    try:
        user_id = int(command.args)
        await message.chat.unban(user_id=user_id)
        await message.answer("✅ Пользователь разбанен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("unwarn"))
async def cmd_unwarn(message: Message):
    if not await db.is_admin(message.from_user.id) or not message.reply_to_message:
        return
    target = message.reply_to_message.from_user
    await db.reset_warns(target.id)
    await message.answer(f"✅ Предупреждения игрока {get_mention(target.id, target.full_name)} аннулированы.", parse_mode="HTML")


# ================= ЗАПУСК СЕРВЕРА =================
async def on_startup(bot: Bot):
    await db.init()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        logging.info(f"Установка Webhook: {webhook_url}")
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
    else:
        logging.info("RENDER_EXTERNAL_URL не задан, запуск в режиме веб-сервера без авто-вебхука.")


def main():
    if RENDER_EXTERNAL_URL:
        # Запуск Webhook для Render / Хостингов
        app = web.Application()
        webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_handler.register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        app.on_startup.append(lambda app: on_startup(bot))
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:
        # Автоматический fallback на локальный Polling
        async def run_polling():
            await db.init()
            await bot.delete_webhook(drop_pending_updates=True)
            logging.info("🚀 Запуск в режиме Polling...")
            await dp.start_polling(bot)

        asyncio.run(run_polling())


if __name__ == "__main__":
    main()