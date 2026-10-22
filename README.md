# MediaNest — Personal Video Tool

**Built by ReelsByTanjim.**

A password-protected media utility for personal/family use (a few people). Built with FastAPI, yt-dlp, FFmpeg and a responsive browser UI.

## Features

- Password-protected access code with signed HTTP-only session cookie and basic login throttling.
- Video metadata lookup and format/quality selection for supported public YouTube, Facebook, Instagram, Pinterest and TikTok URLs, including Pinterest `pin.it` share links.
- MP4 video and MP3 audio download for accessible sources, subject to source availability and platform rules.
- Local video upload fallback (MP4, MOV, MKV, WebM, AVI, M4V, MPEG, MPG, 3GP) up to 90 MB, useful when a platform blocks cloud-server extraction.
- Local upload conversion into MP4/MP3, optional resolution downscale, and Trim only when video duration is at least 10 minutes.
- Backend-side validation of the same Trim rule, one processing job at a time, 15-minute processing deadline, 4-hour maximum duration and 400 MB output limit.
- Temporary files removed after download or expiry; no persistent media library.

## Deploy to Render

The repository includes a Dockerfile and `render.yaml` Blueprint for a Render Free web service. See [DEPLOY_BN.md](DEPLOY_BN.md) for Bengali steps.

Required secret environment variables:
- `APP_PASSWORD`: long, unique shared family access code.
- `SESSION_SECRET`: long random secret used to sign sessions.

Optional:
- `MAX_DOWNLOAD_MB`: 50–1000, default 400.
- `MAX_UPLOAD_MB`: 10–95, default 90.

Never commit environment secrets to GitHub.

## Known limitations

- YouTube may reject requests from shared cloud IPs with “Sign in to confirm you're not a bot.” MediaNest now displays a plain-language explanation. It does not bypass bot checks or authentication controls. For videos you own, use YouTube Studio or Google Takeout to download the file, then upload the file into MediaNest for processing.
- Other social platforms can change page formats, require login, rate-limit requests, or disallow downloads. Not every URL is guaranteed to work.
- Render Free sleeps after inactivity and has limited CPU/RAM plus ephemeral disk. FFmpeg, long videos, large uploads and 4K processing can fail or time out; 24/7 uptime is not guaranteed.
- Uploaded and generated files are temporary. The app supports only MP4/MP3 output in this version. Actual output quality depends on available source media and server capacity.
- Use only videos you own or have permission to download. Follow each platform's terms. DRM, paywalls, login challenges, and platform access controls are not bypassed.
