import asyncio
import time

import app


def test_shared_job_list_returns_website_and_telegram_jobs_without_internal_fields():
    original = app.jobs.copy()
    now = time.time()
    try:
        app.jobs.clear()
        app.jobs["test-web-job"] = {
            "id": "test-web-job",
            "status": "running",
            "progress": 25,
            "message": "Website progress",
            "created_at": now,
            "title": "Website image archive",
            "filename": None,
            "output_file": None,
            "interface": "website",
            "kind": "image",
            "format": "zip-or-image",
            "task": object(),
            "process": object(),
        }
        app.jobs["test-telegram-job"] = {
            "id": "test-telegram-job",
            "status": "completed",
            "progress": 100,
            "message": "Telegram output ready",
            "created_at": now + 1,
            "title": "Telegram video",
            "filename": "video.mp4",
            "output_file": None,
            "interface": "telegram",
            "kind": "video",
            "format": "mp4",
        }

        response = asyncio.run(app.list_jobs())
        assert response["running"] is True
        assert response["limit"] == 1
        assert len(response["jobs"]) == 2
        by_id = {item["id"]: item for item in response["jobs"]}
        assert by_id["test-web-job"]["interface"] == "website"
        assert by_id["test-web-job"]["kind"] == "image"
        assert by_id["test-telegram-job"]["interface"] == "telegram"
        assert by_id["test-telegram-job"]["kind"] == "video"
        assert "task" not in by_id["test-web-job"]
        assert "process" not in by_id["test-web-job"]
        assert "output_file" not in by_id["test-web-job"]
    finally:
        app.jobs.clear()
        app.jobs.update(original)


def test_job_error_classifier_returns_actionable_codes():
    assert app.classify_job_error({
        "status": "failed",
        "message": "Processing reached the 15-minute limit",
    })["error_code"] == "PROCESSING_TIMEOUT"
    restricted = app.classify_job_error({
        "status": "failed",
        "message": "YouTube bot verification was blocked",
    })
    assert restricted["error_code"] == "SOURCE_ACCESS_RESTRICTED"
    assert restricted["recovery_hint"]
    assert app.classify_job_error({
        "status": "failed",
        "message": "The output exceeded the 400 MB size limit",
    })["error_code"] == "FILE_SIZE_LIMIT"
    assert app.classify_job_error({
        "status": "completed",
        "message": "All done",
    }) is None


def test_telegram_hosted_and_local_api_file_limits_are_bounded():
    from telegram_bot import _telegram_file_limits

    hosted_upload, hosted_download = _telegram_file_limits(
        "https://api.telegram.org", upload_mb="1900", download_mb="1900"
    )
    assert hosted_upload == 47 * 1024 * 1024
    assert hosted_download == 20 * 1024 * 1024

    local_upload, local_download = _telegram_file_limits(
        "http://localhost:8081", upload_mb="1900", download_mb="650"
    )
    assert local_upload == 1900 * 1024 * 1024
    assert local_download == 650 * 1024 * 1024

    # Invalid values fall back to safe defaults and cannot exceed local limits.
    default_upload, default_download = _telegram_file_limits("http://bot-api.internal")
    assert default_upload == 400 * 1024 * 1024
    assert default_download == 400 * 1024 * 1024
    capped_upload, capped_download = _telegram_file_limits(
        "http://localhost:8081", upload_mb="9999", download_mb="-3"
    )
    assert capped_upload == 1900 * 1024 * 1024
    assert capped_download == 1 * 1024 * 1024


def test_telegram_trim_range_validation():
    from telegram_bot import _parse_trim_range

    assert _parse_trim_range("15,45", 900) == (15.0, 45.0)
    assert _parse_trim_range("15 - 45", 900) == (15.0, 45.0)
    assert _parse_trim_range("45,15", 900) is None
    assert _parse_trim_range("15,901", 900) is None
    assert _parse_trim_range("not-a-range", 900) is None
    assert _parse_trim_range("15,45", 599) is None


def test_telegram_conversion_keyboard_offers_mp4_mp3_and_trim_when_available():
    from telegram_bot import _conversion_keyboard

    keyboard = _conversion_keyboard("abcd1234", {
        "qualities": [720, 360],
        "has_audio": True,
        "duration": 1200,
    })
    callbacks = [
        button["callback_data"]
        for row in keyboard["inline_keyboard"]
        for button in row
    ]
    assert "convert:abcd1234:mp4:best" in callbacks
    assert "convert:abcd1234:mp4:720" in callbacks
    assert "convert:abcd1234:mp3:best" in callbacks
    assert "trimask:abcd1234" in callbacks
    assert all(len(callback) <= 64 for callback in callbacks)


def test_telegram_converter_rejects_files_over_hosted_bot_api_limit(tmp_path, monkeypatch):
    import telegram_bot

    messages = []

    async def fake_send(chat_id, text, reply_markup=None):
        messages.append(text)
        return {"ok": True}

    monkeypatch.setattr(telegram_bot, "_send_message", fake_send)
    telegram_bot.configure_telegram({
        "jobs": {},
        "uploads": {},
        "TEMP_ROOT": tmp_path,
    })
    asyncio.run(telegram_bot._receive_telegram_video(
        123,
        "456",
        {"video": {"file_id": "file-id", "file_size": telegram_bot.TELEGRAM_DOWNLOAD_LIMIT + 1,
                   "file_name": "large.mp4"}},
        {"file_id": "file-id", "file_size": telegram_bot.TELEGRAM_DOWNLOAD_LIMIT + 1,
         "file_name": "large.mp4"},
    ))
    assert messages
    assert "20 MB" in messages[0]
    assert list(tmp_path.iterdir()) == []


def test_telegram_converter_trim_buttons_hidden_for_short_video():
    from telegram_bot import _conversion_keyboard

    keyboard = _conversion_keyboard("deadbeef", {
        "qualities": [360],
        "has_audio": True,
        "duration": 300,
    })
    callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
    assert "trimask:deadbeef" not in callbacks


def test_telegram_converter_downloads_and_inspects_a_small_upload(tmp_path, monkeypatch):
    import telegram_bot

    original_services = telegram_bot._services.copy()
    original_session_tokens = set(telegram_bot._converter_sessions)
    messages = []
    calls = []

    async def fake_api(method, *, data=None, files=None, timeout=20.0):
        calls.append((method, data))
        return {"ok": True, "result": {"file_path": "video/source.mp4", "file_size": 14}}

    async def fake_send(chat_id, text, reply_markup=None):
        messages.append((text, reply_markup))
        return {"ok": True}

    class FakeResponse:
        status_code = 200
        headers = {"content-length": "14"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self, chunk_size=65536):
            yield b"fake-video-data"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url.startswith(telegram_bot.TELEGRAM_API_BASE_URL + "/file/bot")
            return FakeResponse()

    async def fake_probe(path):
        raise AssertionError("probe_local_video must run in a worker thread")

    def probe_video(path):
        assert path.is_file()
        assert path.read_bytes() == b"fake-video-data"
        return {"duration": 1200, "height": 720, "qualities": [720, 360], "has_audio": True}

    monkeypatch.setattr(telegram_bot, "_api", fake_api)
    monkeypatch.setattr(telegram_bot, "_send_message", fake_send)
    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", FakeClient)
    telegram_bot.configure_telegram({
        "jobs": {},
        "uploads": {},
        "TEMP_ROOT": tmp_path,
        "probe_local_video": probe_video,
        "duration_text": lambda value: "20:00",
    })

    try:
        media = {"file_id": "file-id", "file_size": 14, "file_name": "source.mp4"}
        asyncio.run(telegram_bot._receive_telegram_video(
            123, "456", {"video": media}, media
        ))
        assert calls[0][0] == "getFile"
        assert calls[0][1]["file_id"] == "file-id"
        assert len(telegram_bot._services["uploads"]) == 1
        assert messages
        keyboard = messages[-1][1]
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        assert any(callback.startswith("convert:") and ":mp4:" in callback for callback in callbacks)
        assert any(callback.startswith("convert:") and ":mp3:" in callback for callback in callbacks)
        assert any(callback.startswith("trimask:") for callback in callbacks)
    finally:
        telegram_bot.configure_telegram(original_services)
        for token in set(telegram_bot._converter_sessions) - original_session_tokens:
            telegram_bot._converter_sessions.pop(token, None)
