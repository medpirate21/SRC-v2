import asyncio
import logging
from typing import Optional, Dict
from pyrogram import Client, filters, idle
from pyrogram.errors import UserAlreadyParticipant, InviteHashExpired, UsernameNotOccupied, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram import utils
from motor.motor_asyncio import AsyncIOMotorClient
from config import TOKEN, HASH, ID, USAGE, MONGODB_URI, DUMP_CHANNEL_ID
from utils import get_message_type, MediaHandler, cleanup_old_status_files
from pyrogram.handlers import CallbackQueryHandler
from task_manager import task_manager
from settings import Settings
from video_handler import split_video, get_video_duration

def get_peer_type_new(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"

utils.get_peer_type = get_peer_type_new
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def get_peer_type_new(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"
utils.get_peer_type = get_peer_type_new

processing_messages: Dict[int, bool] = {}  # user_id -> is_cancelled

def create_cancel_batch_button(user_id: int) -> InlineKeyboardMarkup:
    """Create cancel button for batch processing"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Batch", callback_data=f"cancel_batch_{user_id}")]
    ])

async def handle_cancel_batch(client: Client, callback_query: CallbackQuery):
    """Handle cancel batch button press"""
    user_id = int(callback_query.data.split('_')[2])
    processing_messages[user_id] = True
    await callback_query.answer("Batch processing cancelled")

class TelegramBot:
    def __init__(self):
        self.bot = Client("mybot", api_id=ID, api_hash=HASH, bot_token=TOKEN)
        self.mongo_client = AsyncIOMotorClient(MONGODB_URI)
        self.db = self.mongo_client.telegrami_bot
        self.sessions = self.db.sessions
        self.sessions_str = self.db.sessions_str
        self.user_sessions: Dict[int, Client] = {}  # Cache for active sessions
        self.user_auth_states: Dict[int, Dict] = {}  # Store user auth states
        self.semaphore = asyncio.Semaphore(10)  # Limit concurrent operations
        self.dump_channel_id = DUMP_CHANNEL_ID  # Assuming a dump_channel_id attribute
        self.bot.add_handler(CallbackQueryHandler(handle_cancel_batch, filters.regex(r'^cancel_batch_\d+$')))
        self.settings = Settings(self.db)  # Initialize settings

    async def initialize(self):
        """Initialize the bot and load sessions"""
        await self.bot.start()
        logger.info("Bot client initialized")
        
        # Clean up old status files
        await cleanup_old_status_files()
        
        # Add settings handlers
        @self.bot.on_message(filters.command("uset"))
        async def settings_handler(client, message):
            await self.settings.settings_command(client, message)

        @self.bot.on_message(filters.command(["settb"]))  # Change to list format
        async def thumbnail_handler(client, message):
            await self.settings.set_thumbnail(client, message)
        
        @self.bot.on_message(filters.command("setid"))
        async def channel_handler(client, message):
            await self.settings.set_channel(client, message)
        
        @self.bot.on_message(filters.command("setcc"))
        async def caption_handler(client, message):
            await self.settings.set_caption(client, message)
            
        @self.bot.on_message(filters.command("rm"))
        async def replacement_handler(client, message):
            await self.settings.set_replacement(client, message)
            
        # Load existing sessions from MongoDB
        async for session in self.sessions.find():
            try:
                user_id = session['user_id']
                string_session = session['session_string']
                
                # Create client with stored session
                user_client = Client(
                    f"user_{user_id}",
                    api_id=ID,
                    api_hash=HASH,
                    session_string=string_session
                )
                await user_client.start()
                
                # Store in memory cache
                self.user_sessions[user_id] = user_client
                logger.info(f"Loaded session for user {user_id}")
            except Exception as e:
                logger.error(f"Error loading session for user {session.get('user_id')}: {e}")

        @self.bot.on_callback_query()
        async def callback_handler(client, callback_query):
            await self.settings.handle_callback(client, callback_query)

    async def save_session(self, user_id: int, string_session: str):
        """Save session to MongoDB"""
        await self.sessions.update_one(
            {'user_id': user_id},
            {'$set': {
                'user_id': user_id,
                'session_string': string_session,
                'updated_at': asyncio.get_event_loop().time()
            }},
            upsert=True
        )
        logger.info(f"Saved session for user {user_id}")

    async def delete_session(self, user_id: int):
        """Delete session from MongoDB"""
        await self.sessions.delete_one({'user_id': user_id})
        logger.info(f"Deleted session for user {user_id}")

    async def get_user_session(self, user_id: int) -> Optional[Client]:
        """Get or create user session"""
        if user_id not in self.user_sessions:
            # Try to load from MongoDB
            session = await self.sessions.find_one({'user_id': user_id})
            if session:
                try:
                    user_client = Client(
                        f"user_{user_id}",
                        api_id=ID,
                        api_hash=HASH,
                        session_string=session['session_string']
                    )
                    await user_client.start()
                    self.user_sessions[user_id] = user_client
                    return user_client
                except Exception as e:
                    logger.error(f"Error loading session for user {user_id}: {e}")
                    await self.delete_session(user_id)
            return None
        return self.user_sessions[user_id]

    async def handle_private_message(self, message: Message, chatid: int, msgid: int):
        """Handle private message processing"""
        async with self.semaphore:
            user_session = await self.get_user_session(message.from_user.id)
            if not user_session:
                await self.bot.send_message(
                    message.chat.id,
                    "**Please sign in first using /signin**",
                    reply_to_message_id=message.id
                )
                return

            try:
                msg = await user_session.get_messages(chatid, msgid)
                if msg is None:
                    await self.bot.send_message(
                        message.chat.id,
                        "**Message not found. The message may have been deleted or you may not have access to it.**",
                        reply_to_message_id=message.id
                    )
                    return

                msg_type = get_message_type(msg)

                # Special handling for large videos
                if msg_type == "Video" and msg.video and msg.video.file_size > 2*1024*1024*1024:  # 2GB
                    await self.handle_large_video(message, msg)
                    return
                
                if msg_type == "Text":
                    await self.bot.send_message(
                        message.chat.id,
                        msg.text,
                        entities=msg.entities,
                        reply_to_message_id=message.id
                    )
                    return

                media_handler = MediaHandler(self.bot, user_session, db=self.db)  # Pass db instance when creating MediaHandler
                await media_handler.handle_media(message, msg, msg_type)
            except Exception as e:
                logger.error(f"Error handling private message: {e}")
                await self.bot.send_message(
                    message.chat.id,
                    f"**Error** : __{e}__",
                    reply_to_message_id=message.id
                )

    async def handle_public_message(self, message: Message, username: str, msgid: int):
        """Handle public message processing"""
        async with self.semaphore:
            try:
                # Try to get message directly with bot first
                try:
                    msg = await self.bot.get_messages(username, msgid)
                except UsernameNotOccupied:
                    await self.bot.send_message(
                        message.chat.id,
                        "**The username is not occupied by anyone**",
                        reply_to_message_id=message.id
                    )
                    return

                try:
                    # First forward to dump channel
                    if '?single' not in message.text:
                        dump_msg = await self.bot.copy_message(
                            self.dump_channel_id,
                            msg.chat.id,
                            msg.id
                        )
                        # Then forward to user
                        await self.bot.copy_message(
                            message.chat.id,
                            self.dump_channel_id,
                            dump_msg.id,
                            reply_to_message_id=message.id
                        )
                    else:
                        dump_msgs = await self.bot.copy_media_group(
                            self.dump_channel_id,
                            msg.chat.id,
                            msg.id
                        )
                        # Then forward to user
                        await self.bot.copy_media_group(
                            message.chat.id,
                            self.dump_channel_id,
                            dump_msgs[0].id,
                            reply_to_message_id=message.id
                        )
                except:
                    # If direct copy fails, try using user session
                    user_session = await self.get_user_session(message.from_user.id)
                    if not user_session:
                        await self.bot.send_message(
                            message.chat.id,
                            "**Please sign in first using /signin**",
                            reply_to_message_id=message.id
                        )
                        return
                    await self.handle_private_message(message, username, msgid)
            except Exception as e:
                logger.error(f"Error handling public message: {e}")
                await self.bot.send_message(
                    message.chat.id,
                    f"**Error** : __{e}__",
                    reply_to_message_id=message.id
                )

    async def handle_join_chat(self, message: Message):
        """Handle chat joining"""
        user_session = await self.get_user_session(message.from_user.id)
        if not user_session:
            await self.bot.send_message(
                message.chat.id,
                "**Please sign in first using /signin**",
                reply_to_message_id=message.id
            )
            return

        try:
            await user_session.join_chat(message.text)
            await self.bot.send_message(
                message.chat.id,
                "**Chat Joined**",
                reply_to_message_id=message.id
            )
        except UserAlreadyParticipant:
            await self.bot.send_message(
                message.chat.id,
                "**Chat already Joined**",
                reply_to_message_id=message.id
            )
        except InviteHashExpired:
            await self.bot.send_message(
                message.chat.id,
                "**Invalid Link**",
                reply_to_message_id=message.id
            )
        except Exception as e:
            await self.bot.send_message(
                message.chat.id,
                f"**Error** : __{e}__",
                reply_to_message_id=message.id
            )

    async def process_message(self, message: Message):
        """Process incoming message"""
        logger.info(f"Processing message from user {message.from_user.id}: {message.text}")

        if "https://t.me/+" in message.text or "https://t.me/joinchat/" in message.text:
            await self.handle_join_chat(message)
        elif "https://t.me/" in message.text:
            datas = message.text.split("/")
            temp = datas[-1].replace("?single", "").split("-")
            fromID = int(temp[0].strip())
            toID = int(temp[1].strip()) if len(temp) > 1 else fromID

            # Calculate total messages
            total_messages = toID - fromID + 1
            
            user_id = message.from_user.id
            processing_messages[user_id] = False  # Initialize cancellation status
            
            progress_message = await self.bot.send_message(
                message.chat.id,
                f"📥 **Processing {total_messages} messages...**",
                reply_to_message_id=message.id,
                reply_markup=create_cancel_batch_button(user_id)  # Add cancel button
            )
            
            try:
                await self.bot.pin_chat_message(message.chat.id, progress_message.id, both_sides=True, disable_notification=True)
            except Exception as e:
                logger.error(f"Error pinning message: {e}")

            processed = 0
            success = 0
            failed = 0

            # Process messages one by one
            for msgid in range(fromID, toID + 1):
                if processing_messages.get(user_id, False):
                    logger.info(f"Batch processing cancelled by user {user_id}")
                    await progress_message.edit_text("❌ Batch processing cancelled.")
                    break
                try:
                    if "https://t.me/c/" in message.text:
                        chatid = int("-100" + datas[4])
                        await self.handle_private_message(message, chatid, msgid)
                    elif "https://t.me/b/" in message.text:
                        username = datas[4]
                        await self.handle_private_message(message, username, msgid)
                    else:
                        username = datas[3]
                        await self.handle_public_message(message, username, msgid)
                    
                    success += 1
                except Exception as e:
                    logger.error(f"Error processing message {msgid}: {e}")
                    failed += 1
                
                processed += 1
                
                # Add delay between messages
                if processed < total_messages:
                    await asyncio.sleep(2)  # Rate limiting between messages

            # Final progress update
            if not processing_messages.get(user_id, False):
                await progress_message.edit_text(
                    f"✅ **Completed processing {total_messages} messages!**\n"
                    f"Successfully forwarded: {success}\n"
                    f"Failed: {failed}"
                )
            
            try:
                await self.bot.unpin_chat_message(message.chat.id, progress_message.id)
            except Exception as e:
                logger.error(f"Error unpinning message: {e}")
            
            if user_id in processing_messages:
                del processing_messages[user_id]

    async def start(self):
        await self.initialize()

        @self.bot.on_message(filters.command(["start"]))
        async def start_command(client: Client, message: Message):
            
            logger.info(f"Start command received from user {message.from_user.id}")
            intro_text = """**Glitch Save Bot**

I transform restricted content into accessible files.
Simply share a post link, and I'll handle the rest.

```
> Forward messages from private channels
> Customize file names and captions
> Set personal thumbnails
```
`Use` /uset `to configure your preferences`

`Type` /help `for detailed usage guide`"""
            await self.bot.send_message(
                message.chat.id,
                intro_text,
                reply_to_message_id=message.id
            )

        @self.bot.on_message(filters.command(["help"]))
        async def help_command(client: Client, message: Message):
            await self.bot.send_message(
                message.chat.id,
                USAGE,
                disable_web_page_preview=True,
                reply_to_message_id=message.id
            )
            
        @self.bot.on_message(filters.command(["signin"]))
        async def signin_command(client: Client, message: Message):
            user_id = message.from_user.id
            
            # Check if user already has a session
            if user_id in self.user_sessions:
                await self.bot.send_message(
                    message.chat.id,
                    "**You are already signed in!**\n"
                    "Use /logout to sign out first.",
                    reply_to_message_id=message.id
                )
                return

            # Start signin process
            await self.bot.send_message(
                message.chat.id,
                "**Please send your phone number in international format.**\n"
                "Example: `+919876543210`",
                reply_to_message_id=message.id
            )
            self.user_auth_states[user_id] = {"step": "phone"}

        @self.bot.on_message(filters.command(["cancel"]))
        async def cancel_user_task(client, message: Message):
            task_manager.cancel(message.from_user.id)
            await message.reply("✅ Your current task has been marked for cancellation.")

        @self.bot.on_message(filters.command(["log2"]))
        async def log2_command(client: Client, message: Message):
            user_id = message.from_user.id
            
            # Check if user already has a session
            if user_id in self.user_sessions:
                await self.bot.send_message(
                    message.chat.id,
                    "**You are already signed in!**\n"
                    "Use /logout to sign out first.",
                    reply_to_message_id=message.id
                )
                return

            # Start log2 process
            await self.bot.send_message(
                message.chat.id,
                "**Please send your phone number in international format.**\n"
                "Example: `+919876543210`",
                reply_to_message_id=message.id
            )
            self.user_auth_states[user_id] = {"step": "phone", "method": "log2"}

        @self.bot.on_message(filters.command(["setss"]))
        async def setss_command(client: Client, message: Message):
            user_id = message.from_user.id
            
            # Check if user already has a session
            if user_id in self.user_sessions:
                await self.bot.send_message(
                    message.chat.id,
                    "**You are already signed in!**\n"
                    "Use /logout to sign out first.",
                    reply_to_message_id=message.id
                )
                return

            # Get the session string from the message
            args = message.text.split()
            if len(args) < 2:
                await self.bot.send_message(
                    message.chat.id,
                    "**Please provide a session string.**\n"
                    "Usage: `/setss your_session_string`",
                    reply_to_message_id=message.id
                )
                return

            session_string = args[1].strip()
            
            try:
                # Create new client with provided session string
                user_client = Client(
                    f"user_{user_id}",
                    api_id=ID,
                    api_hash=HASH,
                    session_string=session_string
                )
                await user_client.start()
                
                # Verify the session is valid
                me = await user_client.get_me()
                if not me:
                    raise Exception("Invalid session string")
                
                # Save session to MongoDB
                await self.save_session(user_id, session_string)
                
                # Store the session in memory cache
                self.user_sessions[user_id] = user_client
                
                await self.bot.send_message(
                    message.chat.id,
                    "✅ **Session set successfully!**\n"
                    "Your session has been saved and will persist across restarts.",
                    reply_to_message_id=message.id
                )
            except Exception as e:
                logger.error(f"Error setting session: {e}")
                await self.bot.send_message(
                    message.chat.id,
                    f"**Error setting session: {e}**\n"
                    "Please check your session string and try again.",
                    reply_to_message_id=message.id
                )
                if 'user_client' in locals():
                    await user_client.disconnect()

        @self.bot.on_message(filters.text)
        async def handle_text_message(client: Client, message: Message):
            """Handle text messages for signin process"""
            # Skip if it's a command
            if message.text.startswith('/'):
                return
                
            user_id = message.from_user.id
            
            if user_id not in self.user_auth_states:
                await self.process_message(message)
                return

            auth_state = self.user_auth_states[user_id]
            
            if auth_state["step"] == "phone":
                # Store phone number and request code
                phone = message.text.strip()
                try:
                    # Create a new client for signin
                    signin_client = Client(
                        f"signin_{user_id}",
                        api_id=ID,
                        api_hash=HASH
                    )
                    await signin_client.connect()
                    
                    # Send code request
                    sent_code = await signin_client.send_code(phone)
                    
                    # Store signin client and phone code hash
                    self.user_auth_states[user_id].update({
                        "step": "code",
                        "phone_code_hash": sent_code.phone_code_hash,
                        "signin_client": signin_client,
                        "phone": phone
                    })
                    
                    await self.bot.send_message(
                        message.chat.id,
                        "**Please send the verification code you received.**\n"
                        "Example: `12345`",
                        reply_to_message_id=message.id
                    )
                except Exception as e:
                    logger.error(f"Error requesting code: {e}")
                    await self.bot.send_message(
                        message.chat.id,
                        f"**Error requesting code: {e}**\n"
                        "Please try again with /signin or use /setss to set a session string directly",
                        reply_to_message_id=message.id
                    )
                    del self.user_auth_states[user_id]
                    
            elif auth_state["step"] == "code":
                try:
                    # Get the code from message
                    code = message.text.strip()
                    
                    # Sign in with code
                    await auth_state["signin_client"].sign_in(
                        auth_state["phone"],
                        auth_state["phone_code_hash"],
                        code
                    )
                    
                    # Get session string
                    string_session = await auth_state["signin_client"].export_session_string()
                    
                    # Save session to MongoDB
                    await self.save_session(user_id, string_session)
                    
                    # Create new client with generated session
                    user_client = Client(
                        f"user_{user_id}",
                        api_id=ID,
                        api_hash=HASH,
                        session_string=string_session
                    )
                    await user_client.start()
                    
                    # Store the session in memory cache
                    self.user_sessions[user_id] = user_client
                    
                    # Clean up signin client
                    await auth_state["signin_client"].disconnect()
                    del self.user_auth_states[user_id]
                    
                    await self.bot.send_message(
                        message.chat.id,
                        "✅ **Sign in successful!**\n"
                        "Your session has been saved and will persist across restarts.",
                        reply_to_message_id=message.id
                    )
                except PhoneCodeInvalid:
                    await self.bot.send_message(
                        message.chat.id,
                        "❌ **Invalid code.**\n"
                        "Please try again with /signin",
                        reply_to_message_id=message.id
                    )
                    await auth_state["signin_client"].disconnect()
                    del self.user_auth_states[user_id]
                except PhoneCodeExpired:
                    await self.bot.send_message(
                        message.chat.id,
                        "❌ **Code expired.**\n"
                        "Please try again with /signin",
                        reply_to_message_id=message.id
                    )
                    await auth_state["signin_client"].disconnect()
                    del self.user_auth_states[user_id]
                except SessionPasswordNeeded:
                    await self.bot.send_message(
                        message.chat.id,
                        "**Please send your 2FA password.**",
                        reply_to_message_id=message.id
                    )
                    self.user_auth_states[user_id]["step"] = "password"
                except Exception as e:
                    logger.error(f"Error during sign in: {e}")
                    await self.bot.send_message(
                        message.chat.id,
                        f"**Error during sign in: {e}**\n"
                        "Please try again with /signin",
                        reply_to_message_id=message.id
                    )
                    if "signin_client" in auth_state:
                        await auth_state["signin_client"].disconnect()
                    del self.user_auth_states[user_id]

        @self.bot.on_message(filters.command(["logout"]))
        async def logout_command(client: Client, message: Message):
            """Handle logout command"""
            user_id = message.from_user.id
            if user_id in self.user_sessions:
                try:
                    await self.user_sessions[user_id].stop()
                    del self.user_sessions[user_id]
                    await self.delete_session(user_id)
                    await self.bot.send_message(
                        message.chat.id,
                        "✅ **Logged out successfully!**",
                        reply_to_message_id=message.id
                    )
                except Exception as e:
                    logger.error(f"Error logging out: {e}")
                    await self.bot.send_message(
                        message.chat.id,
                        f"**Error logging out: {e}**",
                        reply_to_message_id=message.id
                    )
            else:
                await self.bot.send_message(
                    message.chat.id,
                    "**You are not signed in!**",
                    reply_to_message_id=message.id
                )

        logger.info("Bot started and running...")
        await idle()

async def main():
    bot = TelegramBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
