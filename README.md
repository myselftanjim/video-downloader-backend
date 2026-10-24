# MediaNest — Personal Video Tool

**Built by ReelsByTanjim.**

A password-protected media utility for personal/family use (a few people). Built with FastAPI, yt-dlp, gallery-dl, FFmpeg and a responsive browser UI.

## Features

- Password-protected access code with signed HTTP-only session cookie and basic login throttling.
- Video metadata lookup and format/quality selection for supported public YouTube, Facebook, Instagram, Pinterest and TikTok URLs, including Pinterest `pin.it` share links.
- MP4 video and MP3 audio download for accessible sources, subject to source availability and platform rules.
- High-quality image downloader for allowlisted direct CDN URLs and supported public post/Pin/gallery links, including Pinterest, Instagram, Facebook, TikTok, X/Twitter, Reddit, Tumblr, Flickr, Imgur, DeviantArt, Pixiv, Pexels, Unsplash, Pixabay, ArtStation, Behance, Bluesky and Wikimedia. It tries source/original-size CDN variants first, prefers gallery-dl source images, packs multi-image posts as ZIP without resizing/recompressing, and falls back to the best available yt-dlp thumbnail when the source exposes one. Limited to 40 images and the configured output size cap per job.
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


## Telegram Bot (Phase A)

The Telegram bot shares the existing FastAPI process and MediaNest job engine; no separate worker service is required. It uses a Telegram webhook and is disabled by default until all required environment variables are present.

Configure these variables in Render (never commit the bot token):
- \`TELEGRAM_BOT_TOKEN\`: token created by \`@BotFather\`.
- \`TELEGRAM_ALLOWED_USER_IDS\`: comma-separated numeric Telegram user IDs allowed to use the bot. Keep this list private and small.
- \`TELEGRAM_WEBHOOK_SECRET\`: random secret containing only letters, numbers, underscores or hyphens.
- \`TELEGRAM_WEBHOOK_URL\`: public HTTPS URL ending in \`/telegram/webhook\`, for example \`https://medianest.reelsbytanjim.com/telegram/webhook\` if that domain is live.

On startup, MediaNest calls Telegram's \`setWebhook\` method. In a private chat, the allowlisted user can choose Video or Image, paste a public URL, choose an available MP4 quality or MP3, check job status, or cancel their own running job. Completed files up to 49 MiB are sent through Telegram; larger outputs use an expiring temporary download link when a public base URL is available.

The bot intentionally works only in private chats and only for user IDs in the allowlist. Job state and temporary links are in memory, so a Render restart clears them. Phase A needs a real deployment and live tests before it can be considered operational.

## Known limitations

- YouTube may reject requests from shared cloud IPs with “Sign in to confirm you're not a bot.” MediaNest now displays a plain-language explanation. It does not bypass bot checks or authentication controls. For videos you own, use YouTube Studio or Google Takeout to download the file, then upload the file into MediaNest for processing.
- Image extraction varies by platform and post type. Some gallery-dl extractors may require cookies or authentication not configured in this private app; private/login-restricted content is not bypassed. Video links can return only a thumbnail, not the original post image.
- Other social platforms can change page formats, require login, rate-limit requests, or disallow downloads. Not every URL is guaranteed to work.
- Render Free sleeps after inactivity and has limited CPU/RAM plus ephemeral disk. FFmpeg, long videos, large uploads and 4K processing can fail or time out; 24/7 uptime is not guaranteed.
- Uploaded and generated files are temporary. The app supports only MP4/MP3 output in this version. Actual output quality depends on available source media and server capacity.
- Use only videos you own or have permission to download. Follow each platform's terms. DRM, paywalls, login challenges, and platform access controls are not bypassed.
