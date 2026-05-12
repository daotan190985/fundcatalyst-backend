# Deploy lên Cloud - Hướng dẫn chi tiết

Em recommend **Railway** vì:
- Cài đặt nhanh nhất (5 phút)
- Tự deploy Postgres + Redis + API
- ~$5-10/tháng cho project này
- Có Singapore region → latency thấp cho user VN
- Free $5 credit để thử

## ⚠️ TRƯỚC KHI DEPLOY - CHẠY DIAGNOSTIC

App phụ thuộc vào `vnstock` để lấy dữ liệu thật. Library này thuê người scrape, **API có thể đã đổi**. Bắt buộc test trước:

```bash
# Local Mac/Linux
cd fundcatalyst-backend
python3 -m venv venv && source venv/bin/activate
pip install vnstock httpx loguru
python check_data_sources.py
```

Kết quả mong đợi:
```
✅ vnstock        OK
✅ fireant        OK
✅ cafef          OK (hoặc ❌ nhưng có fallback)
⚠️  llm           SKIP (nếu chưa có API key)

→ vnstock hoạt động → CÓ THỂ DEPLOY
```

Nếu `vnstock ❌`:
- `pip install --upgrade vnstock`
- Hoặc nhắn em fix code để dùng API khác (entrade-api, ssi-fast-connect)

---

# Option 1: Railway (RECOMMENDED)

## Bước 1: Đăng ký Railway

1. Vào https://railway.com
2. Sign in with GitHub
3. Verify email
4. Free $5 credit cho tài khoản mới (đủ chạy 1 tháng cho project này)

## Bước 2: Push code lên GitHub

```bash
cd fundcatalyst-backend
git init
git add .
git commit -m "Initial deploy"

# Tạo repo trên github.com (private được), rồi:
git remote add origin git@github.com:YOUR_USERNAME/fundcatalyst-backend.git
git branch -M main
git push -u origin main
```

## Bước 3: Tạo Project trên Railway

1. Dashboard → **New Project** → **Deploy from GitHub repo**
2. Chọn repo `fundcatalyst-backend`
3. Railway sẽ auto-detect Dockerfile, build và deploy
4. Đợi 5-10 phút cho build xong

## Bước 4: Add Postgres + Redis

Trong cùng project:

1. Click **+ New** → **Database** → **PostgreSQL**
2. Click **+ New** → **Database** → **Redis**

Railway tự tạo 2 service, tự generate connection string.

## Bước 5: Connect DATABASE_URL + REDIS_URL

1. Click vào service API (cái Docker build từ repo)
2. Tab **Variables**
3. Click **+ New Variable**, gõ:
   - `DATABASE_URL` → **Add Reference** → chọn `Postgres.DATABASE_URL`
   - `REDIS_URL` → **Add Reference** → chọn `Redis.REDIS_URL`
4. Thêm các vars khác:
   ```
   TZ=Asia/Ho_Chi_Minh
   ENABLE_SCHEDULER=true
   DEBUG=false
   ANTHROPIC_API_KEY=sk-ant-...   (optional)
   ALLOWED_ORIGINS=["*"]
   ```

5. Service tự redeploy với env vars mới

## Bước 6: Expose Public URL

1. Tab **Settings** → **Networking**
2. Click **Generate Domain** → được URL kiểu `fundcatalyst-api-production.up.railway.app`

## Bước 7: Load Data Lần Đầu

```bash
# Cài Railway CLI
npm i -g @railway/cli
railway login

# Link to project
cd fundcatalyst-backend
railway link

# Run bootstrap (mất 5-10 phút)
railway run python scripts/bootstrap.py

# Hoặc trong dashboard:
# Service → Settings → Custom Start Command (tạm thời):
#   sh -c "python scripts/bootstrap.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT"
# Sau khi bootstrap xong, đổi lại về:
#   uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Bước 8: Test

```bash
curl https://YOUR-URL.up.railway.app/health
# Expect: {"status":"ok",...}

curl https://YOUR-URL.up.railway.app/stocks?limit=5
# Expect: 5 cổ phiếu với giá thật
```

## Bước 9: Connect Frontend

Mở `fundcatalyst-vn.html`, sửa:

```html
<script>
window.FCVN_API_URL = 'https://YOUR-URL.up.railway.app';
</script>
<script src="frontend-integration.js"></script>
```

Frontend HTML có thể deploy lên Cloudflare Pages / Vercel / Netlify miễn phí.

---

# Option 2: Render.com

Giá tương đương Railway, có file `render.yaml` sẵn rồi.

## Bước 1-2: Tương tự Railway

Đăng ký + push code lên GitHub.

## Bước 3: Blueprint Deploy

1. Render dashboard → **New** → **Blueprint**
2. Chọn repo
3. Render đọc `render.yaml` và tự tạo:
   - Web service (API)
   - PostgreSQL database
   - Redis instance
4. Click **Apply**

## Bước 4: Set LLM Key (Optional)

1. Service `fundcatalyst-api` → **Environment**
2. Thêm `ANTHROPIC_API_KEY`

## Bước 5: Bootstrap

```bash
# SSH vào instance (Starter plan trở lên)
# Hoặc dùng Shell trong dashboard
python scripts/bootstrap.py
```

---

# Option 3: Fly.io (cheapest)

```bash
# Cài Fly CLI
curl -L https://fly.io/install.sh | sh

cd fundcatalyst-backend
fly launch  # tạo app + chọn region (sin)
fly postgres create  # PostgreSQL
fly redis create     # Redis (Upstash)

# Set secrets
fly secrets set ANTHROPIC_API_KEY=sk-ant-...

# Deploy
fly deploy

# Bootstrap
fly ssh console -C "python scripts/bootstrap.py"
```

Giá: ~$3-5/tháng cho 256MB instance.

---

# Sau khi Deploy

## Monitor

```bash
# Railway
railway logs

# Render: dashboard → Logs tab

# Fly.io
fly logs
```

Kiểm tra:
```bash
curl https://YOUR-URL/jobs
# Phải thấy các job: quote_refresh, news_ingest... chạy thành công

curl https://YOUR-URL/news/stats
# total_articles tăng dần

curl https://YOUR-URL/alerts?hours=24
# Có alerts mới sau vài giờ chạy
```

## Custom Domain

Mua domain (Namecheap/GoDaddy/...). Trong Railway/Render dashboard:
- Settings → Networking → Custom Domain → `api.fundcatalyst.vn`
- Add CNAME record từ DNS provider trỏ về Railway/Render

## Backup Postgres

**Railway**: Settings → Backups → Enable Daily (free)
**Render**: Tự động daily với plan Standard+
**Fly.io**: `fly postgres backup create`

---

# Chi phí dự kiến

| Provider | Plan | Components | $/tháng |
|---|---|---|---|
| Railway | Hobby | API + PG + Redis | $5-10 |
| Render | Starter | API + PG + Redis | $14 ($7+$7+$0 for free Redis) |
| Fly.io | Hobby | API + PG + Upstash Redis | $3-5 |
| AWS | t4g.small + RDS small | All-in | $25-30 |

**LLM cost** (riêng):
- Claude Haiku 4.5: ~$0.05/ngày = $1.5/tháng cho 30 mã
- GPT-4o-mini: ~$0.10/ngày = $3/tháng

Tổng: **$7-15/tháng cho production-grade backend chạy 24/7.**

---

# Troubleshooting

## Build fails: "vnstock requires Python 3.10+"
Dockerfile đã set Python 3.12, không vấn đề.

## "vnstock can't connect" trong production
- Check log → có thể VCI block IP của Railway/Render
- Switch sang TCBS: set `VNSTOCK_SOURCE=TCBS`
- Hoặc dùng proxy (Railway có service Proxy add-on)

## "FireAnt returned 403"
- Set User-Agent header khác trong `app/scrapers/fireant.py`
- Hoặc dùng residential proxy (BrightData, ScraperAPI)

## CORS error từ frontend
- Set `ALLOWED_ORIGINS=["https://your-frontend-domain.com"]` cụ thể
- KHÔNG dùng `["*"]` ở production

## "Out of memory"
- Default Railway/Render starter có 512MB
- Scheduler + LLM + vnstock load 1 lúc có thể peak ~700MB
- Upgrade lên plan 1GB ($10/tháng)

---

# Kiểm tra dữ liệu THẬT đã chạy chưa

Sau 30 phút deploy + bootstrap:

```bash
# 1. Health check
curl https://YOUR-URL/health
# tickers_tracked: 30 (hoặc số em chọn)
# last_quote_refresh: thời điểm gần đây

# 2. Xem 1 mã cụ thể
curl https://YOUR-URL/stocks/FPT | jq '.price, .score, .pe, .roe_ttm'
# price: số thật (không phải 0)
# score: 0-100
# pe: tỉ lệ thật

# 3. Xem giá lịch sử
curl https://YOUR-URL/stocks/FPT/quotes?days=7 | jq 'length'
# Phải có 5-7 bars (5 ngày giao dịch + 2 cuối tuần)

# 4. Score breakdown chi tiết
curl https://YOUR-URL/scoring/breakdown/FPT | jq
# Phải thấy 6 factors với value + explain

# 5. Catalysts (sau 1-2 giờ)
curl https://YOUR-URL/news/catalysts | jq 'length'
# Phải > 0 sau khi LLM xử lý

# 6. Alerts đang fire
curl https://YOUR-URL/alerts?hours=24 | jq 'length'
# Số tuỳ điều kiện thị trường, nhưng phải > 0 nếu có ticker thay đổi mạnh
```

Nếu tất cả ✅, anh đã có **production app với dữ liệu thời gian thực**.
