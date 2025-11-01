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

class Config:
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    DB_URL = os.environ.get("DB_URL", "")
    DB_NAME = "RenameBot"

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

health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

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

async def process_thumb_async(ph_path):
    pass

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

db = Database(Config.DB_URL, Config.DB_NAME)

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

user_states = {}

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

@app.on_message(filters.private & filters.command("cover"))
async def cover_command(client, message):
    user_id = message.from_user.id
    
    if not message.reply_to_message:
        await message.reply_text("**❌ Please reply /cover to a video message!**")
        return
    
    replied_message = message.reply_to_message
    
    if not replied_message.video:
        await message.reply_text("**❌ Please reply to a video file!**")
        return
    
    thumbnail = await db.get_thumbnail(user_id)
    if not thumbnail:
        await message.reply_text("**❌ No thumbnail found in database!**")
        return
    
    try:
        video = replied_message.video
        video_file_id = video.file_id
        video_file_name = getattr(video, 'file_name', 'video.mp4')
        video_duration = getattr(video, 'duration', 0)
        
        progress_msg = await message.reply_text("🔄 Applying cover to video...")
        
        download_path = f"downloads/cover_{user_id}_{video_file_id}.mp4"
        os.makedirs("downloads", exist_ok=True)
        
        start_time = time.time()
        file_path = await client.download_media(
            replied_message,
            file_name=download_path,
            progress=progress_for_pyrogram,
            progress_args=("📥 **Downloading Video**", progress_msg, start_time, video_file_name)
        )
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Video download failed")
        
        thumb_path = f"downloads/thumb_{user_id}.jpg"
        thumb_path = await client.download_media(thumbnail, file_name=thumb_path)
        
        start_time = time.time()
        
        await client.send_video(
            chat_id=message.chat.id,
            video=file_path,
            thumb=thumb_path,
            caption=f"**🎥 Video with Custom Cover**\n`{video_file_name}`",
            duration=video_duration,
            supports_streaming=True,
            progress=progress_for_pyrogram,
            progress_args=("📤 **Uploading Video with Cover**", progress_msg, start_time, video_file_name)
        )
        
        await message.reply_text("**✅ Video Cover Applied Successfully!**")
        
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"**❌ Error applying cover:** `{str(e)}`"
        await message.reply_text(error_msg)
        logging.error(f"Cover error: {e}")
    
    finally:
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

@app.on_message(filters.private & filters.command("cover2"))
async def cover_command_advanced(client, message):
    user_id = message.from_user.id
    
    if not message.reply_to_message:
        await message.reply_text("**❌ Please reply /cover2 to a video message!**")
        return
    
    replied_message = message.reply_to_message
    
    if not replied_message.video:
        await message.reply_text("**❌ Please reply to a video file!**")
        return
    
    thumbnail = await db.get_thumbnail(user_id)
    if not thumbnail:
        await message.reply_text("**❌ No thumbnail found in database!**")
        return
    
    try:
        video = replied_message.video
        video_file_id = video.file_id
        
        thumb_path = f"downloads/thumb_adv_{user_id}.jpg"
        thumb_path = await client.download_media(thumbnail, file_name=thumb_path)
        
        progress_msg = await message.reply_text("🔄 Applying cover (advanced method)...")
        
        await client.send_video(
            chat_id=message.chat.id,
            video=video_file_id,
            thumb=thumb_path,
            caption="**🎥 Video with Custom Cover**\n(Advanced Method)",
            duration=video.duration,
            supports_streaming=True
        )
        
        await message.reply_text("**✅ Advanced cover applied!**")
        
        try:
            await progress_msg.delete()
        except:
            pass
        
    except Exception as e:
        await message.reply_text(f"**❌ Advanced cover error:** `{str(e)}`")
        logging.error(f"Advanced cover error: {e}")
    
    finally:
        if 'thumb_path' in locals() and thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except:
                pass

@app.on_message(filters.private & filters.command("cover_help"))
async def cover_help(client, message):
    help_text = """
**🎥 Video Cover Commands:**

**/cover** - Apply your thumbnail as video cover
• Reply to a video with `/cover`
• Uses thumbnail from your database
• Creates new video with custom cover

**/cover2** - Advanced cover feature
• Alternative method using file_id
• Faster for some videos

**How to use:**
1. First set a thumbnail by sending a photo
2. Send or forward a video to me
3. Reply to the video with `/cover`
4. I'll send back the video with your custom cover
"""
    await message.reply_text(help_text)

@app.on_message(filters.private & filters.command("cancel"))
async def cancel_command(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        del user_states[user_id]
        await message.reply_text("**✅ Process cancelled successfully!**")
    else:
        await message.reply_text("**❌ No active process to cancel.**")

@app.on_message(filters.private & filters.command("start"))
async def start_command(client, message):
    await message.reply_text(
        "**👋 Hello! I'm a File Rename Bot**\n\n"
        "**How to use:**\n"
        "1. Send me any file (document/video/audio)\n"
        "2. Click 'Rename' button\n"
        "3. Enter new filename\n"
        "4. Select upload type\n\n"
        "**🎥 Video Cover Feature:**\n"
        "• Set thumbnail by sending a photo\n"
        "• Reply /cover to any video to apply cover\n\n"
        "**Thumbnail Commands:**\n"
        "• Send a photo to set thumbnail\n"
        "• /view_thumb - View current thumbnail\n"
        "• /del_thumb - Delete thumbnail\n"
        "• /cover_help - Video cover help\n\n"
        "**Other Commands:**\n"
        "• /cancel - Cancel current process"
    )

@app.on_message(filters.private & (filters.document | filters.video | filters.audio))
async def handle_file(client, message):
    user_id = message.from_user.id
    
    if user_id in user_states:
        await message.reply_text("**❌ Please complete your current process first!**\nUse /cancel to cancel.")
        return
    
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
        'step': 'awaiting_rename'
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

@app.on_callback_query(filters.regex("^start_rename$"))
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
        
        progress_msg = await callback_query.message.reply_text("🔄 Processing your file...")
        
        start_time = time.time()
        
        file_path = await client.download_media(
            original_message,
            file_name=download_path,
            progress=progress_for_pyrogram,
            progress_args=("📥 **Downloading File**", progress_msg, start_time, final_filename)
        )
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Download failed")
        
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
            except:
                pass
        
        start_time = time.time()
        
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
        else:
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
        
        duration_text = convert_seconds(original_duration) if original_duration > 0 else "Unknown"
        await callback_query.message.reply_text(
            f"**✅ File Renamed Successfully!**\n\n"
            f"**New Name:** `{final_filename}`\n"
            f"**Type:** `{upload_type.title()}`\n"
            f"**Duration:** `{duration_text}`"
        )
        
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"**❌ Error:** `{str(e)}`"
        await callback_query.message.reply_text(error_msg)
        logging.error(f"Upload error: {e}")
    
    finally:
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
        
        if user_id in user_states:
            del user_states[user_id]

@app.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "view_thumb", "del_thumb", "cover", "cover2", "cover_help"]))
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
    
    clean_name = re.sub(r'[<>:"/\\|?*]', '', new_name)
    
    if not clean_name:
        await message.reply_text("**❌ Invalid filename!**")
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
    user_data = user_states[user_id]
    
    try:
        file_info = user_data['file_info']
        original_message = file_info['original_message']
        original_duration = file_info['duration']
        
        download_path = f"downloads/{final_name}"
        os.makedirs("downloads", exist_ok=True)
        
        progress_msg = await message.reply_text("🔄 Processing your file...")
        
        start_time = time.time()
        
        file_path = await client.download_media(
            original_message,
            file_name=download_path,
            progress=progress_for_pyrogram,
            progress_args=("📥 **Downloading File**", progress_msg, start_time, final_name)
        )
        
        if not file_path or not os.path.exists(file_path):
            raise Exception("Download failed")
        
        thumbnail = await db.get_thumbnail(user_id)
        thumb_path = None
        if thumbnail:
            try:
                thumb_path = await client.download_media(thumbnail)
            except:
                pass
        
        start_time = time.time()
        
        await client.send_document(
            message.chat.id,
            document=file_path,
            thumb=thumb_path,
            caption=f"`{final_name}`",
            progress=progress_for_pyrogram,
            progress_args=("📤 **Uploading File**", progress_msg, start_time, final_name)
        )
        
        await message.reply_text(
            f"**✅ File Renamed Successfully!**\n\n"
            f"**New Name:** `{final_name}`\n"
            f"**Type:** `Document`"
        )
        
        try:
            await progress_msg.delete()
        except:
            pass
            
    except Exception as e:
        error_msg = f"**❌ Error:** `{str(e)}`"
        await message.reply_text(error_msg)
        logging.error(f"Upload error: {e}")
    
    finally:
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
        
        if user_id in user_states:
            del user_states[user_id]

if __name__ == "__main__":
    print("🚀 Bot is starting...")
    print("🌐 Health check server running on port 8080")
    app.run()
