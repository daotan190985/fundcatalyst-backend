"""Pydantic schemas for API request/response validation."""
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, date
from typing import Optional


class StockBase(BaseModel):
    """Minimal stock info."""
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    name: str
    exchange: str
    sector: Optional[str] = None


class QuoteOut(BaseModel):
    """Single OHLCV bar."""
    model_config = ConfigDict(from_attributes=True)

    date: date
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: float
    volume: Optional[int] = None


class StockSummary(BaseModel):
    """Card-level data — for dashboard list."""
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    name: str
    sector: Optional[str] = None
    price: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[int] = None
    volume_spike: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None
    roe_ttm: Optional[float] = None
    eps_ttm: Optional[float] = None
    market_cap: Optional[int] = None
    foreign_net_5d: Optional[float] = None
    score: Optional[float] = None
    score_components: Optional[dict] = None


class FinancialOut(BaseModel):
    """Quarterly financial point."""
    model_config = ConfigDict(from_attributes=True)

    year: int
    quarter: int
    revenue: Optional[float] = None
    net_income: Optional[float] = None
    eps: Optional[float] = None
    roe: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_yoy: Optional[float] = None
    net_income_yoy: Optional[float] = None


class StockDetail(StockSummary):
    """Full detail view — for /stocks/{ticker} endpoint."""
    industry: Optional[str] = None
    listed_shares: Optional[int] = None
    description: Optional[str] = None
    quotes: list[QuoteOut] = Field(default_factory=list)
    financials: list[FinancialOut] = Field(default_factory=list)


class JobRunOut(BaseModel):
    """Background job audit info."""
    model_config = ConfigDict(from_attributes=True)

    job_name: str
    started_at: datetime
    finished_at: Optional[datetime]
    status: str
    records_processed: int
    errors: Optional[str]


class HealthOut(BaseModel):
    """Service health check."""
    status: str
    version: str
    database: str
    last_quote_refresh: Optional[datetime] = None
    tickers_tracked: int = 0


class ErrorOut(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None
