"""Scoring Engine — chấm điểm cổ phiếu trên 6 yếu tố cơ bản.

Công thức cuối:
    score = Σ (factor_value × weight)

Mỗi factor được normalize về thang 0-100 bằng các quy tắc cụ thể.
Trọng số có thể chỉnh trong config; mặc định dựa trên kinh nghiệm
phân tích cơ bản (Buffett-style + Vietnam market context).

Mỗi yếu tố trả về:
    FactorScore(value=0-100, raw=giá trị gốc, weight=trọng số, explain=lý do)

Toàn bộ kết quả lưu vào LatestMetric.score_components dưới dạng JSON
để frontend hiển thị breakdown.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
import json
import math
from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func
from loguru import logger

from app.models import Stock, Quote, FinancialQuarter, LatestMetric, ForeignTrade


# ============================================================
# Configuration: trọng số 6 yếu tố
# ============================================================

WEIGHTS = {
    "eps_growth":       0.25,  # Tăng trưởng EPS YoY
    "profitability":    0.20,  # ROE, biên LN
    "valuation":        0.15,  # PE, PB so với median ngành
    "momentum":         0.15,  # Đà giá + volume
    "money_flow":       0.15,  # Khối ngoại + volume spike
    "financial_health": 0.10,  # Tỷ lệ nợ, margin trend
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 0.001, "Weights must sum to 1.0"


@dataclass
class FactorScore:
    """Kết quả chấm điểm cho 1 yếu tố."""
    name: str
    value: float        # 0-100 (đã normalize)
    raw: Optional[float] = None  # giá trị gốc trước normalize
    weight: float = 0.0
    explain: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": round(self.value, 1),
            "raw": round(self.raw, 2) if self.raw is not None else None,
            "weight": self.weight,
            "explain": self.explain,
        }


@dataclass
class ScoreResult:
    """Tổng hợp điểm + breakdown."""
    score: float                  # 0-100 (composite)
    factors: list[FactorScore]
    quality_flag: str             # "strong", "neutral", "weak", "insufficient_data"
    confidence: float             # 0-1, dựa vào completeness của dữ liệu

    def to_json(self) -> str:
        return json.dumps({
            "score": round(self.score, 1),
            "quality": self.quality_flag,
            "confidence": round(self.confidence, 2),
            "factors": [f.to_dict() for f in self.factors],
        }, ensure_ascii=False)


# ============================================================
# Helpers
# ============================================================

def _sigmoid_score(x: float, midpoint: float, steepness: float = 0.1) -> float:
    """Sigmoid normalize: midpoint = giá trị cho 50 điểm.
    steepness càng cao = chuyển nhanh quanh midpoint."""
    if x is None:
        return 50.0
    try:
        return 100.0 / (1.0 + math.exp(-steepness * (x - midpoint)))
    except OverflowError:
        return 100.0 if x > midpoint else 0.0


def _linear_score(x: float, low: float, high: float, invert: bool = False) -> float:
    """Linear normalize: x=low → 0, x=high → 100. invert=True đảo ngược."""
    if x is None:
        return 50.0
    if high == low:
        return 50.0
    score = (x - low) / (high - low) * 100
    score = max(0.0, min(100.0, score))
    return 100 - score if invert else score


def _clip(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


# ============================================================
# 6 Factor calculators
# ============================================================

class FactorCalculator:
    """Tính từng yếu tố. Lấy DB session để query thêm context khi cần."""

    def __init__(self, db: Session):
        self.db = db

    # ---------- 1. EPS GROWTH ----------
    def eps_growth(self, ticker: str) -> FactorScore:
        """Đánh giá tốc độ tăng trưởng EPS qua 4-8 quý gần nhất.
        
        Logic:
        - YoY growth của quý gần nhất là chỉ số mạnh nhất
        - Tăng trưởng đều > 1 quý đột biến
        - Penalty nếu có quý lỗ
        """
        fins = self.db.execute(
            select(FinancialQuarter).where(FinancialQuarter.ticker == ticker)
            .order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
            .limit(8)
        ).scalars().all()

        if not fins:
            return FactorScore("eps_growth", 50.0, None, WEIGHTS["eps_growth"],
                              "Thiếu dữ liệu BCTC")

        latest = fins[0]
        if latest.net_income_yoy is None:
            return FactorScore("eps_growth", 50.0, None, WEIGHTS["eps_growth"],
                              "Chưa tính được YoY (cần ít nhất 5 quý)")

        # Penalty nếu có quý lỗ
        has_loss = any(f.net_income and f.net_income < 0 for f in fins[:4])
        loss_penalty = 15 if has_loss else 0

        # Tốc độ tăng trưởng quý gần nhất (sigmoid quanh 20%)
        yoy_score = _sigmoid_score(latest.net_income_yoy, midpoint=20, steepness=0.05)

        # Tính độ ổn định: bao nhiêu trong 4 quý gần nhất có YoY > 0
        positive_quarters = sum(1 for f in fins[:4] if f.net_income_yoy and f.net_income_yoy > 0)
        stability_score = (positive_quarters / 4) * 100

        # Trung bình có trọng số: 70% YoY mới nhất + 30% stability
        final = 0.7 * yoy_score + 0.3 * stability_score - loss_penalty
        final = _clip(final)

        explain = f"YoY LNST: {latest.net_income_yoy:+.1f}%, {positive_quarters}/4Q tăng"
        if has_loss:
            explain += " (có quý lỗ)"

        return FactorScore("eps_growth", final, latest.net_income_yoy,
                          WEIGHTS["eps_growth"], explain)

    # ---------- 2. PROFITABILITY ----------
    def profitability(self, ticker: str) -> FactorScore:
        """ROE + biên lợi nhuận. ROE cao + biên ổn định = chất lượng cao.
        
        Benchmark Việt Nam:
        - ROE > 20%: xuất sắc (ngân hàng top, MWG, PNJ, FPT)
        - ROE 15-20%: tốt
        - ROE 10-15%: trung bình
        - ROE < 10%: yếu
        """
        fins = self.db.execute(
            select(FinancialQuarter).where(FinancialQuarter.ticker == ticker)
            .order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
            .limit(4)
        ).scalars().all()

        if not fins:
            return FactorScore("profitability", 50.0, None, WEIGHTS["profitability"],
                              "Thiếu dữ liệu BCTC")

        roes = [f.roe for f in fins if f.roe is not None]
        net_margins = [f.net_margin for f in fins if f.net_margin is not None]

        if not roes:
            return FactorScore("profitability", 50.0, None, WEIGHTS["profitability"],
                              "Thiếu ROE")

        avg_roe = sum(roes) / len(roes)
        # ROE score: sigmoid quanh 15% (midpoint cho 50 điểm)
        roe_score = _sigmoid_score(avg_roe, midpoint=15, steepness=0.15)

        # Margin score
        if net_margins:
            avg_margin = sum(net_margins) / len(net_margins)
            margin_score = _sigmoid_score(avg_margin, midpoint=10, steepness=0.12)
        else:
            avg_margin = None
            margin_score = roe_score  # fallback

        final = 0.7 * roe_score + 0.3 * margin_score
        final = _clip(final)

        explain = f"ROE TB 4Q: {avg_roe:.1f}%"
        if avg_margin is not None:
            explain += f", Biên LN: {avg_margin:.1f}%"

        return FactorScore("profitability", final, avg_roe,
                          WEIGHTS["profitability"], explain)

    # ---------- 3. VALUATION ----------
    def valuation(self, ticker: str) -> FactorScore:
        """PE + PB so với MEDIAN cùng ngành.
        
        Logic:
        - PE thấp hơn median ngành = rẻ → điểm cao
        - PB tương tự
        - Nhưng PE quá thấp (< 5) có thể là bẫy giá trị → trung tính
        - PEG (PE / growth) là yếu tố bổ sung nếu có growth
        """
        metric = self.db.get(LatestMetric, ticker)
        stock = self.db.get(Stock, ticker)
        if not metric or not stock:
            return FactorScore("valuation", 50.0, None, WEIGHTS["valuation"],
                              "Thiếu LatestMetric")

        if metric.pe is None or metric.pe <= 0:
            return FactorScore("valuation", 40.0, metric.pe, WEIGHTS["valuation"],
                              "PE âm hoặc null - cảnh báo")

        # Lấy median PE/PB của ngành (loại bỏ outliers)
        sector_metrics = self.db.execute(
            select(LatestMetric).join(Stock, LatestMetric.ticker == Stock.ticker)
            .where(Stock.sector == stock.sector)
            .where(LatestMetric.pe.isnot(None))
            .where(LatestMetric.pe > 0)
            .where(LatestMetric.pe < 100)  # remove outliers
        ).scalars().all()

        pe_list = sorted([m.pe for m in sector_metrics if m.pe])
        if len(pe_list) >= 3:
            median_pe = pe_list[len(pe_list) // 2]
        else:
            # Fallback: median của toàn thị trường (~14 cho VN)
            median_pe = 14.0

        # PE score: PE / median, lower is better
        # ratio 0.7 (PE rẻ hơn median 30%) → ~80 điểm
        # ratio 1.0 (bằng median) → 50 điểm
        # ratio 1.5 (đắt hơn 50%) → ~20 điểm
        pe_ratio = metric.pe / median_pe
        pe_score = _linear_score(pe_ratio, low=0.5, high=1.8, invert=True)

        # Bẫy giá trị: PE < 5 là cờ đỏ
        if metric.pe < 5:
            pe_score = min(pe_score, 60)  # cap để không quá cao

        # PB score nếu có
        if metric.pb is not None and metric.pb > 0:
            pb_list = sorted([m.pb for m in sector_metrics if m.pb and m.pb > 0])
            median_pb = pb_list[len(pb_list) // 2] if len(pb_list) >= 3 else 1.8
            pb_ratio = metric.pb / median_pb
            pb_score = _linear_score(pb_ratio, low=0.5, high=2.0, invert=True)
            final = 0.65 * pe_score + 0.35 * pb_score
        else:
            final = pe_score

        final = _clip(final)
        explain = f"PE {metric.pe:.1f} vs median ngành {median_pe:.1f}"
        if metric.pb:
            explain += f", PB {metric.pb:.1f}"

        return FactorScore("valuation", final, metric.pe,
                          WEIGHTS["valuation"], explain)

    # ---------- 4. MOMENTUM ----------
    def momentum(self, ticker: str) -> FactorScore:
        """Đà giá: 20D return + tỷ lệ phiên xanh / phiên đỏ.
        
        Khác với phân tích cơ bản thuần, momentum giúp đồng pha entry timing.
        """
        quotes = self.db.execute(
            select(Quote).where(Quote.ticker == ticker)
            .order_by(desc(Quote.date)).limit(60)
        ).scalars().all()

        if len(quotes) < 20:
            return FactorScore("momentum", 50.0, None, WEIGHTS["momentum"],
                              "Thiếu dữ liệu giá (<20 phiên)")

        latest = quotes[0].close
        price_20d = quotes[19].close if len(quotes) >= 20 else quotes[-1].close
        price_60d = quotes[-1].close

        return_20d = (latest - price_20d) / price_20d * 100 if price_20d > 0 else 0
        return_60d = (latest - price_60d) / price_60d * 100 if price_60d > 0 else 0

        # 20D return score: sigmoid quanh 5% (1 tháng tăng 5% là tốt cho VN)
        r20_score = _sigmoid_score(return_20d, midpoint=5, steepness=0.15)
        r60_score = _sigmoid_score(return_60d, midpoint=10, steepness=0.08)

        # Tỷ lệ phiên xanh trong 20 phiên gần nhất
        green_days = 0
        for i in range(min(20, len(quotes) - 1)):
            if quotes[i].close > quotes[i + 1].close:
                green_days += 1
        green_ratio = green_days / min(20, len(quotes) - 1) * 100

        final = 0.5 * r20_score + 0.3 * r60_score + 0.2 * green_ratio
        final = _clip(final)

        explain = f"20D: {return_20d:+.1f}%, 60D: {return_60d:+.1f}%, {green_days}/20 phiên xanh"

        return FactorScore("momentum", final, return_20d,
                          WEIGHTS["momentum"], explain)

    # ---------- 5. MONEY FLOW ----------
    def money_flow(self, ticker: str) -> FactorScore:
        """Khối ngoại mua ròng 5D + volume spike.
        
        Money flow tốt = institutional interest. Riêng VN, khối ngoại
        thường là dòng tiền thông minh.
        """
        # Foreign net 5d
        foreign_5d = self.db.execute(
            select(func.sum(ForeignTrade.net_value)).where(
                ForeignTrade.ticker == ticker,
                ForeignTrade.date >= date.today() - timedelta(days=7),
            )
        ).scalar() or 0.0

        # Volume spike từ LatestMetric
        metric = self.db.get(LatestMetric, ticker)
        volume_spike = metric.volume_spike if metric else 1.0

        # Foreign score: > +50 tỷ rất tốt, > +10 tỷ tốt, < -50 tỷ rất xấu
        # Scale: -100 tỷ → 0, +100 tỷ → 100
        foreign_score = _linear_score(foreign_5d, low=-100, high=100)

        # Volume spike score: 1x = 50 điểm, 2x = 80, 3x+ = 95
        if volume_spike is None:
            volume_score = 50
        else:
            volume_score = _linear_score(volume_spike, low=0.5, high=3.0)

        final = 0.6 * foreign_score + 0.4 * volume_score
        final = _clip(final)

        explain = f"NN 5D: {foreign_5d:+.1f} tỷ, Vol spike: {volume_spike:.1f}x" if volume_spike else f"NN 5D: {foreign_5d:+.1f} tỷ"

        return FactorScore("money_flow", final, foreign_5d,
                          WEIGHTS["money_flow"], explain)

    # ---------- 6. FINANCIAL HEALTH ----------
    def financial_health(self, ticker: str) -> FactorScore:
        """Sức khỏe tài chính: D/E, xu hướng biên lợi nhuận, ổn định doanh thu."""
        fins = self.db.execute(
            select(FinancialQuarter).where(FinancialQuarter.ticker == ticker)
            .order_by(desc(FinancialQuarter.year), desc(FinancialQuarter.quarter))
            .limit(8)
        ).scalars().all()

        if not fins:
            return FactorScore("financial_health", 50.0, None, WEIGHTS["financial_health"],
                              "Thiếu BCTC")

        latest = fins[0]

        # D/E ratio
        de_ratio = None
        if latest.total_debt and latest.total_equity and latest.total_equity > 0:
            de_ratio = latest.total_debt / latest.total_equity
            # D/E < 0.5 rất tốt, 1.0 ổn, > 2.0 cảnh báo
            de_score = _linear_score(de_ratio, low=0.2, high=2.5, invert=True)
        else:
            de_score = 50

        # Margin trend: biên LN có cải thiện không?
        margins = [f.net_margin for f in fins[:4] if f.net_margin is not None]
        if len(margins) >= 2:
            margin_trend = margins[0] - margins[-1]  # latest - oldest in window
            margin_trend_score = _sigmoid_score(margin_trend, midpoint=0, steepness=0.5)
        else:
            margin_trend_score = 50

        # Revenue stability: CV (coefficient of variation) doanh thu
        revenues = [f.revenue for f in fins[:6] if f.revenue]
        if len(revenues) >= 4:
            mean_rev = sum(revenues) / len(revenues)
            std_rev = (sum((r - mean_rev) ** 2 for r in revenues) / len(revenues)) ** 0.5
            cv = (std_rev / mean_rev * 100) if mean_rev > 0 else 100
            # CV thấp = ổn định = điểm cao
            stability_score = _linear_score(cv, low=5, high=40, invert=True)
        else:
            stability_score = 50

        final = 0.4 * de_score + 0.3 * margin_trend_score + 0.3 * stability_score
        final = _clip(final)

        parts = []
        if de_ratio is not None:
            parts.append(f"D/E: {de_ratio:.2f}")
        if len(margins) >= 2:
            parts.append(f"Margin trend: {margin_trend:+.1f}%")
        explain = ", ".join(parts) or "Dữ liệu hạn chế"

        return FactorScore("financial_health", final, de_ratio,
                          WEIGHTS["financial_health"], explain)


# ============================================================
# Main scoring engine
# ============================================================

class ScoringEngine:
    """Orchestrator gọi 6 factor và tổng hợp."""

    def __init__(self, db: Session):
        self.db = db
        self.calc = FactorCalculator(db)

    def score_ticker(self, ticker: str) -> ScoreResult:
        """Chấm điểm 1 mã. Trả về kết quả đầy đủ kèm breakdown."""
        factors = [
            self.calc.eps_growth(ticker),
            self.calc.profitability(ticker),
            self.calc.valuation(ticker),
            self.calc.momentum(ticker),
            self.calc.money_flow(ticker),
            self.calc.financial_health(ticker),
        ]

        # Weighted sum
        weighted_sum = sum(f.value * f.weight for f in factors)

        # Confidence dựa trên có bao nhiêu factor có raw != None
        data_present = sum(1 for f in factors if f.raw is not None)
        confidence = data_present / len(factors)

        # Quality flag
        if confidence < 0.4:
            quality = "insufficient_data"
        elif weighted_sum >= 75:
            quality = "strong"
        elif weighted_sum >= 55:
            quality = "neutral"
        else:
            quality = "weak"

        return ScoreResult(
            score=weighted_sum,
            factors=factors,
            quality_flag=quality,
            confidence=confidence,
        )

    def score_and_save(self, ticker: str) -> Optional[ScoreResult]:
        """Chấm điểm và lưu vào LatestMetric.score + score_components."""
        result = self.score_ticker(ticker)

        metric = self.db.get(LatestMetric, ticker)
        if not metric:
            logger.warning(f"No LatestMetric for {ticker}, cannot save score")
            return result

        metric.score = result.score
        metric.score_components = result.to_json()
        self.db.commit()
        return result

    def score_all(self, tickers: list[str]) -> dict[str, float]:
        """Chấm điểm hàng loạt. Trả về dict ticker → score."""
        results = {}
        for t in tickers:
            try:
                r = self.score_and_save(t)
                if r:
                    results[t] = r.score
            except Exception as e:
                logger.exception(f"Failed to score {t}: {e}")
        return results
