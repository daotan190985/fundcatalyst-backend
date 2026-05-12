"""vnstock integration service.

vnstock is a community library that scrapes data from VCI/TCBS/MSN.
Their APIs change frequently — we wrap with retry logic, type coercion,
and graceful degradation so the rest of the app doesn't crash.
"""
import json
from datetime import datetime, date, timedelta
from typing import Optional, Any
from dataclasses import dataclass
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import pandas as pd

from app.config import settings


@dataclass
class StockInfo:
    """Normalized company info."""
    ticker: str
    name: str
    exchange: str
    sector: Optional[str] = None
    industry: Optional[str] = None
    listed_shares: Optional[int] = None
    description: Optional[str] = None


@dataclass
class QuoteBar:
    """Normalized OHLCV bar."""
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class FinancialPoint:
    """Normalized quarterly financial data."""
    year: int
    quarter: int
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    operating_profit: Optional[float] = None
    net_income: Optional[float] = None
    total_assets: Optional[float] = None
    total_equity: Optional[float] = None
    total_debt: Optional[float] = None
    eps: Optional[float] = None
    bvps: Optional[float] = None
    roe: Optional[float] = None
    roa: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None


class VNStockClient:
    """Thin wrapper around vnstock v3 API.

    Why a wrapper:
    - vnstock raises bare Exception on rate limits / 5xx — we want typed errors
    - Result schemas drift between versions — we coerce to our own dataclasses
    - We add retry with exponential backoff (transient failures common)
    """

    def __init__(self, source: str = None):
        self.source = source or settings.vnstock_source
        self._stock_cache: dict[str, Any] = {}

    def _get_stock(self, ticker: str):
        """Lazy-init Vnstock object per ticker (it's session-stateful)."""
        if ticker not in self._stock_cache:
            try:
                from vnstock import Vnstock
                self._stock_cache[ticker] = Vnstock().stock(symbol=ticker, source=self.source)
            except ImportError:
                raise RuntimeError("vnstock not installed. Run: pip install vnstock")
        return self._stock_cache[ticker]

    @retry(
        stop=stop_after_attempt(settings.vnstock_max_retries),
        wait=wait_exponential(multiplier=settings.vnstock_retry_delay, min=1, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        reraise=True,
    )
    def get_company_info(self, ticker: str) -> Optional[StockInfo]:
        """Fetch company profile. Returns None if unavailable."""
        try:
            stock = self._get_stock(ticker)
            df = stock.company.overview()
            if df is None or df.empty:
                logger.warning(f"No company info for {ticker}")
                return None

            row = df.iloc[0]
            return StockInfo(
                ticker=ticker,
                name=str(row.get("companyName") or row.get("short_name") or ticker),
                exchange=str(row.get("exchange") or "HOSE"),
                sector=str(row.get("industry") or row.get("sector") or "") or None,
                industry=str(row.get("subIndustry") or "") or None,
                listed_shares=self._safe_int(row.get("issueShare") or row.get("outstandingShare")),
                description=str(row.get("companyProfile") or "")[:2000] or None,
            )
        except Exception as e:
            logger.error(f"Failed to fetch company info for {ticker}: {e}")
            return None

    @retry(
        stop=stop_after_attempt(settings.vnstock_max_retries),
        wait=wait_exponential(multiplier=settings.vnstock_retry_delay, min=1, max=10),
        reraise=True,
    )
    def get_quotes(self, ticker: str, start: date, end: date) -> list[QuoteBar]:
        """Fetch daily OHLCV between dates. Returns empty list on failure."""
        try:
            stock = self._get_stock(ticker)
            df = stock.quote.history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1D",
            )
            if df is None or df.empty:
                return []

            bars = []
            for _, row in df.iterrows():
                d = row.get("time")
                if isinstance(d, str):
                    d = datetime.strptime(d[:10], "%Y-%m-%d").date()
                elif isinstance(d, pd.Timestamp):
                    d = d.date()

                if not isinstance(d, date):
                    continue

                bars.append(QuoteBar(
                    date=d,
                    open=float(row.get("open") or 0),
                    high=float(row.get("high") or 0),
                    low=float(row.get("low") or 0),
                    close=float(row.get("close") or 0),
                    volume=int(row.get("volume") or 0),
                ))
            return bars
        except Exception as e:
            logger.error(f"Failed to fetch quotes for {ticker}: {e}")
            return []

    def get_latest_quote(self, ticker: str) -> Optional[QuoteBar]:
        """Get most recent close. Tries last 7 days to handle weekends/holidays."""
        end = date.today()
        start = end - timedelta(days=10)
        bars = self.get_quotes(ticker, start, end)
        return bars[-1] if bars else None

    @retry(
        stop=stop_after_attempt(settings.vnstock_max_retries),
        wait=wait_exponential(multiplier=settings.vnstock_retry_delay, min=1, max=10),
        reraise=True,
    )
    def get_financials_quarterly(self, ticker: str, n_quarters: int = 8) -> list[FinancialPoint]:
        """Fetch quarterly income statement + balance sheet + ratios.

        vnstock returns multiple DataFrames — we merge on (year, quarter).
        """
        try:
            stock = self._get_stock(ticker)

            income = self._safe_df(lambda: stock.finance.income_statement(period="quarter", lang="en"))
            balance = self._safe_df(lambda: stock.finance.balance_sheet(period="quarter", lang="en"))
            ratios = self._safe_df(lambda: stock.finance.ratio(period="quarter", lang="en"))

            if income.empty and balance.empty:
                logger.warning(f"No financial data for {ticker}")
                return []

            # vnstock uses (yearReport, lengthReport) or ("Năm", "Kỳ") — handle both
            def period_cols(df):
                if df.empty:
                    return None, None
                cols = df.columns
                y_col = next((c for c in cols if "year" in c.lower() or c == "Năm"), None)
                q_col = next((c for c in cols if c.lower() in ("lengthreport", "quarter") or c == "Kỳ"), None)
                return y_col, q_col

            points: dict[tuple[int, int], FinancialPoint] = {}

            # Income statement
            if not income.empty:
                y_col, q_col = period_cols(income)
                if y_col and q_col:
                    for _, row in income.iterrows():
                        y, q = self._safe_int(row[y_col]), self._safe_int(row[q_col])
                        if y is None or q is None or not (1 <= q <= 4):
                            continue
                        p = points.setdefault((y, q), FinancialPoint(year=y, quarter=q))
                        p.revenue = self._pick(row, ["Revenue", "Doanh thu thuần", "Net Revenue"])
                        p.gross_profit = self._pick(row, ["Gross Profit", "Lợi nhuận gộp"])
                        p.operating_profit = self._pick(row, ["Operating Profit", "LN từ HĐKD"])
                        p.net_income = self._pick(row, ["Net Profit For the Year", "LNST", "Net Income", "Attribute to parent company"])

            # Balance sheet
            if not balance.empty:
                y_col, q_col = period_cols(balance)
                if y_col and q_col:
                    for _, row in balance.iterrows():
                        y, q = self._safe_int(row[y_col]), self._safe_int(row[q_col])
                        if y is None or q is None or not (1 <= q <= 4):
                            continue
                        p = points.setdefault((y, q), FinancialPoint(year=y, quarter=q))
                        p.total_assets = self._pick(row, ["TOTAL RESOURCES", "Tổng tài sản", "Total Assets"])
                        p.total_equity = self._pick(row, ["OWNER'S EQUITY(Bn.VND)", "Vốn chủ sở hữu", "Equity"])
                        p.total_debt = self._pick(row, ["LIABILITIES", "Tổng nợ", "Total Liabilities"])

            # Ratios
            if not ratios.empty:
                y_col, q_col = period_cols(ratios)
                if y_col and q_col:
                    for _, row in ratios.iterrows():
                        y, q = self._safe_int(row[y_col]), self._safe_int(row[q_col])
                        if y is None or q is None or not (1 <= q <= 4):
                            continue
                        p = points.setdefault((y, q), FinancialPoint(year=y, quarter=q))
                        p.eps = self._pick(row, ["EPS (VND)", "EPS"])
                        p.bvps = self._pick(row, ["BVPS (VND)", "BVPS"])
                        p.roe = self._pick(row, ["ROE (%)", "ROE"])
                        p.roa = self._pick(row, ["ROA (%)", "ROA"])
                        p.gross_margin = self._pick(row, ["Gross Profit Margin (%)", "Biên LN gộp"])
                        p.net_margin = self._pick(row, ["Net Profit Margin (%)", "Biên LNST"])

            # Compute margins if not available from ratios
            for p in points.values():
                if p.gross_margin is None and p.revenue and p.gross_profit:
                    p.gross_margin = (p.gross_profit / p.revenue) * 100
                if p.net_margin is None and p.revenue and p.net_income:
                    p.net_margin = (p.net_income / p.revenue) * 100

            # Sort newest first, take n_quarters
            sorted_points = sorted(points.values(), key=lambda x: (x.year, x.quarter), reverse=True)
            return sorted_points[:n_quarters]

        except Exception as e:
            logger.error(f"Failed to fetch financials for {ticker}: {e}")
            return []

    def get_foreign_trade(self, ticker: str, days: int = 30) -> list[dict]:
        """Fetch foreign trading data. Optional - some sources don't expose this."""
        try:
            stock = self._get_stock(ticker)
            end = date.today()
            start = end - timedelta(days=days)
            df = stock.quote.history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1D",
            )
            if df is None or df.empty:
                return []

            # Foreign data may or may not be in this endpoint depending on source
            results = []
            for _, row in df.iterrows():
                d = row.get("time")
                if isinstance(d, str):
                    d = datetime.strptime(d[:10], "%Y-%m-%d").date()
                elif isinstance(d, pd.Timestamp):
                    d = d.date()

                fbuy = self._safe_float(row.get("foreign_buy_volume"))
                fsell = self._safe_float(row.get("foreign_sell_volume"))
                if fbuy is None and fsell is None:
                    continue

                close = float(row.get("close") or 0)
                net_volume = (fbuy or 0) - (fsell or 0)
                net_value = net_volume * close / 1_000_000_000  # tỷ VND

                results.append({
                    "date": d,
                    "buy_volume": int(fbuy or 0),
                    "sell_volume": int(fsell or 0),
                    "net_value": net_value,
                })
            return results
        except Exception as e:
            logger.warning(f"Foreign trade data not available for {ticker}: {e}")
            return []

    # ---- Helpers ----

    @staticmethod
    def _safe_df(fn):
        """Run a DataFrame-returning function, return empty DataFrame on error."""
        try:
            df = fn()
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception as e:
            logger.debug(f"DataFrame fetch failed: {e}")
            return pd.DataFrame()

    @staticmethod
    def _safe_int(v) -> Optional[int]:
        try:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return None
            return int(v)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _safe_float(v) -> Optional[float]:
        try:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return None
            return float(v)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _pick(row, candidate_cols: list[str]) -> Optional[float]:
        """Try each candidate column name, return first non-null float."""
        for c in candidate_cols:
            if c in row.index:
                v = row[c]
                if v is not None and not (isinstance(v, float) and pd.isna(v)):
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        continue
        return None


# Global client instance
vn_client = VNStockClient()
