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
from pyrogram.errors import QueryIdInvalid, MessageNotModified
import motor.motor_asyncio

# ========== CONFIG ==========
class Config:
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    DB_URL = os.environ.get("DB_URL", "")
    DB_NAME = "RenameBot"
    # Main owner (your Telegram ID) - only this user can manage allowed users
    OWNER_IDS = [int(x.strip()) for x in os.environ.get("OWNER_IDS", "0").split(",") if x.strip().isdigit()]

# ========== SIMPLE HTTP SERVER FOR RENDER PORT ==========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running!')
    
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    print("🌐 Health check server running on port 8080")
    server.serve_forever()

# Start health server in background
health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

# ========== UTILITY FUNCTIONS ==========
async def progress_for_pyrogram(current, total, ud_type, message, start, filename, user_id):
    now = time.time()
    diff = now - start
    if round(diff % 2.00) == 0 or current == total:  # Update every 2 seconds
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000 if speed > 0 else 0
        estimated_total_time = elapsed_time + time_to_completion

        elapsed_time = TimeFormatter(milliseconds=elapsed_time)
        estimated_total_time = TimeFormatter(milliseconds=estimated_total_time)

        # Progress bar with 10 blocks
        filled_blocks = math.floor(percentage / 10)
        empty_blocks = 10 - filled_blocks
        progress_bar = "▣" * filled_blocks + "□" * empty_blocks
        
        progress_text = f"""
{ud_type}

📄 **File:** `{filename}`

[{progress_bar}] {round(percentage, 1)}%

💾 **Size:** {humanbytes(current)} / {humanbytes(total)}

🚀 **Speed:** {humanbytes(speed)}/s

⏰ **ETA:** {estimated_total_time if estimated_total_time != '' else '0s'}
"""
        
        # Add cancel button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{user_id}")]
        ])
        
        try:
            await message.edit(text=progress_text, reply_markup=keyboard)
        except MessageNotModified:
            pass
        except Exception:
            pass

def humanbytes(size):    
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    Dic_powerN = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power and n < len(Dic_powerN) - 1:
        size /= power
        n += 1
    return f"{size:.1f} {Dic_powerN[n]}"

def TimeFormatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

def convert_seconds(seconds):
    """Convert seconds to MM:SS or HH:MM:SS format"""
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

# Simple thumbnail processing without PIL
async def process_thumb_async(ph_path):
    """Simple thumbnail pass-through without PIL"""
    pass

# ========== DATABASE CLASS ==========
class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.allowed_users = self.db.allowed_users
        self.settings = self.db.settings

    async def set_thumbnail(self, user_id, file_id):
        await self.col.update_one(
            {"_id": user_id},
            {"$set": {"file_id": file_id}},
            upsert=True
        )

    async def get_thumbnail(self, user_id):
        user = await self.col.find_one({"_id": user_id})
        return user.get("file_id") if user else None

    # Allowed users management methods
    async def add_allowed_user(self, user_id):
        await self.allowed_users.update_one(
            {"_id": user_id},
            {"$set": {"added_at": time.time()}},
            upsert=True
        )

    async def remove_allowed_user(self, user_id):
        await self.allowed_users.delete_one({"_id": user_id})

    async def is_allowed_user(self, user_id):
        # Check if user is main owner
        if user_id in Config.OWNER_IDS:
            return True
        # Check database allowed users
        allowed_user = await self.allowed_users.find_one({"_id": user_id})
        return allowed_user is not None

    async def get_all_allowed_users(self):
        allowed_users = []
        # Only return database allowed users (not main owners)
        async for user in self.allowed_users.find():
            allowed_users.append({"id": user["_id"], "added_at": user.get("added_at", 0)})
        return allowed_users

    async def get_all_users(self):
        users = []
        async for user in self.col.find():
            users.append(user["_id"])
        return users

    # Settings management (for private mode)
    async def get_private_mode(self):
        """Get private mode from database, default to False if not set"""
        setting = await self.settings.find_one({"_id": "private_mode"})
        return setting.get("value") if setting else False

    async def set_private_mode(self, value):
        """Set private mode in database"""
        await self.settings.update_one(
            {"_id": "private_mode"},
            {"$set": {"value": value}},
            upsert=True
        )

# Initialize database
db = Database(Config.DB_URL, Config.DB_NAME)

# ========== BOT SETUP ==========
if not all([Config.API_ID, Config.API_HASH, Config.BOT_TOKEN]):
    print("❌ ERROR: Missing API credentials! Please set environment variables.")
    print(f"API_ID: {'✅' if Config.API_ID else '❌'}")
    print(f"API_HASH: {'✅' if Config.API_HASH else '❌'}")
    print(f"BOT_TOKEN: {'✅' if Config.BOT_TOKEN else '❌'}")
    exit(1)

app = Client(
    "rename_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN
)

# ========== GLOBAL VARIABLES ==========
user_states = {}
cancellation_flags = {}

# ========== ACCESS CONTROL DECORATOR ==========
def private_access(func):
    async def wrapper(client, message):
        user_id = message.from_user.id
        
        # Get current private mode from database
        private_mode = await db.get_private_mode()
        
        # If private mode is disabled, allow all users
        if not private_mode:
            return await func(client, message)
        
        # Check if user is allowed
        if await db.is_allowed_user(user_id):
            return await func(client, message)
        else:
            await message.reply_text(
                "**🚫 Access Denied**\n\n"
                "This bot is currently in private mode and can only be used by authorized users."
            )
            return
    
    return wrapper

# ========== MAIN OWNER CHECK DECORATOR ==========
def main_owner_only(func):
    async def wrapper(client, message):
        user_id = message.from_user.id
        
        # Check if user is main owner
        if user_id in Config.OWNER_IDS:
            return await func(client, message)
        else:
            await message.reply_text("**🚫 This command is only for main owners.**")
            return
    
    return wrapper

# ========== ALLOWED USER MANAGEMENT COMMANDS ==========
@app.on_message(filters.private & filters.command("addalloweduser"))
@main_owner_only
async def add_allowed_user_command(client, message):
    user_id = message.from_user.id
    
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/addalloweduser <user_id>`")
        return
    
    try:
        new_user_id = int(message.command[1])
        await db.add_allowed_user(new_user_id)
        await message.reply_text(f"**✅ User `{new_user_id}` added as allowed user.**")
    except ValueError:
        await message.reply_text("**❌ Invalid user ID. Please provide a numeric ID.**")
    except Exception as e:
        await message.reply_text(f"**❌ Error:** `{str(e)}`")

@app.on_message(filters.private & filters.command("removealloweduser"))
@main_owner_only
async def remove_allowed_user_command(client, message):
    user_id = message.from_user.id
    
    if len(message.command) < 2:
        await message.reply_text("**Usage:** `/removealloweduser <user_id>`")
        return
    
    try:
        remove_user_id = int(message.command[1])
        await db.remove_allowed_user(remove_user_id)
        await message.reply_text(f"**✅ User `{remove_user_id}` removed from allowed users.**")
    except ValueError:
        await message.reply_text("**❌ Invalid user ID. Please provide a numeric ID.**")
    except Exception as e:
        await message.reply_text(f"**❌ Error:** `{str(e)}`")

@app.on_message(filters.private & filters.command("allowedusers"))
@main_owner_only
async def list_allowed_users_command(client, message):
    user_id = message.from_user.id
    
    allowed_users = await db.get_all_allowed_users()
    
    if not allowed_users:
        await message.reply_text("**No allowed users found.**")
        return
    
    allowed_users_text = "**👥 Allowed Users:**\n\n"
    for user in allowed_users:
        added_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(user["added_at"]))
        allowed_users_text += f"`{user['id']}` - Added: `{added_time}`\n"
    
    await message.reply_text(allowed_users_text)

@app.on_message(filters.private & filters.command("users"))
@main_owner_only
async def list_users_command(client, message):
    user_id = message.from_user.id
    
    users = await db.get_all_users()
    
    if not users:
        await message.reply_text("**No users found in database.**")
        return
    
    users_text = f"**👥 Total Users: {len(users)}**\n\n"
    for user_id in users[:20]:  # Show first 20 users
        users_text += f"`{user_id}`\n"
    
    if len(users) > 20:
        users_text += f"\n... and {len(users) - 20} more users."
    
    await message.reply_text(users_text)

@app.on_message(filters.private & filters.command("mode"))
@main_owner_only
async def mode_command(client, message):
    user_id = message.from_user.id
    
    if len(message.command) < 2:
        current_mode = await db.get_private_mode()
        mode_text = "PRIVATE" if current_mode else "PUBLIC"
        await message.reply_text(
            f"**🔒 Current Mode:** `{mode_text}`\n\n"
            "**Usage:** `/mode <private|public>`\n"
            "• `private` - Only allowed users can use the bot\n"
            "• `public` - Anyone can use the bot"
        )
        return
    
    mode = message.command[1].lower()
    if mode in ["private", "true", "1"]:
        await db.set_private_mode(True)
        await message.reply_text("**✅ Bot mode set to PRIVATE**\nOnly allowed users can use the bot.")
    elif mode in ["public", "false", "0"]:
        await db.set_private_mode(False)
        await message.reply_text("**✅ Bot mode set to PUBLIC**\nAnyone can use the bot.")
    else:
        await message.reply_text("**❌ Invalid mode. Use `private` or `public`**")

# ========== CANCEL COMMAND ==========
@app.on_message(filters.private & filters.command("cancel"))
@private_access
async def cancel_command(client, message):
    user_id = message.from_user.id
    
    # Set cancellation flag
    cancellation_flags[user_id] = True
    
    # Clear user state and cleanup files
    if user_id in user_states:
        # Cleanup downloaded files
        user_data = user_states[user_id]
        if 'file_path' in user_data and user_data['file_path'] and os.path.exists(user_data['file_path']):
            try:
                os.remove(user_data['file_path'])
            except:
                pass
        if 'thumb_path' in user_data and user_data['thumb_path'] and os.path.exists(user_data['thumb_path']):
            try:
                os.remove(user_data['thumb_path'])
            except:
                pass
        
        # Clear user state
        del user_states[user_id]
    
    await message.reply_text("**✅ Process cancelled successfully! All files cleared.**")

# ========== CANCEL CALLBACK HANDLER ==========
@app.on_callback_query(filters.regex(r"^cancel_(\d+)$"))
async def cancel_callback_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    # Check if the user is cancelling their own process
    if user_id != target_user_id:
        await callback_query.answer("You can only cancel your own processes!", show_alert=True)
        return
    
    # Show confirmation buttons
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Cancel", callback_data=f"confirm_cancel_{user_id}")],
        [InlineKeyboardButton("❌ No, Continue", callback_data=f"deny_cancel_{user_id}")]
    ])
    
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("Are you sure you want to cancel?")
    except Exception as e:
        await callback_query.answer("Error updating message", show_alert=True)

@app.on_callback_query(filters.regex(r"^confirm_cancel_(\d+)$"))
async def confirm_cancel_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("You can only cancel your own processes!", show_alert=True)
        return
    
    # Set cancellation flag
    cancellation_flags[user_id] = True
    
    # Cleanup files and clear state
    if user_id in user_states:
        user_data = user_states[user_id]
        if 'file_path' in user_data and user_data['file_path'] and os.path.exists(user_data['file_path']):
            try:
                os.remove(user_data['file_path'])
            except:
                pass
        if 'thumb_path' in user_data and user_data['thumb_path'] and os.path.exists(user_data['thumb_path']):
            try:
                os.remove(user_data['thumb_path'])
            except:
                pass
        del user_states[user_id]
    
    await callback_query.message.edit_text("**✅ Process cancelled successfully! All files cleared.**")
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^deny_cancel_(\d+)$"))
async def deny_cancel_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("Access denied!", show_alert=True)
        return
    
    # Clear cancellation flag
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
    # Restore cancel button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{user_id}")]
    ])
    
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("Process continued")
    except Exception as e:
        await callback_query.answer("Error updating message", show_alert=True)

# ========== THUMBNAIL MANAGEMENT ==========
@app.on_message(filters.private & filters.command(["view_thumb", "viewthumbnail"]))
@private_access
async def view_thumbnail(client, message):
    thumbnail = await db.get_thumbnail(message.from_user.id)
    if thumbnail:
        await client.send_photo(message.chat.id, thumbnail, caption="**Your Current Thumbnail**")
    else:
        await message.reply_text("**You don't have any thumbnail set.**")

@app.on_message(filters.private & filters.command(["del_thumb", "deletethumbnail"]))
@private_access
async def delete_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, None)
    await message.reply_text("**Thumbnail deleted successfully!**")

@app.on_message(filters.private & filters.photo)
@private_access
async def save_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, message.photo.file_id)
    await message.reply_text("**Thumbnail saved successfully!**")

# ========== START COMMAND ==========
@app.on_message(filters.private & filters.command("start"))
async def start_command(client, message):
    user_id = message.from_user.id
    
    # Get current private mode from database
    private_mode = await db.get_private_mode()
    
    # Check access for private mode
    if private_mode and not await db.is_allowed_user(user_id):
        await message.reply_text(
            "**🚫 Access Denied**\n\n"
            "This bot is currently in private mode and can only be used by authorized users."
        )
        return
    
    is_main_owner = user_id in Config.OWNER_IDS
    is_allowed_user = await db.is_allowed_user(user_id)
    
    welcome_text = "**👋 Hello! I'm a File Rename Bot**\n\n"
    welcome_text += "**How to use:**\n"
    welcome_text += "1. Send me any file (document/video/audio)\n"
    welcome_text += "2. Click 'Rename' button\n"
    welcome_text += "3. Enter new filename\n"
    welcome_text += "4. Select upload type\n\n"
    welcome_text += "**Thumbnail Commands:**\n"
    welcome_text += "• Send a photo to set thumbnail\n"
    welcome_text += "• /view_thumb - View current thumbnail\n"
    welcome_text += "• /del_thumb - Delete thumbnail\n\n"
    welcome_text += "**Other Commands:**\n"
    welcome_text += "• /cancel - Cancel current process and clear files"
    
    # Add owner commands if user is main owner
    if is_main_owner:
        current_mode = await db.get_private_mode()
        mode_text = "PRIVATE" if current_mode else "PUBLIC"
        welcome_text += "\n\n**👑 Main Owner Commands:**\n"
        welcome_text += "• /addalloweduser <id> - Add allowed user\n"
        welcome_text += "• /removealloweduser <id> - Remove allowed user\n"
        welcome_text += "• /allowedusers - List all allowed users\n"
        welcome_text += "• /users - List all users\n"
        welcome_text += "• /mode <private|public> - Change bot mode\n"
        welcome_text += f"• **Current Mode:** `{mode_text}`"
    
    await message.reply_text(welcome_text)

# ========== FILE RENAME HANDLER ==========
@app.on_message(filters.private & (filters.document | filters.video | filters.audio))
@private_access
async def handle_file(client, message):
    user_id = message.from_user.id
    
    # Block if user already has active process
    if user_id in user_states:
        await message.reply_text("**❌ Please complete your current process first!**\nUse /cancel to cancel.")
        return
    
    # Clear any previous cancellation flag
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

    file_name = getattr(file, 'file_name', 'Unknown')
    file_size = humanbytes(getattr(file, 'file_size', 0))
    
    # Store file info with duration
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
        'file_path': None,
        'thumb_path': None
    }

    # Show file info with buttons - include duration
    duration_text = convert_seconds(duration) if duration > 0 else "Not available"
    
    info_text = f"""**📁 File Information:**

**Name:** `{file_name}`
**Size:** `{file_size}`
**Type:** `{file_type.title()}`
**Duration:** `{duration_text}`

**Click RENAME to continue.**"""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rename", callback_data="start_rename")]
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
    
    # Delete the rename prompt message
    try:
        await callback_query.message.delete()
    except:
        pass
    
    # Ask for filename directly without buttons
    ask_msg = await callback_query.message.reply_text(
        "**📝 Please reply with the new filename:**\n\n"
        "**Note:** Don't include file extension\n"
        "Example: `my_renamed_file`\n\n"
        "💡 *You can reply to this message*"
    )
    
    # Store the ask message ID for auto-reply
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
        # Delete the selection message
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
        
        final_filename = f"{new_filename}{original_ext}"
        
        # Download path
        download_path = f"downloads/{final_filename}"
        os.makedirs("downloads", exist_ok=True)
        
        # Create progress message
        progress_msg = await callback_query.message.reply_text("🔄 Processing your file...")
        
        # Download file with progress
        start_time = time.time()
        
        file_path = await client.download_media(
            original_message,
            file_name=download_path,
            progress=lambda current, total: progress_for_pyrogram(
                current, total, "📥 **Downloading File**", progress_msg, start_time, final_filename, user_id
            )
        )
        
        # Check if download was cancelled
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("**✅ Download cancelled by user.**")
            # Cleanup
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            if user_id in user_states:
                del user_states[user_id]
            if user_id in cancellation_flags:
                del cancellation_flags[user_id]
            return
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Download failed")
        
        # Store file path for cleanup
        user_states[user_id]['file_path'] = file_path
        
        # Get thumbnail
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
                user_states[user_id]['thumb_path'] = thumb_path
            except:
                pass
        
        # Upload file with progress
        start_time = time.time()
        
        # Check if file should be forced as document
        force_document = False
        if original_ext.lower() in ['.pdf', '.html', '.htm', '.txt', '.doc', '.docx']:
            force_document = True
            upload_type = "document"
        
        if upload_type == "document" or force_document:
            await client.send_document(
                callback_query.message.chat.id,
                document=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                progress=lambda current, total: progress_for_pyrogram(
                    current, total, "📤 **Uploading File**", progress_msg, start_time, final_filename, user_id
                )
            )
        else:  # video
            await client.send_video(
                callback_query.message.chat.id,
                video=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                duration=original_duration,
                supports_streaming=True,
                progress=lambda current, total: progress_for_pyrogram(
                    current, total, "📤 **Uploading File**", progress_msg, start_time, final_filename, user_id
                )
            )
        
        # Check if upload was cancelled
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("**✅ Upload cancelled by user.**")
            return
        
        # Success message
        duration_text = convert_seconds(original_duration) if original_duration > 0 else "Unknown"
        await callback_query.message.reply_text(
            f"**✅ File Renamed Successfully!**\n\n"
            f"**New Name:** `{final_filename}`\n"
            f"**Type:** `{upload_type.title()}`\n"
            f"**Duration:** `{duration_text}`"
        )
        
        # Cleanup progress message
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"**❌ Error:** `{str(e)}`"
        await callback_query.message.reply_text(error_msg)
        logging.error(f"Upload error: {e}")
    
    finally:
        # Cleanup files
        if user_id in user_states:
            user_data = user_states[user_id]
            if 'file_path' in user_data and user_data['file_path'] and os.path.exists(user_data['file_path']):
                try:
                    os.remove(user_data['file_path'])
                except:
                    pass
            if 'thumb_path' in user_data and user_data['thumb_path'] and os.path.exists(user_data['thumb_path']):
                try:
                    os.remove(user_data['thumb_path'])
                except:
                    pass
        
        # Clear user state and cancellation flag
        if user_id in user_states:
            del user_states[user_id]
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]

# ========== FILENAME INPUT HANDLER ==========
@app.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "view_thumb", "del_thumb", "addalloweduser", "removealloweduser", "allowedusers", "users", "mode"]))
@private_access
async def handle_filename(client, message):
    user_id = message.from_user.id
    
    if user_id not in user_states:
        return
    
    user_data = user_states[user_id]
    
    if user_data['step'] != 'awaiting_filename':
        return
    
    new_name = message.text.strip()
    
    if not new_name:
        await message.reply_text("**❌ Filename cannot be empty!**")
        return
    
    # Clean filename
    clean_name = re.sub(r'[<>:"/\\|?*]', '', new_name)
    
    if not clean_name:
        await message.reply_text("**❌ Invalid filename!**")
        return
    
    user_states[user_id]['new_filename'] = clean_name
    user_states[user_id]['step'] = 'awaiting_upload_type'
    
    # Delete the user's filename message and the ask message
    try:
        await message.delete()
    except:
        pass
    
    try:
        if 'ask_message_id' in user_data:
            await client.delete_messages(message.chat.id, user_data['ask_message_id'])
    except:
        pass
    
    # Show upload type selection
    original_name = user_data['file_info']['file_name']
    if not original_name or original_name == 'Unknown':
        file_type = user_data['file_info']['file_type']
        if file_type == 'video':
            original_ext = '.mp4'
        elif file_type == 'audio':
            original_ext = '.mp3'
        else:
            original_ext = '.bin'
    else:
        _, original_ext = os.path.splitext(original_name)
        if not original_ext:
            file_type = user_data['file_info']['file_type']
            if file_type == 'video':
                original_ext = '.mp4'
            elif file_type == 'audio':
                original_ext = '.mp3'
            else:
                original_ext = '.bin'
    
    final_name = f"{clean_name}{original_ext}"
    
    # Auto-select document for specific file types
    if original_ext.lower() in ['.pdf', '.html', '.htm', '.txt', '.doc', '.docx']:
        # Auto-upload as document without asking
        user_states[user_id]['step'] = 'processing'
        await handle_auto_upload(client, message, user_id, final_name, "document")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Document", callback_data="upload_document")],
        [InlineKeyboardButton("🎥 Video", callback_data="upload_video")]
    ])
    
    await message.reply_text(
        f"**Select Upload Type:**\n\n**File:** `{final_name}`",
        reply_markup=keyboard
    )

async def handle_auto_upload(client, message, user_id, final_name, upload_type):
    """Handle automatic upload for document files"""
    user_data = user_states[user_id]
    
    try:
        file_info = user_data['file_info']
        original_message = file_info['original_message']
        original_duration = file_info['duration']
        
        # Download path
        download_path = f"downloads/{final_name}"
        os.makedirs("downloads", exist_ok=True)
        
        # Create progress message
        progress_msg = await message.reply_text("🔄 Processing your file...")
        
        # Download file with progress
        start_time = time.time()
        
        file_path = await client.download_media(
            original_message,
            file_name=download_path,
            progress=lambda current, total: progress_for_pyrogram(
                current, total, "📥 **Downloading File**", progress_msg, start_time, final_name, user_id
            )
        )
        
        # Check if download was cancelled
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("**✅ Download cancelled by user.**")
            # Cleanup
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            if user_id in user_states:
                del user_states[user_id]
            if user_id in cancellation_flags:
                del cancellation_flags[user_id]
            return
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Download failed")
        
        # Store file path for cleanup
        user_states[user_id]['file_path'] = file_path
        
        # Get thumbnail
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
                user_states[user_id]['thumb_path'] = thumb_path
            except:
                pass
        
        # Upload file with progress
        start_time = time.time()
        
        await client.send_document(
            message.chat.id,
            document=file_path,
            thumb=thumb_path,
            caption=f"`{final_name}`",
            progress=lambda current, total: progress_for_pyrogram(
                current, total, "📤 **Uploading File**", progress_msg, start_time, final_name, user_id
            )
        )
        
        # Check if upload was cancelled
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("**✅ Upload cancelled by user.**")
            return
        
        # Success message
        await message.reply_text(
            f"**✅ File Renamed Successfully!**\n\n"
            f"**New Name:** `{final_name}`\n"
            f"**Type:** `Document`"
        )
        
        # Cleanup progress message
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"**❌ Error:** `{str(e)}`"
        await message.reply_text(error_msg)
        logging.error(f"Upload error: {e}")
    
    finally:
        # Cleanup files
        if user_id in user_states:
            user_data = user_states[user_id]
            if 'file_path' in user_data and user_data['file_path'] and os.path.exists(user_data['file_path']):
                try:
                    os.remove(user_data['file_path'])
                except:
                    pass
            if 'thumb_path' in user_data and user_data['thumb_path'] and os.path.exists(user_data['thumb_path']):
                try:
                    os.remove(user_data['thumb_path'])
                except:
                    pass
        
        # Clear user state and cancellation flag
        if user_id in user_states:
            del user_states[user_id]
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]

# ========== START BOT ==========
if __name__ == "__main__":
    print("🚀 Bot is starting...")
    print("🌐 Health check server running on port 8080")
    print(f"👑 Main Owners: {len(Config.OWNER_IDS)} users")
    app.run()
