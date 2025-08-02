# Jukeborgee Discord Bot

Aggressive, sarcastic Discord bot with music playback, games, and AI chat functionality.

## Features

**Music**
- YouTube/Spotify playlist support
- DRM bypass with fallback methods
- Queue management with loop/shuffle
- Auto-disconnect when voice channels empty

**Games**
- Russian roulette, rock paper scissors, dice rolling
- 8-ball/7-ball, coin flip, fortune cookies
- Rating system, text transformation
- Roast generator

**AI Chat**
- Local LLM integration via Ollama
- Personality-based responses (short, sarcastic, Polish)
- Conversation history per user
- Prompt injection protection

## Setup

**Environment Variables**
```
BOT_TOKEN=your_discord_bot_token
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_REFRESH_TOKEN=your_spotify_refresh_token
YOUTUBE_API_KEY=your_youtube_api_key
LLAMA_API_URL=http://localhost:11434/api/generate
```

**Run Bot**
```bash
pip install -r requirements.txt
python jukeborgee.py
```

**Run LLM Server**
```bash
docker build -t llama-server .
docker run --gpus all -p 8080:8080 -v ./models:/app/models llama-server
```

## Commands

**Music**
- `!play <url>` - Play Spotify/YouTube
- `!queue` - Show current queue
- `!skip` - Skip current song
- `!loop` - Toggle queue loop

**Games**
- `!roulette` - Russian roulette
- `!rate <thing>` - Rate something /10
- `!roastme` - Get roasted

**AI**
- `!chat <message>` - Chat with AI
- `!enable_ai` - Toggle AI (sol/solkitsune only)

## Dependencies

- discord.py - Discord API
- yt-dlp - YouTube download
- spotipy - Spotify API
- aiohttp - HTTP requests
- FFmpeg - Audio processing

## Notes

- Requires CUDA-compatible GPU for local LLM
- Uses dolphin-mistral:7b model
- Temp audio files auto-cleanup
- DRM detection with alternative searching
