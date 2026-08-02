import asyncio
import os
import logging
import json
from datetime import datetime
from typing import Optional, Set, Dict, Any, List, Union

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    TelegramObject,
    ChatMemberUpdated,
    ChatJoinRequest
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")

CHANNEL_ID = os.getenv("CHANNEL_ID", "@zakazat_sayt_dlya_shkoly")
GROUP_ID = os.getenv("GROUP_ID", "@zakazatsaytdlyashkoly")
SITE_URL = os.getenv("SITE_URL", "https://www.metaimperiya.com/")

MAIN_BOT_URL = "https://t.me/Biznes_kursy_bot"
ADMIN_USERNAME = "@METAIMPERIYA"

ADMIN_IDS: Set[int] = set()
raw_admin_ids = os.getenv("ADMIN_ID", "")
if raw_admin_ids:
    for part in raw_admin_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))
    if ADMIN_IDS:
        logger.info(f"✅ Админы: {ADMIN_IDS}")
    else:
        logger.warning("⚠️ ADMIN_ID не содержит корректных ID")

# ==================== БАЗА ДАННЫХ ====================
DB_NAME = os.getenv("DB_NAME", "bot_database.db")

class Database:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_name)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA busy_timeout = 5000;")
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._init_db()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _init_db(self):
        async with self._conn.cursor() as cursor:
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    name TEXT,
                    service TEXT,
                    contact TEXT,
                    comment TEXT,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'new'
                )
            """)
            
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    text TEXT,
                    photo_id TEXT,
                    video_id TEXT,
                    media_type TEXT,
                    buttons TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    action_type TEXT,
                    action_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await self._conn.commit()
            logger.info("✅ База данных готова")

    async def save_user(self, telegram_id: int, username: str, first_name: str, last_name: str = ""):
        await self._conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, last_active)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_active = CURRENT_TIMESTAMP
            """,
            (telegram_id, username[:50], first_name[:50], last_name[:50]),
        )
        await self._conn.commit()

    async def save_order(self, telegram_id: int, name: str, service: str, 
                         contact: str, comment: str, username: str) -> int:
        cursor = await self._conn.execute(
            """
            INSERT INTO orders (telegram_id, name, service, contact, comment, username)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, name[:100], service[:200], contact[:200], comment[:500], username[:50]),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def save_post_full(self, admin_id: int, text: str, media_type: str = "text", 
                             media_id: Optional[str] = None, buttons: Optional[List] = None) -> int:
        buttons_json = json.dumps(buttons) if buttons else None
        cursor = await self._conn.execute(
            """INSERT INTO posts (admin_id, text, photo_id, video_id, media_type, buttons) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            (admin_id, text, 
             media_id if media_type == "photo" else None,
             media_id if media_type == "video" else None,
             media_type, buttons_json)
        )
        await self._conn.commit()
        return cursor.lastrowid
    
    async def save_action(self, telegram_id: int, action_type: str, action_data: str = ""):
        await self._conn.execute(
            "INSERT INTO user_actions (telegram_id, action_type, action_data) VALUES (?, ?, ?)",
            (telegram_id, action_type, action_data[:200])
        )
        await self._conn.commit()
    
    async def get_posts(self, limit: int = 10) -> List[Dict]:
        async with self._conn.cursor() as cursor:
            await cursor.execute(
                "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            rows = await cursor.fetchall()
            posts = []
            for row in rows:
                post = dict(row)
                if post.get('buttons'):
                    post['buttons'] = json.loads(post['buttons'])
                posts.append(post)
            return posts
    
    async def delete_post(self, post_id: int):
        await self._conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        await self._conn.commit()

db = Database()

# ==================== MIDDLEWARE ====================
class UserTrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            try:
                await db.save_user(
                    telegram_id=user.id,
                    username=user.username or "",
                    first_name=user.first_name or "",
                    last_name=user.last_name or "",
                )
                await notify_admin_about_user(user)
            except Exception as e:
                logger.error(f"Ошибка сохранения пользователя: {e}")
        return await handler(event, data)

# ==================== УВЕДОМЛЕНИЯ АДМИНУ ====================
async def notify_admin_about_user(user) -> bool:
    try:
        notification_text = (
            "🔔 <b>НОВЫЙ ПОЛЬЗОВАТЕЛЬ!</b>\n\n"
            f"👤 <b>Имя:</b> {user.first_name}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
            f"📛 <b>Юзернейм:</b> @{user.username or 'нет'}\n"
            f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"🌐 <a href='{SITE_URL}'>На сайт</a> | <a href='{MAIN_BOT_URL}'>Главный бот</a>"
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=notification_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Не удалось отправить админу {admin_id}: {e}")
        
        try:
            await bot.send_message(chat_id=ADMIN_USERNAME, text=notification_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Не удалось отправить @{ADMIN_USERNAME}: {e}")
            
        return True
    except Exception as e:
        logger.error(f"Ошибка уведомления: {e}")
        return False

async def notify_admin_about_action(action_type: str, user, extra_data: str = ""):
    try:
        notification_text = (
            f"📊 <b>ДЕЙСТВИЕ ПОЛЬЗОВАТЕЛЯ</b>\n\n"
            f"🎯 <b>Действие:</b> {action_type}\n"
            f"👤 <b>Имя:</b> {user.first_name}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
            f"📛 <b>Юзернейм:</b> @{user.username or 'нет'}\n"
            f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
        )
        
        if extra_data:
            notification_text += f"\n📝 <b>Доп.инфо:</b> {extra_data}\n"
        
        notification_text += f"\n🌐 <a href='{SITE_URL}'>На сайт</a> | <a href='{MAIN_BOT_URL}'>Главный бот</a>"
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=notification_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Не удалось отправить админу {admin_id}: {e}")
                
        try:
            await bot.send_message(chat_id=ADMIN_USERNAME, text=notification_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Не удалось отправить @{ADMIN_USERNAME}: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка уведомления о действии: {e}")

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
dp.update.outer_middleware(UserTrackingMiddleware())

# ==================== ДАННЫЕ УСЛУГ ====================
SERVICES = {
    "album": {
        "emoji": "🎓",
        "name": "Выпускной альбом / Класс",
        "price": "от $80",
        "description": "• Живые фото и видео\n• Персональная страница каждого ученика\n• Онлайн-таймер до выпускного",
        "photo": "https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=800&q=80",
    },
    "primary": {
        "emoji": "🎒",
        "name": "Сайт для 1-4 классов",
        "price": "от $65",
        "description": "• Расписание уроков и объявлений\n• Фотоотчеты с мероприятий и экскурсий\n• Удобный доступ для родителей",
        "photo": "https://images.unsplash.com/photo-1509062522246-3755977927d7?auto=format&fit=crop&w=800&q=80",
    },
    "school": {
        "emoji": "🏫",
        "name": "Официальный сайт школы",
        "price": "от $220",
        "description": "• Полное соответствие стандартам\n• Разделы: Документы, Педсостав, Новости\n• Высокая защита и быстродействие",
        "photo": "https://images.unsplash.com/photo-1580582932707-520aed937b7b?auto=format&fit=crop&w=800&q=80",
    },
    "portfolio": {
        "emoji": "🏆",
        "name": "Портфолио ученика / Учителя",
        "price": "от $40",
        "description": "• Для аттестации учителя или поступления ученика\n• Галерея грамот, проектов и достижений\n• Презентабельный вид на любых устройствах",
        "photo": "https://images.unsplash.com/photo-1434030216411-0b793f4b4173?auto=format&fit=crop&w=800&q=80",
    }
}

# ==================== СОСТОЯНИЯ FSM ====================
class PostStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_text = State()
    waiting_for_buttons = State()
    waiting_for_confirmation = State()

class AdminPostStates(StatesGroup):
    waiting_for_media = State()
    waiting_for_text = State()
    waiting_for_buttons = State()
    waiting_for_confirmation = State()

class OrderStates(StatesGroup):
    service = State()
    name = State()
    contact = State()
    comment = State()

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎓 Выпускной альбом / Класс")],
            [KeyboardButton(text="🎒 Сайт для 1-4 классов")],
            [KeyboardButton(text="🏫 Официальный сайт школы")],
            [KeyboardButton(text="🏆 Портфолио ученика / Учителя")],
            [KeyboardButton(text="📱 Наш сайт"), KeyboardButton(text="📞 Заявка")],
            [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="💼 Вакансии")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите услугу..."
    )

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True
    )

def get_post_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="post_publish")],
        [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="post_edit_text")],
        [InlineKeyboardButton(text="🔄 Изменить медиа", callback_data="post_edit_photo")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="post_cancel")]
    ])

def get_channel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL),
            InlineKeyboardButton(text="🌐 Наш сайт", url=SITE_URL)
        ],
        [
            InlineKeyboardButton(text="📈 Бизнес-коучинг", url="https://t.me/Biznes_kouching"),
            InlineKeyboardButton(text="💼 Бизнес-консультант", url="https://t.me/Bizneskonsultant")
        ],
        [
            InlineKeyboardButton(text="📊 Тренинги продаж", url="https://t.me/Treningi_po_prodazham"),
            InlineKeyboardButton(text="📚 Курсы коучинга", url="https://t.me/kursy_biznes_kouchinga")
        ],
        [
            InlineKeyboardButton(text="📝 Заказать услугу", callback_data="order_from_channel"),
            InlineKeyboardButton(text="❓ FAQ", callback_data="faq")
        ]
    ])

def get_all_links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL),
            InlineKeyboardButton(text="🌐 Сайт", url=SITE_URL)
        ],
        [
            InlineKeyboardButton(text="📈 Коучинг", url="https://t.me/Biznes_kouching"),
            InlineKeyboardButton(text="💼 Консалтинг", url="https://t.me/Bizneskonsultant")
        ],
        [
            InlineKeyboardButton(text="📊 Тренинги", url="https://t.me/Treningi_po_prodazham"),
            InlineKeyboardButton(text="📚 Курсы", url="https://t.me/kursy_biznes_kouchinga")
        ]
    ])

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ==================== ПРИВЕТСТВИЕ ====================
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    await db.save_action(message.from_user.id, "start", "Запуск бота")
    await notify_admin_about_action("🚀 Запуск бота", message.from_user)
    
    welcome_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL),
            InlineKeyboardButton(text="🌐 Наш сайт", url=SITE_URL)
        ],
        [
            InlineKeyboardButton(text="📈 Бизнес-коучинг", url="https://t.me/Biznes_kouching"),
            InlineKeyboardButton(text="💼 Бизнес-консультант", url="https://t.me/Bizneskonsultant")
        ],
        [
            InlineKeyboardButton(text="📊 Тренинги продаж", url="https://t.me/Treningi_po_prodazham"),
            InlineKeyboardButton(text="📚 Курсы бизнес-коучинга", url="https://t.me/kursy_biznes_kouchinga")
        ],
        [
            InlineKeyboardButton(text="📞 Заказать звонок", callback_data="call_request"),
            InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")
        ]
    ])
    
    welcome_text = (
        f"👋 <b>Салам, {message.from_user.first_name}!</b>\n\n"
        "Добро пожаловать в <b>MetaImperiya</b>! 🚀\n\n"
        "🎯 <b>Наши проекты:</b>\n"
        "🤖 <a href='{}'>Главный бот</a> - бизнес-курсы\n"
        "🌐 <a href='{}'>Сайт</a> - полная информация\n\n"
        "📢 <b>Наши каналы:</b>\n"
        "• <a href='https://t.me/Biznes_kouching'>Бизнес-коучинг</a>\n"
        "• <a href='https://t.me/Bizneskonsultant'>Бизнес-консультант</a>\n"
        "• <a href='https://t.me/Treningi_po_prodazham'>Тренинги продаж</a>\n"
        "• <a href='https://t.me/kursy_biznes_kouchinga'>Курсы коучинга</a>\n\n"
        "💰 <b>Все цены указаны в USD</b>\n\n"
        "📌 <b>Выберите действие ниже:</b>"
    ).format(MAIN_BOT_URL, SITE_URL)
    
    await message.answer(welcome_text, reply_markup=welcome_keyboard)
    await message.answer("Или выберите услугу в меню ниже:", reply_markup=get_main_keyboard())

# ==================== ВХОД В КАНАЛ/ГРУППУ ====================
@dp.chat_member()
async def chat_member_update(event: ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        user = event.from_user
        chat = event.chat
        
        await db.save_action(user.id, "join_chat", f"Вступил в {chat.title} ({chat.id})")
        await notify_admin_about_action(f"➕ Вступил в канал/группу: {chat.title}", user, f"Чат ID: {chat.id}")
        
        try:
            welcome_text = (
                f"👋 <b>Добро пожаловать, {user.first_name}!</b>\n\n"
                "🎯 <b>Наши ресурсы:</b>\n"
                "🤖 <a href='{}'>Главный бот</a> - бизнес-курсы\n"
                "🌐 <a href='{}'>Сайт</a> - полная информация\n\n"
                "📢 <b>Другие наши каналы:</b>\n"
                "• <a href='https://t.me/Biznes_kouching'>Бизнес-коучинг</a>\n"
                "• <a href='https://t.me/Bizneskonsultant'>Бизнес-консультант</a>\n"
                "• <a href='https://t.me/Treningi_po_prodazham'>Тренинги продаж</a>\n"
                "• <a href='https://t.me/kursy_biznes_kouchinga'>Курсы коучинга</a>"
            ).format(MAIN_BOT_URL, SITE_URL)
            
            await bot.send_message(chat_id=user.id, text=welcome_text, reply_markup=get_channel_menu())
        except Exception as e:
            logger.error(f"Не удалось отправить приветствие пользователю {user.id}: {e}")

@dp.chat_join_request()
async def handle_join_request(event: ChatJoinRequest):
    user = event.from_user
    chat = event.chat
    
    try:
        await event.approve()
        await db.save_action(user.id, "join_request_approved", f"Одобрена заявка в {chat.title}")
        await notify_admin_about_action(f"✅ Одобрена заявка в {chat.title}", user)
        
        welcome_text = (
            f"👋 <b>Добро пожаловать, {user.first_name}!</b>\n\n"
            "🎯 <b>Наши ресурсы:</b>\n"
            "🤖 <a href='{}'>Главный бот</a> - бизнес-курсы\n"
            "🌐 <a href='{}'>Сайт</a> - полная информация\n\n"
            "📢 <b>Наши каналы:</b>\n"
            "• <a href='https://t.me/Biznes_kouching'>Бизнес-коучинг</a>\n"
            "• <a href='https://t.me/Bizneskonsultant'>Бизнес-консультант</a>\n"
            "• <a href='https://t.me/Treningi_po_prodazham'>Тренинги продаж</a>\n"
            "• <a href='https://t.me/kursy_biznes_kouchinga'>Курсы коучинга</a>"
        ).format(MAIN_BOT_URL, SITE_URL)
        
        await bot.send_message(chat_id=user.id, text=welcome_text, reply_markup=get_channel_menu())
    except Exception as e:
        logger.error(f"Ошибка обработки заявки: {e}")

# ==================== CALLBACK'И ====================
@dp.callback_query(F.data == "call_request")
async def call_request(callback: CallbackQuery):
    await callback.answer()
    await db.save_action(callback.from_user.id, "call_request", "Запрос звонка")
    await notify_admin_about_action("📞 Запрос звонка", callback.from_user)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL)],
        [InlineKeyboardButton(text="🌐 Наш сайт", url=SITE_URL)]
    ])
    
    await callback.message.answer(
        "📞 <b>Заказать звонок</b>\n\n"
        "Напишите нам в главном боте, и мы перезвоним вам в течение 15 минут!\n\n"
        "📌 <i>Укажите удобное время для звонка</i>",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "show_channels")
async def show_channels(callback: CallbackQuery):
    await callback.answer()
    
    await callback.message.answer(
        "📢 <b>Наши каналы и ресурсы</b>\n\n"
        "🤖 <b>Главный бот:</b>\n"
        f"<a href='{MAIN_BOT_URL}'>Biznes_kursy_bot</a>\n\n"
        "🌐 <b>Сайт:</b>\n"
        f"<a href='{SITE_URL}'>MetaImperiya</a>\n\n"
        "📈 <b>Бизнес-коучинг:</b>\n"
        "<a href='https://t.me/Biznes_kouching'>@Biznes_kouching</a>\n\n"
        "💼 <b>Бизнес-консультант:</b>\n"
        "<a href='https://t.me/Bizneskonsultant'>@Bizneskonsultant</a>\n\n"
        "📊 <b>Тренинги продаж:</b>\n"
        "<a href='https://t.me/Treningi_po_prodazham'>@Treningi_po_prodazham</a>\n\n"
        "📚 <b>Курсы бизнес-коучинга:</b>\n"
        "<a href='https://t.me/kursy_biznes_kouchinga'>@kursy_biznes_kouchinga</a>",
        reply_markup=get_all_links_keyboard()
    )

@dp.callback_query(F.data == "order_from_channel")
async def order_from_channel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "📝 <b>Оформление заявки</b>\n\n"
        "Напишите, какой проект вас интересует:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(OrderStates.service)

@dp.callback_query(F.data == "show_portfolio")
async def show_portfolio(callback: CallbackQuery):
    await callback.answer()
    await db.save_action(callback.from_user.id, "view_portfolio", "Просмотр портфолио")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Смотреть все проекты", url=SITE_URL)],
        [InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL)]
    ])
    
    await callback.message.answer(
        "📸 <b>Наши работы</b>\n\n"
        "Мы создали более 100+ проектов для школ и учебных заведений.\n\n"
        "🎯 <b>Примеры наших работ:</b>\n"
        "• Интерактивные выпускные альбомы\n"
        "• Современные сайты для школ\n"
        "• Цифровые портфолио\n\n"
        "👉 <i>Все проекты смотрите на нашем сайте:</i>",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "faq")
async def faq(callback: CallbackQuery):
    await callback.answer()
    await db.save_action(callback.from_user.id, "faq", "Просмотр FAQ")
    
    faq_text = (
        "❓ <b>Частые вопросы</b>\n\n"
        "🔹 <b>Сколько времени занимает разработка?</b>\n"
        "Обычно 3-7 дней, в зависимости от сложности проекта.\n\n"
        "🔹 <b>Какая оплата?</b>\n"
        "Работаем по предоплате 50%. Оплата в USD.\n\n"
        "🔹 <b>Что нужно для старта?</b>\n"
        "Достаточно заполнить заявку или написать менеджеру.\n\n"
        "🔹 <b>Есть ли гарантия?</b>\n"
        "Да, мы даем гарантию 6 месяцев на все работы.\n\n"
        "🔹 <b>Можно ли внести правки?</b>\n"
        "Да, мы вносим правки до полного утверждения.\n\n"
        "📌 <i>Остались вопросы? Напишите менеджеру!</i>"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL)],
        [InlineKeyboardButton(text="🌐 Наш сайт", url=SITE_URL)]
    ])
    
    await callback.message.answer(faq_text, reply_markup=keyboard)

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Создать пост", callback_data="admin_create_post")],
        [InlineKeyboardButton(text="📋 Мои посты", callback_data="admin_my_posts")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="admin_orders")],
        [InlineKeyboardButton(text="🔔 Тест уведомления", callback_data="admin_test_notify")]
    ])
    
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        f"📢 Канал: {CHANNEL_ID}\n"
        f"💬 Группа: {GROUP_ID}\n"
        f"💰 Валюта: USD\n"
        f"🤖 Главный бот: {MAIN_BOT_URL}\n"
        f"🌐 Сайт: {SITE_URL}",
        reply_markup=keyboard
    )

# ==================== СОЗДАНИЕ ПОСТОВ (АДМИНКА) ====================
@dp.callback_query(F.data == "admin_create_post")
async def admin_create_post(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    
    await state.set_state(AdminPostStates.waiting_for_media)
    await callback.message.answer(
        "📝 <b>Создание поста</b>\n\n"
        "Отправьте <b>фото</b> (JPG/PNG) или <b>видео</b> (MP4)\n"
        "Или нажмите /skip - для текстового поста\n\n"
        "🔄 /cancel - отмена"
    )

@dp.message(Command("cancel"), StateFilter(AdminPostStates))
async def cancel_post_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Создание поста отменено.", reply_markup=get_main_keyboard())

@dp.message(Command("skip"), AdminPostStates.waiting_for_media)
async def skip_media_admin(message: Message, state: FSMContext):
    await state.update_data(media_type="text", media_id=None)
    await state.set_state(AdminPostStates.waiting_for_text)
    await message.answer("✏️ Введите <b>текст поста</b> (поддерживается HTML):")

@dp.message(AdminPostStates.waiting_for_media, F.photo)
async def process_photo_admin(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(media_type="photo", media_id=photo_id)
    await state.set_state(AdminPostStates.waiting_for_text)
    await message.answer("✅ Фото принято! Теперь введите <b>текст поста</b>:")

@dp.message(AdminPostStates.waiting_for_media, F.video)
async def process_video_admin(message: Message, state: FSMContext):
    video_id = message.video.file_id
    await state.update_data(media_type="video", media_id=video_id)
    await state.set_state(AdminPostStates.waiting_for_text)
    await message.answer("✅ Видео принято! Теперь введите <b>текст поста</b>:")

@dp.message(AdminPostStates.waiting_for_media)
async def process_media_unknown_admin(message: Message, state: FSMContext):
    await message.answer(
        "❌ Отправьте <b>фото</b> или <b>видео</b>\n"
        "Или нажмите /skip для текстового поста"
    )

@dp.message(AdminPostStates.waiting_for_text)
async def process_text_admin(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(AdminPostStates.waiting_for_buttons)
    
    await message.answer(
        "🔘 <b>Добавьте кнопки</b>\n\n"
        "Формат: <code>Текст - https://ссылка.com</code>\n"
        "Пример:\n"
        "<code>Заказать - https://t.me/Biznes_kursy_bot</code>\n"
        "<code>На сайт - https://www.metaimperiya.com/</code>\n\n"
        "📌 /skip - если кнопки не нужны"
    )

@dp.message(Command("skip"), AdminPostStates.waiting_for_buttons)
async def skip_buttons_admin(message: Message, state: FSMContext):
    await state.update_data(buttons=[])
    await show_post_preview_admin(message, state)

@dp.message(AdminPostStates.waiting_for_buttons)
async def process_buttons_admin(message: Message, state: FSMContext):
    lines = message.text.strip().split("\n")
    buttons = []
    
    for line in lines:
        if " - " in line:
            parts = line.split(" - ", 1)
            btn_text = parts[0].strip()
            btn_url = parts[1].strip()
            
            if btn_url.startswith(("http://", "https://", "tg://")):
                buttons.append([InlineKeyboardButton(text=btn_text, url=btn_url)])
    
    if not buttons:
        await message.answer("⚠️ Не найдено кнопок. Нажмите /skip")
        return
    
    await state.update_data(buttons=buttons)
    await show_post_preview_admin(message, state)

async def show_post_preview_admin(message: Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("text", "")
    media_type = data.get("media_type", "text")
    media_id = data.get("media_id")
    buttons = data.get("buttons", [])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    preview_text = (
        "📋 <b>Превью поста</b>\n\n"
        f"📝 {text[:300]}{'...' if len(text) > 300 else ''}\n\n"
        f"🖼 Медиа: {'✅ ' + media_type if media_id else '❌'}\n"
        f"🔘 Кнопки: {len(buttons)} шт.\n\n"
        "👇 Нажмите 'Опубликовать'"
    )
    
    try:
        if media_type == "photo" and media_id:
            await message.answer_photo(
                photo=media_id,
                caption=preview_text,
                reply_markup=get_post_confirm_keyboard()
            )
        elif media_type == "video" and media_id:
            await message.answer_video(
                video=media_id,
                caption=preview_text,
                reply_markup=get_post_confirm_keyboard()
            )
        else:
            await message.answer(preview_text, reply_markup=get_post_confirm_keyboard())
        
        await state.set_state(AdminPostStates.waiting_for_confirmation)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(F.data == "post_publish", AdminPostStates.waiting_for_confirmation)
async def publish_post_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        await callback.message.answer("⛔ Доступ запрещен!")
        return
    
    data = await state.get_data()
    text = data.get("text", "")
    media_type = data.get("media_type", "text")
    media_id = data.get("media_id")
    buttons = data.get("buttons", [])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    try:
        if media_type == "photo" and media_id:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=media_id, caption=text, reply_markup=keyboard)
        elif media_type == "video" and media_id:
            await bot.send_video(chat_id=CHANNEL_ID, video=media_id, caption=text, reply_markup=keyboard)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=keyboard)
        
        await db.save_post_full(
            admin_id=callback.from_user.id,
            text=text,
            media_type=media_type,
            media_id=media_id,
            buttons=buttons
        )
        
        await bot.send_message(
            chat_id=GROUP_ID,
            text=f"📢 <b>Новый пост!</b>\n\n{text[:200]}...",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Перейти в канал", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")]
            ])
        )
        
        await callback.message.answer(f"✅ <b>Пост опубликован!</b>\n📢 {CHANNEL_ID}")
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await callback.message.answer(f"❌ Ошибка публикации: {e}")

@dp.callback_query(F.data == "post_edit_text", AdminPostStates.waiting_for_confirmation)
async def edit_post_text_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdminPostStates.waiting_for_text)
    await callback.message.answer("✏️ Введите новый текст:")

@dp.callback_query(F.data == "post_edit_photo", AdminPostStates.waiting_for_confirmation)
async def edit_post_media_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdminPostStates.waiting_for_media)
    await callback.message.answer("📸 Отправьте новое фото/видео или /skip:")

@dp.callback_query(F.data == "post_cancel", AdminPostStates.waiting_for_confirmation)
async def cancel_publish_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("❌ Отменено.", reply_markup=get_main_keyboard())

# ==================== МОИ ПОСТЫ ====================
@dp.callback_query(F.data == "admin_my_posts")
async def admin_my_posts(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    
    posts = await db.get_posts(limit=10)
    
    if not posts:
        await callback.message.answer("📭 Пока нет постов.")
        return
    
    text = "📋 <b>Последние посты:</b>\n\n"
    
    for post in posts:
        post_id = post['id']
        date = post['created_at'][:10] if post['created_at'] else "дата неизвестна"
        media_icon = "🎬" if post['media_type'] == "video" else ("🖼" if post['media_type'] == "photo" else "📝")
        text += f"{media_icon} Пост #{post_id} | {date}\n"
    
    text += "\n🔜 Скоро: просмотр и удаление постов"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
    ])
    
    await callback.message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    await callback.answer()
    await admin_panel(callback.message)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    
    await callback.message.answer(
        "📊 <b>Статистика</b>\n\n"
        "Данные загружаются из БД..."
    )

@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    
    await callback.message.answer(
        "📋 <b>Последние заявки</b>\n\n"
        "Данные загружаются из БД..."
    )

@dp.callback_query(F.data == "admin_test_notify")
async def admin_test_notify(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    
    await notify_admin_about_action(
        "🧪 Тестовое уведомление",
        callback.from_user,
        "Это тестовое сообщение от бота"
    )
    await callback.message.answer("✅ Тестовое уведомление отправлено!")

# ==================== ОФОРМЛЕНИЕ ЗАЯВОК ====================
@dp.message(F.text == "📞 Заявка")
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderStates.service)
    await message.answer(
        "📝 <b>Оформление заявки</b>\n\nНапишите, какой проект вас интересует:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(F.text == "❌ Отменить", StateFilter(OrderStates))
async def cancel_order(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Заявка отменена.", reply_markup=get_main_keyboard())

@dp.message(OrderStates.service)
async def process_service(message: Message, state: FSMContext):
    await state.update_data(service=message.text)
    await state.set_state(OrderStates.name)
    await message.answer("👤 Как к вам обращаться?", reply_markup=get_cancel_keyboard())

@dp.message(OrderStates.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(OrderStates.contact)
    await message.answer("📱 Укажите телефон или @username:", reply_markup=get_cancel_keyboard())

@dp.message(OrderStates.contact)
async def process_contact(message: Message, state: FSMContext):
    await state.update_data(contact=message.text)
    await state.set_state(OrderStates.comment)
    await message.answer(
        "💬 Комментарий (необязательно):\n/skip - пропустить",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(Command("skip"), OrderStates.comment)
async def skip_comment(message: Message, state: FSMContext):
    await state.update_data(comment="Нет")
    await finish_order(message, state)

@dp.message(OrderStates.comment)
async def process_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text)
    await finish_order(message, state)

async def finish_order(message: Message, state: FSMContext):
    data = await state.get_data()
    
    try:
        order_id = await db.save_order(
            telegram_id=message.from_user.id,
            name=data.get('name', 'Не указано'),
            service=data.get('service', 'Не указано'),
            contact=data.get('contact', 'Не указано'),
            comment=data.get('comment', 'Нет'),
            username=message.from_user.username or "нет_юзернейма"
        )
        logger.info(f"✅ Заявка #{order_id} создана")
        
        await db.save_action(
            message.from_user.id,
            "order_created",
            f"Заявка #{order_id}: {data.get('service', '')}"
        )
        
        await notify_admin_about_action(
            f"📝 Новая заявка #{order_id}",
            message.from_user,
            f"Услуга: {data.get('service', '')}"
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        await message.answer("❌ Ошибка при сохранении")
        await state.clear()
        return
    
    order_text = (
        "🚀 <b>НОВАЯ ЗАЯВКА!</b>\n"
        f"🆔 №: {order_id}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"👤 Клиент: {data.get('name')}\n"
        f"🛠 Услуга: {data.get('service')}\n"
        f"📞 Контакт: {data.get('contact')}\n"
        f"💬 Коммент: {data.get('comment')}\n"
        f"🔗 @{message.from_user.username or 'нет'}"
    )
    
    try:
        await bot.send_message(chat_id=GROUP_ID, text=order_text)
        logger.info(f"📨 Заявка в группу {GROUP_ID}")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=order_text)
        except Exception as e:
            logger.error(f"❌ Ошибка админу {admin_id}: {e}")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на сайт", url=SITE_URL)],
        [InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL)]
    ])
    
    await message.answer(
        "✅ <b>Заявка принята!</b>\nМы свяжемся с вами!",
        reply_markup=keyboard
    )
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())
    await state.clear()

# ==================== ОБРАБОТЧИКИ УСЛУГ ====================
@dp.message(F.text.in_([f"{data['emoji']} {data['name']}" for data in SERVICES.values()]))
async def show_service_card(message: Message):
    service_key = None
    for key, value in SERVICES.items():
        if f"{value['emoji']} {value['name']}" == message.text:
            service_key = key
            break
    
    if not service_key:
        return
    
    service = SERVICES[service_key]
    
    await db.save_action(
        message.from_user.id,
        "view_service",
        f"Просмотр услуги: {service['name']}"
    )
    
    caption = (
        f"{service['emoji']} <b>{service['name']}</b>\n\n"
        f"{service['description']}\n\n"
        f"💰 <b>Цена:</b> {service['price']} USD"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Подробнее", url=SITE_URL)],
        [InlineKeyboardButton(text="✍️ Заказать", callback_data=f"order_{service_key}")],
        [InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL)]
    ])
    
    await message.answer_photo(
        photo=service['photo'],
        caption=caption,
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("order_"))
async def order_from_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    service_key = callback.data.replace("order_", "")
    service = SERVICES.get(service_key)
    
    if not service:
        await callback.message.answer("❌ Услуга не найдена")
        return
    
    await db.save_action(
        callback.from_user.id,
        "order_started",
        f"Начало заказа: {service['name']}"
    )
    
    await state.update_data(service=service['name'])
    await state.set_state(OrderStates.name)
    
    await callback.message.answer(
        f"✅ Вы выбрали: <b>{service['name']}</b>\n"
        f"💰 Цена: {service['price']} USD\n\n"
        "Теперь укажите ваше имя:",
        reply_markup=get_cancel_keyboard()
    )

@dp.callback_query(F.data == "contact_manager")
async def contact_manager(callback: CallbackQuery):
    await callback.answer()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Главный бот", url=MAIN_BOT_URL)]
    ])
    await callback.message.answer(
        "📞 <b>Связаться с менеджером</b>\n\nНапишите нам в главном боте!",
        reply_markup=keyboard
    )

# ==================== ВЕБ-СЕРВЕР ====================
class WebServer:
    def __init__(self):
        self.app = web.Application()
        self.runner = None
        self.setup_routes()

    def setup_routes(self):
        self.app.router.add_get("/", self.handle_ping)
        self.app.router.add_get("/health", self.handle_health)

    async def handle_ping(self, request):
        return web.Response(text="OK", status=200)

    async def handle_health(self, request):
        return web.json_response({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "channel": CHANNEL_ID,
            "group": GROUP_ID,
            "currency": "USD",
            "main_bot": MAIN_BOT_URL,
            "site": SITE_URL
        })

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌐 Веб-сервер запущен на порту {port}")

    async def stop(self):
        if self.runner:
            await self.runner.shutdown()
            await self.runner.cleanup()

web_server = WebServer()

# ==================== ЗАПУСК ====================
async def main():
    try:
        logger.info("🚀 Запуск бота...")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"💬 Группа: {GROUP_ID}")
        logger.info("💰 Валюта: USD")
        logger.info(f"🤖 Главный бот: {MAIN_BOT_URL}")
        logger.info(f"🌐 Сайт: {SITE_URL}")
        logger.info(f"👤 Админ для уведомлений: {ADMIN_USERNAME}")
        
        await db.connect()
        await web_server.start()
        await bot.delete_webhook(drop_pending_updates=True)
        
        logger.info("🤖 Бот готов к работе!")
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("⚠️ Остановка...")
    finally:
        await web_server.stop()
        await db.close()
        await bot.session.close()
        logger.info("👋 Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())
