# FundCatalyst VN — Backend

Backend API hoàn chỉnh cho app phân tích chứng khoán cơ bản Việt Nam. **Sản phẩm thật, không phải demo.**

## Có gì

| Module | Tóm tắt |
|---|---|
| **vnstock integration** | Lấy giá thật + BCTC từ VCI/TCBS/MSN. Retry, error handling, schema normalization. |
| **Scoring Engine** | Composite score 0-100 trên 6 yếu tố: EPS growth, ROE/biên LN, định giá vs ngành, momentum, dòng tiền NN, sức khỏe tài chính. Trọng số có thể chỉnh. |
| **Backtest** | Rolling backtest đo alpha của top-decile vs benchmark equal-weight. |
| **News Scraper** | Crawl tin từ CafeF + FireAnt với rate limit, retry, dedup theo URL. |
| **LLM Summarizer** | Claude/OpenAI tóm tắt tin tiếng Việt, phát hiện catalyst, phân loại sentiment + importance. Có fallback rule-based nếu không có API key. |
| **Alert Engine** | 7 rule types (price spike, volume spike, foreign flow, score change, catalyst mới, earnings surprise...) với dedup 2h. |
| **Scheduler** | 6 background jobs tự chạy: quote refresh 5p/lần, alert eval 10p/lần, news 30p/lần, LLM 15p/lần, full refresh + rescore hàng ngày sau 15:30. |
| **REST API** | 25+ endpoints, OpenAPI docs auto tại `/docs`. |
| **Watchlist** | Lưu watchlist server-side, sync với frontend. |

## Chạy nhanh (Docker)

```bash
cd fundcatalyst-backend

# (Tuỳ chọn) Set LLM key để có news summarization chất lượng
export ANTHROPIC_API_KEY="sk-ant-..."
# hoặc: export OPENAI_API_KEY="sk-..."

# Build & start
docker compose up -d --build

# Load dữ liệu lần đầu (mất 5-10 phút)
docker compose exec api python scripts/bootstrap.py

# Skip news nếu muốn nhanh hơn
docker compose exec api python scripts/bootstrap.py --skip-news

# Mở docs
open http://localhost:8000/docs
```

## Chạy local (không Docker)

```bash
# 1. Postgres + Redis (macOS)
brew install postgresql@16 redis
brew services start postgresql@16 redis
createdb fundcatalyst

# 2. Python env
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Config
cp .env.example .env
# Edit .env, set ANTHROPIC_API_KEY hoặc OPENAI_API_KEY nếu muốn LLM thật

# 4. Load data
python scripts/bootstrap.py

# 5. Run
uvicorn app.main:app --reload
```

## API Endpoints (25+)

### Core
- `GET /` – API info
- `GET /health` – Health check + freshness
- `GET /sectors` – Heatmap ngành
- `GET /jobs` – Lịch sử background jobs

### Stocks
- `GET /stocks?min_score=70&sector=Công nghệ&sort=score` – Dashboard list
- `GET /stocks/{ticker}` – Chi tiết đầy đủ (giá + BCTC + score)
- `GET /stocks/{ticker}/quotes?days=90` – OHLCV history
- `GET /stocks/{ticker}/financials?n_quarters=8` – BCTC quý
- `POST /stocks/{ticker}/refresh?full=true` – Force refresh
- `POST /admin/refresh-all` – Refresh toàn bộ tickers
- `POST /admin/refresh-quotes` – Refresh chỉ giá

### Scoring
- `GET /scoring/weights` – Trọng số 6 yếu tố
- `GET /scoring/breakdown/{ticker}` – Chi tiết điểm số kèm explain mỗi factor
- `POST /scoring/rescore-all` – Tính lại tất cả tickers
- `POST /scoring/backtest` – Rolling backtest

### News & Catalysts
- `GET /news?ticker=FPT&sentiment=positive&days=7` – List tin (filtered)
- `GET /news/catalysts?ticker=FPT&impact=bullish` – Catalysts đã phát hiện
- `POST /news/ingest/{ticker}` – Trigger crawl + LLM cho 1 mã
- `POST /news/process-pending` – Run LLM cho bài chưa xử lý
- `GET /news/stats` – Pipeline stats

### Alerts
- `GET /alerts?hours=24&severity=high` – Recent alerts (cho feed)
- `POST /alerts/{id}/ack` – Đánh dấu đã xem
- `POST /alerts/evaluate` – Trigger engine chạy ngay
- `GET /alerts/stats` – Alert stats
- `GET /alert-rules` – List rules
- `PATCH /alert-rules/{id}` – Bật/tắt hoặc đổi ngưỡng

### Watchlist
- `GET /watchlist?user_id=default` – Get watchlist
- `POST /watchlist` – Add ticker
- `DELETE /watchlist/{ticker}` – Remove

## Ví dụ

```bash
# Top 10 mã điểm cao nhất, kèm breakdown
curl 'http://localhost:8000/stocks?min_score=70&limit=10' | jq

# Chi tiết FPT với 90 ngày giá + 8 quý BCTC
curl http://localhost:8000/stocks/FPT | jq

# Tại sao FPT được điểm cao?
curl http://localhost:8000/scoring/breakdown/FPT | jq

# Trọng số scoring hiện tại
curl http://localhost:8000/scoring/weights | jq

# Backtest 6 tháng gần nhất, hold 60 ngày
curl -X POST http://localhost:8000/scoring/backtest \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2025-11-01","hold_days":60,"rebalance_days":30}' | jq

# Tin tức tích cực về FPT 7 ngày qua
curl 'http://localhost:8000/news?ticker=FPT&sentiment=positive&days=7' | jq

# Catalysts bullish toàn thị trường
curl 'http://localhost:8000/news/catalysts?impact=bullish&limit=20' | jq

# Alerts mức cao trong 24h qua
curl 'http://localhost:8000/alerts?severity=high&hours=24' | jq

# Thêm HPG vào watchlist
curl -X POST http://localhost:8000/watchlist \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"HPG","notes":"Watch for Dung Quat 2"}'

# Force refresh tin tức cho FPT (crawl + LLM)
curl -X POST http://localhost:8000/news/ingest/FPT?max_articles=10
```

## Kết nối với frontend HTML

Mở `fundcatalyst-vn.html`, thêm 2 dòng này TRƯỚC `</body>`:

```html
<script>window.FCVN_API_URL = 'http://localhost:8000';</script>
<script src="frontend-integration.js"></script>
```

`frontend-integration.js` (kèm theo) sẽ:
- ✅ Fetch giá thật từ `/stocks`
- ✅ Fetch catalysts từ `/news/catalysts` → hiển thị trong card detail
- ✅ Fetch alerts realtime từ `/alerts` → feed cảnh báo
- ✅ Fetch news từ `/news` → tab tin tức
- ✅ Sync watchlist với `/watchlist`
- ✅ Score breakdown trong detail từ `/scoring/breakdown/{ticker}`
- ✅ Auto-refresh mỗi 5 phút trong giờ giao dịch VN
- ✅ Fallback về mock data + banner cảnh báo nếu backend down

## Scoring Engine — Công thức

```
score = Σ (factor.value × factor.weight)
```

| Factor | Weight | Logic |
|---|---|---|
| EPS Growth | 25% | 70% YoY LNST quý gần + 30% ổn định 4Q. Penalty 15đ nếu có quý lỗ. Sigmoid quanh +20%. |
| Profitability | 20% | 70% ROE TB 4Q + 30% biên LNST. Sigmoid quanh ROE 15% và margin 10%. |
| Valuation | 15% | 65% PE vs MEDIAN NGÀNH + 35% PB vs median. Linear: rẻ hơn 30% → 80đ, đắt hơn 50% → 20đ. Cap nếu PE<5 (bẫy giá trị). |
| Momentum | 15% | 50% return 20D + 30% return 60D + 20% tỷ lệ phiên xanh. Sigmoid quanh 5%/20D. |
| Money Flow | 15% | 60% NN net buy 5D + 40% volume spike. Linear: ±100 tỷ → 0-100đ. |
| Financial Health | 10% | 40% D/E + 30% margin trend + 30% revenue stability (CV). |

Mỗi factor trả về:
```json
{
  "name": "eps_growth",
  "value": 87.5,
  "raw": 28.5,
  "weight": 0.25,
  "explain": "YoY LNST: +28.5%, 4/4Q tăng"
}
```

Toàn bộ breakdown lưu vào `LatestMetric.score_components` (JSON), expose qua `/scoring/breakdown/{ticker}`.

### Điều chỉnh trọng số

Sửa `app/scoring/engine.py`:
```python
WEIGHTS = {
    "eps_growth":       0.30,  # tăng nếu thị trường ưu tiên growth
    "profitability":    0.20,
    "valuation":        0.20,  # tăng nếu thị trường defensive
    "momentum":         0.10,
    "money_flow":       0.15,
    "financial_health": 0.05,
}
```

Sau khi sửa, chạy:
```bash
curl -X POST http://localhost:8000/scoring/rescore-all
```

## News Pipeline

```
CafeF/FireAnt → Scraper → NewsArticle (raw)
                                ↓
                          LLM Summarizer
                                ↓
                  ┌─────────────┴─────────────┐
                  │                           │
        Update article.summary       Create Catalyst record
        (sentiment, category)        (if is_catalyst=true)
                                              │
                                              ↓
                                       Alert Engine triggers
                                       "new_catalyst" alert
```

LLM prompt schema (see `app/llm/summarizer.py`):
- Output: structured JSON với summary, sentiment, category, importance, is_catalyst, catalyst_type, catalyst_impact
- Model: Claude Haiku 4.5 (default) hoặc GPT-4o-mini
- Fallback: rule-based (không có chất lượng LLM nhưng không crash)

## Alert Engine

Default rules (configurable qua `/alert-rules`):

| Type | Default trigger | Severity |
|---|---|---|
| price_spike | ±5% trong 1 phiên | medium |
| volume_spike | ≥2x trung bình 20 phiên | high |
| foreign_net_buy | NN mua ròng ≥30 tỷ 5D | high |
| foreign_net_sell | NN bán ròng ≥50 tỷ 5D | medium |
| score_change | Δscore ≥8 điểm | medium |
| new_catalyst | LLM phát hiện, confidence ≥0.6 | high |
| earnings_surprise | LNST YoY ≥30% | high |

Dedup: cùng (ticker, type) không fire lại trong 2 giờ.

## Cấu trúc

```
fundcatalyst-backend/
├── app/
│   ├── main.py                          # FastAPI entry
│   ├── config.py                        # Settings (env vars)
│   ├── database.py                      # SQLAlchemy session
│   ├── scheduler.py                     # 6 background jobs
│   ├── models/
│   │   ├── db_models.py                 # Stock, Quote, FinancialQuarter, LatestMetric
│   │   ├── news_models.py               # NewsArticle, NewsMention, Catalyst
│   │   ├── alert_models.py              # Alert, AlertRule, Watchlist
│   │   └── schemas.py                   # Pydantic API schemas
│   ├── services/
│   │   ├── vnstock_service.py           # vnstock client wrapper
│   │   └── ingestion.py                 # Fetch → DB upsert
│   ├── scoring/
│   │   ├── engine.py                    # 6-factor scoring
│   │   └── backtest.py                  # Rolling backtest
│   ├── scrapers/
│   │   ├── cafef.py                     # CafeF.vn scraper
│   │   ├── fireant.py                   # FireAnt JSON endpoint
│   │   └── ingestion.py                 # Orchestrator
│   ├── llm/
│   │   ├── client.py                    # Claude/OpenAI/Fallback
│   │   └── summarizer.py                # News → structured analysis
│   ├── alerts/
│   │   └── engine.py                    # Rule-based alert engine
│   └── api/
│       ├── stocks.py                    # /stocks
│       ├── meta.py                      # /health, /sectors, /admin
│       ├── news.py                      # /news, /news/catalysts
│       ├── alerts.py                    # /alerts, /watchlist
│       └── scoring.py                   # /scoring/*
├── scripts/
│   └── bootstrap.py                     # Initial data load
├── tests/
│   └── test_smoke.py                    # 16+ unit tests
├── docker-compose.yml                   # postgres + redis + api
├── Dockerfile
├── requirements.txt
├── .env.example
└── frontend-integration.js              # Client-side connector
```

## Tests

```bash
pytest tests/ -v

# Expected output:
# test_imports PASSED
# test_config_defaults PASSED
# test_schemas PASSED
# test_growth_calculation PASSED
# test_vnstock_helpers PASSED
# test_scoring_weights_sum_to_one PASSED
# test_scoring_helpers PASSED
# test_factor_score_dataclass PASSED
# test_alert_default_rules PASSED
# test_cafef_ticker_extraction PASSED
# test_cafef_date_parse PASSED
# test_llm_fallback_works_without_api_key PASSED
# test_llm_json_parser PASSED
# test_llm_client_factory PASSED
# test_fastapi_routes PASSED
# test_trading_hours PASSED
```

## Production checklist

- [ ] `ALLOWED_ORIGINS` thay vì `*`
- [ ] `DEBUG=false`
- [ ] Alembic migrations thay `Base.metadata.create_all`
- [ ] Redis job store cho APScheduler khi chạy multi-worker
- [ ] Rate limiting (slowapi) cho API public
- [ ] Authentication (JWT) cho `/admin/*`
- [ ] Logging tới Sentry/Datadog
- [ ] Postgres backup hàng ngày
- [ ] Secret rotation cho LLM API keys
- [ ] CDN cho frontend assets (nếu deploy public)
- [ ] WebSocket cho realtime price tick (thay vì polling 5p/lần)

## Limitations

- **vnstock dependency**: API community-driven, có thể đổi. Wrapper có retry nhưng không cứu được nếu vnstock breaking change.
- **News scraping fragility**: CafeF có thể đổi HTML structure. FireAnt là fallback.
- **Backtest local data**: Cần data lịch sử đầy đủ; bootstrap chỉ load 1 năm. Để có ý nghĩa thống kê, cần ≥3-5 năm.
- **LLM cost**: Mỗi article ~500-800 tokens. Với 30 mã × 5 tin/ngày × 30 phút/lần ≈ 7,200 calls/ngày. Claude Haiku ~$0.05/ngày, GPT-4o-mini ~$0.10/ngày.
- **Single tenant**: User auth chưa có; `user_id="default"` cho watchlist.

## Disclaimer

Thông tin và score chỉ mang tính tham khảo, **không phải lời khuyên đầu tư**. Người dùng tự chịu trách nhiệm với quyết định của mình.
