"""SQLAlchemy ORM models. Schema designed for VN stock fundamental analysis."""
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Date, Text, ForeignKey,
    UniqueConstraint, Index, BigInteger, Boolean
)
from sqlalchemy.orm import relationship
from datetime import datetime, date as date_type

from app.database import Base


class Stock(Base):
    """Master table of tracked stocks."""
    __tablename__ = "stocks"

    ticker = Column(String(10), primary_key=True)  # FPT, HPG, ...
    name = Column(String(255), nullable=False)
    exchange = Column(String(10), nullable=False)  # HOSE, HNX, UPCOM
    sector = Column(String(100), index=True)
    industry = Column(String(100))
    listed_shares = Column(BigInteger)  # số lượng CP niêm yết
    description = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    quotes = relationship("Quote", back_populates="stock", cascade="all, delete-orphan")
    financials = relationship("FinancialQuarter", back_populates="stock", cascade="all, delete-orphan")
    latest_metric = relationship("LatestMetric", back_populates="stock", uselist=False, cascade="all, delete-orphan")


class Quote(Base):
    """Daily OHLCV history."""
    __tablename__ = "quotes"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float, nullable=False)
    volume = Column(BigInteger, default=0)
    value = Column(BigInteger, default=0)  # giá trị giao dịch (VND)

    stock = relationship("Stock", back_populates="quotes")

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_quote_ticker_date"),
        Index("ix_quote_ticker_date", "ticker", "date"),
    )


class FinancialQuarter(Base):
    """Quarterly financial statements."""
    __tablename__ = "financials_quarter"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False)
    year = Column(Integer, nullable=False)
    quarter = Column(Integer, nullable=False)  # 1-4

    # Income statement (đơn vị: triệu VND, theo vnstock)
    revenue = Column(Float)  # doanh thu thuần
    gross_profit = Column(Float)  # lợi nhuận gộp
    operating_profit = Column(Float)  # LN hoạt động
    net_income = Column(Float)  # LNST

    # Balance sheet
    total_assets = Column(Float)
    total_equity = Column(Float)
    total_debt = Column(Float)

    # Derived metrics
    eps = Column(Float)  # EPS (VND)
    bvps = Column(Float)  # Book value per share
    roe = Column(Float)  # %
    roa = Column(Float)  # %
    gross_margin = Column(Float)  # %
    net_margin = Column(Float)  # %

    # YoY/QoQ growth (computed on insert)
    revenue_yoy = Column(Float)  # %
    net_income_yoy = Column(Float)  # %
    revenue_qoq = Column(Float)
    net_income_qoq = Column(Float)

    reported_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    stock = relationship("Stock", back_populates="financials")

    __table_args__ = (
        UniqueConstraint("ticker", "year", "quarter", name="uq_fin_q"),
        Index("ix_fin_ticker_period", "ticker", "year", "quarter"),
    )


class LatestMetric(Base):
    """Denormalized 'current snapshot' for fast dashboard reads.
    Updated by scoring job. One row per ticker.
    """
    __tablename__ = "latest_metrics"

    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), primary_key=True)

    # Current price
    price = Column(Float)
    change_pct = Column(Float)  # % change today
    volume = Column(BigInteger)
    avg_volume_20 = Column(BigInteger)
    volume_spike = Column(Float)  # volume / avg_volume_20

    # Valuation (TTM)
    pe = Column(Float)
    pb = Column(Float)
    market_cap = Column(BigInteger)  # tỷ VND

    # Latest financials
    eps_ttm = Column(Float)
    roe_ttm = Column(Float)
    revenue_yoy = Column(Float)
    net_income_yoy = Column(Float)

    # Foreign flow (last 5 days net buy, tỷ VND)
    foreign_net_5d = Column(Float)

    # Score (0-100)
    score = Column(Float)
    score_components = Column(Text)  # JSON: { eps_growth: 85, roe: 90, ... }

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    stock = relationship("Stock", back_populates="latest_metric")


class ForeignTrade(Base):
    """Daily foreign trading flow."""
    __tablename__ = "foreign_trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    buy_volume = Column(BigInteger, default=0)
    sell_volume = Column(BigInteger, default=0)
    net_value = Column(Float)  # tỷ VND (positive = net buy)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_foreign_ticker_date"),
    )


class JobRun(Base):
    """Audit log for scheduled jobs - critical for debugging data freshness."""
    __tablename__ = "job_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_name = Column(String(100), nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
    status = Column(String(20))  # success, failed, partial
    records_processed = Column(Integer, default=0)
    errors = Column(Text)
    notes = Column(Text)
