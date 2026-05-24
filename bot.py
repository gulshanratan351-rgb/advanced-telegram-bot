import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import re
from io import BytesIO

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, ChatPermissions
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, JobQueue
)
from telegram.constants import ParseMode
from pymongo import MongoClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
import requests

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
PREMIUM_FRAMES = {
    'gold': {'color': '#FFD700', 'width': 5},
    'silver': {'color': '#C0C0C0', 'width': 5},
    'bronze': {'color': '#CD7F32', 'width': 5},
    'rainbow': {'color': 'rainbow', 'width': 5}
}

class Database:
    def __init__(self):
        self.client = MongoClient(MONGO_URI)
        self.db = self.client.telegram_bot
        
        # Collections
        self.settings = self.db.settings
        self.scheduled_posts = self.db.scheduled_posts
        self.auto_forward = self.db.auto_forward
        self.users = self.db.users
        
        # Initialize default settings
        self._init_defaults()
    
    def _init_defaults(self):
        # Default bot settings
        if not self.settings.find_one({'_id': 'global'}):
            self.settings.insert_one({
                '_id': 'global',
                'header': '',
                'footer': '',
                'premium_frame': 'gold',
                'delete_service_msgs': True,
                'auto_approve_requests': False
            })
    
    def get_setting(self, key: str, default=None):
        setting = self.settings.find_one({'_id': 'global'})
        return setting.get(key, default) if setting else default
    
    def update_setting(self, key: str, value):
        self.settings.update_one(
            {'_id': 'global'},
            {'$set': {key: value}},
            upsert=True
        )
    
    def add_scheduled_post(self, post_data: dict):
        return self.scheduled_posts.insert_one(post_data)
    
    def get_scheduled_posts(self, status='pending'):
        return list(self.scheduled_posts.find({'status': status}))
    
    def update_scheduled_post(self, post_id, update_data):
        self.scheduled_posts.update_one(
            {'_id': post_id},
            {'$set': update_data}
        )
    
    def add_auto_forward(self, config: dict):
        self.auto_forward.insert_one(config)
    
    def get_auto_forward_configs(self):
        return list(self.auto_forward.find({'active': True}))
    
    def add_user(self, user_id: int, username: str = None):
        self.users.update_one(
            {'user_id': user_id},
            {'$set': {'username': username, 'last_active': datetime.now()}},
            upsert=True
        )

db = Database()

class PremiumEffects:
    @staticmethod
    def add_frame_to_image(image_bytes: bytes, frame_type: str = 'gold') -> BytesIO:
        """Add premium frame to image"""
        image = Image.open(BytesIO(image_bytes))
        
        if frame_type == 'rainbow':
            # Create rainbow gradient frame
            draw = ImageDraw.Draw(image)
            width, height = image.size
            frame_width = 10
            
            colors = ['#FF0000', '#FF7F00', '#FFFF00', '#00FF00', '#0000FF', '#4B0082', '#9400D3']
            segment = width // len(colors)
            
            for i, color in enumerate(colors):
                x1 = i * segment
                x2 = (i + 1) * segment
                draw.rectangle([x1, 0, x2, frame_width], fill=color)
                draw.rectangle([x1, height-frame_width, x2, height], fill=color)
                draw.rectangle([0, x1, frame_width, x2], fill=color)
                draw.rectangle([width-frame_width, x1, width, x2], fill=color)
        else:
            # Regular colored frame
            frame_config = PREMIUM_FRAMES.get(frame_type, PREMIUM_FRAMES['gold'])
            draw = ImageDraw.Draw(image)
            width, height = image.size
            frame_width = frame_config['width']
            
            for i in range(frame_width):
                draw.rectangle(
                    [i, i, width-1-i, height-1-i],
                    outline=frame_config['color']
                )
        
        output = BytesIO()
        image.save(output, format='PNG')
        output.seek(0)
        return output

class MessageModifier:
    @staticmethod
    def add_header_footer(text: str, header: str = None, footer: str = None) -> str:
        """Add header and footer to message"""
        if not header:
            header = db.get_setting('header', '')
        if not footer:
            footer = db.get_setting('footer', '')
        
        modified_text = ""
        if header:
            modified_text += f"{header}\n\n"
        modified_text += text
        if footer:
            modified_text += f"\n\n{footer}"
        
        return modified_text
    
    @staticmethod
    def format_with_buttons(text: str, buttons: List[List[dict]] = None) -> tuple:
        """Add inline buttons to message"""
        keyboard = []
        if buttons:
            for row in buttons:
                keyboard_row = []
                for btn in row:
                    if btn.get('url'):
                        keyboard_row.append(InlineKeyboardButton(btn['text'], url=btn['url']))
                    else:
                        keyboard_row.append(InlineKeyboardButton(btn['text'], callback_data=btn.get('callback', 'none')))
                keyboard.append(keyboard_row)
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        return text, reply_markup
    
    @staticmethod
    def create_spoiler(text: str) -> str:
        """Create spoiler tag for text"""
        return f"<span class=\"tg-spoiler\">{text}</span>"
    
    @staticmethod
    def hide_image_as_spoiler(photo_file_id: str) -> dict:
        """Mark image as spoiler"""
        return {'photo': photo_file_id, 'has_spoiler': True}

class AutoForwardManager:
    def __init__(self, application: Application):
        self.application = application
        
    async def start_forwarding(self):
        """Start monitoring source channels for auto-forward"""
        async def check_and_forward():
            configs = db.get_auto_forward_configs()
            for config in configs:
                # Get last processed message ID
                last_msg_id = config.get('last_processed_id', 0)
                
                try:
                    # Get recent messages from source chat
                    async for message in self.application.bot.get_chat_history(
                        chat_id=config['source_chat'],
                        limit=100
                    ):
                        if message.message_id > last_msg_id:
                            # Forward to destination channels
                            for dest_chat in config['destination_chats']:
                                try:
                                    await self.forward_message(message, dest_chat, config)
                                except Exception as e:
                                    logger.error(f"Forward error: {e}")
                            
                            # Update last processed ID
                            db.auto_forward.update_one(
                                {'_id': config['_id']},
                                {'$set': {'last_processed_id': message.message_id}}
                            )
                except Exception as e:
                    logger.error(f"Auto-forward check error: {e}")
        
        # Run every 30 seconds
        job_queue = self.application.job_queue
        job_queue.run_repeating(check_and_forward, interval=30, first=10)
    
    async def forward_message(self, message, dest_chat: int, config: dict):
        """Forward a single message with modifications"""
        settings = {
            'add_header': config.get('add_header', True),
            'add_footer': config.get('add_footer', True),
            'premium_frame': config.get('premium_frame', None),
            'hide_as_spoiler': config.get('hide_as_spoiler', False),
            'buttons': config.get('buttons', [])
        }
        
        modified_text = None
        if message.caption:
            modified_text = MessageModifier.add_header_footer(
                message.caption,
                header=None if not settings['add_header'] else '',
                footer=None if not settings['add_footer'] else ''
            )
        
        text, reply_markup = MessageModifier.format_with_buttons(
            modified_text or message.text or '',
            settings.get('buttons')
        )
        
        try:
            if message.photo:
                photo_file = await message.photo[-1].get_file()
                photo_bytes = await photo_file.download_as_bytearray()
                
                if settings['premium_frame']:
                    framed_image = PremiumEffects.add_frame_to_image(
                        bytes(photo_bytes),
                        settings['premium_frame']
                    )
                    await self.application.bot.send_photo(
                        chat_id=dest_chat,
                        photo=framed_image,
                        caption=text if text else None,
                        reply_markup=reply_markup,
                        has_spoiler=settings['hide_as_spoiler']
                    )
                else:
                    await self.application.bot.send_photo(
                        chat_id=dest_chat,
                        photo=message.photo[-1].file_id,
                        caption=text if text else None,
                        reply_markup=reply_markup,
                        has_spoiler=settings['hide_as_spoiler']
                    )
            elif message.text:
                await self.application.bot.send_message(
                    chat_id=dest_chat,
                    text=text or message.text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            else:
                await self.application.bot.forward_message(
                    chat_id=dest_chat,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id
                )
        except Exception as e:
            logger.error(f"Message forwarding failed: {e}")

class ScheduledPostManager:
    def __init__(self, application: Application):
        self.application = application
        self.scheduler = AsyncIOScheduler()
        
    async def schedule_posts(self):
        """Schedule all pending posts"""
        posts = db.get_scheduled_posts('pending')
        
        for post in posts:
            schedule_time = post['schedule_time']
            if schedule_time > datetime.now():
                self.scheduler.add_job(
                    self.send_scheduled_post,
                    'date',
                    run_date=schedule_time,
                    args=[post['_id']],
                    id=str(post['_id'])
                )
        
        self.scheduler.start()
    
    async def send_scheduled_post(self, post_id):
        """Send a scheduled post"""
        post = db.scheduled_posts.find_one({'_id': post_id})
        if not post:
            return
        
        try:
            for chat_id in post['destination_chats']:
                modified_text = MessageModifier.add_header_footer(
                    post['content'],
                    header=post.get('header'),
                    footer=post.get('footer')
                )
                
                text, reply_markup = MessageModifier.format_with_buttons(
                    modified_text,
                    post.get('buttons', [])
                )
                
                if post.get('media_type') == 'photo':
                    await self.application.bot.send_photo(
                        chat_id=chat_id,
                        photo=post['media_id'],
                        caption=text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await self.application.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML
                    )
            
            # Mark as sent
            db.update_scheduled_post(post_id, {'status': 'sent', 'sent_at': datetime.now()})
        except Exception as e:
            logger.error(f"Failed to send scheduled post: {e}")
            db.update_scheduled_post(post_id, {'status': 'failed', 'error': str(e)})

# Admin Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username)
    
    is_admin = user.id in ADMIN_IDS
    
    keyboard = [
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📝 Schedule Post", callback_data="schedule")],
        [InlineKeyboardButton("🔄 Auto Forward", callback_data="auto_forward_menu")],
    ]
    
    if is_admin:
        keyboard.append([InlineKeyboardButton("👥 User Stats", callback_data="user_stats")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"Welcome {user.first_name}! 👋\n\n"
        "I'm an advanced Telegram bot with powerful features:\n\n"
        "✨ **Features:**\n"
        "• Auto-forward posts between channels\n"
        "• Schedule posts for later\n"
        "• Premium frames for images\n"
        "• Header/Footer system\n"
        "• Spoiler hidden images\n"
        "• Auto join request accept\n"
        "• Inline buttons support\n\n"
        "Use the buttons below to get started!",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    header = db.get_setting('header', 'Not set')
    footer = db.get_setting('footer', 'Not set')
    frame = db.get_setting('premium_frame', 'gold')
    auto_approve = db.get_setting('auto_approve_requests', False)
    delete_service = db.get_setting('delete_service_msgs', True)
    
    keyboard = [
        [InlineKeyboardButton("📝 Set Header", callback_data="set_header")],
        [InlineKeyboardButton("📝 Set Footer", callback_data="set_footer")],
        [InlineKeyboardButton(f"🖼️ Premium Frame: {frame.title()}", callback_data="change_frame")],
        [InlineKeyboardButton(f"✅ Auto Approve: {'ON' if auto_approve else 'OFF'}", callback_data="toggle_auto_approve")],
        [InlineKeyboardButton(f"🗑️ Delete Service Msgs: {'ON' if delete_service else 'OFF'}", callback_data="toggle_service_delete")],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")]
    ]
    
    text = f"**Bot Settings**\n\n"
    text += f"**Header:** {header[:50]}...\n" if len(header) > 50 else f"**Header:** {header or 'Not set'}\n"
    text += f"**Footer:** {footer[:50]}...\n" if len(footer) > 50 else f"**Footer:** {footer or 'Not set'}\n"
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def set_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_header'] = True
    
    await query.edit_message_text(
        "Please send the header text you want to add to all messages.\n"
        "Send /cancel to cancel.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="settings")
        ]])
    )

async def set_footer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_footer'] = True
    
    await query.edit_message_text(
        "Please send the footer text you want to add to all messages.\n"
        "Send /cancel to cancel.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="settings")
        ]])
    )

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_header'):
        header = update.message.text
        db.update_setting('header', header)
        context.user_data.pop('awaiting_header')
        
        await update.message.reply_text(
            f"✅ Header has been set successfully!\n\n"
            f"**Header:** {header}",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Show settings menu again
        keyboard = [[InlineKeyboardButton("⚙️ Back to Settings", callback_data="settings")]]
        await update.message.reply_text(
            "What would you like to do next?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif context.user_data.get('awaiting_footer'):
        footer = update.message.text
        db.update_setting('footer', footer)
        context.user_data.pop('awaiting_footer')
        
        await update.message.reply_text(
            f"✅ Footer has been set successfully!\n\n"
            f"**Footer:** {footer}",
            parse_mode=ParseMode.MARKDOWN
        )
        
        keyboard = [[InlineKeyboardButton("⚙️ Back to Settings", callback_data="settings")]]
        await update.message.reply_text(
            "What would you like to do next?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def change_frame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = []
    for frame_name in PREMIUM_FRAMES.keys():
        keyboard.append([InlineKeyboardButton(
            f"🎨 {frame_name.title()}",
            callback_data=f"set_frame_{frame_name}"
        )])
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="settings")])
    
    await query.edit_message_text(
        "**Select Premium Frame Style:**\n\n"
        "Choose a frame to apply to all outgoing images:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def set_frame_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    frame = query.data.replace('set_frame_', '')
    
    if frame in PREMIUM_FRAMES:
        db.update_setting('premium_frame', frame)
        await query.answer(f"✅ {frame.title()} frame activated!")
        
        # Show settings menu
        await settings_menu(update, context)

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    total_users = db.users.count_documents({})
    scheduled_count = db.scheduled_posts.count_documents({'status': 'pending'})
    auto_forward_count = db.auto_forward.count_documents({'active': True})
    
    keyboard = [
        [InlineKeyboardButton("📅 View Scheduled Posts", callback_data="view_scheduled")],
        [InlineKeyboardBu
