import os
import re
import uuid
import logging
import asyncio
import asyncpg
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
from dotenv import load_dotenv
from aiohttp import web
import aiohttp

# تنظیمات محیطی
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
WEBHOOK_PATH = f"/{BOT_TOKEN}"
PORT = int(os.getenv('PORT', 8080))

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# کیبوردهای اصلی
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📁 ساخت دسته جدید", "📂 نمایش دسته‌ها"],
        ["📤 آپلود فایل", "⏱ تایمر خودکار"],
        ["📢 تنظیم کانال اجباری", "👤 مدیریت ادمین‌ها"]
    ],
    resize_keyboard=True,
    input_field_placeholder="لطفا یک گزینه را انتخاب کنید"
)

USER_MENU = ReplyKeyboardMarkup(
    [["🔍 جستجوی فایل‌ها", "❓ راهنما"]],
    resize_keyboard=True
)

BACK_MENU = ReplyKeyboardMarkup(
    [["↩️ بازگشت به منوی اصلی"]],
    resize_keyboard=True
)

# حالت‌های گفتگو
(
    UPLOADING, WAITING_CHANNEL_INFO, AWAITING_CATEGORY_NAME,
    CATEGORY_MANAGEMENT, TIMER_SETTINGS, POST_MESSAGE_SETUP,
    AWAITING_POST_MESSAGE, AWAITING_POST_CAPTION
) = range(8)

class Database:
    """مدیریت دیتابیس PostgreSQL"""
    
    def __init__(self):
        self.pool = None

    async def connect(self):
        """اتصال به دیتابیس"""
        self.pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'))
        await self.init_db()
    
    async def init_db(self):
        """ایجاد جداول مورد نیاز"""
        async with self.pool.acquire() as conn:
            # جدول دسته‌ها
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS categories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # جدول فایل‌ها
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    id SERIAL PRIMARY KEY,
                    category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    file_size BIGINT NOT NULL,
                    file_type TEXT NOT NULL,
                    caption TEXT,
                    upload_date TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # جدول کانال‌ها
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL UNIQUE,
                    channel_name TEXT NOT NULL,
                    invite_link TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # جدول تایمر خودکار
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS auto_delete_settings (
                    id SERIAL PRIMARY KEY,
                    is_active BOOLEAN DEFAULT FALSE,
                    delete_after_seconds INTEGER,
                    post_delete_message TEXT DEFAULT '⏰ زمان مشاهده فایل به پایان رسید!'
                )
            ''')
            
            # جدول ادمین‌ها
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY,
                    is_super BOOLEAN NOT NULL DEFAULT FALSE,
                    added_by BIGINT,
                    added_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # جدول پیام‌های پس از ارسال
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS post_messages (
                    category_id TEXT PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    caption TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # ایندکس‌های بهینه‌سازی
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_files_category ON files(category_id)')
            logger.info("Database initialized")

    # --- مدیریت دسته‌ها ---
    async def add_category(self, name: str, created_by: int) -> str:
        """ایجاد دسته جدید"""
        category_id = str(uuid.uuid4())[:8]
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO categories(id, name, created_by) VALUES($1, $2, $3)",
                category_id, name, created_by
            )
        return category_id
    
    async def get_categories(self) -> dict:
        """دریافت تمام دسته‌ها"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name FROM categories")
            return {row['id']: row['name'] for row in rows}
    
    async def get_category(self, category_id: str) -> dict:
        """دریافت اطلاعات یک دسته"""
        async with self.pool.acquire() as conn:
            category = await conn.fetchrow(
                "SELECT name, created_by FROM categories WHERE id = $1", category_id
            )
            if not category:
                return None
                
            files = await conn.fetch(
                "SELECT file_id, file_type, caption FROM files WHERE category_id = $1", category_id
            )
            
            # دریافت پیام پس از ارسال
            post_message = await conn.fetchrow(
                "SELECT message_type, content, caption FROM post_messages WHERE category_id = $1", 
                category_id
            )
            
            return {
                'name': category['name'],
                'files': [dict(file) for file in files],
                'post_message': dict(post_message) if post_message else None
            }
            
    async def delete_category(self, category_id: str) -> bool:
        """حذف دسته"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM categories WHERE id = $1", category_id
            )
            return result.split()[-1] == '1'

    # --- مدیریت فایل‌ها ---
    async def add_file(self, category_id: str, file_info: dict) -> bool:
        """افزودن فایل به دسته"""
        async with self.pool.acquire() as conn:
            try:
                # بررسی تکراری نبودن فایل
                exists = await conn.fetchval(
                    "SELECT 1 FROM files WHERE file_id = $1", 
                    file_info['file_id']
                )
                if exists:
                    return False
                
                await conn.execute(
                    "INSERT INTO files(category_id, file_id, file_name, file_size, file_type, caption) "
                    "VALUES($1, $2, $3, $4, $5, $6)",
                    category_id,
                    file_info['file_id'],
                    file_info['file_name'],
                    file_info['file_size'],
                    file_info['file_type'],
                    file_info.get('caption', '')
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
    
    async def add_files(self, category_id: str, files: list) -> int:
        async with self.pool.acquire() as conn:
            inserted_count = 0
            for f in files:
                try:
                    # بررسی تکراری نبودن فایل
                    exists = await conn.fetchval(
                        "SELECT 1 FROM files WHERE file_id = $1", 
                        f['file_id']
                    )
                    if exists:
                        continue
                    
                    await conn.execute(
                        "INSERT INTO files(category_id, file_id, file_name, file_size, file_type, caption) "
                        "VALUES($1, $2, $3, $4, $5, $6)",
                        category_id,
                        f['file_id'],
                        f['file_name'],
                        f['file_size'],
                        f['file_type'],
                        f.get('caption', '')
                    )
                    inserted_count += 1
                except asyncpg.UniqueViolationError:
                    continue
            return inserted_count

    # --- مدیریت کانال‌ها ---
    async def add_channel(self, channel_id: str, name: str, link: str) -> bool:
        """افزودن کانال اجباری"""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO channels(channel_id, channel_name, invite_link) VALUES($1, $2, $3)",
                    channel_id, name, link
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
    
    async def get_channels(self) -> list:
        """دریافت لیست کانال‌ها"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT channel_id, channel_name, invite_link FROM channels")
    
    async def delete_channel(self, channel_id: str) -> bool:
        """حذف کانال"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM channels WHERE channel_id = $1", channel_id
            )
            return result.split()[-1] == '1'

    # --- مدیریت تایمر خودکار ---
    async def get_timer_settings(self):
        """دریافت تنظیمات تایمر"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM auto_delete_settings LIMIT 1")
    
    async def update_timer_settings(self, is_active: bool, delete_after: int = None, message: str = None):
        """به‌روزرسانی تنظیمات تایمر"""
        async with self.pool.acquire() as conn:
            settings = await self.get_timer_settings()
            
            if settings:
                # به‌روزرسانی تنظیمات موجود
                await conn.execute(
                    "UPDATE auto_delete_settings SET "
                    "is_active = $1, "
                    "delete_after_seconds = COALESCE($2, delete_after_seconds), "
                    "post_delete_message = COALESCE($3, post_delete_message) "
                    "WHERE id = $4",
                    is_active,
                    delete_after,
                    message,
                    settings['id']
                )
            else:
                # ایجاد تنظیمات جدید
                await conn.execute(
                    "INSERT INTO auto_delete_settings(is_active, delete_after_seconds, post_delete_message) "
                    "VALUES($1, $2, $3)",
                    is_active,
                    delete_after,
                    message
                )

    # --- مدیریت ادمین‌ها ---
    async def add_admin(self, user_id: int, is_super: bool, added_by: int):
        """افزودن ادمین جدید"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO admins(user_id, is_super, added_by) "
                "VALUES($1, $2, $3) ON CONFLICT (user_id) DO UPDATE "
                "SET is_super = EXCLUDED.is_super",
                user_id, is_super, added_by
            )
    
    async def remove_admin(self, user_id: int):
        """حذف ادمین"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM admins WHERE user_id = $1 AND is_super = FALSE",
                user_id
            )
    
    async def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM admins WHERE user_id = $1)",
                user_id
            )
    
    # --- مدیریت پیام‌های پس از ارسال ---
    async def set_post_message(self, category_id: str, message_type: str, content: str, caption: str = None):
        """تنظیم پیام پس از ارسال برای دسته"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO post_messages(category_id, message_type, content, caption) "
                "VALUES($1, $2, $3, $4) "
                "ON CONFLICT (category_id) DO UPDATE SET "
                "message_type = EXCLUDED.message_type, "
                "content = EXCLUDED.content, "
                "caption = EXCLUDED.caption",
                category_id, message_type, content, caption
            )
    
    async def delete_post_message(self, category_id: str):
        """حذف پیام پس از ارسال"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM post_messages WHERE category_id = $1",
                category_id
            )

class BotManager:
    """مدیریت اصلی ربات"""
    
    def __init__(self):
        self.db = Database()
        self.bot_username = None
        self.user_data = {}
    
    async def init(self, bot_username: str):
        """راه‌اندازی اولیه"""
        self.bot_username = bot_username
        await self.db.connect()
        
        # افزودن ادمین‌های اولیه از متغیر محیطی
        for admin_id in ADMIN_IDS:
            await self.db.add_admin(admin_id, True, 0)
    
    async def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        return await self.db.is_admin(user_id)
    
    def generate_link(self, category_id: str) -> str:
        """تولید لینک دسته با یوزرنیم صحیح"""
        if self.bot_username:
            return f"https://t.me/{self.bot_username}?start=cat_{category_id}"
        # Fallback در صورت عدم وجود یوزرنیم
        bot_id = BOT_TOKEN.split(':')[0]
        return f"https://t.me/{bot_id}?start=cat_{category_id}"
    
    def extract_file_info(self, update: Update) -> dict:
        """استخراج اطلاعات فایل از پیام"""
        msg = update.message

        if msg.document:
            file = msg.document
            file_type = 'document'
            file_name = file.file_name or f"document_{file.file_id[:8]}"
        elif msg.photo:
            file = msg.photo[-1]  # بالاترین کیفیت
            file_type = 'photo'
            file_name = f"photo_{file.file_id[:8]}.jpg"
        elif msg.video:
            file = msg.video
            file_type = 'video'
            file_name = f"video_{file.file_id[:8]}.mp4"
        elif msg.audio:
            file = msg.audio
            file_type = 'audio'
            file_name = f"audio_{file.file_id[:8]}.mp3"
        else:
            return None

        return {
            'file_id': file.file_id,
            'file_name': file_name,
            'file_size': file.file_size,
            'file_type': file_type,
            'caption': msg.caption or ''
        }
    
    async def check_channel_membership(self, user_id: int, channel_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """بررسی عضویت کاربر در کانال"""
        for _ in range(3):  # 3 بار تلاش
            try:
                member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
                if member.status in ['member', 'administrator', 'creator']:
                    return True
            except Exception as e:
                logger.warning(f"خطا در بررسی عضویت: {e}")
            
            await asyncio.sleep(2)  # تاخیر 2 ثانیه‌ای بین هر تلاش
        
        return False
    
    async def send_post_message(self, message: Message, context: ContextTypes.DEFAULT_TYPE, post_message: dict):
        """ارسال پیام پس از ارسال فایل‌ها"""
        if not post_message:
            return
        
        msg_type = post_message['message_type']
        content = post_message['content']
        caption = post_message.get('caption', '')
        
        try:
            if msg_type == 'text':
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=content,
                    reply_to_message_id=message.message_id
                )
            elif msg_type == 'photo':
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=content,
                    caption=caption,
                    reply_to_message_id=message.message_id
                )
            elif msg_type == 'video':
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    video=content,
                    caption=caption,
                    reply_to_message_id=message.message_id
                )
            elif msg_type == 'document':
                await context.bot.send_document(
                    chat_id=message.chat_id,
                    document=content,
                    caption=caption,
                    reply_to_message_id=message.message_id
                )
        except Exception as e:
            logger.error(f"خطا در ارسال پیام پس از ارسال: {e}")

# ایجاد نمونه
bot_manager = BotManager()

# ========================
# ==== HANDLER FUNCTIONS ===
# ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور شروع برای ادمین‌ها"""
    user_id = update.effective_user.id
    
    if await bot_manager.is_admin(user_id):
        await update.message.reply_text(
            "👋 سلام ادمین!\n\n"
            "از منوی زیر انتخاب کنید:",
            reply_markup=MAIN_MENU
        )
    else:
        await user_start(update, context)

async def user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور شروع برای کاربران عادی"""
    user_id = update.effective_user.id
    
    # دسترسی از طریق لینک دسته
    if context.args and context.args[0].startswith('cat_'):
        category_id = context.args[0][4:]
        await handle_category(update, context, category_id)
        return
    
    await update.message.reply_text(
        "👋 سلام! برای دریافت فایل‌ها از لینک‌ها استفاده کنید.",
        reply_markup=USER_MENU
    )

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """مدیریت دسترسی به دسته"""
    # استخراج user_id و message بسته به نوع update
    if update.message:
        user_id = update.message.from_user.id
        message = update.message
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
        message = update.callback_query.message
    else:
        logger.error("Unsupported update type")
        return

    # بررسی ادمین
    if await bot_manager.is_admin(user_id):
        await admin_category_menu(message, category_id)
        return
    
    # بررسی عضویت در کانال‌ها
    channels = await bot_manager.db.get_channels()
    if not channels:
        await send_category_files(message, context, category_id)
        return
    
    non_joined = []
    for channel in channels:
        is_member = await bot_manager.check_channel_membership(user_id, channel['channel_id'], context)
        if not is_member:
            non_joined.append(channel)
    
    if not non_joined:
        await send_category_files(message, context, category_id)
        return
    
    # ایجاد صفحه عضویت
    keyboard = []
    for channel in non_joined:
        button = InlineKeyboardButton(
            text=f"📢 {channel['channel_name']}",
            url=channel['invite_link']
        )
        keyboard.append([button])
    
    keyboard.append([
        InlineKeyboardButton(
            "✅ عضو شدم", 
            callback_data=f"check_{category_id}"
        )
    ])
    
    await message.reply_text(
        "⚠️ برای دسترسی ابتدا در کانال‌های زیر عضو شوید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_category_menu(message: Message, category_id: str):
    """منوی مدیریت دسته برای ادمین"""
    try:
        category = await bot_manager.db.get_category(category_id)
        if not category:
            await message.reply_text("❌ دسته یافت نشد!")
            return
        
        # بررسی وجود پیام پس از ارسال
        has_post_message = "✅" if category['post_message'] else "❌"
        
        keyboard = [
            [InlineKeyboardButton("📁 مشاهده فایل‌ها", callback_data=f"view_{category_id}")],
            [InlineKeyboardButton("➕ افزودن فایل", callback_data=f"add_{category_id}")],
            [InlineKeyboardButton("💬 پیام پس از ارسال", callback_data=f"postmsg_{category_id}")],
            [InlineKeyboardButton("🗑 حذف دسته", callback_data=f"delcat_{category_id}")]
        ]
        
        await message.reply_text(
            f"📂 دسته: {category['name']}\n"
            f"📦 تعداد فایل‌ها: {len(category['files'])}\n"
            f"💬 پیام پس از ارسال: {has_post_message}\n\n"
            "لطفا عملیات مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"خطا در منوی ادمین: {e}")
        await message.reply_text("❌ خطایی در نمایش منو رخ داد")

async def send_category_files(message: Message, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """ارسال فایل‌های یک دسته"""
    try:
        chat_id = message.chat_id
        
        category = await bot_manager.db.get_category(category_id)
        if not category or not category['files']:
            await message.reply_text("❌ فایلی برای نمایش وجود ندارد!")
            return
        
        await message.reply_text(f"📤 ارسال فایل‌های '{category['name']}'...")
        
        # ارسال فایل‌ها
        for file in category['files']:
            try:
                send_func = {
                    'document': context.bot.send_document,
                    'photo': context.bot.send_photo,
                    'video': context.bot.send_video,
                    'audio': context.bot.send_audio
                }.get(file['file_type'])
                
                if send_func:
                    await send_func(
                        chat_id=chat_id,
                        **{file['file_type']: file['file_id']},
                        caption=file.get('caption', '')[:1024]
                    )
                await asyncio.sleep(0.5)  # افزایش تاخیر برای جلوگیری از محدودیت
            except Exception as e:
                logger.error(f"ارسال فایل خطا: {e}")
                await asyncio.sleep(2)
        
        # ارسال پیام پس از ارسال فایل‌ها
        if category.get('post_message'):
            await bot_manager.send_post_message(message, context, category['post_message'])
    except Exception as e:
        logger.error(f"خطا در ارسال فایل‌ها: {e}")
        await message.reply_text("❌ خطایی در ارسال فایل‌ها رخ داد")

# ========================
# ==== ADMIN COMMANDS ====
# ========================

async def new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ایجاد دسته جدید"""
    user_id = update.effective_user.id
    if not await bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    await update.message.reply_text(
        "لطفاً نام دسته جدید را وارد کنید:",
        reply_markup=BACK_MENU
    )
    return AWAITING_CATEGORY_NAME

async def save_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    user_id = update.effective_user.id
    category_id = await bot_manager.db.add_category(name, user_id)
    link = bot_manager.generate_link(category_id)
    
    await update.message.reply_text(
        f"✅ دسته «{name}» با موفقیت ایجاد شد.\n\n"
        f"🔗 لینک دسته:\n{link}",
        reply_markup=MAIN_MENU
    )
    return ConversationHandler.END

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع آپلود فایل"""
    user_id = update.effective_user.id
    if not await bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        # نمایش دسته‌های موجود برای انتخاب
        categories = await bot_manager.db.get_categories()
        if not categories:
            await update.message.reply_text("❌ هیچ دسته‌ای وجود ندارد! ابتدا یک دسته ایجاد کنید.")
            return
        
        keyboard = []
        for cid, name in categories.items():
            keyboard.append([InlineKeyboardButton(
                f"📁 {name} (ID: {cid})", 
                callback_data=f"upload_cat_{cid}"
            )])
        
        await update.message.reply_text(
            "لطفاً دسته مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    category_id = context.args[0]
    category = await bot_manager.db.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته یافت نشد!")
        return
    
    # ذخیره اطلاعات آپلود
    context.user_data['upload'] = {
        'category_id': category_id,
        'files': []
    }
    
    await update.message.reply_text(
        f"📤 حالت آپلود فعال شد! فایل‌ها را ارسال کنید.\n"
        f"برای پایان: /finish_upload\n"
        f"برای لغو: /cancel"
    )
    return UPLOADING

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش فایل‌های ارسالی"""
    user_id = update.effective_user.id
    if 'upload' not in context.user_data:
        return
    
    file_info = bot_manager.extract_file_info(update)
    if not file_info:
        await update.message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود!")
        return
    
    upload = context.user_data['upload']
    upload['files'].append(file_info)
    
    await update.message.reply_text(f"✅ فایل دریافت شد! (تعداد: {len(upload['files'])})")

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پایان آپلود فایل‌ها"""
    user_id = update.effective_user.id
    if 'upload' not in context.user_data:
        await update.message.reply_text("❌ هیچ آپلودی فعال نیست!")
        return ConversationHandler.END
    
    upload = context.user_data.pop('upload')
    if not upload['files']:
        await update.message.reply_text("❌ فایلی دریافت نشد!")
        return ConversationHandler.END
    
    count = await bot_manager.db.add_files(upload['category_id'], upload['files'])
    link = bot_manager.generate_link(upload['category_id'])
    
    await update.message.reply_text(
        f"✅ {count} فایل با موفقیت ذخیره شد!\n\n"
        f"🔗 لینک دسته:\n{link}",
        reply_markup=MAIN_MENU
    )
    return ConversationHandler.END

async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست دسته‌ها"""
    if not await bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    categories = await bot_manager.db.get_categories()
    if not categories:
        await update.message.reply_text("📂 هیچ دسته‌ای وجود ندارد!")
        return
    
    message = "📁 لیست دسته‌ها:\n\n"
    for cid, name in categories.items():
        message += f"• {name} [ID: {cid}]\n"
        message += f"  لینک: {bot_manager.generate_link(cid)}\n\n"
    
    await update.message.reply_text(message, reply_markup=MAIN_MENU)

# ========================
# === CHANNEL MANAGEMENT ==
# ========================

async def channel_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کانال‌های اجباری"""
    if not await bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    keyboard = [
        [InlineKeyboardButton("➕ اضافه کردن کانال", callback_data="add_channel")],
        [InlineKeyboardButton("➖ حذف کانال", callback_data="remove_channel")],
        [InlineKeyboardButton("👁️ مشاهده کانال‌ها", callback_data="list_channels")]
    ]
    
    await update.message.reply_text(
        "مدیریت کانال‌های اجباری:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع افزودن کانال"""
    if not await bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    await update.message.reply_text(
        "لطفا آیدی کانال را ارسال کنید (مثال: -1001234567890):\n\n"
        "⚠️ توجه: ربات باید ادمین کانال باشد!",
        reply_markup=BACK_MENU
    )
    return WAITING_CHANNEL_INFO

async def handle_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش آیدی کانال"""
    channel_id = update.message.text.strip()
    
    # اعتبارسنجی آیدی کانال
    if not re.match(r'^-100\d+$', channel_id):
        await update.message.reply_text(
            "❌ فرمت آیدی نامعتبر!\n"
            "لطفاً آیدی را به فرمت '-1001234567890' وارد کنید:",
            reply_markup=BACK_MENU
        )
        return WAITING_CHANNEL_INFO
    
    context.user_data['channel_id'] = channel_id
    await update.message.reply_text(
        "✅ آیدی کانال معتبر است.\n\n"
        "لطفاً نام کانال را وارد کنید:",
        reply_markup=BACK_MENU
    )
    return WAITING_CHANNEL_INFO

async def handle_channel_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش نام کانال"""
    name = update.message.text.strip()
    context.user_data['channel_name'] = name
    await update.message.reply_text(
        "✅ نام کانال ذخیره شد.\n\n"
        "لطفاً لینک دعوت به کانال را وارد کنید:",
        reply_markup=BACK_MENU
    )
    return WAITING_CHANNEL_INFO

async def handle_channel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش لینک دعوت و ذخیره نهایی"""
    link = update.message.text.strip()
    
    # اعتبارسنجی لینک دعوت
    if not re.match(r'^https?://t\.me/[\w-]+(/[\w-]+)?$', link):
        await update.message.reply_text(
            "❌ فرمت لینک نامعتبر!\n"
            "لطفاً لینک معتبر تلگرام وارد کنید (مثال: https://t.me/joinchat/ABC123):",
            reply_markup=BACK_MENU
        )
        return WAITING_CHANNEL_INFO
    
    # ذخیره در دیتابیس
    channel_id = context.user_data['channel_id']
    name = context.user_data['channel_name']
    
    try:
        success = await bot_manager.db.add_channel(channel_id, name, link)
        if success:
            await update.message.reply_text(
                f"✅ کانال «{name}» با موفقیت افزوده شد!",
                reply_markup=MAIN_MENU
            )
        else:
            await update.message.reply_text(
                "❌ خطا در افزودن کانال (احتمالاً تکراری است)!",
                reply_markup=MAIN_MENU
            )
    except Exception as e:
        logger.error(f"Error adding channel: {e}")
        await update.message.reply_text(
            "❌ خطای سیستمی در افزودن کانال!",
            reply_markup=MAIN_MENU
        )
    
    # پاکسازی داده‌های موقت
    context.user_data.pop('channel_id', None)
    context.user_data.pop('channel_name', None)
    
    return ConversationHandler.END

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست کانال‌ها"""
    if not await bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    channels = await bot_manager.db.get_channels()
    if not channels:
        await update.message.reply_text("📢 هیچ کانالی ثبت نشده است!", reply_markup=MAIN_MENU)
        return
    
    message = "📢 کانال‌های اجباری:\n\n"
    for i, ch in enumerate(channels, 1):
        message += (
            f"{i}. {ch['channel_name']}\n"
            f"   آیدی: {ch['channel_id']}\n"
            f"   لینک: {ch['invite_link']}\n\n"
        )
    
    await update.message.reply_text(message, reply_markup=MAIN_MENU)

# ========================
# === POST MESSAGE SYSTEM =
# ========================

async def setup_post_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنظیم پیام پس از ارسال"""
    query = update.callback_query
    await query.answer()
    
    category_id = query.data.split('_')[1]
    context.user_data['post_message'] = {'category_id': category_id}
    
    keyboard = [
        [InlineKeyboardButton("📝 متن", callback_data="post_text")],
        [InlineKeyboardButton("🖼 عکس", callback_data="post_photo")],
        [InlineKeyboardButton("🎥 ویدیو", callback_data="post_video")],
        [InlineKeyboardButton("📄 سند", callback_data="post_document")],
        [InlineKeyboardButton("🗑 حذف پیام", callback_data=f"delpost_{category_id}")]
    ]
    
    await query.edit_message_text(
        "لطفاً نوع پیام پس از ارسال را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return POST_MESSAGE_SETUP

async def handle_post_message_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش نوع پیام پس از ارسال"""
    query = update.callback_query
    await query.answer()
    
    action = query.data
    context.user_data['post_message']['type'] = action.split('_')[1]
    
    if action == 'delpost':
        category_id = query.data.split('_')[1]
        await bot_manager.db.delete_post_message(category_id)
        await query.edit_message_text(
            "✅ پیام پس از ارسال حذف شد!",
            reply_markup=MAIN_MENU
        )
        return ConversationHandler.END
    
    if action == 'post_text':
        await query.edit_message_text(
            "لطفاً متن پیام را ارسال کنید:",
            reply_markup=BACK_MENU
        )
        return AWAITING_POST_MESSAGE
    
    # برای مدیاها نیاز به ارسال فایل داریم
    await query.edit_message_text(
        f"لطفاً {'عکس' if 'photo' in action else 'ویدیو' if 'video' in action else 'سند'} را ارسال کنید:",
        reply_markup=BACK_MENU
    )
    return AWAITING_POST_MESSAGE

async def save_post_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ذخیره پیام پس از ارسال"""
    post_data = context.user_data.get('post_message')
    if not post_data:
        await update.message.reply_text("❌ خطا در پردازش پیام!")
        return ConversationHandler.END
    
    msg_type = post_data['type']
    category_id = post_data['category_id']
    
    try:
        if msg_type == 'text':
            content = update.message.text
            await bot_manager.db.set_post_message(category_id, 'text', content)
            await update.message.reply_text(
                "✅ متن پیام پس از ارسال ذخیره شد!",
                reply_markup=MAIN_MENU
            )
        else:
            # استخراج فایل بر اساس نوع
            if msg_type == 'photo':
                file_id = update.message.photo[-1].file_id
            elif msg_type == 'video':
                file_id = update.message.video.file_id
            elif msg_type == 'document':
                file_id = update.message.document.file_id
            else:
                raise ValueError("نوع پیام نامعتبر")
            
            # ذخیره کپشن اگر وجود دارد
            caption = update.message.caption or ""
            
            await bot_manager.db.set_post_message(category_id, msg_type, file_id, caption)
            
            await update.message.reply_text(
                f"✅ {'عکس' if msg_type == 'photo' else 'ویدیو' if msg_type == 'video' else 'سند'} پیام پس از ارسال ذخیره شد!",
                reply_markup=MAIN_MENU
            )
    except Exception as e:
        logger.error(f"خطا در ذخیره پیام پس از ارسال: {e}")
        await update.message.reply_text(
            "❌ خطا در ذخیره پیام! لطفاً مجدداً تلاش کنید.",
            reply_markup=MAIN_MENU
        )
    
    # پاکسازی داده‌های موقت
    context.user_data.pop('post_message', None)
    return ConversationHandler.END

# ========================
# ==== UTILITY HANDLERS ===
# ========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو عملیات جاری"""
    # پاکسازی داده‌های موقت
    context.user_data.pop('upload', None)
    context.user_data.pop('channel_id', None)
    context.user_data.pop('channel_name', None)
    context.user_data.pop('post_message', None)
    
    await update.message.reply_text("❌ عملیات لغو شد.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه‌ها"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # بررسی عضویت در کانال‌ها
    if data.startswith('check_'):
        category_id = data[6:]
        user_id = query.from_user.id
        
        # بررسی مجدد عضویت
        channels = await bot_manager.db.get_channels()
        non_joined = []
        for channel in channels:
            is_member = await bot_manager.check_channel_membership(user_id, channel['channel_id'], context)
            if not is_member:
                non_joined.append(channel)
        
        if non_joined:
            # هنوز در برخی کانال‌ها عضو نیست
            keyboard = []
            for channel in non_joined:
                button = InlineKeyboardButton(
                    text=f"📢 {channel['channel_name']}",
                    url=channel['invite_link']
                )
                keyboard.append([button])
            
            keyboard.append([
                InlineKeyboardButton(
                    "✅ عضو شدم", 
                    callback_data=f"check_{category_id}"
                )
            ])
            
            await query.edit_message_text(
                "⚠️ هنوز در کانال‌های زیر عضو نشده‌اید:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            # حالا عضو شده است
            await query.edit_message_text("✅ عضویت شما تأیید شد! در حال آماده‌سازی فایل‌ها...")
            await send_category_files(query.message, context, category_id)
        return
    
    # دستورات ادمین
    user_id = query.from_user.id
    if not await bot_manager.is_admin(user_id):
        await query.edit_message_text("❌ دسترسی ممنوع!")
        return
    
    if data.startswith('view_'):
        category_id = data[5:]
        await send_category_files(query.message, context, category_id)
    
    elif data.startswith('add_'):
        category_id = data[4:]
        context.user_data['upload'] = {
            'category_id': category_id,
            'files': []
        }
        await query.edit_message_text(
            "📤 فایل‌ها را ارسال کنید.\n"
            "برای پایان: /finish_upload\n"
            "برای لغو: /cancel")
        return UPLOADING
    
    elif data.startswith('delcat_'):
        category_id = data[7:]
        success = await bot_manager.db.delete_category(category_id)
        if success:
            await query.edit_message_text("✅ دسته با موفقیت حذف شد!")
        else:
            await query.edit_message_text("❌ خطا در حذف دسته!")
    
    elif data.startswith('postmsg_'):
        await setup_post_message(update, context)
    
    elif data.startswith('delpost_'):
        category_id = data[8:]
        await bot_manager.db.delete_post_message(category_id)
        await query.edit_message_text("✅ پیام پس از ارسال حذف شد!")
    
    elif data in ['post_text', 'post_photo', 'post_video', 'post_document']:
        await handle_post_message_type(update, context)
    
    # مدیریت کانال‌ها
    elif data == "add_channel":
        await start_add_channel(update, context)
    
    elif data == "remove_channel":
        channels = await bot_manager.db.get_channels()
        if not channels:
            await query.edit_message_text("📢 هیچ کانالی ثبت نشده است!")
            return
        
        keyboard = []
        for channel in channels:
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ {channel['channel_name']}",
                    callback_data=f"delchan_{channel['channel_id']}"
                )
            ])
        
        await query.edit_message_text(
            "کانال مورد نظر برای حذف را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "list_channels":
        await list_channels(query.message)
    
    elif data.startswith('delchan_'):
        channel_id = data[8:]
        success = await bot_manager.db.delete_channel(channel_id)
        if success:
            await query.edit_message_text("✅ کانال با موفقیت حذف شد!")
        else:
            await query.edit_message_text("❌ خطا در حذف کانال!")

# ========================
# === TIMER MANAGEMENT ===
# ========================

async def timer_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت تایمر خودکار"""
    if not await bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    settings = await bot_manager.db.get_timer_settings()
    status = "فعال ✅" if settings and settings['is_active'] else "غیرفعال ❌"
    interval = settings['delete_after_seconds'] if settings else "تنظیم نشده"
    message = settings['post_delete_message'] if settings else "تنظیم نشده"
    
    keyboard = [
        [InlineKeyboardButton(f"⏱ وضعیت: {status}", callback_data="toggle_timer")],
        [InlineKeyboardButton(f"🕒 تنظیم زمان ({interval})", callback_data="set_timer_interval")],
        [InlineKeyboardButton("✏️ ویرایش پیام", callback_data="edit_timer_message")]
    ]
    
    await update.message.reply_text(
        f"مدیریت تایمر خودکار:\n\n"
        f"• وضعیت: {status}\n"
        f"• زمان حذف: {interval} ثانیه\n"
        f"• پیام: {message}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def toggle_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تغییر وضعیت تایمر"""
    query = update.callback_query
    await query.answer()
    
    settings = await bot_manager.db.get_timer_settings()
    new_status = not settings['is_active'] if settings else True
    
    await bot_manager.db.update_timer_settings(new_status)
    
    status = "فعال ✅" if new_status else "غیرفعال ❌"
    await query.edit_message_text(
        f"✅ وضعیت تایمر به «{status}» تغییر یافت",
        reply_markup=MAIN_MENU
    )

# ========================
# === WEB SERVER SETUP ===
# ========================

async def health_check(request):
    """صفحه سلامت برای بررسی وضعیت ربات"""
    return web.Response(text="🤖 Telegram Bot is Running!")

async def keep_alive():
    """ارسال درخواست به health endpoint هر 5 دقیقه"""
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(WEBHOOK_URL + "/health") as resp:
                    if resp.status == 200:
                        logger.info("✅ Keep-alive ping sent successfully")
                    else:
                        logger.warning(f"⚠️ Keep-alive failed: {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Keep-alive exception: {e}")
        
        await asyncio.sleep(300)  # هر 5 دقیقه

async def webhook_handler(request):
    """مدیریت درخواست‌های Webhook"""
    application = request.app['bot_application']
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response()

async def setup_bot():
    """تنظیم و اجرای ربات با Webhook"""
    # ایجاد برنامه تلگرام
    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    
    # دریافت یوزرنیم ربات
    await application.initialize()
    bot = await application.bot.get_me()
    bot_username = bot.username
    logger.info(f"Bot username: @{bot_username}")
    await bot_manager.init(bot_username)
    
    # دستورات اصلی
    application.add_handler(CommandHandler("start", start))
    
    # مدیریت دسته‌ها
    category_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📁 ساخت دسته جدید$"), new_category)],
        states={
            AWAITING_CATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_category)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(category_handler)
    
    application.add_handler(MessageHandler(filters.Regex("^📂 نمایش دسته‌ها$"), categories_list))
    
    # آپلود فایل‌ها
    upload_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📤 آپلود فایل$"), upload_command),
            CallbackQueryHandler(lambda u, c: upload_command(u, c), pattern="^upload_cat_")
        ],
        states={
            UPLOADING: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO,
                    handle_file
                ),
                CommandHandler("finish_upload", finish_upload)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(upload_handler)
    
    # تایمر خودکار
    application.add_handler(MessageHandler(filters.Regex("^⏱ تایمر خودکار$"), timer_management))
    application.add_handler(CallbackQueryHandler(toggle_timer, pattern="^toggle_timer$"))
    
    # مدیریت کانال‌ها
    channel_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📢 تنظیم کانال اجباری$"), channel_management),
            CallbackQueryHandler(start_add_channel, pattern="^add_channel$")
        ],
        states={
            WAITING_CHANNEL_INFO: [
                MessageHandler(filters.TEXT, handle_channel_id),
                MessageHandler(filters.TEXT, handle_channel_name),
                MessageHandler(filters.TEXT, handle_channel_link)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(channel_handler)
    
    # مدیریت پیام‌های پس از ارسال
    post_message_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(setup_post_message, pattern="^postmsg_")],
        states={
            POST_MESSAGE_SETUP: [CallbackQueryHandler(handle_post_message_type)],
            AWAITING_POST_MESSAGE: [MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.DOCUMENT, save_post_message)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(post_message_handler)
    
    # دکمه‌های اینلاین
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # دستور لغو عمومی
    application.add_handler(CommandHandler("cancel", cancel))
    
    return application

async def run_web_server():
    """اجرای سرور وب و تنظیم Webhook"""
    # ساخت برنامه وب
    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_post(WEBHOOK_PATH, webhook_handler)
    
    # تنظیم ربات
    application = await setup_bot()
    app['bot_application'] = application
    
    # تنظیم Webhook
    await application.bot.set_webhook(
        url=WEBHOOK_URL + WEBHOOK_PATH,
        drop_pending_updates=True
    )
    logger.info(f"Webhook set to: {WEBHOOK_URL}{WEBHOOK_PATH}")
    
    # اجرای وظیفه keep_alive در پس‌زمینه
    asyncio.create_task(keep_alive())
    
    # اجرای سرور
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started at port {PORT}")
    
    # نگه داشتن برنامه در حال اجرا
    while True:
        await asyncio.sleep(3600)

async def main():
    """اجرای اصلی برنامه"""
    await run_web_server()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Critical error: {e}")