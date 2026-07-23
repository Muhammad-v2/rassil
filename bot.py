import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait
from database import init_db

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")

ADMIN_IDS = [
    int(x.strip()) 
    for x in os.getenv("ADMIN_IDS", "").split(",") 
    if x.strip().isdigit()
]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

SESSIONS_DIR = "./sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Состояния FSM для ввода текста объявления
class BroadcastStates(StatesGroup):
    waiting_for_message = State()

# Middleware для проверки доступа по Telegram ID
@dp.update.outer_middleware()
async def check_access_middleware(handler, event, data):
    user = data.get("event_from_user")
    if not user or user.id not in ADMIN_IDS:
        if isinstance(event, types.Message):
            await event.answer("🚫 **Вход запрещен.** У вас нет прав для управления этим ботом.")
        elif isinstance(event, types.CallbackQuery):
            await event.answer("🚫 Вход запрещен.", show_alert=True)
        return
    return await handler(event, data)

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Активные сессии аккаунта", callback_data="list_tg_sessions")],
        [InlineKeyboardButton(text="📢 Отправить сообщение в группы", callback_data="start_group_broadcast")],
        [InlineKeyboardButton(text="🚪 Выйти из сессии и удалить файл", callback_data="terminate_self")]
    ])

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 **Панель управления сессиями Telegram**\n\n"
        "Отправьте мне `.session` файл (Pyrogram/Telethon), чтобы начать работу.",
        reply_markup=get_main_keyboard()
    )

# Прием файла .session
@dp.message(F.document & F.document.file_name.endswith('.session'))
async def handle_session_upload(message: types.Message):
    file_id = message.document.file_id
    file_name = message.document.file_name
    file_path = os.path.join(SESSIONS_DIR, file_name)

    await bot.download(file_id, destination=file_path)
    session_name = file_name.replace('.session', '')
    
    try:
        user_client = Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR)
        await user_client.start()
        me = await user_client.get_me()
        await user_client.stop()

        await message.reply(
            f"✅ Сессия успешно загружена!\n"
            f"**Имя:** {me.first_name} (@{me.username or 'N/A'})\n"
            f"**ID:** `{me.id}`",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        await message.reply(f"❌ Ошибка подключения сессии: {str(e)}")

# Просмотр сессий
@dp.callback_query(F.data == "list_tg_sessions")
async def list_active_sessions(callback: types.CallbackQuery):
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    if not files:
        await callback.message.edit_text("❌ Загруженные сессии отсутствуют.", reply_markup=get_main_keyboard())
        return

    session_name = files[0].replace('.session', '')
    
    try:
        user_client = Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR)
        await user_client.start()
        
        authorizations = await user_client.get_authorizations()
        text = "📱 **Список активных сессий аккаунта:**\n\n"
        buttons = []

        for auth in authorizations:
            status = "🟢 (Текущая)" if auth.is_current else "⚪"
            text += (
                f"{status} **Устройство:** {auth.device_model} ({auth.platform})\n"
                f"└ **IP:** `{auth.ip}` | **Дата:** {auth.date_created}\n\n"
            )
            if not auth.is_current:
                buttons.append([InlineKeyboardButton(
                    text=f"❌ Удалить: {auth.device_model}", 
                    callback_data=f"kill_session_{auth.hash}"
                )])

        await user_client.stop()
        buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="list_tg_sessions")])
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {str(e)}", reply_markup=get_main_keyboard())

# Запрос текста для отправки в группы
@dp.callback_query(F.data == "start_group_broadcast")
async def start_broadcast_prompt(callback: types.CallbackQuery, state: FSMContext):
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    if not files:
        await callback.answer("❌ Сначала загрузите .session файл!", show_alert=True)
        return

    await state.set_state(BroadcastStates.waiting_for_message)
    await callback.message.answer("✏️ **Введите текст сообщения**, которое нужно отправить во все группы аккаунта:")
    await callback.answer()

# Обработка введенного текста и отправка по группам
@dp.message(BroadcastStates.waiting_for_message)
async def process_group_broadcast(message: types.Message, state: FSMContext):
    broadcast_text = message.text
    await state.clear()

    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    if not files:
        await message.answer("❌ Сессия не найдена.", reply_markup=get_main_keyboard())
        return

    session_name = files[0].replace('.session', '')
    status_msg = await message.answer("🔄 Подключение к аккаунту и поиск групп...")

    try:
        user_client = Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR)
        await user_client.start()

        group_ids = []
        async for dialog in user_client.get_dialogs():
            # Фильтруем: только группы и супергруппы
            if dialog.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                group_ids.append((dialog.chat.id, dialog.chat.title))

        if not group_ids:
            await user_client.stop()
            await status_msg.edit_text("⚠️ Группы на этом аккаунте не найдены.", reply_markup=get_main_keyboard())
            return

        await status_msg.edit_text(f"🚀 Найдено групп: {len(group_ids)}. Начинаю отправку...")

        success_count = 0
        fail_count = 0

        for chat_id, title in group_ids:
            try:
                await user_client.send_message(chat_id, broadcast_text)
                success_count += 1
                await asyncio.sleep(2)  # Пауза между отправками во избежание блокировок
            except FloodWait as e:
                await asyncio.sleep(e.value)
                await user_client.send_message(chat_id, broadcast_text)
                success_count += 1
            except Exception as err:
                logging.error(f"Ошибка отправки в {title} ({chat_id}): {err}")
                fail_count += 1

        await user_client.stop()

        await status_msg.edit_text(
            f"✅ **Рассылка завершена!**\n\n"
            f"🟢 Успешно: `{success_count}`\n"
            f"🔴 Ошибок: `{fail_count}`",
            reply_markup=get_main_keyboard()
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при выполнении: {str(e)}", reply_markup=get_main_keyboard())

# Удаление отдельной сессии по хешу
@dp.callback_query(F.data.startswith("kill_session_"))
async def kill_session(callback: types.CallbackQuery):
    session_hash = int(callback.data.split("_")[2])
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    if not files:
        await callback.answer("Файл сессии не найден.", show_alert=True)
        return

    session_name = files[0].replace('.session', '')

    try:
        user_client = Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR)
        await user_client.start()
        await user_client.reset_authorization(session_hash)
        await user_client.stop()
        
        await callback.answer("✅ Сессия завершена!", show_alert=True)
        await list_active_sessions(callback)
    except Exception as e:
        await callback.answer(f"⚠️ Ошибка сброса (Telegram требует 24ч жизни сессии): {e}", show_alert=True)

# Завершение текущей сессии и удаление .session файла
@dp.callback_query(F.data == "terminate_self")
async def terminate_self_session(callback: types.CallbackQuery):
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    if not files:
        await callback.answer("Файлы .session отсутствуют.", show_alert=True)
        return

    file_name = files[0]
    session_name = file_name.replace('.session', '')
    file_path = os.path.join(SESSIONS_DIR, file_name)

    try:
        user_client = Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR)
        await user_client.start()
        await user_client.log_out()
    except Exception:
        pass
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    await callback.message.edit_text(
        "🚪 Сессия завершена в Telegram, а файл `.session` удален с сервера.",
        reply_markup=get_main_keyboard()
    )

async def main():
    await init_db()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
