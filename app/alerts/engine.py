"""Alert engine.

Định kỳ chạy qua tất cả tickers + active rules để phát hiện sự kiện:
- price_spike: giá tăng/giảm bất thường (> X% trong 1 phiên)
- volume_spike: volume > N lần trung bình 20 phiên
- foreign_net_buy: khối ngoại mua ròng > X tỷ
- score_change: score thay đổi > delta
- new_catalyst: có catalyst mới được LLM phát hiện
- price_in_buy_zone: giá vào vùng mua đề xuất
- earnings_surprise: BCTC vượt YoY > X%

Dedup: cùng (ticker, alert_type) không fire trong 2 giờ.
"""
from __future__ import annotations
from datetime import datetime, timedelta, date
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, and_, func, or_
from loguru import logger

from app.models import (
    Stock, Quote, FinancialQuarter, LatestMetric, ForeignTrade,
    Alert, AlertRule, Catalyst,
)


# ============================================================
# Default rules - seeded into DB on first run
# ============================================================

DEFAULT_RULES = [
    {
        "name": "Giá tăng mạnh trong 1 phiên",
        "rule_type": "price_spike",
        "params": {"min_change_pct": 5.0, "direction": "up"},
        "severity": "medium",
        "description": "Phát hiện khi giá tăng > 5% trong 1 phiên giao dịch.",
    },
    {
        "name": "Volume bất thường",
        "rule_type": "volume_spike",
        "params": {"min_multiplier": 2.0},
        "severity": "high",
        "description": "Volume hôm nay > 2x trung bình 20 phiên gần nhất.",
    },
    {
        "name": "Khối ngoại mua ròng mạnh",
        "rule_type": "foreign_net_buy",
        "params": {"min_value_billion": 30.0, "window_days": 5},
        "severity": "high",
        "description": "Khối ngoại mua ròng > 30 tỷ trong 5 phiên gần nhất.",
    },
    {
        "name": "Khối ngoại bán ròng mạnh",
        "rule_type": "foreign_net_sell",
        "params": {"min_value_billion": 50.0, "window_days": 5},
        "severity": "medium",
        "description": "Khối ngoại bán ròng > 50 tỷ trong 5 phiên gần nhất.",
    },
    {
        "name": "Score thay đổi đáng kể",
        "rule_type": "score_change",
        "params": {"min_delta": 8.0},
        "severity": "medium",
        "description": "Score tăng hoặc giảm > 8 điểm so với phiên trước.",
    },
    {
        "name": "Catalyst mới phát hiện",
        "rule_type": "new_catalyst",
        "params": {"min_confidence": 0.6},
        "severity": "high",
        "description": "LLM phát hiện catalyst mới từ tin tức.",
    },
    {
        "name": "Earnings surprise tích cực",
        "rule_type": "earnings_surprise",
        "params": {"min_yoy_pct": 30.0},
        "severity": "high",
        "description": "LNST tăng > 30% YoY trong quý mới công bố.",
    },
]


class AlertEngine:
    """Engine kiểm tra rules và phát alerts."""

    DEDUP_WINDOW_HOURS = 2

    def __init__(self, db: Session):
        self.db = db

    def seed_default_rules(self):
        """Insert default rules nếu chưa có."""
        for rule_data in DEFAULT_RULES:
            existing = self.db.execute(
                select(AlertRule).where(
                    AlertRule.rule_type == rule_data["rule_type"],
                    AlertRule.name == rule_data["name"]
                )
            ).scalar_one_or_none()
            if not existing:
                self.db.add(AlertRule(**rule_data))
        self.db.commit()
        logger.info(f"Default rules seeded: {len(DEFAULT_RULES)} rules")

    def evaluate_all(self, tickers: Optional[list[str]] = None) -> dict:
        """Chạy tất cả rules cho tất cả tickers active.
        
        Returns: { "alerts_created": N, "rules_run": N, "errors": [...] }
        """
        rules = self.db.execute(
            select(AlertRule).where(AlertRule.enabled == True)
        ).scalars().all()

        if tickers is None:
            tickers = [s.ticker for s in self.db.execute(select(Stock)).scalars().all()]

        stats = {"alerts_created": 0, "rules_run": 0, "errors": []}

        for ticker in tickers:
            for rule in rules:
                try:
                    alert = self._evaluate_rule(rule, ticker)
                    stats["rules_run"] += 1
                    if alert:
                        # Dedup check
                        if not self._is_duplicate(alert):
                            self.db.add(alert)
                            stats["alerts_created"] += 1
                except Exception as e:
                    stats["errors"].append(f"{rule.rule_type}/{ticker}: {e}")
                    logger.exception(f"Rule eval failed {rule.rule_type}/{ticker}: {e}")

        self.db.commit()
        return stats

    def _evaluate_rule(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        """Dispatch to specific handler. Returns Alert if triggered, None otherwise."""
        handler = {
            "price_spike": self._check_price_spike,
            "volume_spike": self._check_volume_spike,
            "foreign_net_buy": self._check_foreign_net_buy,
            "foreign_net_sell": self._check_foreign_net_sell,
            "score_change": self._check_score_change,
            "new_catalyst": self._check_new_catalyst,
            "earnings_surprise": self._check_earnings_surprise,
        }.get(rule.rule_type)

        if not handler:
            return None
        return handler(rule, ticker)

    # ====== Individual rule handlers ======

    def _check_price_spike(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        params = rule.params or {}
        min_change = float(params.get("min_change_pct", 5.0))
        direction = params.get("direction", "both")

        metric = self.db.get(LatestMetric, ticker)
        if not metric or metric.change_pct is None:
            return None

        change = metric.change_pct
        triggered = False
        if direction == "up" and change >= min_change:
            triggered = True
        elif direction == "down" and change <= -min_change:
            triggered = True
        elif direction == "both" and abs(change) >= min_change:
            triggered = True

        if not triggered:
            return None

        dir_text = "tăng mạnh" if change > 0 else "giảm mạnh"
        return Alert(
            ticker=ticker,
            rule_id=rule.id,
            alert_type=rule.rule_type,
            severity=rule.severity,
            title=f"{ticker}: Giá {dir_text} {change:+.2f}%",
            message=f"Giá {ticker} {dir_text} {change:+.2f}% trong phiên gần nhất. "
                    f"Cần xem catalyst hoặc tin tức để hiểu nguyên nhân.",
            payload={"change_pct": change, "price": metric.price},
        )

    def _check_volume_spike(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        params = rule.params or {}
        min_mult = float(params.get("min_multiplier", 2.0))

        metric = self.db.get(LatestMetric, ticker)
        if not metric or metric.volume_spike is None:
            return None

        if metric.volume_spike < min_mult:
            return None

        return Alert(
            ticker=ticker,
            rule_id=rule.id,
            alert_type=rule.rule_type,
            severity=rule.severity,
            title=f"{ticker}: Volume {metric.volume_spike:.1f}x trung bình",
            message=f"Khối lượng giao dịch {ticker} cao gấp {metric.volume_spike:.1f} lần "
                    f"trung bình 20 phiên - dấu hiệu institutional interest.",
            payload={"volume_spike": metric.volume_spike, "volume": metric.volume},
        )

    def _check_foreign_net_buy(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        params = rule.params or {}
        min_value = float(params.get("min_value_billion", 30.0))
        window = int(params.get("window_days", 5))

        net = self.db.execute(
            select(func.sum(ForeignTrade.net_value)).where(
                ForeignTrade.ticker == ticker,
                ForeignTrade.date >= date.today() - timedelta(days=window + 2),
            )
        ).scalar() or 0

        if net < min_value:
            return None

        return Alert(
            ticker=ticker,
            rule_id=rule.id,
            alert_type=rule.rule_type,
            severity=rule.severity,
            title=f"{ticker}: NN mua ròng {net:+.1f} tỷ ({window}D)",
            message=f"Khối ngoại mua ròng {net:+.1f} tỷ {ticker} trong {window} phiên - "
                    f"dòng tiền tổ chức tích cực.",
            payload={"foreign_net": net, "window_days": window},
        )

    def _check_foreign_net_sell(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        params = rule.params or {}
        min_value = float(params.get("min_value_billion", 50.0))
        window = int(params.get("window_days", 5))

        net = self.db.execute(
            select(func.sum(ForeignTrade.net_value)).where(
                ForeignTrade.ticker == ticker,
                ForeignTrade.date >= date.today() - timedelta(days=window + 2),
            )
        ).scalar() or 0

        if net > -min_value:
            return None

        return Alert(
            ticker=ticker,
            rule_id=rule.id,
            alert_type=rule.rule_type,
            severity=rule.severity,
            title=f"{ticker}: NN bán ròng {net:.1f} tỷ ({window}D)",
            message=f"Cảnh báo: Khối ngoại bán ròng {abs(net):.1f} tỷ {ticker} trong {window} phiên.",
            payload={"foreign_net": net, "window_days": window},
        )

    def _check_score_change(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        """So sánh score hiện tại với 24h trước (qua audit log JobRun nếu có)."""
        params = rule.params or {}
        min_delta = float(params.get("min_delta", 8.0))

        # Lấy score gần nhất từ alert payload trong 24h
        prev_alert = self.db.execute(
            select(Alert).where(
                Alert.ticker == ticker,
                Alert.alert_type == "score_snapshot",
                Alert.triggered_at >= datetime.utcnow() - timedelta(days=2)
            ).order_by(desc(Alert.triggered_at)).limit(1)
        ).scalar_one_or_none()

        metric = self.db.get(LatestMetric, ticker)
        if not metric or metric.score is None:
            return None

        # Lưu snapshot hiện tại cho lần sau
        # (Trong production có thể dùng bảng riêng để tracking history)
        # Đây dùng Alert table - không hoàn hảo nhưng tiết kiệm bảng

        if prev_alert and prev_alert.payload:
            prev_score = prev_alert.payload.get("score", metric.score)
            delta = metric.score - prev_score
            if abs(delta) >= min_delta:
                direction = "tăng" if delta > 0 else "giảm"
                return Alert(
                    ticker=ticker,
                    rule_id=rule.id,
                    alert_type=rule.rule_type,
                    severity=rule.severity,
                    title=f"{ticker}: Score {direction} {abs(delta):.1f} điểm",
                    message=f"Score thay đổi từ {prev_score:.0f} → {metric.score:.0f} ({delta:+.1f})",
                    payload={"score": metric.score, "previous_score": prev_score, "delta": delta},
                )

        return None

    def _check_new_catalyst(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        params = rule.params or {}
        min_conf = float(params.get("min_confidence", 0.6))

        recent_catalyst = self.db.execute(
            select(Catalyst).where(
                Catalyst.ticker == ticker,
                Catalyst.detected_at >= datetime.utcnow() - timedelta(hours=6),
                Catalyst.confidence >= min_conf,
            ).order_by(desc(Catalyst.detected_at)).limit(1)
        ).scalar_one_or_none()

        if not recent_catalyst:
            return None

        return Alert(
            ticker=ticker,
            rule_id=rule.id,
            alert_type=rule.rule_type,
            severity=rule.severity,
            title=f"{ticker}: Catalyst mới - {recent_catalyst.catalyst_type}",
            message=recent_catalyst.title,
            payload={
                "catalyst_id": recent_catalyst.id,
                "catalyst_type": recent_catalyst.catalyst_type,
                "impact": recent_catalyst.impact,
                "confidence": recent_catalyst.confidence,
            },
        )

    def _check_earnings_surprise(self, rule: AlertRule, ticker: str) -> Optional[Alert]:
        params = rule.params or {}
        min_yoy = float(params.get("min_yoy_pct", 30.0))

        latest_fin = self.db.execute(
            select(FinancialQuarter).where(FinancialQuarter.ticker == ticker)
            .order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
            .limit(1)
        ).scalar_one_or_none()

        if not latest_fin or latest_fin.net_income_yoy is None:
            return None

        if latest_fin.net_income_yoy < min_yoy:
            return None

        return Alert(
            ticker=ticker,
            rule_id=rule.id,
            alert_type=rule.rule_type,
            severity=rule.severity,
            title=f"{ticker}: LNST Q{latest_fin.quarter}/{latest_fin.year} +{latest_fin.net_income_yoy:.1f}% YoY",
            message=f"Lợi nhuận sau thuế quý gần nhất tăng mạnh +{latest_fin.net_income_yoy:.1f}% so với cùng kỳ năm trước.",
            payload={
                "year": latest_fin.year,
                "quarter": latest_fin.quarter,
                "net_income_yoy": latest_fin.net_income_yoy,
                "revenue_yoy": latest_fin.revenue_yoy,
            },
        )

    def _is_duplicate(self, alert: Alert) -> bool:
        """Check if same alert fired in dedup window."""
        cutoff = datetime.utcnow() - timedelta(hours=self.DEDUP_WINDOW_HOURS)
        existing = self.db.execute(
            select(Alert).where(
                Alert.ticker == alert.ticker,
                Alert.alert_type == alert.alert_type,
                Alert.triggered_at >= cutoff,
            ).limit(1)
        ).scalar_one_or_none()
        return existing is not None
