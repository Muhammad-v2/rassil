import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import db
import userbot

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

_bot = None  # устанавливается в start_scheduler


async def send_scheduled_messages():
    """Проверяет и отправляет все сообщения, время которых наступило."""
    pending = await db.get_pending_messages()

    for msg_row in pending:
        try:
            account = await db.get_account(msg_row["account_id"])
            if not account:
                logger.warning(f"Account {msg_row['account_id']} not found for scheduled message {msg_row['id']}")
                continue

            async with userbot.client_from_blob(account["session_blob"], label=f"sched_{msg_row['account_id']}") as client:
                for chat_id in msg_row["chat_ids"]:
                    try:
                        if msg_row["message_photo"]:
                            await userbot.send_message_as_account(
                                client,
                                str(chat_id),
                                msg_row["message_text"],
                                photo_bytes=msg_row["message_photo"],
                            )
                        else:
                            await userbot.send_message_as_account(
                                client,
                                str(chat_id),
                                msg_row["message_text"],
                            )
                        logger.info(f"Message {msg_row['id']} sent to chat {chat_id}")
                    except Exception as e:
                        logger.error(f"Failed to send message {msg_row['id']} to chat {chat_id}: {e}")

            await db.update_message_sent(msg_row["id"], msg_row["repeat_interval_minutes"])
            logger.info(f"Scheduled message {msg_row['id']} processed")

        except Exception as e:
            logger.error(f"Error processing scheduled message {msg_row['id']}: {e}")


async def check_new_sessions_for_account(account_id: int, owner_id: int):
    """Проверяет один аккаунт на новые сессии и шлёт уведомление владельцу при необходимости."""
    account = await db.get_account(account_id)
    if not account:
        return

    try:
        async with userbot.client_from_blob(account["session_blob"], label=f"sess_check_{account_id}") as client:
            sessions = await userbot.list_sessions(client)

            for session in sessions:
                await db.log_session(account_id, session["hash"], session)

            new_sessions = await db.get_new_sessions(account_id)

            for ns in new_sessions:
                await db.mark_alert_sent(account_id, owner_id, ns["auth_hash"])

                if _bot:
                    text = (
                        f"🔔 <b>Новая сессия на аккаунте «{account['label']}»</b>\n\n"
                        f"🖥️ {ns['device_model'] or '—'}\n"
                        f"🌍 {ns['country'] or '—'}{', ' + ns['region'] if ns['region'] else ''}\n"
                        f"📡 IP: {ns['ip'] or '—'}\n\n"
                        f"Если это не вы — зайдите в панель и завершите эту сессию."
                    )
                    try:
                        await _bot.send_message(owner_id, text, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Failed to send session alert to {owner_id}: {e}")

    except Exception as e:
        logger.error(f"Error checking sessions for account {account_id}: {e}")


async def check_new_sessions():
    """Проверяет новые сессии по всем аккаунтам всех владельцев."""
    accounts = await db.pool().fetch("SELECT id, owner_id FROM accounts")
    for row in accounts:
        await check_new_sessions_for_account(row["id"], row["owner_id"])


async def prime_session_baseline(account_id: int):
    """
    Вызывается сразу после добавления нового аккаунта: логирует текущие сессии
    как baseline, чтобы они не считались 'новыми' и не создавали ложных уведомлений.
    """
    account = await db.get_account(account_id)
    if not account:
        return
    try:
        async with userbot.client_from_blob(account["session_blob"], label=f"baseline_{account_id}") as client:
            sessions = await userbot.list_sessions(client)
            for session in sessions:
                await db.log_session(account_id, session["hash"], session)
                await db.mark_alert_sent(account_id, account["owner_id"], session["hash"])
    except Exception as e:
        logger.error(f"Error priming baseline for account {account_id}: {e}")


def start_scheduler(bot=None):
    """Запускает планировщик. bot нужен для отправки уведомлений о новых сессиях."""
    global _bot
    _bot = bot

    scheduler.add_job(send_scheduled_messages, trigger=IntervalTrigger(seconds=30), id="send_scheduled", replace_existing=True)
    scheduler.add_job(check_new_sessions, trigger=IntervalTrigger(minutes=5), id="check_sessions", replace_existing=True)

    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
