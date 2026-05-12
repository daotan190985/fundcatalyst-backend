"""Scoring engine API endpoints."""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db, SessionLocal
from app.scoring import ScoringEngine, Backtester, WEIGHTS
from app.models import Stock
from app.config import settings

router = APIRouter(prefix="/scoring", tags=["scoring"])


class ScoreBreakdownOut(BaseModel):
    ticker: str
    score: float
    quality: str
    confidence: float
    factors: list[dict]


class BacktestRequest(BaseModel):
    start_date: date
    end_date: Optional[date] = None
    hold_days: int = 60
    rebalance_days: int = 30
    tickers: Optional[list[str]] = None


@router.get("/weights")
def get_weights():
    """Get current scoring weights."""
    return {"weights": WEIGHTS, "sum": round(sum(WEIGHTS.values()), 3)}


@router.get("/breakdown/{ticker}", response_model=ScoreBreakdownOut)
def get_score_breakdown(ticker: str, db: Session = Depends(get_db)):
    """Get detailed score breakdown for a ticker."""
    ticker = ticker.upper()
    if not db.get(Stock, ticker):
        raise HTTPException(404, f"{ticker} not tracked")

    engine = ScoringEngine(db)
    result = engine.score_ticker(ticker)
    return ScoreBreakdownOut(
        ticker=ticker,
        score=round(result.score, 1),
        quality=result.quality_flag,
        confidence=result.confidence,
        factors=[f.to_dict() for f in result.factors],
    )


@router.post("/rescore-all", status_code=202)
def rescore_all(background: BackgroundTasks):
    """Re-run scoring for all tracked tickers."""
    def do_rescore():
        from loguru import logger
        db = SessionLocal()
        try:
            engine = ScoringEngine(db)
            tickers = [s.ticker for s in db.execute(__import__('sqlalchemy').select(Stock)).scalars().all()]
            results = engine.score_all(tickers)
            logger.info(f"Rescored {len(results)} tickers")
        finally:
            db.close()
    background.add_task(do_rescore)
    return {"message": "Rescoring all tickers in background"}


@router.post("/backtest")
def run_backtest(req: BacktestRequest, db: Session = Depends(get_db)):
    """Run backtest. Note: needs historical data already in DB.
    
    For meaningful results, populate at least 1 year of quotes + financials first.
    """
    bt = Backtester(db)
    end = req.end_date or date.today() - timedelta(days=req.hold_days)
    results = bt.run_rolling(
        start_date=req.start_date,
        end_date=end,
        hold_days=req.hold_days,
        rebalance_days=req.rebalance_days,
        tickers=req.tickers,
    )
    return bt.summary(results)
