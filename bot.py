import os
import random
import string
import logging
from contextlib import asynccontextmanager

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Render автоматически выдает RENDER_EXTERNAL_URL (например, https://my-bot.onrender.com)
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH = "/webhook"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# --- БАЗА ДАННЫХ ---
DB_NAME = "anon_chat.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица пользователей и их ссылок
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                link_hash TEXT UNIQUE
            )
        ''')
        # Таблица диалогов (закрепляет уникальный ник за связкой отправитель-получатель)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS dialogs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER,
                target_id INTEGER,
                nickname TEXT,
                UNIQUE(sender_id, target_id)
            )
        ''')
        await db.commit()

# --- ГЕНЕРАТОР НИКОВ ---
ADJECTIVES = ["Тайный", "Скрытный", "Одинокий", "Дерзкий", "Веселый", "Мрачный", "Хитрый", "Милый"]
NOUNS = ["Енот", "Волк", "Кот", "Ворон", "Лис", "Призрак", "Барсук", "Сокол"]

def generate_nickname():
    return f"{random.choice(ADJECTIVES)} {random.choice(NOUNS)} {random.randint(10, 99)}"

# --- СОСТОЯНИЯ (FSM) ---
class ChatStates(StatesGroup):
    writing_message = State()

# --- ХЭНДЛЕРЫ ---

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandStart, state: FSMContext):
    await state.clear()
    args = command.args

    async with aiosqlite.connect(DB_NAME) as db:
        if not args:
            # Создаем или получаем ссылку пользователя
            async with db.execute("SELECT link_hash FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
                row = await cursor.fetchone()
            
            if row:
                link_hash = row[0]
            else:
                link_hash = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                await db.execute("INSERT INTO users (user_id, link_hash) VALUES (?, ?)", (message.from_user.id, link_hash))
                await db.commit()
            
            bot_info = await bot.get_me()
            link = f"https://t.me/{bot_info.username}?start={link_hash}"
            await message.answer(
                f"🎭 <b>Твоя анонимная ссылка:</b>\n\n<code>{link}</code>\n\n"
                f"Размести её в профиле или сторис. Все сообщения придут сюда!",
                parse_mode="HTML"
            )
            return

        # Если перешли по чужой ссылке
        async with db.execute("SELECT user_id FROM users WHERE link_hash = ?", (args,)) as cursor:
            row = await cursor.fetchone()
        
        if not row:
            await message.answer("❌ Ссылка недействительна.")
            return
        
        target_id = row[0]
        if target_id == message.from_user.id:
            await message.answer("😅 Нельзя писать анонимку самому себе!")
            return

        # Находим или создаем диалог (чтобы выдать постоянный ник)
        async with db.execute("SELECT id, nickname FROM dialogs WHERE sender_id = ? AND target_id = ?", 
                              (message.from_user.id, target_id)) as cursor:
            dialog = await cursor.fetchone()
        
        if not dialog:
            nickname = generate_nickname()
            await db.execute("INSERT INTO dialogs (sender_id, target_id, nickname) VALUES (?, ?, ?)", 
                             (message.from_user.id, target_id, nickname))
            await db.commit()
            async with db.execute("SELECT id FROM dialogs WHERE sender_id = ? AND target_id = ?", 
                                  (message.from_user.id, target_id)) as cursor:
                dialog_id = (await cursor.fetchone())[0]
        else:
            dialog_id = dialog[0]

        # Сохраняем id диалога в стейт
        await state.update_data(dialog_id=dialog_id)
        await state.set_state(ChatStates.writing_message)
        await message.answer("✍️ <b>Режим диалога включен.</b>\nНапиши свое сообщение (текст, фото или кружок), и я доставлю его анонимно!", parse_mode="HTML")

@router.callback_query(F.data.startswith("reply_"))
async def callback_reply(callback: CallbackQuery, state: FSMContext):
    dialog_id = int(callback.data.split("_")[1])
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT sender_id, target_id, nickname FROM dialogs WHERE id = ?", (dialog_id,)) as cursor:
            dialog = await cursor.fetchone()
            
    if not dialog:
        await callback.answer("Диалог не найден.", show_alert=True)
        return
        
    sender_id, target_id, nickname = dialog
    
    # Определяем, кто нажал на кнопку (владелец ссылки или аноним)
    is_owner = (callback.from_user.id == target_id)
    recipient_name = f"анониму <b>{nickname}</b>" if is_owner else "владельцу ссылки"

    await state.update_data(dialog_id=dialog_id)
    await state.set_state(ChatStates.writing_message)
    await callback.message.answer(f"✍️ Напиши ответ {recipient_name}:", parse_mode="HTML")
    await callback.answer()

@router.message(StateFilter(ChatStates.writing_message))
async def process_message(message: Message, state: FSMContext):
    data = await state.get_data()
    dialog_id = data.get("dialog_id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT sender_id, target_id, nickname FROM dialogs WHERE id = ?", (dialog_id,)) as cursor:
            dialog = await cursor.fetchone()
            
    if not dialog:
        await message.answer("Ошибка: диалог закрыт.")
        await state.clear()
        return
        
    sender_id, target_id, nickname = dialog
    is_owner = (message.from_user.id == target_id)
    recipient_id = sender_id if is_owner else target_id
    
    # Формируем кнопку для ответа
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Ответить", callback_data=f"reply_{dialog_id}")
    ]])

    header_text = f"📩 Ответик от владельца:" if is_owner else f"🎭 Новое сообщение от <b>{nickname}</b>:"

    try:
        # Отправляем шапку, затем копируем само сообщение пользователя (сохраняет медиа, стикеры, форматирование)
        await bot.send_message(chat_id=recipient_id, text=header_text, parse_mode="HTML")
        await message.send_copy(chat_id=recipient_id, reply_markup=markup)
        
        await message.react([{"type": "emoji", "emoji": "👍"}]) # Ставим реакцию об успехе
    except Exception as e:
        logging.error(f"Failed to send message: {e}")
        await message.answer("❌ Не удалось доставить сообщение. Возможно, пользователь заблокировал бота.")

# --- ЗАПУСК WEBHOOK (ДЛЯ RENDER) ---
dp.include_router(router)

async def on_startup(bot: Bot):
    await init_db()
    if WEBHOOK_URL:
        # Ставим вебхук только если есть URL (на Render)
        await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}")

def main():
    dp.startup.register(on_startup)
    app = web.Application()
    
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)

if __name__ == '__main__':
    main()
