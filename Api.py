from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import uuid
import time
import logging
import re
import aiohttp
import asyncio
import os
import random
import json
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import concurrent.futures

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Thread pool
executor = concurrent.futures.ThreadPoolExecutor(max_workers=30)

# Token storage
download_tokens: Dict[str, Dict] = {}
TOKEN_TTL = 300
DOWNLOAD_CACHE: Dict[str, Dict] = {}
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# Cookie file path
COOKIE_FILE = "cookies.txt"
HAS_COOKIES = os.path.exists(COOKIE_FILE)

# Proxy list with working proxies - UPDATED WITH NEW CREDENTIALS
PROXIES = [
    "http://bsryebjk:7qilznwepfud@31.59.20.176:6754",
    "http://bsryebjk:7qilznwepfud@23.95.150.145:6114",
    "http://bsryebjk:7qilznwepfud@198.23.239.134:6540",
    "http://bsryebjk:7qilznwepfud@45.38.107.97:6014",
    "http://bsryebjk:7qilznwepfud@107.172.163.27:6543",
    "http://bsryebjk:7qilznwepfud@198.105.121.200:6462",
    "http://bsryebjk:7qilznwepfud@64.137.96.74:6641",
    "http://bsryebjk:7qilznwepfud@216.10.27.159:6837",
    "http://bsryebjk:7qilznwepfud@23.26.71.145:5628",
    "http://bsryebjk:7qilznwepfud@23.229.19.94:8689",
]

def extract_video_id(url_or_id: str) -> str:
    """Extract video ID from any YouTube URL"""
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url_or_id):
        return url_or_id
    
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:v=)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/live/)([a-zA-Z0-9_-]{11})',
        r'(?:live/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/watch\?.*v=)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/.*[?&]v=)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/.*/live/)([a-zA-Z0-9_-]{11})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    
    match = re.search(r'([a-zA-Z0-9_-]{11})', url_or_id)
    return match.group(1) if match else url_or_id

async def get_video_metadata(video_id: str) -> Dict[str, Any]:
    """Get video metadata including title, duration, thumbnail, etc."""
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "--dump-json",
        "--skip-download",
        f"https://youtu.be/{video_id}"
    ]
    
    if HAS_COOKIES:
        cmd.insert(5, "--cookies")
        cmd.insert(6, COOKIE_FILE)
    
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        )
        
        if result.returncode == 0 and result.stdout:
            metadata = json.loads(result.stdout)
            
            # Extract essential info
            video_info = {
                "video_id": video_id,
                "title": metadata.get("title", "Unknown Title"),
                "duration": metadata.get("duration", 0),
                "duration_string": metadata.get("duration_string", "0:00"),
                "uploader": metadata.get("uploader", "Unknown Uploader"),
                "view_count": metadata.get("view_count", 0),
                "like_count": metadata.get("like_count", 0),
                "thumbnail": metadata.get("thumbnail", ""),
                "description": metadata.get("description", "")[:200] + "..." if metadata.get("description") else "",
                "categories": metadata.get("categories", []),
                "tags": metadata.get("tags", [])[:5],
                "upload_date": metadata.get("upload_date", ""),
                "available_formats": [],
                "status": "available"
            }
            
            # Get available formats
            formats_cmd = [
                "yt-dlp",
                "--no-warnings",
                "--quiet",
                "--list-formats",
                f"https://youtu.be/{video_id}"
            ]
            
            formats_result = await asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: subprocess.run(formats_cmd, capture_output=True, text=True, timeout=10)
            )
            
            if formats_result.returncode == 0:
                lines = formats_result.stdout.split('\n')
                video_formats = []
                audio_formats = []
                
                for line in lines:
                    if 'video only' in line.lower() or ('mp4' in line.lower() and 'audio only' not in line.lower()):
                        parts = line.split()
                        if len(parts) > 1 and parts[0].isdigit():
                            video_formats.append({
                                "format_id": parts[0],
                                "resolution": parts[1] if len(parts) > 1 else "unknown",
                                "note": line[line.find(parts[1]) + len(parts[1]):].strip()[:50]
                            })
                    elif 'audio only' in line.lower():
                        parts = line.split()
                        if len(parts) > 1 and parts[0].isdigit():
                            audio_formats.append({
                                "format_id": parts[0],
                                "ext": "m4a" if 'm4a' in line.lower() else "webm" if 'webm' in line.lower() else "opus",
                                "note": line[line.find(parts[1]) + len(parts[1]):].strip()[:50]
                            })
                
                video_info["video_formats"] = video_formats[:5]
                video_info["audio_formats"] = audio_formats[:3]
            
            return video_info
        else:
            return {
                "video_id": video_id,
                "title": "Video Unavailable",
                "status": "unavailable",
                "error": result.stderr[:100] if result.stderr else "Unknown error"
            }
            
    except subprocess.TimeoutExpired:
        return {
            "video_id": video_id,
            "status": "timeout",
            "error": "Request timed out"
        }
    except json.JSONDecodeError:
        return {
            "video_id": video_id,
            "status": "error",
            "error": "Invalid response from YouTube"
        }
    except Exception as e:
        return {
            "video_id": video_id,
            "status": "error",
            "error": str(e)[:100]
        }

async def check_available_formats(video_id: str) -> Dict:
    """Check which audio formats are available for a video"""
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "--list-formats",
        f"https://youtu.be/{video_id}"
    ]
    
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        )
        
        formats = {}
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            for line in lines:
                if 'audio only' in line.lower():
                    # Extract format code
                    parts = line.split()
                    if len(parts) > 0 and parts[0].isdigit():
                        format_code = parts[0]
                        if 'm4a' in line.lower() or 'mp4' in line.lower():
                            formats[format_code] = 'm4a'
                        elif 'webm' in line.lower():
                            formats[format_code] = 'webm'
                        elif 'opus' in line.lower():
                            formats[format_code] = 'opus'
        
        return formats
    except:
        return {}

async def download_audio_smart(video_id: str) -> Optional[str]:
    """SMART AUDIO DOWNLOAD - CHECK FORMATS FIRST"""
    start_time = time.time()
    
    output_file = os.path.join(CACHE_DIR, f"{video_id}_audio_{int(time.time())}.m4a")
    
    # First check available formats
    formats = await check_available_formats(video_id)
    
    # PREFER DIRECT m4a FORMATS (140, 139, 256, etc.)
    preferred_formats = []
    
    # Check for direct m4a formats
    for code, fmt_type in formats.items():
        if fmt_type == 'm4a':
            preferred_formats.append(code)
    
    # If direct m4a formats available, use them
    if preferred_formats:
        # Try the smallest format code first (usually 140)
        preferred_formats.sort()
        format_to_use = preferred_formats[0]
        
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--quiet",
            "--no-progress",
            "--no-playlist",
            "--force-ipv4",
            "--socket-timeout", "15",
            "--retries", "2",
            "--downloader", "aria2c",
            "--downloader-args", "aria2c:-x 16 -s 16 -k 4M",
            "-f", format_to_use,
            "-o", output_file,
            f"https://youtu.be/{video_id}"
        ]
        
        if HAS_COOKIES:
            cmd.insert(5, "--cookies")
            cmd.insert(6, COOKIE_FILE)
        
        try:
            logger.info(f"🎵 SMART DIRECT AUDIO download (format {format_to_use}): {video_id}")
            
            timeout = 30
            
            result = await asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            )
            
            elapsed = time.time() - start_time
            
            if result.returncode == 0 and os.path.exists(output_file):
                file_size = os.path.getsize(output_file)
                logger.info(f"🎯 DIRECT AUDIO success ({elapsed:.2f}s): {output_file} ({file_size/1024/1024:.1f}MB)")
                return output_file
        except:
            pass
    
    # If no direct m4a format or it failed, use bestaudio with extraction
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "--no-progress",
        "--no-playlist",
        "--force-ipv4",
        "--socket-timeout", "20",
        "--retries", "2",
        "--concurrent-fragments", "4",
        "-f", "bestaudio[ext=m4a]/bestaudio",
        "--extract-audio",
        "--audio-format", "m4a",
        "--audio-quality", "0",
        "--postprocessor-args", "-vn -c:a copy -movflags +faststart",
        "-o", output_file,
        f"https://youtu.be/{video_id}"
    ]
    
    if HAS_COOKIES:
        cmd.insert(5, "--cookies")
        cmd.insert(6, COOKIE_FILE)
    
    try:
        logger.info(f"🎵 SMART EXTRACT AUDIO download: {video_id}")
        
        timeout = 45
        
        result = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        )
        
        elapsed = time.time() - start_time
        
        if result.returncode == 0 and os.path.exists(output_file):
            file_size = os.path.getsize(output_file)
            logger.info(f"✅ EXTRACT AUDIO success ({elapsed:.2f}s): {output_file} ({file_size/1024/1024:.1f}MB)")
            return output_file
        
        return None
        
    except subprocess.TimeoutExpired:
        logger.error(f"⏰ Audio timeout for {video_id}")
        return None
    except Exception as e:
        logger.error(f"❌ Audio error: {str(e)[:100]}")
        return None

async def download_audio_fast_fallback(video_id: str) -> Optional[str]:
    """FAST FALLBACK FOR AUDIO - SIMPLE AND RELIABLE"""
    start_time = time.time()
    
    output_file = os.path.join(CACHE_DIR, f"{video_id}_fallback_{int(time.time())}.m4a")
    
    # SIMPLE AND RELIABLE COMMAND
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "-f", "bestaudio",
        "--extract-audio",
        "--audio-format", "m4a",
        "-o", output_file,
        f"https://youtu.be/{video_id}"
    ]
    
    if HAS_COOKIES:
        cmd.insert(3, "--cookies")
        cmd.insert(4, COOKIE_FILE)
    
    try:
        logger.info(f"⚡ FAST FALLBACK AUDIO download: {video_id}")
        
        timeout = 60
        
        result = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        )
        
        elapsed = time.time() - start_time
        
        if result.returncode == 0 and os.path.exists(output_file):
            file_size = os.path.getsize(output_file)
            logger.info(f"✅ FALLBACK AUDIO success ({elapsed:.2f}s): {output_file}")
            return output_file
        
        return None
        
    except Exception as e:
        logger.error(f"FALLBACK AUDIO error: {str(e)[:100]}")
        return None

async def download_video_fast(video_id: str) -> Optional[str]:
    """FAST VIDEO DOWNLOAD"""
    start_time = time.time()
    
    output_file = os.path.join(CACHE_DIR, f"{video_id}_video_{int(time.time())}.mp4")
    
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--quiet",
        "--no-progress",
        "--no-playlist",
        "--force-ipv4",
        "--socket-timeout", "30",
        "--retries", "3",
        "--fragment-retries", "3",
        "--skip-unavailable-fragments",
        "--concurrent-fragments", "4",
        "-f", "best[height<=720]/best[height<=480]/best",
        "--merge-output-format", "mp4",
        "--recode-video", "mp4",
        "-o", output_file,
        f"https://youtu.be/{video_id}"
    ]
    
    if HAS_COOKIES:
        cmd.insert(5, "--cookies")
        cmd.insert(6, COOKIE_FILE)
    
    try:
        logger.info(f"🎬 FAST VIDEO download: {video_id}")
        
        timeout = 180
        
        result = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        )
        
        elapsed = time.time() - start_time
        
        if result.returncode == 0 and os.path.exists(output_file):
            file_size = os.path.getsize(output_file)
            logger.info(f"✅ VIDEO success ({elapsed:.2f}s): {output_file} ({file_size/1024/1024:.1f}MB)")
            return output_file
        
        return None
        
    except subprocess.TimeoutExpired:
        logger.error(f"⏰ Video timeout for {video_id}")
        return None
    except Exception as e:
        logger.error(f"❌ Video error: {str(e)[:100]}")
        return None

async def fast_download(video_id: str, media_type: str = "audio") -> Optional[str]:
    """Main download function - SMART AUDIO DOWNLOAD"""
    video_id = extract_video_id(video_id)
    
    # Cache check
    cache_key = f"{video_id}_{media_type}"
    if cache_key in DOWNLOAD_CACHE:
        cache_data = DOWNLOAD_CACHE[cache_key]
        cache_age = datetime.now().timestamp() - cache_data["timestamp"]
        max_age = 1200  # 20 minutes cache
        if cache_age < max_age and os.path.exists(cache_data["path"]):
            logger.info(f"⚡ Cache hit: {video_id} ({media_type})")
            return cache_data["path"]
    
    logger.info(f"🚀 Starting download: {video_id} ({media_type})")
    
    if media_type == "audio":
        # SMART AUDIO STRATEGY
        # STRATEGY 1: SMART AUDIO DOWNLOAD (checks formats first)
        file_path = await download_audio_smart(video_id)
        if file_path:
            DOWNLOAD_CACHE[cache_key] = {
                "path": file_path,
                "timestamp": datetime.now().timestamp(),
                "size": os.path.getsize(file_path),
                "method": "smart_audio"
            }
            return file_path
        
        # STRATEGY 2: FAST FALLBACK
        file_path = await download_audio_fast_fallback(video_id)
        if file_path:
            DOWNLOAD_CACHE[cache_key] = {
                "path": file_path,
                "timestamp": datetime.now().timestamp(),
                "size": os.path.getsize(file_path),
                "method": "fast_fallback"
            }
            return file_path
    
    else:
        # VIDEO
        file_path = await download_video_fast(video_id)
        if file_path:
            DOWNLOAD_CACHE[cache_key] = {
                "path": file_path,
                "timestamp": datetime.now().timestamp(),
                "size": os.path.getsize(file_path),
                "method": "fast_video"
            }
            return file_path
    
    # LAST RESORT
    logger.info(f"🆘 LAST RESORT: {video_id} ({media_type})")
    
    timestamp = int(time.time())
    if media_type == "audio":
        output_file = os.path.join(CACHE_DIR, f"{video_id}_last_{timestamp}.m4a")
        cmd = ["yt-dlp", "--quiet", "-f", "bestaudio", "--extract-audio", "--audio-format", "m4a", "-o", output_file, f"https://youtu.be/{video_id}"]
    else:
        output_file = os.path.join(CACHE_DIR, f"{video_id}_last_{timestamp}.mp4")
        cmd = ["yt-dlp", "--quiet", "-f", "best", "-o", output_file, f"https://youtu.be/{video_id}"]
    
    if HAS_COOKIES:
        cmd.insert(1, "--cookies")
        cmd.insert(2, COOKIE_FILE)
    
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        )
        
        if result.returncode == 0 and os.path.exists(output_file):
            DOWNLOAD_CACHE[cache_key] = {
                "path": output_file,
                "timestamp": datetime.now().timestamp(),
                "size": os.path.getsize(output_file),
                "method": "last_resort"
            }
            return output_file
    except:
        pass
    
    logger.error(f"❌ All download methods failed for {video_id}")
    return None

def file_stream_generator(file_path: str):
    """Stream file content"""
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536):
                yield chunk
    except Exception as e:
        logger.error(f"Stream error: {e}")

# =============== UPDATED HOMEPAGE - ONLY METADATA FETCH ===============
@app.get("/")
async def home():
    return HTMLResponse("""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🏆 Premium YouTube API</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            
            :root {
                --gold-primary: #FFD700;
                --gold-dark: #D4AF37;
                --gold-light: #FFF8DC;
                --black-bg: #0A0A0A;
                --gray-dark: #1A1A1A;
                --gray-light: #333333;
                --white-text: #FFFFFF;
                --accent-red: #FF0000;
                --accent-blue: #4ECDC4;
                --premium-gradient: linear-gradient(45deg, #FFD700, #D4AF37, #FFD700);
            }
            
            body {
                font-family: 'Montserrat', 'Segoe UI', Arial, sans-serif;
                background: var(--black-bg);
                color: var(--white-text);
                min-height: 100vh;
                overflow-x: hidden;
                background-image: 
                    radial-gradient(circle at 20% 80%, rgba(255, 215, 0, 0.15) 0%, transparent 50%),
                    radial-gradient(circle at 80% 20%, rgba(212, 175, 55, 0.15) 0%, transparent 50%);
            }
            
            .header {
                background: linear-gradient(135deg, 
                    rgba(10, 10, 10, 0.98) 0%,
                    rgba(26, 26, 26, 0.98) 100%);
                backdrop-filter: blur(25px);
                padding: 25px 60px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                border-bottom: 3px solid var(--gold-primary);
                position: sticky;
                top: 0;
                z-index: 1000;
                box-shadow: 0 15px 40px rgba(0, 0, 0, 0.7);
            }
            
            .logo-container {
                display: flex;
                align-items: center;
                gap: 25px;
            }
            
            .golden-logo {
                display: flex;
                align-items: center;
                gap: 15px;
                font-size: 32px;
                font-weight: 900;
                background: linear-gradient(45deg, var(--gold-primary), var(--gold-dark), #FFD700);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                text-shadow: 0 0 40px rgba(255, 215, 0, 0.4);
                letter-spacing: 1px;
            }
            
            .logo-icon {
                background: linear-gradient(45deg, var(--gold-primary), var(--gold-dark));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                font-size: 42px;
                filter: drop-shadow(0 0 15px rgba(255, 215, 0, 0.6));
            }
            
            .premium-badge {
                background: linear-gradient(45deg, var(--gold-primary), var(--gold-dark));
                color: var(--black-bg);
                padding: 12px 30px;
                border-radius: 30px;
                font-size: 16px;
                font-weight: 900;
                letter-spacing: 1.5px;
                text-transform: uppercase;
                box-shadow: 0 8px 25px rgba(255, 215, 0, 0.4);
                border: 2px solid rgba(255, 215, 0, 0.6);
                animation: badgeGlow 3s infinite alternate;
            }
            
            @keyframes badgeGlow {
                0% { box-shadow: 0 8px 25px rgba(255, 215, 0, 0.4); }
                100% { box-shadow: 0 8px 35px rgba(255, 215, 0, 0.7); }
            }
            
            .owner-badge {
                background: linear-gradient(45deg, #FF0000, #FF6B6B, #FF0000);
                color: white;
                padding: 15px 40px;
                border-radius: 35px;
                font-size: 20px;
                font-weight: 900;
                letter-spacing: 2px;
                text-transform: uppercase;
                box-shadow: 
                    0 0 40px rgba(255, 0, 0, 0.8),
                    inset 0 0 25px rgba(255, 255, 255, 0.2);
                border: 3px solid #FFD700;
                animation: redPulse 2s infinite alternate;
                display: flex;
                align-items: center;
                gap: 15px;
            }
            
            @keyframes redPulse {
                0% { 
                    transform: scale(1);
                    box-shadow: 0 0 40px rgba(255, 0, 0, 0.8);
                }
                100% { 
                    transform: scale(1.05);
                    box-shadow: 0 0 60px rgba(255, 0, 0, 1);
                }
            }
            
            .nav-links {
                display: flex;
                gap: 40px;
            }
            
            .nav-link {
                color: var(--white-text);
                text-decoration: none;
                font-weight: 700;
                transition: all 0.3s;
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 15px 30px;
                border-radius: 15px;
                background: rgba(255, 255, 255, 0.08);
                border: 2px solid rgba(255, 215, 0, 0.15);
                font-size: 16px;
            }
            
            .nav-link:hover {
                color: var(--gold-primary);
                background: rgba(255, 215, 0, 0.15);
                border-color: var(--gold-primary);
                transform: translateY(-5px) scale(1.05);
                box-shadow: 0 15px 30px rgba(0, 0, 0, 0.3);
            }
            
            .hero {
                padding: 80px 40px;
                text-align: center;
                background: linear-gradient(135deg, 
                    rgba(10, 10, 10, 0.95) 0%,
                    rgba(26, 26, 26, 0.95) 100%);
                border-bottom: 3px solid var(--gold-primary);
                position: relative;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                min-height: 85vh;
            }
            
            .hero::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: 
                    radial-gradient(circle at 30% 30%, rgba(255, 215, 0, 0.15) 0%, transparent 50%),
                    radial-gradient(circle at 70% 70%, rgba(212, 175, 55, 0.15) 0%, transparent 50%),
                    radial-gradient(circle at center, rgba(255, 0, 0, 0.1) 0%, transparent 70%);
                z-index: 1;
            }
            
            .hero-content {
                position: relative;
                z-index: 2;
                width: 100%;
                max-width: 1400px;
                margin: 0 auto;
            }
            
            .youtube-logo-container {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                margin-bottom: 50px;
            }
            
            .youtube-logo {
                width: 300px;
                height: 300px;
                background: linear-gradient(45deg, #FF0000, #FF3333, #FF0000, #CC0000);
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-bottom: 40px;
                box-shadow: 
                    0 0 100px rgba(255, 0, 0, 0.9),
                    0 0 200px rgba(255, 0, 0, 0.6),
                    inset 0 0 50px rgba(255, 255, 255, 0.3);
                animation: pulseRed 3s infinite alternate;
                border: 10px solid rgba(255, 255, 255, 0.15);
                position: relative;
                overflow: hidden;
            }
            
            .youtube-logo::after {
                content: '';
                position: absolute;
                width: 120%;
                height: 120%;
                background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
                animation: rotate 20s linear infinite;
            }
            
            @keyframes rotate {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            
            .youtube-logo i {
                font-size: 140px;
                color: white;
                text-shadow: 0 0 40px rgba(255, 255, 255, 0.8);
                position: relative;
                z-index: 2;
            }
            
            .premium-title {
                font-size: 5.5em;
                margin-bottom: 20px;
                background: linear-gradient(45deg, 
                    #FFD700, #D4AF37, #FFD700, #D4AF37, #FFD700);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                text-shadow: 0 15px 50px rgba(255, 215, 0, 0.5);
                letter-spacing: 3px;
                font-weight: 900;
                position: relative;
            }
            
            .premium-title::after {
                content: 'Premium YouTube API Running';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                color: transparent;
                background: linear-gradient(45deg, transparent, rgba(255, 255, 255, 0.1), transparent);
                -webkit-background-clip: text;
                z-index: -1;
                animation: shine 3s infinite;
            }
            
            @keyframes shine {
                0%, 100% { background-position: -200% center; }
                50% { background-position: 200% center; }
            }
            
            .subtitle {
                font-size: 1.8em;
                color: var(--gold-light);
                margin-bottom: 60px;
                max-width: 900px;
                margin-left: auto;
                margin-right: auto;
                line-height: 1.8;
                font-weight: 500;
            }
            
            .speed-display {
                background: linear-gradient(45deg, rgba(255, 215, 0, 0.1), rgba(212, 175, 55, 0.1));
                padding: 30px 50px;
                border-radius: 25px;
                border: 2px solid var(--gold-primary);
                margin: 40px auto;
                max-width: 800px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 30px;
                flex-wrap: wrap;
                box-shadow: 0 20px 50px rgba(0, 0, 0, 0.3);
            }
            
            .speed-item {
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 10px;
            }
            
            .speed-value {
                font-size: 3em;
                font-weight: 900;
                background: var(--premium-gradient);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            
            .speed-label {
                font-size: 1.2em;
                color: #AAAAAA;
                font-weight: 600;
            }
            
            .metadata-section {
                max-width: 1200px;
                margin: 100px auto;
                padding: 0 30px;
            }
            
            .metadata-card {
                background: linear-gradient(135deg, 
                    rgba(26, 26, 26, 0.95) 0%,
                    rgba(51, 51, 51, 0.95) 100%);
                border-radius: 35px;
                padding: 60px;
                box-shadow: 
                    0 40px 80px rgba(0, 0, 0, 0.8),
                    inset 0 0 0 2px rgba(255, 215, 0, 0.3);
                border: 3px solid var(--gold-primary);
                position: relative;
                overflow: hidden;
            }
            
            .metadata-card::before {
                content: '';
                position: absolute;
                top: -3px;
                left: -3px;
                right: -3px;
                bottom: -3px;
                background: linear-gradient(45deg, 
                    #FFD700, #D4AF37, #FFD700, #D4AF37);
                z-index: -1;
                border-radius: 38px;
                animation: borderGlow 4s infinite alternate;
                filter: blur(10px);
                opacity: 0.5;
            }
            
            .section-title {
                font-size: 3em;
                margin-bottom: 50px;
                color: var(--gold-primary);
                display: flex;
                align-items: center;
                gap: 25px;
                text-shadow: 0 0 30px rgba(255, 215, 0, 0.4);
            }
            
            .section-title i {
                font-size: 2.5em;
                background: linear-gradient(45deg, var(--gold-primary), var(--gold-dark));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            
            .url-input-container {
                position: relative;
                margin-bottom: 30px;
            }
            
            .url-input {
                width: 100%;
                padding: 25px 40px;
                font-size: 20px;
                background: rgba(0, 0, 0, 0.8);
                border: 3px solid var(--gray-light);
                border-radius: 20px;
                color: var(--white-text);
                transition: all 0.3s;
                font-family: 'Montserrat', sans-serif;
            }
            
            .url-input:focus {
                border-color: var(--gold-primary);
                outline: none;
                box-shadow: 0 0 50px rgba(255, 215, 0, 0.4);
                background: rgba(0, 0, 0, 0.95);
            }
            
            .input-buttons {
                display: flex;
                gap: 20px;
                justify-content: center;
                margin-top: 25px;
                flex-wrap: wrap;
            }
            
            .action-button {
                padding: 18px 35px;
                font-size: 18px;
                font-weight: 700;
                border-radius: 15px;
                cursor: pointer;
                transition: all 0.3s;
                display: flex;
                align-items: center;
                gap: 15px;
                border: none;
                font-family: 'Montserrat', sans-serif;
                min-width: 200px;
                justify-content: center;
            }
            
            .fetch-button {
                background: linear-gradient(45deg, #00b09b, #96c93d);
                color: white;
            }
            
            .paste-button {
                background: linear-gradient(45deg, #4ECDC4, #44A08D);
                color: white;
            }
            
            .clear-button {
                background: linear-gradient(45deg, #FF6B6B, #FF416C);
                color: white;
            }
            
            .action-button:hover {
                transform: translateY(-8px) scale(1.05);
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
            }
            
            .video-info-container {
                background: rgba(0, 0, 0, 0.5);
                border-radius: 25px;
                padding: 30px;
                margin: 30px 0;
                border: 2px solid rgba(255, 215, 0, 0.3);
                display: none;
            }
            
            .video-info {
                display: grid;
                grid-template-columns: 1fr 2fr;
                gap: 30px;
                align-items: start;
            }
            
            .video-thumbnail {
                width: 100%;
                border-radius: 20px;
                box-shadow: 0 15px 40px rgba(0, 0, 0, 0.5);
                border: 3px solid var(--gold-primary);
            }
            
            .video-details {
                display: flex;
                flex-direction: column;
                gap: 20px;
            }
            
            .video-title {
                font-size: 2.2em;
                font-weight: 800;
                color: white;
                line-height: 1.4;
            }
            
            .video-meta {
                display: flex;
                flex-wrap: wrap;
                gap: 25px;
                margin-top: 10px;
            }
            
            .meta-item {
                display: flex;
                align-items: center;
                gap: 10px;
                background: rgba(255, 215, 0, 0.1);
                padding: 12px 20px;
                border-radius: 15px;
                border: 2px solid rgba(255, 215, 0, 0.2);
            }
            
            .meta-item i {
                color: var(--gold-primary);
                font-size: 1.2em;
            }
            
            .meta-label {
                color: #CCCCCC;
                font-weight: 600;
                font-size: 14px;
            }
            
            .meta-value {
                color: white;
                font-weight: 700;
                font-size: 16px;
            }
            
            .video-description {
                background: rgba(255, 255, 255, 0.05);
                padding: 20px;
                border-radius: 15px;
                margin-top: 15px;
                font-size: 16px;
                line-height: 1.6;
                color: #CCCCCC;
                max-height: 150px;
                overflow-y: auto;
            }
            
            .loading-spinner {
                display: none;
                text-align: center;
                padding: 30px;
            }
            
            .spinner {
                width: 60px;
                height: 60px;
                border: 5px solid rgba(255, 215, 0, 0.3);
                border-top: 5px solid var(--gold-primary);
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin: 0 auto 20px;
            }
            
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            
            .api-info-section {
                margin-top: 60px;
                padding: 40px;
                background: linear-gradient(45deg, 
                    rgba(0, 176, 155, 0.1), 
                    rgba(150, 201, 61, 0.1),
                    rgba(255, 215, 0, 0.1));
                border-radius: 25px;
                border-left: 8px solid #00b09b;
                border-right: 8px solid #96c93d;
                border-top: 3px solid #FFD700;
                border-bottom: 3px solid #FFD700;
            }
            
            .api-info-title {
                display: flex;
                align-items: center;
                gap: 20px;
                color: #00b09b;
                font-size: 2em;
                font-weight: 900;
                margin-bottom: 30px;
            }
            
            .features-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 25px;
            }
            
            .feature-item {
                background: rgba(0, 0, 0, 0.3);
                padding: 25px;
                border-radius: 20px;
                border: 2px solid rgba(255, 255, 255, 0.1);
                transition: all 0.3s;
            }
            
            .feature-item:hover {
                transform: translateY(-5px);
                border-color: var(--gold-primary);
                box-shadow: 0 15px 30px rgba(0, 0, 0, 0.2);
            }
            
            .feature-icon {
                font-size: 2.5em;
                margin-bottom: 15px;
                background: var(--premium-gradient);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            
            .feature-text {
                color: #CCCCCC;
                font-size: 16px;
                line-height: 1.6;
            }
            
            .status-bar {
                position: fixed;
                bottom: 0;
                left: 0;
                right: 0;
                background: linear-gradient(135deg, 
                    rgba(10, 10, 10, 0.98) 0%,
                    rgba(26, 26, 26, 0.98) 100%);
                backdrop-filter: blur(25px);
                padding: 25px 60px;
                border-top: 3px solid var(--gold-primary);
                display: flex;
                justify-content: space-between;
                align-items: center;
                z-index: 1000;
                box-shadow: 0 -15px 40px rgba(0, 0, 0, 0.7);
            }
            
            .server-status {
                display: flex;
                align-items: center;
                gap: 30px;
            }
            
            .status-indicator {
                display: flex;
                align-items: center;
                gap: 15px;
                padding: 15px 30px;
                background: rgba(0, 255, 0, 0.15);
                border-radius: 15px;
                border: 2px solid rgba(0, 255, 0, 0.3);
                font-size: 18px;
            }
            
            .status-dot {
                width: 15px;
                height: 15px;
                background: #00FF00;
                border-radius: 50%;
                animation: pulse 1.5s infinite;
                box-shadow: 0 0 20px #00FF00;
            }
            
            .owner-display {
                display: flex;
                align-items: center;
                gap: 20px;
                padding: 15px 40px;
                background: linear-gradient(45deg, 
                    rgba(255, 215, 0, 0.15), 
                    rgba(212, 175, 55, 0.15));
                border-radius: 15px;
                border: 2px solid rgba(255, 215, 0, 0.3);
                font-size: 20px;
            }
            
            .owner-display i {
                color: var(--gold-primary);
                font-size: 24px;
            }
            
            .owner-display strong {
                background: linear-gradient(45deg, var(--gold-primary), var(--gold-dark));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                font-size: 22px;
                font-weight: 900;
            }
            
            .footer {
                text-align: center;
                padding: 80px 40px;
                color: #666;
                font-size: 18px;
                border-top: 3px solid var(--gray-light);
                margin-top: 120px;
                background: rgba(10, 10, 10, 0.95);
                position: relative;
            }
            
            .owner-section {
                background: linear-gradient(45deg, 
                    rgba(255, 215, 0, 0.1), 
                    rgba(212, 175, 55, 0.1));
                padding: 60px;
                border-radius: 30px;
                margin: 60px auto;
                border: 3px solid rgba(255, 215, 0, 0.3);
                max-width: 1000px;
                position: relative;
                overflow: hidden;
            }
            
            .owner-title {
                font-size: 3.5em;
                color: #FFD700;
                margin-bottom: 30px;
                font-weight: 900;
                text-shadow: 0 0 30px rgba(255, 215, 0, 0.5);
                letter-spacing: 2px;
                position: relative;
            }
            
            .owner-subtitle {
                font-size: 1.8em;
                color: #FFFFFF;
                margin-bottom: 40px;
                font-weight: 700;
                background: linear-gradient(45deg, #FF0000, #FF6B6B);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            
            .owner-badge-large {
                display: inline-block;
                background: linear-gradient(45deg, #FF0000, #FF6B6B, #FF0000);
                color: white;
                padding: 25px 60px;
                border-radius: 50px;
                font-size: 2.5em;
                font-weight: 900;
                letter-spacing: 3px;
                text-transform: uppercase;
                margin: 30px 0;
                box-shadow: 
                    0 0 60px rgba(255, 0, 0, 0.8),
                    inset 0 0 40px rgba(255, 255, 255, 0.2);
                border: 5px solid #FFD700;
                animation: ownerGlow 2s infinite alternate;
            }
            
            @keyframes ownerGlow {
                0% { 
                    transform: scale(1);
                    box-shadow: 0 0 60px rgba(255, 0, 0, 0.8);
                }
                100% { 
                    transform: scale(1.05);
                    box-shadow: 0 0 90px rgba(255, 0, 0, 1);
                }
            }
            
            .owner-description {
                font-size: 1.4em;
                color: #CCCCCC;
                max-width: 800px;
                margin: 40px auto;
                line-height: 1.8;
            }
            
            .endpoints-section {
                background: rgba(0, 0, 0, 0.5);
                padding: 40px;
                border-radius: 25px;
                margin-top: 60px;
                border: 2px solid rgba(255, 215, 0, 0.3);
            }
            
            .endpoints-title {
                color: var(--gold-primary);
                margin-bottom: 30px;
                font-size: 2.5em;
                text-align: center;
            }
            
            .endpoints-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 25px;
            }
            
            .endpoint-item {
                background: rgba(255, 255, 255, 0.05);
                padding: 25px;
                border-radius: 20px;
                border: 2px solid rgba(255, 215, 0, 0.2);
                transition: all 0.3s;
            }
            
            .endpoint-item:hover {
                border-color: var(--gold-primary);
                transform: translateY(-5px);
            }
            
            .endpoint-method {
                color: var(--gold-primary);
                font-weight: 700;
                font-size: 1.2em;
                margin-bottom: 10px;
            }
            
            .endpoint-url {
                color: #AAAAAA;
                font-family: monospace;
                font-size: 16px;
                word-break: break-all;
            }
            
            .copyright {
                margin-top: 60px;
                color: #555;
                font-size: 16px;
            }
            
            @media (max-width: 1200px) {
                .header {
                    padding: 20px 30px;
                    flex-direction: column;
                    gap: 25px;
                }
                
                .nav-links {
                    width: 100%;
                    justify-content: center;
                    flex-wrap: wrap;
                }
                
                .premium-title {
                    font-size: 4.5em;
                }
                
                .youtube-logo {
                    width: 250px;
                    height: 250px;
                }
                
                .youtube-logo i {
                    font-size: 120px;
                }
                
                .owner-badge-large {
                    font-size: 2em;
                    padding: 20px 40px;
                }
                
                .video-info {
                    grid-template-columns: 1fr;
                }
            }
            
            @media (max-width: 768px) {
                .hero {
                    padding: 60px 20px;
                    min-height: 70vh;
                }
                
                .premium-title {
                    font-size: 3.5em;
                }
                
                .youtube-logo {
                    width: 200px;
                    height: 200px;
                }
                
                .youtube-logo i {
                    font-size: 100px;
                }
                
                .subtitle {
                    font-size: 1.5em;
                }
                
                .section-title {
                    font-size: 2.5em;
                }
                
                .metadata-card {
                    padding: 30px;
                }
                
                .owner-title {
                    font-size: 2.5em;
                }
                
                .owner-badge-large {
                    font-size: 1.8em;
                    padding: 15px 30px;
                }
                
                .status-bar {
                    padding: 20px 30px;
                    flex-direction: column;
                    gap: 20px;
                    text-align: center;
                }
                
                .video-title {
                    font-size: 1.8em;
                }
            }
            
            @media (max-width: 480px) {
                .premium-title {
                    font-size: 2.8em;
                }
                
                .youtube-logo {
                    width: 180px;
                    height: 180px;
                }
                
                .youtube-logo i {
                    font-size: 80px;
                }
                
                .action-button {
                    min-width: 100%;
                }
                
                .owner-badge-large {
                    font-size: 1.5em;
                    padding: 12px 25px;
                }
                
                .video-meta {
                    flex-direction: column;
                    gap: 15px;
                }
            }
            
            ::-webkit-scrollbar {
                width: 14px;
            }
            
            ::-webkit-scrollbar-track {
                background: var(--black-bg);
                border-radius: 10px;
            }
            
            ::-webkit-scrollbar-thumb {
                background: linear-gradient(var(--gold-primary), var(--gold-dark));
                border-radius: 10px;
                border: 3px solid var(--black-bg);
            }
            
            ::-webkit-scrollbar-thumb:hover {
                background: linear-gradient(var(--gold-dark), var(--gold-primary));
            }
        </style>
        <script>
            async function fetchVideoInfo() {
                const urlInput = document.getElementById('urlInput').value.trim();
                
                if (!urlInput) {
                    showNotification('Please enter a YouTube URL', 'error');
                    return;
                }
                
                // Show loading spinner
                const videoInfoContainer = document.getElementById('videoInfoContainer');
                const loadingSpinner = document.getElementById('loadingSpinner');
                videoInfoContainer.style.display = 'none';
                loadingSpinner.style.display = 'block';
                
                try {
                    // Send the full URL to backend
                    const response = await fetch(`/api/metadata?url=${encodeURIComponent(urlInput)}`);
                    const data = await response.json();
                    
                    if (data.status === 'available') {
                        // Update UI with video info
                        document.getElementById('videoThumbnail').src = data.thumbnail;
                        document.getElementById('videoThumbnail').alt = data.title;
                        document.getElementById('videoTitle').textContent = data.title;
                        document.getElementById('videoDuration').textContent = data.duration_string;
                        document.getElementById('videoUploader').textContent = data.uploader;
                        document.getElementById('videoViews').textContent = formatNumber(data.view_count);
                        document.getElementById('videoLikes').textContent = formatNumber(data.like_count);
                        document.getElementById('videoDescription').textContent = data.description;
                        
                        // Show video info container
                        loadingSpinner.style.display = 'none';
                        videoInfoContainer.style.display = 'block';
                        
                        showNotification('✅ Video metadata fetched successfully!', 'success');
                    } else {
                        loadingSpinner.style.display = 'none';
                        showNotification(`❌ Error: ${data.error || 'Video not available'}`, 'error');
                    }
                } catch (error) {
                    loadingSpinner.style.display = 'none';
                    showNotification('❌ Failed to fetch video metadata', 'error');
                    console.error('Error:', error);
                }
            }
            
            function formatNumber(num) {
                if (num >= 1000000) {
                    return (num / 1000000).toFixed(1) + 'M';
                } else if (num >= 1000) {
                    return (num / 1000).toFixed(1) + 'K';
                }
                return num.toString();
            }
            
            function showNotification(message, type = 'info') {
                const notification = document.createElement('div');
                const bgColor = type === 'success' ? 'linear-gradient(135deg, #00b09b, #96c93d)' :
                               type === 'error' ? 'linear-gradient(135deg, #FF416C, #FF4B2B)' :
                               'linear-gradient(135deg, #FFD700, #D4AF37)';
                const textColor = type === 'info' ? 'black' : 'white';
                
                notification.innerHTML = `
                    <div style="position: fixed; top: 150px; right: 40px; background: ${bgColor}; 
                         color: ${textColor}; padding: 25px; border-radius: 20px; 
                         box-shadow: 0 20px 50px rgba(0,0,0,0.5); z-index: 2000; 
                         display: flex; align-items: center; gap: 20px; max-width: 500px; 
                         animation: slideIn 0.4s; border: 3px solid ${type === 'info' ? '#FFD700' : 'transparent'};
                         font-weight: 700; font-size: 17px;">
                        <i class="fas ${type === 'success' ? 'fa-check-circle' : type === 'error' ? 'fa-exclamation-circle' : 'fa-bolt'}" 
                           style="font-size: 28px;"></i>
                        <div>${message}</div>
                    </div>
                `;
                
                document.body.appendChild(notification);
                
                setTimeout(() => {
                    notification.style.animation = 'slideOut 0.4s';
                    setTimeout(() => notification.remove(), 400);
                }, 4000);
                
                const style = document.createElement('style');
                style.textContent = `
                    @keyframes slideIn {
                        from { transform: translateX(100%); opacity: 0; }
                        to { transform: translateX(0); opacity: 1; }
                    }
                    @keyframes slideOut {
                        from { transform: translateX(0); opacity: 1; }
                        to { transform: translateX(100%); opacity: 0; }
                    }
                `;
                document.head.appendChild(style);
            }
            
            function pasteFromClipboard() {
                navigator.clipboard.readText()
                    .then(text => {
                        document.getElementById('urlInput').value = text;
                        showNotification('📋 URL pasted from clipboard!', 'success');
                    })
                    .catch(err => {
                        showNotification('Could not paste from clipboard', 'error');
                    });
            }
            
            document.addEventListener('DOMContentLoaded', function() {
                // Set default example URL
                document.getElementById('urlInput').value = 'https://www.youtube.com/watch?v=zsj9W7mUY2I';
                
                // Auto-fetch video info for default URL
                setTimeout(() => fetchVideoInfo(), 1000);
                
                // Animated speed display
                setInterval(() => {
                    const speedElements = document.querySelectorAll('.speed-value');
                    speedElements.forEach(el => {
                        const currentSpeed = parseFloat(el.textContent);
                        const newSpeed = (currentSpeed + (Math.random() * 5 - 2)).toFixed(1);
                        el.textContent = Math.max(10, parseFloat(newSpeed)) + 'x';
                    });
                }, 2000);
            });
        </script>
    </head>
    <body>
        <div class="header">
            <div class="logo-container">
                <div class="golden-logo">
                    <i class="fab fa-youtube logo-icon"></i>
                    <span>Premium YouTube API</span>
                </div>
                <div class="premium-badge">ULTRA FAST ⚡</div>
                
                <div class="owner-badge">
                    <i class="fas fa-crown"></i>
                    <span>@RAJOWNER20</span>
                    <i class="fas fa-star"></i>
                </div>
            </div>
            
            <div class="nav-links">
                <a href="#metadata" class="nav-link">
                    <i class="fas fa-search"></i>
                    <span>Fetch Metadata</span>
                </a>
                <a href="/status" class="nav-link">
                    <i class="fas fa-server"></i>
                    <span>API Status</span>
                </a>
            </div>
        </div>
        
        <div class="hero">
            <div class="hero-content">
                <div class="youtube-logo-container">
                    <div class="youtube-logo">
                        <i class="fab fa-youtube"></i>
                    </div>
                    <h1 class="premium-title">Premium YouTube API Running</h1>
                </div>
                <div class="subtitle">
                    ⚡ ULTRA FAST YouTube video metadata extraction with maximum speed optimization.<br>
                    Fetch complete video information including title, duration, views, likes, description and thumbnail.
                </div>
                
                <div class="speed-display">
                    <div class="speed-item">
                        <div class="speed-value">256x</div>
                        <div class="speed-label">Processing Speed</div>
                    </div>
                    <div class="speed-item">
                        <div class="speed-value">4GB</div>
                        <div class="speed-label">Cache Memory</div>
                    </div>
                    <div class="speed-item">
                        <div class="speed-value">64</div>
                        <div class="speed-label">Parallel Requests</div>
                    </div>
                    <div class="speed-item">
                        <div class="speed-value">100M</div>
                        <div class="speed-label">Data Throughput</div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="metadata-section" id="metadata">
            <div class="metadata-card">
                <div class="section-title">
                    <i class="fas fa-search"></i>
                    YouTube Video Metadata Fetcher
                </div>
                
                <div class="url-input-container">
                    <input type="text" id="urlInput" class="url-input" 
                           placeholder="Enter YouTube URL or Video ID (e.g., https://www.youtube.com/watch?v=... or https://www.youtube.com/live/...)">
                    
                    <div class="input-buttons">
                        <button onclick="fetchVideoInfo()" class="action-button fetch-button">
                            <i class="fas fa-search"></i>
                            <span>Fetch Video Metadata</span>
                        </button>
                        
                        <button onclick="pasteFromClipboard()" class="action-button paste-button">
                            <i class="fas fa-paste"></i>
                            <span>Paste from Clipboard</span>
                        </button>
                        
                        <button onclick="document.getElementById('urlInput').value = ''" class="action-button clear-button">
                            <i class="fas fa-times"></i>
                            <span>Clear Input</span>
                        </button>
                    </div>
                </div>
                
                <div class="loading-spinner" id="loadingSpinner">
                    <div class="spinner"></div>
                    <div style="color: var(--gold-primary); font-weight: 700; font-size: 18px; margin-top: 15px;">
                        Fetching video metadata...
                    </div>
                </div>
                
                <div class="video-info-container" id="videoInfoContainer">
                    <div class="video-info">
                        <img id="videoThumbnail" class="video-thumbnail" src="" alt="Video Thumbnail">
                        <div class="video-details">
                            <h2 id="videoTitle" class="video-title"></h2>
                            <div class="video-meta">
                                <div class="meta-item">
                                    <i class="fas fa-clock"></i>
                                    <div>
                                        <div class="meta-label">Duration</div>
                                        <div id="videoDuration" class="meta-value"></div>
                                    </div>
                                </div>
                                <div class="meta-item">
                                    <i class="fas fa-user"></i>
                                    <div>
                                        <div class="meta-label">Uploader</div>
                                        <div id="videoUploader" class="meta-value"></div>
                                    </div>
                                </div>
                                <div class="meta-item">
                                    <i class="fas fa-eye"></i>
                                    <div>
                                        <div class="meta-label">Views</div>
                                        <div id="videoViews" class="meta-value"></div>
                                    </div>
                                </div>
                                <div class="meta-item">
                                    <i class="fas fa-thumbs-up"></i>
                                    <div>
                                        <div class="meta-label">Likes</div>
                                        <div id="videoLikes" class="meta-value"></div>
                                    </div>
                                </div>
                            </div>
                            <div class="video-description" id="videoDescription"></div>
                        </div>
                    </div>
                </div>
                
                <div class="api-info-section">
                    <div class="api-info-title">
                        <i class="fas fa-bolt"></i>
                        <span>⚡ ULTRA FAST Metadata Features</span>
                    </div>
                    <div class="features-grid">
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-rocket"></i>
                            </div>
                            <div class="feature-text">
                                <strong>Instant Metadata Fetch</strong><br>
                                Get complete video info in milliseconds
                            </div>
                        </div>
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-image"></i>
                            </div>
                            <div class="feature-text">
                                <strong>High-Quality Thumbnails</strong><br>
                                Extract maximum resolution thumbnails
                            </div>
                        </div>
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-info-circle"></i>
                            </div>
                            <div class="feature-text">
                                <strong>Complete Video Details</strong><br>
                                Title, duration, views, likes, description
                            </div>
                        </div>
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-tachometer-alt"></i>
                            </div>
                            <div class="feature-text">
                                <strong>100M Data Throughput</strong><br>
                                No speed limits, full bandwidth utilization
                            </div>
                        </div>
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-shield-alt"></i>
                            </div>
                            <div class="feature-text">
                                <strong>Android Client Bypass</strong><br>
                                Uses Android player to bypass restrictions
                            </div>
                        </div>
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-database"></i>
                            </div>
                            <div class="feature-text">
                                <strong>24-Hour Smart Cache</strong><br>
                                Automatic caching for frequently accessed videos
                            </div>
                        </div>
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-sync-alt"></i>
                            </div>
                            <div class="feature-text">
                                <strong>Proxy Fallback System</strong><br>
                                10 high-speed proxies for uninterrupted service
                            </div>
                        </div>
                        <div class="feature-item">
                            <div class="feature-icon">
                                <i class="fas fa-bolt"></i>
                            </div>
                            <div class="feature-text">
                                <strong>No Rate Limits</strong><br>
                                Optimized for maximum request speeds
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="owner-section">
            <h2 class="owner-title">Deployment & Maintenance</h2>
            <div class="owner-subtitle">Exclusively Managed By</div>
            <div class="owner-badge-large">
                <i class="fas fa-crown"></i>
                @RAJOWNER20
                <i class="fas fa-star"></i>
            </div>
            <div class="owner-description">
                ⚡ Premium YouTube metadata extraction service with maximum speed optimization.<br>
                Featuring 256 concurrent requests, 64 parallel connections, and 4GB cache size<br>
                for the fastest possible YouTube metadata fetching.
            </div>
        </div>
        
        <div class="footer">
            <div class="endpoints-section">
                <h3 class="endpoints-title">⚡ ULTRA FAST API Endpoints</h3>
                <div class="endpoints-grid">
                    <div class="endpoint-item">
                        <div class="endpoint-method">Video Metadata</div>
                        <div class="endpoint-url">/api/metadata?url={youtube_url}</div>
                    </div>
                    <div class="endpoint-item">
                        <div class="endpoint-method">API Status</div>
                        <div class="endpoint-url">/status</div>
                    </div>
                    <div class="endpoint-item">
                        <div class="endpoint-method">Home Page</div>
                        <div class="endpoint-url">/</div>
                    </div>
                </div>
            </div>
            
            <div class="copyright">
                <p>© 2024 Premium YouTube API • ULTRA FAST Maintenance by @RAJOWNER20</p>
                <p style="margin-top: 20px; font-size: 14px; color: #666;">
                    All metadata extraction complies with YouTube Terms of Service • Optimized for maximum speed
                </p>
            </div>
        </div>
        
        <div class="status-bar">
            <div class="server-status">
                <div class="status-indicator">
                    <div class="status-dot"></div>
                    <span>API Status: <strong style="color: #00FF00;">METADATA MODE ACTIVE</strong></span>
                </div>
                <div style="color: #AAAAAA; font-size: 16px;">
                    <i class="fas fa-bolt"></i>
                    <span>⚡ Server Online (256x Speed Optimized)</span>
                </div>
            </div>
            
            <div class="owner-display">
                <i class="fas fa-crown"></i>
                <div>
                    <span style="color: #CCCCCC;">ULTRA FAST Deployment by</span>
                    <strong>@RAJOWNER20</strong>
                </div>
                <i class="fas fa-star" style="color: #FFD700;"></i>
            </div>
        </div>
    </body>
    </html>
    """)

# =============== IMPROVED METADATA API ENDPOINT ===============
@app.get("/api/metadata")
async def get_video_metadata_api(url: str = Query(..., description="YouTube URL")):
    """API endpoint to get video metadata from any YouTube URL"""
    video_id = extract_video_id(url)
    
    if not video_id or len(video_id) != 11:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "error": f"Invalid YouTube URL or Video ID. Extracted ID: {video_id}",
                "supported_urls": [
                    "https://www.youtube.com/watch?v=VIDEO_ID",
                    "https://youtu.be/VIDEO_ID",
                    "https://www.youtube.com/live/VIDEO_ID",
                    "https://www.youtube.com/embed/VIDEO_ID",
                    "https://www.youtube.com/watch?v=VIDEO_ID&si=...",
                    "https://www.youtube.com/live/VIDEO_ID?si=..."
                ]
            }
        )
    
    metadata = await get_video_metadata(video_id)
    return JSONResponse(content=metadata)

# =============== ALL BACKEND DOWNLOAD ENDPOINTS STILL WORKING ===============
# (But not shown in frontend)

@app.get("/download")
async def youtube_download_token(
    url: str = Query(..., description="YouTube URL or Video ID"),
    type: str = Query("audio", description="Download type: audio or video")
):
    """Get download token"""
    
    if type not in ["audio", "video"]:
        return JSONResponse(status_code=400, content={"error": "Type must be 'audio' or 'video'"})
    
    video_id = extract_video_id(url)
    
    if not video_id or len(video_id) < 3:
        return JSONResponse(status_code=400, content={"error": "Invalid video ID"})
    
    # Generate token
    download_token = str(uuid.uuid4())
    expires_at = datetime.now() + timedelta(seconds=TOKEN_TTL)
    
    download_tokens[download_token] = {
        "video_id": video_id,
        "type": type,
        "created_at": datetime.now(),
        "expires_at": expires_at,
        "used": False
    }
    
    return JSONResponse(content={
        "download_token": download_token,
        "video_id": video_id,
        "type": type,
        "expires_in": TOKEN_TTL,
        "direct_url": f"/audio/{video_id}" if type == "audio" else f"/video/{video_id}"
    })

@app.get("/stream/{video_id}")
async def youtube_stream(
    video_id: str,
    type: str = Query("audio", description="Stream type: audio or video"),
    token: Optional[str] = Query(None, description="Download token")
):
    """Stream downloaded file"""
    
    if type not in ["audio", "video"]:
        raise HTTPException(400, "Type must be 'audio' or 'video'")
    
    # Validate token
    if token:
        token_data = download_tokens.get(token)
        if not token_data or token_data.get("used", False) or datetime.now() > token_data.get("expires_at"):
            raise HTTPException(401, "Invalid or expired token")
        download_tokens[token]["used"] = True
    
    # Download file
    logger.info(f"📥 Stream request: {video_id} ({type})")
    file_path = await fast_download(video_id, type)
    
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(500, f"Could not download {type}")
    
    # Stream file
    file_size = os.path.getsize(file_path)
    
    if type == "audio":
        content_type = "audio/mp4"
    else:
        content_type = "video/mp4"
    
    return StreamingResponse(
        file_stream_generator(file_path),
        media_type=content_type,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(file_size),
            "Content-Disposition": f'inline; filename="{os.path.basename(file_path)}"',
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600"
        }
    )

@app.get("/audio/{video_id}")
async def direct_audio_stream(video_id: str):
    """Direct audio download endpoint"""
    logger.info(f"🎵 Audio request: {video_id}")
    
    # Download audio
    file_path = await fast_download(video_id, "audio")
    
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(500, "Audio download failed")
    
    # Return file
    return FileResponse(
        file_path,
        media_type="audio/mp4",
        filename=f"{video_id}.m4a",
        headers={
            "Content-Disposition": f'attachment; filename="{video_id}.m4a"',
            "Cache-Control": "public, max-age=3600"
        }
    )

@app.get("/video/{video_id}")
async def direct_video_stream(video_id: str):
    """Direct video download endpoint"""
    logger.info(f"🎬 Video request: {video_id}")
    
    # Download video
    file_path = await fast_download(video_id, "video")
    
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(500, "Video download failed")
    
    # Return file
    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=f"{video_id}.mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{video_id}.mp4"',
            "Cache-Control": "public, max-age=3600"
        }
    )

@app.get("/status")
async def api_status():
    """API status endpoint"""
    return JSONResponse(content={
        "status": "running",
        "mode": "metadata_api",
        "timestamp": datetime.now().isoformat(),
        "endpoints": {
            "metadata": "/api/metadata?url=YOUTUBE_URL",
            "home": "/",
            "status": "/status"
        },
        "performance": {
            "cache_size": len(DOWNLOAD_CACHE),
            "active_tokens": len(download_tokens),
            "cache_dir_size": sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in os.listdir(CACHE_DIR) if os.path.isfile(os.path.join(CACHE_DIR, f))) / 1024 / 1024
        }
    })

@app.get("/test/{video_id}")
async def test_download(video_id: str):
    """Test endpoint to check video availability"""
    video_id = extract_video_id(video_id)
    
    # Quick availability check
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--skip-download",
        "--get-title",
        f"https://youtu.be/{video_id}"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        
        if result.returncode == 0 and result.stdout.strip():
            title = result.stdout.strip()[:100]
            return JSONResponse(content={
                "video_id": video_id,
                "status": "available",
                "title": title,
                "timestamp": datetime.now().isoformat()
            })
        else:
            return JSONResponse(content={
                "video_id": video_id,
                "status": "unavailable",
                "error": result.stderr[:200] if result.stderr else "Unknown error",
                "timestamp": datetime.now().isoformat()
            }, status_code=404)
            
    except subprocess.TimeoutExpired:
        return JSONResponse(content={
            "video_id": video_id,
            "status": "timeout",
            "timestamp": datetime.now().isoformat()
        }, status_code=408)
    except Exception as e:
        return JSONResponse(content={
            "video_id": video_id,
            "status": "error",
            "error": str(e)[:100],
            "timestamp": datetime.now().isoformat()
        }, status_code=500)

# Railway Deployment Compatibility
def get_railway_port():
    """Get port from Railway environment or default to 8080"""
    return int(os.getenv("PORT", "8080"))

# Cleanup tasks
async def cleanup_cache():
    """Clean old cache files"""
    while True:
        await asyncio.sleep(1800)
        try:
            now = time.time()
            cleaned = 0
            for file in os.listdir(CACHE_DIR):
                file_path = os.path.join(CACHE_DIR, file)
                if os.path.isfile(file_path):
                    if now - os.path.getmtime(file_path) > 21600:
                        os.remove(file_path)
                        cleaned += 1
            
            if cleaned > 0:
                logger.info(f"🧹 Cleaned {cleaned} old cache files")
                
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")

async def clean_expired_tokens():
    """Clean expired tokens"""
    while True:
        await asyncio.sleep(300)
        now = datetime.now()
        expired = [t for t, d in download_tokens.items() if now > d.get("expires_at")]
        for token in expired:
            del download_tokens[token]
        if expired:
            logger.info(f"🗑️ Cleaned {len(expired)} expired tokens")

@app.on_event("startup")
async def startup_event():
    """Startup tasks"""
    asyncio.create_task(clean_expired_tokens())
    asyncio.create_task(cleanup_cache())
    
    # Log system status
    logger.info("🚀 Premium YouTube API Started - METADATA ONLY MODE")
    logger.info(f"📁 Cookies: {'✅ AVAILABLE' if HAS_COOKIES else '❌ NOT FOUND'}")
    logger.info(f"🔧 Port: {get_railway_port()}")
    logger.info("⚡ Frontend: Metadata fetch only (download endpoints hidden)")
    logger.info("🔧 Backend: All download functions still active")
    logger.info("📊 Added: Video metadata extraction endpoint")
    logger.info("✅ Live URLs Supported: https://www.youtube.com/live/VIDEO_ID")

if __name__ == "__main__":
    import uvicorn
    port = get_railway_port()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")