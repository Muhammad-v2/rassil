import os
import tempfile
import io
from contextlib import asynccontextmanager
from datetime import datetime

from pyrogram import Client
from pyrogram.raw import functions, types
from pyrogram.types import Chat

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]


@asynccontextmanager
async def client_from_blob(session_blob: bytes, label: str):
    """Развёртывает .session из БД, поднимает клиент и корректно закрывает."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session_path = os.path.join(tmpdir, f"{label}.session")
        with open(session_path, "wb") as f:
            f.write(session_blob)

        client = Client(
            name=label,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=tmpdir,
            no_updates=True,
        )
        await client.start()
        try:
            yield client
        finally:
            await client.stop()


async def get_account_info(client: Client) -> dict:
    """Получить базовую инфо об аккаунте."""
    me = await client.get_me()
    return {
        "id": me.id,
        "first_name": me.first_name,
        "last_name": me.last_name,
        "username": me.username,
        "phone": me.phone_number,
        "is_premium": bool(getattr(me, "is_premium", False)),
    }


async def list_sessions(client: Client) -> list[dict]:
    """Список активных сессий."""
    result = await client.invoke(functions.account.GetAuthorizations())
    sessions = []
    for auth in result.authorizations:
        sessions.append({
            "hash": auth.hash,
            "current": auth.current,
            "device_model": auth.device_model,
            "platform": auth.platform,
            "system_version": auth.system_version,
            "app_name": auth.app_name,
            "app_version": auth.app_version,
            "country": auth.country,
            "region": auth.region,
            "ip": auth.ip,
            "date_created": auth.date_created,
            "date_active": auth.date_active,
        })
    return sessions


async def terminate_session(client: Client, auth_hash: int):
    """Завершить сессию."""
    await client.invoke(functions.account.ResetAuthorization(hash=auth_hash))


async def send_message_as_account(client: Client, username: str, text: str, photo_bytes: bytes | None = None):
    """Отправить сообщение (текст или фото+подпись)."""
    username = username.lstrip("@")
    if photo_bytes:
        await client.send_photo(username, photo=io.BytesIO(photo_bytes), caption=text)
    else:
        await client.send_message(username, text)


async def get_all_dialogs(client: Client) -> dict:
    """Получить все диалоги, разделив на личные и групповые."""
    dialogs = {"private": [], "groups": []}
    
    async for dialog in client.get_dialogs():
        chat = dialog.chat
        chat_info = {
            "id": chat.id,
            "title": chat.title or chat.first_name or chat.username or f"Chat {chat.id}",
            "is_group": chat.is_group or chat.is_supergroup,
            "username": chat.username,
            "is_bot": getattr(chat, "is_bot", False),
        }
        
        if chat.is_group or chat.is_supergroup:
            dialogs["groups"].append(chat_info)
        else:
            dialogs["private"].append(chat_info)
    
    return dialogs


async def update_first_name(client: Client, first_name: str):
    """Изменить имя."""
    await client.invoke(functions.account.UpdateProfile(first_name=first_name))


async def update_last_name(client: Client, last_name: str):
    """Изменить фамилию."""
    await client.invoke(functions.account.UpdateProfile(last_name=last_name))


async def update_username(client: Client, username: str):
    """Изменить username."""
    await client.invoke(functions.account.UpdateUsername(username=username))


async def set_profile_photo(client: Client, photo_bytes: bytes):
    """Загрузить фото профиля."""
    file = await client.save_file(io.BytesIO(photo_bytes))
    await client.invoke(functions.photos.UploadProfilePhoto(file=file))


async def delete_profile_photo(client: Client):
    """Удалить фото профиля."""
    # Получить текущее фото
    me = await client.get_me()
    if me.photo:
        await client.invoke(
            functions.photos.DeletePhotos(id=[me.photo.file_id])
        )


async def get_profile_photo_bytes(client: Client) -> bytes | None:
    """Скачать фото профиля в bytes."""
    try:
        me = await client.get_me()
        if me.photo:
            return await client.download_media(me.photo)
    except:
        pass
    return None


# ---------- Вход по номеру телефона ----------

async def start_phone_login(phone_number: str, workdir: str, label: str) -> Client:
    """
    Создаёт клиент, подключается и запрашивает код подтверждения на указанный номер.
    Клиент остаётся подключённым — его нужно передать в confirm_phone_code,
    а после успешного или неуспешного входа обязательно disconnect().
    """
    client = Client(
        name=label,
        api_id=API_ID,
        api_hash=API_HASH,
        workdir=workdir,
        no_updates=True,
    )
    await client.connect()
    sent_code = await client.send_code(phone_number)
    return client, sent_code.phone_code_hash


async def confirm_phone_code(client: Client, phone_number: str, phone_code_hash: str, code: str):
    """
    Завершает вход по коду. Если на аккаунте включена двухфакторка,
    бросает pyrogram.errors.SessionPasswordNeeded — в этом случае
    нужно вызвать confirm_phone_password.
    """
    await client.sign_in(phone_number, phone_code_hash, code)


async def confirm_phone_password(client: Client, password: str):
    """Завершает вход паролем 2FA после SessionPasswordNeeded."""
    await client.check_password(password)
