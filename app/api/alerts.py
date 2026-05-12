"""Alert + watchlist API endpoints."""
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query, Body
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func
from pydantic import BaseModel, ConfigDict
from loguru import logger

from app.database import get_db, SessionLocal
from app.models import Alert, AlertRule, Watchlist, Stock
from app.alerts import AlertEngine

router = APIRouter(tags=["alerts"])


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ticker: str
    alert_type: str
    severity: str
    title: str
    message: Optional[str] = None
    payload: Optional[dict] = None
    triggered_at: datetime
    acknowledged: bool


class AlertRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    rule_type: str
    params: Optional[dict] = None
    enabled: bool
    severity: str
    description: Optional[str] = None


class WatchlistAdd(BaseModel):
    ticker: str
    notes: Optional[str] = None


class WatchlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ticker: str
    added_at: datetime
    notes: Optional[str] = None


# ====== Alerts ======

@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    ticker: Optional[str] = Query(None),
    severity: Optional[str] = Query(None, regex="^(high|medium|low)$"),
    alert_type: Optional[str] = Query(None),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(50, ge=1, le=200),
    only_unack: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Get recent alerts. Used by frontend alert feed."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    stmt = select(Alert).where(Alert.triggered_at >= cutoff)

    if ticker:
        stmt = stmt.where(Alert.ticker == ticker.upper())
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    if alert_type:
        stmt = stmt.where(Alert.alert_type == alert_type)
    if only_unack:
        stmt = stmt.where(Alert.acknowledged == False)

    stmt = stmt.order_by(desc(Alert.triggered_at)).limit(limit)
    return db.execute(stmt).scalars().all()


@router.post("/alerts/{alert_id}/ack", status_code=200)
def acknowledge_alert(alert_id: int, db: Session = Depends(get_db)):
    """Mark alert as acknowledged."""
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert.acknowledged = True
    db.commit()
    return {"status": "ok"}


@router.post("/alerts/evaluate", status_code=202)
def trigger_evaluation(background: BackgroundTasks):
    """Trigger alert engine to run all rules. Returns immediately."""
    def do_eval():
        db = SessionLocal()
        try:
            engine = AlertEngine(db)
            stats = engine.evaluate_all()
            logger.info(f"Alert eval: {stats}")
        finally:
            db.close()

    background.add_task(do_eval)
    return {"message": "Alert evaluation started"}


@router.get("/alerts/stats")
def alerts_stats(db: Session = Depends(get_db)):
    """Stats về alerts."""
    last_24h = datetime.utcnow() - timedelta(hours=24)
    last_7d = datetime.utcnow() - timedelta(days=7)

    return {
        "alerts_24h": db.scalar(select(func.count(Alert.id)).where(Alert.triggered_at >= last_24h)) or 0,
        "alerts_7d": db.scalar(select(func.count(Alert.id)).where(Alert.triggered_at >= last_7d)) or 0,
        "unacknowledged": db.scalar(select(func.count(Alert.id)).where(Alert.acknowledged == False)) or 0,
        "high_severity_24h": db.scalar(
            select(func.count(Alert.id)).where(
                Alert.triggered_at >= last_24h, Alert.severity == "high"
            )
        ) or 0,
    }


# ====== Alert rules management ======

@router.get("/alert-rules", response_model=list[AlertRuleOut])
def list_rules(db: Session = Depends(get_db)):
    """List all alert rules."""
    rows = db.execute(select(AlertRule).order_by(AlertRule.id)).scalars().all()
    return rows


@router.patch("/alert-rules/{rule_id}")
def update_rule(
    rule_id: int,
    enabled: Optional[bool] = Body(None),
    params: Optional[dict] = Body(None),
    db: Session = Depends(get_db),
):
    """Update rule config (enable/disable, change thresholds)."""
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    if enabled is not None:
        rule.enabled = enabled
    if params is not None:
        rule.params = params
    db.commit()
    return {"status": "ok", "rule_id": rule.id}


# ====== Watchlist ======

@router.get("/watchlist", response_model=list[WatchlistOut])
def list_watchlist(
    user_id: str = Query("default"),
    db: Session = Depends(get_db),
):
    """Get user's watchlist."""
    rows = db.execute(
        select(Watchlist).where(Watchlist.user_id == user_id)
        .order_by(desc(Watchlist.added_at))
    ).scalars().all()
    return rows


@router.post("/watchlist", response_model=WatchlistOut)
def add_to_watchlist(
    payload: WatchlistAdd,
    user_id: str = Query("default"),
    db: Session = Depends(get_db),
):
    """Add ticker to watchlist. Returns 200 if already exists."""
    ticker = payload.ticker.upper()
    if not db.get(Stock, ticker):
        raise HTTPException(404, f"{ticker} chưa được track. Gọi POST /stocks/{ticker}/refresh trước.")

    existing = db.execute(
        select(Watchlist).where(Watchlist.user_id == user_id, Watchlist.ticker == ticker)
    ).scalar_one_or_none()

    if existing:
        if payload.notes:
            existing.notes = payload.notes
            db.commit()
        return existing

    item = Watchlist(user_id=user_id, ticker=ticker, notes=payload.notes)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/watchlist/{ticker}", status_code=204)
def remove_from_watchlist(
    ticker: str,
    user_id: str = Query("default"),
    db: Session = Depends(get_db),
):
    """Remove from watchlist."""
    ticker = ticker.upper()
    item = db.execute(
        select(Watchlist).where(Watchlist.user_id == user_id, Watchlist.ticker == ticker)
    ).scalar_one_or_none()
    if item:
        db.delete(item)
        db.commit()
    return None
