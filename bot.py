import asyncio
import logging
import os
import random
from urllib.parse import quote
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

# Токен бота из переменной окружения Render или вставить вручную
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB_NAME = "anon_bot.db"

# --- Рандомайзер анонимных имен ---
ADJECTIVES = ["Везучий", "Солнечный", "Мудрый", "Быстрый", "Загадочный", "Хитрый", "Ночной", "Яркий", "Смелый", "Лесной", "Огненный"]
NOUNS = ["Волк", "Луч", "Кот", "Феникс", "Странник", "Лев", "Орел", "Дракон", "Енот", "Лис", "Тигр", "Сокол"]

def generate_random_nickname() -> str:
    return f"{random.choice(ADJECTIVES)} {random.choice(NOUNS)}"

# --- FSM Состояния ---
class AnonState(StatesGroup):
    waiting_for_message = State()

# --- Работа с Базой Данных ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nicknames (
                owner_id INTEGER,
                partner_id INTEGER,
                nickname TEXT,
                PRIMARY KEY (owner_id, partner_id)
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

async def get_or_create_nickname(owner_id: int, partner_id: int) -> str:
    """Получает или создает имя для partner_id в глазах owner_id"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT nickname FROM nicknames WHERE owner_id = ? AND partner_id = ?",
            (owner_id, partner_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]

        new_nick = generate_random_nickname()
        await db.execute(
            "INSERT INTO nicknames (owner_id, partner_id, nickname) VALUES (?, ?, ?)",
            (owner_id, partner_id, new_nick)
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

async def get_interlocutors(user_id: int):
    """Список всех ID пользователей, с кем есть переписка"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT DISTINCT 
                CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END as partner_id
            FROM messages
            WHERE sender_id = ? OR recipient_id = ?
        """, (user_id, user_id, user_id)) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def get_chat_history(user_id: int, partner_id: int, limit: int = 5):
    """Последние 5 сообщений диалога между двумя юзерами"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT sender_id, text, created_at
            FROM messages
            WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, partner_id, partner_id, user_id, limit)) as cursor:
            rows = await cursor.fetchall()
            return list(reversed(rows))

# --- Главная функция отправки "Чистого" сообщения (Delete + Send) ---
async def send_fresh(chat_id: int, text: str, reply_markup=None, old_message: Message = None):
    if old_message:
        try:
            await old_message.delete()
        except Exception:
            pass
    return await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")

# --- Главное меню ---
async def show_main_menu(user_id: int, old_message: Message = None):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    # Исправленный формат ссылки для sharing
    share_text = quote("Отправь мне анонимное сообщение!")
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={share_text}"

    text = (
        f"Вот твоя личная ссылка:\n\n"
        f"👉 <code>{ref_link}</code>\n\n"
        f"Опубликуй её и получай анонимные сообщения!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Поделиться ссылкой", url=share_url)],
        [InlineKeyboardButton(text="👥 Список собеседников", callback_data="list_chats")]
    ])

    await send_fresh(chat_id=user_id, text=text, reply_markup=kb, old_message=old_message)

# --- Хэндлеры ---

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    # Удаляем входящую команду /start
    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    args = command.args

    # Если перешли по реферальной ссылке (/start TARGET_ID)
    if args and args.isdigit():
        target_id = int(args)
        if target_id == message.from_user.id:
            await show_main_menu(message.from_user.id)
            return

        await state.update_data(target_id=target_id)
        await state.set_state(AnonState.waiting_for_message)

        target_nick = await get_or_create_nickname(message.from_user.id, target_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
        ])
        await send_fresh(
            chat_id=message.from_user.id,
            text=f"🔒 Напиши анонимное сообщение для <b>{target_nick}</b>:",
            reply_markup=kb
        )
        return

    # Обычный старт
    await show_main_menu(message.from_user.id)

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_menu(callback.from_user.id, old_message=callback.message)
    await callback.answer()

@dp.callback_query(F.data == "list_chats")
async def cb_list_chats(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    partners = await get_interlocutors(callback.from_user.id)

    if not partners:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
        ])
        await send_fresh(
            chat_id=callback.from_user.id,
            text="📭 <b>Список собеседников пуст.</b>\nПока никто не написал вам сообщения.",
            reply_markup=kb,
            old_message=callback.message
        )
    else:
        buttons = []
        for p_id in partners:
            nick = await get_or_create_nickname(callback.from_user.id, p_id)
            buttons.append([InlineKeyboardButton(text=f"👤 {nick}", callback_data=f"chat_{p_id}")])
        
        buttons.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await send_fresh(
            chat_id=callback.from_user.id,
            text="👥 <b>Ваши собеседники:</b>\nВыберите пользователя для просмотра переписки или ответа:",
            reply_markup=kb,
            old_message=callback.message
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("chat_"))
async def cb_open_chat(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    partner_id = int(callback.data.split("_")[1])
    partner_nick = await get_or_create_nickname(callback.from_user.id, partner_id)
    history = await get_chat_history(callback.from_user.id, partner_id, limit=5)

    text = f"💬 <b>Диалог с: {partner_nick}</b>\n\n"
    if not history:
        text += "<i>Сообщений пока нет. Напишите первым!</i>\n"
    else:
        text += "<b>Последние 5 сообщений:</b>\n"
        for sender_id, msg_text, _ in history:
            if sender_id == callback.from_user.id:
                text += f"🟢 <b>Вы:</b> {msg_text}\n"
            else:
                text += f"⚪️ <b>{partner_nick}:</b> {msg_text}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать сообщение", callback_data=f"write_{partner_id}")],
        [InlineKeyboardButton(text="🔄 Обновить диалог", callback_data=f"chat_{partner_id}")],
        [InlineKeyboardButton(text="🔙 К списку собеседников", callback_data="list_chats")]
    ])

    await send_fresh(
        chat_id=callback.from_user.id,
        text=text,
        reply_markup=kb,
        old_message=callback.message
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("write_"))
async def cb_write_start(callback: CallbackQuery, state: FSMContext):
    partner_id = int(callback.data.split("_")[1])
    partner_nick = await get_or_create_nickname(callback.from_user.id, partner_id)

    await state.update_data(target_id=partner_id)
    await state.set_state(AnonState.waiting_for_message)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"chat_{partner_id}")]
    ])

    await send_fresh(
        chat_id=callback.from_user.id,
        text=f"✍️ Напишите анонимное сообщение для <b>{partner_nick}</b>:",
        reply_markup=kb,
        old_message=callback.message
    )
    await callback.answer()

@dp.message(AnonState.waiting_for_message)
async def process_send_message(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("target_id")

    # Удаляем написанный пользователем текст для сохранения чистоты и анонимности
    try:
        await message.delete()
    except Exception:
        pass

    if not target_id:
        await show_main_menu(message.from_user.id)
        await state.clear()
        return

    # Сохраняем сообщение в БД
    await save_message(sender_id=message.from_user.id, recipient_id=target_id, text=message.text)

    # Имена для обеих сторон
    sender_nick_for_target = await get_or_create_nickname(target_id, message.from_user.id)
    target_nick_for_sender = await get_or_create_nickname(message.from_user.id, target_id)

    # Уведомляем получателя
    try:
        kb_target = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Открыть диалог", callback_data=f"chat_{message.from_user.id}")]
        ])
        await bot.send_message(
            chat_id=target_id,
            text=f"📩 <b>Новое анонимное сообщение от {sender_nick_for_target}!</b>\n\n«{message.text}»",
            reply_markup=kb_target,
            parse_mode="HTML"
        )
    except Exception:
        pass # Пользователь заблокировал бота

    await state.clear()

    # Показываем обновленный диалог отправителю
    history = await get_chat_history(message.from_user.id, target_id, limit=5)
    text = f"✅ <b>Сообщение отправлено!</b>\n\n💬 <b>Диалог с: {target_nick_for_sender}</b>\n\n"
    text += "<b>Последние 5 сообщений:</b>\n"
    for sender_id, msg_text, _ in history:
        if sender_id == message.from_user.id:
            text += f"🟢 <b>Вы:</b> {msg_text}\n"
        else:
            text += f"⚪️ <b>{target_nick_for_sender}:</b> {msg_text}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать еще", callback_data=f"write_{target_id}")],
        [InlineKeyboardButton(text="🔙 К списку собеседников", callback_data="list_chats")]
    ])

    await send_fresh(chat_id=message.from_user.id, text=text, reply_markup=kb)

# --- Запуск ---
async def main():
    await init_db()
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
