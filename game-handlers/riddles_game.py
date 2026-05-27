import random
import json
import os
from typing import List, Dict, Optional, Tuple


class RiddlesGame:
    def __init__(self, total_rounds: int = 10, endless: bool = False):
        self.total_rounds = total_rounds
        self.endless = endless
        self.current_round = 0
        self.scores: Dict[int, int] = {}
        self.current_riddle: Optional[dict] = None
        self.used_riddles: List[str] = []
        self.round_in_progress = False

        json_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "riddles", "riddles.json")
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                self.riddles = json.load(f)
        except Exception:
            self.riddles = [
                {"id": "0", "question": "What has a face and two hands but no arms or legs?", "answer": "Clock"},
                {"id": "1", "question": "What gets wetter the more it dries?", "answer": "Towel"},
            ]

    def add_player(self, user_id: int) -> None:
        if user_id not in self.scores:
            self.scores[user_id] = 0

    def remove_player(self, user_id: int) -> None:
        if user_id in self.scores:
            del self.scores[user_id]

    def start_new_round(self) -> Tuple[str, int]:
        self.current_round += 1
        self.round_in_progress = True

        available = [r for r in self.riddles if r["id"] not in self.used_riddles]
        if not available:
            self.used_riddles = []
            available = self.riddles

        self.current_riddle = random.choice(available)
        self.used_riddles.append(self.current_riddle["id"])

        return self.current_riddle["question"], self.current_round

    def check_answer(self, answer: str, user_id: int) -> bool:
        if not self.current_riddle or not self.round_in_progress:
            return False

        if answer.lower().strip() == self.current_riddle["answer"].lower():
            self.scores[user_id] = self.scores.get(user_id, 0) + 1
            self.last_answer = self.current_riddle["answer"]
            self.current_riddle = None
            self.round_in_progress = False
            return True
        return False

    def get_current_answer(self) -> Optional[str]:
        if self.current_riddle:
            return self.current_riddle["answer"]
        return getattr(self, 'last_answer', None)

    def is_game_over(self) -> bool:
        if self.endless:
            return False
        return self.current_round >= self.total_rounds

    def get_scoreboard(self) -> List[Tuple[int, int]]:
        return sorted(self.scores.items(), key=lambda x: x[1], reverse=True)

    def get_winners(self) -> List[int]:
        if not self.scores:
            return []
        scoreboard = self.get_scoreboard()
        if not scoreboard:
            return []
        highest = scoreboard[0][1]
        return [uid for uid, s in scoreboard if s == highest]

    def get_player_count(self) -> int:
        return len(self.scores)
