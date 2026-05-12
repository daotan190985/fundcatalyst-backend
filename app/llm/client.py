"""LLM client abstraction.

Hỗ trợ 2 provider:
- Anthropic Claude (mặc định)
- OpenAI GPT

Config qua biến môi trường:
    LLM_PROVIDER=anthropic|openai
    ANTHROPIC_API_KEY=sk-ant-...
    OPENAI_API_KEY=sk-...
    LLM_MODEL=claude-haiku-4-5-20251001 (tuỳ provider)

Nếu không config gì, fallback về rule-based summarizer (no LLM needed).
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
import os
import json
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


class LLMClient(ABC):
    """Interface cho mọi LLM provider."""

    @abstractmethod
    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1000) -> Optional[str]:
        """Gọi LLM và trả về raw text response."""
        pass


class ClaudeClient(LLMClient):
    """Anthropic Claude API client."""

    DEFAULT_MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.endpoint = "https://api.anthropic.com/v1/messages"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1000) -> Optional[str]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    self.endpoint,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "system": system or "Bạn là trợ lý phân tích chứng khoán Việt Nam.",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                if resp.status_code != 200:
                    logger.error(f"Claude API {resp.status_code}: {resp.text[:200]}")
                    return None
                data = resp.json()
                content = data.get("content", [])
                if not content:
                    return None
                return content[0].get("text", "").strip()
            except Exception as e:
                logger.error(f"Claude call failed: {e}")
                raise


class OpenAIClient(LLMClient):
    """OpenAI GPT API client."""

    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.endpoint = "https://api.openai.com/v1/chat/completions"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1000) -> Optional[str]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "messages": [
                            {"role": "system", "content": system or "Bạn là trợ lý phân tích chứng khoán Việt Nam."},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                if resp.status_code != 200:
                    logger.error(f"OpenAI API {resp.status_code}: {resp.text[:200]}")
                    return None
                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return None
                return choices[0].get("message", {}).get("content", "").strip()
            except Exception as e:
                logger.error(f"OpenAI call failed: {e}")
                raise


class FallbackClient(LLMClient):
    """Rule-based fallback khi không có LLM key.
    
    Output rất đơn giản nhưng giúp app vẫn chạy được mà không cần API key.
    """

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1000) -> Optional[str]:
        # Truncate prompt to first 200 chars as a poor-man's summary
        text = prompt[:300].strip()
        return json.dumps({
            "summary": text[:200] + "...",
            "sentiment": "neutral",
            "sentiment_score": 0.0,
            "category": "noise",
            "importance": 2,
            "is_catalyst": False,
        })


def get_llm_client() -> LLMClient:
    """Factory: chọn client dựa vào env vars.
    
    Ưu tiên: Anthropic > OpenAI > Fallback
    """
    provider = os.environ.get("LLM_PROVIDER", "auto").lower()

    if provider in ("auto", "anthropic"):
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if key:
            model = os.environ.get("LLM_MODEL")
            logger.info(f"LLM: using Claude ({model or ClaudeClient.DEFAULT_MODEL})")
            return ClaudeClient(api_key=key, model=model)

    if provider in ("auto", "openai"):
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if key:
            model = os.environ.get("LLM_MODEL")
            logger.info(f"LLM: using OpenAI ({model or OpenAIClient.DEFAULT_MODEL})")
            return OpenAIClient(api_key=key, model=model)

    logger.warning("LLM: no API key found, using fallback (rule-based). "
                   "Set ANTHROPIC_API_KEY or OPENAI_API_KEY for real summarization.")
    return FallbackClient()
