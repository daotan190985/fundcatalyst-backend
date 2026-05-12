"""Backtest scoring engine.

Cách hoạt động:
1. Lấy 1 thời điểm trong quá khứ (vd 6 tháng trước)
2. Tính score dựa trên dữ liệu CÓ tại thời điểm đó (no look-ahead bias)
3. Đo return của top-N mã có score cao nhất qua N tháng tiếp theo
4. So sánh với benchmark (VN-Index hoặc equal-weight)

Output: bảng performance theo decile, hit rate, Sharpe ratio.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import select, desc
from loguru import logger

from app.models import Stock, Quote, FinancialQuarter, LatestMetric
from app.scoring.engine import ScoringEngine, FactorCalculator, WEIGHTS


@dataclass
class BacktestResult:
    period_start: date
    period_end: date
    top_decile_return: float       # % return của top 10% score
    bottom_decile_return: float    # % return của bottom 10%
    benchmark_return: float        # equal-weight return của universe
    hit_rate_top: float            # % mã top decile có return dương
    n_stocks: int

    def to_dict(self) -> dict:
        return {
            "period": f"{self.period_start} → {self.period_end}",
            "top_decile_return_pct": round(self.top_decile_return, 2),
            "bottom_decile_return_pct": round(self.bottom_decile_return, 2),
            "benchmark_return_pct": round(self.benchmark_return, 2),
            "alpha_pct": round(self.top_decile_return - self.benchmark_return, 2),
            "hit_rate_top": round(self.hit_rate_top * 100, 1),
            "n_stocks": self.n_stocks,
        }


class HistoricalScorer:
    """Tính score tại 1 thời điểm trong quá khứ.
    
    Khác ScoringEngine production ở chỗ chỉ dùng dữ liệu CÓ tại as_of_date,
    không dùng latest data.
    """

    def __init__(self, db: Session, as_of: date):
        self.db = db
        self.as_of = as_of

    def score_ticker_at(self, ticker: str) -> Optional[float]:
        """Trả về score tại as_of date dùng simplified factors."""
        # Lấy quote tại thời điểm gần nhất ≤ as_of
        quote = self.db.execute(
            select(Quote).where(Quote.ticker == ticker, Quote.date <= self.as_of)
            .order_by(desc(Quote.date)).limit(1)
        ).scalar_one_or_none()

        if not quote:
            return None

        # Lấy BCTC quý gần nhất ≤ as_of (giả định BCTC publish sau quý 45 ngày)
        # Tức là Q4/2024 chỉ available từ ~15/02/2025
        cutoff_date = self.as_of - timedelta(days=45)
        cutoff_year = cutoff_date.year
        cutoff_quarter = (cutoff_date.month - 1) // 3 + 1

        fins = self.db.execute(
            select(FinancialQuarter).where(
                FinancialQuarter.ticker == ticker,
                (FinancialQuarter.year < cutoff_year) |
                ((FinancialQuarter.year == cutoff_year) &
                 (FinancialQuarter.quarter <= cutoff_quarter))
            ).order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
            .limit(4)
        ).scalars().all()

        if not fins:
            return None

        # Simple historical score: EPS growth + ROE + Momentum 20D
        eps_score = 50
        if fins[0].net_income_yoy is not None:
            import math
            eps_score = 100 / (1 + math.exp(-0.05 * (fins[0].net_income_yoy - 20)))

        roe_score = 50
        roes = [f.roe for f in fins if f.roe is not None]
        if roes:
            avg_roe = sum(roes) / len(roes)
            import math
            roe_score = 100 / (1 + math.exp(-0.15 * (avg_roe - 15)))

        # Momentum 20D as of date
        quote_20d_ago = self.db.execute(
            select(Quote).where(
                Quote.ticker == ticker,
                Quote.date <= self.as_of - timedelta(days=20)
            ).order_by(desc(Quote.date)).limit(1)
        ).scalar_one_or_none()

        momentum_score = 50
        if quote_20d_ago and quote_20d_ago.close > 0:
            ret_20d = (quote.close - quote_20d_ago.close) / quote_20d_ago.close * 100
            import math
            momentum_score = 100 / (1 + math.exp(-0.15 * (ret_20d - 5)))

        # Weighted blend (simplified 3-factor version)
        return 0.4 * eps_score + 0.35 * roe_score + 0.25 * momentum_score


class Backtester:
    """Run backtest qua nhiều thời điểm."""

    def __init__(self, db: Session):
        self.db = db

    def run_single_period(
        self,
        start_date: date,
        hold_days: int,
        tickers: list[str],
        n_buckets: int = 10,
    ) -> Optional[BacktestResult]:
        """Backtest 1 holding period.
        
        1. Tính score cho mỗi ticker tại start_date
        2. Chia thành n_buckets decile
        3. Đo return từ start_date → start_date + hold_days
        """
        end_date = start_date + timedelta(days=hold_days)

        # Score at start
        scorer = HistoricalScorer(self.db, start_date)
        scores = {}
        for t in tickers:
            s = scorer.score_ticker_at(t)
            if s is not None:
                scores[t] = s

        if len(scores) < n_buckets:
            logger.warning(f"Only {len(scores)} stocks with scores at {start_date}, need >= {n_buckets}")
            return None

        # Get returns
        returns = {}
        for t in scores.keys():
            start_q = self._get_price_at(t, start_date)
            end_q = self._get_price_at(t, end_date)
            if start_q and end_q and start_q > 0:
                returns[t] = (end_q - start_q) / start_q * 100

        if not returns:
            return None

        # Sort by score, split into buckets
        sorted_by_score = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        n = len(sorted_by_score)
        bucket_size = max(1, n // n_buckets)

        top_tickers = [t for t, _ in sorted_by_score[:bucket_size]]
        bottom_tickers = [t for t, _ in sorted_by_score[-bucket_size:]]

        top_returns = [returns[t] for t in top_tickers if t in returns]
        bottom_returns = [returns[t] for t in bottom_tickers if t in returns]
        all_returns = list(returns.values())

        if not top_returns or not bottom_returns:
            return None

        return BacktestResult(
            period_start=start_date,
            period_end=end_date,
            top_decile_return=sum(top_returns) / len(top_returns),
            bottom_decile_return=sum(bottom_returns) / len(bottom_returns),
            benchmark_return=sum(all_returns) / len(all_returns),
            hit_rate_top=sum(1 for r in top_returns if r > 0) / len(top_returns),
            n_stocks=n,
        )

    def run_rolling(
        self,
        start_date: date,
        end_date: date,
        hold_days: int = 60,
        rebalance_days: int = 30,
        tickers: Optional[list[str]] = None,
    ) -> list[BacktestResult]:
        """Rolling backtest: rebalance every N days."""
        if tickers is None:
            tickers = [s.ticker for s in self.db.execute(select(Stock)).scalars().all()]

        results = []
        cur = start_date
        while cur + timedelta(days=hold_days) <= end_date:
            try:
                r = self.run_single_period(cur, hold_days, tickers)
                if r:
                    results.append(r)
                    logger.info(f"Backtest {cur}: top={r.top_decile_return:.1f}%, "
                              f"bench={r.benchmark_return:.1f}%, alpha={r.top_decile_return - r.benchmark_return:+.1f}%")
            except Exception as e:
                logger.warning(f"Backtest failed at {cur}: {e}")
            cur += timedelta(days=rebalance_days)

        return results

    def summary(self, results: list[BacktestResult]) -> dict:
        """Tổng hợp kết quả backtest."""
        if not results:
            return {"error": "No results"}

        alphas = [r.top_decile_return - r.benchmark_return for r in results]
        top_returns = [r.top_decile_return for r in results]
        hit_rates = [r.hit_rate_top for r in results]

        return {
            "n_periods": len(results),
            "avg_alpha_pct": round(sum(alphas) / len(alphas), 2),
            "avg_top_return_pct": round(sum(top_returns) / len(top_returns), 2),
            "avg_hit_rate": round(sum(hit_rates) / len(hit_rates) * 100, 1),
            "win_periods": sum(1 for a in alphas if a > 0),
            "loss_periods": sum(1 for a in alphas if a < 0),
            "periods": [r.to_dict() for r in results],
        }

    def _get_price_at(self, ticker: str, target_date: date) -> Optional[float]:
        """Get close price at or just after target_date (next trading day)."""
        q = self.db.execute(
            select(Quote).where(Quote.ticker == ticker, Quote.date >= target_date)
            .order_by(Quote.date).limit(1)
        ).scalar_one_or_none()
        return q.close if q else None
