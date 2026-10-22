import asyncio
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from secrets import token_hex
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMP_ROOT = Path(tempfile.gettempdir()) / "clipnest-jobs"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
MAX_DOWNLOAD_MB = max(50, min(1000, int(os.getenv("MAX_DOWNLOAD_MB", "400"))))
MAX_UPLOAD_MB = max(10, min(95, int(os.getenv("MAX_UPLOAD_MB", "90"))))
SESSION_COOKIE = "clipnest_session"
SESSION_AGE = 7 * 24 * 60 * 60
JOB_TTL = 30 * 60
MAX_JOBS = 1
MAX_VIDEO_SECONDS = 4 * 60 * 60
ALLOWED_HEIGHTS = {144, 240, 360, 480, 720, 1080, 1440, 2160}
ALLOWED_HOSTS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "facebook.com", "fb.watch",
    "instagram.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
}
serializer = URLSafeTimedSerializer(SESSION_SECRET or "not-configured")
jobs = {}
uploads = {}
login_attempts = {}

app = FastAPI(title="ClipNest", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class URLBody(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class DownloadBody(URLBody):
    quality: str = Field(default="best", pattern=r"^(best|144|240|360|480|720|1080|1440|2160)$")
    output_format: str = Field(default="mp4", pattern=r"^(mp4|mp3)$")
    trim_start: float | None = Field(default=None, ge=0, le=14400)
    trim_end: float | None = Field(default=None, gt=0, le=14400)


class ConvertBody(BaseModel):
    media_id: str = Field(min_length=10, max_length=40, pattern=r"^[a-f0-9]+$")
    quality: str = Field(default="best", pattern=r"^(best|144|240|360|480|720|1080|1440|2160)$")
    output_format: str = Field(default="mp4", pattern=r"^(mp4|mp3)$")
    trim_start: float | None = Field(default=None, ge=0, le=14400)
    trim_end: float | None = Field(default=None, gt=0, le=14400)


def require_session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not SESSION_SECRET:
        raise HTTPException(status_code=401, detail="প্রথমে access code দিয়ে লগইন করো।")
    try:
        data = serializer.loads(token, max_age=SESSION_AGE)
        if data.get("v") != 1:
            raise BadSignature("invalid version")
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Session শেষ হয়েছে। আবার লগইন করো।")
    return True


def validate_url(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise HTTPException(400, "সঠিক ভিডিও লিংক দাও।")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise HTTPException(400, "শুধু public HTTPS লিংক ব্যবহার করো।")
    if port not in (None, 443):
        raise HTTPException(400, "এই URL-এর port অনুমোদিত নয়।")
    if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_HOSTS):
        raise HTTPException(400, "এই সংস্করণে YouTube, Facebook, Instagram ও TikTok-এর লিংক সমর্থিত।")
    if parsed.path in ("", "/"):
        raise HTTPException(400, "ভিডিওর সম্পূর্ণ লিংক দাও।")
    return value


def clean_expired_jobs():
    now = time.time()
    for job_id, job in list(jobs.items()):
        if job.get("status") != "running" and now - job["created_at"] > JOB_TTL:
            shutil.rmtree(TEMP_ROOT / job_id, ignore_errors=True)
            jobs.pop(job_id, None)
    for media_id, item in list(uploads.items()):
        if now - item.get("created_at", now) > JOB_TTL:
            shutil.rmtree(item["directory"], ignore_errors=True)
            uploads.pop(media_id, None)
    for ip, stamps in list(login_attempts.items()):
        fresh = [stamp for stamp in stamps if now - stamp < 900]
        if fresh:
            login_attempts[ip] = fresh
        else:
            login_attempts.pop(ip, None)


def call_ytdlp_info(url: str) -> dict:
    cmd = [
        "yt-dlp", "--js-runtimes", "node", "--dump-single-json", "--skip-download", "--no-warnings",
        "--no-playlist", "--socket-timeout", "12", "--retries", "1",
        "--fragment-retries", "1", "--no-cache-dir", url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=70, check=False)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "ভিডিওর তথ্য আনতে সময় শেষ হয়েছে। পরে আবার চেষ্টা করো।")
    if result.returncode or not result.stdout.strip():
        raw_error = result.stderr or ""
        lowered = raw_error.lower()
        if "sign in to confirm you're not a bot" in lowered or "confirm you're not a bot" in lowered:
            raise HTTPException(422, "YouTube এই cloud server-এর request-এ verification চাইছে, তাই এই লিংকটি এখন সরাসরি প্রসেস করা যাচ্ছে না। আমরা verification bypass করি না। ভিডিওটি নিজের হলে YouTube Studio বা Google Takeout থেকে ফাইলটি ডাউনলোড করে নিচের ‘Upload a video’ অংশে দিলে Trim/MP3/MP4 করা যাবে।")
        if "private video" in lowered or "login required" in lowered or "sign in to confirm" in lowered:
            raise HTTPException(422, "এই ভিডিওটি private বা login/verification চাইছে। অ্যাপ এই access control bypass করবে না। public/অনুমোদিত লিংক ব্যবহার করো, অথবা নিজের ভিডিও official পদ্ধতিতে ডাউনলোড করে Upload অপশন ব্যবহার করো।")
        if "failed to extract any player response" in lowered or "unable to extract any player response" in lowered or "unable to extract yt initial data" in lowered:
            raise HTTPException(422, "YouTube থেকে ভিডিওর তথ্য পড়া যাচ্ছে না। Extractor-এর version/runtime update করা হয়েছে, কিন্তু YouTube যদি cloud server-কে response না দেয়, তাহলে এই লিংক সরাসরি কাজ নাও করতে পারে। নিজের ভিডিও হলে YouTube Studio/Google Takeout থেকে নামিয়ে ‘Upload a video’ অংশে দাও।")
        if "unsupported url" in lowered:
            raise HTTPException(422, "এই লিংকটি সমর্থিত নয়। URL সঠিক কি না এবং ভিডিওটি public কি না যাচাই করো।")
        raise HTTPException(422, "ভিডিওর তথ্য আনা যায়নি। লিংকটি public, accessible এবং এই server থেকে উপলভ্য কি না যাচাই করো। কিছু প্ল্যাটফর্ম cloud request সীমিত করতে পারে।")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(502, "উৎস থেকে সঠিক ভিডিও তথ্য পাওয়া যায়নি।")
    if data.get("is_live"):
        raise HTTPException(422, "লাইভ ভিডিও এই টুলে সমর্থিত নয়।")
    return data


def duration_text(value):
    if value is None:
        return "Duration unavailable"
    value = max(0, int(value))
    hours, rem = divmod(value, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def shape_info(info: dict):
    formats = info.get("formats") or []
    heights = set()
    for item in formats:
        h = item.get("height")
        if h and item.get("vcodec") not in (None, "none"):
            try:
                h = int(h)
                if h in ALLOWED_HEIGHTS:
                    heights.add(h)
            except (TypeError, ValueError):
                pass
    if not heights and info.get("height"):
        try:
            h = int(info["height"])
            if h in ALLOWED_HEIGHTS:
                heights.add(h)
        except (TypeError, ValueError):
            pass
    highest = max(heights) if heights else 360
    qualities = [q for q in sorted(ALLOWED_HEIGHTS, reverse=True) if q <= highest]
    raw_duration = info.get("duration")
    try:
        duration = int(float(raw_duration)) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is not None and (duration < 0 or duration > MAX_VIDEO_SECONDS):
        raise HTTPException(422, "ভিডিওর দৈর্ঘ্য এই free instance-এর সীমার বাইরে (সর্বোচ্চ ৪ ঘণ্টা)।")
    return {
        "title": str(info.get("title") or "Untitled video")[:180],
        "duration": duration,
        "duration_text": duration_text(duration),
        "qualities": qualities,
        "source": str(info.get("extractor_key") or info.get("extractor") or "Media source")[:80],
        "trim_allowed": duration is not None and duration >= 600,
    }



def probe_local_video(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,height,width",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise HTTPException(503, "ভিডিও ফাইল বিশ্লেষণ করা যাচ্ছে না।")
    if result.returncode:
        raise HTTPException(400, "এই ফাইলটি পড়া যাচ্ছে না। MP4, MOV, MKV বা WebM ভিডিও ব্যবহার করো।")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(400, "ভিডিও ফাইলের তথ্য পড়া যায়নি।")
    streams = info.get("streams") or []
    if not any(s.get("codec_type") == "video" for s in streams):
        raise HTTPException(400, "শুধু ভিডিও ফাইল আপলোড করো।")
    audio_available = any(s.get("codec_type") == "audio" for s in streams)
    try:
        duration = int(float((info.get("format") or {}).get("duration")))
    except (TypeError, ValueError):
        duration = None
    if duration is not None and (duration < 0 or duration > MAX_VIDEO_SECONDS):
        raise HTTPException(413, "ভিডিওর দৈর্ঘ্য সর্বোচ্চ ৪ ঘণ্টা হতে পারে।")
    heights = []
    for stream in streams:
        h = stream.get("height")
        if stream.get("codec_type") == "video" and h:
            try:
                heights.append(int(h))
            except (TypeError, ValueError):
                pass
    height = max(heights) if heights else 360
    qualities = [q for q in sorted(ALLOWED_HEIGHTS, reverse=True) if q <= height]
    if not qualities:
        qualities = [144]
    return {"duration": duration, "height": height, "qualities": qualities, "has_audio": audio_available}


async def process_local_conversion(job_id: str, media_id: str, quality: str,
                                   output_format: str, trim):
    job = jobs[job_id]
    source = uploads.get(media_id)
    if not source:
        job.update(status="failed", progress=0, message="Uploaded ফাইলের মেয়াদ শেষ হয়েছে। আবার upload করো।")
        return
    directory = TEMP_ROOT / job_id
    directory.mkdir(parents=True, exist_ok=True)
    try:
        input_path = Path(source["path"]).resolve()
        if not input_path.is_file() or not input_path.is_relative_to(TEMP_ROOT.resolve()):
            raise RuntimeError("Uploaded video ফাইলটি পাওয়া যায়নি।")
        output_path = directory / ("clip.mp3" if output_format == "mp3" else "clip.mp4")
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
        if trim:
            start, end = trim
            cmd += ["-ss", ts(start), "-i", str(input_path), "-t", str(end - start)]
        else:
            cmd += ["-i", str(input_path)]
        if output_format == "mp3":
            if not source["has_audio"]:
                raise RuntimeError("এই ভিডিওতে audio track নেই, তাই MP3 তৈরি করা যায়নি।")
            cmd += ["-vn", "-codec:a", "libmp3lame", "-b:a", "192k", str(output_path)]
        else:
            if quality != "best":
                max_height = int(quality)
                cmd += ["-vf", f"scale=-2:min(ih\\,{max_height})"]
            cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(output_path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        job["process"] = proc
        duration = max(1.0, (trim[1] - trim[0]) if trim else float(source["duration"] or 1))
        async def read_progress():
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("out_time_ms="):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                        job["progress"] = min(99, max(0, seconds / duration * 100))
                    except ValueError:
                        pass
        progress_task = asyncio.create_task(read_progress())
        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=15 * 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("১৫ মিনিটের processing limit শেষ হয়েছে। ছোট ভিডিও বা কম resolution ব্যবহার করো।")
        await progress_task
        stderr = (await stderr_task).decode("utf-8", "replace")
        if code:
            reason = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "FFmpeg conversion ব্যর্থ হয়েছে।")
            raise RuntimeError(reason[:450])
        if not output_path.is_file():
            raise RuntimeError("Output ফাইল তৈরি হয়নি।")
        if output_path.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
            raise RuntimeError(f"তৈরি ফাইল {MAX_DOWNLOAD_MB} MB সীমা ছাড়িয়েছে। কম resolution বেছে নাও।")
        job.update(status="completed", progress=100, message="ফাইল প্রস্তুত।",
                   output_file=str(output_path), filename=output_path.name, upload_id=media_id)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        job.update(status="failed", progress=0, message=str(exc)[:500], output_file=None)
    finally:
        job.pop("process", None)


def ts(value):
    whole = int(value)
    millis = int(round((value - whole) * 1000))
    if millis >= 1000:
        whole += 1
        millis = 0
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{millis:03d}"


def validate_trim(body, duration):
    if body.trim_start is None and body.trim_end is None:
        return None
    if body.trim_start is None or body.trim_end is None:
        raise HTTPException(400, "Trim-এর Start এবং End—দুটো সময়ই দিতে হবে।")
    if duration is None or duration < 600:
        raise HTTPException(400, "১০ মিনিটের কম ভিডিওতে Trim করা যাবে না।")
    if body.trim_end <= body.trim_start or body.trim_end > duration:
        raise HTTPException(400, "Trim-এর সময়সীমা ভিডিওর দৈর্ঘ্যের মধ্যে সঠিকভাবে নির্বাচন করো।")
    if body.trim_end - body.trim_start < 1:
        raise HTTPException(400, "কমপক্ষে ১ সেকেন্ডের অংশ নির্বাচন করো।")
    return body.trim_start, body.trim_end


async def process_download(job_id, url, quality, output_format, trim):
    job = jobs[job_id]
    directory = TEMP_ROOT / job_id
    directory.mkdir(parents=True, exist_ok=True)
    try:
        template = str(directory / "clip.%(ext)s")
        cmd = [
            "yt-dlp", "--js-runtimes", "node", "--newline", "--no-warnings", "--no-playlist",
            "--socket-timeout", "15", "--retries", "1", "--fragment-retries", "1",
            "--max-filesize", f"{MAX_DOWNLOAD_MB}M",
            "--progress-template", "download:__PCT__%(progress._percent_str)s",
            "--print", "after_move:__FILE__%(filepath)s", "-o", template,
        ]
        if output_format == "mp3":
            cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "192K"]
        else:
            selector = "bestvideo+bestaudio/best" if quality == "best" else f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"
            cmd += ["-f", selector, "--merge-output-format", "mp4"]
        if trim:
            cmd += ["--download-sections", f"*{ts(trim[0])}-{ts(trim[1])}", "--force-keyframes-at-cuts"]
        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        job["process"] = proc
        async def read_stdout():
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("__PCT__"):
                    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", line)
                    if match:
                        job["progress"] = min(99, float(match.group(1)))
                elif line.startswith("__FILE__"):
                    job["candidate"] = line[len("__FILE__"):].strip()
        out_task = asyncio.create_task(read_stdout())
        err_task = asyncio.create_task(proc.stderr.read())
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=15 * 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("১৫ মিনিটের processing সীমা শেষ হয়েছে। ছোট ভিডিও বা কম quality বেছে নাও।")
        await out_task
        error = (await err_task).decode("utf-8", "replace")
        if code != 0:
            message = next((ln.strip() for ln in reversed(error.splitlines()) if ln.strip()), "ডাউনলোড ব্যর্থ হয়েছে।")
            raise RuntimeError(message[:450])
        base = directory.resolve()
        candidate = Path(job.get("candidate") or "").resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            found = [p.resolve() for p in directory.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
            candidate = found[0] if found else None
        if not candidate or not candidate.is_relative_to(base) or not candidate.is_file():
            raise RuntimeError("Output ফাইল পাওয়া যায়নি। আবার চেষ্টা করো।")
        if candidate.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
            raise RuntimeError(f"ফাইল {MAX_DOWNLOAD_MB} MB সীমা ছাড়িয়েছে। কম quality বেছে নাও।")
        job.update(status="completed", progress=100, message="ফাইল প্রস্তুত।", output_file=str(candidate),
                   filename="clip.mp3" if output_format == "mp3" else "clip.mp4")
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        job.update(status="failed", progress=0, message=str(exc)[:500], output_file=None)
    finally:
        job.pop("process", None)


def clean_after_download(job_id):
    job = jobs.get(job_id, {})
    shutil.rmtree(TEMP_ROOT / job_id, ignore_errors=True)
    media_id = job.get("upload_id")
    if media_id and media_id in uploads:
        shutil.rmtree(uploads[media_id]["directory"], ignore_errors=True)
        uploads.pop(media_id, None)
    if job_id in jobs:
        jobs[job_id].update(status="expired", output_file=None, message="ডাউনলোড সম্পন্ন; অস্থায়ী ফাইল মুছে ফেলা হয়েছে।")


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.get("/", response_class=HTMLResponse)
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "app": "clipnest"}


@app.get("/api/session")
async def session(request: Request):
    return {"authenticated": bool(request.cookies.get(SESSION_COOKIE) and _valid_session(request.cookies.get(SESSION_COOKIE)))}


def _valid_session(token):
    try:
        return serializer.loads(token, max_age=SESSION_AGE).get("v") == 1 and bool(SESSION_SECRET)
    except (BadSignature, SignatureExpired):
        return False


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    if not APP_PASSWORD or not SESSION_SECRET:
        raise HTTPException(503, "Render-এ APP_PASSWORD ও SESSION_SECRET configure করতে হবে।")
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in login_attempts.get(ip, []) if now - t < 900]
    if len(attempts) >= 8:
        raise HTTPException(429, "অনেকবার চেষ্টা হয়েছে। ১৫ মিনিট পরে আবার চেষ্টা করো।")
    if not hmac.compare_digest(body.password.encode(), APP_PASSWORD.encode()):
        login_attempts[ip] = attempts + [now]
        raise HTTPException(401, "Access code সঠিক নয়।")
    login_attempts.pop(ip, None)
    token = serializer.dumps({"v": 1, "issued": int(now)})
    response = JSONResponse({"ok": True})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=SESSION_AGE, path="/")
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@app.post("/api/info", dependencies=[Depends(require_session)])
async def info(body: URLBody):
    clean_expired_jobs()
    data = await asyncio.to_thread(call_ytdlp_info, validate_url(body.url))
    return shape_info(data)



@app.post("/api/upload", dependencies=[Depends(require_session)])
async def upload_video(file: UploadFile = File(...)):
    clean_expired_jobs()
    if any(job.get("status") == "running" for job in jobs.values()):
        raise HTTPException(429, "একটি processing কাজ চলছে। সেটি শেষ হলে upload করো।")
    filename = Path(file.filename or "video").name
    suffix = Path(filename).suffix.lower()
    allowed_suffixes = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".3gp"}
    if suffix not in allowed_suffixes:
        raise HTTPException(415, "MP4, MOV, MKV, WebM, AVI, M4V বা 3GP ভিডিও আপলোড করো।")
    media_id = token_hex(12)
    directory = TEMP_ROOT / ("upload-" + media_id)
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / ("source" + suffix)
    total = 0
    try:
        with path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"ফাইল সর্বোচ্চ {MAX_UPLOAD_MB} MB হতে পারে।")
                out.write(chunk)
        if total == 0:
            raise HTTPException(400, "খালি ফাইল আপলোড হয়েছে।")
        details = await asyncio.to_thread(probe_local_video, path)
        uploads[media_id] = {
            "id": media_id, "path": str(path), "directory": str(directory),
            "filename": filename[:160], "created_at": time.time(),
            "duration": details["duration"], "height": details["height"],
            "qualities": details["qualities"], "has_audio": details["has_audio"]
        }
        return {
            "media_id": media_id, "title": filename[:160], "duration": details["duration"],
            "duration_text": duration_text(details["duration"]), "qualities": details["qualities"],
            "source": "Uploaded file", "trim_allowed": details["duration"] is not None and details["duration"] >= 600,
            "upload_limit_mb": MAX_UPLOAD_MB
        }
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        await file.close()


@app.post("/api/convert", dependencies=[Depends(require_session)])
async def convert_uploaded(body: ConvertBody):
    clean_expired_jobs()
    source = uploads.get(body.media_id)
    if not source:
        raise HTTPException(404, "Uploaded ফাইলটির মেয়াদ শেষ হয়েছে। আবার upload করো।")
    if any(job.get("status") == "running" for job in jobs.values()):
        raise HTTPException(429, "একটি processing কাজ চলছে। সেটি শেষ হলে আবার চেষ্টা করো।")
    trim = validate_trim(body, source.get("duration"))
    if body.output_format == "mp3" and trim:
        raise HTTPException(400, "MP3 export-এর জন্য এই সংস্করণে Trim আগে video format-এ করো।")
    if body.output_format == "mp3" and not source.get("has_audio"):
        raise HTTPException(400, "এই ভিডিওতে audio track নেই।")
    job_id = token_hex(10)
    jobs[job_id] = {"id": job_id, "status": "running", "progress": 0, "message": "Conversion শুরু হচ্ছে…",
                    "created_at": time.time(), "output_file": None, "filename": None, "upload_id": body.media_id}
    asyncio.create_task(process_local_conversion(job_id, body.media_id, body.quality, body.output_format, trim))
    return {"job_id": job_id, "status": "running"}


@app.post("/api/download", dependencies=[Depends(require_session)])
async def download(body: DownloadBody):
    clean_expired_jobs()
    url = validate_url(body.url)
    if any(job.get("status") == "running" for job in jobs.values()):
        raise HTTPException(429, "একটি কাজ চলছে। সেটি শেষ হলে আবার চেষ্টা করো।")
    info = await asyncio.to_thread(call_ytdlp_info, url)
    shaped = shape_info(info)
    trim = validate_trim(body, shaped["duration"])
    job_id = token_hex(10)
    jobs[job_id] = {"id": job_id, "status": "running", "progress": 0, "message": "কাজ শুরু হচ্ছে…",
                    "created_at": time.time(), "output_file": None, "filename": None}
    asyncio.create_task(process_download(job_id, url, body.quality, body.output_format, trim))
    return {"job_id": job_id, "status": "running"}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_session)])
async def job_status(job_id: str):
    clean_expired_jobs()
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "এই কাজের মেয়াদ শেষ হয়েছে অথবা এটি পাওয়া যায়নি।")
    return {"id": job_id, "status": job["status"], "progress": job["progress"], "message": job["message"],
            "filename": job.get("filename"),
            "download_url": f"/api/jobs/{job_id}/file" if job["status"] == "completed" else None}


@app.get("/api/jobs/{job_id}/file", dependencies=[Depends(require_session)])
async def job_file(job_id: str, background_tasks: BackgroundTasks):
    job = jobs.get(job_id)
    if not job or job.get("status") != "completed" or not job.get("output_file"):
        raise HTTPException(404, "ফাইলটি আর পাওয়া যাচ্ছে না। আবার প্রসেস করো।")
    path = Path(job["output_file"]).resolve()
    if not path.is_file() or not path.is_relative_to(TEMP_ROOT.resolve()):
        raise HTTPException(404, "Output ফাইলটি পাওয়া যায়নি।")
    background_tasks.add_task(clean_after_download, job_id)
    return FileResponse(path, filename=job.get("filename") or path.name, media_type="application/octet-stream")
