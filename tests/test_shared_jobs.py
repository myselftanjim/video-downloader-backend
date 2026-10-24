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
