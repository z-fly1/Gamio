"""
Leaderboard module for persisting and querying player scores across games.
Stores data in leaderboard_data.json.
"""
import os
import json
import logging
import asyncio
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard_data.json")

# Lock to prevent concurrent writes
_file_lock = asyncio.Lock()

# Game code -> human-readable name mapping
GAME_CODE_NAMES: Dict[str, str] = {
    "1": "Word Unscramble",
    "2": "Story Builder",
    "3": "Guess the Imposter",
    "4": "Guess the Logo",
    "5": "GuessMoji",
    "6": "Guess the Movie",
    "7": "Guess the Flag",
    "9": "General Knowledge",
    "10": "Guess the Character",
    "11": "Word Connect",
    "12": "What You Meme",
    "13": "Taylor vs Shakespeare",
    "15": "20 Questions",
    "16": "Guess the Song",
    "17": "Crazy 8",
    "18": "Guess the Book",
    "19": "Guess the Marvel",
    "24": "UNO",
    "26": "Song From Lyrics",
    "27": "Riddles",
}

# Games that don't use a standard scoreboard (skip recording)
SKIP_GAME_CODES = {"2"}  # Story Builder has no scores


def load_leaderboard() -> dict:
    """Load leaderboard data from JSON file."""
    if not os.path.exists(DATA_FILE):
        return {"players": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading leaderboard data: {e}")
        return {"players": {}}


def save_leaderboard(data: dict) -> None:
    """Save leaderboard data to JSON file."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except IOError as e:
        logger.error(f"Error saving leaderboard data: {e}")


async def record_game_scores(
    scoreboard: List[Tuple[int, int]],
    game_code: str,
    chat_id: int,
    context,
) -> None:
    """
    Record scores from a completed game into the persistent leaderboard.

    Args:
        scoreboard: List of (user_id, score) tuples from get_scoreboard()
        game_code: The game code string (e.g. "1", "4")
        chat_id: The Telegram chat ID (used to resolve usernames)
        context: Telegram callback context (for bot.get_chat_member)
    """
    if game_code in SKIP_GAME_CODES:
        return

    game_name = GAME_CODE_NAMES.get(game_code, f"Game {game_code}")

    async with _file_lock:
        data = load_leaderboard()
        players = data.setdefault("players", {})

        for user_id, score in scoreboard:
            if score <= 0:
                continue

            uid_str = str(user_id)

            # Resolve username
            username = "Player"
            try:
                member = await context.bot.get_chat_member(chat_id, user_id)
                username = member.user.first_name or member.user.username or "Player"
            except Exception as e:
                logger.warning(f"Could not resolve username for {user_id}: {e}")

            if uid_str not in players:
                players[uid_str] = {"username": username, "games": {}, "total": 0}
            else:
                # Update username in case it changed
                players[uid_str]["username"] = username

            player = players[uid_str]
            player["games"][game_name] = player["games"].get(game_name, 0) + score
            player["total"] = player.get("total", 0) + score

        save_leaderboard(data)


def get_total_leaderboard(page: int = 1, page_size: int = 10) -> Tuple[List[Tuple[str, str, int]], int, int]:
    """
    Get paginated total leaderboard.

    Returns:
        Tuple of (entries, current_page, total_pages)
        entries = list of (user_id_str, username, total_score)
    """
    data = load_leaderboard()
    players = data.get("players", {})

    # Sort by total score descending
    sorted_players = sorted(players.items(), key=lambda x: x[1].get("total", 0), reverse=True)

    total_pages = max(1, (len(sorted_players) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    page_entries = sorted_players[start:end]

    entries = [
        (uid, info.get("username", "Player"), info.get("total", 0))
        for uid, info in page_entries
    ]

    return entries, page, total_pages


def get_game_leaderboard(game_name: str, page: int = 1, page_size: int = 10) -> Tuple[List[Tuple[str, str, int]], int, int]:
    """
    Get paginated leaderboard filtered by a specific game.

    Returns:
        Tuple of (entries, current_page, total_pages)
        entries = list of (user_id_str, username, game_score)
    """
    data = load_leaderboard()
    players = data.get("players", {})

    # Filter players who have scores in this game
    game_players = []
    for uid, info in players.items():
        game_score = info.get("games", {}).get(game_name, 0)
        if game_score > 0:
            game_players.append((uid, info.get("username", "Player"), game_score))

    # Sort by game score descending
    game_players.sort(key=lambda x: x[2], reverse=True)

    total_pages = max(1, (len(game_players) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size

    return game_players[start:end], page, total_pages


def get_game_names() -> List[str]:
    """Get list of all game names that have recorded scores."""
    data = load_leaderboard()
    players = data.get("players", {})

    games = set()
    for info in players.values():
        for game_name, score in info.get("games", {}).items():
            if score > 0:
                games.add(game_name)

    return sorted(games)
