"""Background job scheduler.

Job schedule:
- quote_refresh: every 5 min during trading hours (giá realtime)
- alert_evaluate: every 10 min (chạy ngay sau quote refresh)
- news_ingest: every 30 min (crawl tin mới)
- llm_process: every 15 min (xử lý LLM cho tin chưa process)
- full_refresh: 15:45 daily (BCTC + company info)
- rescore_all: 16:00 daily (chấm điểm lại toàn bộ)
"""
from datetime import datetime
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.database import SessionLocal
from app.services import IngestionService
from app.scrapers import NewsIngestionService
from app.llm import NewsSummarizer
from app.alerts import AlertEngine
from app.scoring import ScoringEngine
from app.config import settings


def is_trading_hours() -> bool:
    """Vietnam market hours: 9:00-11:30 + 13:00-15:00, Mon-Fri."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    morning = (9, 0) <= (now.hour, now.minute) <= (11, 30)
    afternoon = (13, 0) <= (now.hour, now.minute) <= (15, 0)
    return morning or afternoon


def job_quote_refresh():
    if not is_trading_hours():
        logger.debug("Skipping quote refresh — outside trading hours")
        return
    logger.info("[JOB] quote_refresh")
    db = SessionLocal()
    try:
        svc = IngestionService(db)
        job = svc.run_quote_refresh(settings.default_tickers)
        logger.info(f"[JOB] quote_refresh done: {job.status}, processed={job.records_processed}")
    except Exception as e:
        logger.exception(f"quote_refresh failed: {e}")
    finally:
        db.close()


def job_full_refresh():
    logger.info("[JOB] full_refresh")
    db = SessionLocal()
    try:
        svc = IngestionService(db)
        job = svc.run_full_refresh(settings.default_tickers)
        logger.info(f"[JOB] full_refresh done: {job.status}, processed={job.records_processed}")
    except Exception as e:
        logger.exception(f"full_refresh failed: {e}")
    finally:
        db.close()


def job_evaluate_alerts():
    logger.info("[JOB] evaluate_alerts")
    db = SessionLocal()
    try:
        engine = AlertEngine(db)
        stats = engine.evaluate_all(settings.default_tickers)
        logger.info(f"[JOB] evaluate_alerts done: {stats}")
    except Exception as e:
        logger.exception(f"evaluate_alerts failed: {e}")
    finally:
        db.close()


def job_news_ingest():
    if not is_trading_hours():
        now = datetime.now()
        if now.hour < 8 or now.hour > 20:
            return
    logger.info("[JOB] news_ingest")

    async def run():
        db = SessionLocal()
        try:
            svc = NewsIngestionService(db)
            stats = await svc.ingest_for_all(
                settings.default_tickers[:15],
                max_per_ticker=5,
            )
            logger.info(f"[JOB] news_ingest done: {stats}")
        finally:
            db.close()

    try:
        asyncio.run(run())
    except Exception as e:
        logger.exception(f"news_ingest failed: {e}")


def job_llm_process():
    logger.info("[JOB] llm_process")

    async def run():
        db = SessionLocal()
        try:
            summarizer = NewsSummarizer(db)
            stats = await summarizer.process_unprocessed(batch_size=30)
            logger.info(f"[JOB] llm_process done: {stats}")
        finally:
            db.close()

    try:
        asyncio.run(run())
    except Exception as e:
        logger.exception(f"llm_process failed: {e}")


def job_rescore_all():
    logger.info("[JOB] rescore_all")
    db = SessionLocal()
    try:
        engine = ScoringEngine(db)
        results = engine.score_all(settings.default_tickers)
        logger.info(f"[JOB] rescore_all done: {len(results)} scored")
    except Exception as e:
        logger.exception(f"rescore_all failed: {e}")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")

    scheduler.add_job(job_quote_refresh, "interval",
                      minutes=settings.quote_refresh_interval_min,
                      id="quote_refresh", max_instances=1, coalesce=True)

    scheduler.add_job(job_evaluate_alerts, "interval", minutes=10,
                      id="evaluate_alerts", max_instances=1, coalesce=True)

    scheduler.add_job(job_news_ingest, "interval", minutes=30,
                      id="news_ingest", max_instances=1, coalesce=True)

    scheduler.add_job(job_llm_process, "interval", minutes=15,
                      id="llm_process", max_instances=1, coalesce=True)

    scheduler.add_job(job_full_refresh,
                      CronTrigger(hour=15, minute=45, day_of_week="mon-fri"),
                      id="full_refresh", max_instances=1, coalesce=True)

    scheduler.add_job(job_rescore_all,
                      CronTrigger(hour=16, minute=0, day_of_week="mon-fri"),
                      id="rescore_all", max_instances=1, coalesce=True)

    scheduler.start()
    logger.info("Scheduler started with 6 jobs")
    return scheduler
