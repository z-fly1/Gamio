
![Logo](https://i.ibb.co/S4P22vBK/smwhed.png)
# Gamio - Host Games In Telegram
![Version](https://img.shields.io/badge/version-1.0-blue) 
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://choosealicense.com/licenses/mit/)
![Views](https://visitor-badge.laobi.icu/badge?page_id=z-fly1.Gamio)


Powerful Telegram bot designed to host and manage games in group chats.


## Features


- **20+ Group Games:** Play a variety of text-based games directly inside Telegram groups.
- **HTML5 Web App Games:**
    Play Interactive Telegram HTML5 Games.
- **Bot Cloning:**
    Create your own instance of Gamio and host games in your own groups

## Tech Stack

[![Python](https://img.shields.io/badge/Python-3.x-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-framework-black)](https://flask.palletsprojects.com/)
[![Pillow](https://img.shields.io/badge/Pillow-image%20processing-orange)](https://python-pillow.org/)
[![NumPy](https://img.shields.io/badge/NumPy-numerical-blue)](https://numpy.org/)
[![Google Generative AI](https://img.shields.io/badge/Google%20Generative%20AI-Gemini-red)](https://ai.google.dev/)
[![Supabase](https://img.shields.io/badge/Supabase-backend-green)](https://supabase.com/)

## Game Library


- **Word Unscramble**: Rearrange a provided set of scrambled letters into a valid word.
- **Word Connect**: Identify and submit multiple valid words derived from a specific set of letters.
- **Story Builder**: Users take turns appending single sentences to a shared text block to make a story.

- **Guess the Logo**: Identify a brand or corporation based on a provided logo image.
- **GuessMoji**: Decode a sequence of emojis to identify a specific movie, song, idiom, or place.
- **Guess the Movie**: Identify a film title using movie posters.
- **Guess the Character**: Identify a character based on a provided cutout of their image.
- **Guess the Book**: Identify a book title from its cover image.
- **Guess the Marvel Character**: Identify character names from the Marvel franchise based on their images.
- **Guess Addis**: Identify locations within Addis Ababa using a sequence of emojis as a clue.
- **Name the Player**: Identify and name a professional football player from their photograph.
- **Movie Scene**: Identify a film based on a single frame from a specific scene.

- **General Knowledge**: Respond to questions across various academic and cultural topics.
- **Taylor Swift Or Shakespeare**: Categorize a provided text quote as either a song lyric by Taylor Swift or a literary line by Shakespeare.
- **Guess the Flag**: Identify a country name based on its national flag image.

- **Guess the Song**: Identify a song title using intros of the audio clips.
- **What You Meme**: Submit a meme response that corresponds to a provided scenario.

- **Guess the Imposter**: Identify the single player assigned a different secret word than the rest of the group.
- **20 Questions**: Guess a hidden word by asking up to 20 binary (yes/no) questions to the game host.
- **Hear Me Out**: Submit character images to be "added" to a group hear me out cake.
- **Who Am I**: Guess an assigned character name through interaction with other players.

- **UNO**: A fully-featured digital version of the world-famous card game.
- **Crazy 8**: Match ranks or suits to clear your hand in this classic strategic card game.

## Contributing

Gamio uses a **modular game-handler architecture**, making it easy to add new games.

Want to contribute? See the [Contribution Guide](CONTRIBUTING.md).
## Installation

### Prerequisites
- Python 3.9 or higher
- A Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- Supabase Project (URL and API Key)

### Local Setup
1. **Clone the repository**:
   ```bash
   git clone https://github.com/z-fly1/Gamio.git
   cd Gamio
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment**:
   Create a `.env` file in the root directory:
   ```env
   BOT_TOKEN=your_telegram_bot_token
   SUPABASE_URL=your_supabase_url
   SUPABASE_KEY=your_supabase_key
   MASTER_BOT_USERNAME=your_bot_username
   ```

4. **Run the bot**:
   ```bash
   python main.py
   ```


    
###  Project Structure

```text
Gamio/
├── game-handlers/      # Core logic for individual games
├── assets/             # Images, fonts, and game covers
├── game_manager.py     # State machine for game sessions
├── leaderboard.py      # Score tracking and ranking logic
├── main.py             # Entry point & bot cloning system
├── requirements.txt    # Project dependencies
└── settings_manager.py # Group-specific configuration
```

## License

[MIT](https://choosealicense.com/licenses/mit/)


---

> *“Man is most nearly himself when he achieves the seriousness of a child at play.”*  — *Heraclitus*
