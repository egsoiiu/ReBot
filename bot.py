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
    """Convert seconds to MM:SS or HH:MM:SS format"""
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

    async def set_thumbnail(self, user_id, file_id):
        await self.col.update_one(
            {"_id": user_id},
            {"$set": {"file_id": file_id}},
            upsert=True
        )

    async def get_thumbnail(self, user_id):
        user = await self.col.find_one({"_id": user_id})
        return user.get("file_id") if user else None

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

# ========== THUMBNAIL MANAGEMENT ==========
@app.on_message(filters.private & filters.command(["view_thumb", "viewthumbnail"]))
async def view_thumbnail(client, message):
    thumbnail = await db.get_thumbnail(message.from_user.id)
    if thumbnail:
        await client.send_photo(message.chat.id, thumbnail, caption="**Your Current Thumbnail**")
    else:
        await message.reply_text("**You don't have any thumbnail set.**")

@app.on_message(filters.private & filters.command(["del_thumb", "deletethumbnail"]))
async def delete_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, None)
    await message.reply_text("**Thumbnail deleted successfully!**")

@app.on_message(filters.private & filters.photo)
async def save_thumbnail(client, message):
    await db.set_thumbnail(message.from_user.id, message.photo.file_id)
    await message.reply_text("**Thumbnail saved successfully!**")

# ========== CANCEL COMMAND ==========
@app.on_message(filters.private & filters.command("cancel"))
async def cancel_command(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        del user_states[user_id]
        await message.reply_text("**✅ Process cancelled successfully!**")
    else:
        await message.reply_text("**❌ No active process to cancel.**")

# ========== START COMMAND ==========
@app.on_message(filters.private & filters.command("start"))
async def start_command(client, message):
    await message.reply_text(
        "**👋 Hello! I'm a File Rename Bot**\n\n"
        "**How to use:**\n"
        "1. Send me any file (document/video/audio)\n"
        "2. Click 'Rename' button\n"
        "3. Enter new filename\n"
        "4. Select upload type\n\n"
        "**Thumbnail Commands:**\n"
        "• Send a photo to set thumbnail\n"
        "• /view_thumb - View current thumbnail\n"
        "• /del_thumb - Delete thumbnail\n\n"
        "**Other Commands:**\n"
        "• /cancel - Cancel current process"
    )

# ========== FILE RENAME HANDLER ==========
@app.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def handle_file(client, message):
    user_id = message.from_user.id
    
    # Block if user already has active process
    if user_id in user_states:
        await message.reply_text("**❌ Please complete your current process first!**\nUse /cancel to cancel.")
        return
    
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
        'step': 'awaiting_rename'
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
        # Removed cancel button
    ])
    
    await message.reply_text(info_text, reply_markup=keyboard)

# ========== CALLBACK HANDLERS ==========
@app.on_callback_query(filters.regex("^start_rename$"))
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
            progress=progress_for_pyrogram,
            progress_args=("📥 **Downloading File**", progress_msg, start_time, final_filename)
        )
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Download failed")
        
        # Get thumbnail
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
            except:
                pass
        
        # Upload file with progress (no separate "Uploading" message)
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
                progress=progress_for_pyrogram,
                progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
            )
        else:  # video
            await client.send_video(
                callback_query.message.chat.id,
                video=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                duration=original_duration,
                supports_streaming=True,
                progress=progress_for_pyrogram,
                progress_args=("📤 **Uploading File**", progress_msg, start_time, final_filename)
            )
        
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
        if 'file_path' in locals() and file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        if 'thumb_path' in locals() and thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except:
                pass
        
        # Clear user state
        if user_id in user_states:
            del user_states[user_id]

# ========== FILENAME INPUT HANDLER ==========
@app.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "view_thumb", "del_thumb"]))
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
        # Removed cancel button
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
            progress=progress_for_pyrogram,
            progress_args=("📥 **Downloading File**", progress_msg, start_time, final_name)
        )
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Download failed")
        
        # Get thumbnail
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
            except:
                pass
        
        # Upload file with progress
        start_time = time.time()
        
        await client.send_document(
            message.chat.id,
            document=file_path,
            thumb=thumb_path,
            caption=f"`{final_name}`",
            progress=progress_for_pyrogram,
            progress_args=("📤 **Uploading File**", progress_msg, start_time, final_name)
        )
        
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
        if 'file_path' in locals() and file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        if 'thumb_path' in locals() and thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except:
                pass
        
        # Clear user state
        if user_id in user_states:
            del user_states[user_id]

# ========== START BOT ==========
if __name__ == "__main__":
    print("🚀 Bot is starting...")
    print("🌐 Health check server running on port 8080")
    app.run()
