"""Telegram webhook interface for MediaNest.

The bot reuses the same in-process MediaNest job registry and processing functions.
It is disabled unless a token, webhook secret, webhook URL and explicit user allowlist
are configured in the service environment.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import time
from pathlib import Path
from secrets import token_hex, token_urlsafe
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
ALLOWED_USER_IDS = {
    item.strip() for item in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if item.strip().isdigit()
}
TELEGRAM_UPLOAD_LIMIT = 47 * 1024 * 1024
TELEGRAM_DOWNLOAD_LIMIT = 20_000_000
TELEGRAM_VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.mpeg', '.mpg', '.3gp'}
LINK_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 15 * 60

router = APIRouter()
_services: dict = {}
_user_modes: dict[str, str] = {}
_pending_urls: dict[str, str] = {}
_video_sessions: dict[str, dict] = {}
_converter_sessions: dict[str, dict] = {}
_trim_waiting: dict[str, str] = {}
_job_owners: dict[str, str] = {}
_download_links: dict[str, dict] = {}
_monitor_tasks: dict[str, asyncio.Task] = {}
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def configure_telegram(services: dict) -> None:
    """Inject MediaNest's existing functions after app.py defines them."""
    _services.clear()
    _services.update(services)


def telegram_is_configured() -> bool:
    return bool(BOT_TOKEN and WEBHOOK_SECRET and WEBHOOK_URL and ALLOWED_USER_IDS)


def _base_url() -> str:
    explicit = os.getenv("TELEGRAM_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    parsed = urlsplit(WEBHOOK_URL)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    return render_url


async def _api(method: str, *, data=None, files=None, timeout=20.0) -> dict:
    if not BOT_TOKEN:
        return {"ok": False, "description": "Telegram bot is not configured"}
    endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            response = await client.post(endpoint, data=data, files=files)
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "description": "Telegram returned a non-JSON response"}
        if response.status_code >= 400 and not isinstance(payload, dict):
            return {"ok": False, "description": f"Telegram HTTP {response.status_code}"}
        return payload if isinstance(payload, dict) else {"ok": False, "description": "Invalid Telegram response"}
    except httpx.HTTPError:
        # Do not log exception details: they can contain the token-bearing endpoint.
        return {"ok": False, "description": "Could not reach Telegram API"}


async def _send_message(chat_id, text: str, reply_markup=None) -> dict:
    data = {"chat_id": str(chat_id), "text": text[:4000], "disable_web_page_preview": "true"}
    if reply_markup is not None:
        import json
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return await _api("sendMessage", data=data)


async def _edit_message(chat_id, message_id, text: str, reply_markup=None) -> dict:
    import json
    data = {"chat_id": str(chat_id), "message_id": str(message_id), "text": text[:4000],
            "disable_web_page_preview": "true"}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return await _api("editMessageText", data=data)


async def _answer_callback(callback_id, text: str = "") -> None:
    await _api("answerCallbackQuery", data={"callback_query_id": callback_id, "text": text[:180]})


def _main_menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "🎬 Video Download", "callback_data": "mode:video"},
         {"text": "🖼 Image Download", "callback_data": "mode:image"}],
        [{"text": "🎞 Convert a video", "callback_data": "mode:convert"}],
        [{"text": "📊 My Jobs / Status", "callback_data": "action:status"},
         {"text": "⛔ Cancel Job", "callback_data": "action:cancel"}],
    ]}


def _mode_menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "🎬 Video", "callback_data": "mode:video"},
         {"text": "🖼 Image", "callback_data": "mode:image"}],
    ]}


def _cancel_keyboard(user_id: str) -> dict | None:
    jobs = _services.get("jobs", {})
    rows = []
    for job_id, job in list(jobs.items()):
        owner = _job_owners.get(job_id)
        shared_website_job = job.get("interface") == "website"
        if job and job.get("status") == "running" and (owner == user_id or shared_website_job):
            rows.append([{"text": f"Cancel {str(job.get('title') or job_id)[:24]}",
                          "callback_data": f"cancel:{job_id}"}])
    return {"inline_keyboard": rows} if rows else None


def _owned_jobs(user_id: str) -> list[tuple[str, dict]]:
    jobs = _services.get("jobs", {})
    found = []
    for job_id, job in list(jobs.items()):
        owner = _job_owners.get(job_id)
        # The bot is private and allowlisted. Website jobs are shared in the same
        # MediaNest workspace so the user can inspect and manage them here too.
        if job and (owner == user_id or job.get("interface") == "website"):
            found.append((job_id, job))
    return sorted(found, key=lambda pair: pair[1].get("created_at", 0), reverse=True)[:8]


async def _show_status(chat_id, user_id: str) -> None:
    found = _owned_jobs(user_id)
    if not found:
        await _send_message(chat_id, "তোমার কোনো Telegram job পাওয়া যায়নি। নিচের menu থেকে নতুন কাজ শুরু করো।",
                            _main_menu())
        return
    lines = ["📊 MediaNest job status"]
    rows = []
    for job_id, job in found:
        status = str(job.get("status", "unknown")).upper()
        progress = int(job.get("progress") or 0)
        title = str(job.get("title") or job.get("filename") or "Media job")[:70]
        surface = "Website" if job.get("interface") == "website" else "Telegram"
        lines.append(f"• {title}\n  {surface} · {status} · {progress}%")
        if status == "FAILED":
            details = _services.get("classify_job_error", lambda unused: None)(job) or {}
            if details.get("error_code"):
                lines.append(f"  Error: {details.get('error_code')} — {details.get('recovery_hint', '')}")
        if status == "RUNNING" and (_job_owners.get(job_id) == user_id or job.get("interface") == "website"):
            rows.append([{"text": f"Cancel {job_id[:6]}", "callback_data": f"cancel:{job_id}"}])
        elif status == "COMPLETED" and job.get("output_file"):
            rows.append([{"text": f"📤 Send {job_id[:6]} to Telegram", "callback_data": f"send:{job_id}"}])
    rows.append([{"text": "⬅️ Menu", "callback_data": "action:menu"}])
    await _send_message(chat_id, "\n".join(lines), {"inline_keyboard": rows})


async def startup_telegram() -> None:
    """Automatically configure Telegram's webhook after Render starts the app."""
    if not BOT_TOKEN:
        print("[telegram] disabled: TELEGRAM_BOT_TOKEN missing")
        return
    if not WEBHOOK_SECRET or not WEBHOOK_URL or not ALLOWED_USER_IDS:
        print("[telegram] disabled: configure webhook URL, secret, and allowed user IDs")
        return
    payload = await _api("setWebhook", data={
        "url": WEBHOOK_URL,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": '["message","callback_query"]',
        "max_connections": "5",
    })
    if payload.get("ok"):
        print("[telegram] webhook configured")
    else:
        print(f"[telegram] webhook setup failed: {str(payload.get('description') or 'unknown error')[:180]}")


async def _process_url(chat_id, user_id: str, raw_url: str, mode: str) -> None:
    """Start a video/image flow in the same job registry used by the website."""
    services = _services
    jobs = services["jobs"]
    if any(job.get("status") == "running" for job in jobs.values()):
        await _send_message(chat_id, "একটি MediaNest processing job চলছে। সেটি শেষ হলে আবার চেষ্টা করো।",
                            _main_menu())
        return
    try:
        if mode == "image":
            clean_url = await services["normalize_image_source_url"](raw_url)
            job_id = token_hex(10)
            requested_name = services["safe_image_basename"]("MediaNest Images")
            jobs[job_id] = {
                "id": job_id, "status": "running", "progress": 0,
                "message": "Starting image extraction…", "created_at": time.time(),
                "output_file": None, "title": requested_name, "filename": None,
                "interface": "telegram", "kind": "image", "format": "zip-or-image",
            }
            _job_owners[job_id] = user_id
            jobs[job_id]["task"] = asyncio.create_task(
                services["process_image_download"](job_id, clean_url, requested_name)
            )
            sent = await _send_message(
                chat_id, "🖼 Image download শুরু হয়েছে। Public post-এর original image পাওয়া গেলে সেটি, না হলে available preview চেষ্টা করা হবে।",
                {"inline_keyboard": [[{"text": "📊 Status", "callback_data": "action:status"},
                                      {"text": "⛔ Cancel", "callback_data": f"cancel:{job_id}"}]]},
            )
            message_id = (sent.get("result") or {}).get("message_id")
            _monitor_tasks[job_id] = asyncio.create_task(_monitor_job(chat_id, user_id, job_id, message_id))
            return

        clean_url = await services["normalize_source_url"](raw_url)
        info = await services["get_video_info"](clean_url)
        title = str(info.get("title") or "MediaNest Download")[:160]
        qualities = info.get("qualities") or [360]
        token = token_hex(4)
        _video_sessions[token] = {
            "owner": user_id, "url": clean_url, "title": title,
            "qualities": qualities, "created_at": time.time(),
        }
        _prune_sessions()
        rows = []
        options = [("best", "Best")]
        for height in qualities:
            try:
                h = int(height)
            except (TypeError, ValueError):
                continue
            item = (str(h), f"{h}p")
            if item not in options:
                options.append(item)
        mp4_buttons = []
        for quality, label in options:
            mp4_buttons.append({"text": f"MP4 · {label}", "callback_data": f"dl:{token}:mp4:{quality}"})
            if len(mp4_buttons) == 2:
                rows.append(mp4_buttons)
                mp4_buttons = []
        if mp4_buttons:
            rows.append(mp4_buttons)
        rows.append([{"text": "🎵 MP3 Audio", "callback_data": f"dl:{token}:mp3:best"}])
        rows.append([{"text": "⬅️ Menu", "callback_data": "action:menu"}])
        duration = info.get("duration_text") or "Unknown"
        source = info.get("source") or "Media source"
        await _send_message(
            chat_id,
            f"🎬 ভিডিও পাওয়া গেছে\n{title}\nSource: {source}\nDuration: {duration}\n\nMP4 quality অথবা MP3 বেছে নাও।",
            {"inline_keyboard": rows},
        )
    except HTTPException as exc:
        await _send_message(chat_id, f"❌ {str(exc.detail)[:700]}", _main_menu())
    except Exception as exc:
        # Do not log source URLs or query strings; the message is intentionally generic.
        print(f"[telegram] request failed mode={mode} error={type(exc).__name__}")
        await _send_message(chat_id, "❌ লিংকটি এখন প্রসেস করা যায়নি। Public URL দিয়ে আবার চেষ্টা করো।",
                            _main_menu())


def _prune_sessions() -> None:
    now = time.time()
    for token, session in list(_video_sessions.items()):
        if now - session.get("created_at", 0) > SESSION_TTL_SECONDS:
            _video_sessions.pop(token, None)
    for token, session in list(_converter_sessions.items()):
        if now - session.get("created_at", 0) > SESSION_TTL_SECONDS:
            _converter_sessions.pop(token, None)
    for token, link in list(_download_links.items()):
        if link.get("expires_at", 0) < now or link.get("uses", 0) >= 3:
            _download_links.pop(token, None)


async def _start_video_download(chat_id, user_id: str, token: str, output_format: str, quality: str) -> None:
    session = _video_sessions.get(token)
    if not session or session.get("owner") != user_id or time.time() - session.get("created_at", 0) > SESSION_TTL_SECONDS:
        await _send_message(chat_id, "এই selection-এর মেয়াদ শেষ। আবার URL পাঠিয়ে চেষ্টা করো।", _main_menu())
        return
    jobs = _services["jobs"]
    if any(job.get("status") == "running" for job in jobs.values()):
        await _send_message(chat_id, "একটি MediaNest processing job চলছে। সেটি শেষ হলে আবার চেষ্টা করো।",
                            _main_menu())
        return
    try:
        if quality != "best" and quality not in {str(q) for q in session.get("qualities", [])}:
            await _send_message(chat_id, "এই quality আর উপলভ্য নেই। URL আবার পরীক্ষা করো।", _main_menu())
            return
        job_id = token_hex(10)
        title = session.get("title") or "MediaNest Download"
        filename = _services["safe_download_filename"](title, output_format)
        jobs[job_id] = {
            "id": job_id, "status": "running", "progress": 0,
            "message": "কাজ শুরু হচ্ছে…", "created_at": time.time(),
            "output_file": None, "title": title, "filename": filename,
            "interface": "telegram", "kind": "video", "format": output_format,
        }
        _job_owners[job_id] = user_id
        jobs[job_id]["task"] = asyncio.create_task(
            _services["process_download"](job_id, session["url"], quality, output_format, None)
        )
        sent = await _send_message(
            chat_id, f"⏳ Download শুরু হয়েছে: {title}\nFormat: {output_format.upper()} · Quality: {quality}",
            {"inline_keyboard": [[{"text": "📊 Status", "callback_data": "action:status"},
                                  {"text": "⛔ Cancel", "callback_data": f"cancel:{job_id}"}]]},
        )
        message_id = (sent.get("result") or {}).get("message_id")
        _monitor_tasks[job_id] = asyncio.create_task(_monitor_job(chat_id, user_id, job_id, message_id))
    except Exception as exc:
        print(f"[telegram] could not start job error={type(exc).__name__}")
        await _send_message(chat_id, "❌ Download শুরু করা যায়নি। আবার চেষ্টা করো।", _main_menu())


async def _monitor_job(chat_id, user_id: str, job_id: str, message_id) -> None:
    jobs = _services["jobs"]
    last_edit = 0.0
    last_signature = None
    try:
        while time.time() - jobs.get(job_id, {}).get("created_at", time.time()) < 16 * 60:
            job = jobs.get(job_id)
            if not job:
                await _send_message(chat_id, "এই job-এর তথ্য পাওয়া যাচ্ছে না। /start দিয়ে আবার শুরু করো。", _main_menu())
                return
            status = job.get("status")
            progress = int(job.get("progress") or 0)
            message = str(job.get("message") or "Processing…")[:160]
            signature = (status, progress // 10, message)
            if message_id and signature != last_signature and (time.time() - last_edit >= 4 or status != "running"):
                text = f"📥 {str(job.get('title') or job.get('filename') or 'MediaNest job')[:120]}\nStatus: {str(status).upper()} · {progress}%\n{message}"
                await _edit_message(
                    chat_id, message_id, text,
                    {"inline_keyboard": [[{"text": "📊 Status", "callback_data": "action:status"},
                                          {"text": "⛔ Cancel", "callback_data": f"cancel:{job_id}"}]]}
                    if status == "running" else None,
                )
                last_signature = signature
                last_edit = time.time()
            if status == "completed":
                path_text = job.get("output_file")
                if path_text:
                    path = Path(path_text)
                    try:
                        root = Path(_services["TEMP_ROOT"]).resolve()
                        resolved = path.resolve()
                        if resolved.is_relative_to(root) and resolved.is_file():
                            size = resolved.stat().st_size
                            if size <= TELEGRAM_UPLOAD_LIMIT:
                                result = await _send_document(chat_id, resolved, job.get("filename") or resolved.name)
                                if result.get("ok"):
                                    await _send_message(chat_id, "✅ ফাইল পাঠানো হয়েছে।", _main_menu())
                                    _services["clean_after_download"](job_id)
                                    return
                            link = _create_download_link(job_id, user_id)
                            if link:
                                await _send_message(
                                    chat_id,
                                    f"✅ ফাইল প্রস্তুত ({size / (1024 * 1024):.1f} MB)। Telegram-এর file upload limit-এর কারণে direct download link দিলাম; link ১৫ মিনিট থাকবে, সর্বোচ্চ ৩ বার ব্যবহার করা যাবে।\n{link}",
                                    _main_menu(),
                                )
                                return
                            await _send_message(chat_id, "ফাইল তৈরি হয়েছে, তবে বড় ফাইলের download link তৈরি করা যায়নি। ওয়েবসাইটে লগইন করে download করো।",
                                                _main_menu())
                            return
                    except OSError:
                        pass
                await _send_message(chat_id, "কাজ শেষ হয়েছে, কিন্তু output file পাওয়া যায়নি।", _main_menu())
                return
            if status in ("failed", "cancelled", "expired"):
                if status == "failed":
                    details = _services.get("classify_job_error", lambda unused: None)(job) or {}
                    code = f" [{details.get('error_code')}]" if details.get("error_code") else ""
                    hint = f"\nপরামর্শ: {details.get('recovery_hint')}" if details.get("recovery_hint") else ""
                    await _send_message(chat_id, f"❌{code} {str(job.get('message') or status)[:550]}{hint}", _main_menu())
                else:
                    await _send_message(chat_id, f"❌ {str(job.get('message') or status)[:700]}", _main_menu())
                return
            await asyncio.sleep(2.5)
        await _send_message(chat_id, "Job monitor-এর সময়সীমা শেষ হয়েছে। /status দিয়ে বর্তমান অবস্থা দেখো।", _main_menu())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[telegram] monitor failed job={job_id[:6]} error={type(exc).__name__}")
    finally:
        _monitor_tasks.pop(job_id, None)


async def _send_document(chat_id, file_path: Path, filename: str) -> dict:
    try:
        with file_path.open("rb") as handle:
            return await _api("sendDocument", data={"chat_id": str(chat_id), "caption": "MediaNest download complete"},
                              files={"document": (Path(filename).name, handle, "application/octet-stream")},
                              timeout=180.0)
    except OSError:
        return {"ok": False, "description": "Could not read generated file"}


def _create_download_link(job_id: str, user_id: str) -> str | None:
    base_url = _base_url()
    if not base_url:
        return None
    token = token_urlsafe(32)
    _download_links[token] = {
        "job_id": job_id, "user_id": user_id,
        "expires_at": time.time() + LINK_TTL_SECONDS, "uses": 0,
    }
    return f"{base_url}/telegram/download/{token}"


def _mark_download_link_used(token: str) -> None:
    entry = _download_links.get(token)
    if entry:
        entry["uses"] = int(entry.get("uses", 0)) + 1
        if entry["uses"] >= 3:
            _download_links.pop(token, None)


async def _send_shared_job(chat_id, user_id: str, job_id: str) -> None:
    job = _services.get("jobs", {}).get(job_id)
    if not job or job.get("status") != "completed" or not job.get("output_file"):
        await _send_message(chat_id, "এই job-এর output আর পাওয়া যাচ্ছে না।", _main_menu())
        return
    path = Path(job["output_file"])
    try:
        root = Path(_services["TEMP_ROOT"]).resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise OSError("output missing")
        size = resolved.stat().st_size
    except OSError:
        await _send_message(chat_id, "Output file পাওয়া যাচ্ছে না। নতুন করে download করো।", _main_menu())
        return
    if size <= TELEGRAM_UPLOAD_LIMIT:
        result = await _send_document(chat_id, resolved, job.get("filename") or resolved.name)
        if result.get("ok"):
            # Keep website-generated results available in the website after Telegram shares them.
            if job.get("interface") == "telegram":
                _services["clean_after_download"](job_id)
            await _send_message(chat_id, "✅ ফাইল Telegram-এ পাঠানো হয়েছে।", _main_menu())
            return
    link = _create_download_link(job_id, user_id)
    if link:
        await _send_message(
            chat_id,
            f"ফাইলের size {size / (1024 * 1024):.1f} MB অথবা Telegram upload ব্যর্থ হয়েছে। "
            f"এই temporary link ১৫ মিনিট থাকবে, সর্বোচ্চ ৩ বার ব্যবহার করা যাবে:\n{link}",
            _main_menu(),
        )
    else:
        await _send_message(chat_id, "ফাইলটি Telegram-এ পাঠানো যায়নি। ওয়েবসাইটের Jobs tab থেকে ডাউনলোড করো।",
                            _main_menu())


def _conversion_keyboard(token: str, session: dict) -> dict:
    rows = []
    options = [("best", "Best")]
    for height in session.get("qualities") or []:
        quality = str(height)
        if quality.isdigit() and (quality, quality + "p") not in options:
            options.append((quality, quality + "p"))
    row = []
    for quality, label in options:
        row.append({"text": "MP4 · " + label, "callback_data": f"convert:{token}:mp4:{quality}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if session.get("has_audio"):
        rows.append([{"text": "🎵 Export MP3 audio", "callback_data": f"convert:{token}:mp3:best"}])
    if (session.get("duration") or 0) >= 600:
        rows.append([{"text": "✂️ Trim then convert", "callback_data": f"trimask:{token}"}])
    rows.append([{"text": "⬅️ Menu", "callback_data": "action:menu"}])
    return {"inline_keyboard": rows}


async def _receive_telegram_video(chat_id, user_id: str, message: dict, media: dict) -> None:
    """Download a small Telegram video and register it in MediaNest's converter."""
    services = _services
    if any(job.get("status") == "running" for job in services["jobs"].values()):
        await _send_message(chat_id, "একটি MediaNest processing job চলছে। সেটি শেষ হলে আবার ভিডিও পাঠাও।", _main_menu())
        return
    basename = Path(str(media.get("file_name") or "telegram-video.mp4")).name
    suffix = Path(basename).suffix.lower()
    is_video = bool(message.get("video")) or str(media.get("mime_type") or "").startswith("video/")
    if suffix not in TELEGRAM_VIDEO_EXTENSIONS:
        if is_video:
            suffix = ".mp4"
            basename = str(Path(basename).stem or "telegram-video") + suffix
        else:
            await _send_message(chat_id, "MP4, MOV, MKV, WebM, AVI, M4V, MPEG, MPG অথবা 3GP ভিডিও পাঠাও।", _main_menu())
            return
    try:
        declared_size = int(media.get("file_size") or 0)
    except (TypeError, ValueError):
        declared_size = 0
    if declared_size > TELEGRAM_DOWNLOAD_LIMIT:
        await _send_message(chat_id, "Telegram Bot API দিয়ে 20 MB-এর বেশি ফাইল নামানো যায় না। ছোট ফাইল পাঠাও অথবা website Converter ব্যবহার করো।", _main_menu())
        return
    file_id = str(media.get("file_id") or "")
    if not file_id:
        await _send_message(chat_id, "Telegram video file ID পাওয়া যায়নি। আবার পাঠাও।", _main_menu())
        return

    media_id = token_hex(12)
    directory = Path(services["TEMP_ROOT"]) / ("upload-telegram-" + media_id)
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / ("source" + suffix)
    try:
        file_response = await _api("getFile", data={"file_id": file_id}, timeout=25.0)
        file_info = file_response.get("result") if file_response.get("ok") else None
        file_path = (file_info or {}).get("file_path")
        if not file_path:
            raise RuntimeError("Telegram could not prepare this file.")
        try:
            size = int((file_info or {}).get("file_size") or declared_size or 0)
        except (TypeError, ValueError):
            size = declared_size
        if size > TELEGRAM_DOWNLOAD_LIMIT:
            raise RuntimeError("Telegram Bot API download limit is 20 MB.")
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        total = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False) as client:
            async with client.stream("GET", download_url) as response:
                if response.status_code != 200:
                    raise RuntimeError("Telegram file download failed.")
                content_length = response.headers.get("content-length")
                try:
                    if content_length and int(content_length) > TELEGRAM_DOWNLOAD_LIMIT:
                        raise RuntimeError("Telegram Bot API download limit is 20 MB.")
                except ValueError:
                    pass
                with target.open("wb") as output:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > TELEGRAM_DOWNLOAD_LIMIT:
                            raise RuntimeError("Telegram Bot API download limit is 20 MB.")
                        output.write(chunk)
        if not total:
            raise RuntimeError("Empty video file.")
        details = await asyncio.to_thread(services["probe_local_video"], target)
        services["uploads"][media_id] = {
            "id": media_id, "path": str(target), "directory": str(directory),
            "filename": basename[:160], "created_at": time.time(),
            "duration": details["duration"], "height": details["height"],
            "qualities": details["qualities"], "has_audio": details["has_audio"],
            "interface": "telegram", "telegram_user_id": user_id,
        }
        token = token_hex(4)
        session = {
            "owner": user_id, "media_id": media_id, "title": basename[:150],
            "qualities": details["qualities"], "has_audio": details["has_audio"],
            "duration": details["duration"], "created_at": time.time(), "trim": None,
        }
        _converter_sessions[token] = session
        _prune_sessions()
        await _send_message(
            chat_id,
            f"✅ ভিডিও inspect করা হয়েছে: {basename}\nSize: {total / (1024 * 1024):.1f} MB · Duration: {services['duration_text'](details['duration'])}\n\nMP4 quality বা MP3 বেছে নাও।",
            _conversion_keyboard(token, session),
        )
    except asyncio.CancelledError:
        import shutil
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except Exception as exc:
        import shutil
        shutil.rmtree(directory, ignore_errors=True)
        print(f"[telegram-converter] upload failed user={user_id} error={type(exc).__name__}")
        detail = ("Telegram Bot API download limit is 20 MB." if "20 MB" in str(exc)
                  else "এই ফাইলটি supported video হিসেবে পড়া যায়নি। MP4/MOV/MKV/WebM file দিয়ে চেষ্টা করো।")
        await _send_message(chat_id, "❌ " + detail, _main_menu())


async def _start_uploaded_conversion(chat_id, user_id: str, token: str, output_format: str, quality: str) -> None:
    session = _converter_sessions.get(token)
    if not session or session.get("owner") != user_id or time.time() - session.get("created_at", 0) > SESSION_TTL_SECONDS:
        await _send_message(chat_id, "এই conversion selection-এর মেয়াদ শেষ। আবার ভিডিও পাঠাও।", _main_menu())
        return
    services = _services
    media_id = session.get("media_id")
    source = services["uploads"].get(media_id)
    if not source or source.get("telegram_user_id") != user_id:
        await _send_message(chat_id, "Uploaded video-এর মেয়াদ শেষ। আবার ভিডিও পাঠাও।", _main_menu())
        return
    if any(job.get("status") == "running" for job in services["jobs"].values()):
        await _send_message(chat_id, "একটি MediaNest processing job চলছে। সেটি শেষ হলে আবার চেষ্টা করো।", _main_menu())
        return
    if output_format not in ("mp4", "mp3"):
        await _send_message(chat_id, "Output format সমর্থিত নয়।", _main_menu())
        return
    if output_format == "mp3":
        quality = "best"
        if not source.get("has_audio"):
            await _send_message(chat_id, "এই ভিডিওতে audio track নেই, তাই MP3 তৈরি করা যাবে না।", _main_menu())
            return
    elif quality != "best" and quality not in {str(q) for q in source.get("qualities", [])}:
        await _send_message(chat_id, "এই video quality পাওয়া যাচ্ছে না। আবার ভিডিও পাঠাও।", _main_menu())
        return
    job_id = token_hex(10)
    title = str(session.get("title") or "Telegram video")
    services["jobs"][job_id] = {
        "id": job_id, "status": "running", "progress": 0,
        "message": "Telegram upload conversion শুরু হচ্ছে…", "created_at": time.time(),
        "output_file": None, "title": title,
        "filename": services["safe_download_filename"](title, output_format),
        "interface": "telegram", "kind": "conversion", "format": output_format,
        "upload_id": media_id, "telegram_user_id": user_id, "error_code": None,
    }
    _job_owners[job_id] = user_id
    services["jobs"][job_id]["task"] = asyncio.create_task(
        services["process_local_conversion"](job_id, media_id, quality, output_format, session.get("trim"))
    )
    sent = await _send_message(
        chat_id, f"⏳ Conversion চলছে: {title}\nFormat: {output_format.upper()} · Quality: {quality}\nTrim: {'on' if session.get('trim') else 'off'}",
        {"inline_keyboard": [[{"text": "📊 Status", "callback_data": "action:status"},
                              {"text": "⛔ Cancel", "callback_data": f"cancel:{job_id}"}]]},
    )
    message_id = (sent.get("result") or {}).get("message_id")
    _monitor_tasks[job_id] = asyncio.create_task(_monitor_job(chat_id, user_id, job_id, message_id))


def _parse_trim_range(raw: str, duration: int | None):
    if duration is None or duration < 600:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[,;–-]\s*(\d+(?:\.\d+)?)\s*", raw)
    if not match:
        return None
    start, end = float(match.group(1)), float(match.group(2))
    if start < 0 or end <= start or end > duration or end - start < 1:
        return None
    return (start, end)


async def _handle_callback(callback: dict) -> None:
    callback_id = callback.get("id")
    user = callback.get("from") or {}
    user_id = str(user.get("id") or "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if callback_id:
        await _answer_callback(callback_id)
    if not user_id or str(chat.get("type", "")) != "private":
        return
    if user_id not in ALLOWED_USER_IDS:
        await _send_message(chat_id, "⛔ এই bot ব্যবহারের অনুমতি নেই।", None)
        return
    data = str(callback.get("data") or "")
    if data == "action:menu":
        await _send_message(chat_id, "MediaNest Bot menu:", _main_menu())
        return
    if data == "action:status":
        await _show_status(chat_id, user_id)
        return
    if data == "action:cancel":
        keyboard = _cancel_keyboard(user_id)
        await _send_message(chat_id, "যে running job বাতিল করতে চাও সেটি বেছে নাও।" if keyboard else
                            "তোমার কোনো running job নেই।", keyboard or _main_menu())
        return
    if data in ("mode:video", "mode:image", "mode:convert"):
        mode = data.split(":", 1)[1]
        _user_modes[user_id] = mode
        if mode == "convert":
            _pending_urls.pop(user_id, None)
            await _send_message(chat_id, "🎞 Convert করতে MP4/MOV/MKV/WebM ভিডিও file পাঠাও। Telegram Bot API-তে upload থেকে download limit 20 MB।", _main_menu())
            return
        pending = _pending_urls.pop(user_id, None)
        if pending and mode in ("video", "image"):
            _spawn(_process_url(chat_id, user_id, pending, mode))
        else:
            label = "ভিডিও" if mode == "video" else "ইমেজ"
            await _send_message(chat_id, f"{label} download-এর public URL পাঠাও।", _main_menu())
        return
    if data.startswith("dl:"):
        pieces = data.split(":")
        if len(pieces) == 4:
            _, token, output_format, quality = pieces
            await _start_video_download(chat_id, user_id, token, output_format, quality)
        return
    if data.startswith("trimask:"):
        token = data.split(":", 1)[1]
        session = _converter_sessions.get(token)
        if not session or session.get("owner") != user_id:
            await _send_message(chat_id, "এই converter session পাওয়া যাচ্ছে না। আবার ভিডিও পাঠাও।", _main_menu())
            return
        if (session.get("duration") or 0) < 600:
            await _send_message(chat_id, "Trim করতে ভিডিওর duration কমপক্ষে ১০ মিনিট হতে হবে।", _main_menu())
            return
        _trim_waiting[user_id] = token
        await _send_message(chat_id, "Trim range seconds-এ পাঠাও: start,end\nউদাহরণ: 15,45\nEnd অবশ্যই ভিডিওর duration-এর মধ্যে হতে হবে।")
        return
    if data.startswith("convert:"):
        pieces = data.split(":")
        if len(pieces) == 4:
            _, token, output_format, quality = pieces
            await _start_uploaded_conversion(chat_id, user_id, token, output_format, quality)
        return
    if data.startswith("send:"):
        job_id = data.split(":", 1)[1]
        job = _services.get("jobs", {}).get(job_id)
        if _job_owners.get(job_id) != user_id and not (job and job.get("interface") == "website"):
            await _send_message(chat_id, "এই job-এর ফাইল নেওয়ার অনুমতি নেই।", _main_menu())
            return
        await _send_shared_job(chat_id, user_id, job_id)
        return
    if data.startswith("cancel:"):
        job_id = data.split(":", 1)[1]
        job = _services.get("jobs", {}).get(job_id)
        if _job_owners.get(job_id) != user_id and not (job and job.get("interface") == "website"):
            await _send_message(chat_id, "এই job বাতিল করার অনুমতি নেই।", _main_menu())
            return
        if not job or job.get("status") != "running":
            await _send_message(chat_id, "এই job আর running নেই।", _main_menu())
            return
        await _services["cancel_job"](job_id)
        monitor = _monitor_tasks.get(job_id)
        if monitor and not monitor.done():
            monitor.cancel()
        await _send_message(chat_id, "⛔ Job বাতিল করা হয়েছে।", _main_menu())


async def _handle_message(message: dict) -> None:
    user = message.get("from") or {}
    user_id = str(user.get("id") or "")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not user_id or str(chat.get("type", "")) != "private":
        return
    if user_id not in ALLOWED_USER_IDS:
        await _send_message(chat_id, "⛔ এই bot ব্যবহারের অনুমতি নেই।")
        return

    text = str(message.get("text") or "").strip()
    media = message.get("video") or message.get("document")
    if media:
        mode = _user_modes.pop(user_id, None)
        if mode != "convert":
            await _send_message(chat_id, "ভিডিও file convert করতে menu থেকে *Convert a video* বেছে নাও, তারপর file পাঠাও।", _main_menu())
            return
        _spawn(_receive_telegram_video(chat_id, user_id, message, media))
        return

    if not text:
        await _send_message(chat_id, "একটি public URL পাঠাও অথবা menu থেকে Video/Image/Converter বেছে নাও।", _main_menu())
        return

    command = text.split()[0].split("@")[0].lower()
    if command in ("/start", "/help"):
        await _send_message(chat_id,
            "👋 MediaNest Bot-এ স্বাগতম!\n\nVideo বা Image বেছে public link পাঠাও, অথবা Convert a video চেপে নিজের ছোট video file পাঠাও। MP4 quality, MP3, trim, status ও cancel ব্যবহার করা যাবে।\nPrivate/login-required content সমর্থিত নয়।",
            _main_menu())
        return
    if command == "/video":
        _user_modes[user_id] = "video"
        await _send_message(chat_id, "🎬 ভিডিওর public URL পাঠাও।", _main_menu())
        return
    if command == "/image":
        _user_modes[user_id] = "image"
        await _send_message(chat_id, "🖼 ছবির public post বা direct image URL পাঠাও।", _main_menu())
        return
    if command == "/convert":
        _user_modes[user_id] = "convert"
        await _send_message(chat_id, "🎞 Convert করতে MP4/MOV/MKV/WebM ভিডিও file পাঠাও (সর্বোচ্চ 20 MB)।", _main_menu())
        return
    if command == "/status":
        await _show_status(chat_id, user_id)
        return
    if command == "/cancel":
        keyboard = _cancel_keyboard(user_id)
        await _send_message(chat_id, "বাতিল করার job বেছে নাও।" if keyboard else "তোমার কোনো running job নেই।",
                            keyboard or _main_menu())
        return

    pending_trim_token = _trim_waiting.get(user_id)
    if pending_trim_token:
        session = _converter_sessions.get(pending_trim_token)
        parsed_trim = _parse_trim_range(text, (session or {}).get("duration")) if session else None
        if not parsed_trim:
            await _send_message(chat_id, "Trim range সঠিক নয়। seconds-এ start,end দাও, যেমন 15,45; duration-এর বাইরে দেওয়া যাবে না।")
            return
        session["trim"] = parsed_trim
        _trim_waiting.pop(user_id, None)
        await _send_message(chat_id, f"✅ Trim set: {parsed_trim[0]:g}s–{parsed_trim[1]:g}s. এখন output format/quality বেছে নাও।",
                            _conversion_keyboard(pending_trim_token, session))
        return

    match = re.search(r"https?://[^\s<>]+", text)
    if not match:
        await _send_message(chat_id, "সঠিক HTTPS public link পাঠাও অথবা menu থেকে Video/Image বেছে নাও।",
                            _main_menu())
        return
    raw_url = match.group(0).rstrip(".,);]")
    mode = _user_modes.pop(user_id, None)
    if mode in ("video", "image"):
        _spawn(_process_url(chat_id, user_id, raw_url, mode))
    elif mode == "convert":
        _user_modes[user_id] = "convert"
        await _send_message(chat_id, "Converter-এর জন্য public URL নয়, সরাসরি video file পাঠাও।", _main_menu())
    else:
        _pending_urls[user_id] = raw_url
        await _send_message(chat_id, "লিংক পেয়েছি। কী download করতে চাও?", _mode_menu())


async def _handle_update(update: dict) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        await _handle_callback(callback)
        return
    message = update.get("message")
    if isinstance(message, dict):
        await _handle_message(message)


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not BOT_TOKEN or not WEBHOOK_SECRET or not ALLOWED_USER_IDS:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid update")
    # Return quickly to Telegram; downloads and metadata checks run as background tasks.
    _spawn(_handle_update(update))
    return {"ok": True}


@router.get("/telegram/download/{download_token}")
async def telegram_download(download_token: str):
    _prune_sessions()
    entry = _download_links.get(download_token)
    if not entry or entry.get("expires_at", 0) < time.time() or entry.get("uses", 0) >= 3:
        _download_links.pop(download_token, None)
        raise HTTPException(status_code=404, detail="This temporary download link has expired.")
    job = _services.get("jobs", {}).get(entry.get("job_id"))
    if not job or job.get("status") != "completed" or not job.get("output_file"):
        _download_links.pop(download_token, None)
        raise HTTPException(status_code=404, detail="The temporary file is no longer available.")
    path = Path(job["output_file"])
    root = Path(_services["TEMP_ROOT"]).resolve()
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise HTTPException(status_code=404, detail="The temporary file is not available.")
    except OSError:
        raise HTTPException(status_code=404, detail="The temporary file is not available.")
    return FileResponse(
        resolved, filename=job.get("filename") or resolved.name,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
        background=BackgroundTask(_mark_download_link_used, download_token),
    )
