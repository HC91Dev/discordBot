import discord
from discord.ext import commands
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import asyncio
import os
import random
from urllib.parse import urlparse, parse_qs
import re
import requests
import base64
from dotenv import load_dotenv
import logging
from datetime import datetime, timedelta
import time
import tempfile
import shutil
import googleapiclient.discovery
from googleapiclient.errors import HttpError
from collections import defaultdict
from ai_chat_bot import AIChatBot
import json

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Load responses from JSON
with open('responses.json', 'r', encoding='utf-8') as f:
    RESPONSES = json.load(f)

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
SPOTIFY_REFRESH_TOKEN = os.getenv('SPOTIFY_REFRESH_TOKEN')
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
AI_MODEL_PATH = os.getenv('AI_MODEL_PATH', './models/llama-2-7b-chat.Q4_K_M.gguf')

# Spotify setup
def get_spotify_client():
    try:
        auth_headers = {
            'Authorization': f'Basic {base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()}'
        }
        
        token_data = {
            'grant_type': 'refresh_token',
            'refresh_token': SPOTIFY_REFRESH_TOKEN,
        }
        
        response = requests.post('https://accounts.spotify.com/api/token', 
                               data=token_data, 
                               headers=auth_headers)
        token_info = response.json()
        
        if 'access_token' not in token_info:
            logger.error("Failed to refresh Spotify token")
            return None
            
        return spotipy.Spotify(auth=token_info['access_token'])
    except Exception as e:
        logger.error(f"Error setting up Spotify client: {e}")
        return None

# YT-DLP options
ydl_opts = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'ignoreerrors': True,
    'skip_download': False,
    'continue_dl': True,
    'ignore_no_formats_error': True,
    'ignore_config': True,
    'geo_bypass': True,
    'geo_bypass_country': 'US',
    'default_search': 'ytsearch',
    'extract_flat': 'in_playlist',
    'playlistend': 50,
    'noplaylist': False,
    'postprocessor_args': ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5']
}

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = {}
        self.voice_clients = {}
        self.error_logs = {}
        self.error_threshold = 3
        self.loop = {}
        self.command_channels = {}  # Track where commands are issued from
        self.temp_dir = os.path.join(os.getcwd(), 'temp_audio')
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # Initialize YouTube API client
        if YOUTUBE_API_KEY:
            self.youtube_api_available = True
            self.youtube = googleapiclient.discovery.build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
        else:
            self.youtube_api_available = False
            logger.warning("YouTube API key not found. Using fallback methods only.")
        
        # Start temp cleanup task
        self.cleanup_task = bot.loop.create_task(self.cleanup_temp_files())
    
    def is_drm_error(self, error_message):
        """Check if an error message indicates DRM protection"""
        drm_indicators = ["drm", "protection", "protected", "content protection", 
                          "this site is known to use drm"]
        error_lower = error_message.lower()
        return any(indicator in error_lower for indicator in drm_indicators)
    
    def cog_unload(self):
        """Clean up when cog is unloaded"""
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()
        
        try:
            if os.path.exists(self.temp_dir):
                for file_name in os.listdir(self.temp_dir):
                    file_path = os.path.join(self.temp_dir, file_name)
                    if os.path.isfile(file_path):
                        try:
                            os.remove(file_path)
                        except:
                            pass
        except:
            pass
    
    async def cleanup_temp_files(self):
        """Periodically clean up temp files that aren't in queue"""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                if os.path.exists(self.temp_dir):
                    temp_files = {os.path.join(self.temp_dir, f) for f in os.listdir(self.temp_dir) 
                                 if os.path.isfile(os.path.join(self.temp_dir, f))}
                    
                    files_in_queue = set()
                    for guild_id in self.queue:
                        for url, _ in self.queue[guild_id]:
                            if url.startswith('file://'):
                                files_in_queue.add(url[7:])
                    
                    files_to_delete = temp_files - files_in_queue
                    
                    current_time = time.time()
                    for file_path in temp_files:
                        if os.path.exists(file_path):
                            file_age = current_time - os.path.getmtime(file_path)
                            if file_age > 7200:
                                files_to_delete.add(file_path)
                    
                    for file_path in files_to_delete:
                        try:
                            if os.path.exists(file_path):
                                os.remove(file_path)
                                logger.info(f"Cleaned up unused temp file: {file_path}")
                        except Exception as e:
                            logger.error(f"Error removing temp file {file_path}: {e}")
                    
                    logger.info(f"Temp file cleanup: {len(files_to_delete)} files removed")
                    
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in temp file cleanup: {e}")
                await asyncio.sleep(3600)
    
    async def find_alternative_version(self, original_title, channel):
        """Try multiple search queries to find a non-DRM version"""
        video_id = None
        if "youtube.com" in original_title or "youtu.be" in original_title:
            video_id = self.extract_video_id(original_title)
            if video_id and self.youtube_api_available:
                try:
                    video_info = await self.get_youtube_info(video_id)
                    if video_info:
                        original_title = video_info['title']
                        await channel.send(RESPONSES['music']['drm']['searching_alt'].format(title=original_title))
                except Exception:
                    original_title = video_id
        
        search_variants = [
            f"{original_title} lyrics",
            f"{original_title} audio",
            f"{original_title} official audio",
            f"{original_title} official video",
            f"{original_title} music",
            f"{original_title} full song"
        ]
        
        for variant in search_variants:
            search_query = f"ytsearch:{variant}"
            try:
                with yt_dlp.YoutubeDL({'quiet': True, 'format': 'bestaudio'}) as ytdl:
                    data = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: ytdl.extract_info(search_query, download=False, process=False)
                    )
                    
                    if data and 'entries' in data and data['entries']:
                        entry = data['entries'][0]
                        url = f"https://www.youtube.com/watch?v={entry['id']}"
                        title = entry.get('title', original_title)
                        
                        await channel.send(RESPONSES['music']['status']['found_alternative'].format(title=title))
                        return url, title
            except Exception as e:
                continue
        
        return None, None
    
    async def get_youtube_info(self, video_id):
        """Get video information using YouTube API"""
        if not self.youtube_api_available:
            return None
            
        try:
            request = self.youtube.videos().list(
                part="snippet,contentDetails",
                id=video_id
            )
            response = await asyncio.get_event_loop().run_in_executor(None, request.execute)
            
            if not response['items']:
                return None
                
            video_data = response['items'][0]
            title = video_data['snippet']['title']
            channel = video_data['snippet']['channelTitle']
            duration = video_data['contentDetails']['duration']
            
            return {
                'id': video_id,
                'title': title,
                'channel': channel,
                'duration': duration
            }
        except HttpError as e:
            logger.error(f"YouTube API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching YouTube info: {e}")
            return None
            
    def extract_video_id(self, url):
        """Extract YouTube video ID from URL"""
        if 'youtube.com/watch' in url:
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            if 'v' in query_params:
                return query_params['v'][0]
        elif 'youtu.be/' in url:
            return url.split('youtu.be/')[1].split('?')[0]
        return None
    
    async def download_to_temp_file(self, url, title):
        """Download YouTube audio to a temporary file"""
        safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c==' ']).rstrip()
        safe_title = safe_title.replace(' ', '_')[:50]
        
        file_path = os.path.join(self.temp_dir, f"{safe_title}_{int(time.time())}.mp3")
        
        try:
            download_options = [
                {
                    'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
                    'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
                    'quiet': True,
                    'no_warnings': True,
                    'ignoreerrors': True,
                    'noplaylist': True,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }]
                },
                {
                    'format': 'bestaudio[protocol!*=hls]/best[protocol!*=hls]',
                    'extractor_args': {'youtube': {'player_client': ['android']}},
                    'quiet': True,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }]
                },
                {
                    'format': 'bestaudio[drm=false]/best[drm=false]',
                    'extractor_args': {'youtube': {'player_client': ['web']}},
                    'geo_bypass': True,
                    'geo_bypass_country': 'US',
                    'quiet': True,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }]
                },
                {'format': '251/250/249/bestaudio'},
                {'format': '140/m4a/mp3/bestaudio'}
            ]
            
            success = False
            error_message = ""
            
            is_youtube = "youtube.com" in url or "youtu.be" in url
            video_id = self.extract_video_id(url) if is_youtube else None
            
            if is_youtube and video_id:
                search_query = f"ytsearch:{title} audio"
                try:
                    with yt_dlp.YoutubeDL({'quiet': True, 'format': 'bestaudio'}) as ytdl:
                        data = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: ytdl.extract_info(search_query, download=False, process=False)
                        )
                        
                        if data and 'entries' in data and data['entries']:
                            entry = data['entries'][0]
                            alt_url = f"https://www.youtube.com/watch?v={entry['id']}"
                            
                            for options in download_options:
                                ydl_opts = {
                                    **options,
                                    'outtmpl': file_path,
                                    'quiet': True
                                }
                                
                                try:
                                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                        await asyncio.get_event_loop().run_in_executor(
                                            None, lambda: ydl.download([alt_url])
                                        )
                                    
                                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                                        return file_path
                                except Exception as e:
                                    error_message = str(e)
                                    continue
                except Exception:
                    pass
            
            for options in download_options:
                try:
                    ydl_opts = {
                        **options,
                        'outtmpl': file_path,
                        'quiet': True,
                        'no_warnings': True,
                        'ignoreerrors': True,
                        'noplaylist': True
                    }
                    
                    if 'postprocessors' not in ydl_opts:
                        ydl_opts['postprocessors'] = [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }]
                    
                    ydl_opts['postprocessor_args'] = [
                        '-reconnect', '1',
                        '-reconnect_streamed', '1',
                        '-reconnect_delay_max', '5'
                    ]
                    
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        await asyncio.get_event_loop().run_in_executor(
                            None, lambda: ydl.download([url])
                        )
                    
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                        success = True
                        break
                except Exception as e:
                    error_message = str(e)
                    continue
            
            if success:
                return file_path
            else:
                logger.error(f"Failed to download audio: {error_message}")
                return None
        except Exception as e:
            logger.error(f"Error downloading to temp file: {e}")
            return None
        
    async def post_error_report(self, guild_id, channel=None):
        """Post a collated report of errors and clear the log"""
        if guild_id not in self.error_logs or not self.error_logs[guild_id]:
            return
        
        # Use provided channel or stored command channel
        if not channel:
            channel = self.command_channels.get(guild_id)
        
        if not channel:
            return  # Don't post anywhere if no channel specified
            
        error_list = self.error_logs[guild_id]
        if len(error_list) == 0:
            return
            
        error_report = RESPONSES['music']['error_report']['header'] + "\n```\n"
        
        for track, error_type in error_list:
            if error_type == "drm":
                error_report += RESPONSES['music']['error_report']['drm_error'].format(track=track) + "\n"
            elif error_type == "not_found":
                error_report += RESPONSES['music']['error_report']['not_found'].format(track=track) + "\n"
            else:
                error_report += RESPONSES['music']['error_report']['general_error'].format(track=track) + "\n"
        
        error_report += "```"
        
        if len(error_list) > 0:
            await channel.send(error_report)
        
        self.error_logs[guild_id] = []

    def add_error_log(self, guild_id, track_title, error_type="general"):
        """Add an error to the log"""
        if guild_id not in self.error_logs:
            self.error_logs[guild_id] = []
            
        self.error_logs[guild_id].append((track_title, error_type))
        
        return len(self.error_logs[guild_id]) >= self.error_threshold
        
    async def fetch_youtube_playlist(self, url, ctx):
        """Fetches and processes a YouTube playlist"""
        try:
            await ctx.send(RESPONSES['music']['status']['processing_youtube'].format(type='playlist'))
            
            with yt_dlp.YoutubeDL({
                'extract_flat': True,
                'force_generic_extractor': False,
                'ignoreerrors': True,
                'playlistend': 50
            }) as ytdl:
                playlist_dict = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ytdl.extract_info(url, download=False)
                )
                
            if not playlist_dict:
                await ctx.send(RESPONSES['music']['errors']['youtube_error'])
                return []
                
            tracks = []
            if 'entries' in playlist_dict:
                for entry in playlist_dict['entries']:
                    if entry:
                        video_url = entry.get('url', '')
                        if not video_url and entry.get('id'):
                            video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                            
                        video_title = entry.get('title', 'Unknown Title')
                        if video_url:
                            tracks.append((video_url, video_title))
            
            return tracks
        except Exception as e:
            logger.error(f"Error fetching YouTube playlist: {e}")
            await ctx.send(RESPONSES['music']['errors']['youtube_error'])
            return []
        
    def handle_playback_complete(self, error, guild_id, file_path=None):
        """Handle playback completion and clean up temp files"""
        if error:
            logger.error(f"Playback error: {error}")
        
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Cleaned up temp file: {file_path}")
            except Exception as e:
                logger.error(f"Error cleaning up temp file: {e}")
        
        asyncio.run_coroutine_threadsafe(
            self.play_next(guild_id),
            self.bot.loop
        )
        
    async def play_next(self, guild_id):
        try:
            # Get the command channel for this guild
            channel = self.command_channels.get(guild_id)
            if not channel:
                return
                
            if guild_id in self.queue and self.queue[guild_id]:
                url, title = self.queue[guild_id].pop(0)
                
                if guild_id in self.loop and self.loop[guild_id]:
                    self.queue[guild_id].append((url, title))
                
                voice_client = self.voice_clients[guild_id]
                
                if url.startswith('file://'):
                    file_path = url[7:]
                    
                    if os.path.exists(file_path):
                        try:
                            source = discord.FFmpegPCMAudio(file_path)
                            voice_client.play(
                                source, 
                                after=lambda e: self.handle_playback_complete(e, guild_id, file_path)
                            )
                            await channel.send(RESPONSES['music']['status']['now_playing'].format(title=title))
                        except Exception as e:
                            logger.error(f"Error playing local file: {e}")
                            await channel.send(RESPONSES['music']['errors']['error_playing'].format(title=title))
                            await self.play_next(guild_id)
                        return
                    else:
                        logger.error(f"Local file not found: {file_path}")
                        await channel.send(RESPONSES['music']['errors']['file_not_found'].format(title=title))
                        await self.play_next(guild_id)
                        return
                
                is_search = url.startswith('ytsearch:')
                
                try:
                    loop = asyncio.get_event_loop()
                    
                    ydl_opts = {
                        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
                        'quiet': False,
                        'no_warnings': False,
                        'ignoreerrors': True,
                        'default_search': 'ytsearch' if is_search else None,
                        'noplaylist': True,
                        'skip_download': False,
                        'continue_dl': True,
                        'ignore_no_formats_error': True,
                        'ignore_config': True,
                        'geo_bypass': True,
                        'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
                    }
                    
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ytdl:
                            if is_search:
                                logger.info(f"Searching for: {url}")
                                
                            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
                        
                        if not data:
                            video_id = self.extract_video_id(url)
                            if video_id and self.youtube_api_available:
                                logger.info(f"No data returned from yt-dlp, trying API for: {url}")
                                video_info = await self.get_youtube_info(video_id)
                                if video_info:
                                    logger.info(f"Using YouTube API fallback for: {title}")
                                    await channel.send(RESPONSES['music']['drm']['no_stream'])
                                    temp_file = await self.download_to_temp_file(url, video_info['title'])
                                    if temp_file:
                                        self.queue[guild_id].insert(0, (f"file://{temp_file}", video_info['title']))
                                        await self.play_next(guild_id)
                                        return
                                
                            self.add_error_log(guild_id, title, "not_found")
                            logger.error(f"Error finding: {title}")
                            
                            if len(self.error_logs.get(guild_id, [])) >= self.error_threshold:
                                await self.post_error_report(guild_id, channel)
                            
                            await self.play_next(guild_id)
                            return
                    
                    except Exception as e:
                        error_str = str(e)
                        if self.is_drm_error(error_str):
                            video_id = self.extract_video_id(url)
                            if video_id and self.youtube_api_available:
                                logger.info(f"DRM detected, trying YouTube API: {title}")
                                await channel.send(RESPONSES['music']['drm']['detected'])
                                
                                video_info = await self.get_youtube_info(video_id)
                                if video_info:
                                    temp_file = await self.download_to_temp_file(url, video_info['title'])
                                    if temp_file:
                                        self.queue[guild_id].insert(0, (f"file://{temp_file}", video_info['title']))
                                        await self.play_next(guild_id)
                                        return
                            
                            await channel.send(RESPONSES['music']['drm']['api_failed'])
                            alt_url, alt_title = await self.find_alternative_version(title, channel)
                            if alt_url:
                                self.queue[guild_id].insert(0, (alt_url, alt_title or title))
                            else:
                                search_query = f"ytsearch:{title} lyrics"
                                await channel.send(RESPONSES['music']['drm']['trying_generic'])
                                self.queue[guild_id].insert(0, (search_query, title))
                            
                            await self.play_next(guild_id)
                            return
                        
                        raise
                    
                    if 'entries' in data and data['entries']:
                        data = data['entries'][0]
                    
                    # Check for HLS/SABR streaming that causes 403 errors
                    if ('manifest.googlevideo.com' in str(data.get('url', '')) or 
                        any('hls' in str(f.get('protocol', '')) for f in data.get('formats', []))):
                        logger.info(f"HLS/SABR detected, downloading to temp file: {title}")
                        temp_file = await self.download_to_temp_file(url, title)
                        if temp_file:
                            self.queue[guild_id].insert(0, (f"file://{temp_file}", title))
                            await self.play_next(guild_id)
                            return
                    
                    stream_url = None
                    
                    if 'url' in data:
                        stream_url = data['url']
                    
                    elif 'formats' in data and data['formats']:
                        # Prioritize non-HLS formats to avoid SABR issues
                        audio_formats = [f for f in data['formats'] 
                                        if f.get('acodec') != 'none' and f.get('url') and 'hls' not in f.get('protocol', '')]
                        
                        if not audio_formats:
                            # Fallback to any audio format if no non-HLS found
                            audio_formats = [f for f in data['formats'] 
                                            if f.get('acodec') != 'none' and f.get('url')]
                        
                        if audio_formats:
                            if all('abr' in f for f in audio_formats):
                                audio_formats.sort(key=lambda f: f.get('abr', 0), reverse=True)
                            
                            stream_url = audio_formats[0]['url']
                    
                    if not stream_url:
                        video_id = self.extract_video_id(url)
                        if video_id and self.youtube_api_available:
                            video_info = await self.get_youtube_info(video_id)
                            if video_info:
                                logger.info(f"No stream URL, using YouTube API for: {title}")
                                await channel.send(RESPONSES['music']['drm']['no_stream'])
                                temp_file = await self.download_to_temp_file(url, video_info['title'])
                                if temp_file:
                                    self.queue[guild_id].insert(0, (f"file://{temp_file}", video_info['title']))
                                    await self.play_next(guild_id)
                                    return
                        
                        self.add_error_log(guild_id, title, "no_stream")
                        logger.error(f"No valid audio stream found for: {title}")
                        
                        if len(self.error_logs.get(guild_id, [])) >= self.error_threshold:
                            await self.post_error_report(guild_id, channel)
                            
                        await self.play_next(guild_id)
                        return
                    
                    if 'title' in data:
                        title = data['title']
                    
                    try:
                        source = discord.FFmpegPCMAudio(
                            stream_url, 
                            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                        )
                        voice_client.play(source, after=lambda e: self.handle_playback_error(e, guild_id, url, title, channel))
                        await channel.send(RESPONSES['music']['status']['now_playing'].format(title=title))
                        
                        if guild_id in self.error_logs and self.error_logs[guild_id]:
                            await self.post_error_report(guild_id, channel)
                            
                    except Exception as e:
                        error_msg = str(e).lower()
                        logger.error(f"Error playing song: {e}")
                        
                        if self.is_drm_error(error_msg):
                            video_id = self.extract_video_id(url)
                            if video_id and self.youtube_api_available:
                                logger.info(f"DRM error during playback, trying API: {title}")
                                await channel.send(RESPONSES['music']['drm']['error_playback'])
                                video_info = await self.get_youtube_info(video_id)
                                if video_info:
                                    temp_file = await self.download_to_temp_file(url, video_info['title'])
                                    if temp_file:
                                        self.queue[guild_id].insert(0, (f"file://{temp_file}", video_info['title']))
                                        await self.play_next(guild_id)
                                        return
                            
                            self.add_error_log(guild_id, title, "drm")
                            
                            await channel.send(RESPONSES['music']['drm']['api_failed'])
                            alt_url, alt_title = await self.find_alternative_version(title, channel)
                            if alt_url:
                                self.queue[guild_id].insert(0, (alt_url, alt_title or title))
                                await self.play_next(guild_id)
                            else:
                                search_query = f"ytsearch:{title} lyrics"
                                await channel.send(RESPONSES['music']['drm']['trying_generic'])
                                self.queue[guild_id].insert(0, (search_query, title))
                                await self.play_next(guild_id)
                        else:
                            self.add_error_log(guild_id, title, "general")
                            
                            if len(self.error_logs.get(guild_id, [])) >= self.error_threshold:
                                await self.post_error_report(guild_id, channel)
                                
                            await self.play_next(guild_id)
                except Exception as e:
                    logger.error(f"Error extracting song info: {e}")
                    
                    self.add_error_log(guild_id, title, "processing")
                    
                    if len(self.error_logs.get(guild_id, [])) >= self.error_threshold:
                        await self.post_error_report(guild_id, channel)
                        
                    await self.play_next(guild_id)
        except Exception as e:
            logger.error(f"Error in play_next: {e}")

    def handle_playback_error(self, error, guild_id, url, title, channel):
        """Handle errors that occur during playback"""
        if error:
            asyncio.run_coroutine_threadsafe(
                self.process_playback_error(error, guild_id, url, title, channel),
                self.bot.loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                self.play_next(guild_id),
                self.bot.loop
            )
            
    async def process_playback_error(self, error, guild_id, url, title, channel):
        """Process playback errors and try alternatives for DRM issues"""
        error_msg = str(error).lower()
        logger.error(f"Playback error: {error}")
        
        if self.is_drm_error(error_msg) or "403" in error_msg:
            video_id = self.extract_video_id(url)
            if video_id and self.youtube_api_available:
                logger.info(f"DRM error during playback, using YouTube API for: {title}")
                await channel.send(RESPONSES['music']['drm']['error_playback'])
                
                video_info = await self.get_youtube_info(video_id)
                if video_info:
                    temp_file = await self.download_to_temp_file(url, video_info['title'])
                    if temp_file:
                        self.queue[guild_id].insert(0, (f"file://{temp_file}", video_info['title']))
                        await self.play_next(guild_id)
                        return
            
            self.add_error_log(guild_id, title, "drm")
            
            await channel.send(RESPONSES['music']['drm']['api_failed'])
            alt_url, alt_title = await self.find_alternative_version(title, channel)
            if alt_url:
                self.queue[guild_id].insert(0, (alt_url, alt_title or title))
            else:
                search_query = f"ytsearch:{title} lyrics"
                await channel.send(RESPONSES['music']['drm']['trying_generic'])
                self.queue[guild_id].insert(0, (search_query, title))
        else:
            self.add_error_log(guild_id, title, "playback")
        
        await self.play_next(guild_id)
    
    def get_spotify_track_info(self, url):
        try:
            spotify = get_spotify_client()
            if not spotify:
                return None
                
            track_id = re.search(r'track/([a-zA-Z0-9]+)', url).group(1)
            track = spotify.track(track_id)
            return f"{track['artists'][0]['name']} - {track['name']}"
        except Exception as e:
            logger.error(f"Error getting Spotify track info: {e}")
            return None
    
    def get_spotify_playlist_tracks(self, url):
        try:
            spotify = get_spotify_client()
            if not spotify:
                return []
                
            playlist_id = re.search(r'playlist/([a-zA-Z0-9]+)', url).group(1)
            tracks = []
            
            results = spotify.playlist_tracks(playlist_id)
            
            while results:
                for item in results['items']:
                    if item['track'] and item['track']['type'] == 'track':
                        track = item['track']
                        search_query = f"{track['artists'][0]['name']} - {track['name']}"
                        tracks.append((f"ytsearch:{search_query}", search_query))
                
                if results['next']:
                    results = spotify.next(results)
                else:
                    break
            
            return tracks
        except Exception as e:
            logger.error(f"Error getting Spotify playlist: {e}")
            return []
    
    @commands.command()
    async def join(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            if ctx.author.voice:
                channel = ctx.author.voice.channel
                
                if ctx.guild.id in self.voice_clients:
                    if self.voice_clients[ctx.guild.id].channel == channel:
                        await ctx.send(RESPONSES['music']['errors']['already_connected'])
                        return
                    else:
                        await self.voice_clients[ctx.guild.id].disconnect()
                
                voice_client = await channel.connect()
                self.voice_clients[ctx.guild.id] = voice_client
                await ctx.send(RESPONSES['music']['status']['joined'].format(channel=channel.name))
            else:
                await ctx.send(RESPONSES['music']['errors']['not_in_voice'])
        except Exception as e:
            logger.error(f"Error joining voice channel: {e}")
            await ctx.send(RESPONSES['music']['errors']['join_error'])
            
    @commands.command()
    async def shuffle(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            guild_id = ctx.guild.id
            if guild_id not in self.queue or len(self.queue[guild_id]) < 2:
                await ctx.send(RESPONSES['music']['errors']['shuffle_min'])
                return
                
            current_queue = self.queue[guild_id]
            random.shuffle(current_queue)
            self.queue[guild_id] = current_queue
            
            await ctx.send(RESPONSES['music']['status']['shuffled'])
        except Exception as e:
            logger.error(f"Error shuffling queue: {e}")
            await ctx.send(RESPONSES['music']['errors']['shuffle_error'])
    
    @commands.command()
    async def loop(self, ctx):
        """Toggle queue loop mode"""
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            guild_id = ctx.guild.id
            
            if guild_id not in self.loop:
                self.loop[guild_id] = False
            
            self.loop[guild_id] = not self.loop[guild_id]
            
            if self.loop[guild_id]:
                await ctx.send(RESPONSES['music']['status']['loop_enabled'])
            else:
                await ctx.send(RESPONSES['music']['status']['loop_disabled'])
                
        except Exception as e:
            logger.error(f"Error toggling loop: {e}")
            await ctx.send(RESPONSES['music']['errors']['loop_error'])
        
    @commands.command()
    async def leave(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            if ctx.guild.id in self.voice_clients:
                await self.voice_clients[ctx.guild.id].disconnect()
                del self.voice_clients[ctx.guild.id]
                if ctx.guild.id in self.queue:
                    del self.queue[ctx.guild.id]
                if ctx.guild.id in self.loop:
                    del self.loop[ctx.guild.id]
                if ctx.guild.id in self.command_channels:
                    del self.command_channels[ctx.guild.id]
            else:
                await ctx.send(RESPONSES['music']['errors']['not_connected'])
        except Exception as e:
            logger.error(f"Error leaving voice channel: {e}")
            await ctx.send(RESPONSES['music']['errors']['leave_error'])
            
    @commands.command()
    async def play(self, ctx, *, url):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            if ctx.guild.id not in self.voice_clients:
                await self.join(ctx)
                if ctx.guild.id not in self.voice_clients:
                    return
            
            guild_id = ctx.guild.id
            if guild_id not in self.queue:
                self.queue[guild_id] = []
            
            if 'spotify.com/playlist/' in url:
                await ctx.send(RESPONSES['music']['status']['processing_spotify'].format(type='playlist'))
                tracks = self.get_spotify_playlist_tracks(url)
                
                if not tracks:
                    await ctx.send(RESPONSES['music']['errors']['spotify_error'].format(type='playlist'))
                    return
                
                self.queue[guild_id].extend([(url, title) for url, title in tracks])
                
                if not self.voice_clients[guild_id].is_playing():
                    await self.play_next(guild_id)
                else:
                    await ctx.send(RESPONSES['music']['status']['added_tracks'].format(count=len(tracks), type='playlist'))
                
            elif 'spotify.com/track/' in url:
                await ctx.send(RESPONSES['music']['status']['processing_spotify'].format(type='track'))
                search_query = self.get_spotify_track_info(url)
                if not search_query:
                    await ctx.send(RESPONSES['music']['errors']['spotify_error'].format(type='track'))
                    return
                    
                await ctx.send(RESPONSES['music']['status']['searching_youtube'].format(query=search_query))
                url = f"ytsearch:{search_query}"
                title = search_query
                
                self.queue[guild_id].append((url, title))
                
                if not self.voice_clients[guild_id].is_playing():
                    await self.play_next(guild_id)
                else:
                    await ctx.send(RESPONSES['music']['status']['added_to_queue'].format(title=title))
                
            elif ('youtube.com' in url and 'list=' in url):
                await ctx.send(RESPONSES['music']['status']['processing_youtube'].format(type='playlist'))
                tracks = await self.fetch_youtube_playlist(url, ctx)
                
                if not tracks:
                    await ctx.send(RESPONSES['music']['errors']['youtube_error'])
                    return
                
                self.queue[guild_id].extend([(track_url, track_title) for track_url, track_title in tracks])
                
                if not self.voice_clients[guild_id].is_playing():
                    await self.play_next(guild_id)
                else:
                    await ctx.send(RESPONSES['music']['status']['added_tracks'].format(count=len(tracks), type='YouTube playlist'))
            
            elif ('youtube.com/watch' in url or 'youtu.be/' in url):
                video_id = self.extract_video_id(url)
                if not video_id:
                    await ctx.send(RESPONSES['music']['errors']['invalid_url'])
                    return
                
                await ctx.send(RESPONSES['music']['status']['processing_youtube'].format(type='video'))
                
                try:
                    loop = asyncio.get_event_loop()
                    
                    with yt_dlp.YoutubeDL({
                        'format': 'bestaudio/best',
                        'quiet': True,
                        'default_search': 'ytsearch',
                        'ignoreerrors': True,
                        'noplaylist': True
                    }) as ytdl:
                        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
                        
                        if data:
                            title = data.get('title', 'Unknown Title')
                            webpage_url = data.get('webpage_url', url)
                            
                            self.queue[guild_id].append((webpage_url, title))
                            
                            if not self.voice_clients[guild_id].is_playing():
                                await self.play_next(guild_id)
                            else:
                                await ctx.send(RESPONSES['music']['status']['added_to_queue'].format(title=title))
                            return
                except Exception as e:
                    error_str = str(e)
                    if self.is_drm_error(error_str):
                        await ctx.send(RESPONSES['music']['drm']['detected'])
                    else:
                        raise
                
                title = "YouTube Video"
                channel_name = None
                if self.youtube_api_available:
                    try:
                        video_info = await self.get_youtube_info(video_id)
                        if video_info:
                            title = video_info['title']
                            channel_name = video_info.get('channel', '')
                    except Exception as e:
                        logger.error(f"Error getting YouTube info: {e}")
                
                await ctx.send(RESPONSES['music']['status']['downloading'].format(title=title))
                temp_file = await self.download_to_temp_file(url, title)
                
                if temp_file:
                    self.queue[guild_id].append((f"file://{temp_file}", title))
                    
                    if not self.voice_clients[guild_id].is_playing():
                        await self.play_next(guild_id)
                    else:
                        await ctx.send(RESPONSES['music']['status']['added_to_queue'].format(title=title))
                else:
                    search_text = title
                    if channel_name:
                        search_text = f"{title} {channel_name}"
                        
                    await ctx.send(RESPONSES['music']['status']['searching_exact'].format(title=title))
                    search_query = f"ytsearch:{search_text}"
                    
                    self.queue[guild_id].append((search_query, title))
                    
                    if not self.voice_clients[guild_id].is_playing():
                        await self.play_next(guild_id)
                    else:
                        await ctx.send(RESPONSES['music']['status']['added_to_queue'].format(title=title))
            
            else:
                try:
                    is_search = url.startswith(('ytsearch:', 'scsearch:'))
                    
                    if not is_search and not url.startswith(('http://', 'https://')):
                        search_query = f"ytsearch:{url}"
                        await ctx.send(RESPONSES['music']['status']['searching'].format(query=url))
                        title = url
                        
                        self.queue[guild_id].append((search_query, title))
                    else:
                        loop = asyncio.get_event_loop()
                        with yt_dlp.YoutubeDL({
                            'format': 'bestaudio/best',
                            'quiet': True,
                            'default_search': 'ytsearch',
                            'skip_download': False,
                            'continue_dl': True,
                            'ignore_no_formats_error': True,
                            'ignore_config': True,
                            'geo_bypass': True
                        }) as ytdl:
                            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
                        
                        title = data.get('title', 'Unknown Title')
                        
                        webpage_url = data.get('webpage_url', url)
                        
                        self.queue[guild_id].append((webpage_url, title))
                    
                    if not self.voice_clients[guild_id].is_playing():
                        await self.play_next(guild_id)
                    else:
                        await ctx.send(RESPONSES['music']['status']['added_to_queue'].format(title=title))
                        
                except Exception as e:
                    logger.error(f"Error processing URL: {e}")
                    await ctx.send(RESPONSES['music']['errors']['processing_error'])
                    
                    search_query = f"ytsearch:{url}"
                    title = url
                    
                    self.queue[guild_id].append((search_query, title))
                    
                    if not self.voice_clients[guild_id].is_playing():
                        await self.play_next(guild_id)
                    else:
                        await ctx.send(RESPONSES['music']['status']['added_search'])
                        
        except Exception as e:
            logger.error(f"Error in play command: {e}")
            await ctx.send(RESPONSES['music']['errors']['processing_error'])
    
    @commands.command()
    async def pause(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            if ctx.guild.id in self.voice_clients and self.voice_clients[ctx.guild.id].is_playing():
                self.voice_clients[ctx.guild.id].pause()
                await ctx.send(RESPONSES['music']['status']['paused'])
            else:
                await ctx.send(RESPONSES['music']['errors']['nothing_playing'])
        except Exception as e:
            logger.error(f"Error pausing: {e}")
            await ctx.send(RESPONSES['music']['errors']['pause_error'])
    
    @commands.command()
    async def resume(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            if ctx.guild.id in self.voice_clients and self.voice_clients[ctx.guild.id].is_paused():
                self.voice_clients[ctx.guild.id].resume()
                await ctx.send(RESPONSES['music']['status']['resumed'])
            else:
                await ctx.send(RESPONSES['music']['errors']['nothing_paused'])
        except Exception as e:
            logger.error(f"Error resuming: {e}")
            await ctx.send(RESPONSES['music']['errors']['resume_error'])
    
    @commands.command()
    async def stop(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            if ctx.guild.id in self.voice_clients:
                self.voice_clients[ctx.guild.id].stop()
                if ctx.guild.id in self.queue:
                    self.queue[ctx.guild.id].clear()
                if ctx.guild.id in self.loop:
                    self.loop[ctx.guild.id] = False
                await ctx.send(RESPONSES['music']['status']['stopped'])
            else:
                await ctx.send(RESPONSES['music']['errors']['nothing_playing'])
        except Exception as e:
            logger.error(f"Error stopping: {e}")
            await ctx.send(RESPONSES['music']['errors']['stop_error'])
    
    @commands.command()
    async def skip(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            if ctx.guild.id in self.voice_clients and self.voice_clients[ctx.guild.id].is_playing():
                self.voice_clients[ctx.guild.id].stop()
                await ctx.send(RESPONSES['music']['status']['skipped'])
            else:
                await ctx.send(RESPONSES['music']['errors']['nothing_playing'])
        except Exception as e:
            logger.error(f"Error skipping: {e}")
            await ctx.send(RESPONSES['music']['errors']['skip_error'])
    
    @commands.command()
    async def queue(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            guild_id = ctx.guild.id
            if guild_id in self.queue and self.queue[guild_id]:
                queue_list = "\n".join([f"{i+1}. {title}" for i, (url, title) in enumerate(self.queue[guild_id][:10])])
                if len(self.queue[guild_id]) > 10:
                    queue_list += f"\n" + RESPONSES['music']['queue']['more_items'].format(count=len(self.queue[guild_id]) - 10)
                
                loop_status = RESPONSES['music']['queue']['loop_on'] if guild_id in self.loop and self.loop[guild_id] else RESPONSES['music']['queue']['loop_off']
                
                await ctx.send(RESPONSES['music']['queue']['header'].format(loop_status=loop_status) + f"\n```{queue_list}```")
            else:
                await ctx.send(RESPONSES['music']['queue']['empty'])
        except Exception as e:
            logger.error(f"Error showing queue: {e}")
            await ctx.send(RESPONSES['music']['errors']['queue_error'])
    
    @commands.command()
    async def clear(self, ctx):
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            guild_id = ctx.guild.id
            if guild_id in self.queue:
                self.queue[guild_id].clear()
                loop_status = " (Loop remains ON)" if guild_id in self.loop and self.loop[guild_id] else ""
                await ctx.send(RESPONSES['music']['status']['queue_cleared'].format(loop_status=loop_status))
            else:
                await ctx.send(RESPONSES['music']['status']['queue_empty'])
        except Exception as e:
            logger.error(f"Error clearing queue: {e}")
            await ctx.send(RESPONSES['music']['errors']['clear_error'])
            
    @commands.command()
    async def ytsearch(self, ctx, *, query):
        """Search YouTube for a query and add the first result to the queue"""
        # Store the command channel
        self.command_channels[ctx.guild.id] = ctx.channel
        
        try:
            await ctx.send(RESPONSES['music']['status']['searching_youtube'].format(query=query))
            
            search_query = f"ytsearch:{query}"
            guild_id = ctx.guild.id
            
            if guild_id not in self.voice_clients:
                await self.join(ctx)
                if guild_id not in self.voice_clients:
                    return
            
            if guild_id not in self.queue:
                self.queue[guild_id] = []
            
            self.queue[guild_id].append((search_query, query))
            
            if not self.voice_clients[guild_id].is_playing():
                await self.play_next(guild_id)
            else:
                await ctx.send(RESPONSES['music']['status']['added_to_queue'].format(title=query))
                
        except Exception as e:
            logger.error(f"Error in ytsearch command: {e}")
            await ctx.send(RESPONSES['music']['errors']['search_error'])

class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.roulette_chambers = {}
        self.positive_groob = False 
        
    @commands.command()
    async def sadge(self, ctx):
        self.positive_groob = True
    
    @commands.command()
    async def roulette(self, ctx):
        try:
            user_id = ctx.author.id
            
            if user_id not in self.roulette_chambers:
                self.roulette_chambers[user_id] = random.randint(1, 6)
            
            chamber = self.roulette_chambers[user_id]
            
            if chamber == 1:
                del self.roulette_chambers[user_id]
                await ctx.send(RESPONSES['games']['roulette']['bang'].format(user=ctx.author.display_name))
            else:
                self.roulette_chambers[user_id] -= 1
                await ctx.send(RESPONSES['games']['roulette']['safe'].format(user=ctx.author.display_name))
        except Exception as e:
            logger.error(f"Error in roulette: {e}")
            await ctx.send(RESPONSES['games']['roulette']['jammed'])
            
    @commands.command()
    async def tts(self, ctx):
        return
    
    @commands.command()
    async def rps(self, ctx, choice=None):
        if not choice:
            await ctx.send(RESPONSES['games']['rps']['usage'])
            return
        
        choices = ['rock', 'paper', 'scissors']
        choice = choice.lower()
        
        if choice not in choices:
            await ctx.send(RESPONSES['games']['rps']['invalid'])
            return
        
        bot_choice = random.choice(choices)
        
        if choice == bot_choice:
            result = RESPONSES['games']['rps']['tie']
        elif (choice == 'rock' and bot_choice == 'scissors') or \
             (choice == 'paper' and bot_choice == 'rock') or \
             (choice == 'scissors' and bot_choice == 'paper'):
            result = RESPONSES['games']['rps']['win']
        else:
            result = RESPONSES['games']['rps']['lose']
        
        await ctx.send(RESPONSES['games']['rps']['result'].format(
            user_choice=choice.capitalize(),
            bot_choice=bot_choice.capitalize(),
            result=result
        ))
    
    @commands.command(name='8ball')
    async def magic_8ball(self, ctx, *, question=None):
        if not question:
            await ctx.send(RESPONSES['games']['magic_8ball']['no_question'])
            return
        
        response = random.choice(RESPONSES['games']['magic_8ball']['responses'])
        await ctx.send(f"🎱 **{response}**")
    
    @commands.command()
    async def flip(self, ctx):
        result = random.choice(['Heads', 'Tails'])
        if result == 'Heads':
            await ctx.send(RESPONSES['games']['flip']['heads'])
        else:
            await ctx.send(RESPONSES['games']['flip']['tails'])
    
    @commands.command()
    async def roll(self, ctx, *, dice='1d6'):
        try:
            modifier = 0
            dice = dice.replace(' ', '')
            if '+' in dice:
                dice, mod = dice.split('+')
                modifier = int(mod)
            elif '-' in dice:
                dice, mod = dice.split('-')
                modifier = -int(mod)
            num_dice, num_sides = dice.split('d')
            num_dice = int(num_dice)
            num_sides = int(num_sides)
            if num_dice > 10 or num_sides > 100:
                await ctx.send(RESPONSES['games']['roll']['too_many'])
                return
            rolls = [random.randint(1, num_sides) for _ in range(num_dice)]
            subtotal = sum(rolls)
            total = subtotal + modifier
            mod_str = ""
            if modifier > 0:
                mod_str = f" + {modifier}"
            elif modifier < 0:
                mod_str = f" - {abs(modifier)}"
            if num_dice == 1:
                if modifier != 0:
                    await ctx.send(RESPONSES['games']['roll']['single_mod'].format(
                        subtotal=subtotal, modifier=mod_str, total=total
                    ))
                else:
                    await ctx.send(RESPONSES['games']['roll']['single'].format(result=total))
            else:
                if modifier != 0:
                    await ctx.send(RESPONSES['games']['roll']['multiple_mod'].format(
                        dice=f"{num_dice}d{num_sides}",
                        rolls=rolls,
                        subtotal=subtotal,
                        modifier=mod_str,
                        total=total
                    ))
                else:
                    await ctx.send(RESPONSES['games']['roll']['multiple'].format(
                        dice=f"{num_dice}d{num_sides}",
                        rolls=rolls,
                        total=total
                    ))
        except ValueError:
            await ctx.send(RESPONSES['games']['roll']['invalid'])
    
    @commands.command()
    async def fortune(self, ctx):
        fortune = random.choice(RESPONSES['games']['fortune']['responses'])
        await ctx.send(f"🥠 **Your fortune:** {fortune}")
    
    @commands.command()
    async def choose(self, ctx, *options):
        if len(options) < 2:
            await ctx.send(RESPONSES['games']['choose']['not_enough'])
            return
        
        choice = random.choice(options)
        await ctx.send(RESPONSES['games']['choose']['result'].format(choice=choice))
        
    @commands.command()
    async def uwu(self, ctx, *, text=""):
        if not text:
            await ctx.send(RESPONSES['games']['uwu']['no_text'])
            return
        uwu_text = text.replace('r', 'w').replace('l', 'w').replace('R', 'W').replace('L', 'W')
        uwu_text = uwu_text.replace('n', 'ny').replace('N', 'Ny')
        uwu_text += " " + random.choice(RESPONSES['games']['uwu']['suffixes'])
        await ctx.send(uwu_text)
    
    @commands.command()
    async def rate(self, ctx, *, thing=""):
        if not thing:
            await ctx.send(RESPONSES['games']['rate']['no_thing'])
            return
            
        if thing.lower() == "groob":
            if self.positive_groob:
                rating = random.randint(500, 1000)
                emoji = random.choice(RESPONSES['games']['rate']['emojis']['amazing'])
                self.positive_groob = False
            else:
                weights = [60, 20, 15, 5]
                choice = random.choices(['terrible', 'bad', 'mediocre', 'amazing'], weights=weights)[0]
                
                if choice == 'terrible':
                    rating = random.randint(-1000, -500)
                    emoji = random.choice(RESPONSES['games']['rate']['emojis']['terrible'])
                elif choice == 'bad':
                    rating = random.randint(-499, -1)
                    emoji = random.choice(RESPONSES['games']['rate']['emojis']['bad'])
                elif choice == 'mediocre':
                    rating = random.randint(0, 2)
                    emoji = random.choice(RESPONSES['games']['rate']['emojis']['mediocre'])
                else:
                    rating = random.randint(9, 1000)
                    emoji = random.choice(RESPONSES['games']['rate']['emojis']['amazing'])
        else:
            rating = random.randint(0, 10)
            if rating < 3:
                emoji = random.choice(RESPONSES['games']['rate']['emojis']['terrible'])
            elif rating < 7:
                emoji = random.choice(RESPONSES['games']['rate']['emojis']['good'])
            else:
                emoji = random.choice(RESPONSES['games']['rate']['emojis']['amazing'])
            
        await ctx.send(RESPONSES['games']['rate']['result'].format(thing=thing, rating=rating, emoji=emoji))
        
    @commands.command()
    async def whoban(self, ctx):
        await ctx.send(RESPONSES['games']['whoban'])
        
    @commands.command()
    async def spamdog(self, ctx):
        for i in range(5):
            await ctx.send(RESPONSES['games']['spamdog'])
            await asyncio.sleep(0.5)
            
    @commands.command()
    async def games(self, ctx):
        await ctx.send(RESPONSES['games']['list'])
        
    @commands.command(name='roastme')
    async def roastme(self, ctx):
        roast = random.choice(RESPONSES['games']['roasts'])
        await ctx.send(RESPONSES['games']['roast_format'].format(mention=ctx.author.mention, roast=roast))

    @commands.command(name='7ball')
    async def seven_ball(self, ctx, *, question=None):
        if not question:
            await ctx.send(RESPONSES['games']['seven_ball']['no_question'])
            return
        
        response = random.choice(RESPONSES['games']['seven_ball']['responses'])
        await ctx.send(f"🎱 **{response}**")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, case_insensitive=True)
ydl = yt_dlp.YoutubeDL(ydl_opts)

# Global event to check for auto-leave when users leave voice channels
@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id:
        return
    
    if before.channel and (not after.channel or before.channel != after.channel):
        guild = before.channel.guild
        guild_id = guild.id
        
        music_cog = bot.get_cog('Music')
        if not music_cog:
            return
        
        if guild_id in music_cog.voice_clients and music_cog.voice_clients[guild_id].channel == before.channel:
            human_count = sum(1 for m in before.channel.members if not m.bot)
            
            if human_count == 0:
                if guild_id in music_cog.queue:
                    music_cog.queue[guild_id].clear()
                
                if guild_id in music_cog.loop:
                    del music_cog.loop[guild_id]
                
                if guild_id in music_cog.command_channels:
                    del music_cog.command_channels[guild_id]
                
                try:
                    await music_cog.voice_clients[guild_id].disconnect(force=True)
                    del music_cog.voice_clients[guild_id]
                    logger.info(f"Auto-disconnected from {before.channel.name} - all users left")
                except Exception as e:
                    logger.error(f"Error auto-disconnecting: {e}")

@bot.command()
async def commands(ctx):
    await ctx.send(RESPONSES['commands']['list'])

@bot.event
async def on_ready():
    print(f'✅ {bot.user} has connected to Discord!')
    try:
        await bot.add_cog(Music(bot))
        await bot.add_cog(Games(bot))
        
        # Create AI cog with responses
        ai_cog = AIChatBot(bot)
        ai_cog.responses = RESPONSES['ai']
        await bot.add_cog(ai_cog)
        
        print('✅ All cogs loaded successfully')
        
        if bot.get_cog('AIChatBot'):
            print('✅ AI chat bot loaded successfully')
        else:
            print('❌ AI chat bot failed to load')
    except Exception as e:
        logger.error(f"Error loading cogs: {e}")

# Run the bot
if __name__ == "__main__":
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.error(f"Error running bot: {e}")
        print("❌ Failed to start bot. Check your BOT_TOKEN in .env file")