import os
import asyncio
import logging
import math
import time
import re
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
import motor.motor_asyncio

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# CONFIG
class Config:
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    DB_URL = os.environ.get("DB_URL", "")
    DB_NAME = "RenameBot"

# DATABASE
class Database:
    def __init__(self, uri, dbname):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[dbname]
        self.col = self.db.users

    async def set_thumbnail(self, user_id, file_id):
        await self.col.update_one({"_id": user_id}, {"$set": {"file_id": file_id}}, upsert=True)

    async def get_thumbnail(self, user_id):
        user = await self.col.find_one({"_id": user_id})
        return user.get("file_id") if user else None

db = Database(Config.DB_URL, Config.DB_NAME)

# BOT SETUP
if not all([Config.API_ID, Config.API_HASH, Config.BOT_TOKEN]):
    print("❌ Missing API credentials!")
    exit(1)

app = Client("rename_bot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)

user_states = {}

# HELPERS
def humanbytes(size):
    if not size:
        return "0 B"
    power = 2 ** 10
    n = 0
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    while size > power and n < len(units) - 1:
        size /= power
        n += 1
    return f"{size:.1f} {units[n]}"

def convert_seconds(seconds):
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def get_appropriate_ext(file_type, original_ext):
    if original_ext:
        return original_ext
    if file_type.lower() == "video":
        return ".mp4"
    elif file_type.lower() == "audio":
        return ".mp3"
    else:
        return ".bin"

async def progress_for_pyrogram(current, total, ud_type, message, start, filename):
    now = time.time()
    diff = now - start
    if round(diff % 2.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        elapsed = round(diff) * 1000
        eta = round((total - current) / speed) * 1000 if speed > 0 else 0
        elapsed_fmt = convert_seconds(elapsed // 1000)
        eta_fmt = convert_seconds(eta // 1000)
        filled = math.floor(percentage / 10)
        progress_bar = "▣" * filled + "□" * (10 - filled)
        progress_text = (f"{ud_type}\n\n📄 `{filename}`\n\n[{progress_bar}] {percentage:.1f}%\n"
                         f"💾 {humanbytes(current)} / {humanbytes(total)}\n"
                         f"🚀 {humanbytes(speed)}/s\n⏰ ETA: {eta_fmt}")
        try:
            await message.edit(text=progress_text)
        except MessageNotModified:
            pass

# HANDLERS

@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "🤖 **File Renamer Bot**\n\n"
        "Send me a file to rename it with custom filename and thumbnail!\n\n"
        "**Commands:**\n"
        "• /start - Start the bot\n"
        "• /view_thumb - View your current thumbnail\n"
        "• /del_thumb - Delete your thumbnail\n"
        "• /cancel - Cancel current operation\n\n"
        "**How to use:**\n"
        "1. Send a photo to set as thumbnail\n"
        "2. Send any file (document/video/audio)\n"
        "3. Choose new filename\n"
        "4. Select upload type"
    )

@app.on_message(filters.private & filters.photo)
async def save_thumb(client, message):
    await db.set_thumbnail(message.from_user.id, message.photo.file_id)
    await message.reply_text("✅ Thumbnail saved successfully!")

@app.on_message(filters.private & filters.command(["view_thumb", "viewthumbnail"]))
async def view_thumb(client, message):
    thumb_id = await db.get_thumbnail(message.from_user.id)
    if thumb_id:
        await client.send_photo(message.chat.id, thumb_id, caption="📷 Your saved thumbnail")
    else:
        await message.reply_text("❌ No thumbnail set. Send a photo to set one.")

@app.on_message(filters.private & filters.command(["del_thumb", "deletethumbnail"]))
async def del_thumb(client, message):
    await db.set_thumbnail(message.from_user.id, None)
    await message.reply_text("✅ Thumbnail deleted successfully!")

@app.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def file_received(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        await message.reply_text("⚠️ Please finish your current process or use /cancel first.")
        return
    
    file = message.document or message.video or message.audio
    file_name = getattr(file, "file_name", "Unknown")
    file_size = humanbytes(getattr(file, "file_size", 0))
    duration = getattr(file, "duration", 0)
    file_type = "Document" if message.document else "Video" if message.video else "Audio"
    
    user_states[user_id] = {
        "file_info": {
            "file_name": file_name, 
            "file_id": file.file_id,
            "file_type": file_type, 
            "duration": duration, 
            "original_message": message
        },
        "step": "awaiting_rename"
    }
    
    duration_str = convert_seconds(duration) if duration else "N/A"
    text = (
        f"📁 **File Information:**\n\n"
        f"📄 **Name:** `{file_name}`\n"
        f"💾 **Size:** `{file_size}`\n"
        f"📦 **Type:** `{file_type}`\n"
        f"⏱️ **Duration:** `{duration_str}`\n\n"
        f"Click **Rename** to proceed with renaming."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Rename", callback_data="start_rename")]])
    await message.reply_text(text, reply_markup=keyboard)

@app.on_callback_query(filters.regex("^start_rename$"))
async def rename_start(client, cq):
    user_id = cq.from_user.id
    if user_id not in user_states:
        await cq.answer("❌ Session expired. Please send the file again.", show_alert=True)
        return
    
    user_states[user_id]["step"] = "awaiting_filename"
    try:
        await cq.message.delete()
    except: 
        pass
    
    msg = await cq.message.reply_text(
        "📝 **Please send the new filename** (without extension):\n\n"
        "⚠️ *Invalid characters will be automatically removed*"
    )
    user_states[user_id]["ask_message_id"] = msg.id
    await cq.answer()

@app.on_message(filters.private & filters.text & ~filters.command(["start","cancel","view_thumb","del_thumb"]))
async def rename_receive(client, message):
    user_id = message.from_user.id
    if user_id not in user_states or user_states[user_id].get("step") != "awaiting_filename":
        return
    
    new_name = message.text.strip()
    if not new_name:
        await message.reply_text("❌ Filename cannot be empty!")
        return
    
    # Clean filename from invalid characters
    new_name = re.sub(r'[<>:"/\\|?*]', '', new_name)
    if not new_name:
        await message.reply_text("❌ Invalid filename after cleaning!")
        return
    
    user_states[user_id]["new_filename"] = new_name
    user_states[user_id]["step"] = "awaiting_upload_type"
    
    try:
        await message.delete()
    except: 
        pass
    
    # Clean up ask message
    if "ask_message_id" in user_states[user_id]:
        try:
            await client.delete_messages(message.chat.id, user_states[user_id]["ask_message_id"])
        except: 
            pass

    # Determine file extension
    file_info = user_states[user_id]["file_info"]
    original_name = file_info["file_name"] or "file"
    _, original_ext = os.path.splitext(original_name)
    ext = get_appropriate_ext(file_info["file_type"], original_ext)
    final_name = f"{new_name}{ext}"
    
    # Check if user has thumbnail
    thumb_id = await db.get_thumbnail(user_id)
    thumb_status = "✅ With thumbnail" if thumb_id else "❌ No thumbnail"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Document", callback_data="upload_document"),
         InlineKeyboardButton("🎥 Video", callback_data="upload_video")]
    ])
    
    await message.reply_text(
        f"📤 **Upload Settings**\n\n"
        f"📄 **Filename:** `{final_name}`\n"
        f"🎨 **Thumbnail:** {thumb_status}\n\n"
        f"Select upload type:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^upload_(document|video)$"))
async def upload_file(client, cq):
    user_id = cq.from_user.id
    if user_id not in user_states:
        await cq.answer("❌ Session expired! Please send file again.", show_alert=True)
        return
    
    upload_type = cq.data.split("_")[1]
    user_data = user_states[user_id]
    file_info = user_data["file_info"]
    new_name = user_data.get("new_filename", "renamed_file")

    # Determine file extension
    original_name = file_info["file_name"] or "file"
    _, original_ext = os.path.splitext(original_name)
    ext = get_appropriate_ext(file_info["file_type"], original_ext)
    final_name = f"{new_name}{ext}"

    thumb_file_id = await db.get_thumbnail(user_id)
    thumb_path = None

    # Download thumbnail if exists
    if thumb_file_id:
        progress_msg = await cq.message.reply_text("📥 Downloading thumbnail...")
        try:
            thumb_path = await client.download_media(
                thumb_file_id, 
                file_name=f"temp_thumb_{user_id}.jpg"
            )
            await progress_msg.edit_text("✅ Thumbnail downloaded! Starting upload...")
        except Exception as e:
            logger.error(f"Thumbnail download failed: {e}")
            await progress_msg.edit_text("⚠️ Thumbnail download failed, proceeding without thumbnail...")
            thumb_path = None
    else:
        progress_msg = await cq.message.reply_text("⏳ Starting upload without thumbnail...")

    try:
        await cq.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete callback message: {e}")

    try:
        # Send file with thumbnail and progress
        start_time = time.time()
        
        if upload_type == "document":
            await client.send_document(
                chat_id=user_id,
                document=file_info["file_id"],
                thumb=thumb_path,  # Apply thumbnail for documents
                file_name=final_name,
                caption=f"**📄 {final_name}**",
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading document...", progress_msg, start_time, final_name)
            )
        else:
            await client.send_video(
                chat_id=user_id,
                video=file_info["file_id"],
                thumb=thumb_path,  # Apply thumbnail for videos
                file_name=final_name,
                caption=f"**🎥 {final_name}**",
                duration=file_info.get("duration", 0),
                supports_streaming=True,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading video...", progress_msg, start_time, final_name)
            )
            
        await progress_msg.edit_text(f"✅ **Success!**\n\n**File:** `{final_name}`\n\nThumbnail was applied successfully! 🎨")
        
    except Exception as e:
        error_msg = f"❌ **Upload Failed**\n\nError: `{str(e)}`\n\nPlease try again."
        await progress_msg.edit_text(error_msg)
        logger.error(f"Upload error for user {user_id}: {e}")
        
    finally:
        # Clean up thumbnail file
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
                logger.info(f"Cleaned up thumbnail for user {user_id}")
            except Exception as e:
                logger.warning(f"Failed to delete thumbnail file: {e}")
        
        # Clean up user state
        user_states.pop(user_id, None)
        
    await cq.answer()

@app.on_message(filters.private & filters.command("cancel"))
async def cancel_process(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        user_states.pop(user_id, None)
        await message.reply_text("✅ Operation cancelled successfully!")
    else:
        await message.reply_text("ℹ️ No active operation to cancel.")

@app.on_message(filters.private & filters.command("help"))
async def help_cmd(client, message):
    await start_cmd(client, message)

# Error handler
@app.on_errors()
async def error_handler(client, error):
    logger.error(f"Error occurred: {error}")

if __name__ == "__main__":
    print("🤖 Rename Bot Started...")
    print("Bot is running...")
    app.run()
