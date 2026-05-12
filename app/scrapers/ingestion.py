"""Orchestrate news scraping: query multiple sources, dedupe, store."""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional
import asyncio
from sqlalchemy.orm import Session
from sqlalchemy import select
from loguru import logger

from app.models import NewsArticle, NewsMention, Stock
from app.scrapers.cafef import CafeFScraper, ScrapedArticle
from app.scrapers.fireant import FireAntScraper


class NewsIngestionService:
    """Crawl tin tức và lưu DB. Sau đó LLM sẽ summarize asynchronously."""

    def __init__(self, db: Session):
        self.db = db

    async def ingest_for_ticker(self, ticker: str, max_articles: int = 10) -> int:
        """Crawl tin cho 1 mã từ tất cả nguồn. Trả về số bài mới insert.
        
        Strategy: FireAnt trước (stable), fallback CafeF nếu fail.
        """
        ticker = ticker.upper()
        articles: list[ScrapedArticle] = []

        # Source 1: FireAnt (stable)
        fa = FireAntScraper()
        try:
            fa_articles = await fa.fetch_news_for_ticker(ticker, limit=max_articles)
            articles.extend(fa_articles)
            logger.debug(f"FireAnt: {len(fa_articles)} articles for {ticker}")
        except Exception as e:
            logger.warning(f"FireAnt failed for {ticker}: {e}")
        finally:
            await fa.close()

        # Source 2: CafeF (fallback)
        if len(articles) < 3:
            cafef = CafeFScraper()
            try:
                urls = await cafef.search_by_ticker(ticker, max_results=max_articles)
                for url in urls[:5]:  # limit để tránh rate limit
                    art = await cafef.fetch_article(url)
                    if art:
                        articles.append(art)
            except Exception as e:
                logger.warning(f"CafeF failed for {ticker}: {e}")
            finally:
                await cafef.close()

        return self._persist_articles(articles, primary_ticker=ticker)

    def _persist_articles(self, articles: list[ScrapedArticle], primary_ticker: str) -> int:
        """Save articles to DB with dedup on URL."""
        new_count = 0
        for art in articles:
            if not art.url or not art.title:
                continue

            # Dedup
            existing = self.db.execute(
                select(NewsArticle).where(NewsArticle.url == art.url)
            ).scalar_one_or_none()
            if existing:
                # Optionally update content if richer
                if art.content and len(art.content) > len(existing.content_text or ""):
                    existing.content_text = art.content
                    self.db.commit()
                continue

            db_article = NewsArticle(
                url=art.url[:500],
                title=art.title[:500],
                source=art.source,
                published_at=art.published_at,
                content_text=art.content,
            )
            self.db.add(db_article)
            self.db.flush()  # get ID

            # Link to tickers found in title (or primary if none)
            tickers_to_link = set(art.tickers_in_title or [])
            tickers_to_link.add(primary_ticker)

            for t in tickers_to_link:
                # Check ticker exists in DB
                if not self.db.get(Stock, t):
                    continue
                relevance = 1.0 if t == primary_ticker else 0.7
                self.db.add(NewsMention(
                    article_id=db_article.id,
                    ticker=t,
                    relevance=relevance,
                ))

            new_count += 1

        self.db.commit()
        return new_count

    async def ingest_for_all(self, tickers: list[str], max_per_ticker: int = 5) -> dict:
        """Bulk ingest. Returns summary stats."""
        stats = {"tickers_processed": 0, "new_articles": 0, "errors": []}
        for t in tickers:
            try:
                n = await self.ingest_for_ticker(t, max_articles=max_per_ticker)
                stats["new_articles"] += n
                stats["tickers_processed"] += 1
                logger.info(f"News for {t}: +{n} new articles")
            except Exception as e:
                stats["errors"].append(f"{t}: {e}")
                logger.exception(f"News ingestion failed for {t}: {e}")
        return stats
