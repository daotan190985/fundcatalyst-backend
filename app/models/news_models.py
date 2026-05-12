"""News & catalyst tables."""
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Date, Text, ForeignKey,
    UniqueConstraint, Index, BigInteger, Boolean
)
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class NewsArticle(Base):
    """Tin tức được crawl từ CafeF/Vietstock/Fiin."""
    __tablename__ = "news_articles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url = Column(String(500), unique=True, nullable=False)
    title = Column(String(500), nullable=False)
    source = Column(String(50), nullable=False)  # cafef, vietstock, fireant, fiin
    published_at = Column(DateTime, index=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)

    # Original content
    raw_content = Column(Text)  # full HTML/text crawl được
    content_text = Column(Text)  # đã clean

    # LLM analysis (filled after summarization)
    summary = Column(Text)  # 2-3 câu tiếng Việt
    sentiment = Column(String(20))  # positive, negative, neutral
    sentiment_score = Column(Float)  # -1 to 1
    category = Column(String(50))  # catalyst, macro, earnings, technical, noise
    importance = Column(Integer)  # 1-5
    llm_processed_at = Column(DateTime)

    # Index
    __table_args__ = (
        Index("ix_news_published", "published_at"),
        Index("ix_news_source_pub", "source", "published_at"),
    )

    # Many-to-many with stocks via mention table
    mentions = relationship("NewsMention", back_populates="article", cascade="all, delete-orphan")


class NewsMention(Base):
    """Link giữa article và stock (1 article có thể nhắc nhiều mã)."""
    __tablename__ = "news_mentions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    article_id = Column(BigInteger, ForeignKey("news_articles.id", ondelete="CASCADE"), nullable=False)
    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False)
    relevance = Column(Float, default=1.0)  # 0-1, ticker xuất hiện trong title vs content

    article = relationship("NewsArticle", back_populates="mentions")

    __table_args__ = (
        UniqueConstraint("article_id", "ticker", name="uq_news_mention"),
        Index("ix_mention_ticker", "ticker"),
    )


class Catalyst(Base):
    """Catalyst extracted từ news (BCTC vượt dự phóng, trúng thầu, mua lại CP, cổ tức...)."""
    __tablename__ = "catalysts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ticker = Column(String(10), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False, index=True)
    article_id = Column(BigInteger, ForeignKey("news_articles.id", ondelete="SET NULL"), nullable=True)
    
    catalyst_type = Column(String(50))  # earnings_beat, dividend, buyback, contract, ipo, m&a, etc
    title = Column(String(500), nullable=False)
    description = Column(Text)
    impact = Column(String(20))  # bullish, bearish, mixed
    confidence = Column(Float)  # 0-1
    
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)
    valid_until = Column(DateTime)  # khi catalyst hết hiệu lực (vd: ngày ex-dividend)
    
    __table_args__ = (
        Index("ix_catalyst_ticker_detected", "ticker", "detected_at"),
    )
