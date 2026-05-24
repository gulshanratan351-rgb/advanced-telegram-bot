import os
import logging
from datetime import datetime
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from pymongo import MongoClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

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
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set!")
    exit(1)

if not MONGO_URI:
    logger.error("MONGO_URI not set!")
    exit(1)

# Database Class
class Database:
    def __init__(self):
        try:
            self.client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            self.db = self.client.telegram_bot
            self.users = self.db.users
            self.settings = self.db.settings
            self.scheduled_posts = self.db.scheduled_posts
            self.auto_forward = self.db.auto_forward
            
            # Test connection
            self.client.admin.command('ping')
            logger.info("MongoDB connected successfully!")
            
            # Initialize default settings
            if not self.settings.find_one({'_id': 'global'}):
                self.settings.insert_one({
                    '_id': 'global',
                    'header': '',
                    'footer': '',
                    'premium_frame': 'gold',
                    'delete_service_msgs': True,
                    'auto_approve_requests': False
                })
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}")
            raise
    
    def get_setting(self, key, default=None):
        setting = self.settings.find_one({'_id': 'global'})
        return setting.get(key, default) if setting else default
    
    def update_setting(self, key, value):
        self.settings.update_one({'_id': 'global'}, {'$set': {key: value}}, upsert=True)
    
    def add_user(self, user_id, username=None, first_name=None):
        self.users.update_one(
            {'user_id': user_id},
            {'$set': {
                'username': username,
                'first_name': first_name,
                'last_active': datetime.now()
            }},
            upsert=True
        )
    
    def get_user_count(self):
        return self.users.count_documents({})
    
    def add_scheduled_post(self, post_data):
        return self.scheduled_posts.insert_one(post_data)
    
    def get_scheduled_posts(self, status='pending'):
        return list(self.scheduled_posts.find({'status': status}))
    
    def add_auto_forward(self, config):
        return self.auto_forward.insert_one(config)
    
    def get_auto_forward_configs(self):
        return list(self.auto_forward.find({'active': True}))

# Initialize database
try:
    db = Database()
except Exception as e:
    logger.error(f"Failed to initialize database: {e}")
    db = None

# Message Modifier Class
class MessageModifier:
    @staticmethod
    def add_header_footer(text, header=None, footer=None):
        if not header:
            header = db.get_setting('header', '') if db else ''
        if not footer:
            footer = db.get_setting('footer', '') if db else ''
        
        modified_text = ""
        if header:
            modified_text += f"{header}\n\n"
        modified_text += text
        if footer:
            modified_text += f"\n\n{footer}"
        
        return modified_text

# ============= HANDLERS =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    if not db:
        await update.message.reply_text("❌ Database connection failed. Please try again later.")
        return
    
    user = update.effective_user
    db.add_user(user.id, user.username, user.first_name)
    
    is_admin = user.id in ADMIN_IDS
    
    keyboard = [
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📝 Schedule Post", callback_data="schedule")],
        [InlineKeyboardButton("🔄 Auto Forward", callback_data="auto_forward")],
    ]
    
    if is_admin:
        keyboard.append([InlineKeyboardButton("👥 User Stats", callback_data="user_stats")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✨ **Welcome {user.first_name}!** ✨\n\n"
        f"🤖 **Advanced Telegram Modifier Bot**\n\n"
        f"📊 **Users:** {db.get_user_count()}\n"
        f"💾 **Database:** ✅ Connected\n\n"
        f"**Features:**\n"
        f"• Auto Forward Posts\n"
        f"• Multi Channel Posting\n"
        f"• Scheduled Posting\n"
        f"• Premium Frames\n"
        f"• Header/Footer System\n"
        f"• Inline Buttons\n\n"
        f"Use buttons below to get started!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dashboard callback"""
    query = update.callback_query
    await query.answer()
    
    if not db:
        await query.edit_message_text("❌ Database not connected!")
        return
    
    total_users = db.get_user_count()
    scheduled_count = db.scheduled_posts.count_documents({'status': 'pending'})
    auto_forward_count = db.auto_forward.count_documents({'active': True})
    
    text = f"📊 **Dashboard**\n\n"
    text += f"👥 **Total Users:** {total_users}\n"
    text += f"📝 **Scheduled Posts:** {scheduled_count}\n"
    text += f"🔄 **Auto-Forward Rules:** {auto_forward_count}\n"
    text += f"🤖 **Bot Status:** 🟢 Online\n"
    text += f"💾 **Database:** ✅ Connected\n"
    
    keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="main_menu")]]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Settings menu"""
    query = update.callback_query
    await query.answer()
    
    if not db:
        await query.edit_message_text("❌ Database not connected!")
        return
    
    header = db.get_setting('header', 'Not set')
    footer = db.get_setting('footer', 'Not set')
    delete_service = db.get_setting('delete_service_msgs', True)
    auto_approve = db.get_setting('auto_approve_requests', False)
    
    text = f"⚙️ **Bot Settings**\n\n"
    text += f"📝 **Header:** {header[:30] if header else 'Not set'}...\n" if len(str(header)) > 30 else f"📝 **Header:** {header or 'Not set'}\n"
    text += f"📝 **Footer:** {footer[:30] if footer else 'Not set'}...\n" if len(str(footer)) > 30 else f"📝 **Footer:** {footer or 'Not set'}\n"
    text += f"🗑️ **Delete Service Msgs:** {'✅ ON' if delete_service else '❌ OFF'}\n"
    text += f"✅ **Auto Approve Joins:** {'✅ ON' if auto_approve else '❌ OFF'}\n"
    
    keyboard = [
        [InlineKeyboardButton("📝 Set Header", callback_data="set_header")],
        [InlineKeyboardButton("📝 Set Footer", callback_data="set_footer")],
        [InlineKeyboardButton(f"🗑️ Service Messages", callback_data="toggle_service")],
        [InlineKeyboardButton(f"✅ Auto Approve", callback_data="toggle_approve")],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def set_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set header"""
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_header'] = True
    
    await query.edit_message_text(
        "📝 **Set Header**\n\n"
        "Send the header text you want to add to all messages.\n\n"
        "Send /cancel to cancel.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="settings")
        ]]),
        parse_mode='Markdown'
    )

async def set_footer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set footer"""
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_footer'] = True
    
    await query.edit_message_text(
        "📝 **Set Footer**\n\n"
        "Send the footer text you want to add to all messages.\n\n"
        "Send /cancel to cancel.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="settings")
        ]]),
        parse_mode='Markdown'
    )

async def toggle_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle service message deletion"""
    query = update.callback_query
    await query.answer()
    
    current = db.get_setting('delete_service_msgs', True)
    db.update_setting('delete_service_msgs', not current)
    
    status = "ON" if not current else "OFF"
    await query.edit_message_text(
        f"✅ Service message deletion turned **{status}**!",
        parse_mode='Markdown'
    )
    await settings_menu(update, context)

async def toggle_auto_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto approve join requests"""
    query = update.callback_query
    await query.answer()
    
    current = db.get_setting('auto_approve_requests', False)
    db.update_setting('auto_approve_requests', not current)
    
    status = "ON" if not current else "OFF"
    await query.edit_message_text(
        f"✅ Auto approve join requests turned **{status}**!",
        parse_mode='Markdown'
    )
    await settings_menu(update, context)

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for settings"""
    if context.user_data.get('awaiting_header'):
        header = update.message.text
        db.update_setting('header', header)
        context.user_data.pop('awaiting_header')
        
        await update.message.reply_text(
            f"✅ **Header has been set!**\n\n",
            parse_mode='Markdown'
        )
        
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
            f"✅ **Footer has been set!**\n\n",
            parse_mode='Markdown'
        )
        
        keyboard = [[InlineKeyboardButton("⚙️ Back to Settings", callback_data="settings")]]
        await update.message.reply_text(
            "What would you like to do next?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule post menu"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📝 **Schedule Post**\n\n"
        "Feature coming soon!\n\n"
        "This will allow you to:\n"
        "• Schedule messages for future\n"
        "• Set multiple destinations\n"
        "• Add headers/footers\n"
        "• Add inline buttons",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Back", callback_data="main_menu")
        ]]),
        parse_mode='Markdown'
    )

async def auto_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto forward menu"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🔄 **Auto Forward**\n\n"
        "Feature coming soon!\n\n"
        "This will allow you to:\n"
        "• Auto-forward from source channels\n"
        "• Forward to multiple destinations\n"
        "• Add custom modifications\n"
        "• Apply premium frames",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Back", callback_data="main_menu")
        ]]),
        parse_mode='Markdown'
    )

async def user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User statistics for admin"""
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ **Admin only command!**", parse_mode='Markdown')
        return
    
    if not db:
        await query.edit_message_text("❌ Database not connected!")
        return
    
    total_users = db.get_user_count()
    
    # Get recent users
    recent_users = list(db.users.find().sort('last_active', -1).limit(10))
    
    text = f"👥 **User Statistics**\n\n"
    text += f"📊 **Total Users:** {total_users}\n\n"
    text += f"🆕 **Recent Users:**\n"
    
    for user in recent_users:
        name = user.get('first_name', 'Unknown')
        username = user.get('username', 'no_username')
        last_active = user.get('last_active', datetime.now()).strftime('%Y-%m-%d')
        text += f"• {name} (@{username}) - {last_active}\n"
    
    keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="main_menu")]]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return to main menu"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    is_admin = user.id in ADMIN_IDS
    
    keyboard = [
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📝 Schedule Post", callback_data="schedule")],
        [InlineKeyboardButton("🔄 Auto Forward", callback_data="auto_forward")],
    ]
    
    if is_admin:
        keyboard.append([InlineKeyboardButton("👥 User Stats", callback_data="user_stats")])
    
    await query.edit_message_text(
        f"✨ **Main Menu** ✨\n\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current operation"""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Operation cancelled!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Back to Menu", callback_data="main_menu")
        ]])
    )

async def handle_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete service messages if enabled"""
    if not db:
        return
    
    delete_service = db.get_setting('delete_service_msgs', True)
    
    if delete_service and update.message:
        if update.message.new_chat_members or update.message.left_chat_member:
            try:
                await update.message.delete()
            except:
                pass

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto approve join requests"""
    if not db:
        return
    
    auto_approve = db.get_setting('auto_approve_requests', False)
    
    if auto_approve and update.chat_join_request:
        try:
            await update.chat_join_request.approve()
            logger.info(f"Approved join request from {update.effective_user.id}")
        except Exception as e:
            logger.error(f"Failed to approve join request: {e}")

# ============= MAIN =============

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("No BOT_TOKEN provided!")
        return
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(dashboard, pattern='^dashboard$'))
    application.add_handler(CallbackQueryHandler(settings_menu, pattern='^settings$'))
    application.add_handler(CallbackQueryHandler(main_menu, pattern='^main_menu$'))
    application.add_handler(CallbackQueryHandler(user_stats, pattern='^user_stats$'))
    application.add_handler(CallbackQueryHandler(set_header, pattern='^set_header$'))
    application.add_handler(CallbackQueryHandler(set_footer, pattern='^set_footer$'))
    application.add_handler(CallbackQueryHandler(schedule_post, pattern='^schedule$'))
    application.add_handler(CallbackQueryHandler(auto_forward, pattern='^auto_forward$'))
    application.add_handler(CallbackQueryHandler(toggle_service_messages, pattern='^toggle_service$'))
    application.add_handler(CallbackQueryHandler(toggle_auto_approve, pattern='^toggle_approve$'))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_service_messages))
    application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_service_messages))
    application.add_handler(MessageHandler(filters.StatusUpdate.CHAT_JOIN_REQUEST, handle_join_request))
    
    # Error handler
    async def error_handler(update, context):
        logger.error(f"Update {update} caused error {context.error}")
    
    application.add_error_handler(error_handler)
    
    # Start bot
    logger.info("🚀 Bot started successfully!")
    print("✅ Bot is running... Press Ctrl+C to stop")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
