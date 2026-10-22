import argparse
import asyncio
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import nodriver
from potoken_generator.extractor import PotokenExtractor

logger = logging.getLogger("potoken")


def make_handler(extractor):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status, value):
            payload = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path.rstrip("/") in ("", "/health"):
                token = extractor.get()
                self._send_json(200, {
                    "status": "ready" if token else "starting",
                    "token_available": bool(token),
                })
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self):
            # Cobalt posts here when it requests YouTube poToken data.
            if self.path.rstrip("/") != "/get_pot":
                self._send_json(404, {"error": "not_found"})
                return
            token = extractor.get()
            if token is None:
                self._send_json(503, {"error": "token_not_ready"})
                return
            self._send_json(200, {
                "poToken": token.potoken,
                "visitorData": token.visitor_data,
                "po_token": token.potoken,
                "visitor_data": token.visitor_data,
            })

        def log_message(self, format, *args):
            # No request body or token values are logged.
            logger.info("http %s %s", self.command, self.path.split("?")[0])

    return Handler


async def run(loop: asyncio.AbstractEventLoop, update_interval: int,
              bind_address: str, port: int,
              browser_path: Optional[Path] = None) -> None:
    extractor = PotokenExtractor(loop, update_interval=update_interval,
                                  browser_path=browser_path)

    # Serve the Cobalt-compatible /get_pot endpoint immediately. Token generation
    # runs concurrently; requests receive a clear 503 until a real token exists.
    httpd = ThreadingHTTPServer((bind_address, port), make_handler(extractor))
    server_task = loop.create_task(asyncio.to_thread(httpd.serve_forever))
    extractor_task = loop.create_task(extractor.run())
    logger.info("session adapter listening on %s:%s", bind_address, port)
    try:
        await asyncio.gather(extractor_task, server_task)
    finally:
        httpd.shutdown()
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-interval", "-u", type=int, default=300)
    parser.add_argument("--port", "-p", type=int, default=8080)
    parser.add_argument("--bind", "-b", default="0.0.0.0")
    parser.add_argument("--chrome-path", "-c", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("nodriver").setLevel(logging.WARNING)
    loop = nodriver.loop()
    loop.run_until_complete(
        run(loop, args.update_interval, args.bind, args.port, args.chrome_path)
    )


if __name__ == "__main__":
    main()
