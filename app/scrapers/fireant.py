"""FireAnt.vn news source.

FireAnt có hidden JSON endpoint (không phải public API chính thức nhưng stable hơn HTML scraping):
- /api/Data/Markets/News?symbol=FPT&limit=20

Khi CafeF bị anti-bot, fallback sang FireAnt.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from app.scrapers.cafef import ScrapedArticle


class FireAntScraper:
    BASE_URL = "https://fireant.vn"
    NEWS_API = "/api/Data/Markets/News"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
        "Referer": "https://fireant.vn/",
    }

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.HEADERS,
                timeout=self.timeout,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=10))
    async def fetch_news_for_ticker(self, ticker: str, limit: int = 20) -> list[ScrapedArticle]:
        """Fetch recent news for a ticker. Returns articles with full content."""
        client = await self._get_client()
        url = f"{self.BASE_URL}{self.NEWS_API}"
        params = {"symbol": ticker.upper(), "offset": 0, "limit": limit}

        try:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning(f"FireAnt returned {resp.status_code} for {ticker}")
                return []
            data = resp.json()
        except Exception as e:
            logger.warning(f"FireAnt fetch failed for {ticker}: {e}")
            return []

        if not isinstance(data, list):
            return []

        articles = []
        for item in data:
            try:
                pub = None
                date_str = item.get("date") or item.get("postDate")
                if date_str:
                    try:
                        pub = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    except ValueError:
                        pub = None

                articles.append(ScrapedArticle(
                    url=item.get("originalURL") or item.get("link") or f"fireant://{item.get('postID', '')}",
                    title=item.get("title", "").strip(),
                    sapo=item.get("description", "").strip(),
                    content=item.get("content", "").strip()[:5000],
                    source="fireant",
                    published_at=pub or datetime.utcnow(),
                    tickers_in_title=[ticker.upper()],
                ))
            except Exception as e:
                logger.debug(f"Skipping FireAnt item: {e}")
                continue

        return articles
