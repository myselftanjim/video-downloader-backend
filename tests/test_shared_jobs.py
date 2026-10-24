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
