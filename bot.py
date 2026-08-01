import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

import db
import scheduler as sched
import web_app

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ENV_ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
WEBAPP_URL = os.environ["WEBAPP_URL"]  # публичный HTTPS-адрес (Railway public domain)

router = Router()


async def check_admin(user_id: int) -> bool:
    return await db.is_admin(user_id, ENV_ADMIN_IDS)


@router.message(Command("start"))
async def cmd_start(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🚫 Вы не администратор этого бота. Доступ запрещён.")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎛 Открыть панель управления", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )
    await message.answer(
        "🎛 <b>Session Manager</b>\n\n"
        "Вся работа с аккаунтами теперь через панель — сессии, отправка сообщений, "
        "профиль, расписание рассылок и уведомления о новых входах.\n\n"
        "Нажмите кнопку ниже, чтобы открыть панель.",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(Command("admins"))
async def cmd_list_admins(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🚫 Вы не администратор этого бота.")

    db_admins = await db.list_admins()
    lines = ["👥 <b>Администраторы</b>\n"]
    for aid in ENV_ADMIN_IDS:
        lines.append(f"🔒 <code>{aid}</code> (супер-админ)")
    for row in db_admins:
        lines.append(f"👤 <code>{row['id']}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🚫 Вы не администратор этого бота.")
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Использование: /addadmin 123456789")
    new_id = int(parts[1])
    await db.add_admin(new_id, added_by=message.from_user.id)
    await message.answer(f"✅ Пользователь <code>{new_id}</code> добавлен в админы.", parse_mode="HTML")


@router.message(Command("deladmin"))
async def cmd_del_admin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🚫 Вы не администратор этого бота.")
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Использование: /deladmin 123456789")
    target_id = int(parts[1])
    if target_id in ENV_ADMIN_IDS:
        return await message.answer("⚠️ Нельзя удалить супер-админа (задан в переменных окружения).")
    await db.remove_admin(target_id)
    await message.answer(f"✅ Пользователь <code>{target_id}</code> удалён из админов.", parse_mode="HTML")


async def main():
    await db.init_db()

    bot = Bot(token=os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Планировщик отложенных сообщений и мониторинга сессий
    sched.start_scheduler(bot=bot)

    # Веб-сервер для Mini App (порт из окружения Railway, по умолчанию 8080)
    port = int(os.environ.get("PORT", 8080))
    await web_app.run_web_app(port=port, bot=bot)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started, polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
