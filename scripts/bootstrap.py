"""Initial data load. Run once after first deploy.

Pipeline:
1. Create schema (if not exist)
2. Seed alert rules
3. Load stocks: company info + 1y quotes + 8 quarters BCTC
4. Compute latest metrics
5. Run scoring engine
6. Crawl news (optional, slower)

Usage:
    python scripts/bootstrap.py                          # all defaults
    python scripts/bootstrap.py --tickers FPT,HPG        # specific tickers
    python scripts/bootstrap.py --skip-news              # faster, no news crawl
    python scripts/bootstrap.py --news-only              # only crawl news (assume data exists)
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from app.database import SessionLocal, engine
from app.models.db_models import Base
from app.services import IngestionService
from app.scoring import ScoringEngine
from app.scrapers import NewsIngestionService
from app.llm import NewsSummarizer
from app.alerts import AlertEngine
from app.config import settings


def step(n, total, msg):
    """Pretty step printer."""
    logger.info(f"━━━ [{n}/{total}] {msg} ━━━")


def main():
    parser = argparse.ArgumentParser(description="Bootstrap FundCatalyst DB with real data")
    parser.add_argument("--tickers", help="CSV list of tickers (default: VN30)")
    parser.add_argument("--quotes-days", type=int, default=365)
    parser.add_argument("--quarters", type=int, default=8)
    parser.add_argument("--skip-news", action="store_true", help="Skip news crawl + LLM")
    parser.add_argument("--news-only", action="store_true", help="Only crawl news")
    parser.add_argument("--max-news-per-ticker", type=int, default=5)
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in (args.tickers or ",".join(settings.default_tickers)).split(",")]

    start = time.time()
    total_steps = 6 if not args.skip_news else 4

    # ---- Step 1: Schema ----
    step(1, total_steps, "Creating database schema")
    Base.metadata.create_all(bind=engine)

    # ---- Step 2: Seed alert rules ----
    step(2, total_steps, "Seeding default alert rules")
    db = SessionLocal()
    try:
        AlertEngine(db).seed_default_rules()
    finally:
        db.close()

    if args.news_only:
        run_news_pipeline(tickers, args.max_news_per_ticker)
        logger.info(f"Done in {time.time() - start:.1f}s")
        return

    # ---- Step 3: Load stocks ----
    step(3, total_steps, f"Loading stock data for {len(tickers)} tickers (this takes ~3-5 minutes)")
    db = SessionLocal()
    failures = []
    try:
        svc = IngestionService(db)
        for i, ticker in enumerate(tickers, 1):
            try:
                logger.info(f"  [{i}/{len(tickers)}] {ticker}")
                stock = svc.upsert_stock(ticker, force_refresh=True)
                if not stock:
                    failures.append(ticker)
                    continue
                n_quotes = svc.ingest_quotes(ticker, days=args.quotes_days)
                n_fin = svc.ingest_financials(ticker, n_quarters=args.quarters)
                metric = svc.update_latest_metric(ticker)
                price = metric.price if metric else None
                score = metric.score if metric else None
                logger.info(f"    ✓ {n_quotes} quotes, {n_fin} quarters, "
                          f"price={price:.0f if price else 'NA'}, score={score:.1f if score else 'NA'}")
            except Exception as e:
                logger.exception(f"    ✗ {ticker}: {e}")
                failures.append(ticker)
    finally:
        db.close()

    if failures:
        logger.warning(f"Failed tickers: {', '.join(failures)}")

    # ---- Step 4: Re-score everything (now that we have full data) ----
    step(4, total_steps, "Running scoring engine on all tickers")
    db = SessionLocal()
    try:
        engine_ = ScoringEngine(db)
        results = engine_.score_all([t for t in tickers if t not in failures])
        logger.info(f"  Scored {len(results)} tickers")
        # Show top 5 by score
        top = sorted(results.items(), key=lambda x: x[1], reverse=True)[:5]
        logger.info("  Top 5 by score:")
        for t, s in top:
            logger.info(f"    {t}: {s:.1f}")
    finally:
        db.close()

    # ---- Step 5+6: News pipeline ----
    if not args.skip_news:
        run_news_pipeline(tickers[:15], args.max_news_per_ticker)  # limit to 15 to avoid rate limit

    logger.info(f"━━━ Bootstrap complete in {time.time() - start:.1f}s ━━━")
    logger.info(f"Open http://localhost:8000/docs to explore the API")


def run_news_pipeline(tickers, max_per_ticker):
    """Crawl news + LLM processing."""
    step(5, 6, f"Crawling news for {len(tickers)} tickers")

    async def run_crawl():
        db = SessionLocal()
        try:
            svc = NewsIngestionService(db)
            stats = await svc.ingest_for_all(tickers, max_per_ticker=max_per_ticker)
            logger.info(f"  News crawl: {stats}")
        finally:
            db.close()

    asyncio.run(run_crawl())

    step(6, 6, "Processing news through LLM (summarize + catalyst detection)")

    async def run_llm():
        db = SessionLocal()
        try:
            summarizer = NewsSummarizer(db)
            # Loop until no more pending
            iterations = 0
            while iterations < 10:
                stats = await summarizer.process_unprocessed(batch_size=20)
                logger.info(f"  LLM batch {iterations + 1}: {stats}")
                if stats["processed"] == 0:
                    break
                iterations += 1
        finally:
            db.close()

    asyncio.run(run_llm())


if __name__ == "__main__":
    main()
