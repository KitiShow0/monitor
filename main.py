import os
import re
import json
import html
import asyncio
import logging
from enum import Enum

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, errors, types as tl_types
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import ResolveUsernameRequest

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
API_ID = int(os.getenv("API_ID", "1234567"))
API_HASH = os.getenv("API_HASH", "ВАШ_API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING", "")
PORT = int(os.getenv("PORT", 8080))
DATA_FILE = os.getenv("DATA_FILE", "monitoring.json")

VIP_USER_ID = 6783917438

# --- ACCURACY SETTINGS ---
CHECK_INTERVAL = 600
CONFIRM_ATTEMPTS = 3
CONFIRM_DELAY = 3.0
STRIKES_TO_ALERT = 2
DELAY_BETWEEN_TARGETS = 1.5
MAX_FLOOD_WAIT = 120
WATCHDOG_INTERVAL = 60

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

global_telethon_client = None
monitoring_tasks = {}
health = {"checker_ok": True}

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class Status(Enum):
    OK = "ok"
    BANNED = "banned"
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


BAD_STATUSES = {Status.BANNED, Status.DELETED, Status.NOT_FOUND}


class MonitorState(StatesGroup):
    waiting_for_type = State()
    waiting_for_username = State()


# --- PERSISTENCE ---
def save_data():
    try:
        data = {str(uid): targets for uid, targets in monitoring_tasks.items()}
        tmp_path = DATA_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, DATA_FILE)
    except Exception as e:
        logging.error(f"Не удалось сохранить данные: {e}")


def load_data():
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for uid, targets in data.items():
            monitoring_tasks[int(uid)] = targets
        logging.info(f"Загружено пользователей: {len(monitoring_tasks)}")
    except Exception as e:
        logging.error(f"Не удалось загрузить данные (файл повреждён?): {e}")
        try:
            os.replace(DATA_FILE, DATA_FILE + ".broken")
        except Exception:
            pass


# --- SELF-HEALING SUPERVISOR ---
async def supervise(name, factory):
    """Restarts a task forever if it crashes, with growing backoff."""
    delay = 5
    while True:
        try:
            await factory()
            logging.warning(f"Задача {name} завершилась, перезапускаю")
            delay = 5
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Задача {name} упала: {e!r}. Перезапуск через {delay}с")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 120)


# --- WEB SERVER (Render keep-alive) ---
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


# --- HELPERS ---
def normalize_username(raw):
    s = raw.strip()
    s = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", s, flags=re.I)
    s = s.lstrip("@")
    s = re.split(r"[/?#\s]", s)[0]
    return s


def global_restriction(entity):
    """True only if restricted on ALL platforms (ios/android-only does not count)."""
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


async def ensure_connected(client):
    if not client.is_connected():
        logging.warning("Telethon не подключён, переподключаюсь")
        await client.connect()


# --- SINGLE CHECK ---
async def check_once(client, target_type, username):
    try:
        await ensure_connected(client)
        resolved = await client(ResolveUsernameRequest(username))
        health["checker_ok"] = True
    except errors.UsernameNotOccupiedError:
        health["checker_ok"] = True
        return Status.NOT_FOUND, f"Юзернейм @{username} не существует (свободен)."
    except errors.UsernameInvalidError:
        return Status.UNKNOWN, "Некорректный юзернейм."
    except errors.FloodWaitError:
        raise
    except errors.ChannelBannedError:
        return Status.BANNED, f"@{username} заблокирован Telegram."
    except errors.ChannelPrivateError:
        logging.info(f"{username}: ChannelPrivate, недостаточно для вывода о бане")
        return Status.UNKNOWN, "Канал приватный или недоступен."
    except errors.RPCError as e:
        if "FROZEN" in str(e).upper():
            health["checker_ok"] = False
            logging.error("Аккаунт-ПРОВЕРЩИК заморожен! Проверки недостоверны.")
        else:
            logging.warning(f"RPC ошибка при проверке {username}: {e}")
        return Status.UNKNOWN, "Ошибка Telegram API."
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        logging.warning(f"Сетевая ошибка при проверке {username}: {e}")
        return Status.UNKNOWN, "Сетевая ошибка."
    except Exception as e:
        logging.error(f"Неожиданная ошибка при проверке {username}: {e!r}")
        return Status.UNKNOWN, "Неожиданная ошибка."

    entity = find_entity(resolved)
    if entity is None:
        return Status.UNKNOWN, "Не удалось получить сущность."

    if isinstance(entity, tl_types.User):
        if target_type in ("channel", "group"):
            return Status.MISMATCH, f"@{username} это не канал/группа, а пользователь или бот."
        if target_type == "bot" and not entity.bot:
            return Status.MISMATCH, f"@{username} это обычный аккаунт, а не бот."
        if target_type == "account" and entity.bot:
            return Status.MISMATCH, f"@{username} это бот, а не аккаунт."
        if entity.deleted:
            return Status.DELETED, f"Аккаунт @{username} удалён."
        restricted, reason = global_restriction(entity)
        if restricted:
            kind = "Бот" if entity.bot else "Аккаунт"
            return Status.BANNED, f"{kind} @{username} заблокирован Telegram (причина: {reason})."
        return Status.OK, ""

    if isinstance(entity, tl_types.Channel):
        if target_type in ("bot", "account"):
            return Status.MISMATCH, f"@{username} это канал или группа, а не пользователь."
        if target_type == "channel" and not entity.broadcast:
            return Status.MISMATCH, f"@{username} это группа, а не канал."
        if target_type == "group" and entity.broadcast and not entity.megagroup:
            return Status.MISMATCH, f"@{username} это канал, а не группа."
        restricted, reason = global_restriction(entity)
        if restricted:
            return Status.BANNED, f"Ресурс @{username} заблокирован Telegram (причина: {reason})."
        return Status.OK, ""

    if isinstance(entity, tl_types.ChannelForbidden):
        return Status.UNKNOWN, "Нет доступа к каналу."

    return Status.UNKNOWN, "Неизвестный тип сущности."


async def safe_check(client, target_type, username):
    for _ in range(2):
        try:
            return await check_once(client, target_type, username)
        except errors.FloodWaitError as e:
            if e.seconds > MAX_FLOOD_WAIT:
                logging.warning(f"FloodWait {e.seconds}s, пропускаю {username}")
                return Status.UNKNOWN, "FloodWait."
            logging.info(f"FloodWait {e.seconds}s, жду")
            await asyncio.sleep(e.seconds + 1)
    return Status.UNKNOWN, "FloodWait."


async def check_target_status(client, target_type, username):
    """A ban verdict needs CONFIRM_ATTEMPTS identical bad results in a row."""
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

    logging.info(f"{username}: нестабильные результаты")
    return Status.UNKNOWN, "Нестабильный результат проверки."


# --- TELETHON WATCHDOG ---
async def telethon_watchdog():
    while global_telethon_client is None:
        await asyncio.sleep(1)

    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        client = global_telethon_client
        try:
            if not client.is_connected():
                logging.warning("Watchdog: переподключение Telethon")
                await client.connect()
            if not await client.is_user_authorized():
                health["checker_ok"] = False
                logging.error("Watchdog: сессия Telethon не авторизована! Нужен новый SESSION_STRING.")
                continue
            await client.get_me()
            health["checker_ok"] = True
        except errors.RPCError as e:
            if "FROZEN" in str(e).upper():
                health["checker_ok"] = False
                logging.error("Watchdog: аккаунт-проверщик заморожен.")
            else:
                logging.warning(f"Watchdog RPC: {e}")
        except Exception as e:
            logging.warning(f"Watchdog ошибка: {e!r}")


# --- MONITORING LOOP ---
async def monitoring_loop():
    while global_telethon_client is None:
        await asyncio.sleep(1)

    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        if not health["checker_ok"]:
            logging.warning("Пропускаю цикл: аккаунт-проверщик нездоров")
            continue

        for user_id, targets in list(monitoring_tasks.items()):
            for target in targets[:]:
                try:
                    status, msg = await check_target_status(
                        global_telethon_client, target["type"], target["username"]
                    )

                    if status == Status.OK:
                        if target.get("strikes", 0) != 0:
                            target["strikes"] = 0
                            save_data()
                    elif status in BAD_STATUSES:
                        target["strikes"] = target.get("strikes", 0) + 1
                        uname = target["username"]
                        count = target["strikes"]
                        logging.info(f"{uname}: {status.value}, strike {count}/{STRIKES_TO_ALERT}")
                        if count >= STRIKES_TO_ALERT:
                            await notify_and_remove(user_id, targets, target, msg)
                        else:
                            save_data()
                except Exception as e:
                    logging.error(f"Ошибка при обработке {target.get('username')}: {e!r}")

                await asyncio.sleep(DELAY_BETWEEN_TARGETS)


async def notify_and_remove(user_id, targets, target, msg):
    text = "⚠️ <b>ВНИМАНИЕ</b>\n" + html.escape(msg)
    try:
        await bot.send_message(user_id, text)
        if target in targets:
            targets.remove(target)
        save_data()
    except TelegramForbiddenError:
        logging.info(f"Пользователь {user_id} заблокировал бота, удаляю его цели")
        monitoring_tasks.pop(user_id, None)
        save_data()
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение {user_id}: {e}")


# --- BOT HANDLERS ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Аккаунт", callback_data="type_account")
    builder.button(text="🤖 Бот", callback_data="type_bot")
    builder.button(text="📢 Канал", callback_data="type_channel")
    builder.button(text="👥 Группа", callback_data="type_group")
    builder.adjust(2)

    await message.answer("Привет! Выбери тип ресурса для мониторинга:", reply_markup=builder.as_markup())
    await state.set_state(MonitorState.waiting_for_type)


@dp.callback_query(MonitorState.waiting_for_type, F.data.startswith("type_"))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    target_type = callback.data.split("_")[1]
    await state.update_data(target_type=target_type)

    type_names = {"account": "Аккаунт", "bot": "Бот", "channel": "Канал", "group": "Группа"}
    text = "Выбран тип: <b>" + type_names[target_type] + "</b>.\n"
    text += "Отправь мне юзернейм (например, <code>@username</code>)."
    await callback.message.edit_text(text)
    await state.set_state(MonitorState.waiting_for_username)
    await callback.answer()


@dp.message(MonitorState.waiting_for_username, F.text)
async def process_username(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_type = data.get("target_type")
    username = normalize_username(message.text)

    if not USERNAME_RE.match(username):
        await message.answer("Некорректный юзернейм. Попробуй еще раз.")
        return

    user_id = message.from_user.id

    if user_id == VIP_USER_ID and global_telethon_client is not None:
        status_msg = await message.answer(f"🔍 Делаю моментальную проверку @{username}...")
        status, msg = await check_target_status(global_telethon_client, target_type, username)

        if status in BAD_STATUSES:
            text = "⚠️ <b>ВНИМАНИЕ</b>\n" + html.escape(msg) + "\n\n"
            text += "Ресурс <b>не добавлен</b> в мониторинг, так как уже заблокирован или удалён."
            await status_msg.edit_text(text)
            await state.clear()
            return

        if status == Status.MISMATCH:
            text = "❗ " + html.escape(msg) + "\n\n"
            text += "Ресурс <b>не добавлен</b>. Нажми /start и выбери правильный тип."
            await status_msg.edit_text(text)
            await state.clear()
            return

        if status == Status.UNKNOWN:
            text = f"❔ Не удалось надёжно проверить @{username} прямо сейчас "
            text += "(это <b>не</b> означает блокировку). Добавляю в мониторинг."
            await status_msg.edit_text(text)
        else:
            text = f"✅ @{username} чист! Добавляю в общий мониторинг (проверка каждые 10 минут)."
            await status_msg.edit_text(text)
    else:
        text = f"✅ @{username} добавлен в мониторинг!\n"
        text += "Я проверяю его каждые 10 минут. Если Telegram его заблокирует, я сразу тебе напишу."
        await message.answer(text)

    monitoring_tasks.setdefault(user_id, []).append(
        {"type": target_type, "username": username, "strikes": 0}
    )
    save_data()
    await state.clear()


# --- STARTUP ---
async def run_polling():
    await dp.start_polling(bot, handle_signals=False)


async def main():
    global global_telethon_client

    if not SESSION_STRING:
        raise SystemExit("SESSION_STRING не задан. Сгенерируй его и добавь в переменные окружения.")

    load_data()
    await start_web_server()

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        connection_retries=-1,
        retry_delay=3,
        auto_reconnect=True,
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("SESSION_STRING недействителен или сессия отозвана. Создай новый.")
    global_telethon_client = client
    logging.info("Telethon запущен.")

    asyncio.create_task(supervise("monitoring", monitoring_loop))
    asyncio.create_task(supervise("watchdog", telethon_watchdog))

    logging.info("Бот запущен.")
    await supervise("polling", run_polling)


if __name__ == "__main__":
    asyncio.run(main())
