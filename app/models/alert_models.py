"""Alert engine database models."""
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey,
    UniqueConstraint, Index, BigInteger, JSON
)
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class AlertRule(Base):
    """Rule definitions cho alert engine.
    
    Mỗi rule có loại + ngưỡng cụ thể. Vd:
    - type=price_in_buy_zone, params={"buffer_pct": 1.0}
    - type=volume_spike, params={"threshold": 2.0}
    - type=score_change, params={"min_delta": 5}
    - type=foreign_net_buy, params={"min_value_billion": 50}
    - type=earnings_surprise, params={"min_yoy_pct": 30}
    """
    __tablename__ = "alert_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    rule_type = Column(String(50), nullable=False, index=True)
    params = Column(JSON, default=dict)  # rule-specific config
    enabled = Column(Boolean, default=True)
    severity = Column(String(20), default="medium")  # high, medium, low
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Alert(Base):
    """Triggered alerts. Append-only audit log + feed for users."""
    __tablename__ = "alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False, index=True)
    rule_id = Column(Integer, ForeignKey("alert_rules.id", ondelete="SET NULL"))
    
    alert_type = Column(String(50), nullable=False, index=True)
    severity = Column(String(20), default="medium")
    title = Column(String(200), nullable=False)
    message = Column(Text)
    payload = Column(JSON)  # context data: { price, volume, score, ... }
    
    triggered_at = Column(DateTime, default=datetime.utcnow, index=True)
    acknowledged = Column(Boolean, default=False)
    
    __table_args__ = (
        Index("ix_alert_ticker_triggered", "ticker", "triggered_at"),
        # Dedup: same ticker + type within short window won't fire twice (handled in code)
    )


class Watchlist(Base):
    """User watchlists. Simple single-user MVP — extend with user_id when adding auth."""
    __tablename__ = "watchlists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), default="default", index=True)  # placeholder for multi-user
    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)

    __table_args__ = (
        UniqueConstraint("user_id", "ticker", name="uq_watchlist_user_ticker"),
        Index("ix_watchlist_user", "user_id"),
    )
