import os
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, errors
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

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальная переменная для клиента Telethon
global_telethon_client = None

# Хранилище задач
monitoring_tasks = {}

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

# --- ЛОГИКА ПРОВЕРОК ---
async def check_target_status(client: TelegramClient, target_type: str, username: str):
    clean_username = username.lstrip('@').replace('https://t.me/', '')
    try:
        if target_type == 'bot':
            try:
                await bot.get_chat(f"@{clean_username}")
                return False, ""
            except Exception as e:
                error_text = str(e).lower()
                if "chat not found" in error_text or "user not found" in error_text or "blocked" in error_text:
                    return True, f"Бот @{clean_username} был удалён модерацией Telegram (или владельцем)."
                return False, ""

        elif target_type == 'account':
            try:
                result = await client(ResolveUsernameRequest(clean_username))
                if result.users:
                    user = result.users[0]
                    if hasattr(user, 'restricted') and user.restricted:
                        return True, f"Аккаунт @{clean_username} был заморожен модерацией Telegram."
                    return False, ""
                return True, f"Аккаунт @{clean_username} удалён."
            except errors.rpcerrorlist.UsernameNotOccupiedError:
                return True, f"Аккаунт @{clean_username} не существует."
            except errors.rpcerrorlist.BadRequestError as e:
                if "FROZEN_METHOD_INVALID" in str(e):
                    return True, f"Аккаунт @{clean_username} был заморожен модерацией Telegram."
                raise e

        elif target_type in ['channel', 'group']:
            try:
                entity = await client.get_entity(clean_username)
                if hasattr(entity, 'restricted') and entity.restricted:
                    return True, f"Ресурс @{clean_username} заморожен модерацией."
                return False, ""
            except errors.rpcerrorlist.ChannelBannedError:
                return True, f"Канал/Группа @{clean_username} заблокирован(а) (нарушение ToS)."
            except errors.rpcerrorlist.ChannelPrivateError:
                return True, f"Доступ к @{clean_username} закрыт (удалён или забанен)."
            except errors.rpcerrorlist.UsernameNotOccupiedError:
                return True, f"Ресурс @{clean_username} не существует."
    except Exception as e:
        logging.error(f"Ошибка проверки {username}: {e}")
    return False, ""

# --- ФОНОВЫЙ ЦИКЛ МОНИТОРИНГА ---
async def monitoring_loop():
    # Ждем инициализации клиента
    while global_telethon_client is None:
        await asyncio.sleep(1)
        
    while True:
        await asyncio.sleep(600)  # Ждем 10 минут
        
        for user_id, targets in list(monitoring_tasks.items()):
            for target in targets[:]:
                is_banned, msg = await check_target_status(global_telethon_client, target['type'], target['username'])
                if is_banned:
                    try:
                        await bot.send_message(user_id, f"⚠️ **ВНИМАНИЕ**\n{msg}")
                        targets.remove(target)
                    except Exception as e:
                        logging.error(f"Не удалось отправить сообщение {user_id}: {e}")

# --- ХЭНДЛЕРЫ БОТА ---
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
    await callback.message.edit_text(f"Выбран тип: **{type_names[target_type]}**.\nОтправь мне юзернейм (например, `@username`).")
    await state.set_state(MonitorState.waiting_for_username)
    await callback.answer()

@dp.message(MonitorState.waiting_for_username, F.text)
async def process_username(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_type = data.get('target_type')
    username = message.text.strip().replace('https://t.me/', '').replace('@', '')
    
    if not username:
        await message.answer("Некорректный юзернейм. Попробуй еще раз.")
        return

    user_id = message.from_user.id
    
    # ==========================================
    # ЛОГИКА МОМЕНТАЛЬНОЙ ПРОВЕРКИ ДЛЯ VIP-ЮЗЕРА
    # ==========================================
    if user_id == VIP_USER_ID:
        status_msg = await message.answer(f"🔍 Делаю моментальную проверку @{username}...")
        is_banned, msg = await check_target_status(global_telethon_client, target_type, username)
        
        if is_banned:
            await status_msg.edit_text(
                f"⚠️ **ВНИМАНИЕ**\n{msg}\n\n"
                f"Ресурс **не добавлен** в мониторинг, так как уже заблокирован или удалён."
            )
            await state.clear()
            return
            
        await status_msg.edit_text(f"✅ @{username} чист! Добавляю в общий мониторинг (проверка каждые 10 минут).")
        
    # ==========================================
    # СТАНДАРТНАЯ ЛОГИКА ДЛЯ ВСЕХ ОСТАЛЬНЫХ
    # ==========================================
    else:
        await message.answer(f"✅ @{username} добавлен в мониторинг!\nЯ проверяю его каждые 10 минут. Если Telegram его заблокирует, я сразу тебе напишу.")

    # Добавляем в базу мониторинга (для всех)
    if user_id not in monitoring_tasks:
        monitoring_tasks[user_id] = []
    
    monitoring_tasks[user_id].append({"type": target_type, "username": username})
    await state.clear()

# --- ЗАПУСК ---
async def main():
    global global_telethon_client
    
    await start_web_server()
    
    # Инициализируем и запускаем клиент
    global_telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await global_telethon_client.start()
    logging.info("Telethon запущен.")
    
    # Запускаем фоновую задачу (передавать client в аргументы больше не нужно, он берет глобальный)
    asyncio.create_task(monitoring_loop())
    
    logging.info("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
