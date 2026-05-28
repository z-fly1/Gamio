import json
import os
import random
import re
import io
from typing import List, Dict, Optional, Tuple, Set
from PIL import Image, ImageDraw, ImageFont

class JeopardyGame:
    """Manages the Jeopardy game engine, including database clues, state tracking, and clue board image generation."""

    def __init__(self, used_categories: Optional[List[str]] = None, used_clues: Optional[List[str]] = None):
        self.used_categories = used_categories if used_categories is not None else []
        self.used_clues = used_clues if used_clues is not None else []
        self.scores: Dict[int, int] = {}  # user_id -> points
        self.players: Dict[int, str] = {}  # user_id -> display_name
        self.turn_order: List[int] = []
        self.current_turn_index = 0

        self.categories: List[str] = []
        self.clues_by_points: Dict[str, Dict[int, Dict]] = {}  # category -> points -> clue_dict
        self.answered_cells: Set[Tuple[str, int]] = set()  # (category, points)
        self.total_rounds = 16
        self.current_round = 0

        self.current_selected_clue: Optional[Dict] = None
        self.current_selected_category: Optional[str] = None
        self.current_selected_points: Optional[int] = None

        self._load_database()

    def _load_database(self) -> None:
        """Load categories and clues from the JSON file."""
        json_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "jeopardy", "jeopardy-list.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                all_cats = data.get("categories", [])
                
                # Filter categories and pick 4
                available_cats = [c for c in all_cats if c["category_name"] not in self.used_categories]
                if len(available_cats) < 4:
                    self.used_categories = []
                    available_cats = all_cats

                # Select 4 random categories
                chosen_cats_data = random.sample(available_cats, min(4, len(available_cats)))
                
                for cat_data in chosen_cats_data:
                    cat_name = cat_data["category_name"]
                    self.categories.append(cat_name)
                    self.used_categories.append(cat_name)
                    self.clues_by_points[cat_name] = {}
                    
                    # For points 2, 5, 10, 15
                    for pts in [2, 5, 10, 15]:
                        clues_for_pts = [clue for clue in cat_data["clues"] if clue["points"] == pts]
                        if not clues_for_pts:
                            # Fallback if specific points not found
                            clues_for_pts = cat_data["clues"]
                            
                        # Try to pick one that is not used yet
                        unused_clues = [clue for clue in clues_for_pts if clue["clue"] not in self.used_clues]
                        if not unused_clues:
                            unused_clues = clues_for_pts
                            
                        chosen_clue = random.choice(unused_clues)
                        self.used_clues.append(chosen_clue["clue"])
                        self.clues_by_points[cat_name][pts] = chosen_clue
        except Exception as e:
            # Fallback mock data in case of error
            self.categories = ["Geography", "Science", "History", "Literature"]
            for cat in self.categories:
                self.clues_by_points[cat] = {
                    2: {"clue": f"Mock 2 pt clue for {cat}", "answer": "What is Mock?"},
                    5: {"clue": f"Mock 5 pt clue for {cat}", "answer": "What is Mock?"},
                    10: {"clue": f"Mock 10 pt clue for {cat}", "answer": "What is Mock?"},
                    15: {"clue": f"Mock 15 pt clue for {cat}", "answer": "What is Mock?"}
                }

    def add_player(self, user_id: int, display_name: str = "Player") -> None:
        """Add a player to the game."""
        self.players[user_id] = display_name
        if user_id not in self.scores:
            self.scores[user_id] = 0
        if user_id not in self.turn_order:
            self.turn_order.append(user_id)

    def remove_player(self, user_id: int) -> None:
        """Remove a player from the game."""
        if user_id in self.players:
            del self.players[user_id]
        if user_id in self.scores:
            del self.scores[user_id]
        if user_id in self.turn_order:
            self.turn_order.remove(user_id)
            if self.current_turn_index >= len(self.turn_order) and self.turn_order:
                self.current_turn_index = 0

    def start_game(self) -> bool:
        """Initialize the turn order."""
        if not self.players:
            return False
        # Only initialize if turn_order is not set or empty
        if not getattr(self, '_started', False):
            self.turn_order = list(self.players.keys())
            random.shuffle(self.turn_order)
            self.current_turn_index = 0
            self._started = True
        return True

    def get_current_turn_player(self) -> Optional[int]:
        """Get the user ID of the player whose turn it is to select."""
        if not self.turn_order:
            return None
        return self.turn_order[self.current_turn_index]

    def select_clue(self, player_id: int, category_query: str, points: int) -> Optional[Dict]:
        """Allow a player whose turn it is to choose a category and point value."""
        if not self.turn_order or player_id != self.get_current_turn_player():
            return None
        
        # Match category name loosely
        matched_category = None
        # Try exact match first
        for cat in self.categories:
            if cat.lower().strip() == category_query.lower().strip():
                matched_category = cat
                break
                
        # Try substring match if no exact match
        if not matched_category:
            for cat in self.categories:
                if category_query.lower().strip() in cat.lower():
                    matched_category = cat
                    break
        
        if not matched_category:
            return None
            
        if points not in [2, 5, 10, 15]:
            return None
            
        if (matched_category, points) in self.answered_cells:
            return None

        self.current_selected_clue = self.clues_by_points[matched_category][points]
        self.current_selected_category = matched_category
        self.current_selected_points = points
        return self.current_selected_clue

    def _normalize(self, text: str) -> str:
        """Normalize answer text for robust comparisons."""
        text = text.lower().strip()
        if text.endswith('?'):
            text = text[:-1].strip()
        # Remove standard question templates and articles
        prefixes = ["what is", "who is", "what are", "who are", "is it", "the ", "a ", "an "]
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        # Strip all punctuation and non-alphanumeric chars
        text = re.sub(r'[^a-z0-9]', '', text)
        return text

    def check_answer(self, text: str, user_id: int) -> bool:
        """Check if the user's answer matches the expected answer."""
        if not self.current_selected_clue or user_id not in self.players:
            return False
            
        expected_answer = self.current_selected_clue["answer"]
        norm_guess = self._normalize(text)
        norm_expected = self._normalize(expected_answer)
        
        if norm_guess == norm_expected:
            # Correct answer! Add points and assign selection turn to this user
            points = self.current_selected_points or 2
            self.scores[user_id] = self.scores.get(user_id, 0) + points
            
            # Update turn order so the correct answerer selects next
            if user_id in self.turn_order:
                self.current_turn_index = self.turn_order.index(user_id)
                
            # Mark cell as answered
            if self.current_selected_category and self.current_selected_points:
                self.answered_cells.add((self.current_selected_category, self.current_selected_points))
                
            self.current_round = len(self.answered_cells)
            self.current_selected_clue = None
            self.current_selected_category = None
            self.current_selected_points = None
            return True
            
        return False

    def resolve_unanswered_clue(self) -> None:
        """Resolve a clue that nobody got right or was timed out."""
        if self.current_selected_category and self.current_selected_points:
            self.answered_cells.add((self.current_selected_category, self.current_selected_points))
        self.current_round = len(self.answered_cells)
        self.current_selected_clue = None
        self.current_selected_category = None
        self.current_selected_points = None
        
        # Turn stays with current player, or if they left, reset to next available
        if self.turn_order and self.current_turn_index >= len(self.turn_order):
            self.current_turn_index = 0

    def is_game_over(self) -> bool:
        """Game is over when all 16 board cells have been resolved."""
        return len(self.answered_cells) >= 16

    def get_scoreboard(self) -> List[Tuple[int, int]]:
        """Return sorted scores (user_id, score)."""
        return sorted(self.scores.items(), key=lambda x: x[1], reverse=True)

    def get_winners(self) -> List[int]:
        """Return list of user_ids with the highest score."""
        if not self.scores:
            return []
        scoreboard = self.get_scoreboard()
        highest = scoreboard[0][1]
        return [uid for uid, s in scoreboard if s == highest]

    def get_board_image(self) -> Optional[io.BytesIO]:
        """Generate the dynamic clue board PNG using Pillow with robust error handling."""
        try:
            img_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "jeopardy", "clue-card-template.png")
            font_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "jeopardy", "impact.ttf")
            
            try:
                img = Image.open(img_path).convert("RGB")
            except Exception:
                return None
                
            draw = ImageDraw.Draw(img)
            
            # Coordinates mapping for the 4 columns
            coords = {
                0: {
                    "title": (160.0, 110.9, 576.0, 285.9),
                    2: (155.7, 381.9, 576.0, 492.8),
                    5: (155.7, 550.4, 571.7, 663.5),
                    10: (155.7, 721.1, 576.0, 834.1),
                    15: (155.7, 889.6, 573.9, 1002.7)
                },
                1: {
                    "title": (761.6, 108.8, 1179.7, 285.9),
                    2: (761.6, 381.9, 1179.7, 494.9),
                    5: (759.5, 552.5, 1179.7, 663.5),
                    10: (761.6, 718.9, 1177.6, 834.1),
                    15: (761.6, 889.6, 1179.7, 1004.8)
                },
                2: {
                    "title": (1365.3, 108.8, 1785.6, 283.7),
                    2: (1365.3, 379.7, 1785.6, 492.8),
                    5: (1365.3, 546.1, 1783.5, 663.5),
                    10: (1365.3, 718.9, 1785.6, 834.1),
                    15: (1365.3, 889.6, 1785.6, 1004.8)
                },
                3: {
                    "title": (1984.0, 108.8, 2402.1, 281.6),
                    2: (1984.0, 379.7, 2404.3, 494.9),
                    5: (1984.0, 548.3, 2402.1, 663.5),
                    10: (1984.0, 716.8, 2402.1, 834.1),
                    15: (1984.0, 889.6, 2404.3, 1002.7)
                }
            }
            
            for col_idx, cat in enumerate(self.categories):
                col_coords = coords[col_idx]
                
                # Draw Title
                self._wrap_and_draw_title(draw, cat, col_coords["title"], font_path)
                
                # Draw Point values
                for pts in [2, 5, 10, 15]:
                    if (cat, pts) in self.answered_cells:
                        # Hide already answered clues
                        continue
                    self._draw_centered_point(draw, str(pts), col_coords[pts], font_path)
                    
            output = io.BytesIO()
            img.save(output, format="PNG")
            output.seek(0)
            return output
        except Exception:
            return None

    def _wrap_and_draw_title(self, draw, text: str, box: Tuple[float, float, float, float], font_path: str) -> None:
        x1, y1, x2, y2 = box
        box_width = x2 - x1
        box_height = y2 - y1
        
        font_size = 50
        while font_size >= 15:
            try:
                font = ImageFont.truetype(font_path, font_size)
            except Exception:
                font = ImageFont.load_default()
                
            words = text.split(" ")
            lines = []
            current_line = []
            
            for word in words:
                test_line = " ".join(current_line + [word])
                bbox = draw.textbbox((0, 0), test_line, font=font)
                w = bbox[2] - bbox[0]
                
                if w <= box_width:
                    current_line.append(word)
                else:
                    if current_line:
                        lines.append(" ".join(current_line))
                        current_line = [word]
                    else:
                        lines.append(word)
                        current_line = []
            if current_line:
                lines.append(" ".join(current_line))
                
            if len(lines) > 2:
                font_size -= 2
                continue
                
            line_heights = []
            line_widths = []
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                lw = bbox[2] - bbox[0]
                lh = bbox[3] - bbox[1]
                line_widths.append(lw)
                line_heights.append(lh)
                
            spacing = 5
            total_height = sum(line_heights) + spacing * (len(lines) - 1)
            
            if total_height <= box_height and all(lw <= box_width for lw in line_widths):
                current_y = y1 + (box_height - total_height) / 2
                for idx, line in enumerate(lines):
                    lw = line_widths[idx]
                    lh = line_heights[idx]
                    cx = x1 + (box_width - lw) / 2
                    draw.text((cx, current_y), line, fill="#ffe14d", font=font)
                    current_y += lh + spacing
                return
            else:
                font_size -= 2
                
        # Fallback
        try:
            font = ImageFont.truetype(font_path, 15)
        except Exception:
            font = ImageFont.load_default()
        draw.text((x1 + 10, y1 + 10), text[:20], fill="#ffe14d", font=font)

    def _draw_centered_point(self, draw, text: str, box: Tuple[float, float, float, float], font_path: str) -> None:
        x1, y1, x2, y2 = box
        box_width = x2 - x1
        box_height = y2 - y1
        
        font_size = 70
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = ImageFont.load_default()
            
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        
        cx = x1 + (box_width - w) / 2
        cy = y1 + (box_height - h) / 2
        draw.text((cx, cy), text, fill="#ffe14d", font=font)
