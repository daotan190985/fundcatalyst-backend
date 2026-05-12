"""Sector aggregations and admin endpoints."""
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import select, func, desc
from datetime import datetime, timedelta

from app.database import get_db, SessionLocal
from app.models import Stock, LatestMetric, JobRun
from app.models.schemas import HealthOut, JobRunOut
from app.services import IngestionService
from app.config import settings

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)):
    """Service health & data freshness check."""
    try:
        db.execute(select(1))
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    last_job = db.execute(
        select(JobRun).where(JobRun.job_name == "quote_refresh")
        .order_by(desc(JobRun.finished_at)).limit(1)
    ).scalar_one_or_none()

    tickers_count = db.scalar(select(func.count(Stock.ticker))) or 0

    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        version=settings.app_version,
        database=db_status,
        last_quote_refresh=last_job.finished_at if last_job else None,
        tickers_tracked=tickers_count,
    )


@router.get("/sectors")
def list_sectors(db: Session = Depends(get_db)):
    """Sector aggregates for heatmap: avg change, volume, top hot score."""
    rows = db.execute(
        select(
            Stock.sector,
            func.avg(LatestMetric.change_pct).label("avg_change"),
            func.sum(LatestMetric.volume).label("total_volume"),
            func.max(LatestMetric.score).label("max_score"),
            func.count(Stock.ticker).label("count"),
        )
        .join(LatestMetric, Stock.ticker == LatestMetric.ticker)
        .where(Stock.sector.isnot(None))
        .group_by(Stock.sector)
    ).all()

    sectors = []
    for r in rows:
        sectors.append({
            "name": r.sector,
            "change": round(float(r.avg_change or 0), 2),
            "volume": int((r.total_volume or 0) / 1000),  # thousand units
            "hot": round(float(r.max_score or 0)),
            "count": r.count,
        })
    sectors.sort(key=lambda s: s["hot"], reverse=True)
    return sectors


@router.get("/jobs", response_model=list[JobRunOut])
def recent_jobs(limit: int = 20, db: Session = Depends(get_db)):
    """Recent background job runs for monitoring."""
    rows = db.execute(
        select(JobRun).order_by(desc(JobRun.started_at)).limit(limit)
    ).scalars().all()
    return [JobRunOut.model_validate(r) for r in rows]


@router.post("/admin/refresh-all", status_code=202)
def refresh_all(background: BackgroundTasks):
    """Trigger refresh of all tracked tickers. Async."""
    def do_refresh():
        db = SessionLocal()
        try:
            svc = IngestionService(db)
            svc.run_full_refresh(settings.default_tickers)
        finally:
            db.close()

    background.add_task(do_refresh)
    return {"message": f"Refreshing {len(settings.default_tickers)} tickers in background"}


@router.post("/admin/refresh-quotes", status_code=202)
def refresh_quotes_only(background: BackgroundTasks):
    """Lighter refresh: just quotes + metrics."""
    def do_refresh():
        db = SessionLocal()
        try:
            svc = IngestionService(db)
            svc.run_quote_refresh(settings.default_tickers)
        finally:
            db.close()

    background.add_task(do_refresh)
    return {"message": "Quote refresh started"}
