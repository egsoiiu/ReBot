import os
import asyncio
import logging
import math
import time
import re
import secrets
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
async def progress_for_pyrogram(current, total, ud_type, message, start, filename):
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
        try:
            await message.edit(text=progress_text)
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
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

# ========== DATABASE CLASS ==========
class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.allowed_users = self.db.allowed_users
        self.settings = self.db.settings
        self.file_references = self.db.file_references

    async def set_thumbnail(self, user_id, file_id):
        await self.col.update_one(
            {"_id": user_id},
            {"$set": {"file_id": file_id}},
            upsert=True
        )

    async def get_thumbnail(self, user_id):
        user = await self.col.find_one({"_id": user_id})
        return user.get("file_id") if user else None

    async def add_allowed_user(self, user_id):
        await self.allowed_users.update_one(
            {"_id": user_id},
            {"$set": {"added_at": time.time()}},
            upsert=True
        )

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
        await self.settings.update_one(
            {"_id": "private_mode"},
            {"$set": {"value": value}},
            upsert=True
        )

    async def store_file_reference(self, reference_id, file_data):
        await self.file_references.update_one(
            {"_id": reference_id},
            {"$set": {
                "file_data": file_data,
                "created_at": time.time(),
                "downloads": 0
            }},
            upsert=True
        )

    async def get_file_reference(self, reference_id):
        file_ref = await self.file_references.find_one({"_id": reference_id})
        if file_ref:
            await self.file_references.update_one(
                {"_id": reference_id},
                {"$inc": {"downloads": 1}}
            )
            return file_ref.get("file_data")
        return None

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
        
        private_mode = await db.get_private_mode()
        
        if not private_mode:
            return await func(client, message)
        
        if await db.is_allowed_user(user_id):
            return await func(client, message)
        else:
            await message.reply_text("🚫 **This bot is in private mode. Contact owner for access.**")
            return
    
    return wrapper

# ========== MAIN OWNER CHECK DECORATOR ==========
def main_owner_only(func):
    async def wrapper(client, message):
        user_id = message.from_user.id
        
        if user_id in Config.OWNER_IDS:
            return await func(client, message)
        else:
            await message.reply_text("🚫 **This command is only for main owners.**")
            return
    
    return wrapper

# ========== DEEP LINK HANDLER ==========
@app.on_message(filters.private & filters.command("start"))
async def start_command(client, message):
    user_id = message.from_user.id
    
    if len(message.command) > 1:
        reference_id = message.command[1]
        await handle_file_reference(client, message, reference_id)
        return
    
    private_mode = await db.get_private_mode()
    
    is_allowed = await db.is_allowed_user(user_id)
    
    if private_mode and not is_allowed:
        await message.reply_text("🚫 **This bot is in private mode. Contact owner for access.**")
        return
    
    is_owner = user_id in Config.OWNER_IDS
    
    welcome_text = "👋 **Hello! I'm a File Rename Bot**\n\n"
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
    welcome_text += "• /cancel - Cancel current process"
    
    if is_owner:
        current_mode = await db.get_private_mode()
        mode_text = "PRIVATE" if current_mode else "PUBLIC"
        welcome_text += "\n\n**👑 Owner Commands:**\n"
        welcome_text += "• /addalloweduser <id> - Add allowed user\n"
        welcome_text += "• /removealloweduser <id> - Remove allowed user\n"
        welcome_text += "• /allowedusers - List all allowed users\n"
        welcome_text += "• /users - List all users\n"
        welcome_text += "• /mode <private|public> - Change bot mode\n"
        welcome_text += f"• **Current Mode:** `{mode_text}`"
    
    await message.reply_text(welcome_text)

async def handle_file_reference(client, message, reference_id):
    file_data = await db.get_file_reference(reference_id)
    
    if not file_data:
        await message.reply_text("❌ **File link expired or invalid!**")
        return
    
    try:
        file_id = file_data['file_id']
        file_type = file_data['file_type']
        file_name = file_data['file_name']
        thumb = file_data.get('thumb')
        duration = file_data.get('duration', 0)
        
        if file_type == "document":
            await client.send_document(
                message.chat.id,
                document=file_id,
                caption=f"`{file_name}`",
                thumb=thumb
            )
        elif file_type == "video":
            await client.send_video(
                message.chat.id,
                video=file_id,
                caption=f"`{file_name}`",
                duration=duration,
                thumb=thumb,
                supports_streaming=True
            )
        elif file_type == "audio":
            await client.send_audio(
                message.chat.id,
                audio=file_id,
                caption=f"`{file_name}`",
                thumb=thumb
            )
        
        await message.reply_text("✅ **File sent successfully!**")
        
    except Exception as e:
        await message.reply_text(f"❌ **Error sending file:** `{str(e)}`")

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
        await message.reply_text(f"✅ **User `{new_user_id}` added as allowed user.**")
    except ValueError:
        await message.reply_text("❌ **Invalid user ID. Please provide a numeric ID.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{str(e)}`")

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
        await message.reply_text(f"✅ **User `{remove_user_id}` removed from allowed users.**")
    except ValueError:
        await message.reply_text("❌ **Invalid user ID. Please provide a numeric ID.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{str(e)}`")

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
    for user_id in users[:20]:
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
        await message.reply_text("✅ **Bot mode set to PRIVATE**\nOnly allowed users can use the bot.")
    elif mode in ["public", "false", "0"]:
        await db.set_private_mode(False)
        await message.reply_text("✅ **Bot mode set to PUBLIC**\nAnyone can use the bot.")
    else:
        await message.reply_text("❌ **Invalid mode. Use `private` or `public`**")

# ========== CANCEL COMMAND ==========
@app.on_message(filters.private & filters.command("cancel"))
@private_access
async def cancel_command(client, message):
    user_id = message.from_user.id
    
    cancellation_flags[user_id] = True
    
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
    
    await message.reply_text("✅ **Process cancelled successfully! All files cleared.**")

# ========== CANCEL CALLBACK HANDLERS ==========
@app.on_callback_query(filters.regex(r"^cancel_confirm_(\d+)$"))
async def cancel_confirm_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("❌ You can only cancel your own processes!", show_alert=True)
        return
    
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
    
    cancellation_flags[user_id] = True
    
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
    
    await callback_query.message.edit_text("✅ **Process cancelled successfully! All files cleared.**")
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^cancel_no_(\d+)$"))
async def cancel_no_handler(client, callback_query):
    user_id = callback_query.from_user.id
    target_user_id = int(callback_query.matches[0].group(1))
    
    if user_id != target_user_id:
        await callback_query.answer("❌ Access denied!", show_alert=True)
        return
    
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
    ])
    
    try:
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer("✅ Process continued")
    except Exception as e:
        await callback_query.answer("Error updating message", show_alert=True)

# ========== THUMBNAIL MANAGEMENT ==========
@app.on_message(filters.private & filters.command(["view_thumb", "viewthumbnail"]))
@private_access
async def view_thumbnail(client, message):
    try:
        thumbnail = await db.get_thumbnail(message.from_user.id)
        if thumbnail:
            await client.send_photo(message.chat.id, thumbnail, caption="**Your Current Thumbnail**")
        else:
            await message.reply_text("**You don't have any thumbnail set.**")
    except Exception as e:
        await message.reply_text("❌ **Error loading thumbnail. It may be invalid. Please set a new thumbnail.**")
        await db.set_thumbnail(message.from_user.id, None)

@app.on_message(filters.private & filters.command(["del_thumb", "deletethumbnail"]))
@private_access
async def delete_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, None)
    await message.reply_text("✅ **Thumbnail deleted successfully!**")

@app.on_message(filters.private & filters.photo)
@private_access
async def save_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, message.photo.file_id)
    await message.reply_text("✅ **Thumbnail saved successfully!**")

# ========== FILE RENAME HANDLER ==========
@app.on_message(filters.private & (filters.document | filters.video | filters.audio))
@private_access
async def handle_file(client, message):
    user_id = message.from_user.id
    
    if user_id in user_states:
        await message.reply_text("❌ **Please complete your current process first!**\nUse /cancel to cancel.")
        return
    
    if user_id in cancellation_flags:
        del cancellation_flags[user_id]
    
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
    
    try:
        await callback_query.message.delete()
    except:
        pass
    
    ask_msg = await callback_query.message.reply_text(
        "**📝 Please reply with the new filename:**\n\n"
        "**Note:** Don't include file extension\n"
        "Example: `my_renamed_file`\n\n"
        "💡 *You can reply to this message*"
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
        try:
            await callback_query.message.delete()
        except:
            pass
        
        file_info = user_data['file_info']
        new_filename = user_data['new_filename']
        original_message = file_info['original_message']
        original_duration = file_info['duration']
        
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
        
        download_path = f"downloads/{final_filename}"
        os.makedirs("downloads", exist_ok=True)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        progress_msg = await callback_query.message.reply_text("🔄 **Processing your file...**", reply_markup=keyboard)
        
        start_time = time.time()
        
        file_path = await client.download_media(
            original_message,
            file_name=download_path,
            progress=progress_for_pyrogram,
            progress_args=("📥 **Downloading File**", progress_msg, start_time, final_filename)
        )
        
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("✅ **Download cancelled!**")
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
        
        user_states[user_id]['file_path'] = file_path
        
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
                user_states[user_id]['thumb_path'] = thumb_path
            except:
                pass
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        await progress_msg.edit_text("🔄 **Uploading file...**", reply_markup=keyboard)
        
        start_time = time.time()
        
        if upload_type == "document" or original_ext.lower() in ['.pdf', '.html', '.htm', '.txt', '.doc', '.docx']:
            sent_message = await client.send_document(
                "me",
                document=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                progress=progress_for_pyrogram,
                progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
            )
            file_id = sent_message.document.file_id
            final_file_type = "document"
        else:
            sent_message = await client.send_video(
                "me",
                video=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                duration=original_duration,
                supports_streaming=True,
                progress=progress_for_pyrogram,
                progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
            )
            file_id = sent_message.video.file_id
            final_file_type = "video"
        
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("✅ **Upload cancelled!**")
            return
        
        reference_id = secrets.token_urlsafe(12)
        
        file_data = {
            'file_id': file_id,
            'file_type': final_file_type,
            'file_name': final_filename,
            'thumb': thumbnail,
            'duration': original_duration,
            'user_id': user_id
        }
        
        await db.store_file_reference(reference_id, file_data)
        
        bot_info = await client.get_me()
        bot_username = bot_info.username
        
        deep_link = f"https://t.me/{bot_username}?start={reference_id}"
        
        success_text = f"""✅ **File Uploaded Successfully!**

**File Name:** `{final_filename}`
**File Size:** {humanbytes(os.path.getsize(file_path))}
**File Type:** {final_file_type.title()}

📦 **Your file is ready! Click the button below to get it instantly:**

The link will work for 24 hours."""
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Get Your File", url=deep_link)],
            [InlineKeyboardButton("🔗 Copy Link", callback_data=f"copy_link_{reference_id}")]
        ])
        
        await callback_query.message.reply_text(success_text, reply_markup=keyboard)
        
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"❌ **Error:** `{str(e)}`"
        await callback_query.message.reply_text(error_msg)
        logging.error(f"Upload error: {e}")
    
    finally:
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
        
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]

# ========== COPY LINK HANDLER ==========
@app.on_callback_query(filters.regex(r"^copy_link_(.+)$"))
async def copy_link_handler(client, callback_query):
    reference_id = callback_query.matches[0].group(1)
    
    bot_info = await client.get_me()
    bot_username = bot_info.username
    
    deep_link = f"https://t.me/{bot_username}?start={reference_id}"
    
    await callback_query.answer(f"Link: {deep_link}\n\nYou can copy this link manually.", show_alert=True)

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
        await message.reply_text("❌ **Filename cannot be empty!**")
        return
    
    clean_name = re.sub(r'[<>:"/\\|?*]', '', new_name)
    
    if not clean_name:
        await message.reply_text("❌ **Invalid filename!**")
        return
    
    user_states[user_id]['new_filename'] = clean_name
    user_states[user_id]['step'] = 'awaiting_upload_type'
    
    try:
        await message.delete()
    except:
        pass
    
    try:
        if 'ask_message_id' in user_data:
            await client.delete_messages(message.chat.id, user_data['ask_message_id'])
    except:
        pass
    
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
    
    if original_ext.lower() in ['.pdf', '.html', '.htm', '.txt', '.doc', '.docx']:
        user_states[user_id]['step'] = 'processing'
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Document", callback_data="upload_document")],
        [InlineKeyboardButton("🎥 Video", callback_data="upload_video")]
    ])
    
    await message.reply_text(
        f"**Select Upload Type:**\n\n**File:** `{final_name}`",
        reply_markup=keyboard
    )

# ========== START BOT ==========
if __name__ == "__main__":
    print("🚀 Bot is starting...")
    print("🌐 Health check server running on port 8080")
    print("🔒 Deep Link System: Enabled")
    app.run()
