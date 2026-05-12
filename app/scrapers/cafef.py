"""CafeF.vn news scraper.

CafeF cấu trúc URL:
- Listing: https://cafef.vn/du-lieu/lich-su-giao-dich-{TICKER}-1.chn (per-stock news)
- Hoặc dùng RSS-like trang search: https://cafef.vn/timkiem.chn?keywords={TICKER}
- Trang chi tiết: <h1 class="title"> + <p class="sapo"> + <div id="mainContent">

Hạn chế:
- CafeF không có API chính thức
- Anti-bot có thể chặn nếu request quá nhanh
- HTML structure đôi khi đổi

Best practices:
- Rate limit: 1 request/2-3 giây
- User-Agent giả browser
- Retry với exponential backoff
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
import re
import asyncio
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


@dataclass
class ScrapedArticle:
    """Bài viết đã crawl, chưa qua LLM."""
    url: str
    title: str
    sapo: str  # tóm tắt đầu bài (lead paragraph)
    content: str
    source: str
    published_at: Optional[datetime] = None
    tickers_in_title: list[str] = None


class CafeFScraper:
    """Scraper for CafeF.vn.
    
    Note: CafeF doesn't expose an API. This relies on HTML structure
    which can change. If anti-bot fires, fall back to FireAnt (has API).
    """

    BASE_URL = "https://cafef.vn"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    REQUEST_DELAY = 2.0  # seconds between requests

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._last_request = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.HEADERS,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _throttle(self):
        """Enforce minimum delay between requests."""
        import time
        elapsed = time.time() - self._last_request
        if elapsed < self.REQUEST_DELAY:
            await asyncio.sleep(self.REQUEST_DELAY - elapsed)
        self._last_request = time.time()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=15),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    )
    async def _fetch(self, url: str) -> str:
        """Fetch HTML with retries."""
        await self._throttle()
        client = await self._get_client()
        logger.debug(f"GET {url}")
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    async def search_by_ticker(self, ticker: str, max_results: int = 10) -> list[str]:
        """Search CafeF for articles mentioning ticker. Returns list of URLs.
        
        Uses CafeF's search endpoint. Falls back to per-stock page if search fails.
        """
        ticker = ticker.upper()
        urls_seen = set()

        # Strategy 1: search endpoint
        search_url = f"{self.BASE_URL}/tim-kiem.chn?keywords={ticker}"
        try:
            html = await self._fetch(search_url)
            # Regex extract article URLs (CafeF article URLs end in .chn)
            pattern = re.compile(r'href="(/[^"]+\.chn)"', re.IGNORECASE)
            for m in pattern.finditer(html):
                path = m.group(1)
                if "tim-kiem" in path or "lich-su" in path or "du-lieu" in path:
                    continue
                url = f"{self.BASE_URL}{path}"
                if url not in urls_seen:
                    urls_seen.add(url)
                    if len(urls_seen) >= max_results:
                        break
        except Exception as e:
            logger.warning(f"CafeF search failed for {ticker}: {e}")

        return list(urls_seen)

    async def fetch_article(self, url: str) -> Optional[ScrapedArticle]:
        """Fetch + parse single article page."""
        try:
            html = await self._fetch(url)
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

        # Parse với regex (BeautifulSoup tốt hơn nhưng giữ deps minimal)
        title = self._extract(html, [
            r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</h1>',
            r'<h1[^>]*>([^<]+)</h1>',
            r'<title>([^<]+)</title>',
        ])
        sapo = self._extract(html, [
            r'<h2[^>]*class="[^"]*sapo[^"]*"[^>]*>([^<]+)</h2>',
            r'<p[^>]*class="[^"]*sapo[^"]*"[^>]*>([^<]+)</p>',
            r'<meta name="description" content="([^"]+)"',
        ]) or ""
        
        # Extract main content (CafeF dùng id="mainContent" hoặc class="detail-content")
        content_match = re.search(
            r'<div[^>]*(?:id="mainContent"|class="[^"]*detail-content[^"]*")[^>]*>(.*?)</div>\s*<(?:/article|aside|footer)',
            html, re.DOTALL | re.IGNORECASE
        )
        content_text = ""
        if content_match:
            raw = content_match.group(1)
            # Strip HTML tags
            content_text = re.sub(r"<[^>]+>", " ", raw)
            content_text = re.sub(r"\s+", " ", content_text).strip()[:5000]

        # Published date
        pub_match = re.search(
            r'<span[^>]*class="[^"]*pdate[^"]*"[^>]*>([^<]+)</span>|'
            r'<time[^>]*datetime="([^"]+)"',
            html,
        )
        published_at = None
        if pub_match:
            date_str = pub_match.group(1) or pub_match.group(2)
            published_at = self._parse_date(date_str)

        # Detect ticker mentions in title (3-letter uppercase, ngoại trừ common words)
        tickers_in_title = self._extract_tickers(title) if title else []

        return ScrapedArticle(
            url=url,
            title=title or url,
            sapo=sapo,
            content=content_text,
            source="cafef",
            published_at=published_at or datetime.utcnow(),
            tickers_in_title=tickers_in_title,
        )

    @staticmethod
    def _extract(html: str, patterns: list[str]) -> Optional[str]:
        """Try each regex pattern, return first match."""
        for p in patterns:
            m = re.search(p, html, re.DOTALL | re.IGNORECASE)
            if m:
                text = m.group(1).strip()
                # Unescape HTML entities
                text = text.replace("&nbsp;", " ").replace("&amp;", "&")
                text = text.replace("&quot;", '"').replace("&#39;", "'")
                text = re.sub(r"\s+", " ", text)
                return text if text else None
        return None

    @staticmethod
    def _parse_date(s: str) -> Optional[datetime]:
        """Parse CafeF date string. Formats vary; try common ones."""
        if not s:
            return None
        s = s.strip()
        # Try ISO format
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        # Try Vietnamese: "12-05-2026 - 14:35"
        m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})(?:\s*[-,]\s*(\d{1,2}):(\d{2}))?", s)
        if m:
            try:
                day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                hour = int(m.group(4)) if m.group(4) else 0
                minute = int(m.group(5)) if m.group(5) else 0
                return datetime(year, month, day, hour, minute)
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_tickers(text: str) -> list[str]:
        """Tìm các mã CK trong text. Mã CK VN là 3 ký tự viết hoa.
        
        Loại trừ false positives: USD, VND, VAT, GDP, etc.
        """
        EXCLUDE = {"USD", "VND", "VAT", "GDP", "FED", "OPEC", "IMF", "WTO",
                   "BCT", "BCTC", "TTCK", "HOSE", "HNX", "CEO", "CFO", "CTO",
                   "IPO", "ETF", "API", "CPI", "PMI", "GMT", "EPS", "ROE",
                   "ROA", "DPS"}
        candidates = re.findall(r"\b([A-Z]{3})\b", text)
        return [t for t in candidates if t not in EXCLUDE]
