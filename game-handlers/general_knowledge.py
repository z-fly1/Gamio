import json
import os
import random
import re
from typing import List, Dict, Optional, Union, Tuple


class GeneralKnowledgeGame:
    """Manages a general knowledge trivia game with multiple rounds."""

    def __init__(self, total_rounds: int = 15, game_type: str = "Answer", time_limit: int = 30):
        self.total_rounds = total_rounds
        self.game_type = game_type
        self.time_limit = time_limit
        self.current_round = 0
        self.scores: Dict[int, int] = {}
        self.questions: List[Dict] = []
        self.mcq_questions: List[Dict] = []
        self.used_question_indices: List[int] = []
        self.used_mcq_indices: List[int] = []
        self.current_question: Optional[Dict] = None
        self.players: Dict[int, str] = {}
        self.round_in_progress: bool = False

        self._load_questions()
        self._load_mcq_questions()

    def _load_questions(self):
        json_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "general knowledge", "questions.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                self.questions = json.load(f)
        except Exception as e:
            print(f"Error loading general knowledge questions: {e}")
            self.questions = []

    def _load_mcq_questions(self):
        json_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "general knowledge", "mcq-questions.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                self.mcq_questions = json.load(f)
        except Exception as e:
            print(f"Error loading MCQ questions: {e}")
            self.mcq_questions = []

    def add_player(self, user_id: int, display_name: str = "Player") -> None:
        self.players[user_id] = display_name
        if user_id not in self.scores:
            self.scores[user_id] = 0

    def remove_player(self, user_id: int) -> None:
        if user_id in self.players:
            del self.players[user_id]
        if user_id in self.scores:
            del self.scores[user_id]

    def _normalize(self, text: str) -> str:
        if not text:
            return ""
        return re.sub(r'[^a-z0-9]', '', text.lower())

    def start_new_round(self) -> Tuple[str, int]:
        self.current_round += 1

        if self.game_type == "MCQ":
            return self._start_mcq_round()
        return self._start_answer_round()

    def _start_answer_round(self) -> Tuple[str, int]:
        if not self.questions:
            return "No questions available.", self.current_round

        if len(self.used_question_indices) >= len(self.questions):
            self.used_question_indices = []

        available_indices = [i for i in range(len(self.questions)) if i not in self.used_question_indices]
        idx = random.choice(available_indices)
        self.used_question_indices.append(idx)

        self.current_question = self.questions[idx]
        self.round_in_progress = True
        return self.current_question["question"], self.current_round

    def _start_mcq_round(self) -> Tuple[str, int]:
        if not self.mcq_questions:
            return "No MCQ questions available.", self.current_round

        if len(self.used_mcq_indices) >= len(self.mcq_questions):
            self.used_mcq_indices = []

        available_indices = [i for i in range(len(self.mcq_questions)) if i not in self.used_mcq_indices]
        idx = random.choice(available_indices)
        self.used_mcq_indices.append(idx)

        self.current_question = self.mcq_questions[idx]
        self.round_in_progress = True
        return self.current_question["question"], self.current_round

    def get_mcq_options(self) -> Tuple[List[str], int]:
        if not self.current_question:
            return [], 0

        labels = ["A", "B", "C", "D"]
        options = []
        correct_id = 0
        for i, label in enumerate(labels):
            if label in self.current_question:
                options.append(self.current_question[label])
                if label == self.current_question["answer"]:
                    correct_id = i

        return options, correct_id

    def check_answer(self, user_id: int, answer: str) -> bool:
        if not self.current_question or user_id not in self.players:
            return False

        correct_answer = self.current_question["answer"]
        norm_user_answer = self._normalize(answer)

        if isinstance(correct_answer, list):
            for valid in correct_answer:
                if norm_user_answer == self._normalize(valid):
                    self.scores[user_id] += 1
                    self.round_in_progress = False
                    return True
        else:
            if norm_user_answer == self._normalize(correct_answer):
                self.scores[user_id] += 1
                self.round_in_progress = False
                return True

        return False

    def answer_mcq(self, user_id: int, selected_option_id: int) -> bool:
        if not self.current_question or user_id not in self.players or not self.round_in_progress:
            return False

        _, correct_id = self.get_mcq_options()
        if selected_option_id == correct_id:
            self.scores[user_id] += 1
            return True
        return False

    def get_current_answer(self) -> str:
        if not self.current_question:
            return ""

        if self.game_type == "MCQ":
            ans_letter = self.current_question["answer"]
            return self.current_question.get(ans_letter, ans_letter)

        ans = self.current_question["answer"]
        if isinstance(ans, list):
            return " or ".join(ans)
        return ans

    def is_game_over(self) -> bool:
        return self.current_round >= self.total_rounds

    def get_scoreboard(self) -> List[Tuple[int, int]]:
        return sorted(self.scores.items(), key=lambda x: x[1], reverse=True)

    def get_winners(self) -> List[int]:
        if not self.scores:
            return []

        scoreboard = self.get_scoreboard()
        if not scoreboard:
            return []

        highest_score = scoreboard[0][1]
        return [user_id for user_id, score in scoreboard if score == highest_score]

    def get_player_count(self) -> int:
        return len(self.players)
