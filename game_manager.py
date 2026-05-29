import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'game-handlers'))

from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Set, Union, List
from word_unscramble import WordUnscrambleGame
from story_builder import StoryBuilderGame
from guess_the_imposter import GuessTheImposterGame
from guess_the_logo import GuessTheLogoGame
from guessmoji import GuessMojiGame
from guess_the_movie import GuessTheMovieGame
from guess_the_flag import GuessTheFlagGame
from general_knowledge import GeneralKnowledgeGame
from guess_character import GuessCharacterGame
from word_connect import WordConnectGame
from wdym_game import MemeGame
from taylor_shakespeare import TaylorShakespeareGame
from twenty_questions import TwentyQuestionsGame
from guess_the_song import GuessTheSongGame
from crazy_eight import Crazy8Game
from guess_the_book import GuessTheBookGame
from guess_the_marvel import GuessMarvelGame
from guess_addis import GuessAddisGame
from hear_me_out import HearMeOutGame
from name_the_player import NameThePlayerGame
from movie_scene import MovieSceneGame
from who_am_i import WhoAmIGame
from song_from_lyrics import SongFromLyricsGame
from riddles_game import RiddlesGame
from jeopardy import JeopardyGame




class GameState(Enum):
    """Possible states for a game session."""
    WAITING_FOR_GAME_CODE = "waiting_for_game_code"
    JOINING = "joining"
    IN_PROGRESS = "in_progress"
    ENDED = "ended"


class GameSession:
    """Represents a single game session in a group."""
    
    def __init__(self, chat_id: int):
        """Initialize a new game session.
        
        Args:
            chat_id: Telegram chat ID
        """
        self.chat_id = chat_id
        self.state = GameState.WAITING_FOR_GAME_CODE
        self.game_code: Optional[str] = None
        self.players: Set[int] = set()  # Set of user IDs
        self.game: Optional[Union[WordUnscrambleGame, StoryBuilderGame, GuessTheImposterGame, GuessTheLogoGame, GuessMojiGame, GuessTheMovieGame, GuessTheFlagGame, GeneralKnowledgeGame, GuessCharacterGame, WordConnectGame, MemeGame, TaylorShakespeareGame, TwentyQuestionsGame, GuessTheSongGame, Crazy8Game, GuessTheBookGame, GuessMarvelGame, GuessAddisGame, HearMeOutGame, NameThePlayerGame, MovieSceneGame, WhoAmIGame, SongFromLyricsGame, RiddlesGame, JeopardyGame]] = None

        self.joining_deadline: Optional[datetime] = None
        
        # New fields for restricted controls and voting
        self.initiator_id: Optional[int] = None
        self.quit_votes: Set[int] = set()  # user_ids who voted to quit
        self.quit_vote_message_id: Optional[int] = None
        self.joining_message_id: Optional[int] = None
        
    def set_game_code(self, code: str, used_images: Optional[List[str]] = None) -> bool:
        """Set the game code and initialize the appropriate game.
        
        Args:
            code: Game code (e.g., "1" for word unscramble)
            used_images: Optional list of already used images (for Guess Addis)
            
        Returns:
            True if game code is valid, False otherwise
        """
        if code == "1":
            self.game_code = code
            self.game = WordUnscrambleGame(total_rounds=10)
            self.state = GameState.JOINING
            return True
        elif code == "2":
            self.game_code = code
            self.game = StoryBuilderGame(rounds_per_player=2)
            self.state = GameState.JOINING
            return True
        elif code == "3":
            self.game_code = code
            self.game = GuessTheImposterGame()
            self.state = GameState.JOINING
            return True
        elif code == "4":
            self.game_code = code
            self.game = GuessTheLogoGame(rounds_limit=15)
            self.state = GameState.JOINING
            return True
        elif code == "5":
            self.game_code = code
            self.game = GuessMojiGame(total_rounds=20)
            self.state = GameState.JOINING
            return True
        elif code == "6":
            self.game_code = code
            self.game = GuessTheMovieGame(rounds_limit=15)
            self.state = GameState.JOINING
            return True
        elif code == "7":
            self.game_code = code
            self.game = GuessTheFlagGame(rounds_limit=15)
            self.state = GameState.JOINING
            return True
        elif code == "9":
            self.game_code = code
            self.game = GeneralKnowledgeGame(total_rounds=15, game_type="Answer")
            self.state = GameState.JOINING
            return True
        elif code == "10":
            self.game_code = code
            self.game = GuessCharacterGame(rounds_limit=15)
            self.state = GameState.JOINING
            return True
        elif code == "11":
            self.game_code = code
            self.game = WordConnectGame(rounds_limit=10)
            self.state = GameState.JOINING
            return True
        elif code == "12":
            self.game_code = code
            self.game = MemeGame(rounds_limit=10)
            self.state = GameState.JOINING
            return True
        elif code == "13":
            self.game_code = code
            self.game = TaylorShakespeareGame(rounds=10)
            self.state = GameState.JOINING
            return True
        elif code == "15":
            self.game_code = code
            self.game = TwentyQuestionsGame(rounds_limit=10)
            self.state = GameState.JOINING
            return True
        elif code == "16":
            self.game_code = code
            self.game = GuessTheSongGame(total_rounds=15)
            self.state = GameState.JOINING
            return True
        elif code == "17":
            self.game_code = code
            self.game = Crazy8Game()
            self.state = GameState.JOINING
            return True
        elif code == "18":
            self.game_code = code
            self.game = GuessTheBookGame(rounds_limit=15)
            self.state = GameState.JOINING
            return True
        elif code == "19":
            self.game_code = code
            self.game = GuessMarvelGame(rounds_limit=15)
            self.state = GameState.JOINING
            return True
        elif code == "20":
            self.game_code = code
            self.game = GuessAddisGame(rounds_limit=15, used_images=used_images)
            self.state = GameState.JOINING
            return True
        elif code == "21":
            self.game_code = code
            self.game = HearMeOutGame(chat_id=self.chat_id)
            self.state = GameState.JOINING
            return True
        elif code == "22":
            self.game_code = code
            self.game = NameThePlayerGame(rounds_limit=15, used_images=used_images)
            self.state = GameState.JOINING
            return True
        elif code == "23":
            self.game_code = code
            self.game = MovieSceneGame(rounds_limit=15, used_images=used_images)
            self.state = GameState.JOINING
            return True

        elif code == "25":
            self.game_code = code
            self.game = WhoAmIGame()
            self.state = GameState.JOINING
            return True

        elif code == "26":
            self.game_code = code
            self.game = SongFromLyricsGame(total_rounds=10)
            self.state = GameState.JOINING
            return True

        elif code == "27":
            self.game_code = code
            self.game = RiddlesGame(total_rounds=10)
            self.state = GameState.JOINING
            return True

        elif code == "28":
            self.game_code = code
            used_cats = None
            used_cls = None
            if isinstance(used_images, dict):
                used_cats = used_images.get("seen_categories")
                used_cls = used_images.get("seen_clues")
            self.game = JeopardyGame(used_categories=used_cats, used_clues=used_cls)
            self.state = GameState.JOINING
            return True

        return False
    
    def add_player(self, user_id: int, username: Optional[str] = None) -> bool:
        """Add a player to the game session.
        
        Args:
            user_id: Telegram user ID
            username: Telegram username (optional)
            
        Returns:
            True if player was added, False if already in game
        """
        if user_id in self.players:
            return False
        
        self.players.add(user_id)
        if self.game:
            # Handle different game signatures
            if isinstance(self.game, (StoryBuilderGame, GuessTheImposterGame, GuessTheLogoGame, GuessTheMovieGame, GuessTheFlagGame, GeneralKnowledgeGame, GuessCharacterGame, WordConnectGame, MemeGame, TaylorShakespeareGame, TwentyQuestionsGame, GuessTheSongGame, Crazy8Game, GuessTheBookGame, GuessMarvelGame, GuessAddisGame, HearMeOutGame, NameThePlayerGame, MovieSceneGame, WhoAmIGame, SongFromLyricsGame, JeopardyGame)):

                display_name = username or "Player"
                self.game.add_player(user_id, display_name)
            else:
                self.game.add_player(user_id)
        return True

    def remove_player(self, user_id: int) -> bool:
        """Remove a player from the game session.
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            True if player was removed, False if not in game
        """
        if user_id not in self.players:
            return False
            
        self.players.remove(user_id)
        if self.game:
            self.game.remove_player(user_id)
        return True
    
    def start_game(self) -> bool:
        """Start the game if enough players have joined.
        
        Returns:
            True if game started, False if not enough players
        """
        min_players = 1 if self.game_code in ["9", "27"] else 2
        if len(self.players) < min_players:
            return False
        
        self.state = GameState.IN_PROGRESS
        return True
    
    def get_player_count(self) -> int:
        """Get the number of players in the session."""
        return len(self.players)
    
    def end_game(self) -> None:
        """Mark the game as ended."""
        self.state = GameState.ENDED


class GameManager:
    """Manages all active game sessions across different chats."""
    
    def __init__(self):
        """Initialize the game manager."""
        self.active_games: Dict[int, GameSession] = {}  # chat_id -> GameSession
    
    def create_game(self, chat_id: int) -> GameSession:
        """Create a new game session for a chat.
        
        Args:
            chat_id: Telegram chat ID
            
        Returns:
            The newly created GameSession
        """
        session = GameSession(chat_id)
        self.active_games[chat_id] = session
        return session
    
    def get_game(self, chat_id: int) -> Optional[GameSession]:
        """Get the active game session for a chat.
        
        Args:
            chat_id: Telegram chat ID
            
        Returns:
            GameSession if one exists, None otherwise
        """
        return self.active_games.get(chat_id)
    
    def has_active_game(self, chat_id: int) -> bool:
        """Check if a chat has an active game.
        
        Args:
            chat_id: Telegram chat ID
            
        Returns:
            True if there's an active game, False otherwise
        """
        return chat_id in self.active_games
    
    def remove_game(self, chat_id: int) -> None:
        """Remove a game session.
        
        Args:
            chat_id: Telegram chat ID
        """
        if chat_id in self.active_games:
            del self.active_games[chat_id]
