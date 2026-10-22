import argparse
import asyncio
import logging
from pathlib import Path
from typing import Optional

import nodriver
from potoken_generator.extractor import PotokenExtractor
from potoken_generator.server import PotokenServer

logger = logging.getLogger("potoken")


async def run(loop: asyncio.AbstractEventLoop, update_interval: int,
              bind_address: str, port: int,
              browser_path: Optional[Path] = None) -> None:
    # Start the HTTP server immediately so hosting providers can detect port 8080,
    # while the first YouTube token is being generated in the background.
    extractor = PotokenExtractor(loop, update_interval=update_interval,
                                  browser_path=browser_path)
    server = PotokenServer(extractor, port=port, bind_address=bind_address)
    extractor_task = loop.create_task(extractor.run())
    server_task = loop.create_task(asyncio.to_thread(server.run))
    try:
        await asyncio.gather(extractor_task, server_task)
    finally:
        server.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-interval", "-u", type=int, default=300)
    parser.add_argument("--port", "-p", type=int, default=8080)
    parser.add_argument("--bind", "-b", default="127.0.0.1")
    parser.add_argument("--chrome-path", "-c", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y/%m/%d %H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.INFO)
    logging.getLogger("nodriver").setLevel(logging.WARNING)
    loop = nodriver.loop()
    loop.run_until_complete(
        run(loop, args.update_interval, args.bind, args.port, args.chrome_path)
    )


if __name__ == "__main__":
    main()
