"""REST API endpoints for stocks."""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

from app.database import get_db
from app.models import Stock, Quote, FinancialQuarter, LatestMetric
from app.models.schemas import StockSummary, StockDetail, QuoteOut, FinancialOut, ErrorOut
from app.services import IngestionService
import json

router = APIRouter(prefix="/stocks", tags=["stocks"])


@router.get("", response_model=list[StockSummary])
def list_stocks(
    sector: Optional[str] = Query(None, description="Lọc theo ngành"),
    min_score: Optional[float] = Query(None, ge=0, le=100),
    sort: str = Query("score", regex="^(score|change|volume|market_cap)$"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """List stocks with their current snapshot. Powers the dashboard."""
    stmt = (
        select(Stock, LatestMetric)
        .join(LatestMetric, Stock.ticker == LatestMetric.ticker, isouter=True)
    )

    if sector:
        stmt = stmt.where(Stock.sector == sector)

    if min_score is not None:
        stmt = stmt.where(LatestMetric.score >= min_score)

    # Sorting
    sort_col = {
        "score": LatestMetric.score,
        "change": LatestMetric.change_pct,
        "volume": LatestMetric.volume,
        "market_cap": LatestMetric.market_cap,
    }[sort]
    stmt = stmt.order_by(desc(sort_col).nullslast()).limit(limit)

    rows = db.execute(stmt).all()
    results = []
    for stock, metric in rows:
        if not metric:
            continue
        summary = StockSummary(
            ticker=stock.ticker,
            name=stock.name,
            sector=stock.sector,
            price=metric.price,
            change_pct=metric.change_pct,
            volume=metric.volume,
            volume_spike=metric.volume_spike,
            pe=metric.pe,
            pb=metric.pb,
            roe_ttm=metric.roe_ttm,
            eps_ttm=metric.eps_ttm,
            market_cap=metric.market_cap,
            foreign_net_5d=metric.foreign_net_5d,
            score=metric.score,
            score_components=json.loads(metric.score_components) if metric.score_components else None,
        )
        results.append(summary)
    return results


@router.get("/{ticker}", response_model=StockDetail, responses={404: {"model": ErrorOut}})
def get_stock_detail(
    ticker: str,
    quotes_days: int = Query(90, ge=7, le=365, description="Số ngày lịch sử giá"),
    db: Session = Depends(get_db),
):
    """Full detail for one stock: company info + recent quotes + quarterly financials + score."""
    ticker = ticker.upper().strip()
    stock = db.get(Stock, ticker)
    if not stock:
        raise HTTPException(404, f"Ticker {ticker} chưa được track. Gọi POST /stocks/{ticker}/refresh để thêm.")

    metric = db.get(LatestMetric, ticker)
    cutoff = date.today() - timedelta(days=quotes_days)
    quotes = db.execute(
        select(Quote).where(Quote.ticker == ticker, Quote.date >= cutoff)
        .order_by(Quote.date)
    ).scalars().all()

    financials = db.execute(
        select(FinancialQuarter).where(FinancialQuarter.ticker == ticker)
        .order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
        .limit(12)
    ).scalars().all()
    financials.reverse()  # oldest first for chart display

    return StockDetail(
        ticker=stock.ticker,
        name=stock.name,
        sector=stock.sector,
        industry=stock.industry,
        listed_shares=stock.listed_shares,
        description=stock.description,
        price=metric.price if metric else None,
        change_pct=metric.change_pct if metric else None,
        volume=metric.volume if metric else None,
        volume_spike=metric.volume_spike if metric else None,
        pe=metric.pe if metric else None,
        pb=metric.pb if metric else None,
        roe_ttm=metric.roe_ttm if metric else None,
        eps_ttm=metric.eps_ttm if metric else None,
        market_cap=metric.market_cap if metric else None,
        foreign_net_5d=metric.foreign_net_5d if metric else None,
        score=metric.score if metric else None,
        score_components=json.loads(metric.score_components) if metric and metric.score_components else None,
        quotes=[QuoteOut.model_validate(q) for q in quotes],
        financials=[FinancialOut.model_validate(f) for f in financials],
    )


@router.post("/{ticker}/refresh", status_code=202)
def refresh_stock(
    ticker: str,
    background: BackgroundTasks,
    full: bool = Query(False, description="Nếu true thì fetch cả BCTC, không chỉ giá"),
    db: Session = Depends(get_db),
):
    """Trigger background refresh from vnstock. Returns immediately."""
    ticker = ticker.upper().strip()
    ingestion = IngestionService(db)

    def do_refresh():
        # Need a fresh session for background task
        from app.database import SessionLocal
        bg_db = SessionLocal()
        try:
            svc = IngestionService(bg_db)
            svc.upsert_stock(ticker, force_refresh=True)
            if full:
                svc.ingest_quotes(ticker, days=365)
                svc.ingest_financials(ticker, n_quarters=12)
            else:
                svc.ingest_quotes(ticker, days=30)
            svc.update_latest_metric(ticker)
        finally:
            bg_db.close()

    background.add_task(do_refresh)
    return {"message": f"Đang refresh {ticker}", "full": full}


@router.get("/{ticker}/quotes", response_model=list[QuoteOut])
def get_quotes(
    ticker: str,
    days: int = Query(90, ge=1, le=1825),
    db: Session = Depends(get_db),
):
    """OHLCV history for chart."""
    ticker = ticker.upper().strip()
    cutoff = date.today() - timedelta(days=days)
    quotes = db.execute(
        select(Quote).where(Quote.ticker == ticker, Quote.date >= cutoff)
        .order_by(Quote.date)
    ).scalars().all()
    return [QuoteOut.model_validate(q) for q in quotes]


@router.get("/{ticker}/financials", response_model=list[FinancialOut])
def get_financials(
    ticker: str,
    n_quarters: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Quarterly financials, oldest first (for chart)."""
    ticker = ticker.upper().strip()
    rows = db.execute(
        select(FinancialQuarter).where(FinancialQuarter.ticker == ticker)
        .order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
        .limit(n_quarters)
    ).scalars().all()
    rows.reverse()
    return [FinancialOut.model_validate(r) for r in rows]
