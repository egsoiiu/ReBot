import os
import asyncio
import logging
import math
import time
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import QueryIdInvalid, MessageNotModified, FloodWait
import motor.motor_asyncio

# ========== CONFIG ==========
class Config:
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    DB_URL = os.environ.get("DB_URL", "")
    DB_NAME = "RenameBot"
    
    # Fix owner IDs parsing
    owner_ids_str = os.environ.get("OWNER_IDS", "0")
    OWNER_IDS = []
    for x in owner_ids_str.split(","):
        x = x.strip()
        if x and x.isdigit():
            OWNER_IDS.append(int(x))
    
    # If no owner IDs set, add your ID manually
    if not OWNER_IDS:
        OWNER_IDS = [123456789]  # Replace with your actual Telegram ID

# ========== SIMPLE HTTP SERVER ==========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    server.serve_forever()

health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

# ========== UTILITY FUNCTIONS ==========
def humanbytes(size):    
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
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

def get_file_icon(file_type):
    icons = {
        'document': '📄',
        'video': '🎥', 
        'audio': '🎵',
        'image': '🖼️'
    }
    return icons.get(file_type, '📁')

# ========== DATABASE CLASS ==========
class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.allowed_users = self.db.allowed_users
        self.settings = self.db.settings

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
        # Check if user is owner first
        if user_id in Config.OWNER_IDS:
            return True
        # Then check allowed users database
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

# Initialize database
db = Database(Config.DB_URL, Config.DB_NAME)

# ========== BOT SETUP ==========
app = Client("rename_bot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)

# ========== GLOBAL VARIABLES ==========
user_states = {}
cancellation_flags = {}

# ========== ACCESS CONTROL ==========
def private_access(func):
    async def wrapper(client, message):
        user_id = message.from_user.id
        
        # Always allow owners
        if user_id in Config.OWNER_IDS:
            return await func(client, message)
        
        private_mode = await db.get_private_mode()
        
        if not private_mode:
            return await func(client, message)
        
        if await db.is_allowed_user(user_id):
            return await func(client, message)
        else:
            await message.reply_text("🚫 **This bot is in private mode. Contact owner for access.**")
            return  # Stop execution
    return wrapper

def main_owner_only(func):
    async def wrapper(client, message):
        if message.from_user.id in Config.OWNER_IDS:
            return await func(client, message)
        else:
            await message.reply_text("🚫 **Owner only command.**")
            return
    return wrapper

# ========== PROGRESS WITH CANCEL BUTTON ==========
async def progress_for_pyrogram(current, total, ud_type, message, start, filename, user_id):
    """Progress callback with persistent cancel button"""
    
    # INSTANT cancellation check
    if user_id in cancellation_flags and cancellation_flags[user_id]:
        raise Exception("OPERATION_CANCELLED")
    
    now = time.time()
    diff = now - start
    
    # Update every 2 seconds or when complete
    if round(diff % 2.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000 if speed > 0 else 0
        estimated_total_time = TimeFormatter(milliseconds=elapsed_time + time_to_completion)

        progress_bar = "▣" * math.floor(percentage / 10) + "□" * (10 - math.floor(percentage / 10))
        
        progress_text = f"""
{ud_type}

📄 **File:** `{filename}`

[{progress_bar}] **{round(percentage, 1)}%**

💾 **Size:** {humanbytes(current)} / {humanbytes(total)}
🚀 **Speed:** {humanbytes(speed)}/s
⏰ **ETA:** {estimated_total_time if estimated_total_time != '' else '0s'}
"""
        # ALWAYS include cancel button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        
        try:
            await message.edit(text=progress_text, reply_markup=keyboard)
        except MessageNotModified:
            pass
        except Exception:
            pass

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
    
    text = """👋 **File Rename Bot**

Send any file to rename it with custom name.

**Basic Commands:**
• /view_thumb - View your thumbnail
• /del_thumb - Delete thumbnail  
• /cancel - Cancel current process
• /help - Show help guide"""
    
    if is_owner:
        mode = "PRIVATE" if private_mode else "PUBLIC"
        text += f"\n\n**Owner Commands:**\n• /addalloweduser ID\n• /removealloweduser ID\n• /allowedusers\n• /users\n• /mode private|public\n• **Current Mode:** {mode}"
    
    await message.reply_text(text)

@app.on_message(filters.private & filters.command("help"))
async def help_command(client, message):
    help_text = """
🤖 **File Rename Bot - Help Guide**

**How to Use:**
1. **Send any file** (document, video, audio, image)
2. **Click 'Rename'** button in file preview
3. **Enter new filename** (without extension)
4. **Choose upload type** (Document/Video)
5. **Wait for processing** - You can cancel anytime!

**Available Commands:**
• /start - Start the bot
• /view_thumb - View your thumbnail
• /del_thumb - Delete thumbnail
• /cancel - Cancel current operation
• /help - Show this help message

**Features:**
✅ Custom filenames
✅ Persistent thumbnails  
✅ Progress tracking
✅ Instant cancellation
✅ Multiple file types
"""
    await message.reply_text(help_text)

@app.on_message(filters.private & filters.command("debug"))
@main_owner_only
async def debug_command(client, message):
    user_id = message.from_user.id
    private_mode = await db.get_private_mode()
    is_allowed = await db.is_allowed_user(user_id)
    is_owner = user_id in Config.OWNER_IDS
    
    debug_text = f"""
🔍 **Debug Information**

**User ID:** `{user_id}`
**Is Owner:** `{is_owner}`
**Private Mode:** `{private_mode}`
**Is Allowed:** `{is_allowed}`
**Active Sessions:** `{len(user_states)}`
**Owner IDs:** `{Config.OWNER_IDS}`
"""
    await message.reply_text(debug_text)

@app.on_message(filters.private & filters.command("addalloweduser"))
@main_owner_only
async def add_allowed_user_command(client, message):
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/addalloweduser USER_ID`\n\nExample: `/addalloweduser 123456789`")
        return
    
    try:
        user_id = int(message.command[1])
        await db.add_allowed_user(user_id)
        await message.reply_text(f"✅ **User {user_id} added to allowed list**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID**\nPlease provide a valid numeric ID.")

@app.on_message(filters.private & filters.command("removealloweduser"))
@main_owner_only
async def remove_allowed_user_command(client, message):
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/removealloweduser USER_ID`\n\nExample: `/removealloweduser 123456789`")
        return
    
    try:
        user_id = int(message.command[1])
        await db.remove_allowed_user(user_id)
        await message.reply_text(f"✅ **User {user_id} removed from allowed list**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID**\nPlease provide a valid numeric ID.")

@app.on_message(filters.private & filters.command("allowedusers"))
@main_owner_only
async def list_allowed_users_command(client, message):
    users = await db.get_all_allowed_users()
    if not users:
        await message.reply_text("**No allowed users**")
        return
    
    text = "**👥 Allowed Users:**\n\n"
    for user in users:
        text += f"• `{user['id']}`\n"
    
    await message.reply_text(text)

@app.on_message(filters.private & filters.command("users"))
@main_owner_only
async def list_users_command(client, message):
    users = await db.get_all_users()
    text = f"**📊 Total Users:** {len(users)}\n\n"
    for user_id in users[:20]:  # Show first 20 users
        text += f"• `{user_id}`\n"
    
    if len(users) > 20:
        text += f"\n... and {len(users) - 20} more users"
    
    await message.reply_text(text)

@app.on_message(filters.private & filters.command("mode"))
@main_owner_only
async def mode_command(client, message):
    if len(message.command) < 2:
        mode = await db.get_private_mode()
        status = "🔒 PRIVATE" if mode else "🌐 PUBLIC"
        await message.reply_text(f"**Current Mode:** {status}")
        return
    
    mode = message.command[1].lower()
    if mode in ["private", "true", "1"]:
        await db.set_private_mode(True)
        await message.reply_text("✅ **Private Mode ON**\n\nOnly allowed users can use the bot.")
    else:
        await db.set_private_mode(False)
        await message.reply_text("✅ **Public Mode ON**\n\nEveryone can use the bot.")

@app.on_message(filters.private & filters.command("cancel"))
@private_access
async def cancel_command(client, message):
    user_id = message.from_user.id
    
    if user_id not in user_states:
        await message.reply_text("ℹ️ **No active process to cancel.**")
        return
    
    # Set cancellation flag
    cancellation_flags[user_id] = True
    
    # Cleanup files and state
    if user_id in user_states:
        user_data = user_states[user_id]
        if user_data.get('file_path') and os.path.exists(user_data['file_path']):
            try: 
                os.remove(user_data['file_path'])
            except: 
                pass
        if user_data.get('thumb_path') and os.path.exists(user_data['thumb_path']):
            try: 
                os.remove(user_data['thumb_path'])
            except: 
                pass
        del user_states[user_id]
    
    # Clear cancellation flag
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
    await message.reply_text("✅ **Process cancelled successfully!**")

# ========== CANCEL CALLBACK HANDLERS ==========
@app.on_callback_query(filters.regex(r"^cancel_confirm_(\d+)$"))
async def cancel_confirm_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("❌ You can only cancel your own processes!", show_alert=True)
        return
    
    # Set flag to prevent progress updates from overwriting
    if user_id in user_states:
        user_states[user_id]['showing_cancel_confirm'] = True
    
    # Show confirmation buttons
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Cancel", callback_data=f"cancel_yes_{user_id}")],
        [InlineKeyboardButton("❌ No, Continue", callback_data=f"cancel_no_{user_id}")]
    ])
    
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("Are you sure you want to cancel?")
    except Exception as e:
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
    if user_id in user_states:
        user_data = user_states[user_id]
        if user_data.get('file_path') and os.path.exists(user_data['file_path']):
            try: os.remove(user_data['file_path'])
            except: pass
        if user_data.get('thumb_path') and os.path.exists(user_data['thumb_path']):
            try: os.remove(user_data['thumb_path'])
            except: pass
        del user_states[user_id]
    
    # Clear flags
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
    await callback_query.message.edit_text("✅ **Process cancelled successfully!**")
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^cancel_no_(\d+)$"))
async def cancel_no_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("❌ Access denied!", show_alert=True)
        return
    
    # Clear cancellation flag and confirmation flag
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
    if user_id in user_states:
        user_states[user_id]['showing_cancel_confirm'] = False
    
    # Restore original cancel button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
    ])
    
    try:
        # Get current message text and add back cancel button
        current_text = callback_query.message.text
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("✅ Process continued")
    except Exception as e:
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
        await message.reply_text("**No thumbnail set**\n\nSend a photo to set as thumbnail.")

@app.on_message(filters.private & filters.command(["del_thumb", "deletethumbnail"]))
@private_access
async def delete_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, None)
    await message.reply_text("✅ **Thumbnail deleted successfully**")

@app.on_message(filters.private & filters.photo)
@private_access
async def save_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, message.photo.file_id)
    await message.reply_text("✅ **Thumbnail saved successfully**\n\nIt will be used for your future uploads.")

# ========== FILE HANDLING ==========
@app.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.photo))
@private_access
async def handle_file(client, message):
    user_id = message.from_user.id
    
    if user_id in user_states:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_confirm_{user_id}")]
        ])
        await message.reply_text(
            "⚠️ **You have an ongoing operation!**\n\nPlease complete or cancel it before sending a new file.",
            reply_markup=keyboard
        )
        return
    
    # Clear any previous cancellation flags
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
    elif message.photo:
        file = message.photo
        file_type = "image"
        duration = 0
    else:
        return

    file_name = getattr(file, 'file_name', 'Unknown')
    file_size = humanbytes(file.file_size)
    file_icon = get_file_icon(file_type)
    
    user_states[user_id] = {
        'file_info': {
            'file_name': file_name,
            'file_size': file_size,
            'file_type': file_type,
            'duration': duration,
            'original_message': message,
            'file_id': file.file_id
        },
        'step': 'awaiting_rename',
        'start_time': time.time()
    }

    duration_text = convert_seconds(duration) if duration > 0 else "N/A"
    
    # Create file preview with buttons
    info_text = f"""**{file_icon} File Received**

**📝 Name:** `{file_name}`
**📦 Size:** `{file_size}`
**🎯 Type:** `{file_type.title()}`
**⏱️ Duration:** `{duration_text}`

Click **Rename** to set custom filename."""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rename", callback_data="start_rename")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
    ])
    
    await message.reply_text(info_text, reply_markup=keyboard)

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
    
    ask_msg = await callback_query.message.reply_text(
        "**📝 Enter new filename (without extension):**\n\n"
        "Example: `my_custom_file`\n\n"
        "Type /cancel to abort."
    )
    user_states[user_id]['ask_message_id'] = ask_msg.id
    await callback_query.answer()

@app.on_callback_query(filters.regex("^upload_(document|video)$"))
@private_access
async def upload_type_callback(client, callback_query):
    user_id = callback_query.from_user.id
    upload_type = callback_query.data.split("_")[1]
    
    if user_id not in user_states:
        await callback_query.answer("Session expired!", show_alert=True)
        return
    
    user_data = user_states[user_id]
    
    try:
        # Delete previous messages
        try:
            await callback_query.message.delete()
        except:
            pass
        
        file_info = user_data['file_info']
        new_filename = user_data['new_filename']
        original_message = file_info['original_message']
        original_duration = file_info['duration']
        
        # Get original extension
        original_name = file_info['file_name']
        if not original_name or original_name == 'Unknown':
            if file_info['file_type'] == 'video':
                original_ext = '.mp4'
            elif file_info['file_type'] == 'audio':
                original_ext = '.mp3'
            elif file_info['file_type'] == 'image':
                original_ext = '.jpg'
            else:
                original_ext = '.bin'
        else:
            _, original_ext = os.path.splitext(original_name)
            if not original_ext:
                if file_info['file_type'] == 'video':
                    original_ext = '.mp4'
                elif file_info['file_type'] == 'audio':
                    original_ext = '.mp3'
                elif file_info['file_type'] == 'image':
                    original_ext = '.jpg'
                else:
                    original_ext = '.bin'
        
        final_filename = f"{new_filename}{original_ext}"
        download_path = f"downloads/{user_id}_{final_filename}"
        os.makedirs("downloads", exist_ok=True)
        
        # Create progress message with cancel button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        progress_msg = await callback_query.message.reply_text(
            f"**🔄 Processing your file...**\n\n"
            f"**File:** `{final_filename}`\n"
            f"**Type:** {upload_type.title()}\n\n"
            f"Starting download...",
            reply_markup=keyboard
        )
        
        start_time = time.time()
        
        try:
            # Download with progress
            file_path = await client.download_media(
                original_message,
                file_name=download_path,
                progress=progress_for_pyrogram,
                progress_args=("📥 **Downloading File**", progress_msg, start_time, final_filename, user_id)
            )
            
            # Check if download was cancelled
            if user_id in cancellation_flags and cancellation_flags[user_id]:
                await progress_msg.edit_text("✅ **Download cancelled!**")
                if file_path and os.path.exists(file_path):
                    try: os.remove(file_path)
                    except: pass
                if user_id in user_states: del user_states[user_id]
                if user_id in cancellation_flags: del cancellation_flags[user_id]
                return
            
            if not file_path or not os.path.exists(file_path):
                raise Exception("Download failed")
            
            user_states[user_id]['file_path'] = file_path
            
            # Get thumbnail
            thumbnail = await db.get_thumbnail(user_id)
            thumb_path = None
            if thumbnail:
                try:
                    thumb_path = await client.download_media(thumbnail, file_name=f"downloads/{user_id}_thumb.jpg")
                    user_states[user_id]['thumb_path'] = thumb_path
                except:
                    pass
            
            # Update progress message for upload
            await progress_msg.edit_text(
                f"**🔄 Uploading file...**\n\n"
                f"**File:** `{final_filename}`\n"
                f"**Type:** {upload_type.title()}\n\n"
                f"Starting upload...",
                reply_markup=keyboard
            )
            
            start_time = time.time()
            
            # Upload file based on type
            if upload_type == "document" or file_info['file_type'] in ['document', 'audio']:
                await client.send_document(
                    callback_query.message.chat.id,
                    document=file_path,
                    thumb=thumb_path,
                    caption=f"`{final_filename}`",
                    progress=progress_for_pyrogram,
                    progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename, user_id)
                )
            else:
                await client.send_video(
                    callback_query.message.chat.id,
                    video=file_path,
                    thumb=thumb_path,
                    caption=f"`{final_filename}`",
                    duration=original_duration,
                    supports_streaming=True,
                    progress=progress_for_pyrogram,
                    progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename, user_id)
                )
            
            # Check if upload was cancelled
            if user_id in cancellation_flags and cancellation_flags[user_id]:
                await progress_msg.edit_text("✅ **Upload cancelled!**")
                return
            
            # Success message
            await callback_query.message.reply_text(
                f"✅ **File renamed successfully!**\n\n"
                f"**Original:** `{file_info['file_name']}`\n"
                f"**New Name:** `{final_filename}`\n"
                f"**Type:** {upload_type.title()}"
            )
            
            try:
                await progress_msg.delete()
            except:
                pass
                
        except Exception as e:
            if "OPERATION_CANCELLED" in str(e):
                await progress_msg.edit_text("✅ **Operation cancelled successfully!**")
            else:
                await progress_msg.edit_text(f"❌ **Error:** `{str(e)}`")
            return
            
    except Exception as e:
        await callback_query.message.reply_text(f"❌ **Unexpected Error:** `{str(e)}`")
    
    finally:
        # Cleanup
        if user_id in user_states:
            user_data = user_states[user_id]
            if user_data.get('file_path') and os.path.exists(user_data['file_path']):
                try: os.remove(user_data['file_path'])
                except: pass
            if user_data.get('thumb_path') and os.path.exists(user_data['thumb_path']):
                try: os.remove(user_data['thumb_path'])
                except: pass
            del user_states[user_id]
        
        # Clear cancellation flag
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]

# ========== FILENAME HANDLER ==========
@app.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "view_thumb", "del_thumb", "addalloweduser", "removealloweduser", "allowedusers", "users", "mode", "help", "debug"]))
@private_access
async def handle_filename(client, message):
    user_id = message.from_user.id
    
    if user_id not in user_states or user_states[user_id]['step'] != 'awaiting_filename':
        return
    
    new_name = message.text.strip()
    if not new_name:
        await message.reply_text("❌ **Filename cannot be empty**\n\nPlease enter a valid filename:")
        return
    
    # Clean filename
    clean_name = re.sub(r'[<>:"/\\|?*]', '', new_name)
    if not clean_name:
        await message.reply_text("❌ **Invalid filename**\n\nPlease use only allowed characters:")
        return
    
    user_states[user_id]['new_filename'] = clean_name
    user_states[user_id]['step'] = 'awaiting_upload_type'
    
    # Cleanup previous messages
    try:
        await message.delete()
    except:
        pass
    
    try:
        if 'ask_message_id' in user_states[user_id]:
            await client.delete_messages(message.chat.id, user_states[user_id]['ask_message_id'])
    except:
        pass
    
    # Get file extension and show upload options
    file_info = user_states[user_id]['file_info']
    original_name = file_info['file_name']
    
    if not original_name or original_name == 'Unknown':
        if file_info['file_type'] == 'video':
            original_ext = '.mp4'
        elif file_info['file_type'] == 'audio':
            original_ext = '.mp3'
        elif file_info['file_type'] == 'image':
            original_ext = '.jpg'
        else:
            original_ext = '.bin'
    else:
        _, original_ext = os.path.splitext(original_name)
        if not original_ext:
            if file_info['file_type'] == 'video':
                original_ext = '.mp4'
            elif file_info['file_type'] == 'audio':
                original_ext = '.mp3'
            elif file_info['file_type'] == 'image':
                original_ext = '.jpg'
            else:
                original_ext = '.bin'
    
    final_name = f"{clean_name}{original_ext}"
    
    # Auto select for documents and audio
    if file_info['file_type'] in ['document', 'audio']:
        # Automatically proceed with document upload
        user_states[user_id]['step'] = 'processing'
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Upload as Document", callback_data="upload_document")]
        ])
        await message.reply_text(
            f"**✅ Ready to Upload**\n\n"
            f"**File:** `{final_name}`\n"
            f"**Auto-selected:** Document format\n\n"
            f"Click below to start:",
            reply_markup=keyboard
        )
        return
    
    # Show upload options for videos and images
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Document", callback_data="upload_document")],
        [InlineKeyboardButton("🎥 Video", callback_data="upload_video")]
    ])
    
    await message.reply_text(
        f"**📤 Select Upload Type**\n\n"
        f"**File:** `{final_name}`\n"
        f"**Original Type:** {file_info['file_type'].title()}\n\n"
        f"Choose how you want to upload:",
        reply_markup=keyboard
    )

# ========== SESSION CLEANUP ==========
async def cleanup_old_sessions():
    """Clean up old user sessions"""
    current_time = time.time()
    expired_users = []
    
    for user_id, session_data in user_states.items():
        if current_time - session_data.get('start_time', 0) > 1800:  # 30 minutes
            expired_users.append(user_id)
    
    for user_id in expired_users:
        if user_id in user_states:
            # Cleanup files
            user_data = user_states[user_id]
            if user_data.get('file_path') and os.path.exists(user_data['file_path']):
                try: os.remove(user_data['file_path'])
                except: pass
            if user_data.get('thumb_path') and os.path.exists(user_data['thumb_path']):
                try: os.remove(user_data['thumb_path'])
                except: pass
            del user_states[user_id]
        
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]

# ========== START BOT ==========
if __name__ == "__main__":
    print("🚀 Bot starting...")
    
    # Create downloads directory
    os.makedirs("downloads", exist_ok=True)
    
    
    app.run()
