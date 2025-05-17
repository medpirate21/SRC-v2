from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto
from utils import process_thumbnail, get_user_thumbnail, get_destination_channel, set_destination_channel
from utils import set_user_caption, get_user_caption
import os

class Settings:
    def __init__(self, db):
        self.db = db

    async def get_user_settings(self, user_id: int) -> dict:
        result = await self.db.users.find_one({'_id': user_id})
        return result or {}

    async def build_settings_text(self, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        """Build settings text and keyboard"""
        settings = await self.get_user_settings(user_id)
        
        # Check thumbnail
        thumb_path = await get_user_thumbnail(user_id, self.db)
        thumb_status = "✓ " if thumb_path else "X"
        
        # Check destination channel
        dest_channel = await get_destination_channel(user_id, self.db)
        channel_status = f" {dest_channel}" if dest_channel else "X"

        # Get replacements
        replacements = settings.get('replacements', {})
        replace_text = "X " if not replacements else "\n".join(
            f"• {k} → {v if v else '[REMOVE]'}" for k, v in replacements.items()
        )
        
        # Get caption
        caption, use_filename = await get_user_caption(user_id, self.db)
        caption_status = "X"
        if caption:
            caption_status = caption[:30] + ('...' if len(caption) > 30 else '')
            if use_filename:
                caption_status += ' (with filename)'

        settings_text = f"""
  **User Settings** `{user_id}` 

 **Thumbnail :** {thumb_status} 
 **Channel :** {channel_status}
 **Caption :** {caption_status}
 **Remname Rules :** {replace_text}

"""

        

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Clear Caption", callback_data=f"clear_caption_{user_id}"), InlineKeyboardButton("🗑 Clear Channel", callback_data=f"clear_channel_{user_id}")],
            
            [InlineKeyboardButton("🗑 Clear Rules", callback_data=f"clear_rules_{user_id}"), InlineKeyboardButton("🗑 Clear Thumb", callback_data=f"clear_thumb_{user_id}")],
            
            [InlineKeyboardButton("❌ Close", callback_data=f"close_settings_{user_id}")]
        ])

        return settings_text, keyboard

    async def settings_command(self, client: Client, message: Message):
        user_id = message.from_user.id
        thumb_path = await get_user_thumbnail(user_id, self.db)
        settings_text, keyboard = await self.build_settings_text(user_id)

        if thumb_path and os.path.exists(thumb_path):
            await message.reply_photo(
                photo=thumb_path,
                caption=settings_text,
                reply_markup=keyboard
            )
        else:
            await message.reply_photo(
                photo="thumbnail.jpg",
                caption=settings_text,
                reply_markup=keyboard
            )

    async def handle_callback(self, client: Client, callback: CallbackQuery):
        data, user_id = callback.data.rsplit("_", 1)
        user_id = int(user_id)

        if callback.from_user.id != user_id:
            await callback.answer("This is not your settings menu!", show_alert=True)
            return

        msg = callback.message

        if data == "clear_caption":
            await self.db.users.update_one(
                {'_id': user_id},
                {'$unset': {'caption': 1, 'caption_with_filename': 1}}
            )
            await callback.answer("Caption cleared!")

        elif data == "clear_channel":
            await self.db.users.update_one(
                {'_id': user_id},
                {'$unset': {'destination_channel': 1}}
            )
            await callback.answer("Destination channel cleared!")

        elif data == "clear_rules":
            await self.db.users.update_one(
                {'_id': user_id},
                {'$unset': {'replacements': 1}}
            )
            await callback.answer("Filename rules cleared!")

        elif data == "clear_thumb":
            result = await self.db.users.find_one({'_id': user_id})
            if result and 'thumb_path' in result:
                thumb_path = result['thumb_path']
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
            await self.db.users.update_one(
                {'_id': user_id},
                {'$unset': {'thumb_path': 1}}
            )
            await callback.answer("Thumbnail cleared!")

        elif data == "close_settings":
            await msg.delete()
            return

        # Update settings text if action was taken
        if data != "close_settings":
            settings_text, keyboard = await self.build_settings_text(user_id)
            thumb_path = await get_user_thumbnail(user_id, self.db)
            
            if thumb_path and os.path.exists(thumb_path):
                await msg.edit_media(
                    media=InputMediaPhoto(thumb_path, caption=settings_text),
                    reply_markup=keyboard
                )
            else:
                await msg.edit_media(
                    media=InputMediaPhoto("thumbnail.jpg", caption=settings_text),
                    reply_markup=keyboard
                )

    async def set_thumbnail(self, client: Client, message: Message):
        replied = message.reply_to_message
        if not replied or not replied.photo:
            help_text = """
** Set Custom Thumbnail ‼️**
```
• Reply to a photo with /settb to set it as thumbnail
• The image will be resized to 320x180
• The thumbnail will be used for all your uploads
• Bot will use default thumbnail if none set
```
**Steps:**
1. Send/Forward an image
2. Reply to it with /settb

**Note:** You can see your current thumbnail in /uset"""
            await message.reply_text(help_text)
            return
        
        user_id = message.from_user.id
        photo_path = await replied.download()
        
        thumb_path = await process_thumbnail(user_id, photo_path, self.db)
        os.remove(photo_path)  # Clean up downloaded photo
        
        if thumb_path:
            await message.reply_text("✅ Custom thumbnail has been set successfully!")
        else:
            await message.reply_text("❌ Failed to set thumbnail. Please try again.")

    async def set_channel(self, client: Client, message: Message):
        try:
            args = message.text.split(None, 1)
            if len(args) < 2:
                help_text = """
**Set Destination Channel ‼️**

```
• Use /setid -100xxxxxxxxxxxx
• Get channel ID by forwarding message from channel to @MissRose_bot
• Bot must be admin in the channel

Steps:
1. Create a channel
2. Add this bot as admin
3. Forward any message from channel to <a href='https://t.me/MissRose_bot'>@MissRose_bot</a>
4. Copy the channel ID (starts with -100)
5. Use /setid with the ID
```
**Example:**
`/setid -100123456789`

**Note:** All files will be sent to both dump and your channel
"""
                await message.reply_text(help_text)
                return

            channel_id = int(args[1])
            
            # Verify bot has access to channel
            try:
                await client.get_chat(channel_id)
            except Exception:
                await message.reply_text(
                    "❌ Failed to access channel. Please:\n"
                    "1. Make sure the ID is correct\n"
                    "2. Add the bot as admin in your channel"
                )
                return

            # Save to database
            if await set_destination_channel(message.from_user.id, channel_id, self.db):
                await message.reply_text("✅ Destination channel set successfully!")
            else:
                await message.reply_text("❌ Failed to set destination channel")

        except ValueError:
            await message.reply_text("❌ Invalid channel ID format")
        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}")

    async def set_replacement(self, client: Client, message: Message):
        try:
            args = message.text.split(None, 1)
            if len(args) < 2:
                help_text = """```
**📝 Filename Replacement Rules ‼️**
```
1️⃣Remove Usernames:
• Use: `/rm username` (without @)
• Example: `/rm dams`
• This removes all instances of @dams, @DAMS, etc.

2️⃣Replace Words:
• Use: `/rm oldword-newword`
• Example: `/rm lecture-class`
• This replaces "lecture" with "class"

3️⃣Multiple Rules:
Send commands separately for multiple rules.
```
**Examples:**
`/rm dams` → Removes @dams
`/rm cat-dog` → Replaces "cat" with "dog"

⚠️ Note: Don't use @ symbol in commands```
"""
                await message.reply_text(help_text)
                return

            rule = args[1].strip()
            user_id = message.from_user.id
            
            # Get current replacements
            settings = await self.get_user_settings(user_id)
            replacements = settings.get('replacements', {})
            
            if '-' in rule:
                # Replace word case
                old, new = rule.split('-', 1)
                replacements[old.strip()] = new.strip()
            else:
                # Remove word case
                word = rule.strip()
                replacements[word] = ''
            
            # Save to database
            await self.db.users.update_one(
                {'_id': user_id},
                {'$set': {'replacements': replacements}},
                upsert=True
            )
            
            await message.reply_text("✅ Filename rule added successfully!")
            
        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}")

    async def set_caption(self, client: Client, message: Message):
        try:
            args = message.text.split(None, 1)
            if len(args) < 2:
                help_text = """
**📝 Set Custom Caption ‼️**
```
1️⃣Fixed Caption:
• /setcc Your caption text
• Same caption for all files

2️⃣ Dynamic Caption:
• /setcc {filename} Your caption
• {filename} will be replaced with actual filename

3️⃣Clear Caption:
• /setcc clear
• Removes custom caption
```
**Examples:**
• `/setcc Join @mychannel`
• `/setcc 📁 {filename}\n👥 @mychannel`

**Note:** Supports markdown formatting
"""
                await message.reply_text(help_text)
                return

            caption_text = args[1].strip()
            if caption_text.lower() == 'clear':
                await set_user_caption(message.from_user.id, None, False, self.db)
                await message.reply_text("✅ Custom caption cleared!")
                return

            use_filename = '{filename}' in caption_text
            await set_user_caption(message.from_user.id, caption_text, use_filename, self.db)
            
            sample = caption_text.replace("{filename}", "example") if use_filename else caption_text
            await message.reply_text(
                "✅ Caption set successfully!\n\n"
                f"**Preview:**\n{sample}"
            )

        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}")
