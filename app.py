from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import FileResponse, JSONResponse
import asyncio
import os
import time
import shutil
import re
import glob
import secrets
import json
from typing import Dict, Optional

app = FastAPI()

# =========================================================
# CONFIG
# =========================================================

BASE_DIR = os.getenv("APP_DIR", "/tmp")
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(BASE_DIR, "cache"))
COOKIES_FILE = os.getenv("COOKIES_FILE", os.path.join(BASE_DIR, "cookies.txt"))
os.makedirs(CACHE_DIR, exist_ok=True)

# Keep this reasonable.
# 1000 simultaneous downloads can overload the VPS/network.
DOWNLOAD_WORKERS = asyncio.Semaphore(int(os.getenv("DOWNLOAD_WORKERS", "100")))

ACTIVE_DOWNLOADS: Dict[str, asyncio.Lock] = {}

RAM_CACHE = {
    "audio": {},
    "video": {},
    "info": {}
}

TOKENS = {}

TOKEN_EXPIRY = 1200  # 20 minutes




# =========================================================
# YT-DLP COMMON OPTIONS
# =========================================================

YTDLP_COMMON = [
    "--force-ipv4",

    "--js-runtimes",
    "deno",

    "--remote-components",
    "ejs:github",

    "--no-playlist",

    "--socket-timeout",
    "15",

    "--retries",
    "3",

    "--buffer-size",
    "16K",

    "--http-chunk-size",
    "10M",
]


if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
    RUNTIME_COOKIES = "/tmp/annie_cookies.txt"
    shutil.copy2(COOKIES_FILE, RUNTIME_COOKIES)
    YTDLP_COMMON += ["--cookies", RUNTIME_COOKIES]

# =========================================================
# UTIL
# =========================================================

def log(msg):
    print(msg, flush=True)


def extract_video_id(text: str):

    if re.match(r"^[a-zA-Z0-9_-]{11}$", text):
        return text

    match = re.search(
        r"(?:v=|youtu\.be/|shorts/|live/)([a-zA-Z0-9_-]{11})",
        text
    )

    return match.group(1) if match else text


def cleanup_tokens():

    now = time.time()

    expired = [
        token
        for token, data in TOKENS.items()
        if now - data.get("time", 0) > TOKEN_EXPIRY
    ]

    for token in expired:
        TOKENS.pop(token, None)


def validate_token(
    token: Optional[str],
    video_id: str,
    media_type: str
):

    cleanup_tokens()

    if not token:
        raise HTTPException(
            status_code=403,
            detail="Invalid token"
        )

    data = TOKENS.get(token)

    if not data:
        raise HTTPException(
            status_code=403,
            detail="Invalid or expired token"
        )

    if data.get("video_id") != video_id:
        raise HTTPException(
            status_code=403,
            detail="Token mismatch"
        )

    if data.get("type") != media_type:
        raise HTTPException(
            status_code=403,
            detail="Token mismatch"
        )

    if time.time() - data.get("time", 0) > TOKEN_EXPIRY:

        TOKENS.pop(token, None)

        raise HTTPException(
            status_code=403,
            detail="Token expired"
        )

    return data


def format_duration(seconds):

    if not seconds or seconds == 0:
        return "0:00"

    try:

        seconds = int(seconds)

        hours = seconds // 3600

        minutes = (
            seconds % 3600
        ) // 60

        secs = seconds % 60

        if hours > 0:
            return (
                f"{hours}:"
                f"{minutes:02d}:"
                f"{secs:02d}"
            )

        return (
            f"{minutes}:"
            f"{secs:02d}"
        )

    except Exception:
        return "0:00"


async def run_cmd_with_output(cmd):

    process = await asyncio.create_subprocess_exec(
        *cmd,

        stdout=asyncio.subprocess.PIPE,

        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    return (
        process.returncode == 0,
        stdout,
        stderr
    )


async def run_cmd(cmd):

    process = await asyncio.create_subprocess_exec(
        *cmd,

        stdout=asyncio.subprocess.DEVNULL,

        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if stderr:

        print(
            stderr.decode(
                errors="ignore"
            ),
            flush=True
        )

    return process.returncode == 0


def calc_speed(size_mb, seconds):

    if seconds < 0.01:
        return "instant"

    return (
        f"{size_mb / seconds:.2f} MB/s"
    )


def cleanup_temp_files(video_id):

    patterns = [
        f"{CACHE_DIR}/{video_id}_temp.*",
        f"{CACHE_DIR}/{video_id}_temp*",
    ]

    removed = set()

    for pattern in patterns:

        for path in glob.glob(pattern):

            if path in removed:
                continue

            removed.add(path)

            try:

                if os.path.isfile(path):

                    os.remove(path)

                    log(
                        f"🧹 Removed temp: {path}"
                    )

            except Exception as e:

                log(
                    f"⚠️ Temp cleanup failed: {e}"
                )


# =========================================================
# VIDEO INFO
# =========================================================

@app.get("/info/{video_id}")
async def get_video_info(video_id: str):

    video_id = extract_video_id(video_id)

    log(
        f"📊 Info request: {video_id}"
    )

    # -----------------------------------------------------
    # RAM CACHE
    # -----------------------------------------------------

    if video_id in RAM_CACHE["info"]:

        cached_time, cached_data = (
            RAM_CACHE["info"][video_id]
        )

        if time.time() - cached_time < 3600:

            log(
                f"⚡ Info from RAM cache: "
                f"{video_id}"
            )

            return cached_data

        RAM_CACHE["info"].pop(
            video_id,
            None
        )

    # -----------------------------------------------------
    # FILE CACHE
    # -----------------------------------------------------

    cache_file = (
        f"{CACHE_DIR}/"
        f"{video_id}_info.json"
    )

    if os.path.exists(cache_file):

        try:

            with open(
                cache_file,
                "r"
            ) as f:

                cached = json.load(f)

            timestamp = cached.get(
                "timestamp",
                0
            )

            if (
                time.time() - timestamp
                < 3600
            ):

                data = cached.get(
                    "data",
                    {}
                )

                RAM_CACHE["info"][video_id] = (
                    timestamp,
                    data
                )

                log(
                    f"⚡ Info from file cache: "
                    f"{video_id}"
                )

                return data

        except Exception as e:

            log(
                f"⚠️ Cache read error: {e}"
            )

    info_data = None
    last_error = ""

    # Direct metadata request (proxy removed).
    try:
        log(f"🌐 INFO DIRECT: {video_id}")
        cmd = [
            "yt-dlp", *YTDLP_COMMON,
            "--dump-json", "--skip-download", "--no-warnings",
            f"https://youtu.be/{video_id}"
        ]
        success, stdout, stderr = await run_cmd_with_output(cmd)
        if success and stdout:
            info_data = json.loads(stdout.decode(errors="ignore"))
        else:
            last_error = stderr.decode(errors="ignore") if stderr else "Unknown error"
    except Exception as e:
        last_error = str(e)

    # -----------------------------------------------------
    # FAILED
    # -----------------------------------------------------

    if not info_data:

        log(
            f"❌ Info fetch failed: "
            f"{last_error[:300]}"
        )

        return {
            "success": False,
            "error": (
                "Video not found "
                "or unavailable"
            )
        }

    # -----------------------------------------------------
    # FORMAT INFO
    # -----------------------------------------------------

    duration_seconds = (
        info_data.get(
            "duration",
            0
        )
    )

    duration_str = format_duration(
        duration_seconds
    )

    thumbnail = info_data.get(
        "thumbnail",
        ""
    )

    if not thumbnail:

        thumbnails = info_data.get(
            "thumbnails",
            []
        )

        if thumbnails:

            thumbnail = thumbnails[-1].get(
                "url",
                ""
            )

    if not thumbnail:

        thumbnail = (
            "https://img.youtube.com/vi/"
            f"{video_id}/maxresdefault.jpg"
        )

    result = {

        "success": True,

        "title": info_data.get(
            "title",
            "Unknown"
        ),

        "duration": duration_str,

        "duration_seconds":
            duration_seconds,

        "thumbnail": thumbnail,

        "video_id": video_id,

        "channel": info_data.get(
            "channel",
            "Unknown"
        ),

        "channel_id": info_data.get(
            "channel_id",
            ""
        ),

        "views": info_data.get(
            "view_count",
            0
        ),

        "likes": info_data.get(
            "like_count",
            0
        ),

        "is_live": info_data.get(
            "is_live",
            False
        ),

        "upload_date": info_data.get(
            "upload_date",
            ""
        ),

        "description": (
            info_data.get(
                "description",
                ""
            )[:500]
            if info_data.get(
                "description"
            )
            else ""
        ),

        "tags": (
            info_data.get(
                "tags",
                []
            )[:10]
            if info_data.get(
                "tags"
            )
            else []
        )
    }

    # -----------------------------------------------------
    # RAM CACHE
    # -----------------------------------------------------

    now = time.time()

    RAM_CACHE["info"][video_id] = (
        now,
        result
    )

    # -----------------------------------------------------
    # FILE CACHE
    # -----------------------------------------------------

    try:

        with open(
            cache_file,
            "w"
        ) as f:

            json.dump(
                {
                    "timestamp": now,
                    "data": result
                },
                f
            )

    except Exception as e:

        log(
            f"⚠️ Cache write error: {e}"
        )

    log(
        f"✅ Info fetched: "
        f"{result['title'][:50]} | "
        f"Duration: "
        f"{duration_str} | "
        f"Views: "
        f"{result['views']}"
    )

    return result


# =========================================================
# AUDIO DOWNLOAD
# =========================================================

async def download_audio(video_id: str):

    # -----------------------------------------------------
    # RAM CACHE
    # -----------------------------------------------------

    if video_id in RAM_CACHE["audio"]:

        path = RAM_CACHE["audio"][video_id]

        if os.path.exists(path):

            return path

        RAM_CACHE["audio"].pop(
            video_id,
            None
        )

    start = time.time()

    log(
        f"🎵 Audio download request: "
        f"{video_id}"
    )

    file_path = (
        f"{CACHE_DIR}/"
        f"{video_id}.m4a"
    )

    # -----------------------------------------------------
    # FILE CACHE
    # -----------------------------------------------------

    if os.path.exists(file_path):

        RAM_CACHE["audio"][video_id] = (
            file_path
        )

        size = (
            os.path.getsize(
                file_path
            )
            / 1024
            / 1024
        )

        log(
            f"⚡ Audio cache hit: "
            f"{size:.2f}MB"
        )

        return file_path

    async with DOWNLOAD_WORKERS:

        temp = (
            f"{CACHE_DIR}/"
            f"{video_id}_temp.%(ext)s"
        )

        cleanup_temp_files(
            video_id
        )

        # Direct Render download (proxy removed).
        cmd = [
            "yt-dlp", *YTDLP_COMMON,
            "--no-part", "--no-continue",
            "--fragment-retries", "5",
            "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "-o", temp,
            f"https://youtu.be/{video_id}",
        ]
        ok = await run_cmd(cmd)
        files = glob.glob(f"{CACHE_DIR}/{video_id}_temp.*")
        valid_files = [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0]

        if ok and valid_files:
            source = max(valid_files, key=os.path.getsize)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                shutil.move(source, file_path)
            except Exception as e:
                log(f"❌ Audio move failed: {e}")
                cleanup_temp_files(video_id)
                return None

            RAM_CACHE["audio"][video_id] = file_path
            size = os.path.getsize(file_path) / 1024 / 1024
            total = time.time() - start
            log(f"✅ Audio success: {size:.2f}MB | {total:.2f}s | {calc_speed(size, total)}")
            return file_path

        cleanup_temp_files(video_id)
        log(f"❌ Direct audio download failed: {video_id}")

    log(
        f"❌ Audio failed: "
        f"{video_id}"
    )

    return None


# =========================================================
# VIDEO DOWNLOAD
# =========================================================

async def download_video(video_id: str):

    # -----------------------------------------------------
    # RAM CACHE
    # -----------------------------------------------------

    if video_id in RAM_CACHE["video"]:

        path = RAM_CACHE["video"][video_id]

        if os.path.exists(path):

            return path

        RAM_CACHE["video"].pop(
            video_id,
            None
        )

    start = time.time()

    log(
        f"🎬 Video download request: "
        f"{video_id}"
    )

    file_path = (
        f"{CACHE_DIR}/"
        f"{video_id}.mp4"
    )

    # -----------------------------------------------------
    # FILE CACHE
    # -----------------------------------------------------

    if os.path.exists(file_path):

        RAM_CACHE["video"][video_id] = (
            file_path
        )

        size = (
            os.path.getsize(
                file_path
            )
            / 1024
            / 1024
        )

        log(
            f"⚡ Video cache hit: "
            f"{size:.2f}MB"
        )

        return file_path

    async with DOWNLOAD_WORKERS:

        temp = (
            f"{CACHE_DIR}/"
            f"{video_id}_temp.%(ext)s"
        )

        cleanup_temp_files(
            video_id
        )

        # Direct Render download (proxy removed).
        cmd = [
            "yt-dlp", *YTDLP_COMMON,
            "--no-part", "--no-continue",
            "--concurrent-fragments", "8",
            "--fragment-retries", "5",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", temp,
            f"https://youtu.be/{video_id}",
        ]
        ok = await run_cmd(cmd)
        files = glob.glob(f"{CACHE_DIR}/{video_id}_temp*")
        valid_files = [
            f for f in files
            if os.path.isfile(f) and os.path.getsize(f) > 0
            and f.endswith((".mp4", ".mkv", ".webm"))
        ]

        if ok and valid_files:
            source = max(valid_files, key=os.path.getsize)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                shutil.move(source, file_path)
            except Exception as e:
                log(f"❌ Video move failed: {e}")
                cleanup_temp_files(video_id)
                return None

            RAM_CACHE["video"][video_id] = file_path
            size = os.path.getsize(file_path) / 1024 / 1024
            total = time.time() - start
            log(f"✅ Video success: {size:.2f}MB | {total:.2f}s | {calc_speed(size, total)}")
            return file_path

        cleanup_temp_files(video_id)
        log(f"❌ Direct video download failed: {video_id}")

    log(
        f"❌ Video failed: "
        f"{video_id}"
    )

    return None


# =========================================================
# DOWNLOAD TOKEN
# =========================================================

@app.get("/download")
async def download(
    url: str,
    type: str = "audio"
):

    cleanup_tokens()

    if type not in (
        "audio",
        "video"
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "type must be "
                "audio or video"
            )
        )

    video_id = extract_video_id(
        url
    )

    if not video_id:

        raise HTTPException(
            status_code=400,
            detail="Invalid YouTube URL"
        )

    token = secrets.token_urlsafe(
        48
    )

    token = (
        "AnnieMusic"
        + token
        + "AnnieBots"
    )

    TOKENS[token] = {
        "video_id": video_id,
        "type": type,
        "time": time.time()
    }

    return {
        "status": "success",
        "video_id": video_id,
        "type": type,
        "token": token
    }


# =========================================================
# STREAM
# =========================================================

@app.get(
    "/stream/{video_id}"
)
async def stream(
    video_id: str,
    type: str,
    token: Optional[str] = Query(None),
    X_Download_Token: Optional[str] = Header(None)
):

    # -----------------------------------------------------
    # QUERY + HEADER SUPPORT
    # -----------------------------------------------------

    token = (
        token
        or X_Download_Token
    )

    # -----------------------------------------------------
    # TOKEN VALIDATION
    # -----------------------------------------------------

    validate_token(
        token,
        video_id,
        type
    )

    # -----------------------------------------------------
    # MEDIA TYPE VALIDATION
    # -----------------------------------------------------

    if type not in (
        "audio",
        "video"
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid type"
        )

    # -----------------------------------------------------
    # DUPLICATE DOWNLOAD LOCK
    # -----------------------------------------------------

    lock_key = (
        f"{video_id}:"
        f"{type}"
    )

    lock = ACTIVE_DOWNLOADS.setdefault(
        lock_key,
        asyncio.Lock()
    )

    async with lock:

        if type == "audio":

            path = await download_audio(
                video_id
            )

            media_type = (
                "audio/mp4"
            )

            filename = (
                f"{video_id}.m4a"
            )

        else:

            path = await download_video(
                video_id
            )

            media_type = (
                "video/mp4"
            )

            filename = (
                f"{video_id}.mp4"
            )

    if not path:

        raise HTTPException(
            status_code=404,
            detail="Download failed"
        )

    return FileResponse(
        path,
        media_type=media_type,
        filename=filename
    )


# =========================================================
# BULK INFO
# =========================================================

@app.get(
    "/info/bulk"
)
async def get_bulk_info(
    video_ids: str
):

    ids = [
        vid.strip()
        for vid in video_ids.split(",")
        if vid.strip()
    ]

    results = {}

    for vid in ids[:10]:

        try:

            info = await get_video_info(
                vid
            )

            results[vid] = info

        except Exception as e:

            results[vid] = {
                "success": False,
                "error": str(e)
            }

    return JSONResponse(
        results
    )


# =========================================================
# CACHE CLEAR
# =========================================================

@app.get(
    "/clear_cache"
)
async def clear_cache():

    global RAM_CACHE

    RAM_CACHE = {
        "audio": {},
        "video": {},
        "info": {}
    }

    # -----------------------------------------------------
    # INFO CACHE
    # -----------------------------------------------------

    try:

        for f in glob.glob(
            f"{CACHE_DIR}/*_info.json"
        ):

            try:

                os.remove(f)

            except Exception:
                pass

    except Exception:
        pass

    return JSONResponse({
        "status": "success",
        "message": "All cache cleared"
    })


# =========================================================
# ROOT
# =========================================================

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ANNIEMUSIC YouTube API"}

@app.get("/")
async def root():

    return JSONResponse({
        "Status":
            "ANNIEMUSIC YouTube API Running on Render ✅"
    })
