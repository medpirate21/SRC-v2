import os
import asyncio
import logging
import glob

import re
from typing import Optional
from pyrogram import Client
from pyrogram.types import Message
from config import DUMP_CHANNEL_ID
from PIL import Image
from task_manager import task_manager
from video_handler import split_video, cleanup_split_files

# Remove MongoDB imports and initialization since it's in main.py
logger = logging.getLogger(__name__)

THUMB_DIR = "thumbs"
os.makedirs(THUMB_DIR, exist_ok=True)

async def process_thumbnail(user_id: int, photo_path: str, db) -> Optional[str]:
    """Process and save user thumbnail"""
    try:
        thumb_path = os.path.join(THUMB_DIR, f"{user_id}_thumb.jpg")
        with Image.open(photo_path) as img:
            img.resize((320, 180), Image.Resampling.LANCZOS).save(thumb_path, "JPEG")
        # Store path in MongoDB using passed db instance
        await db.users.update_one(
            {'_id': user_id},
            {'$set': {'thumb_path': thumb_path}},
            upsert=True
        )
        return thumb_path
    except Exception as e:
        logger.error(f"Error processing thumbnail: {e}")
        return None

async def get_user_thumbnail(user_id: int, db) -> Optional[str]:
    """Get user's thumbnail path from MongoDB"""
    result = await db.users.find_one({'_id': user_id})
    if result and 'thumb_path' in result:
        thumb_path = result['thumb_path']
        return thumb_path if os.path.exists(thumb_path) else None
    return None

async def set_destination_channel(user_id: int, channel_id: int, db) -> bool:
    """Save user's destination channel ID to MongoDB"""
    try:
        await db.users.update_one(
            {'_id': user_id},
            {'$set': {'destination_channel': channel_id}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving destination channel: {e}")
        return False

async def get_destination_channel(user_id: int, db) -> Optional[int]:
    """Get user's destination channel from MongoDB"""
    result = await db.users.find_one({'_id': user_id})
    return result.get('destination_channel') if result else None

async def cleanup_old_status_files():
    try:
        for file in glob.glob("*status.txt"):
            os.remove(file)
    except Exception as e:
        logger.error(f"Error cleaning up status files: {e}")

async def downstatus(statusfile: str, message: Message, bot: Client, filename: str):
    while not os.path.exists(statusfile):
        await asyncio.sleep(3)

    while os.path.exists(statusfile):
        user_id = message.from_user.id if message.from_user else message.chat.id
        if task_manager.is_cancelled(user_id):

            break
        try:
            with open(statusfile, "r") as f:
                data = f.read().split("|")
                if len(data) == 3:
                    percentage, current, total = data
                    cur_mb = int(current) / (1024 * 1024)
                    total_mb = int(total) / (1024 * 1024)
                    speed = cur_mb / 5

                    progress_bar = create_progress_bar(float(percentage))
                    text = (
                        f"📥 **Downloading**\n\n`{filename}`\n\n"
                        f"```\n{progress_bar}\nProgress: {percentage}%\n"
                        f"Size: {cur_mb:.1f}MB / {total_mb:.1f}MB\n"
                        f"Speed: {speed:.1f}MB/s\n"
                        
                        f"ETA: {((total_mb - cur_mb) / speed):.1f}s\n```"
                        f"Cancel this task using /cancel\n"
                    )
                    await bot.edit_message_text(message.chat.id, message.id, text)
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error in download status: {e}")
            await asyncio.sleep(5)

async def upstatus(statusfile: str, message: Message, bot: Client, filename: str):
    while not os.path.exists(statusfile):
        await asyncio.sleep(3)

    while os.path.exists(statusfile):
        user_id = message.from_user.id if message.from_user else message.chat.id
        if task_manager.is_cancelled(user_id):

            break
        try:
            with open(statusfile, "r") as f:
                data = f.read().split("|")
                if len(data) == 3:
                    percentage, current, total = data
                    cur_mb = int(current) / (1024 * 1024)
                    total_mb = int(total) / (1024 * 1024)
                    speed = cur_mb / 5

                    progress_bar = create_progress_bar(float(percentage))
                    text = (
                        f"📤 **Uploading**\n\n`{filename}`\n\n"
                        f"```\n{progress_bar}\nProgress: {percentage}%\n"
                        f"Size: {cur_mb:.1f}MB / {total_mb:.1f}MB\n"
                        f"Speed: {speed:.1f}MB/s\n"
                        f"ETA: {((total_mb - cur_mb) / speed):.1f}s\n```"
                        f"Cancel this task using  /cancel\n"
                    )
                    await bot.edit_message_text(message.chat.id, message.id, text)
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error in upload status: {e}")
            await asyncio.sleep(5)

def create_progress_bar(percentage: float) -> str:
    filled = int(20 * (percentage / 100))
    bar = '▰' * filled + '▱' * (20 - filled)
    return f"`[{bar}]`"

async def progress(current: int, total: int, message: Message, type: str):
    percentage = (current * 100) / total

    # Cancel if user requested
    if task_manager.is_cancelled(message.from_user.id):
        raise asyncio.CancelledError("Upload cancelled by user.")

    async with asyncio.Lock():
        with open(f'{message.id}{type}status.txt', "w") as f:
            f.write(f"{percentage:.1f}|{current}|{total}")

def get_message_type(msg: Message) -> str:
    try:
        msg.document.file_id; return "Document"
    except: pass
    try:
        msg.video.file_id; return "Video"
    except: pass
    try:
        msg.animation.file_id; return "Animation"
    except: pass
    try:
        msg.sticker.file_id; return "Sticker"
    except: pass
    try:
        msg.voice.file_id; return "Voice"
    except: pass
    try:
        msg.audio.file_id; return "Audio"
    except: pass
    try:
        msg.photo.file_id; return "Photo"
    except: pass
    try:
        msg.text; return "Text"
    except: pass
    return "Unknown"

async def cleanup_files(message_id: int):
    for file in [f"{message_id}downstatus.txt", f"{message_id}upstatus.txt"]:
        if os.path.exists(file):
            os.remove(file)

async def get_user_replacements(user_id: int, db) -> dict:
    """Get user's filename replacement rules"""
    result = await db.users.find_one({'_id': user_id})
    return result.get('replacements', {}) if result else {}

def sanitize_filename(name: str, user_replacements: dict = None) -> str:
    """Sanitize filename using user-specific replacement rules"""
    if user_replacements:
        for pattern, replacement in user_replacements.items():
            if pattern.startswith('@') or not replacement:
                # Remove pattern completely
                name = re.sub(rf'\b{pattern[1:] if pattern.startswith("@") else pattern}\b', '', name, flags=re.IGNORECASE)
            else:
                # Replace with specified text
                name = re.sub(rf'\b{pattern}\b', replacement, name, flags=re.IGNORECASE)
    
    # Remove any remaining unsafe characters
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.strip()

async def set_user_caption(user_id: int, caption: str, use_filename: bool, db) -> bool:
    """Save user's custom caption to MongoDB"""
    try:
        await db.users.update_one(
            {'_id': user_id},
            {'$set': {
                'caption': caption,
                'caption_with_filename': use_filename
            }},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving caption: {e}")
        return False

async def get_user_caption(user_id: int, db) -> tuple[Optional[str], bool]:
    """Get user's custom caption and filename flag from MongoDB"""
    result = await db.users.find_one({'_id': user_id})
    if result:
        return result.get('caption'), result.get('caption_with_filename', False)
    return None, False

class MediaHandler:
    def __init__(self, bot: Client, acc: Optional[Client] = None, db=None):
        self.bot = bot
        self.acc = acc
        self.db = db  # Add db instance
        self.semaphore = asyncio.Semaphore(5)
        self.dump_channel_id = DUMP_CHANNEL_ID
        self.rename_folder = "rename"
        os.makedirs(self.rename_folder, exist_ok=True)
        self.thumb_dir = THUMB_DIR

    async def handle_media(self, message: Message, msg: Message, msg_type: str):
        async with self.semaphore:
            # Get user replacements
            user_replacements = await get_user_replacements(message.from_user.id, self.db)
            
            filename = getattr(msg, msg_type.lower(), None)
            filename = getattr(filename, "file_name", "Unknown") if filename else "Unknown"
            filename = sanitize_filename(filename, user_replacements)

            smsg = await self.bot.send_message(message.chat.id, f"📥 **Downloading**\n`{filename}`", reply_to_message_id=message.id)
            down_task = asyncio.create_task(downstatus(f"{message.id}downstatus.txt", smsg, self.bot, filename))

            file = None
            try:
                if task_manager.is_cancelled(message.from_user.id):
                    raise asyncio.CancelledError("Download cancelled.")

                file = await self.acc.download_media(
                    msg,
                    progress=progress,
                    progress_args=[message, "down"]
                )

                if task_manager.is_cancelled(message.from_user.id):
                    raise asyncio.CancelledError("Download cancelled after complete.")

                await cleanup_files(message.id)

                # Rename
                if os.path.exists(file):
                    ext = os.path.splitext(file)[1]
                    new_file = os.path.join(self.rename_folder, f"{filename}{ext}")
                    os.rename(file, new_file)
                    file = new_file

                await smsg.edit_text(f"📤 **Uploading**\n`{filename}`")
                up_task = asyncio.create_task(upstatus(f"{message.id}upstatus.txt", smsg, self.bot, filename))

                # Send to dump
                dump_msg = await self._send_media_to_dump(file, msg, msg_type, message)

                # Send to user
                await self.bot.copy_message(message.chat.id, self.dump_channel_id, dump_msg.id)
                await cleanup_files(message.id)

            except asyncio.CancelledError as ce:
                task_manager.clear(message.from_user.id)
                await smsg.edit_text("❌ Task cancelled.")
                await cleanup_files(message.id)
                if file and os.path.exists(file): os.remove(file)
                return

            except Exception as e:
                logger.error(f"MediaHandler error: {e}")
                await self.bot.send_message(message.chat.id, f"**Error**: {e}", reply_to_message_id=message.id)

            finally:
                await cleanup_files(message.id)
                await self.bot.delete_messages(message.chat.id, [smsg.id])
                if file and os.path.exists(file): os.remove(file)

    async def _send_media_to_dump(self, file: str, msg: Message, msg_type: str, message: Message):
        user_thumb = await get_user_thumbnail(message.from_user.id, self.db)
        thumb = user_thumb if user_thumb else "thumbnail.jpg"
        
        try:
            # Send to dump channel first
            dump_msg = await self._send_media(self.dump_channel_id, file, msg, msg_type, message, thumb)
            
            # Check if user has custom destination
            dest_channel = await get_destination_channel(message.from_user.id, self.db)
            if dest_channel:
                try:
                    await self.bot.copy_message(
                        dest_channel,
                        self.dump_channel_id,
                        dump_msg.id
                    )
                except Exception as e:
                    logger.error(f"Failed to send to destination channel: {e}")
                    await self.bot.send_message(
                        message.chat.id,
                        "⚠️ Failed to send to your destination channel. Please ensure the bot is admin there."
                    )
            
            return dump_msg

        finally:
            for t in ["resized_thumb.jpg"]:
                if os.path.exists(t): os.remove(t)

    async def _send_media(self, chat_id: int, file: str, msg: Message, msg_type: str, message: Message, thumb: str):
        # Get user's custom caption
        caption, use_filename = await get_user_caption(message.from_user.id, self.db)
        if caption:
            if use_filename:
                filename = os.path.splitext(os.path.basename(file))[0]
                final_caption = caption.replace("{filename}", filename)
            else:
                final_caption = caption
            # Reset entities when using custom caption
            entities = None
        else:
            final_caption = msg.caption
            entities = msg.caption_entities

        if msg_type == "Document":
            return await self.bot.send_document(
                chat_id, file, thumb=thumb, caption=final_caption,
                caption_entities=entities, progress=progress,
                progress_args=[message, "up"]
            )
        elif msg_type == "Video":
            return await self.bot.send_video(
                chat_id, file, duration=msg.video.duration,
                width=320, height=180, thumb=thumb, caption=final_caption,
                caption_entities=entities, progress=progress,
                progress_args=[message, "up"]
            )
        elif msg_type == "Animation":
            return await self.bot.send_animation(
                chat_id, file, thumb=thumb, progress=progress,
                progress_args=[message, "up"]
            )
        elif msg_type == "Sticker":
            return await self.bot.send_sticker(chat_id, file)
        elif msg_type == "Voice":
            return await self.bot.send_voice(
                chat_id, file, caption=final_caption,
                caption_entities=entities, progress=progress,
                progress_args=[message, "up"]
            )
        elif msg_type == "Audio":
            return await self.bot.send_audio(
                chat_id, file, thumb=thumb, caption=final_caption,
                caption_entities=entities, progress=progress,
                progress_args=[message, "up"]
            )
        elif msg_type == "Photo":
            return await self.bot.send_photo(
                chat_id, file, caption=final_caption,
                caption_entities=entities, progress=progress,
                progress_args=[message, "up"]
            )

    async def handle_large_video(self, message: Message, msg: Message):
        """Handle videos larger than 2GB by splitting"""
        status_msg = await self.bot.send_message(
            message.chat.id,
            "📥 **Processing large video...\nDownloading and splitting into parts...**",
            reply_to_message_id=message.id
        )
        
        try:
            # Download video
            file_path = await msg.download()
            
            # Split video
            split_files = await split_video(file_path)
            
            # Upload parts
            for i, part_path in enumerate(split_files, 1):
                caption = f"**{msg.caption or msg.video.file_name}**\n"
                caption += f"Part {i} of {len(split_files)}\n"
                if i == 1:
                    caption += "\n**Note:** Use any video joiner to combine parts after download."
                
                await self.bot.send_video(
                    message.chat.id,
                    video=part_path,
                    caption=caption,
                    thumb=msg.video.thumbs[0].file_id if msg.video.thumbs else None,
                    progress=self.progress_callback,
                    progress_args=(status_msg,)
                )
                
            await status_msg.edit_text("✅ **Video parts uploaded successfully!**")
            
        except Exception as e:
            logger.error(f"Error handling large video: {e}")
            await status_msg.edit_text(f"❌ **Error processing video: {str(e)}**")
            
        finally:
            # Cleanup
            if 'file_path' in locals():
                try:
                    os.remove(file_path)
                except:
                    pass
            if 'split_files' in locals():
                await cleanup_split_files(split_files)
