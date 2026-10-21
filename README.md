# ClipNest — Personal Video Tool

A password-protected video utility for personal/family use (a few people). Built with FastAPI, yt-dlp, FFmpeg and a responsive browser UI.

## Features

- Password-protected access-code login with signed, HTTP-only session cookie and basic login throttling.
- Video metadata lookup and real source quality choices for supported public YouTube, Facebook, Instagram and TikTok URLs.
- MP4 video download or MP3 audio extraction when the source is accessible and the user owns the media or has permission to save it.
- Optional trimming only for videos that are at least 10 minutes long; the backend enforces this even if someone edits browser requests.
- One active download job at a time, 15-minute processing deadline, 4-hour source-duration ceiling, and 400 MB default output cap.
- Temporary output files; output is removed after download or after 30 minutes.

## Deploy to Render

The repository includes a Dockerfile and `render.yaml` Blueprint for the Render Free web service. See [DEPLOY_BN.md](DEPLOY_BN.md) for Bengali steps.

Required secret environment variables:
- `APP_PASSWORD`: shared family access code; use a long unique value.
- `SESSION_SECRET`: a long random secret for signed sessions (Render Blueprint generates this).

Optional:
- `MAX_DOWNLOAD_MB`: 50–1000, default 400.

Do not commit environment secrets into Git. The Render Free service may sleep after inactivity, has limited CPU/RAM and ephemeral disk, and does not provide guaranteed 24/7 uptime.

## Limitations and responsible use

- A social platform can change pages, require authentication, rate-limit requests, or disallow downloads. Not every URL is guaranteed to work.
- This app does not bypass DRM, paywalls, login challenges, or platform access controls.
- Use only videos you own or have permission to download. Follow each platform's terms.
- Only MP4 and MP3 are currently implemented in the interface. Actual quality choices depend on available source formats.
