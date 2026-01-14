from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Comment
from playwright.async_api import async_playwright, Browser, Playwright

from community_intern.config.models import KnowledgeBaseSettings

logger = logging.getLogger(__name__)


class WebFetcher:
    def __init__(self, config: KnowledgeBaseSettings):
        self.config = config
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None

    async def __aenter__(self) -> WebFetcher:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    async def start(self) -> None:
        """Start the browser engine."""
        if self._browser:
            return

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        except Exception as e:
            logger.error("kb.web_fetcher_start_failed error=%s", e)
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the browser engine."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(self, url: str) -> str:
        """
        Fetch URL content using a headless browser to wait for dynamic content.
        Returns the inner HTML of the <body> tag.
        Content is cached to disk.
        """
        logger.debug("kb.fetch_start url=%s", url)
        cache_dir = Path(self.config.web_fetch_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Use a simple hash of the URL as the cache filename
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        cache_file = cache_dir / url_hash

        if cache_file.exists():
            logger.debug("kb.fetch_cache_hit url=%s", url)
            return cache_file.read_text(encoding="utf-8")

        # If browser is not running (e.g. used without context manager), start it temporarily?
        # Better to assume usage within context manager or start/stop.
        # But for robustness, we can auto-start if needed, though inefficient for single calls if not managed.
        should_close = False
        if not self._browser:
            await self.start()
            should_close = True

        try:
            assert self._browser is not None
            page = await self._browser.new_page()
            try:
                # Wait for network idle to ensure dynamic content is loaded
                # Convert seconds to ms
                timeout_ms = self.config.web_fetch_timeout_seconds * 1000
                logger.debug("kb.fetch_navigating url=%s", url)
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)

                # Get body content
                content = await page.inner_html("body")
                logger.debug("kb.fetch_cleaning url=%s raw_len=%d", url, len(content))

                # Clean content
                content = self._clean_content(content)

                # Enforce size limit
                if len(content.encode("utf-8")) > self.config.max_source_bytes:
                    logger.warning("kb.fetch_too_large url=%s size=%d", url, len(content))
                    return ""

                # Cache it
                cache_file.write_text(content, encoding="utf-8")
                logger.debug("kb.fetch_success url=%s size=%d", url, len(content))
                return content
            except Exception as e:
                logger.warning("kb.fetch_exception url=%s error=%s", url, e)
                return ""
            finally:
                await page.close()
        finally:
            if should_close:
                await self.stop()

    def _clean_content(self, html_content: str) -> str:
        """
        Clean HTML content by removing unwanted tags, comments, attributes, and whitespace.
        Returns a compact string representation of the cleaned HTML.
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # Remove unwanted tags
        unwanted_tags = [
            "script", "style", "noscript", "iframe", "svg", "meta", "link",
            "img", "button", "input", "video", "audio", "canvas", "map", "object",
            "select", "textarea", "nav", "footer", "aside"
        ]
        for element in soup(unwanted_tags):
            element.decompose()

        # Remove comments
        for comment in soup.find_all(text=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Remove all attributes from tags
        for tag in soup.find_all(True):
            tag.attrs = {}

        # 1. Strip all text nodes first to expose empty elements that only contain whitespace
        for text in soup.find_all(text=True):
            if text.parent.name not in ['pre', 'code']:
                text.replace_with(text.strip())

        # Recursively remove empty tags (except those that can be empty like br, hr)
        # We do this in a loop until no more changes are made to handle nested empty tags
        while True:
            empty_tags = [
                tag for tag in soup.find_all(True)
                if tag.name not in {'br', 'hr'} and not tag.find(True) and not tag.get_text(strip=True)
            ]
            if not empty_tags:
                break
            for tag in empty_tags:
                tag.decompose()

        # 2. Re-serialize and collapse whitespace in the string representation
        content = str(soup)

        # Replace sequence of whitespace (including \n) with single space
        content = re.sub(r'\s+', ' ', content).strip()

        return content
