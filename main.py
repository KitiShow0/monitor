import os
import re
import html
import asyncio
import logging
from enum import Enum

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, errors, types as tl_types
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import ResolveUsernameRequest

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
API_ID = int(os.getenv("API_ID", "1234567"))
API_HASH = os.getenv("API_HASH", "ВАШ_API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING", "")
PORT = int(os.getenv("PORT", 8080))

# ID пользователя, которому доступна моментальная проверка
VIP_USER_ID = 6783917438

# --- НАСТРОЙКИ ТОЧНОСТИ ---
CHECK_INTERVAL = 600          # пауза между циклами мониторинга (сек)
CONFIRM_ATTEMPTS = 3          # сколько раз подряд проверка должна дать "бан"
CONFIRM_DELAY = 3.0           # пауза между подтверждающими проверками (сек)
STRIKES_TO_ALERT = 2          # сколько циклов подряд ресурс должен быть "забанен"
DELAY_BETWEEN_TARGETS = 1.5   # пауза между ресурсами (защита от FloodWait)
MAX_FLOOD_WAIT = 120          # дольше этого FloodWait не ждём

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

global_telethon_client = None
monitoring_tasks = {}

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


# --- СТАТУСЫ ---
class Status(Enum):
    OK = "ok"                  # ресурс жив
    BANNED = "banned"          # заблокирован Telegram (глобально)
    DELETED = "deleted"        # аккаунт удалён
    NOT_FOUND = "not_found"    # юзернейм свободен / не существует
    MISMATCH = "mismatch"      # тип не совпадает с выбранным
    UNKNOWN = "unknown"        # не удалось проверить (НЕ считается баном)


BAD_STATUSES = {Status.BANNED, Status.DELETED, Status.NOT_FOUND}


# --- МАШИНА СОСТОЯНИЙ (FSM) ---
class MonitorState(StatesGroup):
    waiting_for_type = State()
    waiting_for_username = State()


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (Keep-Alive) ---
async def web_handler(request):
    return web.Response(text="Bot is running 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", web_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def normalize_username(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", s, flags=re.I)
    s = s.lstrip("@")
    s = re.split(r"[/?#\s]", s)[0]
    return s


def global_restriction(entity):
    """
    Возвращает (True, причина), только если ресурс ограничен на ВСЕХ платформах.
    Ограничения вроде ios/android/windows НЕ считаются баном —
    ресурс при этом продолжает работать.
    """
    for r in getattr(entity, "restriction_reason", None) or []:
        if (getattr(r, "platform", "") or "").lower() == "all":
            return True, getattr(r, "reason", "") or "unknown"
    return False, ""


def find_entity(resolved):
    peer = resolved.peer
    if isinstance(peer, tl_types.PeerUser):
        for u in resolved.users:
            if u.id == peer.user_id:
                return u
    elif isinstance(peer, tl_types.PeerChannel):
        for c in resolved.chats:
            if c.id == peer.channel_id:
                return c
    return None


# --- ОДИНОЧНАЯ ПРОВЕРКА ---
async def check_once(client: TelegramClient, target_type: str, username: str):
    """Одна проверка. FloodWaitError пробрасывается наружу."""
    try:
        resolved = await client(ResolveUsernameRequest(username))
    except errors.UsernameNotOccupiedError:
        return Status.NOT_FOUND, f"Юзернейм @{username} не существует (свободен)."
    except errors.UsernameInvalidError:
        logging.warning(f"Некорректный юзернейм: {username}")
        return Status.UNKNOWN, "Некорректный юзернейм."
    except errors.FloodWaitError:
        raise
    except errors.ChannelBannedError:
        return Status.BANNED, f"@{username} заблокирован Telegram."
    except errors.ChannelPrivateError:
        # Может быть просто приватный канал или бан именно НАШЕГО аккаунта — не доказательство
        logging.info(f"{username}: ChannelPrivate — недостаточно для вывода о бане")
        return Status.UNKNOWN, "Канал приватный/недоступен."
    except errors.RPCError as e:
        if "FROZEN" in str(e).upper():
            logging.error("⚠️ Аккаунт-ПРОВЕРЩИК заморожен! Проверки недостоверны.")
        else:
            logging.warning(f"RPC ошибка при проверке {username}: {e}")
        return Status.UNKNOWN, "Ошибка Telegram API."
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        logging.warning(f"Сетевая ошибка при проверке {username}: {e}")
        return Status.UNKNOWN, "Сетевая ошибка."
    except Exception as e:
        logging.error(f"Неожиданная ошибка при проверке {username}: {e}")
        return Status.UNKNOWN, "Неожиданная ошибка."

    entity = find_entity(resolved)
    if entity is None:
        return Status.UNKNOWN, "Не удалось получить сущность."

    # --- Пользователь / бот ---
    if isinstance(entity, tl_types.User):
        if target_type in ("channel", "group"):
            return Status.MISMATCH, f"@{username} — это не канал/группа, а пользователь или бот."
        if target_type == "bot" and not entity.bot:
            return Status.MISMATCH, f"@{username} — это обычный аккаунт, а не бот."
        if target_type == "account" and entity.bot:
            return Status.MISMATCH, f"@{username} — это бот, а не аккаунт."
        if entity.deleted:
            return Status.DELETED, f"Аккаунт @{username} удалён."
        restricted, reason = global_restriction(entity)
        if restricted:
            kind = "Бот" if entity.bot else "Аккаунт"
            return Status.BANNED, f"{kind} @{username} заблокирован/заморожен Telegram (причина: {reason})."
        return Status.OK, ""

    # --- Канал / супергруппа ---
    if isinstance(entity, tl_types.Channel):
        if target_type in ("bot", "account"):
            return Status.MISMATCH, f"@{username} — это канал или группа, а не пользователь."
        if target_type == "channel" and not entity.broadcast:
            return Status.MISMATCH, f"@{username} — это группа, а не канал."
        if target_type == "group" and entity.broadcast and not entity.megagroup:
            return Status.MISMATCH, f"@{username} — это канал, а не группа."
        restricted, reason = global_restriction(entity)
        if restricted:
            return Status.BANNED, f"Ресурс @{username} заблокирован Telegram (причина: {reason})."
        return Status.OK, ""

    if isinstance(entity, tl_types.ChannelForbidden):
        return Status.UNKNOWN, "Нет доступа к каналу (ChannelForbidden)."

    return Status.UNKNOWN, "Неизвестный тип сущности."


async def safe_check(client, target_type, username):
    """check_once с обработкой FloodWait."""
    for _ in range(2):
        try:
            return await check_once(client, target_type, username)
        except errors.FloodWaitError as e:
            if e.seconds > MAX_FLOOD_WAIT:
                logging.warning(f"FloodWait {e.seconds}s — пропускаю проверку {username}")
                return Status.UNKNOWN, "FloodWait."
            logging.info(f"FloodWait {e.seconds}s, жду...")
            await asyncio.sleep(e.seconds + 1)
    return Status.UNKNOWN, "FloodWait."


# --- ПРОВЕРКА С ПОДТВЕРЖДЕНИЕМ ---
async def check_target_status(client: TelegramClient, target_type: str, username: str):
    """
    Вердикт "бан" выносится, только если CONFIRM_ATTEMPTS проверок подряд
    дали один и тот же плохой результат. Любой OK / UNKNOWN / MISMATCH
    возвращается сразу и баном не считается.
    """
    bad_results = []
    for i in range(CONFIRM_ATTEMPTS):
        status, msg = await safe_check(client, target_type, username)

        if status not in BAD_STATUSES:
            return status, msg

        bad_results.append((status, msg))
        if i < CONFIRM_ATTEMPTS - 1:
            await asyncio.sleep(CONFIRM_DELAY)

    if len({s for s, _ in bad_results}) == 1:
        return bad_results[-1]

    # Результаты расходятся — не уверены
    logging.info(f"{username}: нестабильные результаты {[s.value for s, _ in bad_results]}")
    return Status.UNKNOWN, "Нестабильный результат проверки."


# --- ФОНОВЫЙ ЦИКЛ МОНИТОРИНГА ---
async def monitoring_loop():
    while global_telethon_client is None:
        await asyncio.sleep(1)

    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        try:
            for user_id, targets in list(monitoring_tasks.items()):
                for target in targets[:]:
                    status, msg = await check_target_status(
                        global_telethon_client, target["type"], target["username"]
                    )

                    if status == Status.OK:
                        target["strikes"] = 0
                    elif status in BAD_STATUSES:
                        target["strikes"] = target.get("strikes", 0) + 1
                        logging.info(
                            f"{target['username']}: {status.value}, "
                            f"strike
