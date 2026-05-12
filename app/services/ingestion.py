"""Data ingestion service.

Orchestrates fetching from vnstock and upserting into Postgres.
Designed to be called from API endpoints or scheduled jobs.
"""
import json
from datetime import datetime, date, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import select, func, desc
from loguru import logger

from app.models import Stock, Quote, FinancialQuarter, LatestMetric, ForeignTrade, JobRun
from app.services.vnstock_service import vn_client, StockInfo, QuoteBar, FinancialPoint


class IngestionService:
    """All write-side data operations."""

    def __init__(self, db: Session):
        self.db = db

    def upsert_stock(self, ticker: str, force_refresh: bool = False) -> Optional[Stock]:
        """Create or update Stock master record. Returns the ORM object."""
        existing = self.db.get(Stock, ticker)

        # Skip refresh if we already have it and not forcing
        if existing and not force_refresh:
            return existing

        info = vn_client.get_company_info(ticker)
        if not info:
            if existing:
                return existing
            # Create minimal placeholder so we can still track this ticker
            stock = Stock(ticker=ticker, name=ticker, exchange="HOSE")
            self.db.add(stock)
            self.db.commit()
            return stock

        if existing:
            existing.name = info.name
            existing.exchange = info.exchange
            existing.sector = info.sector
            existing.industry = info.industry
            existing.listed_shares = info.listed_shares
            existing.description = info.description
            existing.updated_at = datetime.utcnow()
            stock = existing
        else:
            stock = Stock(
                ticker=info.ticker,
                name=info.name,
                exchange=info.exchange,
                sector=info.sector,
                industry=info.industry,
                listed_shares=info.listed_shares,
                description=info.description,
            )
            self.db.add(stock)

        self.db.commit()
        self.db.refresh(stock)
        return stock

    def ingest_quotes(self, ticker: str, days: int = 365) -> int:
        """Fetch & upsert daily quotes. Returns number of new rows inserted."""
        end = date.today()
        start = end - timedelta(days=days)
        bars = vn_client.get_quotes(ticker, start, end)
        if not bars:
            return 0

        # Get existing dates to avoid duplicate inserts
        existing_dates = {
            row[0] for row in self.db.execute(
                select(Quote.date).where(Quote.ticker == ticker, Quote.date >= start)
            ).all()
        }

        new_count = 0
        for bar in bars:
            if bar.date in existing_dates:
                # Update close in case it's today's bar that's still moving
                if bar.date == end:
                    q = self.db.scalar(
                        select(Quote).where(Quote.ticker == ticker, Quote.date == bar.date)
                    )
                    if q:
                        q.close = bar.close
                        q.high = bar.high
                        q.low = bar.low
                        q.volume = bar.volume
                continue

            q = Quote(
                ticker=ticker,
                date=bar.date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                value=int(bar.close * bar.volume * 1000) if bar.close and bar.volume else 0,
            )
            self.db.add(q)
            new_count += 1

        self.db.commit()
        return new_count

    def ingest_financials(self, ticker: str, n_quarters: int = 12) -> int:
        """Fetch & upsert quarterly financials with computed YoY/QoQ growth."""
        points = vn_client.get_financials_quarterly(ticker, n_quarters=n_quarters)
        if not points:
            return 0

        # Sort oldest first so we can compute growth rates against prior periods
        points_sorted = sorted(points, key=lambda p: (p.year, p.quarter))
        by_period = {(p.year, p.quarter): p for p in points_sorted}

        new_count = 0
        for p in points_sorted:
            # Compute YoY (same quarter, previous year)
            yoy_key = (p.year - 1, p.quarter)
            yoy = by_period.get(yoy_key) or self._fetch_existing(ticker, p.year - 1, p.quarter)
            revenue_yoy = self._growth(p.revenue, yoy.revenue if yoy else None)
            net_income_yoy = self._growth(p.net_income, yoy.net_income if yoy else None)

            # Compute QoQ (previous quarter)
            prev_y, prev_q = (p.year, p.quarter - 1) if p.quarter > 1 else (p.year - 1, 4)
            qoq = by_period.get((prev_y, prev_q)) or self._fetch_existing(ticker, prev_y, prev_q)
            revenue_qoq = self._growth(p.revenue, qoq.revenue if qoq else None)
            net_income_qoq = self._growth(p.net_income, qoq.net_income if qoq else None)

            existing = self.db.scalar(
                select(FinancialQuarter).where(
                    FinancialQuarter.ticker == ticker,
                    FinancialQuarter.year == p.year,
                    FinancialQuarter.quarter == p.quarter,
                )
            )

            data = dict(
                revenue=p.revenue,
                gross_profit=p.gross_profit,
                operating_profit=p.operating_profit,
                net_income=p.net_income,
                total_assets=p.total_assets,
                total_equity=p.total_equity,
                total_debt=p.total_debt,
                eps=p.eps,
                bvps=p.bvps,
                roe=p.roe,
                roa=p.roa,
                gross_margin=p.gross_margin,
                net_margin=p.net_margin,
                revenue_yoy=revenue_yoy,
                net_income_yoy=net_income_yoy,
                revenue_qoq=revenue_qoq,
                net_income_qoq=net_income_qoq,
            )

            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
            else:
                self.db.add(FinancialQuarter(
                    ticker=ticker, year=p.year, quarter=p.quarter, **data
                ))
                new_count += 1

        self.db.commit()
        return new_count

    def update_latest_metric(self, ticker: str) -> Optional[LatestMetric]:
        """Compute & upsert the denormalized latest snapshot for fast reads.

        Calculates: current price, change_pct, volume_spike (20-day avg),
        market_cap, TTM EPS/ROE, foreign_net_5d, score (via ScoringEngine).
        """
        stock = self.db.get(Stock, ticker)
        if not stock:
            return None

        # Last 21 daily bars
        recent = self.db.execute(
            select(Quote).where(Quote.ticker == ticker).order_by(desc(Quote.date)).limit(21)
        ).scalars().all()

        if not recent:
            return None

        latest_bar = recent[0]
        prev_bar = recent[1] if len(recent) > 1 else None
        avg_vol_20 = sum(r.volume for r in recent[:20]) / max(1, len(recent[:20])) if recent else None
        change_pct = ((latest_bar.close - prev_bar.close) / prev_bar.close * 100) if prev_bar and prev_bar.close else 0
        volume_spike = (latest_bar.volume / avg_vol_20) if avg_vol_20 else None

        # TTM financials: sum last 4 quarters of net_income / latest equity
        last_4q = self.db.execute(
            select(FinancialQuarter).where(FinancialQuarter.ticker == ticker)
            .order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
            .limit(4)
        ).scalars().all()

        eps_ttm = None
        roe_ttm = None
        revenue_yoy = None
        net_income_yoy = None
        if last_4q:
            sum_ni = sum(q.net_income for q in last_4q if q.net_income)
            if stock.listed_shares and sum_ni:
                eps_ttm = sum_ni * 1_000_000 / stock.listed_shares
            roe_ttm = last_4q[0].roe
            revenue_yoy = last_4q[0].revenue_yoy
            net_income_yoy = last_4q[0].net_income_yoy

        # Valuation
        price = latest_bar.close * 1000  # vnstock returns in thousands VND
        pe = (price / eps_ttm) if eps_ttm and eps_ttm > 0 else None
        bvps = last_4q[0].bvps if last_4q and last_4q[0].bvps else None
        pb = (price / bvps) if bvps and bvps > 0 else None
        market_cap = int(price * stock.listed_shares / 1_000_000_000) if stock.listed_shares else None

        # Foreign flow last 5 days
        foreign_5d = self.db.execute(
            select(func.sum(ForeignTrade.net_value)).where(
                ForeignTrade.ticker == ticker,
                ForeignTrade.date >= date.today() - timedelta(days=7),
            )
        ).scalar()

        # Upsert metric (without score yet)
        metric = self.db.get(LatestMetric, ticker)
        if not metric:
            metric = LatestMetric(ticker=ticker)
            self.db.add(metric)

        metric.price = price
        metric.change_pct = change_pct
        metric.volume = latest_bar.volume
        metric.avg_volume_20 = int(avg_vol_20) if avg_vol_20 else None
        metric.volume_spike = volume_spike
        metric.pe = pe
        metric.pb = pb
        metric.market_cap = market_cap
        metric.eps_ttm = eps_ttm
        metric.roe_ttm = roe_ttm
        metric.revenue_yoy = revenue_yoy
        metric.net_income_yoy = net_income_yoy
        metric.foreign_net_5d = float(foreign_5d) if foreign_5d else 0
        metric.updated_at = datetime.utcnow()

        self.db.commit()

        # Now compute real score using ScoringEngine
        try:
            from app.scoring import ScoringEngine
            scorer = ScoringEngine(self.db)
            scorer.score_and_save(ticker)
            self.db.refresh(metric)
        except Exception as e:
            logger.warning(f"Scoring failed for {ticker}: {e}")

        return metric

    # ---- Job orchestration ----

    def run_full_refresh(self, tickers: list[str]) -> JobRun:
        """Refresh everything for a list of tickers. Records audit entry."""
        job = JobRun(job_name="full_refresh", started_at=datetime.utcnow(), status="running")
        self.db.add(job)
        self.db.commit()

        errors = []
        processed = 0

        for ticker in tickers:
            try:
                logger.info(f"Refreshing {ticker}")
                self.upsert_stock(ticker)
                self.ingest_quotes(ticker, days=365)
                self.ingest_financials(ticker, n_quarters=8)
                self.update_latest_metric(ticker)
                processed += 1
            except Exception as e:
                err_msg = f"{ticker}: {e}"
                logger.error(err_msg)
                errors.append(err_msg)

        job.finished_at = datetime.utcnow()
        job.status = "success" if not errors else ("partial" if processed > 0 else "failed")
        job.records_processed = processed
        job.errors = "\n".join(errors) if errors else None
        self.db.commit()
        return job

    def run_quote_refresh(self, tickers: list[str]) -> JobRun:
        """Lighter job: only refresh recent quotes and metrics. Runs every 5 min."""
        job = JobRun(job_name="quote_refresh", started_at=datetime.utcnow(), status="running")
        self.db.add(job)
        self.db.commit()

        errors = []
        processed = 0
        for ticker in tickers:
            try:
                self.ingest_quotes(ticker, days=30)
                self.update_latest_metric(ticker)
                processed += 1
            except Exception as e:
                errors.append(f"{ticker}: {e}")

        job.finished_at = datetime.utcnow()
        job.status = "success" if not errors else "partial"
        job.records_processed = processed
        job.errors = "\n".join(errors) if errors else None
        self.db.commit()
        return job

    # ---- Helpers ----

    def _fetch_existing(self, ticker: str, year: int, quarter: int) -> Optional[FinancialQuarter]:
        return self.db.scalar(
            select(FinancialQuarter).where(
                FinancialQuarter.ticker == ticker,
                FinancialQuarter.year == year,
                FinancialQuarter.quarter == quarter,
            )
        )

    @staticmethod
    def _growth(current: Optional[float], prior: Optional[float]) -> Optional[float]:
        """% growth, returns None if either is missing/zero."""
        if current is None or prior is None or prior == 0:
            return None
        return (current - prior) / abs(prior) * 100
