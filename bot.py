import asyncio
import os
import logging
import time
from datetime import datetime
from typing import Optional, Set, Dict, Any, List

import aiosqlite
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    TelegramObject
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

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

ADMIN_IDS: Set[int] = set()
raw_admin_ids = os.getenv("ADMIN_ID", "")
if raw_admin_ids:
    for part in raw_admin_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))

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
            except Exception as e:
                logger.error(f"Ошибка сохранения пользователя: {e}")
        return await handler(event, data)

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
dp.update.outer_middleware(UserTrackingMiddleware())

# ==================== КУРСЫ ПО БИЗНЕС-КОУЧИНГУ ====================
SERVICES = {
    "express_coaching": {
        "emoji": "⚡",
        "name": "Экспресс-разбор бизнеса",
        "price": "от $150",
        "description": "• 1.5 часа интенсивной работы 1-на-1\n• Поиск точек роста и устранение «узких мест»\n• Пошаговый план действий на 30 дней",
        "photo": "https://images.unsplash.com/photo-1552664730-d307ca884978?auto=format&fit=crop&w=800&q=80",
    },
    "group_mentorship": {
        "emoji": "🚀",
        "name": "Групповое наставничество",
        "price": "от $490/мес",
        "description": "• 2 месяца совместной работы в мини-группе\n• Еженедельные зум-разборы и домашние задания\n• Сильное окружение предпринимателей и нетворкинг",
        "photo": "https://images.unsplash.com/photo-1531482615713-2afd69097998?auto=format&fit=crop&w=800&q=80",
    },
    "vip_mentorship": {
        "emoji": "👑",
        "name": "VIP Личное Наставничество",
        "price": "от $1,500",
        "description": "• Полный консалтинг вашего бизнеса на 3 месяца\n• Построение отдела продаж, найм и автоворонки\n• Прямой доступ ко мне в Telegram 24/7",
        "photo": "https://images.unsplash.com/photo-1507679799987-c73779587ccf?auto=format&fit=crop&w=800&q=80",
    },
    "scaling_course": {
        "emoji": "📈",
        "name": "Курс «Бизнес-Масштаб 2.0»",
        "price": "от $290",
        "description": "• Онлайн-курс с предописанными видео-уроками\n• Шаблоны, таблицы финансового учета и регламенты\n• Сертификат о прохождении + чат поддержки",
        "photo": "https://images.unsplash.com/photo-1460925895917-afdab827c52f?auto=format&fit=crop&w=800&q=80",
    }
}

# ==================== СОСТОЯНИЯ FSM ====================
class PostStates(StatesGroup):
    waiting_for_photo = State()
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
            [KeyboardButton(text="⚡ Экспресс-разбор бизнеса")],
            [KeyboardButton(text="🚀 Групповое наставничество")],
            [KeyboardButton(text="👑 VIP Личное Наставничество")],
            [KeyboardButton(text="📈 Курс «Бизнес-Масштаб 2.0»")],
            [KeyboardButton(text="🌐 Наш сайт"), KeyboardButton(text="📝 Записаться на разбор")],
            [KeyboardButton(text="❓ FAQ & Ответы"), KeyboardButton(text="💬 Отзывы учеников")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите программу коучинга..."
    )

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True
    )

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ==================== ПРИВЕТСТВИЕ С КНОПКАМИ ====================
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    """Стартовая команда с богатым блоком кнопок"""
    await state.clear()
    
    welcome_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Официальный сайт", url=SITE_URL),
            InlineKeyboardButton(text="📢 Наш Telegram-канал", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")
        ],
        [
            InlineKeyboardButton(text="🎓 Обо мне / Кейсы", callback_data="about_coach"),
            InlineKeyboardButton(text="🎯 Бесплатный гайд", callback_data="free_guide")
        ],
        [
            InlineKeyboardButton(text="💬 Связаться лично", url="https://t.me/metaimperiya_support"),
            InlineKeyboardButton(text="📞 Заказать обратный звонок", callback_data="call_request")
        ],
        [
            InlineKeyboardButton(text="⭐️ Отзывы клиентов", callback_data="show_reviews"),
            InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")
        ]
    ])
    
    welcome_text = (
        f"🔥 <b>Приветствую, {message.from_user.first_name}!</b>\n\n"
        "Добро пожаловать в Академию Бизнес-Коучинга & Наставничества <b>MetaImperiya</b>! 🚀\n\n"
        "Мы помогаем предпринимателям и экспертам:\n"
        "• Увеличить доход в 2–5 раз без выгорания\n"
        "• Выстроить системный бизнес и делегировать рутину\n"
        "• Запустить продажи через автоворонки и личный бренд\n\n"
        "💎 <b>Выберите нужный раздел или программу ниже:</b>"
    )
    
    await message.answer(welcome_text, reply_markup=welcome_keyboard)
    await message.answer(
        "Вы также можете выбрать готовую программу коучинга из меню:",
        reply_markup=get_main_keyboard()
    )

# ==================== CALLBACK ДЛЯ ПРИВЕТСТВИЯ ====================
@dp.callback_query(F.data == "about_coach")
async def about_coach(callback: CallbackQuery):
    await callback.answer()
    text = (
        "🏆 <b>О Наставнике и Академии MetaImperiya</b>\n\n"
        "• Более 8 лет в бизнесе и цифровом маркетинге\n"
        "• Более 100+ успешных кейсов учеников с суммарным оборотом > $1M\n"
        "• Автор методик по системному росту и работе с мышлением\n\n"
        "👉 Наша цель — дать вам не просто теорию, а работающие инструменты и систему!"
    )
    await callback.message.answer(text)

@dp.callback_query(F.data == "free_guide")
async def free_guide(callback: CallbackQuery):
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать Гайд (PDF)", url=SITE_URL)]
    ])
    await callback.message.answer(
        "🎁 <b>Заберите ваш бонус!</b>\n\n"
        "Пошаговый чек-лист: <i>«5 шагов к выходу из операционки и удвоению чистой прибыли»</i>.",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "call_request")
async def call_request(callback: CallbackQuery):
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать ассистенту", url="https://t.me/metaimperiya_support")]
    ])
    await callback.message.answer(
        "📞 <b>Обратный звонок / Диагностика</b>\n\n"
        "Оставьте сообщение ассистенту, и мы подберем удобное время для бесплатной 15-минутной разбор-сессии!",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "show_reviews")
async def show_reviews(callback: CallbackQuery):
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Читать кейсы в канале", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")]
    ])
    await callback.message.answer(
        "⭐️ <b>Отзывы и результаты учеников</b>\n\n"
        "Все кейсы, аудио-отзывы и видео-интервью мы публикуем в нашем канале!",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "faq")
async def faq(callback: CallbackQuery):
    await callback.answer()
    faq_text = (
        "❓ <b>Часто задаваемые вопросы (FAQ)</b>\n\n"
        "🔹 <b>Подойдет ли мне коучинг, если я только начинаю?</b>\n"
        "Да! Для старта отлично подойдет «Экспресс-разбор» или «Курс 2.0».\n\n"
        "🔹 <b>В каком формате проходят занятия?</b>\n"
        "Все встречи проходят в Zoom 1-на-1 или в мини-группе. Все записи сохраняются.\n\n"
        "🔹 <b>Какая гарантия результата?</b>\n"
        "При полном выполнении всех домашних заданий и рекомендаций вы гарантированно окупаете стоимость программы."
    )
    await callback.message.answer(faq_text)

# ==================== ОФОРМЛЕНИЕ ЗАЯВОК (FSM) ====================
@dp.message(F.text == "📝 Записаться на разбор")
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderStates.service)
    await message.answer(
        "📝 <b>Запись на консультацию / коучинг</b>\n\nУкажите, какая программа или цель вас интересует:",
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
    await message.answer("👤 Как к вам обращаться (Ваше имя)?", reply_markup=get_cancel_keyboard())

@dp.message(OrderStates.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(OrderStates.contact)
    await message.answer("📱 Укажите ваш телефон или @username в Telegram:", reply_markup=get_cancel_keyboard())

@dp.message(OrderStates.contact)
async def process_contact(message: Message, state: FSMContext):
    await state.update_data(contact=message.text)
    await state.set_state(OrderStates.comment)
    await message.answer(
        "💬 Коротко опишите вашу текущую ситуацию в бизнесе (или /skip):",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(Command("skip"), OrderStates.comment)
async def skip_comment(message: Message, state: FSMContext):
    await state.update_data(comment="Не указано")
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
    except Exception as e:
        logger.error(f"❌ Ошибка БД: {e}")
        await message.answer("❌ Ошибка при сохранении заявки")
        await state.clear()
        return
    
    order_text = (
        "🚀 <b>НОВАЯ ЗАЯВКА НА КОУЧИНГ!</b>\n"
        f"🆔 №: {order_id}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"👤 Клиент: {data.get('name')}\n"
        f"🛠 Направление: {data.get('service')}\n"
        f"📞 Контакт: {data.get('contact')}\n"
        f"💬 Описание: {data.get('comment')}\n"
        f"🔗 @{message.from_user.username or 'нет'}"
    )
    
    try:
        await bot.send_message(chat_id=GROUP_ID, text=order_text)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в группу: {e}")
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=order_text)
        except Exception as e:
            logger.error(f"❌ Ошибка админу {admin_id}: {e}")
    
    await message.answer(
        "✅ <b>Заявка успешно принята!</b>\nМы свяжемся с вами в течение 15 минут для уточнения деталей.",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

# ==================== ОБРАБОТЧИКИ КУРСОВ И УСЛУГ ====================
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
    caption = (
        f"{service['emoji']} <b>{service['name']}</b>\n\n"
        f"{service['description']}\n\n"
        f"💰 <b>Инвестиция в рост:</b> {service['price']}"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Забронировать место", callback_data=f"order_{service_key}")],
        [InlineKeyboardButton(text="💬 Задать вопрос", url="https://t.me/metaimperiya_support")]
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
    
    await state.update_data(service=service['name'])
    await state.set_state(OrderStates.name)
    
    await callback.message.answer(
        f"✅ Вы выбрали: <b>{service['name']}</b>\n"
        f"💰 Стоимость: {service['price']}\n\n"
        "Укажите ваше имя:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(F.text == "🌐 Наш сайт")
async def site_link(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на сайт", url=SITE_URL)]
    ])
    await message.answer("👉 Посетите наш официальный сайт:", reply_markup=keyboard)

@dp.message(F.text == "💬 Отзывы учеников")
async def reviews_text(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Смотреть кейсы", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")]
    ])
    await message.answer("Все кейсы и отзывы опубликованы в нашем Telegram-канале:", reply_markup=keyboard)

@dp.message(F.text == "❓ FAQ & Ответы")
async def faq_menu(message: Message):
    await faq(CallbackQuery(id="", from_user=message.from_user, chat_instance="", message=message, data=""))

# ==================== ВЕБ-СЕРВЕР И КЕЕP-ALIVE (БУДИЛЬНИК) ====================
class WebServer:
    def __init__(self):
        self.app = web.Application()
        self.runner = None
        self.setup_routes()

    def setup_routes(self):
        self.app.router.add_get("/", self.handle_ping)
        self.app.router.add_get("/health", self.handle_health)

    async def handle_ping(self, request):
        return web.Response(text="Bot and Keep-Alive Server are running!", status=200)

    async def handle_health(self, request):
        return web.json_response({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "channel": CHANNEL_ID,
            "group": GROUP_ID
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

async def keep_alive_task():
    """Фоновый цикл-будильник: каждые 10 минут отправляет запрос сам на себя"""
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if SITE_URL:
                    async with session.get(SITE_URL) as resp:
                        logger.info(f"⏰ Keep-alive ping sent to {SITE_URL}, status: {resp.status}")
            except Exception as e:
                logger.error(f"❌ Keep-alive error: {e}")
            await asyncio.sleep(600)  # 10 минут

# ==================== ЗАПУСК ====================
async def main():
    try:
        logger.info("🚀 Запуск бота бизнес-коучинга...")
        
        await db.connect()
        await web_server.start()
        await bot.delete_webhook(drop_pending_updates=True)
        
        # Запускаем фоновый «будильник»
        asyncio.create_task(keep_alive_task())
        
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
