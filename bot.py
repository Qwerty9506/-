import asyncio
import logging
import random
import os
import aiosqlite

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)

# Токен бота из переменной окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB_NAME = "anon_bot.db"

# --- Списки для рандомайзера имен ---
ADJECTIVES = ["Везучий", "Солнечный", "Мудрый", "Быстрый", "Загадочный", "Хитрый", "Ночной", "Одинокий", "Яркий", "Смелый"]
NOUNS = ["Волк", "Луч", "Кот", "Феникс", "Странник", "Лев", "Орел", "Дракон", "Енот", "Лис"]

def generate_random_nickname():
    return f"{random.choice(ADJECTIVES)} {random.choice(NOUNS)}"

# --- Состояния FSM ---
class AnonForm(StatesGroup):
    waiting_for_message = State()

# --- Работа с БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nicknames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER,
                recipient_id INTEGER,
                nickname TEXT,
                UNIQUE(sender_id, recipient_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER,
                recipient_id INTEGER,
                text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def get_or_create_nickname(sender_id: int, recipient_id: int) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT nickname FROM nicknames WHERE sender_id = ? AND recipient_id = ?",
            (sender_id, recipient_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]

        # Если имени еще нет — генерируем новое
        new_nick = generate_random_nickname()
        await db.execute(
            "INSERT INTO nicknames (sender_id, recipient_id, nickname) VALUES (?, ?, ?)",
            (sender_id, recipient_id, new_nick)
        )
        await db.commit()
        return new_nick

async def save_message(sender_id: int, recipient_id: int, text: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO messages (sender_id, recipient_id, text) VALUES (?, ?, ?)",
            (sender_id, recipient_id, text)
        )
        await db.commit()

async def get_last_messages(recipient_id: int, limit: int = 5):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT m.text, n.nickname, m.created_at 
            FROM messages m
            JOIN nicknames n ON m.sender_id = n.sender_id AND m.recipient_id = n.recipient_id
            WHERE m.recipient_id = ?
            ORDER BY m.id DESC
            LIMIT ?
        """, (recipient_id, limit)) as cursor:
            return await cursor.fetchall()

# --- Вспомогательная функция отправки "чистого" нового сообщения ---
async def send_fresh(chat_id: int, text: str, reply_markup=None, old_message: Message = None):
    """Удаляет старое сообщение (если есть) и отправляет абсолютно новое."""
    if old_message:
        try:
            await old_message.delete()
        except Exception:
            pass  # Пропускаем, если сообщение уже удалено или устарело
    return await bot.send_message(chat_id, text, reply_markup=reply_markup)

# --- Главные Клавиатуры ---
def main_menu_keyboard(bot_username: str, user_id: int):
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_username}?start={user_id}&text=Отправь%20мне%20анонимный%20вопрос!"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Поделиться ссылкой", url=share_url)],
            [InlineKeyboardButton(text="📥 Последние 5 сообщений", callback_data="view_inbox")],
            [InlineKeyboardButton(text="🔄 Обновить меню", callback_data="refresh_menu")]
        ]
    )

# --- Хэндлеры ---

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    # Обязательное удаление входящего /start сообщения от юзера
    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    args = command.args

    # Если перешли по реферальной ссылке (например, /start 123456789)
    if args and args.isdigit():
        target_id = int(args)
        if target_id == message.from_user.id:
            await send_fresh(
                chat_id=message.from_user.id,
                text="❌ Это твоя собственная ссылка! Напиши её друзьям, чтобы получать вопросы."
            )
            return

        await state.update_data(target_id=target_id)
        await state.set_state(AnonForm.waiting_for_message)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_send")]
        ])
        await send_fresh(
            chat_id=message.from_user.id,
            text="🔒 Напиши сюда анонимное сообщение для этого пользователя.\nОно отправится сразу после ввода!",
            reply_markup=kb
        )
        return

    # Обычный /start без аргументов (главный экран пользователя)
    bot_info = await bot.get_me()
    user_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    text = (
        f"Вот твоя личная ссылка:\n\n"
        f"👉 `{user_link}`\n\n"
        f"Опубликуй её и получай анонимные сообщения!"
    )
    await send_fresh(
        chat_id=message.from_user.id,
        text=text,
        reply_markup=main_menu_keyboard(bot_info.username, message.from_user.id)
    )

@dp.callback_query(F.data == "refresh_menu")
async def cb_refresh_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    bot_info = await bot.get_me()
    user_link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    text = (
        f"Вот твоя личная ссылка:\n\n"
        f"👉 `{user_link}`\n\n"
        f"Опубликуй её и получай анонимные сообщения!"
    )
    # Удаляем старое сообщение и создаем новое
    await send_fresh(
        chat_id=callback.from_user.id,
        text=text,
        reply_markup=main_menu_keyboard(bot_info.username, callback.from_user.id),
        old_message=callback.message
    )
    await callback.answer()

@dp.callback_query(F.data == "view_inbox")
async def cb_view_inbox(callback: CallbackQuery):
    messages = await get_last_messages(recipient_id=callback.from_user.id, limit=5)
    
    if not messages:
        text = "📭 У тебя пока нет анонимных сообщений."
    else:
        text = "📥 **Последние 5 анонимных сообщений:**\n\n"
        for msg_text, sender_nick, created_at in messages:
            text += f"👤 **{sender_nick}**:\n«{msg_text}»\n───\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="refresh_menu")]
    ])

    await send_fresh(
        chat_id=callback.from_user.id,
        text=text,
        reply_markup=kb,
        old_message=callback.message
    )
    await callback.answer()

@dp.callback_query(F.data == "cancel_send")
async def cb_cancel_send(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    bot_info = await bot.get_me()
    
    await send_fresh(
        chat_id=callback.from_user.id,
        text="❌ Отправка отменена.",
        reply_markup=main_menu_keyboard(bot_info.username, callback.from_user.id),
        old_message=callback.message
    )
    await callback.answer()

@dp.message(AnonForm.waiting_for_message)
async def process_anon_message(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("target_id")

    # Удаляем отправленное юзером сообщение с текстом для анонимности
    try:
        await message.delete()
    except Exception:
        pass

    if not target_id:
        await send_fresh(chat_id=message.from_user.id, text="Ошибка адресата. Попробуй снова по ссылке.")
        await state.clear()
        return

    # Получаем/создаем рандомный псевдоним
    nickname = await get_or_create_nickname(sender_id=message.from_user.id, recipient_id=target_id)
    
    # Сохраняем в БД
    await save_message(sender_id=message.from_user.id, recipient_id=target_id, text=message.text)
    
    # Отправляем сообщение получателю
    try:
        incoming_text = f"📩 **Новое анонимное сообщение!**\n\nОт: **{nickname}**\n\n«{message.text}»"
        await bot.send_message(chat_id=target_id, text=incoming_text)
    except Exception:
        pass  # Получатель заблокировал бота или удалил чат

    bot_info = await bot.get_me()
    await send_fresh(
        chat_id=message.from_user.id,
        text=f"✅ Сообщение успешно доставлено пользователю под именем **{nickname}**!",
        reply_markup=main_menu_keyboard(bot_info.username, message.from_user.id)
    )
    await state.clear()

# --- Запуск ---
async def main():
    await init_db()
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
