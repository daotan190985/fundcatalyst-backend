"""Smoke tests + unit tests for all modules.

Run: pytest tests/ -v

Most tests don't need a real DB or network. They verify:
- Imports succeed
- Helper functions work
- ORM models can be instantiated
- API routes are registered
- Scoring math is correct
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================
# Import tests
# ============================================================

def test_imports():
    """Every module imports without error."""
    from app import main, config, database, scheduler
    from app.models import (
        Stock, Quote, FinancialQuarter, LatestMetric, JobRun, ForeignTrade,
        NewsArticle, NewsMention, Catalyst,
        Alert, AlertRule, Watchlist,
    )
    from app.models import schemas
    from app.services import vn_client, VNStockClient, IngestionService
    from app.scrapers import CafeFScraper, FireAntScraper, NewsIngestionService
    from app.llm import get_llm_client, NewsSummarizer
    from app.alerts import AlertEngine, DEFAULT_RULES
    from app.scoring import ScoringEngine, Backtester, WEIGHTS
    from app.api import stocks_router, meta_router, news_router, alerts_router, scoring_router


# ============================================================
# Config
# ============================================================

def test_config_defaults():
    from app.config import settings
    assert settings.app_name
    assert settings.database_url
    assert len(settings.default_tickers) >= 10
    assert "FPT" in settings.default_tickers


# ============================================================
# Schemas
# ============================================================

def test_schemas():
    from app.models.schemas import StockSummary, HealthOut, FinancialOut
    s = StockSummary(ticker="FPT", name="FPT Corp", sector="Tech", price=142.5, score=85.0)
    assert s.ticker == "FPT"
    j = s.model_dump_json()
    assert "FPT" in j

    h = HealthOut(status="ok", version="0.1.0", database="ok")
    assert h.status == "ok"

    f = FinancialOut(year=2025, quarter=1, revenue=1000, eps=500, roe=15.5)
    assert f.year == 2025


# ============================================================
# Ingestion helpers
# ============================================================

def test_growth_calculation():
    from app.services.ingestion import IngestionService
    assert IngestionService._growth(110, 100) == 10.0
    assert IngestionService._growth(90, 100) == -10.0
    assert IngestionService._growth(100, 0) is None
    assert IngestionService._growth(None, 100) is None
    assert IngestionService._growth(100, None) is None


def test_vnstock_helpers():
    from app.services.vnstock_service import VNStockClient
    assert VNStockClient._safe_int(5) == 5
    assert VNStockClient._safe_int("5") == 5
    assert VNStockClient._safe_int(None) is None
    assert VNStockClient._safe_float("3.14") == 3.14
    assert VNStockClient._safe_float(None) is None
    assert VNStockClient._safe_float("not a number") is None


# ============================================================
# Scoring engine
# ============================================================

def test_scoring_weights_sum_to_one():
    from app.scoring import WEIGHTS
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 0.001, f"Weights sum to {total}, must be 1.0"


def test_scoring_helpers():
    from app.scoring.engine import _sigmoid_score, _linear_score, _clip

    # Sigmoid: at midpoint = 50
    assert abs(_sigmoid_score(20, midpoint=20) - 50.0) < 0.1
    # Sigmoid: high above midpoint = ~100
    assert _sigmoid_score(100, midpoint=20) > 95
    # Sigmoid: far below midpoint = ~0
    assert _sigmoid_score(-50, midpoint=20) < 5

    # Linear
    assert _linear_score(50, 0, 100) == 50
    assert _linear_score(0, 0, 100) == 0
    assert _linear_score(100, 0, 100) == 100
    # Invert
    assert _linear_score(25, 0, 100, invert=True) == 75

    # Clip
    assert _clip(150) == 100
    assert _clip(-10) == 0
    assert _clip(50) == 50


def test_factor_score_dataclass():
    from app.scoring.engine import FactorScore
    f = FactorScore("test", 75.0, 20.0, 0.25, "test reason")
    d = f.to_dict()
    assert d["value"] == 75.0
    assert d["weight"] == 0.25


# ============================================================
# Alert engine
# ============================================================

def test_alert_default_rules():
    from app.alerts import DEFAULT_RULES
    assert len(DEFAULT_RULES) >= 5
    rule_types = [r["rule_type"] for r in DEFAULT_RULES]
    assert "price_spike" in rule_types
    assert "volume_spike" in rule_types
    assert "foreign_net_buy" in rule_types
    # Validate params shape
    for r in DEFAULT_RULES:
        assert "params" in r
        assert isinstance(r["params"], dict)


# ============================================================
# News scraper
# ============================================================

def test_cafef_ticker_extraction():
    from app.scrapers.cafef import CafeFScraper
    tickers = CafeFScraper._extract_tickers("FPT báo lãi Q1, HPG mở rộng nhà máy")
    assert "FPT" in tickers
    assert "HPG" in tickers
    # Exclude common false positives
    tickers2 = CafeFScraper._extract_tickers("USD/VND tăng, CEO báo cáo về GDP")
    assert "USD" not in tickers2
    assert "VND" not in tickers2
    assert "CEO" not in tickers2
    assert "GDP" not in tickers2


def test_cafef_date_parse():
    from app.scrapers.cafef import CafeFScraper
    from datetime import datetime
    # ISO
    d = CafeFScraper._parse_date("2026-05-12T14:35:00")
    assert d and d.year == 2026
    # VN format
    d2 = CafeFScraper._parse_date("12-05-2026 - 14:35")
    assert d2 and d2.year == 2026 and d2.month == 5
    # Invalid
    assert CafeFScraper._parse_date("garbage") is None
    assert CafeFScraper._parse_date("") is None
    assert CafeFScraper._parse_date(None) is None


# ============================================================
# LLM
# ============================================================

def test_llm_fallback_works_without_api_key():
    """Fallback client returns valid JSON even without API key."""
    import asyncio, json
    from app.llm.client import FallbackClient
    client = FallbackClient()
    resp = asyncio.run(client.complete("test"))
    data = json.loads(resp)
    assert "summary" in data
    assert "sentiment" in data


def test_llm_json_parser():
    """Summarizer can extract JSON from various LLM responses."""
    from app.llm.summarizer import NewsSummarizer
    # Clean JSON
    assert NewsSummarizer._parse_json('{"a": 1}') == {"a": 1}
    # Markdown fenced
    assert NewsSummarizer._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    # With surrounding text
    assert NewsSummarizer._parse_json('blah blah {"a": 1} more text') == {"a": 1}
    # Invalid
    assert NewsSummarizer._parse_json('not json at all') is None
    assert NewsSummarizer._parse_json('') is None


def test_llm_client_factory():
    """get_llm_client returns FallbackClient when no API keys set."""
    import os
    from app.llm.client import get_llm_client, FallbackClient
    # Backup env
    backup = {
        "ANTHROPIC_API_KEY": os.environ.pop("ANTHROPIC_API_KEY", None),
        "OPENAI_API_KEY": os.environ.pop("OPENAI_API_KEY", None),
    }
    try:
        client = get_llm_client()
        assert isinstance(client, FallbackClient)
    finally:
        for k, v in backup.items():
            if v: os.environ[k] = v


# ============================================================
# FastAPI app
# ============================================================

def test_fastapi_routes():
    """All routers registered with expected paths."""
    from app.main import app
    paths = {r.path for r in app.routes}
    # Core
    assert "/health" in paths
    assert "/sectors" in paths
    assert "/stocks" in paths
    assert "/stocks/{ticker}" in paths
    assert "/stocks/{ticker}/quotes" in paths
    assert "/stocks/{ticker}/financials" in paths
    # News
    assert "/news" in paths
    assert "/news/catalysts" in paths
    assert "/news/stats" in paths
    # Alerts
    assert "/alerts" in paths
    assert "/watchlist" in paths
    assert "/alert-rules" in paths
    # Scoring
    assert "/scoring/weights" in paths
    assert "/scoring/breakdown/{ticker}" in paths
    assert "/scoring/backtest" in paths


# ============================================================
# Trading hours guard
# ============================================================

def test_trading_hours():
    from app.scheduler import is_trading_hours
    from datetime import datetime
    from unittest.mock import patch

    # Saturday
    with patch("app.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 9, 12, 0)
        assert is_trading_hours() is False

    # Monday 10:00 (open)
    with patch("app.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 11, 10, 0)
        assert is_trading_hours() is True

    # Monday 12:00 (lunch)
    with patch("app.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 11, 12, 0)
        assert is_trading_hours() is False

    # Monday 14:00 (open afternoon)
    with patch("app.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 11, 14, 0)
        assert is_trading_hours() is True

    # Monday 15:30 (closed)
    with patch("app.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 11, 15, 30)
        assert is_trading_hours() is False
