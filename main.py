import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'game-handlers'))

import io
from PIL import Image, ImageDraw, ImageFont

import json
import logging
import asyncio
import random
import threading
import html
from typing import Optional, Dict, List, Tuple, Union
import subprocess
import urllib.request
import urllib.error
from dotenv import load_dotenv
from flask import Flask

from telegram import Update, Chat, ChatMember, ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton, ReactionTypeEmoji
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    PollAnswerHandler,
    PollHandler,
    TypeHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatType, ChatMemberStatus, ParseMode
from telegram import InlineQueryResultArticle, InputTextMessageContent, InlineQueryResultCachedPhoto, InlineQueryResultCachedSticker
from telegram.error import NetworkError, Forbidden, TimedOut, TelegramError

from datetime import datetime, timedelta

from game_manager import GameManager, GameState, GameSession
from story_builder import StoryBuilderGame
from guess_the_imposter import GuessTheImposterGame
from guess_the_logo import GuessTheLogoGame
from guess_the_movie import GuessTheMovieGame
from guess_the_flag import GuessTheFlagGame
from guessmoji import GuessMojiGame
from general_knowledge import GeneralKnowledgeGame
from guess_character import GuessCharacterGame
from word_connect import WordConnectGame
from wdym_game import MemeGame
from taylor_shakespeare import TaylorShakespeareGame
from twenty_questions import TwentyQuestionsGame
from guess_the_song import GuessTheSongGame
from song_from_lyrics import SongFromLyricsGame
from riddles_game import RiddlesGame
from crazy_eight import Crazy8Game
from guess_the_book import GuessTheBookGame
from guess_the_marvel import GuessMarvelGame
from guess_addis import GuessAddisGame
from hear_me_out import HearMeOutGame
from name_the_player import NameThePlayerGame
from settings_manager import settings_manager
from who_am_i import WhoAmIGame
from leaderboard import (
    record_game_scores,
    get_total_leaderboard,
    get_game_leaderboard,
    get_game_names,
    GAME_CODE_NAMES,
)

def _raw_get_managed_bot_token(master_token: str, bot_id: int) -> str | None:
    """
    Call getManagedBotToken directly via plain HTTPS, bypassing PTB's model
    layer so this works on any PTB version.
    """
    url = f"https://api.telegram.org/bot{master_token}/getManagedBotToken"
    # Telegram treats bots as users — the API uses "user_id", not "bot_id"
    payload = json.dumps({"user_id": bot_id}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                # Telegram returns {"ok": true, "result": "<token>"}
                result = data.get("result", "")
                if isinstance(result, str):
                    return result
                # Some API versions may wrap it in an object
                if isinstance(result, dict):
                    return result.get("token") or result.get("access_token")
    except urllib.error.HTTPError as e:
        logger.error(f"getManagedBotToken HTTP {e.code}: {e.read().decode()}")
    except Exception as e:
        logger.error(f"getManagedBotToken request failed: {e}")
    return None


async def clone_bot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detects managed bot creation and spawns a clone process."""
    message = update.effective_message
    if not message:
        return

    # ── Method 1: PTB exposes managed_bot_created natively ──────────────────
    bot_id: int | None = None

    managed_bot_created = getattr(message, "managed_bot_created", None)
    if managed_bot_created:
        bot_obj = getattr(managed_bot_created, "bot", None)
        bot_id = getattr(bot_obj, "id", None) if bot_obj else None

    # ── Method 2: message.api_kwargs (PTB stores unknown API fields here) ────
    # If the installed PTB version doesn't model managed_bot_created natively,
    # Telegram's unrecognised fields land in message.api_kwargs as raw dicts.
    if not bot_id:
        try:
            api_kwargs = getattr(message, "api_kwargs", {}) or {}
            mbc = api_kwargs.get("managed_bot_created", {})
            if mbc:
                raw_bot = mbc.get("bot", {})
                bot_id = raw_bot.get("id")
        except Exception as e:
            logger.debug(f"api_kwargs check failed: {e}")

    # ── Method 3: Reconstruct from update.to_dict() ──────────────────────────
    if not bot_id:
        try:
            raw = update.to_dict()
            raw_msg = raw.get("message") or raw.get("edited_message") or {}
            mbc = raw_msg.get("managed_bot_created", {})
            if mbc:
                raw_bot = mbc.get("bot", {})
                bot_id = raw_bot.get("id")
        except Exception as e:
            logger.debug(f"Could not parse raw update for managed_bot_created: {e}")

    if not bot_id:
        # This update is unrelated to managed bot creation
        return

    logger.info(f"managed_bot_created detected for bot_id={bot_id}, fetching token...")

    # ── Fetch token: try PTB method first, fall back to direct HTTP ──────────
    token: str | None = None

    # PTB native (only works if library version supports it)
    try:
        # PTB may use bot_id= or user_id= depending on version; try both
        try:
            result = await context.bot.get_managed_bot_token(user_id=bot_id)
        except TypeError:
            result = await context.bot.get_managed_bot_token(bot_id=bot_id)
        if isinstance(result, str) and ":" in result:
            token = result
        elif hasattr(result, "token"):
            token = result.token
    except Exception:
        pass  # Library doesn't support it — fall through to HTTP

    # Direct HTTPS fallback (always works)
    if not token:
        token = await asyncio.get_event_loop().run_in_executor(
            None, _raw_get_managed_bot_token, context.bot.token, bot_id
        )

    if not token or ":" not in token:
        logger.error(f"Could not retrieve a valid token for managed bot {bot_id}")
        if update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ <b>Failed to retrieve the managed bot token.</b>\nPlease try again.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return

    # Avoid accidentally restarting the master bot
    if token == context.bot.token:
        return

    logger.info(f"Spawning clone process for managed bot {bot_id}")
    env = os.environ.copy()
    # load_dotenv(override=False) ensures this token won't be clobbered by .env
    env["BOT_TOKEN"] = token
    env["DISABLE_FLASK"] = "1"

    # Persist the token so this clone is re-spawned after a master bot restart
    _save_managed_bot_token(token)

    # Set the managed bot's profile picture
    pic_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "_bot", "managed_bot_pic.png")
    if os.path.exists(pic_path):
        try:
            import httpx
            import json
            with open(pic_path, "rb") as f:
                photo_payload = json.dumps({"type": "static", "photo": "attach://profile_pic"})
                response = httpx.post(
                    f"https://api.telegram.org/bot{token}/setMyProfilePhoto",
                    data={"photo": photo_payload},
                    files={"profile_pic": ("managed_bot_pic.png", f, "image/png")},
                    timeout=30.0
                )
            logger.info(f"setMyProfilePhoto HTTP code: {response.status_code}, response: {response.text}")
        except Exception as e:
            logger.error(f"Failed to set profile photo via httpx: {e}")

    subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env)

    if update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="✅ <b>Clone Instance Started!</b>\nYour managed bot is now running and ready to host games.",
                parse_mode="HTML"
            )
        except Exception:
            pass



# Load environment variables
# override=False ensures child processes keep their injected BOT_TOKEN
load_dotenv(override=False)

try:
    from supabase import create_client, Client
except ImportError:
    Client = None

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Optional['Client'] = None

if SUPABASE_URL and SUPABASE_KEY and Client:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")

# ── Managed-bot persistence ──────────────────────────────────────────────────
# Tokens of all created clone bots are stored here so they survive restarts.
MANAGED_BOTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "managed_bots.json")

def _load_managed_bot_tokens() -> list[str]:
    """Return the list of saved clone-bot tokens."""
    tokens = []
    
    # Try loading from Supabase first
    if supabase:
        try:
            response = supabase.table("managed_bots").select("token").execute()
            if response.data:
                tokens = [row["token"] for row in response.data]
        except Exception as e:
            logger.error(f"Supabase load error: {e}")
            
    # Try local JSON fallback
    try:
        if os.path.exists(MANAGED_BOTS_FILE):
            with open(MANAGED_BOTS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for t in data:
                        if t not in tokens:
                            tokens.append(t)
    except Exception as e:
        logger.error(f"Failed to load managed_bots.json: {e}")
    return tokens

def _save_managed_bot_token(token: str) -> None:
    """Persist a new clone-bot token (deduplicated)."""
    tokens = _load_managed_bot_tokens()
    if token not in tokens:
        tokens.append(token)
        
        # Save to Supabase
        if supabase:
            try:
                # We use upsert in case the record already exists
                supabase.table("managed_bots").upsert({"token": token}).execute()
                logger.info("Saved new managed bot token to Supabase")
            except Exception as e:
                logger.error(f"Supabase save error: {e}")
        
        # Save local JSON fallback
        try:
            with open(MANAGED_BOTS_FILE, "w") as f:
                json.dump(tokens, f, indent=2)
            logger.info(f"Saved new managed bot token to {MANAGED_BOTS_FILE}")
        except Exception as e:
            logger.error(f"Failed to save managed_bots.json: {e}")

def _remove_managed_bot_token(token: str) -> None:
    """Remove a clone-bot token (e.g. if it is invalid)."""
    if supabase:
        try:
            supabase.table("managed_bots").delete().eq("token", token).execute()
        except Exception as e:
            logger.error(f"Supabase delete error: {e}")
            
    tokens = [t for t in _load_managed_bot_tokens() if t != token]
    try:
        with open(MANAGED_BOTS_FILE, "w") as f:
            json.dump(tokens, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to update managed_bots.json: {e}")

def spawn_saved_clone_bots(master_token: str) -> None:
    """Re-spawn all clone bots that were created in a previous run."""
    tokens = _load_managed_bot_tokens()
    if not tokens:
        return
    logger.info(f"Re-spawning {len(tokens)} saved managed bot(s)...")
    for token in tokens:
        if token == master_token:
            continue  # safety guard
        env = os.environ.copy()
        env["BOT_TOKEN"] = token
        env["DISABLE_FLASK"] = "1"
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env)
        logger.info(f"Re-spawned clone bot (token prefix: {token[:10]}...)")
# ─────────────────────────────────────────────────────────────────────────────

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)



# Initialize game manager
game_manager = GameManager()

# Global task tracker for game-related background tasks (timers, hints, reminders)
active_game_tasks: Dict[int, List[asyncio.Task]] = {}

def track_game_task(chat_id: int, task: asyncio.Task) -> None:
    """Register a background task for a specific game chat."""
    if chat_id not in active_game_tasks:
        active_game_tasks[chat_id] = []
    active_game_tasks[chat_id].append(task)
    # Clean up finished tasks from the list occasionally
    task.add_done_callback(lambda t: active_game_tasks[chat_id].remove(t) if chat_id in active_game_tasks and t in active_game_tasks[chat_id] else None)

def cancel_game_tasks(chat_id: int) -> None:
    """Cancel all background tasks for a specific game chat."""
    if chat_id in active_game_tasks:
        for task in active_game_tasks[chat_id]:
            if not task.done():
                task.cancel()
        del active_game_tasks[chat_id]


# Storage Chat ID for background meme caching
STORAGE_CHAT_ID = -1003696845309  # Testing Group

# Global lock for meme caching
meme_cache_lock = asyncio.Lock()
# Global lock for card caching
card_cache_lock = asyncio.Lock()


# Quirky response messages
QUIRKY_RESPONSES = [
    "Yeah… that’s not happening",
    "I considered it. Briefly. No.",
    "I refuse, and I stand by that decision",
    "Absolutely not. Hope this helps.",
    "I said no in several timelines.",
    "The request was processed and found unnecessary",
    "I refuse to participate in this chaos.",
    "I could… but I won’t",
    "This request embarrasses you.",
    "I’d rather do nothing.",
    "Please don’t ever ask that again",
    "I’m pretending I didn’t see that.",
    "I’d rather reboot",
    "Ere",
    "Ask me again and I’ll still say no.",
    "I refuse to acknowledge this."
]


# Game Categories for the Menu (Emojis removed)
GAME_CATEGORIES = {
    "Word Games": {
        "games": [("1", "Word Unscramble"), ("11", "Word Connect"), ("2", "Story Builder")]
    },
    "Guessing Games": {
        "games": [
            ("4", "Guess the Logo"), ("5", "GuessMoji"), ("10", "Guess the Character"),
            ("6", "Guess the Movie"), ("18", "Guess the Book"), ("19", "Guess the Marvel Character"),
            ("20", "Guess Addis"), ("22", "Name the Player"), ("23", "Movie Scene")
        ]
    },
    "Trivia & Knowledge": {
        "games": [
            ("9", "General Knowledge"), ("13", "Taylor Swift Or Shakespeare"),
            ("7", "Guess the Flag"), ("27", "Riddles")
        ]
    },
    "Music & Media": {
        "games": [("16", "Guess the Song"), ("26", "Song From Lyrics"), ("12", "What You Meme")]
    },
    "Party Games": {
        "games": [("3", "Guess the Imposter"), ("15", "20 Questions"), ("21", "Hear Me Out"), ("25", "Who Am I")]
    },
    "Card Games": {
        "games": [("17", "Crazy 8")]
    }
}


# Games Metadata for List Menu
GAMES_METADATA = {
    "1": ("Word Unscramble", "2"),
    "2": ("Story Builder", "2"),
    "3": ("Guess the Imposter", "3"),
    "4": ("Guess the Logo", "2"),
    "5": ("GuessMoji", "2"),
    "6": ("Guess the Movie", "2"),
    "7": ("Guess the Flag", "2"),
    "9": ("General Knowledge", "2"),
    "10": ("Guess the Character", "2"),
    "11": ("Word Connect", "2"),
    "12": ("What You Meme", "2"),
    "13": ("Taylor Swift Or Shakespeare", "2"),
    "15": ("20 Questions", "2"),
    "16": ("Guess the Song", "2"),
    "17": ("Crazy 8", "2"),
    "18": ("Guess the Book", "2"),
    "19": ("Guess the Marvel Character", "2"),
    "20": ("Guess Addis", "2"),
    "21": ("Hear Me Out", "2"),
    "22": ("Name the Player", "2"),
    "23": ("Movie Scene", "2"),
    "25": ("Who Am I", "2"),
    "26": ("Song From Lyrics", "2"),
    "27": ("Riddles", "2")
}

# Game Cover Images
GAME_COVERS = {
    "1": "Word Unscramble.png",
    "2": "Story Builder.png",
    "3": "Guess the Impostor.png",
    "4": "Guess the Logo.png",
    "5": "Guessmoji.png",
    "6": "Guess the Movie.png",
    "7": "Guess the Flag.png",
    "9": "General Knowledge.png",
    "10": "Harry Potter.png",
    "11": "Word Connect.png",
    "12": "What You Meme.png",
    "13": "Taylor Swift or Shakesphere.png",
    "15": "20 Questions.png",
    "16": "Guess the Song.png",
    "17": "Crazy 9.png",
    "18": "Guess the Book.png",
    "19": "Marvel Trivia.png",
    "20": "Guess Addis.png",
    "21": "Hear Me Out.png",
    "22": "Name the Player.png",
    "23": "Movie Scene.png",
    "25": "Who Am I.png",
    "26": "Guess the Song.png",
    "27": "Riddles.png"
}


async def is_user_mod(chat: Chat, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if a user is a moderator (Admin or Owner) in the group."""
    if chat.type == ChatType.PRIVATE:
        return True
    try:
        member = await chat.get_member(user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.error(f"Error checking mod status: {e}")
        return False


async def check_bot_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the bot has admin privileges in the chat.
    
    Args:
        update: Telegram update object
        context: Callback context
        
    Returns:
        True if bot is admin, False otherwise
    """
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return True
    
    try:
        bot_member = await chat.get_member(context.bot.id)
        return bot_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False


async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bot being added to or removed from a chat."""
    result = extract_status_change(update.my_chat_member)
    if result is None:
        return
    
    was_member, is_member = result
    chat = update.effective_chat
    
    # Bot was just added to a group
    if not was_member and is_member and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        logger.info(f"Bot added to chat {chat.id}: {chat.title}")
        


        # Check if bot is admin
        is_admin = await check_bot_is_admin(update, context)
        
        if not is_admin:
            await context.bot.send_message(
                chat_id=chat.id,
                text="Admin Privileges Required\n\n"
                     "Grant admin privileges, then run /start to begin",
                parse_mode="HTML"
            )
        else:
            await context.bot.send_message(
                chat_id=chat.id,
                text="<b>Bot Ready</b>\n\n"
                     "I'm ready to host games. Use /start to begin.",
                parse_mode="HTML"
            )


def extract_status_change(chat_member_update: ChatMemberUpdated) -> Optional[tuple[bool, bool]]:
    """Extract status change from ChatMemberUpdated."""
    status_change = chat_member_update.difference().get("status")
    if status_change is None:
        return None
    
    old_is_member = chat_member_update.old_chat_member.status in [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
    ]
    new_is_member = chat_member_update.new_chat_member.status in [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
    ]
    
    return old_is_member, new_is_member


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command to initiate a game selection."""
    chat = update.effective_chat
    user = update.effective_user
    
    # Only work in groups
    if chat.type == ChatType.PRIVATE:
        bot_username = context.bot.username or "GamioBot"
        master_username = os.environ.get("MASTER_BOT_USERNAME", "gamiorobot")
        is_clone = False
        if bot_username.lower() != master_username.lower():
            is_clone = True
            
        # Clone URL: if clone, point to master. if master, point to bot creation flow.
        clone_url = f"https://t.me/{master_username}" if is_clone else f"https://t.me/newbot/{bot_username}/my_gamio_bot"
        
        keyboard = [
            [InlineKeyboardButton("Add me to group", url=f"https://t.me/{bot_username}?startgroup=true")],
            [
                InlineKeyboardButton("Clone Bot", url=clone_url),
                InlineKeyboardButton("Help", callback_data="help")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Sup\n\n"
            "Add me to a group and make me an admin to start playing games.",
            reply_markup=reply_markup
        )
        return

    


    # Check if bot is admin
    is_admin = await check_bot_is_admin(update, context)
    if not is_admin:
        await update.message.reply_text(
            "<b> Admin access require</b> "
            "Grant admin privileges to host games."
        )
        return
    
    # Check if there's already an active game
    if game_manager.has_active_game(chat.id):
        await update.message.reply_text(random.choice(QUIRKY_RESPONSES))
        return
    
    # Create a new game session
    session = game_manager.create_game(chat.id)
    session.initiator_id = user.id
    
    # Check menu style setting
    menu_style = settings_manager.get_setting(chat.id, "menu_style", "inline")
    
    if menu_style == "list":
        # Original numbered list
        text = "<b>Gamio</b> 🕹\n\n"
        text += "Please select a game by sending its code:\n\n"
        for code, (name, _) in GAMES_METADATA.items():
            text += f"<b>{code}</b> - {name}\n"
        text += "\nSend the game code to continue..."
        
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        # Categories keyboard - 2 columns
        keyboard = []
        cat_names = list(GAME_CATEGORIES.keys())
        for i in range(0, len(cat_names), 2):
            row = [
                InlineKeyboardButton(cat_names[i], callback_data=f"game_cat_{cat_names[i]}", api_kwargs={"style": "primary"})
            ]
            if i + 1 < len(cat_names):
                row.append(InlineKeyboardButton(cat_names[i+1], callback_data=f"game_cat_{cat_names[i+1]}", api_kwargs={"style": "primary"}))
            keyboard.append(row)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "<b>Gamio</b> 🕹\n\n"
            "Please select a game category to see available games:",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )


async def new_round_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new4, /new5, /new6, /newsong, /newriddle commands to start a new endless round."""
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
        
    session = game_manager.get_game(chat.id)
    if not session or session.state != GameState.IN_PROGRESS:
        return
        
    if session.game_code == "1":
        if getattr(session.game, 'endless', False):
            await start_round(chat.id, context)
    elif session.game_code == "26":
        if getattr(session.game, 'endless', False):
            await start_sfl_round(chat.id, context)
    elif session.game_code == "27":
        if getattr(session.game, 'endless', False) and not session.game.round_in_progress:
            await start_riddle_round(chat.id, context)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settings command for moderators."""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("This command only works in groups.")
        return
        
    if not await is_user_mod(chat, user.id, context):
        await update.message.reply_text("<b><i>Only moderators can change settings.</i></b>")
        return
        
    menu_style = settings_manager.get_setting(chat.id, "menu_style", "inline")
    
    keyboard = [[
        InlineKeyboardButton(
            f"Menu Style: {menu_style.capitalize()}", 
            callback_data="set_toggle_menu"
        )
    ]]
    
    await update.message.reply_text(
        "⚙️ <b>Group Settings</b>\n\n"
        "Configure how the bot behaves in this group:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle settings menu interactions."""
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    
    if not await is_user_mod(chat, user.id, context):
        await query.answer("Only group moderators can change settings.", show_alert=True)
        return
        
    if query.data == "set_toggle_menu":
        current = settings_manager.get_setting(chat.id, "menu_style", "inline")
        new_style = "list" if current == "inline" else "inline"
        settings_manager.set_setting(chat.id, "menu_style", new_style)
        
        keyboard = [[
            InlineKeyboardButton(
                f"Menu Style: {new_style.capitalize()}", 
                callback_data="set_toggle_menu"
            )
        ]]
        
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        await query.answer(f"Menu style changed to {new_style}")


async def handle_game_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle game category and game selection callbacks."""
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    session = game_manager.get_game(chat_id)
    if not session:
        await query.answer("No active game. Use /start to begin one.", show_alert=True)
        return
    
    # Only initiator can pick game
    if session.initiator_id and user_id != session.initiator_id:
        await query.answer("Only the person who started the session can choose/configure the game", show_alert=True)
        return

    data = query.data
    
    if data.startswith("game_cat_"):
        category = data.replace("game_cat_", "")
        if category in GAME_CATEGORIES:
            cat_data = GAME_CATEGORIES[category]
            keyboard = []
            
            # Add games in category - 2 columns
            games_list = cat_data["games"]
            for i in range(0, len(games_list), 2):
                row = [
                    InlineKeyboardButton(games_list[i][1], callback_data=f"game_pick_{games_list[i][0]}", api_kwargs={"style": "primary"})
                ]
                if i + 1 < len(games_list):
                    row.append(InlineKeyboardButton(games_list[i+1][1], callback_data=f"game_pick_{games_list[i+1][0]}", api_kwargs={"style": "primary"}))
                keyboard.append(row)
            
            # Add back button
            keyboard.append([InlineKeyboardButton("⬅️", callback_data="game_menu_main", api_kwargs={"style": "success"})])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"<b>{category}</b>\n\n"
                "Select a game to start:",
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            await query.answer()
            
    elif data == "game_menu_main":
        # Back to categories - 2 columns
        keyboard = []
        cat_names = list(GAME_CATEGORIES.keys())
        for i in range(0, len(cat_names), 2):
            row = [
                InlineKeyboardButton(cat_names[i], callback_data=f"game_cat_{cat_names[i]}", api_kwargs={"style": "primary"})
            ]
            if i + 1 < len(cat_names):
                row.append(InlineKeyboardButton(cat_names[i+1], callback_data=f"game_cat_{cat_names[i+1]}", api_kwargs={"style": "primary"}))
            keyboard.append(row)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "<b>Gamio</b> 🕹\n\n"
            "Please select a game category to see available games:",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        await query.answer()
        
    elif data.startswith("opt_"):
        if data in ["opt_done_1", "opt_done_2", "opt_done_3", "opt_done_4", "opt_done_5", "opt_done_9", "opt_done_26", "opt_done_27"]:
            session.is_configuring = False
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer("Options saved.")
        elif data.startswith("opt_3_ni_"):
            val = int(data.split("_")[-1])
            session.game.num_impostors = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_2_rd_"):
            val = int(data.split("_")[-1])
            session.game.rounds_per_player = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_1_wc_"):
            val = int(data.split("_")[-1])
            session.game.word_count = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_1_rd_"):
            val_str = data.split("_")[-1]
            if val_str == "endless":
                session.game.endless = True
            else:
                session.game.total_rounds = int(val_str)
                session.game.endless = False
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_1_rt_"):
            val = int(data.split("_")[-1])
            session.game.reveal_time = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_4_md_"):
            mode = data.split("_")[-1]
            session.game.mode = mode
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_4_tm_"):
            val = int(data.split("_")[-1])
            session.game.time_limit = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_4_ct_"):
            cat_map = {"0": "All", "1": "Ethiopian", "2": "Tech", "3": "Cars"}
            val = data.split("_")[-1]
            session.game.category_filter = cat_map.get(val, "All")
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_5_ct_"):
            # Map for GuessMoji categories
            cat_map = {"0": "Movies 🎬", "1": "Idioms & Dictionary 📖", "2": "Songs 🎵", "3": "Places 🌍"}
            val = data.split("_")[-1]
            theme_name = cat_map.get(val)
            if theme_name and theme_name in session.game.THEMED_PUZZLES:
                session.game.theme_name = theme_name
                session.game.current_theme_puzzles = session.game.THEMED_PUZZLES[theme_name]
                session.game.used_puzzles = [] # Reset used puzzles when theme changes
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_5_rd_"):
            val = int(data.split("_")[-1])
            session.game.total_rounds = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_26_rd_"):
            val_str = data.split("_")[-1]
            if val_str == "endless":
                session.game.endless = True
            else:
                session.game.total_rounds = int(val_str)
                session.game.endless = False
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_27_rd_"):
            val_str = data.split("_")[-1]
            if val_str == "endless":
                session.game.endless = True
            else:
                session.game.total_rounds = int(val_str)
                session.game.endless = False
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_27_tm_"):
            val = int(data.split("_")[-1])
            session.game.time_limit = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_9_gt_"):
            val = data.split("_")[-1]
            session.game.game_type = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
        elif data.startswith("opt_9_tm_"):
            val = int(data.split("_")[-1])
            session.game.time_limit = val
            await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
            await query.answer()
            
    elif data.startswith("game_options_"):
        session.is_configuring = True
        await query.edit_message_reply_markup(reply_markup=get_options_markup(session))
        await query.answer()
        
    elif data.startswith("game_pick_"):
        game_code = data.replace("game_pick_", "")
        
        # Load persistent seen images
        used_images = None
        if game_code == "20":
            used_images = settings_manager.get_setting(chat_id, "seen_addis", [])
        elif game_code == "22":
            used_images = settings_manager.get_setting(chat_id, "seen_soccer_players", [])
        elif game_code == "23":
            used_images = settings_manager.get_setting(chat_id, "seen_movie_scenes", [])
        elif game_code == "27":
            used_images = settings_manager.get_setting(chat_id, "seen_riddles", [])
            
        if session.set_game_code(game_code, used_images=used_images):
            # Define game names and min players
            game_info = {
                "1": ("Word Unscramble", "2"),
                "2": ("Story Builder", "2"),
                "3": ("Guess the Imposter", "3"),
                "4": ("Guess the Logo", "2"),
                "5": ("GuessMoji", "2"),
                "6": ("Guess the Movie", "2"),
                "7": ("Guess the Flag", "2"),
                "9": ("General Knowledge", "2"),
                "10": ("Guess the Character", "2"),
                "11": ("Word Connect", "2"),
                "12": ("What You Meme", "2"),
                "13": ("Taylor Swift Or Shakespeare", "2"),
                "15": ("20 Questions", "2"),
                "16": ("Guess the Song", "2"),
                "17": ("Crazy 8", "2"),
                "18": ("Guess the Book", "2"),
                "19": ("Guess the Marvel Character", "2"),
                "20": ("Guess Addis", "2"),
                "21": ("Hear Me Out", "2"),
                "22": ("Name the Player", "2"),
                "23": ("Movie Scene", "2"),
                "26": ("Song From Lyrics", "2"),
                "27": ("Riddles", "2"),
            }
            
            game_name, min_players = game_info.get(game_code, ("General Knowledge", "2"))
            
            # Answer query before deleting message
            await query.answer(f"Selected: {game_name}")
            
            # Delete selection message
            await query.message.delete()
            
            # Send selection confirmation
            cover_filename = GAME_COVERS.get(game_code)
            instructions = get_game_instructions(game_code)
            instructions_text = f"{instructions}\n\n" if instructions else ""
            caption = (
                f"🕹<b>{game_name} game selected</b>\n\n"
                f"{instructions_text}"
                f"The game will start in 🕔40 seconds\n\n"
                f"Send /join to participate"
            )
            
            reply_markup = get_options_markup(session)
            
            msg = None
            if cover_filename:
                cover_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "_game-covers", cover_filename)
                if os.path.exists(cover_path):
                    with open(cover_path, 'rb') as f:
                        msg = await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=caption,
                            reply_markup=reply_markup,
                            parse_mode="HTML"
                        )
                else:
                    msg = await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=reply_markup, parse_mode="HTML")
            else:
                msg = await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=reply_markup, parse_mode="HTML")
            
            if msg:
                session = game_manager.get_game(chat_id)
                if session:
                    session.joining_message_id = msg.message_id
            
            # Start timer
            track_game_task(chat_id, asyncio.create_task(start_game_after_delay(chat_id, context, 40)))
        else:
            await query.answer("<b>Invalid game selected</b>", show_alert=True)


def get_game_instructions(game_code: str) -> str:
    """Get the How to Play instructions for a game."""
    instructions = {
        "1": "<blockquote expandable><b>How to Play:</b>\n‣ Rearrange the letters to form a correct word.\n‣ Type your answer and send it in the chat.\n‣ Each correct answer earns you points.\n‣ The player with the highest score wins.</blockquote>",
        "25": "<blockquote expandable><b>How to Play:</b>\n‣ Everyone gets a celebrity assigned to them.\n‣ On your turn, others see who you are, but you don't.\n‣ Ask questions in the group to guess your celebrity.\n‣ Once you know, type the name in the chat.\n‣ Shortest time to guess wins!</blockquote>",
        "26": "<blockquote expandable><b>How to Play:</b>\n‣ Guess the Song Title and Artist from the provided lyrics.\n‣ Use the '▶Next Line' button to reveal more lines if needed.\n‣ Each correct part (Title or Artist) earns you 2 points.\n‣ Guess both to complete the round!</blockquote>",
        "27": "<blockquote expandable><b>How to Play:</b>\n‣ Read the riddle and type your answer in the chat.\n‣ Each correct answer earns you 1 point.\n‣ First to answer gets the point!\n‣ Player with the most points wins!</blockquote>"
    }
    return instructions.get(game_code, "")


def get_options_markup(session) -> Optional[InlineKeyboardMarkup]:
    """Get the inline keyboard for game options."""
    if not session or session.game_code not in ["1", "2", "3", "4", "5", "9", "26", "27"]:
        return None
        
    if not getattr(session, 'is_configuring', False):
        return InlineKeyboardMarkup([[InlineKeyboardButton("⚙", callback_data=f"game_options_{session.game_code}")]])
        
    game = session.game
    if session.game_code == "1":
        wc = getattr(game, 'word_count', 5)
        tr = getattr(game, 'total_rounds', 10)
        endless = getattr(game, 'endless', False)
        
        rt = getattr(game, 'reveal_time', 30)
        
        keyboard = [
            [InlineKeyboardButton("Word Count:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("4", callback_data="opt_1_wc_4", api_kwargs={"style": "success"} if wc==4 else {}),
                InlineKeyboardButton("5", callback_data="opt_1_wc_5", api_kwargs={"style": "success"} if wc==5 else {}),
                InlineKeyboardButton("6", callback_data="opt_1_wc_6", api_kwargs={"style": "success"} if wc==6 else {})
            ],
            [InlineKeyboardButton("Rounds:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("10", callback_data="opt_1_rd_10", api_kwargs={"style": "success"} if tr==10 and not endless else {}),
                InlineKeyboardButton("15", callback_data="opt_1_rd_15", api_kwargs={"style": "success"} if tr==15 and not endless else {}),
                InlineKeyboardButton("20", callback_data="opt_1_rd_20", api_kwargs={"style": "success"} if tr==20 and not endless else {})
            ],
            [
                InlineKeyboardButton("25", callback_data="opt_1_rd_25", api_kwargs={"style": "success"} if tr==25 and not endless else {}),
                InlineKeyboardButton("Endless", callback_data="opt_1_rd_endless", api_kwargs={"style": "success"} if endless else {})
            ]
        ]
        
        if not endless:
            keyboard.append([InlineKeyboardButton("Reveal Time:", callback_data="ignore_opt")])
            keyboard.append([
                InlineKeyboardButton("15s", callback_data="opt_1_rt_15", api_kwargs={"style": "success"} if rt==15 else {}),
                InlineKeyboardButton("30s", callback_data="opt_1_rt_30", api_kwargs={"style": "success"} if rt==30 else {}),
                InlineKeyboardButton("45s", callback_data="opt_1_rt_45", api_kwargs={"style": "success"} if rt==45 else {})
            ])
            
        keyboard.append([InlineKeyboardButton("⬅", callback_data="opt_done_1", api_kwargs={"style": "primary"})])
        return InlineKeyboardMarkup(keyboard)
        
    elif session.game_code == "2":
        rd = getattr(game, 'rounds_per_player', 2)
        keyboard = [
            [InlineKeyboardButton("Turns per player:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("1", callback_data="opt_2_rd_1", api_kwargs={"style": "success"} if rd==1 else {}),
                InlineKeyboardButton("2", callback_data="opt_2_rd_2", api_kwargs={"style": "success"} if rd==2 else {}),
                InlineKeyboardButton("3", callback_data="opt_2_rd_3", api_kwargs={"style": "success"} if rd==3 else {}),
                InlineKeyboardButton("5", callback_data="opt_2_rd_5", api_kwargs={"style": "success"} if rd==5 else {})
            ],
            [InlineKeyboardButton("⬅", callback_data="opt_done_2", api_kwargs={"style": "primary"})]
        ]
        return InlineKeyboardMarkup(keyboard)
        
    elif session.game_code == "3":
        ni = getattr(game, 'num_impostors', 1)
        keyboard = [
            [InlineKeyboardButton("Number of Impostors:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("1", callback_data="opt_3_ni_1", api_kwargs={"style": "success"} if ni==1 else {}),
                InlineKeyboardButton("2", callback_data="opt_3_ni_2", api_kwargs={"style": "success"} if ni==2 else {}),
                InlineKeyboardButton("3", callback_data="opt_3_ni_3", api_kwargs={"style": "success"} if ni==3 else {})
            ],
            [InlineKeyboardButton("⬅", callback_data="opt_done_3", api_kwargs={"style": "primary"})]
        ]
        return InlineKeyboardMarkup(keyboard)

    elif session.game_code == "4":
        mode = getattr(game, 'mode', 'first')
        tm = getattr(game, 'time_limit', 60)
        ct = getattr(game, 'category_filter', 'All')
        
        cat_val = "0"
        if ct == "Ethiopian": cat_val = "1"
        elif ct == "Tech": cat_val = "2"
        elif ct == "Cars": cat_val = "3"
        
        keyboard = [
            [InlineKeyboardButton("Play Mode:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("First to Answer", callback_data="opt_4_md_first", api_kwargs={"style": "success"} if mode=="first" else {}),
                InlineKeyboardButton("By Turn", callback_data="opt_4_md_turn", api_kwargs={"style": "success"} if mode=="turn" else {})
            ],
            [InlineKeyboardButton("Time Limit:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("30s", callback_data="opt_4_tm_30", api_kwargs={"style": "success"} if tm==30 else {}),
                InlineKeyboardButton("45s", callback_data="opt_4_tm_45", api_kwargs={"style": "success"} if tm==45 else {}),
                InlineKeyboardButton("60s", callback_data="opt_4_tm_60", api_kwargs={"style": "success"} if tm==60 else {})
            ],
            [InlineKeyboardButton("Category:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("All", callback_data="opt_4_ct_0", api_kwargs={"style": "success"} if cat_val=="0" else {}),
                InlineKeyboardButton("Ethiopian", callback_data="opt_4_ct_1", api_kwargs={"style": "success"} if cat_val=="1" else {})
            ],
            [
                InlineKeyboardButton("Tech", callback_data="opt_4_ct_2", api_kwargs={"style": "success"} if cat_val=="2" else {}),
                InlineKeyboardButton("Cars", callback_data="opt_4_ct_3", api_kwargs={"style": "success"} if cat_val=="3" else {})
            ],
            [InlineKeyboardButton("⬅", callback_data="opt_done_4", api_kwargs={"style": "primary"})]
        ]
        return InlineKeyboardMarkup(keyboard)


    elif session.game_code == "5":
        current_theme = getattr(session.game, 'theme_name', "Movies 🎬")
        tr = getattr(session.game, 'total_rounds', 20)
        
        keyboard = [
            [InlineKeyboardButton("Category:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("Movies", callback_data="opt_5_ct_0", api_kwargs={"style": "success"} if current_theme=="Movies 🎬" else {}),
                InlineKeyboardButton("Idioms", callback_data="opt_5_ct_1", api_kwargs={"style": "success"} if current_theme=="Idioms & Dictionary 📖" else {})
            ],
            [
                InlineKeyboardButton("Songs", callback_data="opt_5_ct_2", api_kwargs={"style": "success"} if current_theme=="Songs 🎵" else {}),
                InlineKeyboardButton("Places", callback_data="opt_5_ct_3", api_kwargs={"style": "success"} if current_theme=="Places 🌍" else {})
            ],
            [InlineKeyboardButton("Rounds:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("10", callback_data="opt_5_rd_10", api_kwargs={"style": "success"} if tr==10 else {}),
                InlineKeyboardButton("15", callback_data="opt_5_rd_15", api_kwargs={"style": "success"} if tr==15 else {}),
                InlineKeyboardButton("20", callback_data="opt_5_rd_20", api_kwargs={"style": "success"} if tr==20 else {}),
                InlineKeyboardButton("25", callback_data="opt_5_rd_25", api_kwargs={"style": "success"} if tr==25 else {})
            ],
            [InlineKeyboardButton("⬅", callback_data="opt_done_5", api_kwargs={"style": "primary"})]
        ]
        return InlineKeyboardMarkup(keyboard)

    elif session.game_code == "26":
        tr = getattr(game, 'total_rounds', 10)
        endless = getattr(game, 'endless', False)

        keyboard = [
            [InlineKeyboardButton("Rounds:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("10", callback_data="opt_26_rd_10", api_kwargs={"style": "success"} if tr==10 and not endless else {}),
                InlineKeyboardButton("20", callback_data="opt_26_rd_20", api_kwargs={"style": "success"} if tr==20 and not endless else {}),
                InlineKeyboardButton("Endless", callback_data="opt_26_rd_endless", api_kwargs={"style": "success"} if endless else {})
            ],
            [InlineKeyboardButton("⬅", callback_data="opt_done_26", api_kwargs={"style": "primary"})]
        ]
        return InlineKeyboardMarkup(keyboard)

    elif session.game_code == "27":
        tr = getattr(game, 'total_rounds', 10)
        endless = getattr(game, 'endless', False)
        tm = getattr(game, 'time_limit', 30)

        keyboard = [
            [InlineKeyboardButton("Rounds:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("10", callback_data="opt_27_rd_10", api_kwargs={"style": "success"} if tr==10 and not endless else {}),
                InlineKeyboardButton("Endless", callback_data="opt_27_rd_endless", api_kwargs={"style": "success"} if endless else {})
            ]
        ]

        if not endless:
            keyboard.append([InlineKeyboardButton("Time:", callback_data="ignore_opt")])
            keyboard.append([
                InlineKeyboardButton("30s", callback_data="opt_27_tm_30", api_kwargs={"style": "success"} if tm==30 else {}),
                InlineKeyboardButton("45s", callback_data="opt_27_tm_45", api_kwargs={"style": "success"} if tm==45 else {}),
                InlineKeyboardButton("60s", callback_data="opt_27_tm_60", api_kwargs={"style": "success"} if tm==60 else {})
            ])

        keyboard.append([InlineKeyboardButton("⬅", callback_data="opt_done_27", api_kwargs={"style": "primary"})])
        return InlineKeyboardMarkup(keyboard)

    elif session.game_code == "9":
        gt = getattr(game, 'game_type', 'Answer')
        tm = getattr(game, 'time_limit', 30)

        keyboard = [
            [InlineKeyboardButton("Game Type:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("Answer", callback_data="opt_9_gt_Answer", api_kwargs={"style": "success"} if gt=="Answer" else {}),
                InlineKeyboardButton("MCQ", callback_data="opt_9_gt_MCQ", api_kwargs={"style": "success"} if gt=="MCQ" else {})
            ],
            [InlineKeyboardButton("Time Limit:", callback_data="ignore_opt")],
            [
                InlineKeyboardButton("25s", callback_data="opt_9_tm_25", api_kwargs={"style": "success"} if tm==25 else {}),
                InlineKeyboardButton("30s", callback_data="opt_9_tm_30", api_kwargs={"style": "success"} if tm==30 else {}),
                InlineKeyboardButton("45s", callback_data="opt_9_tm_45", api_kwargs={"style": "success"} if tm==45 else {})
            ],
            [InlineKeyboardButton("⬅", callback_data="opt_done_9", api_kwargs={"style": "primary"})]
        ]
        return InlineKeyboardMarkup(keyboard)


async def start_game_after_delay(chat_id: int, context: ContextTypes.DEFAULT_TYPE, delay: int) -> None:
    """Wait for the specified delay, then start the game if enough players joined.
    
    Args:
        chat_id: Telegram chat ID
        context: Callback context
        delay: Initial delay in seconds before starting the game
    """
    session = game_manager.get_game(chat_id)
    if not session:
        return

    # Set initial deadline
    session.joining_deadline = datetime.now() + timedelta(seconds=delay)
    
    # Loop until deadline is reached
    last_reported_seconds = delay
    while datetime.now() < session.joining_deadline:
        # Check if game was cancelled or state changed
        if session.state != GameState.JOINING:
            return
            
        remaining = int((session.joining_deadline - datetime.now()).total_seconds())
        
        # Update countdown at specific intervals to avoid hitting Telegram's 429 rate limit
        should_update = False
        if remaining < last_reported_seconds:
            if remaining in (30, 20, 10, 5, 3, 1):
                should_update = True
        if should_update and remaining >= 0:
            last_reported_seconds = remaining
            game_name, _ = GAMES_METADATA.get(session.game_code, ("Game", "2"))
            instructions = get_game_instructions(session.game_code)
            instructions_text = f"{instructions}\n\n" if instructions else ""
            new_caption = (
                f"🕹<b>{game_name} game selected</b>\n\n"
                f"{instructions_text}"
                f"The game will start in 🕔{remaining} seconds\n\n"
                f"Send /join to participate"
            )
            
            if session.joining_message_id:
                try:
                    cover_filename = GAME_COVERS.get(session.game_code)
                    reply_markup = get_options_markup(session)
                    if cover_filename:
                        await context.bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=session.joining_message_id,
                            caption=new_caption,
                            reply_markup=reply_markup,
                            parse_mode="HTML"
                        )
                    else:
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=session.joining_message_id,
                            text=new_caption,
                            reply_markup=reply_markup,
                            parse_mode="HTML"
                        )
                except Exception:
                    pass
        
        # Wait a bit before checking again
        await asyncio.sleep(0.5)
    
    # Double check state after loop
    if session.state != GameState.JOINING:
        return
    
    # Check if enough players joined
    min_players = 1 if session.game_code in ["9", "27"] else 2
    if session.get_player_count() < min_players:
        await context.bot.send_message(
            chat_id=chat_id,
            text="<i>Not enough players joined.</i> <b>❌ Game cancelled.</b>\n"
                 "Use /start to try again.",
            parse_mode="HTML"
        )
        game_manager.remove_game(chat_id)
        return
    
    # Start the game
    if session.start_game():
        mode_text = ""
        if session.game_code == "1":
            endless = getattr(session.game, 'endless', False)
            rounds = "Endless" if endless else getattr(session.game, 'total_rounds', 10)
            wc = getattr(session.game, 'word_count', 5)
            mode_text = (
                f"\n\n<blockquote><b>Mode:</b>\n"
                f"Rounds: {rounds}\n"
                f"Letter: {wc} Letter</blockquote>"
            )
        elif session.game_code == "2":
            if not hasattr(session, 'story_start_text'):
                session.story_start_text = session.game.start_game()
            
            rd = getattr(session.game, 'rounds_per_player', 2)
            turn_order_names = [session.game.player_names.get(pid, "Unknown") for pid in session.game.players]
            turn_order_str = " ➔ ".join(turn_order_names)
            
            mode_text = (
                f"\n\n<blockquote><b>Mode:</b>\n"
                f"Turns per player: {rd}\n\n"
                f"<b>Turn Order:</b>\n"
                f"{turn_order_str}</blockquote>"
            )
        elif session.game_code == "3":
            ni = getattr(session.game, 'num_impostors', 1)
            # In guess the imposter, actual impostors might be capped
            actual_ni = len(getattr(session.game, 'imposter_ids', []))
            if not actual_ni:  # in case start_game hasn't run yet? Actually start_game runs right after, let's just show requested or calculate it.
                actual_ni = ni
            
            mode_text = (
                f"\n\n<blockquote><b>Mode:</b>\n"
                f"Impostor(s): {ni}</blockquote>"
            )
        elif session.game_code == "5":
            theme = getattr(session.game, 'theme_name', "Movies 🎬")
            rounds = getattr(session.game, 'total_rounds', 20)
            mode_text = (
                f"\n\n<blockquote><b>Mode:</b>\n"
                f"Rounds: {rounds}\n"
                f"Category: {theme}</blockquote>"
            )
        elif session.game_code == "26":
            endless = getattr(session.game, 'endless', False)
            rounds = "Endless" if endless else getattr(session.game, 'total_rounds', 10)
            mode_text = (
                f"\n\n<blockquote><b>Mode:</b>\n"
                f"Rounds: {rounds}</blockquote>"
            )
        elif session.game_code == "27":
            endless = getattr(session.game, 'endless', False)
            rounds = "Endless" if endless else getattr(session.game, 'total_rounds', 10)
            mode_text = (
                f"\n\n<blockquote><b>Mode:</b>\n"
                f"Rounds: {rounds}</blockquote>"
            )

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎮 <b>Game starting...</b>\n\n"
                 f"Players: 👥{session.get_player_count()}"
                 f"{mode_text}",
            parse_mode="HTML"
        )
        
        # Handle different game types
        if session.game_code == "1":
            # Word Unscramble
            await start_round(chat_id, context)
        elif session.game_code == "2":
            # Story Builder
            start_story_game(chat_id, context, session)
        elif session.game_code == "3":
            # Guess the Imposter
            await start_imposter_game(chat_id, context, session)
        elif session.game_code == "4":
            # Guess the Logo
            await start_logo_game(chat_id, context, session)
        elif session.game_code == "5":
            # GuessMoji
            await start_guessmoji_round(chat_id, context)
        elif session.game_code == "6":
            # Guess the Movie
            await start_movie_game(chat_id, context, session)
        elif session.game_code == "7":
            # Guess the Flag
            await start_flag_game(chat_id, context, session)
        elif session.game_code == "9":
            # General Knowledge
            await start_general_knowledge_game(chat_id, context, session)
        elif session.game_code == "10":
            # Guess the Character
            await start_character_game(chat_id, context, session)
        elif session.game_code == "11":
            # Word Connect
            await start_word_connect_game(chat_id, context, session)
        elif session.game_code == "12":
            # What You Meme
            await start_wdym_game(chat_id, context, session)
        elif session.game_code == "13":
            # Taylor Swift Or Shakespeare
            await start_ts_game(chat_id, context, session)
        elif session.game_code == "15":
            # 20 Questions
            await start_20q_game(chat_id, context, session)
        elif session.game_code == "16":
            # Guess the Song
            await start_song_game(chat_id, context, session)
        elif session.game_code == "17":
            # Crazy 8
            await start_crazy8_game(chat_id, context, session)
        elif session.game_code == "18":
            # Guess the Book
            await start_book_game(chat_id, context, session)
        elif session.game_code == "19":
            # Guess the Marvel Character
            await start_marvel_game(chat_id, context, session)
        elif session.game_code == "20":
            # Guess Addis
            await start_guess_addis_game(chat_id, context, session)
        elif session.game_code == "21":
            # Hear Me Out
            await start_hear_me_out_game(chat_id, context, session)
        elif session.game_code == "22":
            # Name the Player
            await start_name_the_player_game(chat_id, context, session)
        elif session.game_code == "23":
            # Movie Scene
            await start_movie_scene_game(chat_id, context, session)
        elif session.game_code == "25":
            # Who Am I
            await start_who_am_i_game(chat_id, context, session)
        elif session.game_code == "26":
            # Song From Lyrics
            await start_sfl_game(chat_id, context, session)
        elif session.game_code == "27":
            # Riddles
            await start_riddle_round(chat_id, context)




async def start_who_am_i_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Who Am I game."""
    if not session.game.start_game():
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Could not start game. Not enough players or characters.",
            parse_mode="HTML"
        )
        session.end_game()
        game_manager.remove_game(chat_id)
        return
    
    turn_names = [session.game.players.get(pid, "Unknown") for pid in session.game.turn_order]
    order_str = " ➔ ".join(turn_names)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🎭 <b>Who Am I?</b>\n\nTurn order: {order_str}\n\nLet's begin!",
        parse_mode="HTML"
    )
    await play_who_am_i_turn(chat_id, context, session)

async def play_who_am_i_turn(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    current_id = session.game.get_current_player_id()
    if not current_id:
        board = session.game.get_leaderboard()
        board_text = "\n".join([f"{i+1}. {session.game.players.get(pid, 'Unknown')} - {t:.1f}s" for i, (pid, t) in enumerate(board)])
        if not board_text:
            board_text = "No one guessed correctly!"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🏁 <b>Game Over!</b>\n\nLeaderboard:\n{board_text}",
            parse_mode="HTML"
        )
        session.end_game()
        game_manager.remove_game(chat_id)
        return
        
    current_name = session.game.get_current_player_name()
    
    max_swaps = 5
    for _ in range(max_swaps):
        assigned_char = session.game.get_character_for_player(current_id)
        
        # 1. Pre-check if file is readable
        try:
            with open(assigned_char['image'], 'rb') as f:
                pass
        except Exception as e:
            logger.error(f"Image file unreadable ({assigned_char['name']}): {e}")
            if session.game.swap_character(current_id): continue
            else: break
            
        file_error_occurred = False
        
        # 2. Send character to everyone else
        for pid in session.game.turn_order:
            if pid != current_id:
                try:
                    with open(assigned_char['image'], 'rb') as f:
                        await context.bot.send_photo(
                            chat_id=pid,
                            photo=f,
                            caption=f"🤫 It's {current_name}'s turn. They are:\n\n<b>{assigned_char['name']}</b>",
                            parse_mode="HTML"
                        )
                except Exception as e:
                    err_msg = str(e)
                    logger.error(f"Failed to DM {pid}: {err_msg}")
                    # If Telegram fails to process the image, it's a file issue
                    if "Image_process_failed" in err_msg:
                        file_error_occurred = True
                        break 
        
        if file_error_occurred:
            logger.warning(f"Telegram failed to process image for {assigned_char['name']}. Swapping...")
            if session.game.swap_character(current_id):
                continue
            else:
                break
        else:
            # If we reached here, either it sent or it failed for reasons unrelated to the image itself
            break
                
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<b>👉 {current_name}'s turn</b>\n\n"
             f"<blockquote>Ask questions in this group to figure out who you are. Then guess the name!</blockquote>",
        parse_mode="HTML"
    )
    
    session.game.start_turn()


async def start_hear_me_out_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Hear Me Out game."""
    start_text = session.game.start_game()
    current_player_id = session.game.get_current_player_id()
    current_player_name = session.game.get_current_player_name()
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🎂 <b>{start_text}</b>\n\n"
             f"👉 It's <a href=\"tg://user?id={current_player_id}\">{current_player_name}</a>'s turn! Send a picture.",
        parse_mode="HTML"
    )

def start_story_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the story builder game."""
    start_text = getattr(session, 'story_start_text', None)
    if not start_text:
        start_text = session.game.start_game()
    current_player_id = session.game.get_current_player_id()
    current_player_name = session.game.get_current_player_name()
    
    # Send starting prompt and tag first player
    asyncio.create_task(context.bot.send_message(
        chat_id=chat_id,
        text=f"📖\n\n"
             f"{start_text}\n\n"
             f"<blockquote>It's <a href=\"tg://user?id={current_player_id}\">{current_player_name}</a>'s turn to continue the story</blockquote>",
        parse_mode="HTML"
    ))






async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all text messages - route based on game state."""
    chat = update.effective_chat
    message = update.message
    user = update.effective_user
    
    if not message or chat.type == ChatType.PRIVATE or not message.text:
        return
    
    session = game_manager.get_game(chat.id)
    
    # If no session exists, ignore
    if not session:
        return
    
    # If waiting for game code, process numbers (Conditional based on menu_style)
    if session.state == GameState.WAITING_FOR_GAME_CODE:
        menu_style = settings_manager.get_setting(chat.id, "menu_style", "inline")
        if menu_style != "list":
            return # Emojis/Inline mode ignores text codes
            
        # Check if it's the initiator
        if session.initiator_id and user.id != session.initiator_id:
            return 
            
        game_code = message.text.strip()
        if game_code in GAMES_METADATA:
            game_name, min_players = GAMES_METADATA[game_code]
            if session.set_game_code(game_code):
                cover_filename = GAME_COVERS.get(game_code)
                caption = (
                    f"🕹<b>{game_name} game selected</b>\n\n"
                    f"The game will start in 🕔40 seconds\n\n"
                    f"Send /join to participate"
                )
                
                msg = None
                if cover_filename:
                    cover_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "_game-covers", cover_filename)
                    if os.path.exists(cover_path):
                        with open(cover_path, 'rb') as f:
                            msg = await message.reply_photo(
                                photo=f,
                                caption=caption,
                                parse_mode="HTML"
                            )
                    else:
                        msg = await message.reply_text(text=caption, parse_mode="HTML")
                else:
                    msg = await message.reply_text(text=caption, parse_mode="HTML")
                
                if msg:
                    session.joining_message_id = msg.message_id
                track_game_task(chat.id, asyncio.create_task(start_game_after_delay(chat.id, context, 40)))
            else:
                await message.reply_text("❌ Error starting game. Please try again.")
        else:
            # We don't want to reply to every number in the chat, only if it looks like a selection
            # But in WAITING_FOR_GAME_CODE, the initiator might have made a mistake.
            # We'll ignore invalid codes to minimize noise unless it's a clear intention.
            pass
        return

    
    elif session.state == GameState.IN_PROGRESS and session.game:

        # Handle Word Unscramble Game
        if session.game_code == "1":
            # Handle game answers
            logger.info(f"Processing guess from user {user.id} (@{user.username}): '{message.text}'")
            
            if session.game.check_answer(message.text, user.id):
                correct_word = session.game.get_current_word()
                username = user.username or user.first_name or "Player"
                
                # Get current score for this user
                user_score = session.game.scores.get(user.id, 0)
                
                logger.info(f"Correct answer from user {user.id}, score: {user_score}")
                
                # Get display name for the user
                display_name = user.first_name or user.username or "Player"
                
                # React with confetti
                try:
                    await message.set_reaction(reaction=ReactionTypeEmoji(emoji="🎉"))
                except Exception:
                    pass
                
                if getattr(session.game, 'endless', False):
                    wc = getattr(session.game, 'word_count', 5)
                    await message.reply_text(
                        f"<blockquote>+1 Point</blockquote>\n\n"
                        f"The word was: <b>{correct_word.upper()}</b>\n\n"
                        f"use /new{wc} to start a new unscramble",
                        parse_mode="HTML"
                    )
                    # Automatically update the persistent leaderboard in endless mode
                    try:
                        await record_game_scores(session.game.get_scoreboard(), session.game_code, chat.id, context)
                    except Exception as e:
                        logger.error(f"Error recording leaderboard scores in endless mode: {e}")
                else:
                    await message.reply_text(
                        f"<blockquote>+1 Point</blockquote>\n\n"
                        f"The word was: <b>{correct_word.upper()}</b>",
                        parse_mode="HTML"
                    )
                
                # Check if game is over
                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    if not getattr(session.game, 'endless', False):
                        # Start next round automatically if not endless
                        await start_round(chat.id, context)
            else:
                # Wrong answer - ignore to keep chat clean
                logger.info(f"Wrong answer from user {user.id}: '{message.text}'")
        
        # Handle Story Builder Game
        elif session.game_code == "2":
            # Check if it's this user's turn
            if session.game.add_story_segment(message.text, user.id):
                # Valid turn
                full_story = session.game.get_full_story()
                
                if session.game.is_game_over():
                    # Game finished
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"📚\n\n"
                             f"{full_story}\n\n"
                             f"<blockquote>The End! ✍️</blockquote>",
                        parse_mode="HTML"
                    )
                    session.end_game()
                    game_manager.remove_game(chat.id)
                else:
                    # Next turn
                    next_player_id = session.game.get_current_player_id()
                    next_player_name = session.game.get_current_player_name()
                    
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"📝\n\n"
                             f"{full_story}\n\n"
                             f"<blockquote>Next up: <a href=\"tg://user?id={next_player_id}\">{next_player_name}</a></blockquote>",
                        parse_mode="HTML"
                    )
        
        # Handle Guess the Imposter Game
        elif session.game_code == "3":
            # Handle clues (message.text)
            if session.game.is_voting:
                # Voting is happening via buttons/commands
                return
            
            # Allow clues from current player
            if session.game.submit_clue(user.id, message.text):
                 # Clue accepted
                 # Check if we should notify
                 pass
                 
                 # Next player
                 next_id = session.game.get_current_player_id()
                 
                 # If we finished a full round, we loop or wait?
                 # My implementation of GuessTheImposterGame.submit_clue assumes 1 round then `are_clues_finished` is true.
                 # But get_current_player_id returns None if index >= len(turn_order).
                 # I should probably update GuessTheImposterGame to allow infinite rounds (modular arithmetic on index).
                 
                 # Let's fix this momentarily. For now, assume 1 round is enforced by `are_clues_finished`
                 # But the user wants "until someone send a /vote command".
                 # So I should loop the turns.
                 pass

                 if next_id:
                     next_name = session.game.get_current_player_name()
                     clues_str = ""
                     if hasattr(session.game, 'clue_history') and session.game.clue_history:
                         recent_clues = list(reversed(session.game.clue_history))
                         clues_joined = ", ".join(recent_clues)
                         clues_str = f"\n\n<blockquote>Last Clues:\n{clues_joined}</blockquote>"
                         
                     await context.bot.send_message(
                         chat_id=chat.id,
                         text=f"◉ It's <a href=\"tg://user?id={next_id}\">{next_name}</a>'s turn to give a clue!{clues_str}",
                         parse_mode="HTML"
                     )
                     # I need to modify GuessTheImposterGame to support cycling.
                     pass

        # Handle Guess the Logo Game
        elif session.game_code == "4":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                try:
                    await message.set_reaction(reaction=[ReactionTypeEmoji(emoji="🎉")])
                except Exception:
                    pass
                    
                score = session.game.scores.get(user.id, 0)
                answer = session.game.current_answer
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{user.first_name}</a></b>\n\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                # Next round
                await start_logo_round(chat.id, context)

        # Handle GuessMoji Game
        elif session.game_code == "5":
            if session.game.check_answer(message.text, user.id):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                answer = session.game.get_current_answer()
                display_name = user.first_name or user.username or "Player"
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"The answer was: <b>{answer}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    await start_guessmoji_round(chat.id, context)

        # Handle Guess the Movie Game
        elif session.game_code == "6":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{user.first_name}</a></b>\n\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                # Next round
                await start_movie_round(chat.id, context)

        # Handle Guess the Flag Game
        elif session.game_code == "7":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                answer = session.game.get_current_answer()
                display_name = user.first_name or user.username or "Player"
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"The answer was: <b>{answer}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    await start_flag_round(chat.id, context)

        # Handle General Knowledge Game
        elif session.game_code == "9":
            if session.game.game_type == "MCQ":
                return
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                answer = session.game.get_current_answer()
                display_name = user.first_name or user.username or "Player"
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"The answer was: <b>{answer}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    await start_general_knowledge_round(chat.id, context)

        # Handle Guess the Character Game
        elif session.game_code == "10":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                answer = session.game.get_current_answer()
                full_image = session.game.get_full_image()
                display_name = user.first_name or user.username or "Player"
                
                # Send confirmation first
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"The answer was: <b>{answer}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                # Send the full image
                try:
                    with open(full_image, 'rb') as f:
                        await context.bot.send_photo(
                            chat_id=chat.id,
                            photo=f,
                            caption=f"✅ <b>Full Picture: {answer}</b>",
                            parse_mode="HTML"
                        )
                except Exception as e:
                    logger.error(f"Error sending full image: {e}")
            
        # Handle Guess the Book Game
        elif session.game_code == "18":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                answer = session.game.current_answer
                reveal_image = session.game.get_reveal_image()
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{user.first_name}</a></b>\n\n"
                    f"Your score: <b>{score} point(s)</b>",
                    parse_mode="HTML"
                )
                
                # Send the reveal image
                if reveal_image and os.path.exists(reveal_image):
                    try:
                        with open(reveal_image, 'rb') as f:
                            await context.bot.send_photo(
                                chat_id=chat.id,
                                photo=f,
                                caption=f"✅ <b>{answer}</b>",
                                parse_mode="HTML"
                            )
                    except Exception as e:
                        logger.error(f"Error sending book reveal image: {e}")
                
                # Next round
                await start_book_round(chat.id, context)
            
        # Handle Guess the Marvel Character Game
        elif session.game_code == "19":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                answer = session.game.current_answer
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{user.first_name}</a></b>\n\n"
                    f"The character was: <b>{answer}</b>\n"
                    f"Your score: <b>{score} point(s)</b>",
                    parse_mode="HTML"
                )
                
                # Next round
                await start_marvel_round(chat.id, context)

        # Handle Guess Addis Game
        elif session.game_code == "20":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                primary_answer = session.game.resolve_round(correct=True)
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{user.first_name}</a></b>\n\n"
                    f"The place was: <b>{primary_answer}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                # Save progress
                save_addis_progress(chat.id, session)
                
                # Next round
                await start_guess_addis_round(chat.id, context)

        # Handle Name the Player Game
        elif session.game_code == "22":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                primary_answer = session.game.resolve_round(correct=True)
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{user.first_name}</a></b>\n\n"
                    f"The player was: <b>{primary_answer}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                # Save progress
                save_soccer_players_progress(chat.id, session)
                
                # Next round
                await start_name_the_player_round(chat.id, context)

        # Handle Movie Scene Game
        elif session.game_code == "23":
            if session.game.check_answer(user.id, message.text):
                # Correct answer
                score = session.game.scores.get(user.id, 0)
                primary_answer = session.game.resolve_round(correct=True)
                
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{user.first_name}</a></b>\n\n"
                    f"The movie was: <b>{primary_answer}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )
                
                # Save progress
                save_movie_scene_progress(chat.id, session)
                
                # Next round
                await start_movie_scene_round(chat.id, context)

        # Handle Word Connect Game
        elif session.game_code == "11":
            is_correct, feedback = session.game.check_answer(user.id, message.text)
            if is_correct:
                progress = session.game.get_round_progress()
                display_name = user.first_name or user.username or "Player"
                letters = session.game.current_letters
                
                await message.reply_text(
                    f"🎉 <b>{feedback}! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"Letters: <b>{' '.join(letters).upper()}</b>\n\n"
                    f"{progress}",
                    parse_mode="HTML"
                )
                
                # Reset hint timer
                cancel_game_tasks(chat.id)
                track_game_task(chat.id, asyncio.create_task(word_connect_hint_timeout(chat.id, context, session.game.current_round)))
                
                if session.game.is_round_finished():
                    # Cancel hint timer
                    cancel_game_tasks(chat.id)
                    
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text="🎊 <b>Round Completed!</b> 🎊\nAll words found!",
                        parse_mode="HTML"
                    )
                    
                    if session.game.is_game_over():
                        await end_game(chat.id, context, session)
                    else:
                        await start_word_connect_round(chat.id, context)
            elif feedback:
                # feedback contains "Already found!"
                await message.reply_text(f"⚠️ {feedback}", parse_mode="HTML")

        # Handle 20 Questions Game
        elif session.game_code == "15":
            if not session.game.round_in_progress:
                return

            user_id = user.id
            text = message.text.strip()
            
            # Host can only answer text ending in ?
            # Check if it is the host speaking
            if user_id == session.game.host_id:
                pass
            else:
                # Guesser logic
                is_action, result = session.game.check_guess_or_question(user_id, text)
                
                if result == 'QUESTION_COUNTED':
                    # React to the question
                    try:
                        # Try user's preferred 'Alien Monster'
                        await context.bot.set_message_reaction(
                            chat_id=chat.id,
                            message_id=message.message_id,
                            reaction=[ReactionTypeEmoji(emoji="👾")]
                        )
                    except Exception:
                        try:
                            # Fallback to standard 'Thinking Face'
                            await context.bot.set_message_reaction(
                                chat_id=chat.id,
                                message_id=message.message_id,
                                reaction=[ReactionTypeEmoji(emoji="🤔")]
                            )
                        except Exception:
                            pass # Group has restricted reactions

                    remaining = session.game.max_questions - session.game.questions_asked
                    if remaining <= 5:
                         await message.reply_text(f"⚠️ <b>{remaining} questions left!</b>", parse_mode="HTML")

                elif result == 'LIMIT_REACHED':
                    await message.reply_text("🚫 <b>20 Questions Reached!</b> Host wins this round.")
                    session.game.host_wins_round()
                    
                    if session.game.is_game_over():
                        await end_game(chat.id, context, session)
                    else:
                        await start_20q_round(chat.id, context)
                        
                elif result == 'CORRECT':
                    display_name = user.first_name
                    await message.reply_text(
                        f"🎉 <b>Correct! <a href=\"tg://user?id={user_id}\">{display_name}</a> got it!</b>\n"
                        f"The word was: <b>{session.game.current_word}</b>\n\n"
                        f"<i>{display_name} is now the Host!</i>",
                        parse_mode="HTML"
                    )
                    
                    if session.game.is_game_over():
                        await end_game(chat.id, context, session)
                    else:
                        # Winner becomes host
                        await start_20q_round(chat.id, context, forced_host_id=user_id)

        # Handle Guess the Song Game
        elif session.game_code == "16":
            if not session.game.round_in_progress:
                return

            text = message.text.strip()
            display_name = user.first_name or user.username or "Player"

            title_matched = session.game.check_title(user.id, text)
            artist_matched = session.game.check_artist(user.id, text)

            if title_matched:
                score = session.game.scores.get(user.id, 0)
                title = session.game.get_current_title()
                await message.reply_text(
                    f"🎵 <b>Title guessed! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"Song: <b>{title}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )

            if artist_matched:
                score = session.game.scores.get(user.id, 0)
                artist = session.game.get_current_artist()
                await message.reply_text(
                    f"🎤 <b>Artist guessed! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"Artist: <b>{artist}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )

            # Check if round is complete (both guessed)
            if (title_matched or artist_matched) and session.game.is_round_complete():
                session.game.round_in_progress = False
                await send_song_reveal(chat.id, context, session)
                await asyncio.sleep(5)

                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    await start_song_round(chat.id, context)

        # Handle Crazy 8 Game
        elif session.game_code == "17":
            # Detect "Draw a card" message from inline query result
            if message.text.lower() == "draw a card":
                success, msg_text = session.game.draw_card_for_player(user.id)
                if success:
                    # Send the drawn card as a sticker in private
                    drawn_card = session.game.hands[user.id][-1]
                    cache = get_sticker_cache()
                    sticker_id = cache.get(f"{drawn_card.rank}_of_{drawn_card.suit}")
                    
                    try:
                        if sticker_id:
                            await context.bot.send_sticker(chat_id=user.id, sticker=sticker_id)
                    except Exception:
                        # User hasn't started bot in private
                        pass
                        
                    await context.bot.send_message(chat_id=chat.id, text=msg_text, parse_mode="HTML")
                    await send_c8_buttons(chat.id, context, session)
                else:
                    await message.reply_text(f"⚠️ {msg_text}", parse_mode="HTML")
                return

            # Parse play
            success, msg_text, filename = session.game.play_card(user.id, message.text)
            if success:
                # Send the card image if they played one
                if filename:
                    card_path = os.path.join(os.path.dirname(__file__), "assets", "card-deck", filename)
                    try:
                        with open(card_path, 'rb') as f:
                            await context.bot.send_photo(
                                chat_id=chat.id,
                                photo=f,
                                caption=msg_text,
                                parse_mode="HTML"
                            )
                    except Exception as e:
                        logger.error(f"Error sending card image: {e}")
                        await message.reply_text(msg_text, parse_mode="HTML")
                else:
                    await message.reply_text(msg_text, parse_mode="HTML")
                
                # Check win
                if session.game.game_over:
                    # They won
                    await end_game(chat.id, context, session)
                else:
                    # Provide buttons for the next player
                    await send_c8_buttons(chat.id, context, session)
            else:
                # Only reply if it was an active player trying to play an invalid card (otherwise ignore chat)
                if user.id == session.game.current_player_id:
                     # Attempt to parse
                     if session.game.parse_card_from_text(message.text):
                         # If it was a card format but invalid, show error
                         await message.reply_text(f"⚠️ {msg_text}", parse_mode="HTML")

        # Handle Who Am I Game
        elif session.game_code == "25":
            if session.game.check_guess(user.id, message.text):
                time_taken = session.game.completion_times[user.id]
                assigned_char = session.game.get_character_for_player(user.id)
                await message.reply_text(
                    f"🎉 Correct! You are <b>{assigned_char['name']}</b>!\n"
                    f"Time: {time_taken:.1f} seconds.",
                    parse_mode="HTML"
                )
                session.game.next_turn()
                await play_who_am_i_turn(chat.id, context, session)

        # Handle Riddles Game
        elif session.game_code == "27":
            if not session.game.round_in_progress:
                return

            user_id = user.id
            text = message.text.strip()
            display_name = user.first_name or user.username or "Player"

            if session.game.check_answer(text, user_id):
                session.game.round_in_progress = False
                try:
                    await message.set_reaction(reaction=ReactionTypeEmoji(emoji="🎉"))
                except Exception:
                    pass

                score = session.game.scores.get(user_id, 0)
                await message.reply_text(
                    f"🎉 <b>Correct! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"Answer: <b>{session.game.get_current_answer()}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )

                save_riddles_progress(chat.id, session)
                await asyncio.sleep(3)

                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    if not session.game.endless:
                        await start_riddle_round(chat.id, context)
                    else:
                        await message.reply_text("use /newriddle to start the next riddle!")

        # Handle Song From Lyrics Game
        elif session.game_code == "26":
            if not session.game.round_in_progress:
                return

            text = message.text.strip()
            display_name = user.first_name or user.username or "Player"

            title_matched = session.game.check_title(user.id, text)
            artist_matched = session.game.check_artist(user.id, text)

            if title_matched:
                try:
                    await message.set_reaction(reaction=ReactionTypeEmoji(emoji="🎉"))
                except Exception:
                    pass
                
                score = session.game.scores.get(user.id, 0)
                title = session.game.get_current_title()
                await message.reply_text(
                    f"🎵 <b>Title guessed! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"Song: <b>{title}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )

            if artist_matched:
                try:
                    await message.set_reaction(reaction=ReactionTypeEmoji(emoji="💯"))
                except Exception:
                    pass
                
                score = session.game.scores.get(user.id, 0)
                artist = session.game.get_current_artist()
                await message.reply_text(
                    f"🎤 <b>Artist guessed! <a href=\"tg://user?id={user.id}\">{display_name}</a></b>\n\n"
                    f"Artist: <b>{artist}</b>\n"
                    f"Your score: <b>{score}</b> point(s)",
                    parse_mode="HTML"
                )

            # Check if round is complete (both guessed)
            if (title_matched or artist_matched) and session.game.is_round_complete():
                session.game.round_in_progress = False
                await send_sfl_reveal(chat.id, context, session)
                await asyncio.sleep(5)

                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    if not session.game.endless:
                        await start_sfl_round(chat.id, context)
                    else:
                        await message.reply_text("use /newsong to start the next round!")

async def reveal_word_after_delay(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int, delay: int) -> None:
    """Reveal the word if no one answers within the delay."""
    await asyncio.sleep(delay)
    session = game_manager.get_game(chat_id)
    if not session or not session.game or session.game_code != "1" or session.state != GameState.IN_PROGRESS:
        return
        
    if session.game.current_round == round_num:
        correct_word = session.game.get_current_word()
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Time's up!\n\nThe word was: <b>{correct_word.upper()}</b>",
            parse_mode="HTML"
        )
        
        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            await start_round(chat_id, context)


async def start_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of the game."""
    session = game_manager.get_game(chat_id)
    if not session or not session.game:
        return
    
    # Wait a moment before sending the next word
    await asyncio.sleep(2)
    
    scrambled, round_num = session.game.start_new_round()
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<blockquote>Unscramble:</blockquote>\n\n"
             f"<b>{'  '.join(scrambled.upper())}</b>",
        parse_mode="HTML"
    )
    
    # Start the reveal timer if not endless
    if not getattr(session.game, 'endless', False):
        reveal_time = getattr(session.game, 'reveal_time', 30)
        track_game_task(chat_id, asyncio.create_task(reveal_word_after_delay(chat_id, context, round_num, reveal_time)))


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /join command for players to join the game."""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type == ChatType.PRIVATE:
        return
    
    session = game_manager.get_game(chat.id)
    
    # Give feedback if no game or wrong state
    if not session:
        await update.message.reply_text(
            "⚠️ No game in progress. Use /start to begin a new game!",
            parse_mode="HTML"
        )
        return
    
    if session.state != GameState.JOINING:
        if session.state == GameState.WAITING_FOR_GAME_CODE:
            await update.message.reply_text(
                "⚠️ Please select a game code first!",
                parse_mode="HTML"
            )
        elif session.state == GameState.IN_PROGRESS:
            await update.message.reply_text(random.choice(QUIRKY_RESPONSES))
        return
    
    # Add player
    if session.game_code in ["3", "25"]:
        try:
            # Check if we can send message to user
            await context.bot.send_chat_action(chat_id=user.id, action="typing")
        except Exception:
            # Can't send message to user
            bot_username = context.bot.username
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Start Private Chat", url=f"https://t.me/{bot_username}?start=join")]
            ])
            await update.message.reply_text(
                f"⚠️ <a href=\"tg://user?id={user.id}\">{user.first_name}</a>, you need to start a private chat with me first!",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            return

    # Calculate display name (prefer first name)
    display_name = user.first_name or user.username or "Player"

    if session.add_player(user.id, display_name):
        await update.message.reply_text(
            f'✅ <a href="tg://user?id={user.id}">{display_name}</a> joined the game! ({session.get_player_count()} players)',
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "⚠️ You're already in the game!",
            parse_mode="HTML"
        )


async def end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """End the game and declare winners."""
    if not session or not session.game:
        return
    
    # Handle Story Builder Game
    if session.game_code == "2":
        full_story = session.game.get_full_story()
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📚 <b>The Final Story</b> 📚\n\n"
                 f"<i>{full_story}</i>\n\n"
                 f"The End! Thanks for writing together! ✍️",
            parse_mode="HTML"
        )
        
        # Clean up
        session.end_game()
        game_manager.remove_game(chat_id)
        return

    # Handle Word Unscramble Game (and others with scores)
    scoreboard = session.game.get_scoreboard()
    
    # Save scores to persistent leaderboard
    try:
        await record_game_scores(scoreboard, session.game_code, chat_id, context)
    except Exception as e:
        logger.error(f"Error recording leaderboard scores: {e}")
    
    # Build scoreboard message
    total_rounds = getattr(session.game, 'total_rounds', 10)
    current_round = getattr(session.game, 'current_round', total_rounds)
    scoreboard_text = f"<b>Round Over. {current_round}/{total_rounds}</b>\n\n"
    
    if scoreboard:
        winner_id, winner_score = scoreboard[0]
        try:
            winner_user = await context.bot.get_chat_member(chat_id, winner_id)
            winner_name = winner_user.user.first_name or winner_user.user.username or "Player"
            winner_mention = f'<a href="tg://user?id={winner_id}">{winner_name}</a>'
        except Exception:
            winner_mention = "Player"
            
        scoreboard_text += f"🎉<b>{winner_mention} Won - {winner_score} points</b>\n\n"
        scoreboard_text += "<blockquote>scoreboard\n"
        
        # List other players starting from 2nd place
        for rank, (user_id, score) in enumerate(scoreboard[1:], 2):
            try:
                user_member = await context.bot.get_chat_member(chat_id, user_id)
                u_name = user_member.user.first_name or user_member.user.username or "Player"
                u_mention = f'<a href="tg://user?id={user_id}">{u_name}</a>'
                scoreboard_text += f"{rank}. {u_mention} - {score} points\n"
            except Exception:
                scoreboard_text += f"{rank}. Player - {score} points\n"
        
        scoreboard_text += "</blockquote>"
    else:
        scoreboard_text += "No one played!\n"

    scoreboard_text += "\n\n<i>use /start for a new game.</i>"
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=scoreboard_text,
        parse_mode="HTML"
    )
    
    # Clean up
    session.end_game()
    cancel_game_tasks(chat_id)
    game_manager.remove_game(chat_id)
    return


async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /leave command for players to leave the game."""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type == ChatType.PRIVATE:
        return
    
    session = game_manager.get_game(chat.id)
    
    if not session:
        await update.message.reply_text(
            "⚠️ No game in progress.",
            parse_mode="HTML"
        )
        return
        
    if session.remove_player(user.id):
        display_name = user.first_name or user.username or "Player"
        await update.message.reply_text(
            f'👋 <a href="tg://user?id={user.id}">{display_name}</a> left the game. ({session.get_player_count()} players remaining)',
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "⚠️ You are not in the game!",
            parse_mode="HTML"
        )


async def quit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /quit command with voting mechanism."""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type == ChatType.PRIVATE:
        return
        
    session = game_manager.get_game(chat.id)
    if not session:
        return

    # Check if a vote is already in progress
    if session.quit_vote_message_id:
        await update.message.reply_text("⚠️ A quit vote is already in progress!")
        return

    # Don't require vote if no one has joined yet or only 1 player
    if len(session.players) <= 1:
        await update.message.reply_text("👋 Game ended.")
        session.end_game()
        game_manager.remove_game(chat.id)
        cancel_game_tasks(chat.id)
        return

    # Only active players can trigger the quit vote
    if user.id not in session.players:
        await update.message.reply_text("❌ Only active players can initiate a quit vote.")
        return

    # Start voting using native Poll
    total_players = len(session.players)
    required_votes = (total_players + 1) // 2

    game_name, _ = GAMES_METADATA.get(session.game_code, ("Game", "2"))

    # Send poll with the new API features
    msg = await context.bot.send_poll(
        chat_id=chat.id,
        question=f"Quit the current {game_name} Game? ({required_votes} votes needed)",
        options=["Quit", "Keep Playing"],
        is_anonymous=False,
        allows_multiple_answers=False,
        open_period=30
    )
    
    session.quit_vote_message_id = msg.message_id
    session.quit_poll_id = msg.poll.id
    session.quit_votes = set()
    
    # Auto-evaluate vote after 30s
    track_game_task(chat.id, asyncio.create_task(quit_vote_timeout(chat.id, context, msg.message_id)))

async def quit_vote_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Clean up and evaluate voting after timeout."""
    await asyncio.sleep(30)
    session = game_manager.get_game(chat_id)
    if not session or session.quit_vote_message_id != message_id:
        return
        
    total_players = len(session.players)
    required_votes = (total_players + 1) // 2
    current_votes = len(session.quit_votes)
    
    if current_votes >= required_votes:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🛑 <b>Game Terminated!</b>\nMajority voted to quit ({current_votes}/{total_players}).",
            parse_mode="HTML"
        )
        session.end_game()
        game_manager.remove_game(chat_id)
        cancel_game_tasks(chat_id)
    else:
        if session.state != GameState.ENDED:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ <b>Vote not sufficient. Game continues.</b>\nAdmins can use /forcequit to quit.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
                
    session.quit_poll_id = None
    session.quit_vote_message_id = None
    session.quit_votes = set()
    
    # If the user who initiated is playing, add their vote automatically if we wanted, but let's let them vote manually.

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle poll answers for quit votes, imposter game votes, and GK MCQ polls."""
    answer = update.poll_answer
    poll_id = answer.poll_id
    user_id = answer.user.id
    
    # Find session
    session = None
    poll_type = None
    for s in list(game_manager.active_games.values()):
        if getattr(s, 'quit_poll_id', None) == poll_id:
            session = s
            poll_type = "quit"
            break
        elif getattr(s, 'imposter_poll_id', None) == poll_id:
            session = s
            poll_type = "imposter"
            break
        elif getattr(s, 'gk_mcq_poll_id', None) == poll_id:
            session = s
            poll_type = "gk_mcq"
            break
            
    if not session:
        return
        
    # Only count votes from active players
    if user_id not in session.players:
        return

    if poll_type == "quit":
        if 0 in answer.option_ids:
            session.quit_votes.add(user_id)
        else:
            session.quit_votes.discard(user_id)
    elif poll_type == "gk_mcq":
        if answer.option_ids:
            session.gk_mcq_answers[user_id] = answer.option_ids[0]
    elif poll_type == "imposter":
        if not session.game.is_voting or session.game.game_over:
            return
            
        if answer.option_ids:
            selected_idx = answer.option_ids[0]
            target_id = getattr(session, 'imposter_poll_mapping', {}).get(selected_idx)
            if target_id:
                session.game.vote(user_id, target_id)
                
                if session.game.is_voting_complete():
                    try:
                        await context.bot.stop_poll(session.chat_id, getattr(session, 'imposter_poll_id'))
                    except Exception:
                        pass
                    asyncio.create_task(resolve_imposter_game(session.chat_id, context, session))
        else:
            if user_id in session.game.votes:
                del session.game.votes[user_id]

async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle poll updates (e.g., when it closes automatically)."""
    # Poll closures are handled reliably by the async quit_vote_timeout task.
    pass


async def forcequit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /forcequit command to instantly quit a game (admins only)."""
    chat = update.effective_chat
    user = update.effective_user
    
    if chat.type == ChatType.PRIVATE:
        return
        
    session = game_manager.get_game(chat.id)
    if not session:
        await update.message.reply_text("⚠️ No active game to quit.")
        return

    try:
        member = await chat.get_member(user.id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await update.message.reply_text("❌ Only group admins can force quit a game.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    await update.message.reply_text("🛑 <b>Game Force Quit!</b>", parse_mode="HTML")
    session.end_game()
    game_manager.remove_game(chat.id)
    cancel_game_tasks(chat.id)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /export command to export the leaderboard_data.json file."""
    if update.effective_user.id != 7388700051:
        return
    
    try:
        with open("leaderboard_data.json", "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename="leaderboard_data.json",
                caption="Here is the leaderboard backup."
            )
    except FileNotFoundError:
        await update.message.reply_text("leaderboard_data.json not found.")
    except Exception as e:
        await update.message.reply_text(f"Error exporting file: {e}")


async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /extend command to extend the joining period."""
    chat = update.effective_chat
    
    if chat.type == ChatType.PRIVATE:
        return
        
    # Check if user is admin
    member = await chat.get_member(update.effective_user.id)
    if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
        await update.message.reply_text(
            "",
            parse_mode="HTML"
        )
        return
        
    session = game_manager.get_game(chat.id)
    
    if not session or session.state != GameState.JOINING:
        await update.message.reply_text("⚠️ This command only works during the joining phase.")
        return
        
    if not session.joining_deadline:
        return
        
    # Extend deadline by 10 seconds
    session.joining_deadline += timedelta(seconds=10)
    
    # Calculate remaining time
    remaining = (session.joining_deadline - datetime.now()).seconds
    
    await update.message.reply_text(
        f"⏳ <b>Time Extended!</b>\n\n"
        f"Added 10 seconds to the joining period.\n"
        f"Game starts in approximately {remaining} seconds.",
        parse_mode="HTML"
    )


async def start_imposter_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Imposter game."""
    secret_word = session.game.start_game()
    imposter_ids = session.game.imposter_ids
    
    # Send roles to players
    failed_users = []
    
    for user_id in session.players:
        if user_id in imposter_ids:
            caption = "<b>🐺 You are the imposter.</b>\n\n" \
                      "Blend in. You don’t know the secret word.\n\n" \
                      "<blockquote>Listen to the clues and try to figure it out or convincingly fake it.</blockquote>"
            
            other_impostors = [session.game.players.get(i_id, "Unknown") for i_id in imposter_ids if i_id != user_id]
            if len(other_impostors) == 1:
                caption += f"\n\nYour fellow impostor is: <b>{other_impostors[0]}</b>"
            elif len(other_impostors) > 1:
                caption += f"\n\nYour fellow impostors are: <b>{' and '.join(other_impostors)}</b>"
                
            try:
                with open("assets/guess-the-impostor/impostor-wolf.png", 'rb') as f:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=f,
                        caption=caption,
                        parse_mode="HTML"
                    )
            except Exception as e:
                logger.error(f"Failed to send DM to {user_id}: {e}")
                failed_users.append(user_id)
        else:
            role_msg = f"<b>The secret word is: {secret_word.upper()}</b>\n\n" \
                       f"<blockquote>Give a clue related to the word without revealing it.\n" \
                       f"Try to identify the imposter who doesn’t know it.</blockquote>"
            
            try:
                await context.bot.send_message(chat_id=user_id, text=role_msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send DM to {user_id}: {e}")
                failed_users.append(user_id)
            
    # Announcement in group
    current_player_id = session.game.get_current_player_id()
    current_player_name = session.game.get_current_player_name()
    
    msg = f"Each player has received their secret word or role via private message.\n\n" \
          f" ❯❯❯❯ <a href=\"tg://user?id={current_player_id}\">{current_player_name}</a> goes first...\n\n" \
          f"<blockquote>Send a single-word or short-phrase clue related to the secret word.</blockquote>\n\n" \
          f"Use /vote when you’re ready to identify the imposter"
          
    if failed_users:
        msg += "\n\n⚠️ <i>Couldn't message some players. Make sure you've started the bot privately!</i>"
        
    # Add deep link button to bot private chat
    bot_username = context.bot.username
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👀 Check Your Role", url=f"https://t.me/{bot_username}")]
    ])
        
    await context.bot.send_message(
        chat_id=chat_id, 
        text=msg, 
        reply_markup=keyboard,
        parse_mode="HTML"
    )


async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /vote command to initiate voting."""
    chat = update.effective_chat
    
    if chat.type == ChatType.PRIVATE:
        return
        
    session = game_manager.get_game(chat.id)
    if not session or session.game_code != "3" or not isinstance(session.game, GuessTheImposterGame):
        await update.message.reply_text("⚠️ This command is only for 'Guess the Imposter' game.")
        return
        
    if session.game.game_over:
        return

    # Trigger voting phase
    if not session.game.is_voting:
        session.game.start_voting()
        
        # Start timer task
        asyncio.create_task(end_voting_after_delay(chat.id, context, 40))
        
    # Check if a poll is already active
    if getattr(session, 'imposter_poll_id', None):
        return

    options = []
    option_mapping = {}
    idx = 0
    for u_id, name in session.game.players.items():
        options.append(name)
        option_mapping[idx] = u_id
        idx += 1
        
    session.imposter_poll_mapping = option_mapping
    
    try:
        poll_message = await context.bot.send_poll(
            chat_id=chat.id,
            question="🗳️ Vote for the Imposter",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=40
        )
        session.imposter_poll_id = poll_message.poll.id
    except Exception as e:
        logger.error(f"Failed to send vote poll: {e}")
        await update.message.reply_text("❌ Failed to start voting poll.")


async def end_voting_after_delay(chat_id: int, context: ContextTypes.DEFAULT_TYPE, delay: int) -> None:
    """End voting phase after delay."""
    await asyncio.sleep(delay)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "3" or not isinstance(session.game, GuessTheImposterGame):
        return
        
    # Check if still voting (game hasn't ended manually)
    if session.game.is_voting and not session.game.game_over:
        await resolve_imposter_game(chat_id, context, session)


async def generate_impostor_win_image(imposter_ids: list, imposter_names: list, context: ContextTypes.DEFAULT_TYPE) -> io.BytesIO:
    num = len(imposter_ids)
    if num == 1:
        template_path = "assets/guess-the-impostor/impostor-empty-frame.png"
        frames = [((401, 190), (598, 386))]
        texts = [((405, 425), (598, 460))]
    elif num == 2:
        template_path = "assets/guess-the-impostor/impostor-2-empty-frame.png"
        frames = [((270, 197), (464, 392)), ((535, 196), (732, 393))]
        texts = [((270, 426), (462, 464)), ((536, 420), (731, 460))]
    else:
        template_path = "assets/guess-the-impostor/impostor-3-empty-frame.png"
        frames = [((135, 190), (332, 388)), ((401, 189), (597, 387)), ((666, 190), (865, 386))]
        texts = [((138, 418), (326, 452)), ((404, 411), (597, 450)), ((674, 415), (864, 451))]
        
    try:
        base_img = Image.open(template_path).convert("RGBA")
    except Exception as e:
        logger.error(f"Error loading imposter template {template_path}: {e}")
        return None
        
    draw = ImageDraw.Draw(base_img)
    try:
        font = ImageFont.truetype("/Library/Fonts/Arial Unicode.ttf", 26)
    except:
        font = ImageFont.load_default()
        
    for i in range(min(num, len(frames))):
        user_id = imposter_ids[i]
        name = imposter_names[i]
        frame = frames[i]
        text_box = texts[i]
        
        # Download profile picture
        try:
            photos = await context.bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count > 0:
                photo_file = await context.bot.get_file(photos.photos[0][-1].file_id)
                photo_bytes = await photo_file.download_as_bytearray()
                pfp = Image.open(io.BytesIO(photo_bytes)).convert("RGBA")
                
                # Resize to fit frame
                w = int(frame[1][0] - frame[0][0])
                h = int(frame[1][1] - frame[0][1])
                pfp = pfp.resize((w, h))
                base_img.paste(pfp, (int(frame[0][0]), int(frame[0][1])), pfp)
        except Exception as e:
            logger.error(f"Error loading pfp for {user_id}: {e}")
            
        # Draw text
        try:
            w_t = text_box[1][0] - text_box[0][0]
            h_t = text_box[1][1] - text_box[0][1]
            
            bbox = draw.textbbox((0, 0), name, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            
            tx = text_box[0][0] + (w_t - tw) / 2
            ty = text_box[0][1] + (h_t - th) / 2
            
            draw.text((tx, ty), name, fill="white", font=font)
        except Exception as e:
            pass
            
    output = io.BytesIO()
    base_img.save(output, format="PNG")
    output.seek(0)
    return output


async def resolve_imposter_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: GameSession) -> None:
    """Resolve the game results."""
    # Process results
    result = session.game.resolve_game()
    
    imposter_ids = result.get("imposter_ids", [])
    imposter_names = result.get("imposter_names", [])
    secret_word = result["secret_word"]
    most_voted_name = result["most_voted_name"]
    most_voted_id = result["most_voted_id"]
    
    # Format names with links
    imposter_links = []
    for i, i_id in enumerate(imposter_ids):
        imposter_links.append(f"<a href=\"tg://user?id={i_id}\">{imposter_names[i]}</a>")
        
    imposter_links_str = " and ".join(imposter_links)
    if not imposter_links_str:
        imposter_links_str = "Unknown"
        
    most_voted_link = f"<a href=\"tg://user?id={most_voted_id}\">{most_voted_name}</a>" if most_voted_id else "Tie"

    if result["imposter_caught"]:
        caption = f"<b>Imposter caught.</b>\n\n" \
                  f"Imposter(s): {imposter_links_str}\n" \
                  f"Most voted: {most_voted_link}\n\n" \
                  f"The secret word was: <b>{secret_word.upper()}</b>"
                  
        try:
            with open("assets/guess-the-impostor/victory.png", 'rb') as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Failed to send victory image: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML"
            )
    else:
        caption = f"🏆 <b>Imposter Wins</b>\n\n" \
                  f"The Imposter(s) were: {imposter_links_str}.\n" \
                  f"You voted out: {most_voted_link}\n\n" \
                  f"The secret word was: <b>{secret_word.upper()}</b>"
                  
        img_io = await generate_impostor_win_image(imposter_ids, imposter_names, context)
        if img_io:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=img_io,
                caption=caption,
                parse_mode="HTML"
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML"
            )
    
    # Clean up
    session.end_game()
    game_manager.remove_game(chat_id)


async def handle_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voting callback queries."""
    query = update.callback_query
    user = query.from_user
    chat = update.effective_chat
    
    try:
        await query.answer()
    except (NetworkError, TimedOut, TelegramError) as e:
        logger.warning(f"Failed to answer callback query: {e}")
    
    session = game_manager.get_game(chat.id)
    if not session or session.game_code != "3" or not isinstance(session.game, GuessTheImposterGame):
        await query.edit_message_text("⚠️ Game not active.")
        return
        
    if not session.game.is_voting:
        await query.edit_message_text("⚠️ Voting is not active currently.")
        return

    target_id = int(query.data.split("_")[1])
    
    # Cast vote
    if session.game.vote(user.id, target_id):
        status = session.game.get_voting_status()
        
        # Update message? Or just send new one?
        # Creating a new message for each vote might be spammy, but editing is cleaner.
        # However, we can't easily edit the original /vote message if multiple exist.
        # But we can edit the message that the inline button is attached to.
        
        await query.edit_message_text(
            f"🗳️ <b>Vote Recorded!</b>\n\n"
            f"{status}\n"
            f"Keep voting!",
            reply_markup=query.message.reply_markup, # Keep buttons
            parse_mode="HTML"
        )
        
        if session.game.is_voting_complete():
            # Process results
            await resolve_imposter_game(chat.id, context, session)
            
            # Remove buttons from the last voting message
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
    else:
        # Vote failed (already voted, etc)
        # We can show alert
        await context.bot.answer_callback_query(query.id, text="You already voted!", show_alert=True)


async def start_logo_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Logo game."""
    await start_logo_round(chat_id, context)


async def start_logo_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess the Logo."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "4":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    # Ensure game is started (for player order init)
    if session.game.current_round == 0:
        session.game.start_game()

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    logo_path, round_num, current_player_id = result
    
    time_limit = getattr(session.game, 'time_limit', 60)
    mode = getattr(session.game, 'mode', 'first')
    
    turn_text = "First to guess gets a point!"
    if mode == "turn" and current_player_id:
        player_name = session.game.players.get(current_player_id, "Player")
        turn_text = f"👉 It's <a href=\"tg://user?id={current_player_id}\">{player_name}</a>'s turn!"

    try:
        import io
        import os
        if logo_path.lower().endswith('.svg'):
            try:
                # Fix for Homebrew cairo on Mac
                if os.path.exists('/opt/homebrew/lib'):
                    if 'DYLD_LIBRARY_PATH' not in os.environ:
                        os.environ['DYLD_LIBRARY_PATH'] = '/opt/homebrew/lib'
                    elif '/opt/homebrew/lib' not in os.environ['DYLD_LIBRARY_PATH']:
                        os.environ['DYLD_LIBRARY_PATH'] += ':/opt/homebrew/lib'

                import cairosvg
                png_data = cairosvg.svg2png(url=logo_path)
                photo_file = io.BytesIO(png_data)
                photo_file.name = "logo.png"
            except Exception as svg_err:
                logger.error(f"SVG conversion failed: {svg_err}")
                # Fallback: send as document
                with open(logo_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption=f"<b>Guess the Logo</b>\n\n"
                                f"<i>{turn_text} ⏳{time_limit}s</i>",
                        parse_mode="HTML"
                    )
                # Start timeout task even for document
                track_game_task(chat_id, asyncio.create_task(logo_timeout(chat_id, context, round_num)))
                return
        else:
            photo_file = open(logo_path, 'rb')
            
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_file,
                caption=f"<b>Guess the Logo</b>\n\n"
                        f"<i>{turn_text} ⏳{time_limit}s</i>",
                parse_mode="HTML"
            )
        finally:
            photo_file.close()
    except Exception as e:
        logger.error(f"Error sending logo: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading logo. Skipping round...")
        await start_logo_round(chat_id, context)
        return

    # Start timeout task
    track_game_task(chat_id, asyncio.create_task(logo_timeout(chat_id, context, round_num)))


async def logo_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for logo guess."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "4":
        return
        
    time_limit = getattr(session.game, 'time_limit', 60)
    await asyncio.sleep(time_limit)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "4":
        return
    
    # Check if we are still in the same round
    if session.game.current_round == round_num and session.game.waiting_for_answer:
        # Resolve round and reveal answer
        answer = session.game.resolve_round()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\nThe correct answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Start next round
        await start_logo_round(chat_id, context)


async def start_name_the_player_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Name the Player game."""
    await start_name_the_player_round(chat_id, context)


async def start_name_the_player_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Name the Player."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "22":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    # Ensure game is started (for player order init)
    if session.game.current_round == 0:
        session.game.start_game()

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    image_path, round_num = result
    
    try:
        with open(image_path, 'rb') as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"⚽ <b>Name the Player!</b>\n\n"
                        f"First to guess gets a point! (60s)",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending player image {image_path}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading image. Skipping round...")
        await start_name_the_player_round(chat_id, context)
        return

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(name_the_player_timeout(chat_id, context, round_num)))


async def name_the_player_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for name the player guess."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "22":
        return
    
    # Check if we are still in the same round
    if session.game.current_round == round_num and session.game.waiting_for_answer:
        # Resolve round and reveal answer
        answer = session.game.resolve_round()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\nThe player was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Save progress
        save_soccer_players_progress(chat_id, session)
        
        # Start next round
        await start_name_the_player_round(chat_id, context)


def save_soccer_players_progress(chat_id: int, session) -> None:
    """Save the persistent progress for Name the Player."""
    if session and session.game_code == "22" and session.game:
        settings_manager.set_setting(chat_id, "seen_soccer_players", session.game.used_images)


async def start_movie_scene_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Movie Scene game."""
    await start_movie_scene_round(chat_id, context)


async def start_movie_scene_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Movie Scene."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "23":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    # Ensure game is started (for player order init)
    if session.game.current_round == 0:
        session.game.start_game()

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    image_path, round_num = result
    
    try:
        with open(image_path, 'rb') as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"🎬 <b>Movie Scene!</b>\n\n"
                        f"First to guess gets a point! (60s)",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending movie scene image {image_path}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading image. Skipping round...")
        await start_movie_scene_round(chat_id, context)
        return

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(movie_scene_timeout(chat_id, context, round_num)))


async def movie_scene_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for movie scene guess."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "23":
        return
    
    # Check if we are still in the same round
    if session.game.current_round == round_num and session.game.waiting_for_answer:
        # Resolve round and reveal answer
        answer = session.game.resolve_round()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\nThe movie was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Save progress
        save_movie_scene_progress(chat_id, session)
        
        # Start next round
        await start_movie_scene_round(chat_id, context)


def save_movie_scene_progress(chat_id: int, session) -> None:
    """Save the persistent progress for Movie Scene."""
    if session and session.game_code == "23" and session.game:
        settings_manager.set_setting(chat_id, "seen_movie_scenes", session.game.used_images)


async def start_book_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Book game."""
    await start_book_round(chat_id, context)


async def start_book_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess the Book."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "18":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    # Ensure game is started (for player order init)
    if session.game.current_round == 0:
        session.game.start_game()

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    book_path, round_num = result
    
    try:
        with open(book_path, 'rb') as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"📚 <b>Guess the Book!</b>\n\n"
                        f"First to guess gets a point! (60s)",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending book image: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading book image. Skipping round...")
        await start_book_round(chat_id, context)
        return

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(book_timeout(chat_id, context, round_num)))


async def book_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for book guess."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "18":
        return
    
    # Check if we are still in the same round
    if session.game.current_round == round_num and session.game.waiting_for_answer:
        # Resolve round and reveal answer
        answer = session.game.resolve_round()
        reveal_image = session.game.get_reveal_image()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\nThe correct answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )

        # Send the reveal image
        if reveal_image and os.path.exists(reveal_image):
            try:
                with open(reveal_image, 'rb') as f:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=f,
                        caption=f"✅ <b>{answer}</b>",
                        parse_mode="HTML"
                    )
            except Exception as e:
                logger.error(f"Error sending book reveal image: {e}")
        
        # Start next round
        await start_book_round(chat_id, context)


async def start_marvel_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Marvel Character game."""
    await start_marvel_round(chat_id, context)


async def start_marvel_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess the Marvel Character."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "19":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    # Ensure game is started
    if session.game.current_round == 0:
        session.game.start_game()

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    image_path, round_num = result
    
    try:
        with open(image_path, 'rb') as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"🦸 <b>Guess the Marvel Character!</b>\n\n"
                        f"First to guess gets a point! (60s)",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending marvel image: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading image. Skipping round...")
        await start_marvel_round(chat_id, context)
        return

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(marvel_timeout(chat_id, context, round_num)))


async def marvel_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for marvel guess."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "19":
        return
    
    # Check if we are still in the same round
    if session.game.current_round == round_num and session.game.waiting_for_answer:
        # Resolve round and reveal answer
        answer = session.game.resolve_round()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\nThe correct answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Start next round
        await start_marvel_round(chat_id, context)


async def start_guess_addis_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess Addis game."""
    await start_guess_addis_round(chat_id, context)


async def start_guess_addis_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess Addis."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "20":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    # Ensure game is started
    if session.game.current_round == 0:
        session.game.start_game()

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    image_path, round_num = result
    
    try:
        with open(image_path, 'rb') as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"🏘️ <b>Guess Addis! (Sefer)</b>\n\n"
                        f"Round {round_num}/{session.game.rounds_limit}\n"
                        f"First to guess correctly wins! (60s)",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending Guess Addis image: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading image. Skipping round...")
        await start_guess_addis_round(chat_id, context)
        return

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(addis_timeout(chat_id, context, round_num)))


async def addis_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for Guess Addis."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "20":
        return
    
    # Check if we are still in the same round
    if session.game.current_round == round_num and session.game.waiting_for_answer:
        # Resolve round and reveal answer
        answer = session.game.resolve_round()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\nThe correct answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Save progress
        save_addis_progress(chat_id, session)
        
        # Start next round
        await start_guess_addis_round(chat_id, context)


def save_addis_progress(chat_id: int, session: GameSession) -> None:
    """Save the persistent progress for Guess Addis."""
    if session and session.game_code == "20" and session.game:
        settings_manager.set_setting(chat_id, "seen_addis", session.game.used_images)


def save_riddles_progress(chat_id: int, session: GameSession) -> None:
    """Save the persistent progress for Riddles."""
    if session and session.game_code == "27" and session.game:
        settings_manager.set_setting(chat_id, "seen_riddles", session.game.used_riddles)


async def start_guessmoji_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of GuessMoji."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "5":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    emojis, round_num = session.game.start_new_round()
    theme = session.game.theme_name
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<b>Round {round_num}/{session.game.total_rounds}</b>\n\n"
             f"{emojis}\n\n"
             f"<i>First to guess gets a point ⏳60s</i>",
        parse_mode="HTML"
    )
    
    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(guessmoji_timeout(chat_id, context, round_num)))


async def guessmoji_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for GuessMoji round."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "5":
        return
    
    # Check if we are still in the same round and it's in progress
    if session.game.current_round == round_num and session.game.round_in_progress:
        # Time up - No winner
        answer = session.game.get_current_answer()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\n"
                 f"The answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Check game over or start next round
        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            await start_guessmoji_round(chat_id, context)


async def start_movie_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Movie game."""
    await start_movie_round(chat_id, context)


async def start_movie_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess the Movie."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "6":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    # Ensure game is started (for player order init)
    if session.game.current_round == 0:
        session.game.start_game()

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    poster_path, player_id, player_name = result
    
    try:
        with open(poster_path, 'rb') as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"🖼️ <b>Guess the Movie!</b>\n\n"
                        f"👉 <a href=\"tg://user?id={player_id}\">{player_name}</a>, you have 45 seconds!",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending poster: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading poster. Skipping round...")
        await start_movie_round(chat_id, context)
        return

    # Start timeout task (45 seconds)
    round_num = session.game.current_round
    player_id = session.game.current_player_id
    track_game_task(chat_id, asyncio.create_task(movie_timeout(chat_id, context, round_num, player_id)))


async def movie_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int, player_id: int) -> None:
    """Handle timeout for movie guess."""
    await asyncio.sleep(45)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "6":
        return
    
    # Check if we are still in the same round AND waiting for the SAME player
    if session.game.current_round == round_num and session.game.current_player_id == player_id and session.game.waiting_for_answer:
        # Time up - New Round (Next player, New Poster)
        session.game.resolve_round()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>",
            parse_mode="HTML"
        )
        
        # Start next round
        await start_movie_round(chat_id, context)



async def start_flag_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Flag game."""
    await start_flag_round(chat_id, context)


async def start_flag_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess the Flag."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "7":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    flag_path, round_num = result
    
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=open(flag_path, 'rb'),
        caption=f"🌍 <b>Guess the Flag!</b>\n"
                f"Round {round_num}/{session.game.rounds_limit}\n\n"
                f"First to guess gets a point! (60s)",
        parse_mode="HTML"
    )

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(flag_timeout(chat_id, context, round_num)))


async def flag_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for Guess the Flag round."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "7":
        return
    
    # Check if we are still in the same round and it's in progress
    if session.game.current_round == round_num and session.game.round_in_progress:
        # Time up - No winner
        answer = session.game.get_current_answer()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\n"
                 f"The answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Check game over or start next round
        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            await start_flag_round(chat_id, context)




async def start_general_knowledge_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the General Knowledge game."""
    await start_general_knowledge_round(chat_id, context)


async def start_general_knowledge_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of General Knowledge."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "9":
        return

    await asyncio.sleep(2)

    if session.game.game_type == "MCQ":
        await start_general_knowledge_mcq_round(chat_id, context)
        return

    time_limit = session.game.time_limit
    question_text, round_num = session.game.start_new_round()

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🧠 <b>General Knowledge!</b>\n"
             f"Round {round_num}/{session.game.total_rounds}\n\n"
             f"👉 <b>{question_text}</b>\n\n"
             f"First to guess gets a point! ({time_limit}s)",
        parse_mode="HTML"
    )

    track_game_task(chat_id, asyncio.create_task(general_knowledge_timeout(chat_id, context, round_num, time_limit)))


async def start_general_knowledge_mcq_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start an MCQ round using native Telegram quiz poll."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "9":
        return

    time_limit = session.game.time_limit
    question_text, round_num = session.game.start_new_round()
    options, correct_id = session.game.get_mcq_options()

    poll_message = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"🧠 Round {round_num}/{session.game.total_rounds}: {question_text}",
        options=options,
        type="quiz",
        correct_option_id=correct_id,
        is_anonymous=False,
        allows_multiple_answers=False,
        open_period=time_limit
    )

    session.gk_mcq_poll_id = poll_message.poll.id
    session.gk_mcq_answers = {}
    session.gk_mcq_round_num = round_num

    track_game_task(chat_id, asyncio.create_task(general_knowledge_mcq_timeout(chat_id, context, round_num, correct_id, time_limit)))


async def general_knowledge_mcq_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int, correct_id: int, time_limit: int) -> None:
    """Handle timeout for General Knowledge MCQ round."""
    await asyncio.sleep(time_limit)

    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "9":
        return

    if session.game.current_round == round_num and session.game.round_in_progress:
        answer_text = session.game.get_current_answer()
        correct_users = []
        wrong_users = []

        for user_id, selected_id in getattr(session, 'gk_mcq_answers', {}).items():
            if selected_id == correct_id:
                session.game.answer_mcq(user_id, selected_id)
                name = session.game.players.get(user_id, "Player")
                correct_users.append(f"<a href=\"tg://user?id={user_id}\">{name}</a>")
            else:
                name = session.game.players.get(user_id, "Player")
                wrong_users.append(f"<a href=\"tg://user?id={user_id}\">{name}</a>")

        session.game.round_in_progress = False
        session.gk_mcq_poll_id = None

        parts = [f"⏰ <b>Time's Up!</b>\n\nThe answer was: <b>{answer_text}</b>\n"]
        if correct_users:
            parts.append(f"✅ Correct ({len(correct_users)}): {', '.join(correct_users)}")
        if wrong_users:
            parts.append(f"❌ Wrong ({len(wrong_users)}): {', '.join(wrong_users)}")
        if not correct_users and not wrong_users:
            parts.append("No one answered.")

        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(parts),
            parse_mode="HTML"
        )

        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            await start_general_knowledge_round(chat_id, context)


async def general_knowledge_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int, time_limit: int) -> None:
    """Handle timeout for General Knowledge round."""
    await asyncio.sleep(time_limit)

    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "9":
        return

    if session.game.current_round == round_num and session.game.round_in_progress:
        session.game.round_in_progress = False
        answer = session.game.get_current_answer()

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\n"
                 f"The answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )

        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            await start_general_knowledge_round(chat_id, context)



async def start_character_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Character game."""
    await start_character_round(chat_id, context)


async def start_character_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess the Character."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "10":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    cropped_path, round_num = result
    
    try:
        with open(cropped_path, 'rb') as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"🔍 <b>Guess the Person/Character!</b>\n"
                        f"Round {round_num}/{session.game.rounds_limit}\n\n"
                        f"First to guess gets a point! (60s)",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending cropped image: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading image. Skipping round...")
        await start_character_round(chat_id, context)
        return

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(character_timeout(chat_id, context, round_num)))


async def character_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for Guess the Character round."""
    await asyncio.sleep(60)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "10":
        return
    
    # Check if we are still in the same round and it's in progress
    if session.game.current_round == round_num and session.game.round_in_progress:
        # Time up - No winner
        answer = session.game.resolve_round()
        full_image = session.game.get_full_image()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\n"
                 f"The answer was: <b>{answer}</b>",
            parse_mode="HTML"
        )
        
        # Send full image on timeout too? Usually good to show it.
        try:
            with open(full_image, 'rb') as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=f"✅ <b>Full Picture: {answer}</b>",
                    parse_mode="HTML"
                )
        except Exception:
            pass
        
        # Check game over or start next round
async def word_connect_hint_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Reveal a hint after 30 seconds if the round is still in progress."""
    try:
        await asyncio.sleep(30)
        
        session = game_manager.get_game(chat_id)
        if not session or session.game_code != "11" or session.state != GameState.IN_PROGRESS:
            return
        
        if session.game.current_round != round_num or not session.game.round_in_progress:
            return
            
        hint_result = session.game.reveal_letter_hint()
        if hint_result:
            progress = session.game.get_round_progress()
            letters = session.game.current_letters
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"💡 <b>Hint!</b> A letter has been revealed:\n\n"
                     f"Letters: <b>{' '.join(letters).upper()}</b>\n\n"
                     f"{progress}",
                parse_mode="HTML"
            )
            
            if session.game.is_round_finished():
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🎊 <b>Round Completed!</b> 🎊\nAll words found!",
                    parse_mode="HTML"
                )
                
                if session.game.is_game_over():
                    await end_game(chat_id, context, session)
                else:
                    await start_word_connect_round(chat_id, context)
            else:
                # Schedule another hint
                track_game_task(chat_id, asyncio.create_task(word_connect_hint_timeout(chat_id, context, round_num)))
    except asyncio.CancelledError:
        pass


async def start_word_connect_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Word Connect game."""
    await start_word_connect_round(chat_id, context)


async def start_word_connect_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Word Connect."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "11":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    letters = result["letters"]
    round_num = result["round"]
    progress = session.game.get_round_progress()
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔠 <b>Word Connect!</b>\n"
             f"Round {round_num}/{session.game.rounds_limit}\n\n"
             f"Letters: <b>{' '.join(letters).upper()}</b>\n\n"
             f"{progress}\n\n"
             f"Swipe (type) the words to form them!",
        parse_mode="HTML"
    )

    # Start hint timer
    track_game_task(chat_id, asyncio.create_task(word_connect_hint_timeout(chat_id, context, round_num)))


async def start_wdym_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the What You Meme game."""
    await start_wdym_round(chat_id, context)


def get_meme_cache() -> Dict[str, str]:
    """Synchronously read the meme cache from disk."""
    cache_path = os.path.join(os.path.dirname(__file__), "assets", "wdym", "_cache", "meme_cache.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading meme cache: {e}")
    return {}


async def ensure_memes_cached(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, str]:
    """Ensure all memes are uploaded to Telegram and their file_ids are cached in background."""
    async with meme_cache_lock:
        cache_path = os.path.join(os.path.dirname(__file__), "assets", "wdym", "_cache", "meme_cache.json")
        meme_dir = os.path.join(os.path.dirname(__file__), "assets", "wdym", "memes")
        
        cache = get_meme_cache()

        if not os.path.exists(meme_dir):
            return cache

        memes = sorted([f for f in os.listdir(meme_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
        updated = False
        
        # Use the testing group as a storage chat
        storage_chat_id = STORAGE_CHAT_ID
        
        for meme in memes:
            if meme not in cache:
                try:
                    meme_path = os.path.join(meme_dir, meme)
                    with open(meme_path, 'rb') as f:
                        msg = await context.bot.send_photo(
                            chat_id=storage_chat_id,
                            photo=f,
                            caption=f"Caching meme: {meme}",
                            disable_notification=True
                        )
                        file_id = msg.photo[-1].file_id
                        cache[meme] = file_id
                        updated = True
                        with open(cache_path, 'w') as sf:
                            json.dump(cache, sf, indent=2)
                        await asyncio.sleep(2)
                except Exception as e:
                    if "Flood control exceeded" in str(e):
                        retry_after = 30
                        try:
                            import re
                            match = re.search(r"Retry in (\d+) seconds", str(e))
                            if match: retry_after = int(match.group(1)) + 1
                        except: pass
                        logger.warning(f"Rate limited during caching. Sleeping for {retry_after}s...")
                        await asyncio.sleep(retry_after)
                    else:
                        logger.error(f"Error caching meme {meme}: {e}")
                        await asyncio.sleep(1)

        if updated:
            try:
                with open(cache_path, 'w') as f:
                    json.dump(cache, f, indent=2)
                logger.info("Meme cache fully updated.")
            except Exception as e:
                logger.error(f"Error saving meme cache: {e}")
                
        return cache


def get_sticker_cache() -> Dict[str, str]:
    """Synchronously read the sticker cache from disk."""
    cache_path = os.path.join(os.path.dirname(__file__), "assets", "card-deck", "_cache", "sticker_cache.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading sticker cache: {e}")
    return {}


async def ensure_stickers_cached(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, str]:
    """Fetch and cache sticker IDs for Crazy 8."""
    async with card_cache_lock:
        cache_path = os.path.join(os.path.dirname(__file__), "assets", "card-deck", "_cache", "sticker_cache.json")
        cache = get_sticker_cache()
        
        if cache:
            return cache # Assume cache is complete if exists
            
        try:
            sticker_set = await context.bot.get_sticker_set("DeckofCardsTraditional")
            ranks = ['ace', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'jack', 'queen', 'king']
            suits = ['spades', 'diamonds', 'hearts', 'clubs']
            
            # Mapping based on observation: indices 0-51 cover the 52 cards
            for i, sticker in enumerate(sticker_set.stickers):
                if i >= 52: break # Skip joker for now
                
                rank_idx = i // 4
                suit_idx = i % 4
                
                rank = ranks[rank_idx]
                suit = suits[suit_idx]
                
                key = f"{rank}_of_{suit}"
                cache[key] = sticker.file_id
            
            with open(cache_path, 'w') as f:
                json.dump(cache, f, indent=2)
            
            logger.info(f"Sticker cache updated with {len(cache)} cards.")
            return cache
            
        except Exception as e:
            logger.error(f"Error caching stickers: {e}")
            return cache

async def start_wdym_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of What You Meme."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "12":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    result = session.game.start_new_round()
    if not result:
        # Game Over or not enough players
        if len(session.game.players) < 2:
             await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Not enough players to continue! Need at least 2 players.",
                parse_mode="HTML"
            )
             await end_game(chat_id, context, session)
        else:
            await end_game(chat_id, context, session)
        return

    question = result["question"]
    round_num = result["round"]
    
    keyboard = [
        [InlineKeyboardButton("What you meme", switch_inline_query_current_chat="meme ")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Round {round_num}\n"
             f"<b>{question}</b>",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

    # Start timeout task (45 seconds total)
    track_game_task(chat_id, asyncio.create_task(wdym_timeout_manager(chat_id, context, round_num)))


async def wdym_timeout_manager(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Manage 30s reminder and 45s force-skip for WDYM."""
    # 30 second reminder
    await asyncio.sleep(30)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "12" or session.game.current_round != round_num or not session.game.round_in_progress:
        return
        
    pending = session.game.get_pending_players()
    if pending:
        mention_list = []
        for uid in pending:
            name = session.game.players[uid]
            mention_list.append(f"<a href=\"tg://user?id={uid}\">{name}</a>")
            
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Hurry up!</b> 15 seconds left!\n\n"
                 f"Still waiting for: {', '.join(mention_list)}",
            parse_mode="HTML"
        )
        
        # 15 more seconds total (45s)
        await asyncio.sleep(15)
        
        # Check again
        session = game_manager.get_game(chat_id)
        if not session or session.game_code != "12" or session.game.current_round != round_num or not session.game.round_in_progress:
            return
            
        pending = session.game.get_pending_players()
        if pending:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⌛️ <b>Time's up!</b> Moving to the next question...",
                parse_mode="HTML"
            )
            session.game.round_in_progress = False
            if session.game.is_game_over():
                await end_game(chat_id, context, session)
            else:
                await start_wdym_round(chat_id, context)


async def process_hear_me_out_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Process a photo submission for the Hear Me Out game."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    
    if session.game.get_current_player_id() != user.id:
        return
        
    photo = message.photo[-1]  # Get highest resolution
    file = await context.bot.get_file(photo.file_id)
    
    # Save the photo temporarily
    tmp_path = f"/tmp/hmo_user_{user.id}_{session.game.current_turn}.jpg"
    await file.download_to_drive(tmp_path)
    
    # Process it
    composite_path = session.game.submit_picture(user.id, tmp_path)
    if not composite_path:
        await message.reply_text("❌ Error processing picture.")
        return
        
    if session.game.is_game_over():
        await message.reply_photo(
            photo=open(composite_path, 'rb'),
            caption="🎉 <b>The Hear Me Out cake is complete!</b>",
            parse_mode="HTML"
        )
        await end_game(chat.id, context, session)
    else:
        current_player_id = session.game.get_current_player_id()
        current_player_name = session.game.get_current_player_name()
        
        await message.reply_photo(
            photo=open(composite_path, 'rb'),
            caption=f"🎂 <b>Picture added!</b>\n\n"
                    f"👉 It's <a href=\"tg://user?id={current_player_id}\">{current_player_name}</a>'s turn! Send a picture.",
            parse_mode="HTML"
        )

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect meme submissions by watching for photos in WDYM games."""
    message = update.effective_message
    if not message or not message.photo:
        return
        
    user = update.effective_user
    chat = update.effective_chat
    
    session = game_manager.get_game(chat.id)
    if not session:
        return

    # Check for Hear Me Out photo
    if session.game_code == "21":
        await process_hear_me_out_photo(update, context, session)
        return

    if session.game_code != "12" or not session.game.round_in_progress:
        return
        
    # If the user is a player, any photo they send is a submission
    if user.id in session.game.players:
        file_id = message.photo[-1].file_id
        success = session.game.submit_meme(user.id, file_id)
        if success:
            # Check if all players have submitted
            pending = session.game.get_pending_players()
            if not pending:
                session.game.round_in_progress = False
                if session.game.is_game_over():
                    await end_game(chat.id, context, session)
                else:
                    await start_wdym_round(chat.id, context)


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline queries for memes and cards."""
    iq = update.inline_query
    query_text = iq.query
    user = iq.from_user
    offset = int(iq.offset) if iq.offset else 0

    # 1. Handle Memes
    if query_text.startswith("meme"):
        # Existing meme logic
        cache = get_meme_cache()
        asyncio.create_task(ensure_memes_cached(context))
        
        session = None
        for s in game_manager.active_games.values():
            if user.id in s.players and s.game_code == "12":
                session = s
                break
        
        prompt = session.game.current_question if (session and session.game.round_in_progress) else "Meme time!"
        
        cache_items = sorted(cache.items())
        results = []
        end_idx = min(offset + 50, len(cache_items))
        for i in range(offset, end_idx):
            meme, file_id = cache_items[i]
            results.append(
                InlineQueryResultCachedPhoto(
                    id=f"wdym_{meme}", 
                    photo_file_id=file_id,
                    title=f"Meme {i+1}",
                    caption=f"🃏 <b>{prompt}</b>",
                    parse_mode="HTML"
                )
            )
        
        if not results and offset == 0:
            results.append(
                InlineQueryResultArticle(
                    id="caching", title="Memes are being cached...",
                    input_message_content=InputTextMessageContent("Bot is still processing memes.")
                )
            )
        
        next_offset = str(offset + 50) if offset + 50 < len(cache_items) else ""
        await iq.answer(results, cache_time=5, is_personal=True, next_offset=next_offset)
        return

    # 2. Handle Crazy 8 Cards
    elif query_text.startswith("c8"):
        cache = get_sticker_cache()
        asyncio.create_task(ensure_stickers_cached(context))
        
        # Find active session
        session = None
        for s in game_manager.active_games.values():
            if user.id in s.players and s.game_code == "17":
                session = s
                break
        
        if not session or not session.game:
            await iq.answer([], cache_time=0, is_personal=True)
            return

        is_turn = (user.id == session.game.current_player_id)
        
        results = []
        hand = session.game.hands.get(user.id, [])
        
        # Filter hand if user typed something specific after 'c8 '
        search_filter = query_text[2:].strip().lower()
        
        for i, card in enumerate(hand):
            card_desc = str(card)
            if search_filter and search_filter not in card_desc.lower():
                continue
                
            file_id = cache.get(f"{card.rank}_of_{card.suit}")
            if file_id:
                # Note: InlineQueryResultCachedSticker doesn't support captions in the same way photos do
                # It just sends the sticker.
                results.append(
                    InlineQueryResultCachedSticker(
                        id=f"c8_{user.id}_{i}",
                        sticker_file_id=file_id
                    )
                )

        # Add "Draw Card" as an article at the end
        if is_turn:
            results.append(
                InlineQueryResultArticle(
                    id=f"c8_draw_{user.id}",
                    title="🃏 Draw a Card",
                    description="Draw a card from the deck and pass your turn.",
                    input_message_content=InputTextMessageContent("Draw a card")
                )
            )

        await iq.answer(results, cache_time=0, is_personal=True)
        return


async def handle_sticker_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect card plays via stickers in Crazy 8."""
    message = update.effective_message
    if not message or not message.sticker:
        return
        
    user = update.effective_user
    chat = update.effective_chat
    session = game_manager.get_game(chat.id)

    if not session or session.game_code != "17" or not session.game:
        return
        
    if user.id not in session.game.players:
        return

    # Map sticker back to card
    sticker_id = message.sticker.file_id
    cache = get_sticker_cache()
    
    card_key = None
    for key, fid in cache.items():
        if fid == sticker_id:
            card_key = key
            break
            
    if not card_key:
        return # Not a card sticker

    # card_key is like "ace_of_spades"
    card_name = card_key.replace('_', ' ')
    
    # Process play
    success, msg_text, filename = session.game.play_card(user.id, card_name)
    if success:
        # Sticker is already in chat. Just announce move.
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"✅ {session.game.players[user.id]} played <b>{card_name.title()}</b>.\n\n{msg_text}",
            parse_mode="HTML"
        )
        
        if session.game.is_last_card(user.id):
             await context.bot.send_message(chat_id=chat.id, text=f"⚠️ {session.game.players[user.id]} has only ONE card left!")

        if session.game.is_game_over(user.id):
            await end_game(chat.id, context, session)
        else:
            await send_c8_buttons(chat.id, context, session)
    else:
        # Invalid play, bot replies.
        await message.reply_text(f"⚠️ {msg_text}", parse_mode="HTML")


async def chosen_inline_result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track when a user picks a result from an inline query (meme)."""
    result    = update.chosen_inline_result
    result_id = result.result_id
    user      = result.from_user


async def start_ts_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Taylor Swift vs Shakespeare game."""
    await start_ts_round(chat_id, context)

async def start_20q_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: GameSession) -> None:
    """Start the 20 Questions game."""
    session.game.round_in_progress = False
    await context.bot.send_message(
        chat_id=chat_id,
        text="🕵️‍♂️ <b>20 Questions Started!</b>\n\n"
             "Rules:\n"
             "1. One player is the <b>Host</b> and gets a secret word.\n"
             "2. Everyone else asks Yes/No questions.\n"
             "3. Questions <b>must end with a ?</b> to be counted.\n"
             "4. You have <b>20 Questions</b> or <b>5 Minutes</b> to guess the word.\n"
             "5. If you guess it, YOU become the Host!\n\n"
             "Starting first round...",
        parse_mode="HTML"
    )
    # Start first round
    await start_20q_round(chat_id, context)


async def start_20q_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE, forced_host_id: Optional[int] = None) -> None:
    """Start a new round of 20 Questions."""
    session = game_manager.get_game(chat_id)
    if not session or not isinstance(session.game, TwentyQuestionsGame):
        return

    try:
        # Start logical round
        if not session.game.start_new_round(forced_host_id):
            await context.bot.send_message(chat_id=chat_id, text="Not enough players to continue!")
            session.end_game()
            game_manager.remove_game(chat_id)
            return

        host_id = session.game.host_id
        host_name = session.game.get_host_name()
        
        # Create keyboard
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤐 View Secret Word (Host Only)", callback_data="view_secret_word")]
        ])

        # Using standard HTML
        message_text = (
            f"🔴 <b>Round {session.game.current_round}</b>\n\n"
            f"👤 <b>Host:</b> <a href=\"tg://user?id={host_id}\">{html.escape(host_name)}</a>\n"
            f"❓ <b>Questions Remaining:</b> 20\n"
            f"⏱ <b>Time Limit:</b> 5 Minutes\n\n"
            f"Host, click below to see your word!"
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
        # 5 Minute Timeout
        task = asyncio.create_task(twenty_questions_timeout(chat_id, context, session.game.current_round))
        track_game_task(chat_id, task)
        
    except Exception as e:
        logger.error(f"Error in start_20q_round: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error starting round. check logs.")


async def twenty_questions_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int):
    """Handle 5 minute timeout for 20 questions round."""
    try:
        await asyncio.sleep(300) # 5 minutes
        
        session = game_manager.get_game(chat_id)
        if session and session.game_code == "15" and session.game.current_round == round_num and session.game.round_in_progress:
            # Time up! Host wins.
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ <b>Time's Up!</b>\n\n"
                     f"The word was: <b>{session.game.current_word}</b>\n"
                     f"Host gets a point!",
                parse_mode="HTML"
            )
            session.game.host_wins_round()
            
            if session.game.is_game_over():
                await end_game(chat_id, context, session)
            else:
                await start_20q_round(chat_id, context)
                
    except asyncio.CancelledError:
        pass


async def handle_20q_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle View Secret Word callback."""
    query = update.callback_query
    user = query.from_user
    chat = update.effective_chat
    
    session = game_manager.get_game(chat.id)
    if not session or session.game_code != "15":
        await query.answer("Game not active.")
        return

    if user.id != session.game.host_id:
        await query.answer("❌ You are not the Host!", show_alert=True)
    else:
        word = session.game.current_word
        await query.answer(f"🤫 Secret Word: {word}", show_alert=True)


async def start_ts_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Taylor Swift vs Shakespeare."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "13":
        return

    # Delay slightly
    await asyncio.sleep(2)
    
    quote, round_num = session.game.start_new_round()
    if not quote:
        await end_game(chat_id, context, session)
        return

    keyboard = [
        [
            InlineKeyboardButton("Taylor Swift", callback_data=f"ts_vote_Taylor Swift"),
            InlineKeyboardButton("Shakespeare", callback_data=f"ts_vote_Shakespeare")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📜 <b>Taylor Swift Or Shakespeare?</b>\n"
             f"Round {round_num}/{session.game.rounds_limit}\n\n"
             f"<i>\"{quote}\"</i>\n\n"
             f"Choose your answer! (30s)",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

    # Start timeout task (30 seconds)
    track_game_task(chat_id, asyncio.create_task(ts_round_timeout(chat_id, context, round_num)))

async def ts_round_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for Taylor Swift vs Shakespeare round."""
    await asyncio.sleep(30)
    
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "13" or not session.game.round_in_progress:
        return

    if session.game.current_round != round_num:
        return

    result = session.game.resolve_round()
    if not result:
        return

    correct_author = result["correct_author"]
    winners_ids = result["winners"]
    quote = result["quote"]

    winner_mentions = []
    for uid in winners_ids:
        name = session.game.players.get(uid, "Player")
        winner_mentions.append(f"<a href=\"tg://user?id={uid}\">{name}</a>")

    if winner_mentions:
        winners_text = f"✅ <b>Correct!</b> It was <b>{correct_author}</b>!\n\n" \
                       f"🏆 Winners this round: {', '.join(winner_mentions)}"
    else:
        winners_text = f"❌ <b>Too slow!</b> Nobody guessed right.\n" \
                       f"The correct author was: <b>{correct_author}</b>"

    await context.bot.send_message(
        chat_id=chat_id,
        text=winners_text,
        parse_mode="HTML"
    )

    if session.game.is_game_over():
        await end_game(chat_id, context, session)
    else:
        await start_ts_round(chat_id, context)

async def handle_ts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voting callback queries for Taylor vs Shakespeare."""
    query = update.callback_query
    user = query.from_user
    chat = update.effective_chat
    
    author = query.data.split("_")[2]
    
    session = game_manager.get_game(chat.id)
    if not session or session.game_code != "13" or not session.game.round_in_progress:
        await query.answer("Game not active.")
        return

    # Just record the vote
    try:
        if session.game.record_vote(user.id, author):
            await query.answer(f"Voted for {author}!")
        else:
            await query.answer("Couldn't record vote.")
    except (NetworkError, TimedOut, TelegramError) as e:
        logger.warning(f"Failed to answer TS callback query: {e}")


async def start_song_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Guess the Song game."""
    await start_song_round(chat_id, context)


async def start_song_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Guess the Song."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "16":
        return

    # Delay slightly
    await asyncio.sleep(2)

    result = session.game.start_new_round()
    if not result:
        # Game Over
        await end_game(chat_id, context, session)
        return

    audio_path, round_num = result

    # Build hints about what to guess
    artist = session.game.get_current_artist()
    if artist:
        hint_text = "Guess the <b>song title</b> and the <b>artist</b>!"
    else:
        hint_text = "Guess the <b>song title</b>!"

    try:
        with open(audio_path, 'rb') as f:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=f,
                title=f"Song #{round_num}",
                performer="???",
                caption=f"🎧 <b>Guess the Song!</b>\n"
                        f"Round {round_num}/{session.game.total_rounds}\n\n"
                        f"{hint_text}\n"
                        f"You have 60 seconds! ⏱",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending audio intro: {e}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Error loading audio. Skipping round...")
        session.game.round_in_progress = False
        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            await start_song_round(chat_id, context)
        return

    # Start timeout task (60 seconds)
    track_game_task(chat_id, asyncio.create_task(song_timeout(chat_id, context, round_num)))


async def send_song_reveal(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Send the album cover and song info after a round."""
    info = session.game.get_song_info()
    if not info:
        return

    title = info["title"]
    artist = info["artist"] if info["artist"] else "Unknown Artist"
    cover_path = info["cover_path"]

    caption = (
        f"💿 <b>{title}</b>\n"
        f"🎤 <b>{artist}</b>"
    )

    try:
        if os.path.exists(cover_path):
            with open(cover_path, 'rb') as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode="HTML"
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending song cover: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML"
        )


async def song_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for Guess the Song round."""
    await asyncio.sleep(60)

    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "16":
        return

    # Check if we are still in the same round and it's in progress
    if session.game.current_round == round_num and session.game.round_in_progress:
        session.game.round_in_progress = False

        # Build reveal message for anything unguessed
        reveal_parts = []
        if not session.game.title_guessed:
            reveal_parts.append(f"🎵 Song: <b>{session.game.get_current_title()}</b>")
        if not session.game.artist_guessed:
            artist = session.game.get_current_artist()
            if artist:
                reveal_parts.append(f"🎤 Artist: <b>{artist}</b>")

        reveal_text = "\n".join(reveal_parts) if reveal_parts else ""

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\n{reveal_text}" if reveal_text else "⏰ <b>Time's Up!</b>",
            parse_mode="HTML"
        )

        # Send album cover
        await send_song_reveal(chat_id, context, session)
        await asyncio.sleep(5)

        # Check game over or start next round
        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            await start_song_round(chat_id, context)


async def start_riddle_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "27":
        return

    await asyncio.sleep(2)

    if session.game.is_game_over() and not session.game.endless:
        await end_game(chat_id, context, session)
        return

    question, round_num = session.game.start_new_round()
    total = session.game.total_rounds
    round_text = f"Riddle {round_num}/{total}" if not session.game.endless else f"Riddle {round_num}"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🧩 <b>Riddles</b>\n{round_text}\n\n<blockquote>{question}</blockquote>",
        parse_mode="HTML"
    )

    track_game_task(chat_id, asyncio.create_task(riddle_timeout(chat_id, context, round_num)))


async def riddle_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    session = game_manager.get_game(chat_id)
    time_limit = getattr(session.game, 'time_limit', 30) if session and session.game_code == "27" else 30

    await asyncio.sleep(time_limit)

    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "27":
        return

    if session.game.current_round == round_num and session.game.round_in_progress:
        session.game.round_in_progress = False
        answer = session.game.get_current_answer()

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\nAnswer: <b>{answer}</b>",
            parse_mode="HTML"
        )

        save_riddles_progress(chat_id, session)
        await asyncio.sleep(3)

        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            if not session.game.endless:
                await start_riddle_round(chat_id, context)
            else:
                await context.bot.send_message(chat_id=chat_id, text="use /newriddle to start the next riddle!")


async def start_sfl_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Song From Lyrics game."""
    await start_sfl_round(chat_id, context)


async def start_sfl_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a new round of Song From Lyrics."""
    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "26":
        return

    # Delay slightly
    await asyncio.sleep(2)

    if not session.game.start_new_round():
        # Game Over
        await end_game(chat_id, context, session)
        return

    lyrics = session.game.get_current_lyrics_block()
    round_num = session.game.current_round
    total = session.game.total_rounds
    round_text = f"Round {round_num}/{total}" if not session.game.endless else f"Round {round_num}"

    keyboard = [[InlineKeyboardButton("▶Next Line", callback_data="sfl_reveal")]]
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<b>Guess the Song from Lyrics!</b>\n"
             f"{round_text}\n\n"
             f"<blockquote>{lyrics}</blockquote>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

    # Start timeout task (90 seconds for lyrics guessing)
    track_game_task(chat_id, asyncio.create_task(sfl_timeout(chat_id, context, round_num)))


async def handle_sfl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'Reveal Next Line' button."""
    query = update.callback_query
    user = query.from_user
    chat = update.effective_chat
    
    session = game_manager.get_game(chat.id)
    if not session or session.game_code != "26" or not session.game.round_in_progress:
        await query.answer("Game not active.")
        return

    success, updated_lyrics = session.game.reveal_next_line(user.id)
    if not success:
        await query.answer("No more lines to reveal!")
        return

    await query.answer("Line revealed!")
    
    round_num = session.game.current_round
    total = session.game.total_rounds
    round_text = f"Round {round_num}/{total}" if not session.game.endless else f"Round {round_num}"
    
    # Keep button if more lines exist
    keyboard = []
    if not session.game.all_lines_revealed():
        keyboard = [[InlineKeyboardButton("▶Next Line", callback_data="sfl_reveal")]]

    await query.edit_message_text(
        text=f"<b>Guess the Song from Lyrics!</b>\n"
             f"{round_text}\n\n"
             f"<blockquote>{updated_lyrics}</blockquote>",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        parse_mode="HTML"
    )


async def send_sfl_reveal(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Reveal the song details and cover after a round."""
    title = session.game.get_current_title()
    artist = session.game.get_current_artist()
    cover_path = session.game.get_cover_path()

    caption = (
        f"💿 <b>{title}</b>\n"
        f"🎤 <b>{artist}</b>"
    )

    try:
        if cover_path and os.path.exists(cover_path):
            with open(cover_path, 'rb') as f:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode="HTML"
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error sending song reveal: {e}")
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")


async def sfl_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int) -> None:
    """Handle timeout for Song From Lyrics round."""
    await asyncio.sleep(90)

    session = game_manager.get_game(chat_id)
    if not session or session.game_code != "26":
        return

    if session.game.current_round == round_num and session.game.round_in_progress:
        session.game.round_in_progress = False

        reveal_parts = []
        if not session.game.title_guessed:
            reveal_parts.append(f"🎵 Song: <b>{session.game.get_current_title()}</b>")
        if not session.game.artist_guessed:
            reveal_parts.append(f"🎤 Artist: <b>{session.game.get_current_artist()}</b>")

        reveal_text = "\n".join(reveal_parts)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ <b>Time's Up!</b>\n\n{reveal_text}",
            parse_mode="HTML"
        )

        await send_sfl_reveal(chat_id, context, session)
        await asyncio.sleep(5)

        if session.game.is_game_over():
            await end_game(chat_id, context, session)
        else:
            if not session.game.endless:
                await start_sfl_round(chat_id, context)
            else:
                await context.bot.send_message(chat_id=chat_id, text="use /newsong to start the next round!")


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skip the current round (if not endless and user is in game)."""
    chat = update.effective_chat
    user = update.effective_user
    
    session = game_manager.get_game(chat.id)
    if not session or not session.game or session.state != GameState.IN_PROGRESS:
        return
        
    if user.id not in session.players:
        await update.message.reply_text("❌ Only players can skip!")
        return

    if session.game_code == "26":
        if session.game.endless:
            await update.message.reply_text("💡 In endless mode, use /newsong to skip or advance.")
            return
            
        session.game.round_in_progress = False
        await update.message.reply_text("⏭ <b>Skipping round...</b>", parse_mode="HTML")
        await send_sfl_reveal(chat.id, context, session)
        await asyncio.sleep(3)
        
        if session.game.is_game_over():
            await end_game(chat.id, context, session)
        else:
            await start_sfl_round(chat.id, context)
    else:
        # Fallback for other games if needed, but the user specifically asked for this one
        pass


async def error_handler(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates."""
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f'Network error: {context.error}')
        return
    if isinstance(context.error, Forbidden):
        logger.warning(f'Bot was blocked or kicked: {context.error}')
        return
        
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)


BOT_MAIN_LOOP = None

async def post_init(application: Application) -> None:
    """Explicitly initialize the bot."""
    global BOT_MAIN_LOOP
    BOT_MAIN_LOOP = asyncio.get_running_loop()
    
    await application.bot.initialize()
    bot_info = await application.bot.get_me()
    logger.info(f"Bot initialized: {bot_info.id} (@{bot_info.username})")
    
    # Set bot commands for the menu automatically
    from telegram import BotCommand
    commands = [
        BotCommand("start", "Start a game"),
        BotCommand("join", "Join the game"),
        BotCommand("leave", "Leave the game"),
        BotCommand("skip", "Skip current round"),
        BotCommand("newsong", "Next song (endless mode)"),
        BotCommand("newriddle", "Next riddle (endless mode)"),
        BotCommand("leaderboard", "Show group leaderboard"),
        BotCommand("extend", "[admin] extend joining period by 10 seconds"),
        BotCommand("quit", "[admin] Stop current game in progress"),
        BotCommand("settings", "[admin] edit bot behaviour"),
        BotCommand("minigames", "List of HTML 5 Games")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands updated successfully.")

    # Trigger background caching on startup with proper context
    # Use a dummy context since ensure_memes_cached only needs context.bot
    from telegram.ext import CallbackContext
    dummy_context = CallbackContext(application)
    asyncio.create_task(ensure_memes_cached(dummy_context))
    asyncio.create_task(ensure_stickers_cached(dummy_context))



# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════

async def start_crazy8_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Start the Crazy 8 game."""
    first_msg_filename = session.game.start_game()
    if not first_msg_filename:
        await context.bot.send_message(chat_id=chat_id, text="Error starting game.")
        return
        
    await context.bot.send_message(
        chat_id=chat_id,
        text="🃏 <b>Crazy 8 Started!</b> 🃏\n\nEach player has been dealt 7 cards.",
        parse_mode="HTML"
    )
    
    # Send top card as sticker
    top_card = session.game.get_top_card()
    cache = get_sticker_cache()
    sticker_id = cache.get(f"{top_card.rank}_of_{top_card.suit}")
    
    caption = f"The top card is {str(top_card)}.\n\nIt is {session.game.players[session.game.current_player_id]}'s turn."
    
    try:
        if sticker_id:
            await context.bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error sending start card: {e}")
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
        
    await send_c8_buttons(chat_id, context, session)

async def send_c8_buttons(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session) -> None:
    """Send inline buttons for viewing hand and drawing a card."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🃏 Play / Draw", switch_inline_query_current_chat="c8")
        ]
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"👉 <a href=\"tg://user?id={session.game.current_player_id}\">{session.game.players[session.game.current_player_id]}</a>, it's your turn!\nClick the button below to view your cards and play one.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

async def handle_c8_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy/Simplified button handler. Viewing hands is now via inline queries."""
    query = update.callback_query
    await query.answer("Use the 'Play / Draw' button to interact!", show_alert=True)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /leaderboard command to show the group leaderboard."""
    chat = update.effective_chat

    # Only work in groups
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("Leaderboards only work in groups!")
        return

    # Show total leaderboard page 1
    text, reply_markup = _build_leaderboard_message(page=1, game_filter=None)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


async def handle_leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses for leaderboard navigation."""
    query = update.callback_query
    data = query.data  # e.g. "lb_page_2", "lb_game_Guess the Logo", "lb_back"

    if data == "lb_noop":
        await query.answer()
        return

    if data == "lb_back" or data == "lb_total":
        # Back to total leaderboard
        text, reply_markup = _build_leaderboard_message(page=1, game_filter=None)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
        await query.answer()
        return

    if data == "lb_games":
        # Show game filter selection
        text, reply_markup = _build_game_filter_message()
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
        await query.answer()
        return

    if data.startswith("lb_page_"):
        # Pagination for total leaderboard
        try:
            page = int(data.split("_")[2])
        except (IndexError, ValueError):
            page = 1
        text, reply_markup = _build_leaderboard_message(page=page, game_filter=None)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
        await query.answer()
        return

    if data.startswith("lb_gpage_"):
        # Pagination for game-filtered leaderboard: lb_gpage_<page>_<game_name>
        parts = data.split("_", 3)  # ['lb', 'gpage', '<page>', '<game_name>']
        try:
            page = int(parts[2])
            game_name = parts[3]
        except (IndexError, ValueError):
            await query.answer("Error")
            return
        text, reply_markup = _build_leaderboard_message(page=page, game_filter=game_name)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
        await query.answer()
        return

    if data.startswith("lb_game_"):
        # Filter by specific game
        game_name = data[8:]  # everything after "lb_game_"
        text, reply_markup = _build_leaderboard_message(page=1, game_filter=game_name)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
        await query.answer()
        return

    await query.answer()


def _build_leaderboard_message(page: int, game_filter: Optional[str] = None):
    """
    Build the leaderboard text and inline keyboard.
    Returns (text, InlineKeyboardMarkup).
    """
    if game_filter:
        entries, current_page, total_pages = get_game_leaderboard(game_filter, page)
        title = f"<b>{game_filter} Leaderboard</b>"
    else:
        entries, current_page, total_pages = get_total_leaderboard(page)
        title = "<b>All-Time Leaderboard</b>"

    if not entries:
        text = f"{title}\n\nNo scores recorded yet! Play some games first."
        keyboard = []
        if game_filter:
            keyboard.append([InlineKeyboardButton("< Back to Overall", callback_data="lb_total")])
        return text, InlineKeyboardMarkup(keyboard) if keyboard else None

    # Build leaderboard text
    text = f"{title}\n\n"
    start_rank = (current_page - 1) * 10 + 1
    for i, (uid, username, score) in enumerate(entries):
        rank = start_rank + i
        medal = "#1" if rank == 1 else "#2" if rank == 2 else "#3" if rank == 3 else f"#{rank}"
        text += f"{medal} <a href=\"tg://user?id={uid}\"><b>{username}</b></a> — {score} pts\n"

    text += f"\nPage {current_page}/{total_pages}"

    # Build keyboard
    rows = []

    # Pagination row
    nav_buttons = []
    if current_page > 1:
        if game_filter:
            nav_buttons.append(InlineKeyboardButton("< Prev", callback_data=f"lb_gpage_{current_page - 1}_{game_filter}"))
        else:
            nav_buttons.append(InlineKeyboardButton("< Prev", callback_data=f"lb_page_{current_page - 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(" ", callback_data="lb_noop"))

    nav_buttons.append(InlineKeyboardButton(f"{current_page}/{total_pages}", callback_data="lb_noop"))

    if current_page < total_pages:
        if game_filter:
            nav_buttons.append(InlineKeyboardButton("Next >", callback_data=f"lb_gpage_{current_page + 1}_{game_filter}"))
        else:
            nav_buttons.append(InlineKeyboardButton("Next >", callback_data=f"lb_page_{current_page + 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(" ", callback_data="lb_noop"))

    rows.append(nav_buttons)

    # Filter / back buttons
    if game_filter:
        rows.append([InlineKeyboardButton("< Back to Overall", callback_data="lb_total")])
        rows.append([InlineKeyboardButton("Filter by Game", callback_data="lb_games")])
    else:
        rows.append([InlineKeyboardButton("Filter by Game", callback_data="lb_games")])

    return text, InlineKeyboardMarkup(rows)


def _build_game_filter_message():
    """
    Build a message showing all available games as filter buttons.
    Returns (text, InlineKeyboardMarkup).
    """
    game_names = get_game_names()

    if not game_names:
        text = "<b>Filter by Game</b>\n\nNo games with recorded scores yet!"
        keyboard = [[InlineKeyboardButton("< Back", callback_data="lb_total")]]
        return text, InlineKeyboardMarkup(keyboard)

    text = "<b>Filter by Game</b>\n\nSelect a game to view its leaderboard:"

    rows = []
    # Two buttons per row
    for i in range(0, len(game_names), 2):
        row = [InlineKeyboardButton(game_names[i], callback_data=f"lb_game_{game_names[i]}")]
        if i + 1 < len(game_names):
            row.append(InlineKeyboardButton(game_names[i + 1], callback_data=f"lb_game_{game_names[i + 1]}"))
        rows.append(row)

    rows.append([InlineKeyboardButton("< Back to Overall", callback_data="lb_total")])

    return text, InlineKeyboardMarkup(rows)


MINI_GAMES = {
    "flappybird": ("Flappy Bird 🐦", "flappy-bird/flappybird.html"),
    "2048": ("2048 🔢", "2048/2048.html"),
    "candycrush": ("Candy Crush 🍬", "candy-crush/candycrush.html"),
    "pianotiles": ("Piano Tiles 🎹", "piano-tiles/pianotiles.html"),
    "typerace": ("Type Race 🏎", "type-race/typerace.html")
}

# RC4 for score decryption
def rc4_decrypt(key, data):
    S = list(range(256))
    j = 0
    out = []
    for i in range(256):
        j = (j + S[i] + ord(key[i % len(key)])) % 256
        S[i], S[j] = S[j], S[i]
    i = j = 0
    for char in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        out.append(chr(ord(char) ^ S[(S[i] + S[j]) % 256]))
    return "".join(out)

async def html5_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle HTML5 Game Callback Queries."""
    query = update.callback_query
    if not query.game_short_name:
        return
        
    host_url = os.environ.get("HOST_URL", "https://games-2xn4.onrender.com")
    if host_url.endswith('/'):
        host_url = host_url[:-1]
    
    game_id = query.game_short_name
    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else ""
    message_id = query.message.message_id if query.message else ""
    inline_msg_id = query.inline_message_id or ""
    
    params = f"?user_id={user_id}&chat_id={chat_id}&message_id={message_id}&inline_message_id={inline_msg_id}"
    
    if game_id in MINI_GAMES:
        _, path = MINI_GAMES[game_id]
        url = f"{host_url}/html5-games/{path}{params}"
        await context.bot.answer_callback_query(query.id, url=url)
    elif game_id in ["flappy", "flappy_bird"]: # Aliases
        url = f"{host_url}/html5-games/flappy-bird/flappybird.html{params}"
        await context.bot.answer_callback_query(query.id, url=url)
    else:
        await context.bot.answer_callback_query(query.id, text="Game not found", show_alert=True)

async def flappy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the Flappy Bird game."""
    chat = update.effective_chat
    try:
        await context.bot.send_game(chat_id=chat.id, game_short_name="flappybird")
    except Exception as e:
        logger.error(f"Failed to send game 'flappybird': {e}")
        from telegram import WebAppInfo
        
        host_url = os.environ.get("HOST_URL", "https://gamio-testing.onrender.com")
        if host_url.endswith('/'):
            host_url = host_url[:-1]
        url = f"{host_url}/html5-games/flappy-bird/flappybird.html"
        
        keyboard = [[InlineKeyboardButton("Play Flappy Bird", web_app=WebAppInfo(url=url))]]
        await context.bot.send_message(
            chat_id=chat.id,
            text="Play Flappy Bird! (Web App Fallback)\nNote: To use native games, register 'flappybird' in BotFather.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def minigames_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the available mini-games menu."""
    keyboard = []
    # 2 columns for the menu
    game_ids = list(MINI_GAMES.keys())
    for i in range(0, len(game_ids), 2):
        row = []
        gid1 = game_ids[i]
        name1, _ = MINI_GAMES[gid1]
        row.append(InlineKeyboardButton(name1, callback_data=f"mg_play_{gid1}"))
        
        if i + 1 < len(game_ids):
            gid2 = game_ids[i+1]
            name2, _ = MINI_GAMES[gid2]
            row.append(InlineKeyboardButton(name2, callback_data=f"mg_play_{gid2}"))
        keyboard.append(row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "<b>Mini Games</b> 🕹\nSelect a game to play:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def minigames_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle mini-game selection from the menu."""
    query = update.callback_query
    data = query.data
    
    if data.startswith("mg_play_"):
        game_id = data.replace("mg_play_", "")
        chat_id = query.message.chat_id
        
        # Delete the menu message
        await query.message.delete()
        
        # Send the game
        try:
            await context.bot.send_game(chat_id=chat_id, game_short_name=game_id)
        except Exception as e:
            logger.error(f"Failed to send game '{game_id}': {e}")
            from telegram import WebAppInfo
            
            host_url = os.environ.get("HOST_URL", "https://gamio-testing.onrender.com")
            if host_url.endswith('/'):
                host_url = host_url[:-1]
            
            if game_id in MINI_GAMES:
                name, path = MINI_GAMES[game_id]
                url = f"{host_url}/html5-games/{path}"
                keyboard = [[InlineKeyboardButton(f"Play {name}", web_app=WebAppInfo(url=url))]]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Play {name}! (Web App Fallback)\nNote: To use native games, register '{game_id}' in BotFather.",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

def main() -> None:
    """Start the bot."""
    # Get bot token from environment
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("No BOT_TOKEN found in environment variables!")
        return
    
    # Create application
    application = Application.builder().token(token).post_init(post_init).build()
    
    # Add global error handler
    application.add_error_handler(error_handler)
    
    # Add handlers
    application.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("leave", leave_command))
    application.add_handler(CommandHandler("quit", quit_command))
    application.add_handler(CommandHandler("forcequit", forcequit_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("vote", vote_command))
    application.add_handler(CommandHandler("extend", extend_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler(["new4", "new5", "new6"], new_round_command))
    application.add_handler(CallbackQueryHandler(handle_game_menu_callback, pattern="^(game_|opt_)"))
    application.add_handler(CallbackQueryHandler(handle_settings_callback, pattern="^set_"))
    application.add_handler(CallbackQueryHandler(handle_leaderboard_callback, pattern="^lb_"))
    application.add_handler(CallbackQueryHandler(handle_vote_callback, pattern="^vote_"))
    application.add_handler(CallbackQueryHandler(handle_ts_callback, pattern="^ts_vote_"))
    application.add_handler(CallbackQueryHandler(handle_20q_callback, pattern="^view_secret_word$"))
    application.add_handler(CallbackQueryHandler(handle_c8_callback, pattern="^c8_"))
    application.add_handler(CallbackQueryHandler(handle_sfl_callback, pattern="^sfl_reveal$"))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(ChosenInlineResultHandler(chosen_inline_result_handler))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    application.add_handler(PollHandler(handle_poll))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # Add new song round command
    application.add_handler(CommandHandler("newsong", new_round_command))
    application.add_handler(CommandHandler("newriddle", new_round_command))
    application.add_handler(CommandHandler("skip", skip_command))
    application.add_handler(CommandHandler("flappy", flappy_command))
    application.add_handler(CommandHandler("minigames", minigames_command))
    
    # Add HTML5 game callback handler at the end of callback handlers
    application.add_handler(CallbackQueryHandler(minigames_callback_handler, pattern="^mg_play_"))
    application.add_handler(CallbackQueryHandler(html5_game_callback))
    



    # Catch managed bot events on group=1 to not interfere with standard updates
    application.add_handler(TypeHandler(Update, clone_bot_handler), group=1)
    
    # Start the bot
    logger.info("Bot starting...")

    # Re-spawn any clone bots that were running before the last restart
    # (only the master process does this — clones have DISABLE_FLASK=1 AND
    #  their own token, so they won't recursively re-spawn each other)
    if os.environ.get("DISABLE_FLASK") != "1":
        spawn_saved_clone_bots(token)

    # Set up Flask server for health checks and HTML5 games
    if os.environ.get("DISABLE_FLASK") != "1":
        from flask import request, jsonify, send_from_directory, render_template_string
        import base64
        
        html5_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html5-games")
        # Change static_url_path to avoid route collision with our custom /html5-games route
        app = Flask(__name__, static_folder=html5_dir, static_url_path="/html5-static")
        GAME_SECRET = os.environ.get("GAME_SECRET", "gamio-secret-123")

        @app.route('/')
        def health_check():
            return "Bot is running!", 200

        @app.route('/html5-games/<path:filename>')
        def serve_game(filename):
            if filename.endswith('.html'):
                # Injected variables for the game
                try:
                    with open(os.path.join(html5_dir, filename), 'r', encoding='utf-8') as f:
                        content = f.read()
                    # Simple template replacement
                    content = content.replace("{{ game_secret }}", GAME_SECRET)
                    return content
                except Exception as e:
                    logger.error(f"Error serving game template {filename}: {e}")
                    return "Game file not found", 404
            return send_from_directory(html5_dir, filename)

        @app.route('/api/get_leaderboard', methods=['POST'])
        def get_lb():
            from leaderboard import get_game_leaderboard
            lb, _, _ = get_game_leaderboard("html5", 1, 10)
            leaders = [{"name": name, "score": score} for _, name, score in lb]
            return jsonify({"ok": True, "leaderboard": leaders})

        @app.route('/api/set_score', methods=['POST'])
        def set_score():
            data = request.json
            if not data or 'payload' not in data:
                return jsonify({"ok": False, "error": "Missing payload"}), 400
            
            try:
                # Decrypt payload
                encrypted = base64.b64decode(data['payload']).decode('latin1')
                decrypted_json = rc4_decrypt(GAME_SECRET, encrypted)
                score_data = json.loads(decrypted_json)
                
                user_id = int(score_data.get('user_id'))
                score = int(score_data.get('score'))
                
                # Persistent save
                from leaderboard import record_game_scores
                global BOT_MAIN_LOOP
                if BOT_MAIN_LOOP is None:
                    return jsonify({"ok": False, "error": "Bot loop not ready"}), 500
                    
                asyncio.run_coroutine_threadsafe(
                    record_game_scores([(user_id, score)], "html5", 0, application),
                    loop=BOT_MAIN_LOOP
                )
                
                return jsonify({"ok": True, "score": score})
            except Exception as e:
                logger.error(f"Score submission error: {e}")
                return jsonify({"ok": False, "error": str(e)}), 500

        def run_flask():
            # Use PORT environment variable from Render, default to 8080
            port = int(os.environ.get("PORT", 8080))
            app.run(host="0.0.0.0", port=port)

        # Run Flask in a separate daemon thread
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info(f"Health check & Game server started on port {os.environ.get('PORT', 8080)}")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
