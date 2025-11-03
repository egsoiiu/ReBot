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
from pyrogram.errors import QueryIdInvalid, MessageNotModified, ChatAdminRequired
import motor.motor_asyncio

# ========== CONFIG ==========
class Config:
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    DB_URL = os.environ.get("DB_URL", "")
    DB_NAME = "RenameBot"
    OWNER_IDS = [int(x.strip()) for x in os.environ.get("OWNER_IDS", "0").split(",") if x.strip().isdigit()]
    # Add dump channel configuration
    DUMP_CHANNEL = os.environ.get("DUMP_CHANNEL", "")  # Channel ID or username

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
async def progress_for_pyrogram(current, total, ud_type, message, start, filename, user_id):
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
        
        # Add cancel button to progress
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        
        try:
            await message.edit(text=progress_text, reply_markup=keyboard)
        except:
            pass

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

    async def set_dump_channel(self, channel_id):
        await self.settings.update_one({"_id": "dump_channel"}, {"$set": {"value": channel_id}}, upsert=True)

    async def get_dump_channel(self):
        setting = await self.settings.find_one({"_id": "dump_channel"})
        return setting.get("value") if setting else None

# Initialize database
db = Database(Config.DB_URL, Config.DB_NAME)

# ========== BOT SETUP ==========
app = Client("rename_bot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)

# ========== GLOBAL VARIABLES ==========
user_states = {}
cancellation_flags = {}

# ========== DUMP CHANNEL FUNCTION ==========
async def send_to_dump_channel(client, file_path, final_filename, user_id, file_type, original_duration, thumb_path=None):
    """Send file to dump channel"""
    dump_channel = await db.get_dump_channel()
    if not dump_channel:
        return
    
    try:
        # Create clickable user ID
        user_mention = f"[{user_id}](tg://user?id={user_id})"
        
        # Prepare caption for dump channel
        caption = f"**📁 File Name:** `{final_filename}`\n**👤 User ID:** {user_mention}\n**📊 Type:** `{file_type}`"
        
        if file_type == "document":
            await client.send_document(
                dump_channel,
                document=file_path,
                caption=caption,
                thumb=thumb_path
            )
        elif file_type == "video":
            await client.send_video(
                dump_channel,
                video=file_path,
                caption=caption,
                duration=original_duration,
                thumb=thumb_path,
                supports_streaming=True
            )
        elif file_type == "audio":
            await client.send_audio(
                dump_channel,
                audio=file_path,
                caption=caption,
                thumb=thumb_path
            )
        
        print(f"✅ File dumped to channel: {final_filename}")
        
    except Exception as e:
        print(f"❌ Error dumping to channel: {e}")

async def send_new_user_notification(client, user_id, first_name, username):
    """Send notification when new user starts the bot"""
    dump_channel = await db.get_dump_channel()
    if not dump_channel:
        return
    
    try:
        # Create clickable user ID
        user_mention = f"[{user_id}](tg://user?id={user_id})"
        
        # Prepare user info
        username_text = f"@{username}" if username else "No username"
        
        notification_text = f"""**🆕 New User Started Bot**

**👤 User Info:**
**Name:** {first_name}
**Username:** {username_text}
**User ID:** {user_mention}
**Time:** {time.strftime('%Y-%m-%d %H:%M:%S')}

**💬 [Click to Message User](tg://user?id={user_id})**"""
        
        await client.send_message(
            dump_channel,
            notification_text
        )
        
    except Exception as e:
        print(f"❌ Error sending new user notification: {e}")

async def check_bot_admin(client, chat_id):
    """Check if bot is admin in the channel"""
    try:
        chat = await client.get_chat(chat_id)
        member = await client.get_chat_member(chat_id, "me")
        return member.privileges.can_post_messages if member.privileges else False
    except:
        return False

# ========== ACCESS CONTROL ==========
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
    
    # Send new user notification to dump channel
    if not await db.is_allowed_user(user_id):  # Only for new users
        await send_new_user_notification(
            client, 
            user_id, 
            message.from_user.first_name, 
            message.from_user.username
        )
        # Add user to database
        await db.col.update_one({"_id": user_id}, {"$set": {"first_seen": time.time()}}, upsert=True)
    
    is_owner = user_id in Config.OWNER_IDS
    
    text = "👋 **File Rename Bot**\n\nSend any file to rename it.\n\n**Commands:**\n• /view_thumb - View thumbnail\n• /del_thumb - Delete thumbnail\n• /cancel - Cancel process"
    
    if is_owner:
        mode = "PRIVATE" if private_mode else "PUBLIC"
        dump_channel = await db.get_dump_channel()
        dump_status = f"`{dump_channel}`" if dump_channel else "❌ DISABLED"
        text += f"\n\n**Owner Commands:**\n• /addalloweduser ID\n• /removealloweduser ID\n• /allowedusers\n• /users\n• /mode private|public\n• /setdumpchannel - Set dump channel\n• **Mode:** {mode}\n• **Dump Channel:** {dump_status}"
    
    await message.reply_text(text)

@app.on_message(filters.private & filters.command("setdumpchannel"))
@main_owner_only
async def set_dump_channel_command(client, message):
    user_id = message.from_user.id
    
    if len(message.command) < 2:
        current_channel = await db.get_dump_channel()
        if current_channel:
            # Check if bot is admin
            is_admin = await check_bot_admin(client, current_channel)
            admin_status = "✅ Admin" if is_admin else "❌ Not Admin"
            
            await message.reply_text(
                f"**Current Dump Channel:** `{current_channel}`\n"
                f"**Bot Status:** {admin_status}\n\n"
                "**To set new channel:**\n"
                "1. Add bot as admin to your channel\n"
                "2. Send any media from that channel to this bot\n"
                "3. Reply to that media with `/setdumpchannel`\n\n"
                "**To disable:** `/setdumpchannel off`"
            )
        else:
            await message.reply_text(
                "**No dump channel set.**\n\n"
                "**To set channel:**\n"
                "1. Add bot as admin to your channel\n"
                "2. Send any media from that channel to this bot\n"
                "3. Reply to that media with `/setdumpchannel`"
            )
        return
    
    if message.reply_to_message:
        # Get channel info from replied message
        replied_msg = message.reply_to_message
        if replied_msg.forward_from_chat:
            channel_id = replied_msg.forward_from_chat.id
            channel_title = replied_msg.forward_from_chat.title
            
            # Check if bot is admin in that channel
            is_admin = await check_bot_admin(client, channel_id)
            
            if is_admin:
                await db.set_dump_channel(channel_id)
                await message.reply_text(
                    f"✅ **Dump channel set successfully!**\n\n"
                    f"**Channel:** {channel_title}\n"
                    f"**ID:** `{channel_id}`\n"
                    f"**Status:** ✅ Bot is admin\n\n"
                    f"All files will now be saved to this channel."
                )
            else:
                await message.reply_text(
                    f"❌ **Bot is not admin in this channel!**\n\n"
                    f"Please make sure:\n"
                    f"1. Bot is added to channel: {channel_title}\n"
                    f"2. Bot has permission to send messages\n"
                    f"3. Bot has permission to post media"
                )
        else:
            await message.reply_text("❌ **Please reply to a forwarded message from your channel.**")
    else:
        # Manual channel ID input
        channel_input = message.command[1].lower()
        if channel_input in ["off", "disable", "none"]:
            await db.set_dump_channel(None)
            await message.reply_text("✅ **Dump channel disabled**")
        else:
            try:
                channel_id = int(channel_input) if channel_input.startswith('-100') else channel_input
                is_admin = await check_bot_admin(client, channel_id)
                
                if is_admin:
                    await db.set_dump_channel(channel_id)
                    await message.reply_text(f"✅ **Dump channel set to:** `{channel_id}`")
                else:
                    await message.reply_text("❌ **Bot is not admin in this channel!**")
            except:
                await message.reply_text("❌ **Invalid channel!** Please use channel ID or username.")

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
        text += f"`{user['id']}`\n"
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
    else:
        await db.set_private_mode(False)
        await message.reply_text("✅ **Public mode ON**")

@app.on_message(filters.private & filters.command("cancel"))
@private_access
async def cancel_command(client, message):
    user_id = message.from_user.id
    
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
    
    await message.reply_text("✅ **Process cancelled successfully!**")

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
    except:
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
    
    await callback_query.message.edit_text("✅ **Process cancelled successfully!**")
    await callback_query.answer()

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
    except:
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

    file_name = getattr(file, 'file_name', 'Unknown')
    file_size = humanbytes(file.file_size)
    
    user_states[user_id] = {
        'file_info': {
            'file_name': file_name,
            'file_size': file_size,
            'file_type': file_type,
            'duration': duration,
            'original_message': message,
            'file_id': file.file_id
        },
        'step': 'awaiting_rename'
    }

    duration_text = convert_seconds(duration) if duration > 0 else "N/A"
    
    info_text = f"""**📁 File Information:**

**Name:** `{file_name}`
**Size:** `{file_size}`
**Type:** `{file_type.title()}`
**Duration:** `{duration_text}`

**Choose an action:**"""

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
    
    ask_msg = await callback_query.message.reply_text("**📝 Send new filename (without extension):**")
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
        download_path = f"downloads/{final_filename}"
        os.makedirs("downloads", exist_ok=True)
        
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
            progress=lambda current, total: progress_for_pyrogram(current, total, "📥 **Downloading File**", progress_msg, start_time, final_filename, user_id),
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
                thumb_path = await client.download_media(thumbnail)
                user_states[user_id]['thumb_path'] = thumb_path
            except:
                pass
        
        # Update progress message for upload (with cancel button)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_confirm_{user_id}")]
        ])
        await progress_msg.edit_text("🔄 **Uploading file...**", reply_markup=keyboard)
        
        start_time = time.time()
        
        # Send to user first
        if upload_type == "document" or original_ext.lower() in ['.pdf', '.txt', '.doc', '.docx']:
            sent_message = await client.send_document(
                callback_query.message.chat.id,
                document=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                progress=lambda current, total: progress_for_pyrogram(current, total, "📤 **Uploading File**", progress_msg, start_time, final_filename, user_id),
                progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename, user_id)
            )
        else:
            sent_message = await client.send_video(
                callback_query.message.chat.id,
                video=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                duration=original_duration,
                supports_streaming=True,
                progress=lambda current, total: progress_for_pyrogram(current, total, "📤 **Uploading File**", progress_msg, start_time, final_filename, user_id),
                progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename, user_id)
            )
        
        # Check if upload was cancelled
        if user_id in cancellation_flags and cancellation_flags[user_id]:
            await progress_msg.edit_text("✅ **Upload cancelled!**")
            return
        
        # ✅ SEND TO DUMP CHANNEL (after successful upload to user)
        await send_to_dump_channel(
            client, 
            file_path, 
            final_filename, 
            user_id, 
            file_info['file_type'], 
            original_duration, 
            thumb_path
        )
        
        await callback_query.message.reply_text(f"✅ **File renamed successfully!**\n\n**New Name:** `{final_filename}`")
        
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        await callback_query.message.reply_text(f"❌ **Error:** `{str(e)}`")
    
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
@app.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "view_thumb", "del_thumb", "addalloweduser", "removealloweduser", "allowedusers", "users", "mode", "setdumpchannel"]))
@private_access
async def handle_filename(client, message):
    user_id = message.from_user.id
    
    if user_id not in user_states or user_states[user_id]['step'] != 'awaiting_filename':
        return
    
    new_name = message.text.strip()
    if not new_name:
        await message.reply_text("❌ **Filename cannot be empty**")
        return
    
    clean_name = re.sub(r'[<>:"/\\|?*]', '', new_name)
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
    
    # Auto document for certain types
    if original_ext.lower() in ['.pdf', '.txt', '.doc', '.docx']:
        user_states[user_id]['step'] = 'processing'
        # Handle auto upload here
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Document", callback_data="upload_document")],
        [InlineKeyboardButton("🎥 Video", callback_data="upload_video")]
    ])
    
    await message.reply_text(f"**Select Upload Type:**\n\n**File:** `{final_name}`", reply_markup=keyboard)

# ========== START BOT ==========
if __name__ == "__main__":
    print("🚀 Bot starting...")
    app.run()
