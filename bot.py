import os
import asyncio
import logging
import math
import time
import re
import secrets
import string
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from collections import defaultdict
from pyrogram import Client, filters, errors
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import QueryIdInvalid, MessageNotModified
import motor.motor_asyncio
import signal

# ========== LOGGING CONFIGURATION ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== CONFIGURATION ==========
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info("Loaded environment variables from .env file")
except ImportError:
    pass

class Config:
    API_ID = int(os.getenv("API_ID", 0))
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    DB_URL = os.getenv("DB_URL", "")
    DB_NAME = os.getenv("DB_NAME", "RenameBot")
    OWNER_IDS = [int(x.strip()) for x in os.getenv("OWNER_IDS", "0").split(",") if x.strip().isdigit()]
    DUMP_CHANNEL = os.getenv("DUMP_CHANNEL", "")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2147483648))  # 2GB default
    
    # Allowed file extensions
    ALLOWED_EXTENSIONS = {
        'document': ['.pdf', '.txt', '.doc', '.docx', '.zip', '.rar', '.7z', '.tar', '.gz', 
                    '.mp3', '.wav', '.flac', '.ogg', '.aac', '.mp4', '.mkv', '.avi', '.mov'],
        'video': ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'],
        'audio': ['.mp3', '.wav', '.flac', '.ogg', '.aac', '.m4a', '.wma']
    }

# ========== CONFIG VALIDATION ==========
def validate_config():
    """Validate essential configuration"""
    missing = []
    if not Config.API_ID or Config.API_ID == 0:
        missing.append("API_ID")
    if not Config.API_HASH:
        missing.append("API_HASH") 
    if not Config.BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not Config.DB_URL:
        missing.append("DB_URL")
    
    if missing:
        error_msg = f"Missing configuration: {', '.join(missing)}"
        logger.error(error_msg)
        raise Exception(error_msg)
    
    logger.info("Configuration validation successful")

validate_config()

# ========== RATE LIMITING ==========
user_requests = defaultdict(list)

def rate_limit(user_id, max_requests=10, time_window=60):
    """Basic rate limiting"""
    now = time.time()
    user_requests[user_id] = [req_time for req_time in user_requests[user_id] if now - req_time < time_window]
    
    if len(user_requests[user_id]) >= max_requests:
        return False
    
    user_requests[user_id].append(now)
    return True

# ========== UTILITY FUNCTIONS ==========
def sanitize_filename(filename):
    """Sanitize filename to prevent path traversal and remove invalid characters"""
    if not filename:
        return "file"
    
    # Remove path traversal attempts
    filename = os.path.basename(filename)
    
    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    
    # Limit length
    if len(filename) > 100:
        name, ext = os.path.splitext(filename)
        filename = name[:100-len(ext)] + ext
    
    return filename.strip() or "file"

def is_allowed_file_type(filename, file_type):
    """Check if file extension is allowed for the given file type"""
    _, ext = os.path.splitext(filename.lower())
    return ext in Config.ALLOWED_EXTENSIONS.get(file_type, [])

async def check_file_size(message):
    """Check if file size is within limits"""
    file_size = 0
    if message.document:
        file_size = message.document.file_size
    elif message.video:
        file_size = message.video.file_size
    elif message.audio:
        file_size = message.audio.file_size
    
    if file_size > Config.MAX_FILE_SIZE:
        await message.reply_text(f"❌ **File too large! Maximum size is {humanbytes(Config.MAX_FILE_SIZE)}**")
        return False
    return True

async def progress_for_pyrogram(current, total, ud_type, message, start, filename):
    """Progress callback for upload/download"""
    now = time.time()
    diff = now - start
    if round(diff % 2.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000 if speed > 0 else 0
        estimated_total_time = elapsed_time + time_to_completion

        elapsed_time = TimeFormatter(milliseconds=elapsed_time)
        estimated_total_time = TimeFormatter(milliseconds=estimated_total_time)

        progress_bar = "▣" * math.floor(percentage / 10) + "□" * (10 - math.floor(percentage / 10))
        
        progress_text = f"""
{ud_type}

📄 **File:** `{filename}`

[{progress_bar}] {round(percentage, 1)}%

💾 **Size:** {humanbytes(current)} / {humanbytes(total)}

🚀 **Speed:** {humanbytes(speed)}/s

⏰ **ETA:** {estimated_total_time if estimated_total_time != '' else '0s'}
"""
        try:
            await message.edit(text=progress_text)
        except Exception as e:
            logger.error(f"Error updating progress: {e}")

def humanbytes(size):    
    """Convert bytes to human readable format"""
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    units = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power and n < 4:
        size /= power
        n += 1
    return f"{size:.1f} {units[n]}"

def TimeFormatter(milliseconds: int) -> str:
    """Convert milliseconds to human readable time"""
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

def convert_seconds(seconds):
    """Convert seconds to HH:MM:SS or MM:SS format"""
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

# ========== SIMPLE HTTP SERVER ==========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    
    def log_message(self, format, *args):
        logger.info(f"HTTP Server: {format % args}")

def run_health_server():
    """Run health check server in separate thread"""
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    logger.info("Health server started on port 8080")
    server.serve_forever()

health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

# ========== DATABASE CLASS ==========
class Database:
    def __init__(self, uri, database_name):
        try:
            self._client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
            self.db = self._client[database_name]
            self.col = self.db.users
            self.allowed_users = self.db.allowed_users
            self.settings = self.db.settings
            logger.info("Database connection established")
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise

    async def test_connection(self):
        """Test database connection"""
        try:
            await self._client.admin.command('ping')
            return True
        except Exception as e:
            logger.error(f"Database ping failed: {e}")
            return False

    async def set_thumbnail(self, user_id, file_id):
        await self.col.update_one({"_id": user_id}, {"$set": {"file_id": file_id}}, upsert=True)

    async def get_thumbnail(self, user_id):
        user = await self.col.find_one({"_id": user_id})
        return user.get("file_id") if user else None

    async def add_allowed_user(self, user_id):
        await self.allowed_users.update_one({"_id": user_id}, {"$set": {"added_at": time.time()}}, upsert=True)

    async def remove_allowed_user(self, user_id):
        await self.allowed_users.delete_one({"_id": user_id})

    async def is_allowed_user(self, user_id):
        if user_id in Config.OWNER_IDS:
            return True
        allowed_user = await self.allowed_users.find_one({"_id": user_id})
        return allowed_user is not None

    async def get_all_allowed_users(self):
        allowed_users = []
        async for user in self.allowed_users.find():
            allowed_users.append({"id": user["_id"], "added_at": user.get("added_at", 0)})
        return allowed_users

    async def get_all_users(self):
        users = []
        async for user in self.col.find():
            users.append(user["_id"])
        return users

    async def get_private_mode(self):
        setting = await self.settings.find_one({"_id": "private_mode"})
        return setting.get("value") if setting else True

    async def set_private_mode(self, value):
        await self.settings.update_one({"_id": "private_mode"}, {"$set": {"value": value}}, upsert=True)

    # FIX: Store dump channel in database to persist across restarts
    async def get_dump_channel(self):
        setting = await self.settings.find_one({"_id": "dump_channel"})
        return setting.get("value") if setting else ""

    async def set_dump_channel(self, value):
        await self.settings.update_one({"_id": "dump_channel"}, {"$set": {"value": value}}, upsert=True)

# Initialize database
db = Database(Config.DB_URL, Config.DB_NAME)

# ========== BOT SETUP ==========
app = Client("rename_bot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)

# ========== GLOBAL VARIABLES ==========
user_states = {}
cancellation_flags = {}

# ========== RESOURCE CLEANUP ==========
async def cleanup_user_data(user_id):
    """Cleanup user data and files"""
    if user_id in user_states:
        user_data = user_states[user_id]
        
        # Cleanup files
        for file_type in ['file_path', 'thumb_path']:
            if user_data.get(file_type) and os.path.exists(user_data[file_type]):
                try:
                    os.remove(user_data[file_type])
                    logger.info(f"Cleaned up {file_type} for user {user_id}")
                except Exception as e:
                    logger.error(f"Error cleaning {file_type} for user {user_id}: {e}")
        
        # Remove from state
        del user_states[user_id]
    
    # Clear cancellation flag
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]

# ========== DUMP CHANNEL FUNCTIONS ==========
async def initialize_dump_channel():
    """Initialize dump channel from database on startup"""
    dump_channel = await db.get_dump_channel()
    if dump_channel:
        Config.DUMP_CHANNEL = dump_channel
        logger.info(f"Dump channel initialized from database: {dump_channel}")
    else:
        logger.info("No dump channel configured")

async def send_to_dump_channel(client, file_path, final_filename, user_id, file_type, original_duration, thumb_path=None):
    """Send file to dump channel"""
    if not Config.DUMP_CHANNEL:
        return
    
    try:
        # Prepare caption for dump channel
        caption = f"**File Name:** `{final_filename}`\n**User ID:** `{user_id}`\n**Type:** `{file_type}`\n**Time:** {time.ctime()}"
        
        if file_type == "document":
            await client.send_document(
                Config.DUMP_CHANNEL,
                document=file_path,
                caption=caption,
                thumb=thumb_path
            )
        elif file_type == "video":
            await client.send_video(
                Config.DUMP_CHANNEL,
                video=file_path,
                caption=caption,
                duration=original_duration,
                thumb=thumb_path,
                supports_streaming=True
            )
        elif file_type == "audio":
            await client.send_audio(
                Config.DUMP_CHANNEL,
                audio=file_path,
                caption=caption,
                thumb=thumb_path
            )
        
        logger.info(f"File dumped to channel: {final_filename} by user {user_id}")
        
    except Exception as e:
        logger.error(f"Error dumping to channel: {e}")

# ========== ACCESS CONTROL ==========
def private_access(func):
    async def wrapper(client, message):
        user_id = message.from_user.id
        
        # Rate limiting
        if not rate_limit(user_id):
            await message.reply_text("🚫 **Rate limit exceeded. Please wait a minute.**")
            return
        
        private_mode = await db.get_private_mode()
        
        if not private_mode:
            return await func(client, message)
        
        if await db.is_allowed_user(user_id):
            return await func(client, message)
        else:
            await message.reply_text("🚫 **This bot is in private mode. Contact owner for access.**")
            return
    return wrapper

def main_owner_only(func):
    async def wrapper(client, message):
        if message.from_user.id in Config.OWNER_IDS:
            return await func(client, message)
        else:
            await message.reply_text("🚫 **Owner only command.**")
            return
    return wrapper

# ========== COMMANDS ==========
@app.on_message(filters.private & filters.command("start"))
async def start_command(client, message):
    user_id = message.from_user.id
    private_mode = await db.get_private_mode()
    
    # Check if user is allowed (owner or allowed user)
    is_allowed = await db.is_allowed_user(user_id)
    
    if private_mode and not is_allowed:
        await message.reply_text("🚫 **This bot is in private mode. Contact owner for access.**")
        return
    
    is_owner = user_id in Config.OWNER_IDS
    
    text = "👋 **File Rename Bot**\n\nSend any file to rename it.\n\n**Commands:**\n• /view_thumb - View thumbnail\n• /del_thumb - Delete thumbnail\n• /cancel - Cancel process"
    
    if is_owner:
        mode = "PRIVATE" if private_mode else "PUBLIC"
        dump_status = "✅ ENABLED" if Config.DUMP_CHANNEL else "❌ DISABLED"
        text += f"\n\n**Owner Commands:**\n• /addalloweduser ID\n• /removealloweduser ID\n• /allowedusers\n• /users\n• /mode private|public\n• /dumpchannel ID|off\n• **Mode:** {mode}\n• **Dump Channel:** {dump_status}"
    
    await message.reply_text(text)

@app.on_message(filters.private & filters.command("addalloweduser"))
@main_owner_only
async def add_allowed_user_command(client, message):
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/addalloweduser USER_ID`")
        return
    
    try:
        user_id = int(message.command[1])
        await db.add_allowed_user(user_id)
        await message.reply_text(f"✅ **Added {user_id}**")
        logger.info(f"Owner {message.from_user.id} added allowed user {user_id}")
    except:
        await message.reply_text("❌ **Invalid ID**")

@app.on_message(filters.private & filters.command("removealloweduser"))
@main_owner_only
async def remove_allowed_user_command(client, message):
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/removealloweduser USER_ID`")
        return
    
    try:
        user_id = int(message.command[1])
        await db.remove_allowed_user(user_id)
        await message.reply_text(f"✅ **Removed {user_id}**")
        logger.info(f"Owner {message.from_user.id} removed allowed user {user_id}")
    except:
        await message.reply_text("❌ **Invalid ID**")

@app.on_message(filters.private & filters.command("allowedusers"))
@main_owner_only
async def list_allowed_users_command(client, message):
    users = await db.get_all_allowed_users()
    if not users:
        await message.reply_text("**No allowed users**")
        return
    
    text = "**Allowed Users:**\n"
    for user in users:
        text += f"`{user['id']}` - <code>{time.ctime(user['added_at'])}</code>\n"
    await message.reply_text(text)

@app.on_message(filters.private & filters.command("users"))
@main_owner_only
async def list_users_command(client, message):
    users = await db.get_all_users()
    text = f"**Total Users:** {len(users)}\n"
    for user_id in users[:15]:
        text += f"`{user_id}`\n"
    await message.reply_text(text)

@app.on_message(filters.private & filters.command("mode"))
@main_owner_only
async def mode_command(client, message):
    if len(message.command) < 2:
        mode = await db.get_private_mode()
        await message.reply_text(f"**Current mode:** {'PRIVATE' if mode else 'PUBLIC'}")
        return
    
    mode = message.command[1].lower()
    if mode in ["private", "true", "1"]:
        await db.set_private_mode(True)
        await message.reply_text("✅ **Private mode ON**")
        logger.info(f"Owner {message.from_user.id} enabled private mode")
    else:
        await db.set_private_mode(False)
        await message.reply_text("✅ **Public mode ON**")
        logger.info(f"Owner {message.from_user.id} disabled private mode")

@app.on_message(filters.private & filters.command("dumpchannel"))
@main_owner_only
async def dump_channel_command(client, message):
    if len(message.command) < 2:
        current_channel = Config.DUMP_CHANNEL if Config.DUMP_CHANNEL else "Not set"
        await message.reply_text(f"**Current Dump Channel:** `{current_channel}`\n\n**Usage:** `/dumpchannel CHANNEL_ID`\n\nTo disable: `/dumpchannel off`")
        return
    
    channel = message.command[1]
    if channel.lower() in ["off", "disable", "none"]:
        Config.DUMP_CHANNEL = ""
        await db.set_dump_channel("")
        await message.reply_text("✅ **Dump channel disabled**")
        logger.info(f"Owner {message.from_user.id} disabled dump channel")
    else:
        Config.DUMP_CHANNEL = channel
        await db.set_dump_channel(channel)
        await message.reply_text(f"✅ **Dump channel set to:** `{channel}`")
        logger.info(f"Owner {message.from_user.id} set dump channel to {channel}")

@app.on_message(filters.private & filters.command("backup"))
@main_owner_only
async def backup_command(client, message):
    """Create backup of allowed users"""
    try:
        users = await db.get_all_allowed_users()
        if users:
            backup_text = "**Allowed Users Backup:**\n" + "\n".join([f"`{user['id']}`" for user in users])
            await message.reply_text(backup_text)
        else:
            await message.reply_text("No allowed users to backup")
    except Exception as e:
        await message.reply_text(f"Backup failed: {e}")
        logger.error(f"Backup command failed: {e}")

@app.on_message(filters.private & filters.command("cancel"))
@private_access
async def cancel_command(client, message):
    user_id = message.from_user.id
    
    # Set cancellation flag
    cancellation_flags[user_id] = True
    
    # Cleanup files and state
    await cleanup_user_data(user_id)
    
    await message.reply_text("✅ **Process cancelled successfully!**")
    logger.info(f"User {user_id} cancelled process")

# ========== CANCEL CALLBACK HANDLERS ==========
@app.on_callback_query(filters.regex(r"^cancel_confirm_(\d+)$"))
async def cancel_confirm_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("❌ You can only cancel your own processes!", show_alert=True)
        return
    
    # Show confirmation buttons
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Cancel", callback_data=f"cancel_yes_{user_id}")],
        [InlineKeyboardButton("❌ No, Continue", callback_data=f"cancel_no_{user_id}")]
    ])
    
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("Are you sure you want to cancel?")
    except Exception as e:
        logger.error(f"Cancel confirm error: {e}")
        await callback_query.answer("Error updating message", show_alert=True)

@app.on_callback_query(filters.regex(r"^cancel_yes_(\d+)$"))
async def cancel_yes_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("❌ Access denied!", show_alert=True)
        return
    
    # Set cancellation flag
    cancellation_flags[user_id] = True
    
    # Cleanup files and state
    await cleanup_user_data(user_id)
    
    await callback_query.message.edit_text("✅ **Process cancelled successfully!**")
    await callback_query.answer()
    logger.info(f"User {user_id} confirmed cancellation")

@app.on_callback_query(filters.regex(r"^cancel_no_(\d+)$"))
async def cancel_no_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("❌ Access denied!", show_alert=True)
        return
    
    # Clear cancellation flag
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
    # Restore original cancel button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
    ])
    
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("✅ Process continued")
    except Exception as e:
        logger.error(f"Cancel no error: {e}")
        await callback_query.answer("Error updating message", show_alert=True)

# ========== THUMBNAIL MANAGEMENT ==========
@app.on_message(filters.private & filters.command(["view_thumb", "viewthumbnail"]))
@private_access
async def view_thumbnail(client, message):
    thumbnail = await db.get_thumbnail(message.from_user.id)
    if thumbnail:
        try:
            await client.send_photo(message.chat.id, thumbnail, caption="**Your thumbnail**")
        except:
            await message.reply_text("❌ **Invalid thumbnail**")
            await db.set_thumbnail(message.from_user.id, None)
    else:
        await message.reply_text("**No thumbnail set**")

@app.on_message(filters.private & filters.command(["del_thumb", "deletethumbnail"]))
@private_access
async def delete_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, None)
    await message.reply_text("✅ **Thumbnail deleted**")

@app.on_message(filters.private & filters.photo)
@private_access
async def save_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, message.photo.file_id)
    await message.reply_text("✅ **Thumbnail saved**")

# ========== FILE HANDLING ==========
@app.on_message(filters.private & (filters.document | filters.video | filters.audio))
@private_access
async def handle_file(client, message):
    user_id = message.from_user.id
    
    if user_id in user_states:
        await message.reply_text("❌ **Please complete your current process first!**\nUse /cancel to cancel.")
        return
    
    # Check file size
    if not await check_file_size(message):
        return
    
    # Clear cancellation flag
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
    # Get file info
    if message.document:
        file = message.document
        file_type = "document"
        duration = 0
    elif message.video:
        file = message.video
        file_type = "video"
        duration = getattr(file, 'duration', 0)
    elif message.audio:
        file = message.audio
        file_type = "audio"
        duration = getattr(file, 'duration', 0)
    else:
        return

    file_name = sanitize_filename(getattr(file, 'file_name', 'Unknown'))
    file_size = humanbytes(file.file_size)
    
    user_states[user_id] = {
        'file_info': {
            'file_name': file_name,
            'file_size': file_size,
            'file_type': file_type,
            'duration': duration,
            'original_message': message,
            'file_id': file.file_id,
            'mime_type': getattr(file, 'mime_type', '')
        },
        'step': 'awaiting_rename'
    }

    duration_text = convert_seconds(duration) if duration > 0 else "N/A"
    
    info_text = f"""**📁 File Information:**

**Name:** `{file_name}`
**Size:** `{file_size}`
**Type:** `{file_type.title()}`
**Duration:** `{duration_text}`

**Click RENAME to continue.**"""

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Rename", callback_data="start_rename")]])
    await message.reply_text(info_text, reply_markup=keyboard)
    logger.info(f"User {user_id} sent file: {file_name}")

# ========== CALLBACK HANDLERS ==========
@app.on_callback_query(filters.regex("^start_rename$"))
@private_access
async def start_rename_callback(client, callback_query):
    user_id = callback_query.from_user.id
    
    if user_id not in user_states:
        await callback_query.answer("Session expired! Send file again.", show_alert=True)
        return
    
    user_states[user_id]['step'] = 'awaiting_filename'
    
    try:
        await callback_query.message.delete()
    except:
        pass
    
    ask_msg = await callback_query.message.reply_text("**📝 Send new filename (without extension):**")
    user_states[user_id]['ask_message_id'] = ask_msg.id
    await callback_query.answer()

@app.on_callback_query(filters.regex("^upload_(document|video|audio)$"))
@private_access
async def upload_type_callback(client, callback_query):
    user_id = callback_query.from_user.id
    upload_type = callback_query.data.split("_")[1]
    
    if user_id not in user_states:
        await callback_query.answer("Session expired!", show_alert=True)
        return
    
    user_data = user_states[user_id]
    
    try:
        try:
            await callback_query.message.delete()
        except:
            pass
        
        file_info = user_data['file_info']
        new_filename = user_data['new_filename']
        original_message = file_info['original_message']
        original_duration = file_info['duration']
        original_file_type = file_info['file_type']
        
        # Get original extension
        original_name = file_info['file_name']
        if not original_name or original_name == 'Unknown':
            if original_file_type == 'video':
                original_ext = '.mp4'
            elif original_file_type == 'audio':
                original_ext = '.mp3'
            else:
                original_ext = '.bin'
        else:
            _, original_ext = os.path.splitext(original_name)
            if not original_ext:
                if original_file_type == 'video':
                    original_ext = '.mp4'
                elif original_file_type == 'audio':
                    original_ext = '.mp3'
                else:
                    original_ext = '.bin'
        
        final_filename = f"{new_filename}{original_ext}"
        download_path = f"downloads/{final_filename}"
        os.makedirs("downloads", exist_ok=True)
        
        # FIX: Force document upload type if user selects document, regardless of original type
        if upload_type == "document":
            # When user selects document, we'll force upload as document
            actual_upload_type = "document"
        else:
            # Otherwise use the selected type (video/audio)
            actual_upload_type = upload_type
        
        # Create progress message with cancel button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        progress_msg = await callback_query.message.reply_text("🔄 **Processing your file...**", reply_markup=keyboard)
        
        start_time = time.time()
        
        # Download with progress
        file_path = await client.download_media(
            original_message,
            file_name=download_path,
            progress=progress_for_pyrogram,
            progress_args=("📥 **Downloading File**", progress_msg, start_time, final_filename)
        )
        
        # Check if download was cancelled
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("✅ **Download cancelled!**")
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            await cleanup_user_data(user_id)
            return
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Download failed")
        
        user_states[user_id]['file_path'] = file_path
        
        # Get thumbnail
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
                user_states[user_id]['thumb_path'] = thumb_path
            except Exception as e:
                logger.error(f"Thumbnail download error: {e}")
        
        # Update progress message for upload
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        await progress_msg.edit_text("🔄 **Uploading file...**", reply_markup=keyboard)
        
        start_time = time.time()
        
        # Send to user based on selected upload type
        try:
            if actual_upload_type == "document":
                # FIX: Force upload as document regardless of original type
                sent_message = await client.send_document(
                    callback_query.message.chat.id,
                    document=file_path,
                    thumb=thumb_path,
                    caption=f"`{final_filename}`",
                    progress=progress_for_pyrogram,
                    progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
                )
            elif actual_upload_type == "video":
                sent_message = await client.send_video(
                    callback_query.message.chat.id,
                    video=file_path,
                    thumb=thumb_path,
                    caption=f"`{final_filename}`",
                    duration=original_duration,
                    supports_streaming=True,
                    progress=progress_for_pyrogram,
                    progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
                )
            elif actual_upload_type == "audio":
                sent_message = await client.send_audio(
                    callback_query.message.chat.id,
                    audio=file_path,
                    thumb=thumb_path,
                    caption=f"`{final_filename}`",
                    duration=original_duration,
                    progress=progress_for_pyrogram,
                    progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
                )
        except Exception as e:
            logger.error(f"Upload error: {e}")
            # Fallback to document if specific type fails
            sent_message = await client.send_document(
                callback_query.message.chat.id,
                document=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                progress=progress_for_pyrogram,
                progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
            )
        
        # Check if upload was cancelled
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("✅ **Upload cancelled!**")
            return
        
        # ✅ SEND TO DUMP CHANNEL (after successful upload to user)
        if Config.DUMP_CHANNEL:
            await send_to_dump_channel(
                client, 
                file_path, 
                final_filename, 
                user_id, 
                actual_upload_type,  # Use actual upload type for dump
                original_duration, 
                thumb_path
            )
        
        await callback_query.message.reply_text(f"✅ **File renamed successfully!**\n\n**New Name:** `{final_filename}`\n**Upload Type:** `{actual_upload_type.title()}`")
        logger.info(f"User {user_id} successfully renamed file to {final_filename} as {actual_upload_type}")
        
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"❌ **Error:** `{str(e)}`"
        await callback_query.message.reply_text(error_msg)
        logger.error(f"Upload error for user {user_id}: {e}")
    
    finally:
        # Cleanup
        await cleanup_user_data(user_id)

# ========== FILENAME HANDLER ==========
@app.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "view_thumb", "del_thumb", "addalloweduser", "removealloweduser", "allowedusers", "users", "mode", "dumpchannel", "backup"]))
@private_access
async def handle_filename(client, message):
    user_id = message.from_user.id
    
    if user_id not in user_states or user_states[user_id]['step'] != 'awaiting_filename':
        return
    
    new_name = message.text.strip()
    if not new_name:
        await message.reply_text("❌ **Filename cannot be empty**")
        return
    
    clean_name = sanitize_filename(new_name)
    if not clean_name:
        await message.reply_text("❌ **Invalid filename**")
        return
    
    user_states[user_id]['new_filename'] = clean_name
    user_states[user_id]['step'] = 'awaiting_upload_type'
    
    try:
        await message.delete()
    except:
        pass
    
    try:
        if 'ask_message_id' in user_states[user_id]:
            await client.delete_messages(message.chat.id, user_states[user_id]['ask_message_id'])
    except:
        pass
    
    # Get file extension
    file_info = user_states[user_id]['file_info']
    original_name = file_info['file_name']
    if not original_name or original_name == 'Unknown':
        if file_info['file_type'] == 'video':
            original_ext = '.mp4'
        elif file_info['file_type'] == 'audio':
            original_ext = '.mp3'
        else:
            original_ext = '.bin'
    else:
        _, original_ext = os.path.splitext(original_name)
        if not original_ext:
            if file_info['file_type'] == 'video':
                original_ext = '.mp4'
            elif file_info['file_type'] == 'audio':
                original_ext = '.mp3'
            else:
                original_ext = '.bin'
    
    final_name = f"{clean_name}{original_ext}"
    
    # Show appropriate upload options based on file type
    keyboard_buttons = []
    
    # Always show document option
    keyboard_buttons.append([InlineKeyboardButton("📄 Document", callback_data="upload_document")])
    
    # Show video option for video files
    if file_info['file_type'] == 'video':
        keyboard_buttons.append([InlineKeyboardButton("🎥 Video", callback_data="upload_video")])
    
    # Show audio option for audio files  
    if file_info['file_type'] == 'audio':
        keyboard_buttons.append([InlineKeyboardButton("🎵 Audio", callback_data="upload_audio")])
    
    keyboard = InlineKeyboardMarkup(keyboard_buttons)
    
    await message.reply_text(f"**Select Upload Type:**\n\n**File:** `{final_name}`", reply_markup=keyboard)

# ========== GRACEFUL SHUTDOWN ==========
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info("Received shutdown signal...")
    print("🛑 Shutting down bot gracefully...")
    app.stop()

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ========== START BOT ==========
if __name__ == "__main__":
    print("🚀 Bot starting...")
    
    # Initialize dump channel from database
    asyncio.get_event_loop().run_until_complete(initialize_dump_channel())
    
    # Test database connection
    if asyncio.get_event_loop().run_until_complete(db.test_connection()):
        print("✅ Database connection successful")
    else:
        print("❌ Database connection failed")
        exit(1)
    
    if Config.DUMP_CHANNEL:
        print(f"📦 Dump Channel: {Config.DUMP_CHANNEL}")
    else:
        print("📦 Dump Channel: Disabled")
    
    print("🤖 Bot is running...")
    app.run()
