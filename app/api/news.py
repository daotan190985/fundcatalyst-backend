"""News & catalyst API endpoints."""
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func
from pydantic import BaseModel, ConfigDict

from app.database import get_db, SessionLocal
from app.models import NewsArticle, NewsMention, Catalyst, Stock
from app.scrapers import NewsIngestionService
from app.llm import NewsSummarizer

router = APIRouter(prefix="/news", tags=["news"])


class NewsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    source: str
    summary: Optional[str] = None
    sentiment: Optional[str] = None
    sentiment_score: Optional[float] = None
    category: Optional[str] = None
    importance: Optional[int] = None
    published_at: Optional[datetime] = None
    url: str
    tickers: list[str] = []


class CatalystOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ticker: str
    catalyst_type: Optional[str] = None
    title: str
    description: Optional[str] = None
    impact: Optional[str] = None
    confidence: Optional[float] = None
    detected_at: datetime


@router.get("", response_model=list[NewsOut])
def list_news(
    ticker: Optional[str] = Query(None),
    sentiment: Optional[str] = Query(None, regex="^(positive|negative|neutral)$"),
    category: Optional[str] = Query(None),
    min_importance: int = Query(0, ge=0, le=5),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List news articles with filters."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = select(NewsArticle).where(NewsArticle.published_at >= cutoff)

    if ticker:
        stmt = stmt.join(NewsMention).where(NewsMention.ticker == ticker.upper())
    if sentiment:
        stmt = stmt.where(NewsArticle.sentiment == sentiment)
    if category:
        stmt = stmt.where(NewsArticle.category == category)
    if min_importance > 0:
        stmt = stmt.where(NewsArticle.importance >= min_importance)

    stmt = stmt.order_by(desc(NewsArticle.published_at)).limit(limit)
    articles = db.execute(stmt).scalars().all()

    results = []
    for art in articles:
        tickers = [m.ticker for m in art.mentions]
        results.append(NewsOut(
            id=art.id,
            title=art.title,
            source=art.source,
            summary=art.summary,
            sentiment=art.sentiment,
            sentiment_score=art.sentiment_score,
            category=art.category,
            importance=art.importance,
            published_at=art.published_at,
            url=art.url,
            tickers=tickers,
        ))
    return results


@router.get("/catalysts", response_model=list[CatalystOut])
def list_catalysts(
    ticker: Optional[str] = Query(None),
    impact: Optional[str] = Query(None, regex="^(bullish|bearish|mixed)$"),
    days: int = Query(30, ge=1, le=180),
    min_confidence: float = Query(0.5, ge=0, le=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List recent catalysts. Powers the 'Catalyst chính' section."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = select(Catalyst).where(
        Catalyst.detected_at >= cutoff,
        Catalyst.confidence >= min_confidence,
    )
    if ticker:
        stmt = stmt.where(Catalyst.ticker == ticker.upper())
    if impact:
        stmt = stmt.where(Catalyst.impact == impact)

    stmt = stmt.order_by(desc(Catalyst.detected_at)).limit(limit)
    rows = db.execute(stmt).scalars().all()
    return [CatalystOut.model_validate(r) for r in rows]


@router.post("/ingest/{ticker}", status_code=202)
async def ingest_for_ticker(
    ticker: str,
    max_articles: int = Query(10, ge=1, le=30),
    background: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """Trigger background news crawl + LLM processing for 1 ticker."""
    ticker = ticker.upper()
    if not db.get(Stock, ticker):
        raise HTTPException(404, f"{ticker} chưa được track")

    async def do_ingest():
        bg_db = SessionLocal()
        try:
            scraper_svc = NewsIngestionService(bg_db)
            n_new = await scraper_svc.ingest_for_ticker(ticker, max_articles=max_articles)
            # Summarize
            if n_new > 0:
                summarizer = NewsSummarizer(bg_db)
                stats = await summarizer.process_unprocessed(batch_size=n_new)
                logger.info(f"Ingest {ticker}: +{n_new} articles, LLM processed {stats}")
        finally:
            bg_db.close()

    from loguru import logger
    background.add_task(do_ingest)
    return {"message": f"Ingesting news for {ticker} in background"}


@router.post("/process-pending", status_code=202)
async def process_pending(
    batch_size: int = Query(20, ge=1, le=100),
    background: BackgroundTasks = None,
):
    """Process unprocessed articles through LLM (catch-up)."""
    async def do_process():
        bg_db = SessionLocal()
        try:
            summarizer = NewsSummarizer(bg_db)
            stats = await summarizer.process_unprocessed(batch_size=batch_size)
            logger.info(f"LLM processing: {stats}")
        finally:
            bg_db.close()

    from loguru import logger
    background.add_task(do_process)
    return {"message": "Processing pending articles"}


@router.get("/stats")
def news_stats(db: Session = Depends(get_db)):
    """Stats về news pipeline."""
    total = db.scalar(select(func.count(NewsArticle.id))) or 0
    processed = db.scalar(
        select(func.count(NewsArticle.id)).where(NewsArticle.llm_processed_at.isnot(None))
    ) or 0
    total_catalysts = db.scalar(select(func.count(Catalyst.id))) or 0
    recent_catalysts = db.scalar(
        select(func.count(Catalyst.id)).where(
            Catalyst.detected_at >= datetime.utcnow() - timedelta(days=7)
        )
    ) or 0

    return {
        "total_articles": total,
        "llm_processed": processed,
        "pending_llm": total - processed,
        "total_catalysts": total_catalysts,
        "catalysts_last_7d": recent_catalysts,
    }
