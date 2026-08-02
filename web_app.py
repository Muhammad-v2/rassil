import logging
import json
import io
import os
import time
import shutil
import tempfile
import uuid
import hmac
import hashlib
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta
from aiohttp import web
import aiofiles
import asyncio

from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, PasswordHashInvalid, FloodWait

import db
import userbot
import scheduler as sched

logger = logging.getLogger(__name__)

ENV_ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def validate_telegram_init_data(init_data: str, max_age_seconds: int = 86400) -> dict | None:
    """
    Проверяет подпись initData, которую Telegram Web App передаёт на фронтенде.
    Возвращает распарсенный объект user, если подпись верна и данные не устарели, иначе None.
    Алгоритм: https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(parsed.get("auth_date", 0))
    except ValueError:
        return None
    if time.time() - auth_date > max_age_seconds:
        return None

    user_json = parsed.get("user")
    if not user_json:
        return None
    try:
        return json.loads(user_json)
    except json.JSONDecodeError:
        return None

# Незавершённые входы по номеру телефона: login_id -> {client, phone, phone_code_hash, workdir, user_id, created_at}
_pending_logins: dict[str, dict] = {}
LOGIN_TIMEOUT_SECONDS = 10 * 60  # 10 минут на весь процесс входа


def _cleanup_expired_logins():
    now = time.time()
    expired = [lid for lid, d in _pending_logins.items() if now - d["created_at"] > LOGIN_TIMEOUT_SECONDS]
    for lid in expired:
        d = _pending_logins.pop(lid)
        try:
            shutil.rmtree(d["workdir"], ignore_errors=True)
        except Exception:
            pass


class WebAppHandler:
    """Хендлеры для Web Mini App."""
    
    def __init__(self, bot=None):
        self.app = web.Application()
        self.bot = bot  # используется для отправки уведомлений при необходимости
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        self.setup_routes()
    
    def setup_routes(self):
        self.app.router.add_static('/static', path=str(STATIC_DIR), name='static')
        self.app.router.add_get('/', self.index)
        self.app.router.add_post('/api/accounts', self.get_accounts)
        self.app.router.add_post('/api/account/{aid}', self.get_account_details)
        self.app.router.add_post('/api/sessions/{aid}', self.get_sessions)
        self.app.router.add_post('/api/terminate-session', self.terminate_session)
        self.app.router.add_post('/api/dialogs/{aid}', self.get_dialogs)
        self.app.router.add_post('/api/send-message', self.send_message)
        self.app.router.add_post('/api/profile/{aid}', self.get_profile)
        self.app.router.add_post('/api/update-profile', self.update_profile)
        self.app.router.add_post('/api/upload-photo', self.upload_photo)
        self.app.router.add_post('/api/scheduled/{aid}', self.get_scheduled_messages)
        self.app.router.add_post('/api/add-scheduled', self.add_scheduled_message)
        self.app.router.add_post('/api/cancel-scheduled', self.cancel_scheduled)
        self.app.router.add_post('/api/all-sessions', self.get_all_sessions)
        self.app.router.add_post('/api/add-account', self.add_account)
        self.app.router.add_post('/api/delete-account', self.delete_account)
        self.app.router.add_post('/api/admins', self.list_admins)
        self.app.router.add_post('/api/add-admin', self.add_admin)
        self.app.router.add_post('/api/remove-admin', self.remove_admin)
        self.app.router.add_post('/api/login/start', self.login_start)
        self.app.router.add_post('/api/login/code', self.login_code)
        self.app.router.add_post('/api/login/password', self.login_password)
    
    async def index(self, request):
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            return web.Response(
                text=(
                    "index.html не найден на сервере.\n"
                    "Проверьте, что папка static/ с index.html загружена в репозиторий "
                    "(рядом с bot.py), а не только сами .py файлы."
                ),
                status=500,
            )
        return web.FileResponse(str(index_path))
    
    async def get_user_id(self, request) -> int | None:
        """
        Извлечь user_id из initData Telegram Web App с проверкой подписи.
        Заголовок X-Telegram-Init-Data должен содержать сырую строку initData
        (window.Telegram.WebApp.initData), а не готовый user id — это защищает
        от подделки ID через devtools/прокси.
        """
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        user = validate_telegram_init_data(init_data)
        if not user:
            return None
        try:
            return int(user.get("id"))
        except (TypeError, ValueError):
            return None

    async def get_authed_user_id(self, request) -> int | None:
        """Извлекает user_id и проверяет, что это админ панели. None, если доступа нет."""
        user_id = await self.get_user_id(request)
        if not user_id:
            return None
        if not await db.is_admin(user_id, ENV_ADMIN_IDS):
            return None
        return user_id
    
    async def get_accounts(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        accounts = await db.list_accounts(user_id)
        return web.json_response({
            "accounts": [
                {
                    "id": acc["id"],
                    "label": acc["label"],
                    "phone": acc["phone"],
                    "username": acc["username"],
                }
                for acc in accounts
            ]
        })
    
    async def get_account_details(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        account_id = int(request.match_info["aid"])
        account = await db.get_account(account_id)
        
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"info_{account_id}") as client:
                info = await userbot.get_account_info(client)
                return web.json_response({"info": info})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def get_sessions(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        account_id = int(request.match_info["aid"])
        account = await db.get_account(account_id)
        
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"sess_{account_id}") as client:
                sessions = await userbot.list_sessions(client)
                
                # Логировать сессии
                for session in sessions:
                    await db.log_session(account_id, session["hash"], session)
                
                return web.json_response({
                    "sessions": [
                        {
                            "hash": s["hash"],
                            "current": s["current"],
                            "device_model": s["device_model"],
                            "platform": s["platform"],
                            "app_name": s["app_name"],
                            "app_version": s["app_version"],
                            "country": s["country"],
                            "region": s["region"],
                            "ip": s["ip"],
                            "date_active": s["date_active"],
                        }
                        for s in sessions
                    ]
                })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def terminate_session(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        data = await request.json()
        account_id = data.get("account_id")
        auth_hash = data.get("auth_hash")
        
        account = await db.get_account(account_id)
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"term_{account_id}") as client:
                await userbot.terminate_session(client, auth_hash)
                return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def get_dialogs(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        account_id = int(request.match_info["aid"])
        account = await db.get_account(account_id)
        
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        # Сначала попытаемся получить из кеша
        cached = await db.get_chats(account_id)
        if cached:
            private = [c for c in cached if not c["is_group"]]
            groups = [c for c in cached if c["is_group"]]
            return web.json_response({"private": private, "groups": groups})
        
        # Если кеша нет, загружаем свежие данные
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"dial_{account_id}") as client:
                dialogs = await userbot.get_all_dialogs(client)
                
                # Кешируем
                all_chats = dialogs["private"] + dialogs["groups"]
                await db.cache_chats(account_id, all_chats)
                
                return web.json_response({
                    "private": dialogs["private"],
                    "groups": dialogs["groups"],
                })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def send_message(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        data = await request.json()
        account_id = data.get("account_id")
        chat_id = data.get("chat_id")
        text = data.get("text")
        
        account = await db.get_account(account_id)
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"send_{account_id}") as client:
                await userbot.send_message_as_account(client, str(chat_id), text)
                return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def get_profile(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        account_id = int(request.match_info["aid"])
        account = await db.get_account(account_id)
        
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"prof_{account_id}") as client:
                info = await userbot.get_account_info(client)
                return web.json_response({"info": info})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def update_profile(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        data = await request.json()
        account_id = data.get("account_id")
        first_name = data.get("first_name")
        last_name = data.get("last_name")
        username = data.get("username")
        
        account = await db.get_account(account_id)
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"upd_{account_id}") as client:
                if first_name:
                    await userbot.update_first_name(client, first_name)
                if last_name:
                    await userbot.update_last_name(client, last_name)
                if username:
                    await userbot.update_username(client, username)
                
                if username:
                    await db.update_account_info(account_id, username=username)
                
                return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def upload_photo(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        account_id = int((await request.post()).get("account_id"))
        action = (await request.post()).get("action", "set")  # set или delete
        
        account = await db.get_account(account_id)
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            async with userbot.client_from_blob(account["session_blob"], label=f"photo_{account_id}") as client:
                if action == "delete":
                    await userbot.delete_profile_photo(client)
                else:
                    # Получить файл
                    post_data = await request.post()
                    photo_file = post_data.get("photo")
                    if not photo_file:
                        return web.json_response({"error": "No photo provided"}, status=400)
                    
                    photo_bytes = photo_file.file.read()
                    await userbot.set_profile_photo(client, photo_bytes)
                
                return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def get_scheduled_messages(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        account_id = int(request.match_info["aid"])
        account = await db.get_account(account_id)
        
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        messages = await db.get_scheduled_messages(account_id)
        return web.json_response({
            "messages": [
                {
                    "id": msg["id"],
                    "chat_ids": msg["chat_ids"],
                    "text": msg["message_text"][:50] + "..." if len(msg["message_text"]) > 50 else msg["message_text"],
                    "repeat_minutes": msg["repeat_interval_minutes"],
                    "next_send": msg["next_send_at"].isoformat() if msg["next_send_at"] else None,
                }
                for msg in messages
            ]
        })
    
    async def add_scheduled_message(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        data = await request.json()
        account_id = data.get("account_id")
        chat_ids = data.get("chat_ids", [])
        text = data.get("text")
        repeat_minutes = data.get("repeat_minutes")
        send_at = data.get("send_at")  # ISO datetime
        
        account = await db.get_account(account_id)
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)
        
        try:
            next_send = datetime.fromisoformat(send_at) if send_at else datetime.now()
            msg_id = await db.add_scheduled_message(
                account_id, user_id, chat_ids, text,
                repeat_interval_minutes=repeat_minutes,
                next_send_at=next_send,
            )
            return web.json_response({"id": msg_id, "success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def cancel_scheduled(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        data = await request.json()
        message_id = data.get("message_id")
        
        try:
            await db.cancel_scheduled_message(message_id)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    
    async def get_all_sessions(self, request):
        """Получить last-active по всем сессиям всех аккаунтов пользователя."""
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        accounts = await db.list_accounts(user_id)
        all_sessions = []
        
        for account in accounts:
            try:
                async with userbot.client_from_blob(account["session_blob"], label=f"all_sess_{account['id']}") as client:
                    sessions = await userbot.list_sessions(client)
                    
                    for session in sessions:
                        all_sessions.append({
                            "account_label": account["label"],
                            "account_id": account["id"],
                            "hash": session["hash"],
                            "current": session["current"],
                            "device": f"{session['device_model']} ({session['platform']})",
                            "country": session["country"],
                            "last_active": session["date_active"],
                        })
            except Exception as e:
                logger.error(f"Error fetching sessions for account {account['id']}: {e}")
                continue
        
        # Сортировать по last_active
        all_sessions.sort(key=lambda x: x["last_active"], reverse=True)
        
        return web.json_response({"sessions": all_sessions})


    # ---------- Вход по номеру телефона ----------

    async def login_start(self, request):
        """Шаг 1: принимает номер телефона и название аккаунта, запрашивает код у Telegram."""
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        _cleanup_expired_logins()

        data = await request.json()
        phone_number = (data.get("phone") or "").strip()
        label = (data.get("label") or "").strip()[:50]

        if not phone_number or not label:
            return web.json_response({"error": "Укажите номер телефона и название"}, status=400)

        login_id = uuid.uuid4().hex
        workdir = tempfile.mkdtemp(prefix=f"login_{login_id}_")

        try:
            client, phone_code_hash = await userbot.start_phone_login(phone_number, workdir, label=login_id)
        except FloodWait as e:
            shutil.rmtree(workdir, ignore_errors=True)
            return web.json_response({"error": f"Telegram просит подождать {e.value} сек. перед повтором"}, status=429)
        except Exception as e:
            shutil.rmtree(workdir, ignore_errors=True)
            return web.json_response({"error": f"Не удалось отправить код: {e}"}, status=400)

        _pending_logins[login_id] = {
            "client": client,
            "phone": phone_number,
            "label": label,
            "phone_code_hash": phone_code_hash,
            "workdir": workdir,
            "user_id": user_id,
            "created_at": time.time(),
        }

        return web.json_response({"login_id": login_id, "message": "Код отправлен. Проверьте Telegram/SMS на этом номере."})

    async def login_code(self, request):
        """Шаг 2: принимает код, введённый пользователем. Может потребовать пароль 2FA."""
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        login_id = data.get("login_id")
        code = (data.get("code") or "").strip()

        pending = _pending_logins.get(login_id)
        if not pending or pending["user_id"] != user_id:
            return web.json_response({"error": "Сессия входа не найдена или истекла, начните заново"}, status=404)

        client = pending["client"]

        try:
            await userbot.confirm_phone_code(client, pending["phone"], pending["phone_code_hash"], code)
        except SessionPasswordNeeded:
            return web.json_response({"password_required": True})
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            return web.json_response({"error": "Неверный или истёкший код"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

        return await self._finalize_login(login_id)

    async def login_password(self, request):
        """Шаг 3 (если включена 2FA): принимает облачный пароль."""
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        login_id = data.get("login_id")
        password = data.get("password") or ""

        pending = _pending_logins.get(login_id)
        if not pending or pending["user_id"] != user_id:
            return web.json_response({"error": "Сессия входа не найдена или истекла, начните заново"}, status=404)

        client = pending["client"]

        try:
            await userbot.confirm_phone_password(client, password)
        except PasswordHashInvalid:
            return web.json_response({"error": "Неверный пароль"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

        return await self._finalize_login(login_id)

    async def _finalize_login(self, login_id: str):
        """Общий финал: достаём session-файл, сохраняем аккаунт в БД, чистим временные данные."""
        pending = _pending_logins.pop(login_id)
        client = pending["client"]
        workdir = pending["workdir"]

        try:
            info = await userbot.get_account_info(client)
            session_path = os.path.join(workdir, f"{login_id}.session")

            await client.disconnect()

            with open(session_path, "rb") as f:
                session_blob = f.read()

            account_id = await db.add_account(
                owner_id=pending["user_id"],
                label=pending["label"],
                session_blob=session_blob,
                phone=info.get("phone"),
                username=info.get("username"),
            )
            await sched.prime_session_baseline(account_id)

            return web.json_response({"success": True, "account_id": account_id, "info": info})
        except Exception as e:
            return web.json_response({"error": f"Вход выполнен, но не удалось сохранить аккаунт: {e}"}, status=500)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def add_account(self, request):
        """Добавить новый аккаунт по .session файлу, загруженному через Mini App."""
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        post_data = await request.post()
        label = post_data.get("label", "").strip()[:50]
        session_file = post_data.get("session")

        if not label:
            return web.json_response({"error": "Название аккаунта обязательно"}, status=400)
        if not session_file:
            return web.json_response({"error": "Файл сессии не передан"}, status=400)

        session_blob = session_file.file.read()

        # Проверяем, что сессия рабочая, прежде чем сохранять
        try:
            async with userbot.client_from_blob(session_blob, label=f"newacc_{user_id}") as client:
                info = await userbot.get_account_info(client)
        except Exception as e:
            return web.json_response({"error": f"Не удалось авторизоваться: {e}"}, status=400)

        account_id = await db.add_account(
            owner_id=user_id,
            label=label,
            session_blob=session_blob,
            phone=info.get("phone"),
            username=info.get("username"),
        )
        # Логируем текущие сессии как baseline, чтобы не словить ложное уведомление
        await sched.prime_session_baseline(account_id)
        return web.json_response({"success": True, "account_id": account_id, "info": info})

    async def delete_account(self, request):
        """Удалить аккаунт из панели (саму сессию в Telegram это не завершает)."""
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        account_id = data.get("account_id")

        account = await db.get_account(account_id)
        if not account or account["owner_id"] != user_id:
            return web.json_response({"error": "Not found"}, status=404)

        await db.delete_account(account_id)
        return web.json_response({"success": True})

    async def list_admins(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        db_admins = await db.list_admins()
        return web.json_response({
            "super_admins": list(ENV_ADMIN_IDS),
            "admins": [row["id"] for row in db_admins],
        })

    async def add_admin(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        new_admin_id = data.get("admin_id")
        if not new_admin_id:
            return web.json_response({"error": "admin_id обязателен"}, status=400)

        await db.add_admin(int(new_admin_id), added_by=user_id)
        return web.json_response({"success": True})

    async def remove_admin(self, request):
        user_id = await self.get_authed_user_id(request)
        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        target_id = int(data.get("admin_id"))

        if target_id in ENV_ADMIN_IDS:
            return web.json_response({"error": "Нельзя удалить супер-админа (задан в переменных окружения)"}, status=400)

        await db.remove_admin(target_id)
        return web.json_response({"success": True})


async def run_web_app(port=8080, bot=None):
    """Запустить Web App сервер."""
    handler = WebAppHandler(bot=bot)
    runner = web.AppRunner(handler.app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web App started on port {port}")
    return runner
