from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
import asyncio
import os
import time
import shutil
import re
import glob
import secrets
import json
import urllib.request
from typing import Dict, Optional

app = FastAPI()


def log(msg):
    print(msg, flush=True)



# =========================================================
# CONFIG
# =========================================================

BASE_DIR = os.getenv("APP_DIR", "/tmp")
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(BASE_DIR, "cache"))
COOKIES_FILE = os.getenv("COOKIES_FILE", os.path.join(BASE_DIR, "cookies.txt"))
os.makedirs(CACHE_DIR, exist_ok=True)

# Keep this reasonable.
# 1000 simultaneous downloads can overload the VPS/network.
DOWNLOAD_WORKERS = asyncio.Semaphore(max(1, int(os.getenv("DOWNLOAD_WORKERS", "10"))))

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
    "1M",
]

# Optional Render secret file; cookies are not required.
if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
    try:
        RUNTIME_COOKIES = os.path.join(CACHE_DIR, "runtime_cookies.txt")
        if os.path.abspath(COOKIES_FILE) != os.path.abspath(RUNTIME_COOKIES):
            shutil.copy2(COOKIES_FILE, RUNTIME_COOKIES)
        YTDLP_COMMON += ["--cookies", RUNTIME_COOKIES]
        print("🍪 Cookies loaded", flush=True)
    except Exception as e:
        log(f"⚠️ Cookies could not be copied: {e}")


# =========================================================
# UTIL
# =========================================================

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
    """Download audio using yt-dlp's native downloader."""
    if video_id in RAM_CACHE["audio"]:
        path = RAM_CACHE["audio"][video_id]
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        RAM_CACHE["audio"].pop(video_id, None)

    cached_audio = [
        p for p in glob.glob(f"{CACHE_DIR}/{video_id}.audio.*")
        if os.path.isfile(p) and os.path.getsize(p) > 0
    ]
    if cached_audio:
        file_path = max(cached_audio, key=os.path.getsize)
        RAM_CACHE["audio"][video_id] = file_path
        size = os.path.getsize(file_path) / (1024 * 1024)
        log(f"⚡ Audio cache hit: {size:.2f}MB")
        return file_path

    async with DOWNLOAD_WORKERS:
        # Re-check cache after waiting for a worker slot.
        cached_audio = [
            p for p in glob.glob(f"{CACHE_DIR}/{video_id}.audio.*")
            if os.path.isfile(p) and os.path.getsize(p) > 0
        ]
        if cached_audio:
            file_path = max(cached_audio, key=os.path.getsize)
            RAM_CACHE["audio"][video_id] = file_path
            return file_path

        started = time.time()
        log(f"🎵 Audio download request: {video_id}")
        temp = f"{CACHE_DIR}/{video_id}_temp.%(ext)s"
        cleanup_temp_files(video_id)

        cmd = [
            "yt-dlp", *YTDLP_COMMON,
            "--no-part",
            "--no-continue",
            "--concurrent-fragments", "8",
            "--fragment-retries", "5",
            "--retries", "5",
            "--retry-sleep", "http:1",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "-o", temp,
            f"https://youtu.be/{video_id}",
        ]

        ok = await run_cmd(cmd)
        files = [
            p for p in glob.glob(f"{CACHE_DIR}/{video_id}_temp.*")
            if os.path.isfile(p) and os.path.getsize(p) > 0
        ]

        if ok and files:
            source = max(files, key=os.path.getsize)
            ext = os.path.splitext(source)[1].lower() or ".m4a"
            file_path = f"{CACHE_DIR}/{video_id}.audio{ext}"
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                shutil.move(source, file_path)
            except Exception as exc:
                log(f"❌ Audio move failed: {exc}")
                cleanup_temp_files(video_id)
                return None

            RAM_CACHE["audio"][video_id] = file_path
            size = os.path.getsize(file_path) / (1024 * 1024)
            elapsed = max(time.time() - started, 0.001)
            log(
                f"✅ Audio success: {size:.2f}MB | "
                f"{elapsed:.2f}s | {calc_speed(size, elapsed)}"
            )
            return file_path

        cleanup_temp_files(video_id)
        log(f"❌ Direct audio download failed: {video_id}")
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
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
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
# INLINE MEDIA RESPONSE WITH HTTP RANGE SUPPORT
# =========================================================

def media_range_response(request: Request, path: str, media_type: str, filename: str):
    file_size = os.path.getsize(path)
    if file_size <= 0:
        raise HTTPException(status_code=404, detail="Media file is empty")
    range_header = request.headers.get("range")
    start, end = 0, file_size - 1
    status_code = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "public, max-age=3600",
    }

    if range_header:
        match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
        if not match:
            raise HTTPException(status_code=416, detail="Invalid byte range",
                                headers={"Content-Range": f"bytes */{file_size}"})
        first, last = match.groups()
        if first == "" and last == "":
            raise HTTPException(status_code=416, detail="Invalid byte range",
                                headers={"Content-Range": f"bytes */{file_size}"})
        if first == "":
            length = int(last)
            if length <= 0:
                raise HTTPException(status_code=416, detail="Invalid byte range",
                                    headers={"Content-Range": f"bytes */{file_size}"})
            start = max(0, file_size - length)
        else:
            start = int(first)
            end = int(last) if last else file_size - 1
        if start >= file_size or end < start:
            raise HTTPException(status_code=416, detail="Range not satisfiable",
                                headers={"Content-Range": f"bytes */{file_size}"})
        end = min(end, file_size - 1)
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    content_length = end - start + 1
    headers["Content-Length"] = str(content_length)

    async def file_iterator():
        with open(path, "rb") as media_file:
            media_file.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = media_file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        file_iterator(),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


# =========================================================
# STREAM
# =========================================================

@app.get(
    "/stream/{video_id}"
)
async def stream(
    request: Request,
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

    # Inline disposition allows browser playback; byte ranges allow seeking.
    if type == "audio":
        ext = os.path.splitext(path)[1].lower()
        media_type = "audio/mp4" if ext == ".m4a" else "audio/webm"
        filename = f"{video_id}{ext}"
    return media_range_response(request, path, media_type, filename)


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

        patterns = (
            glob.glob(f"{CACHE_DIR}/*_info.json")
            + glob.glob(f"{CACHE_DIR}/*.audio.*")
            + glob.glob(f"{CACHE_DIR}/*.mp4")
            + glob.glob(f"{CACHE_DIR}/*.mkv")
            + glob.glob(f"{CACHE_DIR}/*.webm")
            + glob.glob(f"{CACHE_DIR}/*_temp*")
        )
        for f in set(patterns):
            try:
                if os.path.isfile(f):
                    os.remove(f)
            except Exception as e:
                log(f"⚠️ Cache remove failed: {f}: {e}")

    except Exception:
        pass

    return JSONResponse({
        "status": "success",
        "message": "All cache cleared"
    })


# =========================================================
# RENDER NETWORK SPEED TEST
# =========================================================

def _run_network_speed_test():
    """Measure outbound download speed from the Render instance."""
    test_url = "https://speed.cloudflare.com/__down?bytes=25000000"
    started = time.time()
    total_bytes = 0

    request = urllib.request.Request(
        test_url,
        headers={"User-Agent": "ANNIEMUSIC-Network-Test/1.0"},
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)

    elapsed = max(time.time() - started, 0.001)
    size_mb = total_bytes / (1024 * 1024)

    return {
        "status": "success",
        "size_mb": round(size_mb, 2),
        "time_seconds": round(elapsed, 2),
        "speed_MB_s": round(size_mb / elapsed, 2),
        "speed_Mbps": round((total_bytes * 8) / elapsed / 1_000_000, 2),
        "source": "Cloudflare speed test",
    }


@app.get("/network-test")
async def network_test():
    """
    Test outbound network speed from the Render server.
    This downloads up to 25 MB and does not write a file to disk.
    """
    try:
        result = await asyncio.to_thread(_run_network_speed_test)
        return result
    except Exception as e:
        log(f"\u26a0\ufe0f Network test failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Network speed test failed: {str(e)[:200]}",
        )


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
            "ANNIEMUSIC YouTube API Running on ✅"
    })
