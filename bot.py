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
    await message.reply_text("Send me a file to rename. Send a photo to set thumbnail.\nCommands:\n/view_thumb\n/del_thumb\n/cancel")

@app.on_message(filters.private & filters.photo)
async def save_thumb(client, message):
    await db.set_thumbnail(message.from_user.id, message.photo.file_id)
    await message.reply_text("✅ Thumbnail saved!")

@app.on_message(filters.private & filters.command(["view_thumb", "viewthumbnail"]))
async def view_thumb(client, message):
    thumb_id = await db.get_thumbnail(message.from_user.id)
    if thumb_id:
        await client.send_photo(message.chat.id, thumb_id, caption="Your saved thumbnail")
    else:
        await message.reply_text("No thumbnail set.")

@app.on_message(filters.private & filters.command(["del_thumb", "deletethumbnail"]))
async def del_thumb(client, message):
    await db.set_thumbnail(message.from_user.id, None)
    await message.reply_text("Thumbnail deleted.")

@app.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def file_received(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        await message.reply_text("Finish your current process or /cancel first.")
        return
    file = message.document or message.video or message.audio
    file_name = getattr(file, "file_name", "Unknown")
    file_size = humanbytes(getattr(file, "file_size", 0))
    duration = getattr(file, "duration", 0)
    file_type = "Document" if message.document else "Video" if message.video else "Audio"
    user_states[user_id] = {"file_info": {"file_name": file_name, "file_id": file.file_id,
                                          "file_type": file_type, "duration": duration, "original_message": message},
                            "step": "awaiting_rename"}
    duration_str = convert_seconds(duration) if duration else "N/A"
    text = f"File info:\nName: `{file_name}`\nSize: `{file_size}`\nType: `{file_type}`\nDuration: `{duration_str}`\nClick Rename to proceed."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Rename", callback_data="start_rename")]])
    await message.reply_text(text, reply_markup=keyboard)

@app.on_callback_query(filters.regex("^start_rename$"))
async def rename_start(client, cq):
    user_id = cq.from_user.id
    if user_id not in user_states:
        await cq.answer("Session expired. Send file again.", show_alert=True)
        return
    user_states[user_id]["step"] = "awaiting_filename"
    try:
        await cq.message.delete()
    except: pass
    msg = await cq.message.reply_text("Send new filename without extension:")
    user_states[user_id]["ask_message_id"] = msg.id
    await cq.answer()

@app.on_message(filters.private & filters.text & ~filters.command(["start","cancel","view_thumb","del_thumb"]))
async def rename_receive(client, message):
    user_id = message.from_user.id
    if user_id not in user_states or user_states[user_id].get("step") != "awaiting_filename":
        return
    new_name = message.text.strip()
    if not new_name:
        await message.reply_text("Filename cannot be empty!")
        return
    new_name = re.sub(r'[<>:"/\\|?*]', '', new_name)
    if not new_name:
        await message.reply_text("Invalid filename!")
        return
    user_states[user_id]["new_filename"] = new_name
    user_states[user_id]["step"] = "awaiting_upload_type"
    try:
        await message.delete()
    except: pass
    if "ask_message_id" in user_states[user_id]:
        try:
            await client.delete_messages(message.chat.id, user_states[user_id]["ask_message_id"])
        except: pass

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Document", callback_data="upload_document")],
        [InlineKeyboardButton("🎥 Video", callback_data="upload_video")]
    ])
    final_name = new_name  # extension will be appended on upload
    await message.reply_text(f"Select upload type for `{final_name}`:", reply_markup=keyboard)

@app.on_callback_query(filters.regex("^upload_(document|video)$"))
async def upload_file(client, cq):
    user_id = cq.from_user.id
    if user_id not in user_states:
        await cq.answer("Session expired!", show_alert=True)
        return
    upload_type = cq.data.split("_")[1]
    user_data = user_states[user_id]
    file_info = user_data["file_info"]
    new_name = user_data.get("new_filename", "renamed_file")

    original_name = file_info["file_name"] or "file"
    _, ext = os.path.splitext(original_name)
    if not ext:
        ext = ".mp4" if file_info["file_type"].lower() == "video" else ".bin"
    final_name = f"{new_name}{ext}"

    thumb_file_id = await db.get_thumbnail(user_id)
    thumb_path = None

    progress_msg = await cq.message.reply_text("Downloading thumbnail (if any)...")
    try:
        # Download thumbnail only, if exists
        if thumb_file_id:
            thumb_path = await client.download_media(thumb_file_id)
    except Exception as e:
        logging.warning(f"Thumbnail download failed: {e}")

    try:
        await cq.message.delete()
    except: pass

    try:
        # Send file by reusing file_id, with downloaded thumbnail if any
        if upload_type == "document" or ext.lower() in [".txt", ".pdf", ".doc", ".docx", ".html", ".htm"]:
            await client.send_document(
                user_id,
                document=file_info["file_id"],
                thumb=thumb_path,
                caption=f"`{final_name}`"
            )
        else:
            await client.send_video(
                user_id,
                video=file_info["file_id"],
                thumb=thumb_path,
                caption=f"`{final_name}`",
                duration=file_info.get("duration", 0),
                supports_streaming=True
            )
        await cq.message.reply_text(f"✅ Renamed and sent `{final_name}` successfully!")
    except Exception as e:
        await cq.message.reply_text(f"❌ Error: {e}")
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except: pass
        user_states.pop(user_id, None)
        try:
            await progress_msg.delete()
        except: pass
    await cq.answer()

@app.on_message(filters.private & filters.command("cancel"))
async def cancel_process(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        user_states.pop(user_id, None)
        await message.reply_text("✅ Process cancelled!")
    else:
        await message.reply_text("No active process to cancel.")

if __name__ == "__main__":
    print("Bot started...")
    app.run()
