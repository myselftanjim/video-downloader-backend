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
LINK_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 15 * 60

router = APIRouter()
_services: dict = {}
_user_modes: dict[str, str] = {}
_pending_urls: dict[str, str] = {}
_video_sessions: dict[str, dict] = {}
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
    for job_id, owner in list(_job_owners.items()):
        job = jobs.get(job_id)
        if owner == user_id and job and job.get("status") == "running":
            rows.append([{"text": f"Cancel {str(job.get('title') or job_id)[:24]}",
                          "callback_data": f"cancel:{job_id}"}])
    return {"inline_keyboard": rows} if rows else None


def _owned_jobs(user_id: str) -> list[tuple[str, dict]]:
    jobs = _services.get("jobs", {})
    found = []
    for job_id, owner in list(_job_owners.items()):
        job = jobs.get(job_id)
        if owner == user_id and job:
            found.append((job_id, job))
    return sorted(found, key=lambda pair: pair[1].get("created_at", 0), reverse=True)[:5]


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
        lines.append(f"• {title}\n  {status} · {progress}%")
        if status == "RUNNING":
            rows.append([{"text": f"Cancel {job_id[:6]}", "callback_data": f"cancel:{job_id}"}])
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
    if data in ("mode:video", "mode:image"):
        mode = data.split(":", 1)[1]
        _user_modes[user_id] = mode
        pending = _pending_urls.pop(user_id, None)
        if pending:
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
    if data.startswith("cancel:"):
        job_id = data.split(":", 1)[1]
        if _job_owners.get(job_id) != user_id:
            await _send_message(chat_id, "এই job বাতিল করার অনুমতি নেই।", _main_menu())
            return
        job = _services.get("jobs", {}).get(job_id)
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
    if not text:
        await _send_message(chat_id, "একটি public URL পাঠাও অথবা menu থেকে Video/Image বেছে নাও।", _main_menu())
        return

    command = text.split()[0].split("@")[0].lower()
    if command in ("/start", "/help"):
        await _send_message(chat_id,
            "👋 MediaNest Bot-এ স্বাগতম!\n\nVideo বা Image বেছে নাও, তারপর public link পাঠাও। MP4 quality নির্বাচন, MP3, status ও cancel করা যাবে।\nPrivate/login-required content সমর্থিত নয়।",
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
    if command == "/status":
        await _show_status(chat_id, user_id)
        return
    if command == "/cancel":
        keyboard = _cancel_keyboard(user_id)
        await _send_message(chat_id, "বাতিল করার job বেছে নাও।" if keyboard else "তোমার কোনো running job নেই।",
                            keyboard or _main_menu())
        return

    match = re.search(r"https?://[^\s<>]+", text)
    if not match:
        await _send_message(chat_id, "সঠিক HTTPS public link পাঠাও অথবা menu থেকে Video/Image বেছে নাও।",
                            _main_menu())
        return
    raw_url = match.group(0).rstrip(".,);]")
    mode = _user_modes.pop(user_id, None)
    if mode:
        _spawn(_process_url(chat_id, user_id, raw_url, mode))
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
