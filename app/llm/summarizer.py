"""LLM-powered news summarizer + catalyst detector.

Cho mỗi bài tin chưa xử lý:
1. Gọi LLM với structured prompt → trả về JSON
2. Parse: summary, sentiment, category, importance, is_catalyst
3. Update DB
4. Nếu là catalyst → tạo bản ghi Catalyst riêng để frontend hiển thị

Batch processing để tiết kiệm API call.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, or_
from loguru import logger

from app.models import NewsArticle, NewsMention, Catalyst, Stock
from app.llm.client import LLMClient, get_llm_client


SYSTEM_PROMPT = """Bạn là chuyên gia phân tích chứng khoán Việt Nam.
Nhiệm vụ: đọc bài tin và trả về JSON theo schema chính xác. KHÔNG thêm văn bản nào ngoài JSON."""


PROMPT_TEMPLATE = """Phân tích bài tin sau và trả về JSON:

TIÊU ĐỀ: {title}
NỘI DUNG: {content}
MÃ CK LIÊN QUAN: {tickers}

Trả về JSON với schema sau:
{{
  "summary": "2-3 câu tiếng Việt tóm tắt điểm chính ảnh hưởng đến đầu tư",
  "sentiment": "positive" | "negative" | "neutral",
  "sentiment_score": <số từ -1.0 (rất tiêu cực) đến 1.0 (rất tích cực)>,
  "category": "catalyst" | "earnings" | "macro" | "technical" | "noise",
  "importance": <1-5, 5 là quan trọng nhất, ảnh hưởng lớn đến giá>,
  "is_catalyst": <true/false>,
  "catalyst_type": "earnings_beat" | "dividend" | "buyback" | "contract" | "ipo" | "m_and_a" | "expansion" | "regulatory" | "guidance" | null,
  "catalyst_impact": "bullish" | "bearish" | "mixed" | null,
  "catalyst_title": "<tiêu đề ngắn của catalyst, null nếu không phải catalyst>"
}}

CHÚ Ý:
- Catalyst = sự kiện cụ thể có thể đẩy giá lên/xuống (BCTC, hợp đồng, cổ tức, M&A, mở rộng...)
- "noise" = tin chung chung không actionable
- Đánh giá theo góc nhìn nhà đầu tư cơ bản, không phải technical
- Chỉ trả về JSON, không có ```json hay text khác.
"""


class NewsSummarizer:
    """Process unprocessed news through LLM."""

    def __init__(self, db: Session, llm: Optional[LLMClient] = None):
        self.db = db
        self.llm = llm or get_llm_client()

    async def process_unprocessed(self, batch_size: int = 20) -> dict:
        """Process tất cả bài chưa có llm_processed_at. Returns stats."""
        articles = self.db.execute(
            select(NewsArticle).where(
                NewsArticle.llm_processed_at.is_(None),
                NewsArticle.content_text.isnot(None),
            ).limit(batch_size)
        ).scalars().all()

        if not articles:
            return {"processed": 0, "catalysts_created": 0}

        processed = 0
        catalysts_created = 0
        errors = 0

        for article in articles:
            try:
                ok = await self._process_one(article)
                if ok:
                    processed += 1
                    # Check if catalyst was created
                    if article.category == "catalyst":
                        catalysts_created += 1
            except Exception as e:
                errors += 1
                logger.exception(f"Failed to process article {article.id}: {e}")

        return {
            "processed": processed,
            "catalysts_created": catalysts_created,
            "errors": errors,
            "remaining": self._count_unprocessed(),
        }

    async def _process_one(self, article: NewsArticle) -> bool:
        """LLM analyze 1 article. Updates DB in place."""
        # Get tickers mentioned
        mentions = self.db.execute(
            select(NewsMention).where(NewsMention.article_id == article.id)
        ).scalars().all()
        tickers = [m.ticker for m in mentions]

        prompt = PROMPT_TEMPLATE.format(
            title=article.title,
            content=(article.content_text or "")[:2000],
            tickers=", ".join(tickers) or "Chưa rõ",
        )

        response = await self.llm.complete(prompt, system=SYSTEM_PROMPT, max_tokens=800)
        if not response:
            return False

        # Parse JSON (strip markdown if present)
        data = self._parse_json(response)
        if not data:
            logger.warning(f"Could not parse LLM response for article {article.id}: {response[:200]}")
            return False

        # Update article
        article.summary = data.get("summary", "")[:1000]
        article.sentiment = data.get("sentiment", "neutral")
        article.sentiment_score = self._safe_float(data.get("sentiment_score"))
        article.category = data.get("category", "noise")
        article.importance = self._safe_int(data.get("importance"), default=2)
        article.llm_processed_at = datetime.utcnow()

        # If catalyst → create Catalyst rows
        if data.get("is_catalyst") and data.get("catalyst_title"):
            for ticker in tickers:
                catalyst = Catalyst(
                    ticker=ticker,
                    article_id=article.id,
                    catalyst_type=data.get("catalyst_type", "other"),
                    title=data["catalyst_title"][:500],
                    description=data.get("summary", "")[:1000],
                    impact=data.get("catalyst_impact", "mixed"),
                    confidence=min(1.0, max(0.0, abs(self._safe_float(data.get("sentiment_score")) or 0.5))),
                )
                self.db.add(catalyst)

        self.db.commit()
        return True

    @staticmethod
    def _parse_json(response: str) -> Optional[dict]:
        """Robust JSON parse - strips markdown fences, fixes common issues."""
        if not response:
            return None
        # Strip ```json ... ``` if present
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
        if m:
            response = m.group(1)
        else:
            # Find first { to last }
            start = response.find("{")
            end = response.rfind("}")
            if start != -1 and end > start:
                response = response[start:end + 1]
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _safe_float(v) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _safe_int(v, default: int = 0) -> int:
        try:
            return int(v) if v is not None else default
        except (ValueError, TypeError):
            return default

    def _count_unprocessed(self) -> int:
        from sqlalchemy import func
        return self.db.scalar(
            select(func.count(NewsArticle.id))
            .where(NewsArticle.llm_processed_at.is_(None))
        ) or 0
