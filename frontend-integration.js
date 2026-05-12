// FundCatalyst VN - Frontend Integration v2
// Kết nối HTML app với backend FastAPI để có DATA THẬT.
//
// Cách dùng: thêm vào fundcatalyst-vn.html ngay TRƯỚC </body>:
//   <script>window.FCVN_API_URL = 'http://localhost:8000';</script>
//   <script src="frontend-integration.js"></script>

(function() {
  'use strict';

  const API_URL = (window.FCVN_API_URL || 'http://localhost:8000').replace(/\/$/, '');
  const CACHE_TTL_MS = 60_000;
  const USER_ID = 'default';

  // ============ HTTP helpers ============
  const cache = new Map();
  const getCached = (key) => {
    const v = cache.get(key);
    if (!v || Date.now() - v.t > CACHE_TTL_MS) { cache.delete(key); return null; }
    return v.data;
  };
  const setCached = (key, data) => cache.set(key, { data, t: Date.now() });

  async function api(method, path, body = null, timeout = 10000) {
    const cacheKey = method === 'GET' ? `${method} ${path}` : null;
    if (cacheKey) {
      const hit = getCached(cacheKey);
      if (hit) return hit;
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const opts = {
        method,
        signal: ctrl.signal,
        headers: { 'Accept': 'application/json' },
      };
      if (body) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      const res = await fetch(`${API_URL}${path}`, opts);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const ct = res.headers.get('content-type') || '';
      const data = ct.includes('application/json') ? await res.json() : await res.text();
      if (cacheKey) setCached(cacheKey, data);
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  const apiGet = (path) => api('GET', path);
  const apiPost = (path, body) => api('POST', path, body);
  const apiDelete = (path) => api('DELETE', path);

  // ============ Data transformers ============

  function transformStock(s, catalystByTicker = {}, newsByTicker = {}) {
    const cat = (catalystByTicker[s.ticker] || [])[0];
    const news = newsByTicker[s.ticker] || [];

    return {
      ticker: s.ticker,
      name: s.name,
      sector: s.sector || 'Khác',
      price: s.price ? s.price / 1000 : 0,
      change: s.change_pct || 0,
      score: Math.round(s.score || 50),
      eps: Math.round(s.eps_ttm || 0),
      pe: s.pe || 0,
      pb: s.pb || 0,
      roe: s.roe_ttm || 0,
      marketCap: s.market_cap || 0,
      foreignNet: s.foreign_net_5d || 0,
      volumeSpike: s.volume_spike || 1.0,
      reasons: buildReasons(s),
      catalyst: cat ? cat.title : buildCatalystFromMetrics(s),
      buyZone: computeBuyZone(s.price),
      target: computeTarget(s.price),
      stopLoss: computeStopLoss(s.price),
      risks: buildRisks(s),
      moat: 'Cần phân tích định tính bổ sung',
      valuation: s.pe ? `PE ${s.pe.toFixed(1)}, PB ${(s.pb || 0).toFixed(1)}` : 'Cần dữ liệu',
      scoreComponents: s.score_components || null,
      revenue: [],
      priceHistory: [],
      _newsCount: news.length,
    };
  }

  function buildReasons(s) {
    const reasons = [];
    const comp = s.score_components || {};
    const factors = comp.factors || [];
    // Take top 3 highest-scoring factors
    const sorted = [...factors].sort((a, b) => b.value - a.value).slice(0, 3);
    for (const f of sorted) {
      if (f.value >= 65) reasons.push(f.explain);
    }
    if (reasons.length === 0) {
      if (s.roe_ttm > 15) reasons.push(`ROE ${s.roe_ttm.toFixed(1)}% - hiệu quả cao`);
      if (s.revenue_yoy > 10) reasons.push(`Doanh thu tăng ${s.revenue_yoy.toFixed(1)}% YoY`);
      if (s.foreign_net_5d > 10) reasons.push(`Khối ngoại mua ròng ${s.foreign_net_5d.toFixed(1)} tỷ`);
    }
    if (reasons.length === 0) reasons.push('Cần phân tích thêm');
    return reasons.slice(0, 3);
  }

  function buildCatalystFromMetrics(s) {
    if (s.net_income_yoy && s.net_income_yoy > 30)
      return `Tăng trưởng LNST mạnh +${s.net_income_yoy.toFixed(1)}% YoY`;
    if (s.foreign_net_5d && s.foreign_net_5d > 50)
      return `Khối ngoại mua ròng ${s.foreign_net_5d.toFixed(1)} tỷ trong 5D`;
    return 'Chưa có catalyst rõ rệt. Kiểm tra tab tin tức.';
  }

  function buildRisks(s) {
    const risks = [];
    if (s.pe > 30) risks.push('Định giá cao - cần catalyst để duy trì');
    if (s.foreign_net_5d < -20) risks.push('Khối ngoại bán ròng');
    if (s.net_income_yoy && s.net_income_yoy < -10) risks.push('LNST giảm so với cùng kỳ');
    if (!risks.length) risks.push('Theo dõi vĩ mô và thị trường chung');
    return risks;
  }

  const computeBuyZone = (p) => {
    const x = p / 1000;
    return [Math.round(x * 0.97 * 100) / 100, Math.round(x * 1.01 * 100) / 100];
  };
  const computeTarget = (p) => {
    const x = p / 1000;
    return [Math.round(x * 1.15 * 100) / 100, Math.round(x * 1.25 * 100) / 100];
  };
  const computeStopLoss = (p) => Math.round((p / 1000) * 0.93 * 100) / 100;

  function transformQuotes(quotes) {
    return quotes.map(q => {
      const d = new Date(q.date);
      return {
        date: `${d.getDate()}/${d.getMonth() + 1}`,
        price: q.close,
        volume: q.volume || 0,
      };
    });
  }

  function transformFinancials(financials) {
    return financials.map(f => ({
      q: `Q${f.quarter}/${String(f.year).slice(2)}`,
      value: (f.revenue || 0) / 1000,  // millions → billions
      eps: f.eps || 0,
      roe: f.roe || 0,
    }));
  }

  function transformAlert(a) {
    return {
      ticker: a.ticker,
      type: a.alert_type,
      msg: a.title,
      time: relativeTime(a.triggered_at),
      severity: a.severity || 'medium',
    };
  }

  function transformNews(n) {
    return {
      ticker: (n.tickers && n.tickers[0]) || '—',
      title: n.title,
      time: relativeTime(n.published_at),
      source: n.source || 'unknown',
      summary: n.summary || (n.title.length > 100 ? n.title.slice(0, 100) + '...' : n.title),
    };
  }

  function relativeTime(iso) {
    if (!iso) return 'mới';
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.round(diff / 60000);
    if (mins < 1) return 'mới';
    if (mins < 60) return `${mins} phút`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} giờ`;
    const days = Math.round(hours / 24);
    return `${days} ngày`;
  }

  // ============ Main loader ============

  async function loadRealData() {
    const loader = document.getElementById('loader');
    const setStatus = (txt) => {
      const el = loader?.querySelector('.loader-text');
      if (el) el.textContent = txt;
    };
    setStatus('Đang kết nối backend...');

    try {
      const [stocksRaw, sectorsRaw, alertsRaw, newsRaw, catalystsRaw, watchlistRaw] = await Promise.all([
        apiGet('/stocks?limit=50&sort=score'),
        apiGet('/sectors'),
        apiGet('/alerts?hours=48&limit=20'),
        apiGet('/news?days=7&limit=15'),
        apiGet('/news/catalysts?days=14&min_confidence=0.5&limit=30'),
        apiGet(`/watchlist?user_id=${USER_ID}`).catch(() => []),
      ]);

      setStatus('Xử lý dữ liệu...');

      // Group catalysts by ticker
      const catalystByTicker = {};
      for (const c of catalystsRaw) {
        if (!catalystByTicker[c.ticker]) catalystByTicker[c.ticker] = [];
        catalystByTicker[c.ticker].push(c);
      }

      // Group news by ticker
      const newsByTicker = {};
      for (const n of newsRaw) {
        for (const t of (n.tickers || [])) {
          if (!newsByTicker[t]) newsByTicker[t] = [];
          newsByTicker[t].push(n);
        }
      }

      const stocks = stocksRaw.map(s => transformStock(s, catalystByTicker, newsByTicker));

      const payload = {
        stocks,
        sectors: sectorsRaw.map(s => ({
          name: s.name,
          change: s.change,
          volume: s.volume,
          hot: s.hot,
          count: s.count,
        })),
        indices: [
          { label: 'VN-Index', value: '—', change: 0 },
          { label: 'VN30', value: '—', change: 0 },
          { label: 'HNX', value: '—', change: 0 },
          { label: 'UPCOM', value: '—', change: 0 },
        ],
        alerts: alertsRaw.map(transformAlert),
        news: newsRaw.map(transformNews),
        _meta: {
          watchlistFromBackend: watchlistRaw.map(w => w.ticker),
          backend: API_URL,
        },
      };

      // Inject into the data script element
      const dataEl = document.getElementById('app-data');
      if (dataEl) {
        dataEl.textContent = JSON.stringify(payload);
      }

      console.log(`✓ FundCatalyst connected to ${API_URL}`);
      console.log(`  ${stocks.length} stocks, ${alertsRaw.length} alerts, ${newsRaw.length} news, ${catalystsRaw.length} catalysts`);

      return payload;

    } catch (err) {
      console.error('Backend connection failed:', err);
      showBanner('⚠️ Backend không kết nối được. Đang dùng dữ liệu mẫu.', 'warning');
      return null;
    }
  }

  // ============ Detail enhancement ============

  async function enhanceDetail(ticker) {
    try {
      const [quotes, financials, breakdown] = await Promise.all([
        apiGet(`/stocks/${ticker}/quotes?days=120`),
        apiGet(`/stocks/${ticker}/financials?n_quarters=8`),
        apiGet(`/scoring/breakdown/${ticker}`).catch(() => null),
      ]);
      return {
        priceHistory: transformQuotes(quotes),
        revenue: transformFinancials(financials),
        scoreBreakdown: breakdown,
      };
    } catch (err) {
      console.warn(`Failed to load detail for ${ticker}:`, err);
      return null;
    }
  }

  // ============ Watchlist sync ============

  async function syncWatchlist(localWatchlist) {
    if (!Array.isArray(localWatchlist)) return;
    try {
      const remote = await apiGet(`/watchlist?user_id=${USER_ID}`);
      const remoteTickers = new Set(remote.map(w => w.ticker));
      const localSet = new Set(localWatchlist);

      // Add to remote what's in local but not remote
      for (const t of localSet) {
        if (!remoteTickers.has(t)) {
          await apiPost('/watchlist?user_id=' + USER_ID, { ticker: t });
        }
      }
      // Delete from remote what's not in local
      for (const t of remoteTickers) {
        if (!localSet.has(t)) {
          await apiDelete(`/watchlist/${t}?user_id=${USER_ID}`);
        }
      }
    } catch (err) {
      console.warn('Watchlist sync failed:', err);
    }
  }

  // ============ Banner notification ============

  function showBanner(msg, type = 'info') {
    const colors = {
      info: { bg: 'rgba(52,211,153,0.15)', border: 'rgba(52,211,153,0.4)', text: '#6ee7b7' },
      warning: { bg: 'rgba(245,158,11,0.15)', border: 'rgba(245,158,11,0.4)', text: '#fcd34d' },
      error: { bg: 'rgba(244,63,94,0.15)', border: 'rgba(244,63,94,0.4)', text: '#fda4af' },
    };
    const c = colors[type] || colors.info;
    const banner = document.createElement('div');
    banner.style.cssText = `
      position: fixed; top: 70px; left: 50%; transform: translateX(-50%);
      background: ${c.bg}; border: 1px solid ${c.border}; color: ${c.text};
      padding: 8px 16px; border-radius: 8px; z-index: 9999;
      font-size: 12px; max-width: 90%; text-align: center;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    `;
    banner.innerHTML = msg;
    document.body.appendChild(banner);
    setTimeout(() => banner.style.opacity = '0', 5000);
    setTimeout(() => banner.remove(), 5500);
  }

  // ============ Auto-refresh during trading hours ============

  function isVNTradingHours() {
    const now = new Date();
    const vnTime = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Ho_Chi_Minh' }));
    const day = vnTime.getDay();
    if (day === 0 || day === 6) return false;
    const h = vnTime.getHours(), m = vnTime.getMinutes();
    const time = h * 100 + m;
    return (time >= 900 && time <= 1130) || (time >= 1300 && time <= 1500);
  }

  let refreshInterval = null;
  function startAutoRefresh() {
    if (refreshInterval) return;
    refreshInterval = setInterval(async () => {
      if (!isVNTradingHours()) return;
      cache.clear();
      const data = await loadRealData();
      if (data && typeof window.fcvnRerender === 'function') {
        window.fcvnRerender();
      }
    }, 5 * 60 * 1000);  // every 5 min
    console.log('Auto-refresh enabled (every 5 min during VN trading hours)');
  }

  // ============ Bootstrap ============

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', async () => {
      await loadRealData();
      startAutoRefresh();
    });
  } else {
    loadRealData().then(() => startAutoRefresh());
  }

  // Public API for debugging / advanced use
  window.FCVN = {
    api,
    apiGet,
    apiPost,
    apiDelete,
    reload: loadRealData,
    enhanceDetail,
    syncWatchlist,
    cache,
    config: { API_URL, USER_ID },
  };

  console.log(`FundCatalyst integration v2 loaded. API: ${API_URL}`);
})();
