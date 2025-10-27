import os
import asyncio
import logging
import math
import time
from flask import Flask
from threading import Thread
from PIL import Image
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import QueryIdInvalid
import motor.motor_asyncio

# ========== FLASK SERVER FOR RENDER ==========
app_web = Flask(__name__)

@app_web.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app_web.run(host='0.0.0.0', port=8080, debug=False)

# Start Flask server in a thread
flask_thread = Thread(target=run_flask)
flask_thread.daemon = True
flask_thread.start()

# ========== CONFIG ==========
class Config:
    API_ID = 123456  # Replace with your API ID
    API_HASH = "your_api_hash_here"  # Replace with your API HASH
    BOT_TOKEN = "your_bot_token_here"  # Replace with your Bot Token
    DB_URL = "mongodb_url_here"  # Replace with your MongoDB URL
    DB_NAME = "RenameBot"

# ========== UTILITY FUNCTIONS ==========
async def progress_for_pyrogram(current, total, ud_type, message, start):
    now = time.time()
    diff = now - start
    if round(diff % 5.00) == 0 or current == total:        
        percentage = current * 100 / total
        speed = current / diff
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000
        estimated_total_time = elapsed_time + time_to_completion

        elapsed_time = TimeFormatter(milliseconds=elapsed_time)
        estimated_total_time = TimeFormatter(milliseconds=estimated_total_time)

        progress = "{0}{1}".format(
            ''.join(["■" for i in range(math.floor(percentage / 8.34))]),
            ''.join(["□" for i in range(12 - math.floor(percentage / 8.34))])
        )            
        tmp = progress + " {0}%\n├ 🗂️ {1} / {2}\n├ 🚀 {3}/s\n└ ⏰ {4}".format(
            round(percentage, 2),
            humanbytes(current),
            humanbytes(total),
            humanbytes(speed),            
            estimated_total_time if estimated_total_time != '' else "0 s"
        )
        try:
            await message.edit(text=f"**{ud_type}**\n\n{tmp}")
        except:
            pass

def humanbytes(size):    
    if not size:
        return ""
    power = 2**10
    n = 0
    Dic_powerN = {0: ' ', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + Dic_powerN[n] + 'B'

def TimeFormatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
        ((str(hours) + "h, ") if hours else "") + \
        ((str(minutes) + "m, ") if minutes else "") + \
        ((str(seconds) + "s, ") if seconds else "") + \
        ((str(milliseconds) + "ms, ") if milliseconds else "")
    return tmp[:-2] 

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
    elif message.video:
        file = message.video
        file_type = "video"
    elif message.audio:
        file = message.audio
        file_type = "audio"
    else:
        return

    file_name = getattr(file, 'file_name', 'Unknown')
    file_size = humanbytes(getattr(file, 'file_size', 0))
    
    # Store file info
    user_states[user_id] = {
        'file_info': {
            'file_name': file_name,
            'file_size': file_size,
            'file_type': file_type,
            'original_message': message,
            'file_id': file.file_id
        },
        'step': 'awaiting_rename'
    }

    # Show file info with buttons
    info_text = f"""**📁 File Information:**

**Name:** `{file_name}`
**Size:** `{file_size}`
**Type:** `{file_type.title()}`

**Click RENAME to continue.**"""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Rename", callback_data="start_rename")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_process")]
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
    
    await callback_query.message.edit_text(
        "**📝 Enter the new filename:**\n\n"
        "**Note:** Don't include file extension\n"
        "Example: `my_renamed_file`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_process")]
        ])
    )
    await callback_query.answer()

@app.on_callback_query(filters.regex("^cancel_process$"))
async def cancel_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id in user_states:
        del user_states[user_id]
    await callback_query.message.edit_text("**❌ Process cancelled!**")
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
        await callback_query.message.edit_text("**🔄 Starting process...**")
        
        file_info = user_data['file_info']
        new_filename = user_data['new_filename']
        original_message = file_info['original_message']
        
        # Get original extension
        original_name = file_info['file_name']
        _, original_ext = os.path.splitext(original_name)
        final_filename = f"{new_filename}{original_ext}"
        
        # Download path
        download_path = f"downloads/{final_filename}"
        os.makedirs("downloads", exist_ok=True)
        
        # Download file
        download_msg = await callback_query.message.edit_text("**📥 Downloading...**")
        start_time = time.time()
        
        file_path = await client.download_media(
            original_message,
            file_name=download_path,
            progress=progress_for_pyrogram,
            progress_args=("📥 Downloading", download_msg, start_time)
        )
        
        if not file_path:
            raise Exception("Download failed")
        
        # Get thumbnail
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            thumb_path = await client.download_media(thumbnail)
        
        # Upload file
        await download_msg.edit_text("**📤 Uploading...**")
        start_time = time.time()
        
        if upload_type == "document":
            await client.send_document(
                callback_query.message.chat.id,
                document=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading", download_msg, start_time)
            )
        else:  # video
            await client.send_video(
                callback_query.message.chat.id,
                video=file_path,
                thumb=thumb_path,
                caption=f"`{final_filename}`",
                supports_streaming=True,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading", download_msg, start_time)
            )
        
        # Success message
        await callback_query.message.reply_text(
            f"**✅ File Renamed Successfully!**\n\n"
            f"**New Name:** `{final_filename}`\n"
            f"**Type:** `{upload_type.title()}`"
        )
        
        # Cleanup download message
        try:
            await download_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"**❌ Error:** `{str(e)}`"
        await callback_query.message.edit_text(error_msg)
        logging.error(f"Upload error: {e}")
    
    finally:
        # Cleanup files
        if 'file_path' in locals() and os.path.exists(file_path):
            os.remove(file_path)
        if 'thumb_path' in locals() and thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)
        
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
    
    # Show upload type selection
    original_name = user_data['file_info']['file_name']
    _, original_ext = os.path.splitext(original_name)
    final_name = f"{clean_name}{original_ext}"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Document", callback_data="upload_document")],
        [InlineKeyboardButton("🎥 Video", callback_data="upload_video")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_process")]
    ])
    
    await message.reply_text(
        f"**Select Upload Type:**\n\n**File:** `{final_name}`",
        reply_markup=keyboard
    )

# ========== START BOT ==========
if __name__ == "__main__":
    print("🚀 Bot is starting...")
    print("🌐 Flask server running on port 8080")
    app.run()
