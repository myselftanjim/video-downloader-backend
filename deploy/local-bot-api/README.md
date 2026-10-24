# MediaNest — Optional Telegram Local Bot API

This optional deployment is for MediaNest owners who need Telegram attachments larger than the hosted Bot API limit. The official Local Bot API server can download files without the hosted API's 20 MB limit and upload files up to 2000 MB when started with `--local`.

Official documentation:
- https://core.telegram.org/bots/api#using-a-local-bot-api-server
- https://github.com/tdlib/telegram-bot-api

## Important before enabling it

- This is a separate server, not part of the MediaNest Render web service. It needs a persistent disk and enough RAM/CPU for large transfers.
- Use HTTPS through a TLS reverse proxy if the server is reached over the public internet. The Bot API process itself speaks HTTP. Do not expose port 8081 directly to the public internet without appropriate network protection.
- Never commit the bot token, Telegram API hash, or API ID to GitHub. Store secrets only in the server/Render secret manager.
- To move the bot from Telegram's hosted Bot API, Telegram requires a successful `logOut` call to `https://api.telegram.org` before using the bot on the local server. Do not do this until the Local Bot API endpoint is running and the cutover can be completed.
- The Local Bot API's `getFile` response in `--local` mode is an absolute path on the Local Bot API server. MediaNest can read incoming files above 20 MB only if the exact same data volume is mounted at the same path in the MediaNest app container and `TELEGRAM_LOCAL_FILE_ROOT` is configured. A separate VPS and Render service do not share a filesystem automatically.
- The default MediaNest file limit is 400 MB. Raising Telegram's limit does not raise MediaNest's own processing limits, Render's RAM/CPU/time limits, or guarantee that a large conversion will succeed.

## Build the official server on a dedicated Linux host

Obtain your Telegram `api_id` and `api_hash` from https://my.telegram.org. These are not your BotFather bot token.

Install build dependencies on a supported Linux machine (the exact package names vary by distribution). On Debian/Ubuntu:

```sh
sudo apt-get update
sudo apt-get install -y git cmake g++ zlib1g-dev libssl-dev gperf
git clone --recursive https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api
mkdir build
cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build . --target install -j2
```

Create a persistent data directory owned by the account that runs the service, then launch the server with your own API ID/hash:

```sh
sudo mkdir -p /var/lib/telegram-bot-api
sudo chown "$USER" /var/lib/telegram-bot-api
telegram-bot-api --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" --local --http-port=8081 --dir=/var/lib/telegram-bot-api
```

Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in the server's private environment first. Use a process manager such as systemd to keep it running and restart it after host reboot.

## Point MediaNest at it

After the Local Bot API service is reachable through a protected HTTPS endpoint, configure these Render environment variables:

- `TELEGRAM_API_BASE_URL`: HTTPS base URL ending at the proxy root, for example `https://bot-api.example.com` (do not include `/bot` or the token).
- `TELEGRAM_UPLOAD_LIMIT_MB`: for example `400` initially; only raise it after checking processing resources.
- `TELEGRAM_DOWNLOAD_LIMIT_MB`: for example `400` initially.
- `TELEGRAM_LOCAL_FILE_ROOT`: only if MediaNest and the Local Bot API share the same persistent volume mounted at the same absolute path. This is required for large incoming Converter uploads; leave unset when no shared mount exists.

A safe migration order is: deploy and test the API server; make sure the public endpoint has TLS/network protection; configure the MediaNest endpoint; then call the hosted Bot API `logOut` using your secret bot token and redeploy the app so startup can register the webhook against the local endpoint. Avoid posting token-bearing URLs in chats, screenshots, source files, or logs. Once migrated, test `/start`, a small download, a result larger than 47 MiB, and webhook delivery before relying on it.

## What works without a shared volume?

When `TELEGRAM_API_BASE_URL` points to the Local Bot API, sending generated output from MediaNest to Telegram can use the larger upload limit. However, incoming large files for the Converter require the shared-volume configuration above because the Local Bot API returns its own absolute filesystem path in local mode. If no shared volume is available, keep using the hosted Bot API for incoming Converter uploads (20 MB maximum) or use MediaNest's website upload (currently configured up to 90 MB).

## History behavior

`/history` shows the latest 25 job snapshots in the current MediaNest process and can search by title, filename, format, status, or job ID. It is intentionally metadata-only: it does not retain a permanent copy of files. Render restarts/redeploys clear this in-memory history and temporary files. When an output is sent as a Telegram attachment, the Telegram message itself remains available in the chat unless it is deleted.
