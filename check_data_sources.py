"""Pre-deploy diagnostic.

Chạy script này LOCAL trước khi deploy lên cloud để confirm:
1. vnstock có lấy được giá + BCTC thật không
2. FireAnt API có hoạt động không
3. CafeF có scrape được không
4. LLM API key có valid không

Cách dùng:
    pip install vnstock httpx loguru
    python check_data_sources.py

Hoặc test 1 nguồn cụ thể:
    python check_data_sources.py --only vnstock
    python check_data_sources.py --only fireant
    python check_data_sources.py --only cafef
    python check_data_sources.py --only llm

Sẽ in ra TICK ✅ hoặc CROSS ❌ + lý do cho từng nguồn,
để anh quyết định có deploy được chưa.
"""
import sys
import os
import argparse
import asyncio
import traceback
from datetime import date, timedelta


def section(title):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def ok(msg):
    print(f"  ✅ {msg}")


def fail(msg, detail=""):
    print(f"  ❌ {msg}")
    if detail:
        print(f"     → {detail}")


def warn(msg):
    print(f"  ⚠️  {msg}")


# ================================================================
# Test 1: vnstock — core dependency
# ================================================================
def test_vnstock():
    section("Test 1: vnstock (giá + BCTC)")
    try:
        from vnstock import Vnstock
    except ImportError as e:
        fail("vnstock chưa được cài", "pip install vnstock")
        return False

    ok(f"vnstock imported successfully")

    test_ticker = "FPT"
    sources_to_try = ["VCI", "TCBS", "MSN"]
    working_source = None

    for source in sources_to_try:
        print(f"\n  Trying source: {source}")
        try:
            stock = Vnstock().stock(symbol=test_ticker, source=source)
        except Exception as e:
            fail(f"  {source}: cannot create stock object", str(e)[:100])
            continue

        # Test 1a: company info
        try:
            df = stock.company.overview()
            if df is not None and not df.empty:
                row = df.iloc[0]
                name = row.get("companyName") or row.get("short_name") or "?"
                ok(f"  {source}: company info OK — {test_ticker} = {str(name)[:50]}")
                working_source = source
            else:
                warn(f"  {source}: company.overview() returned empty")
        except Exception as e:
            fail(f"  {source}: company.overview() failed", str(e)[:100])
            continue

        # Test 1b: quote history
        try:
            end = date.today()
            start = end - timedelta(days=10)
            df = stock.quote.history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1D",
            )
            if df is not None and not df.empty:
                latest_close = df.iloc[-1].get("close")
                ok(f"  {source}: {len(df)} OHLCV bars, latest close = {latest_close}")
            else:
                warn(f"  {source}: quote.history() returned empty")
        except Exception as e:
            fail(f"  {source}: quote.history() failed", str(e)[:100])

        # Test 1c: financials
        try:
            df = stock.finance.income_statement(period="quarter", lang="en")
            if df is not None and not df.empty:
                ok(f"  {source}: {len(df)} quarters of income statement available")
                # Show available columns - useful for debugging schema drift
                cols = list(df.columns)[:8]
                print(f"     Columns: {cols}...")
            else:
                warn(f"  {source}: income_statement empty")
        except Exception as e:
            fail(f"  {source}: income_statement failed", str(e)[:100])

        if working_source:
            break

    if working_source:
        print(f"\n  ✅ vnstock works with source: {working_source}")
        print(f"     → Set VNSTOCK_SOURCE={working_source} in .env")
        return True
    else:
        print(f"\n  ❌ vnstock failed on all sources")
        print(f"     → Possible causes: vnstock outdated, network blocked, source down")
        print(f"     → Try: pip install --upgrade vnstock")
        return False


# ================================================================
# Test 2: FireAnt API
# ================================================================
async def test_fireant():
    section("Test 2: FireAnt API (tin tức backup)")
    try:
        import httpx
    except ImportError:
        fail("httpx chưa được cài", "pip install httpx")
        return False

    url = "https://fireant.vn/api/Data/Markets/News"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
        "Referer": "https://fireant.vn/",
    }
    params = {"symbol": "FPT", "offset": 0, "limit": 5}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=headers)

        if resp.status_code != 200:
            fail(f"FireAnt returned HTTP {resp.status_code}", resp.text[:200])
            return False

        try:
            data = resp.json()
        except Exception:
            fail("FireAnt response không phải JSON", resp.text[:200])
            return False

        if not isinstance(data, list):
            fail(f"FireAnt trả về kiểu lạ: {type(data)}", str(data)[:200])
            return False

        ok(f"FireAnt API works: {len(data)} articles for FPT")
        if data:
            first = data[0]
            title = first.get("title", "?")[:80]
            print(f"     Sample: {title}")
            # Check fields chúng ta dùng
            has_fields = {
                "title": "title" in first,
                "description": "description" in first,
                "content": "content" in first or "originalURL" in first,
                "date": "date" in first or "postDate" in first,
            }
            for k, v in has_fields.items():
                if v:
                    ok(f"  Has field: {k}")
                else:
                    warn(f"  Missing field: {k}")
        return True

    except httpx.HTTPError as e:
        fail("FireAnt network error", str(e))
        return False
    except Exception as e:
        fail("FireAnt unexpected error", str(e))
        return False


# ================================================================
# Test 3: CafeF scraper
# ================================================================
async def test_cafef():
    section("Test 3: CafeF.vn HTML scraper")
    try:
        import httpx
    except ImportError:
        fail("httpx chưa cài")
        return False

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9",
    }
    search_url = "https://cafef.vn/tim-kiem.chn?keywords=FPT"

    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
            resp = await client.get(search_url)

        if resp.status_code == 200:
            ok(f"CafeF search responded OK ({len(resp.text):,} bytes)")
            import re
            urls = re.findall(r'href="(/[^"]+\.chn)"', resp.text)
            article_urls = [u for u in urls if "tim-kiem" not in u and "lich-su" not in u]
            ok(f"  Found {len(article_urls)} article URL candidates")

            # Try fetching 1 article
            if article_urls:
                article_url = f"https://cafef.vn{article_urls[0]}"
                resp2 = await client.get(article_url)
                if resp2.status_code == 200:
                    has_title = bool(re.search(r'<h1[^>]*>', resp2.text))
                    has_content = bool(re.search(r'(?:mainContent|detail-content)', resp2.text))
                    if has_title and has_content:
                        ok(f"  Article page parseable: {article_url[:80]}")
                        return True
                    else:
                        warn(f"  Article page structure unexpected - selectors may need update")
                        return True  # still OK, just need to update selectors
                else:
                    warn(f"  Article fetch returned {resp2.status_code}")
                    return False
            else:
                warn("Search returned no article URLs - selectors may need update")
                return False

        elif resp.status_code in (403, 429, 503):
            fail(f"CafeF blocked us (HTTP {resp.status_code})",
                 "Anti-bot fired. Có thể fallback sang FireAnt 100%.")
            return False
        else:
            fail(f"CafeF returned HTTP {resp.status_code}")
            return False

    except Exception as e:
        fail("CafeF error", str(e))
        return False


# ================================================================
# Test 4: LLM API key
# ================================================================
async def test_llm():
    section("Test 4: LLM API (Claude / OpenAI)")
    try:
        import httpx
    except ImportError:
        fail("httpx chưa cài")
        return False

    claude_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not claude_key and not openai_key:
        warn("Không có ANTHROPIC_API_KEY hoặc OPENAI_API_KEY")
        warn("App vẫn chạy với fallback rule-based, nhưng news summarization chất lượng thấp")
        warn("Để có chất lượng cao: export ANTHROPIC_API_KEY=sk-ant-...")
        return None  # not a hard fail

    if claude_key:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": claude_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 50,
                        "messages": [{"role": "user", "content": "Trả lời 1 từ: OK"}],
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", [{}])[0].get("text", "")
                ok(f"Claude API works: response = '{content[:50]}'")
                return True
            else:
                fail(f"Claude returned HTTP {resp.status_code}", resp.text[:200])
                if resp.status_code == 401:
                    print("     → API key sai hoặc hết hạn")
                elif resp.status_code == 429:
                    print("     → Rate limited")
                return False
        except Exception as e:
            fail("Claude API error", str(e))
            return False

    if openai_key:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {openai_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "max_tokens": 50,
                        "messages": [{"role": "user", "content": "Trả lời 1 từ: OK"}],
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                ok(f"OpenAI API works: response = '{content[:50]}'")
                return True
            else:
                fail(f"OpenAI returned HTTP {resp.status_code}", resp.text[:200])
                return False
        except Exception as e:
            fail("OpenAI API error", str(e))
            return False


# ================================================================
# Main
# ================================================================
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["vnstock", "fireant", "cafef", "llm", "all"],
                        default="all", help="Test 1 nguồn cụ thể")
    args = parser.parse_args()

    print()
    print("┌─────────────────────────────────────────────────────┐")
    print("│   FundCatalyst — Pre-deploy diagnostic              │")
    print("│   Kiểm tra các nguồn dữ liệu thật                   │")
    print("└─────────────────────────────────────────────────────┘")

    results = {}

    if args.only in ("all", "vnstock"):
        results["vnstock"] = test_vnstock()
    if args.only in ("all", "fireant"):
        results["fireant"] = await test_fireant()
    if args.only in ("all", "cafef"):
        results["cafef"] = await test_cafef()
    if args.only in ("all", "llm"):
        results["llm"] = await test_llm()

    # Summary
    section("KẾT LUẬN")

    critical = ["vnstock"]
    nice_to_have = ["fireant", "cafef", "llm"]

    for k in critical:
        if k in results:
            status = "✅ OK" if results[k] else "❌ FAIL (BẮT BUỘC FIX)"
            print(f"  {k:12s} {status}")

    for k in nice_to_have:
        if k in results:
            if results[k] is None:
                print(f"  {k:12s} ⚠️  SKIP (optional)")
            else:
                status = "✅ OK" if results[k] else "❌ FAIL (có fallback)"
                print(f"  {k:12s} {status}")

    print()
    if results.get("vnstock"):
        print("  → vnstock hoạt động → CÓ THỂ DEPLOY")
    else:
        print("  → vnstock KHÔNG hoạt động → CHƯA DEPLOY ĐƯỢC")
        print("     Fix vnstock trước khi deploy, hoặc dùng nguồn data khác.")

    print()


if __name__ == "__main__":
    asyncio.run(main())
