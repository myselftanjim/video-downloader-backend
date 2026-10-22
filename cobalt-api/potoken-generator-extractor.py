import asyncio
import dataclasses
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Optional

import nodriver

logger = logging.getLogger("extractor")


@dataclass
class TokenInfo:
    updated: int
    potoken: str
    visitor_data: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))


class PotokenExtractor:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 update_interval: float = 3600,
                 browser_path: Optional[Path] = None) -> None:
        self.update_interval = update_interval
        self.browser_path = browser_path
        self.profile_path = mkdtemp()
        self._loop = loop
        self._token_info: Optional[TokenInfo] = None
        self._ongoing_update: asyncio.Lock = asyncio.Lock()
        self._extraction_done: asyncio.Event = asyncio.Event()
        self._update_requested: asyncio.Event = asyncio.Event()

    def get(self) -> Optional[TokenInfo]:
        return self._token_info

    async def run(self) -> None:
        await self._update()
        while True:
            try:
                await asyncio.wait_for(
                    self._update_requested.wait(), timeout=self.update_interval
                )
            except asyncio.TimeoutError:
                logger.debug("initiating scheduled update")
            else:
                logger.debug("initiating forced update")
            await self._update()
            self._update_requested.clear()

    def request_update(self) -> bool:
        if self._ongoing_update.locked() or self._update_requested.is_set():
            return False
        self._loop.call_soon_threadsafe(self._update_requested.set)
        return True

    @staticmethod
    def _extract_token(request: nodriver.cdp.network.Request) -> Optional[TokenInfo]:
        try:
            body = json.loads(request.post_data)
            visitor_data = body["context"]["client"]["visitorData"]
            potoken = body["serviceIntegrityDimensions"]["poToken"]
        except (json.JSONDecodeError, TypeError, KeyError):
            logger.warning("outgoing player request did not contain a usable token")
            return None
        return TokenInfo(updated=int(time.time()), potoken=potoken,
                         visitor_data=visitor_data)

    async def _update(self) -> None:
        try:
            await asyncio.wait_for(self._perform_update(), timeout=600)
        except asyncio.TimeoutError:
            logger.error("update failed: hard limit timeout exceeded; browser may not be starting")

    async def _perform_update(self) -> None:
        if self._ongoing_update.locked():
            return
        async with self._ongoing_update:
            logger.info("update started")
            self._extraction_done.clear()
            browser = await nodriver.start(
                headless=False,
                browser_executable_path=self.browser_path,
                user_data_dir=self.profile_path,
                sandbox=False,
            )
            try:
                tab = browser.main_tab
                tab.add_handler(nodriver.cdp.network.RequestWillBeSent, self._send_handler)
                await tab.get("https://www.youtube.com/embed/jNQXAC9IVRw")
                clicked = await self._click_on_player(tab)
                if clicked:
                    await self._wait_for_handler()
                await tab.close()
            finally:
                browser.stop()

    @staticmethod
    async def _click_on_player(tab: nodriver.Tab) -> bool:
        # Prefer the actual YouTube play control; clicking the outer player
        # container may not start playback in a virtual display.
        for selector in ("button.ytp-large-play-button", ".ytp-play-button", "#movie_player"):
            try:
                player = await tab.select(selector, 10)
            except asyncio.TimeoutError:
                continue
            try:
                await player.click()
                await asyncio.sleep(2)
                return True
            except Exception as exc:
                logger.debug("player click failed for %s: %s", selector, type(exc).__name__)
        logger.warning("update failed: unable to click a video play control")
        return False

    async def _wait_for_handler(self) -> bool:
        try:
            await asyncio.wait_for(self._extraction_done.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("update failed: timeout waiting for outgoing API request")
            return False
        logger.info("session token generated successfully")
        return True

    async def _send_handler(self, event: nodriver.cdp.network.RequestWillBeSent) -> None:
        if event.request.method != "POST" or "/youtubei/v1/player" not in event.request.url:
            return
        token = self._extract_token(event.request)
        if token is None:
            return
        # Do not print the token itself into hosting logs.
        self._token_info = token
        self._extraction_done.set()
        logger.info("new session token is available")
