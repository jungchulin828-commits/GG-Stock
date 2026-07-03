"""
AI Stock Dashboard 後端代理
--------------------------------
提供台股（證交所）與美股（Yahoo Finance / yfinance）的即時報價、
歷史日K、公司資料、新聞。前端 artifact 因瀏覽器 CORS 限制無法直連
這些來源，此服務作為中介層，並統一補上 CORS 允許標頭。

執行：uvicorn main:app --host 0.0.0.0 --port 8000
"""
import time
import datetime as dt
from typing import Optional

import httpx
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AI Stock Dashboard Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # 供 claude.ai artifact 沙盒呼叫
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------
# 簡易記憶體快取（避免短時間重複請求打爆來源 API / 被限流）
# ---------------------------------------------------------------
_cache = {}

def cache_get(key, ttl_sec):
    hit = _cache.get(key)
    if hit and time.time() - hit["t"] < ttl_sec:
        return hit["v"]
    return None

def cache_set(key, value):
    _cache[key] = {"t": time.time(), "v": value}


# =================================================================
# 台股（TWSE）
# =================================================================
TWSE_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"


def _to_float(v):
    """TWSE 欄位可能是 '--'、空字串或含逗號，統一清洗"""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "--", "-", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@app.get("/tw/quote/{code}")
async def tw_quote(code: str):
    """單一台股即時（當日收盤）報價：價格、漲跌幅、成交量"""
    cache_key = "tw_all"
    data = cache_get(cache_key, ttl_sec=300)
    if data is None:
        try:
            async with httpx.AsyncClient(timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (compatible; StockDashboard/1.0)",
                "Accept": "application/json",
            }) as client:
                r = await client.get(TWSE_ALL_URL)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"TWSE 連線失敗: {type(e).__name__}: {e}")
        if r.status_code != 200:
            raise HTTPException(502, f"TWSE 回應異常: HTTP {r.status_code}, body[:200]={r.text[:200]}")
        try:
            data = r.json()
        except ValueError:
            raise HTTPException(502, f"TWSE 回應非JSON: body[:200]={r.text[:200]}")
        if not isinstance(data, list):
            raise HTTPException(502, f"TWSE 回應格式異常: {str(data)[:200]}")
        cache_set(cache_key, data)

    for row in data:
        if row.get("Code") == code:
            close = _to_float(row.get("ClosingPrice"))
            if close is None:
                raise HTTPException(502, f"該股今日無收盤價資料: {row}")
            chg = _to_float(row.get("Change")) or 0.0
            prev = close - chg
            return {
                "code": code,
                "name": row.get("Name"),
                "price": close,
                "changePct": round(chg / prev * 100, 2) if prev else None,
                "volume": int(_to_float(row.get("TradeVolume")) or 0),
            }
    raise HTTPException(404, f"查無股票代碼 {code}（共{len(data)}筆資料）")


@app.get("/debug/twse")
async def debug_twse():
    """診斷用：直接顯示 TWSE API 的原始回應狀態"""
    try:
        async with httpx.AsyncClient(timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; StockDashboard/1.0)",
            "Accept": "application/json",
        }) as client:
            r = await client.get(TWSE_ALL_URL)
        return {
            "status": r.status_code,
            "content_type": r.headers.get("content-type"),
            "body_head": r.text[:300],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/tw/history/{code}")
async def tw_history(code: str, months: int = 6):
    """個股歷史日K（近 N 個月，逐月呼叫 TWSE STOCK_DAY 並合併）"""
    months = max(1, min(months, 12))
    today = dt.date.today()
    bars = []
    async with httpx.AsyncClient(timeout=15) as client:
        for i in range(months - 1, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            date_str = f"{year}{month:02d}01"
            cache_key = f"tw_day_{code}_{date_str}"
            month_data = cache_get(cache_key, ttl_sec=3600 * 6)
            if month_data is None:
                r = await client.get(TWSE_DAY_URL, params={"response": "json", "date": date_str, "stockNo": code})
                r.raise_for_status()
                payload = r.json()
                month_data = payload.get("data", [])
                cache_set(cache_key, month_data)
            for row in month_data:
                try:
                    y, m, d = row[0].split("/")
                    y = int(y) + 1911  # 民國年轉西元年
                    bars.append({
                        "date": f"{y}-{int(m):02d}-{int(d):02d}",
                        "open": float(row[3].replace(",", "")),
                        "high": float(row[4].replace(",", "")),
                        "low": float(row[5].replace(",", "")),
                        "close": float(row[6].replace(",", "")),
                        "volume": int(row[1].replace(",", "") or 0),
                    })
                except (ValueError, IndexError):
                    continue
    if not bars:
        raise HTTPException(404, f"查無 {code} 的歷史資料")
    return {"code": code, "bars": bars}


# =================================================================
# 美股（Yahoo Finance via yfinance）
# =================================================================
@app.get("/us/quote/{ticker}")
async def us_quote(ticker: str):
    cache_key = f"us_quote_{ticker}"
    data = cache_get(cache_key, ttl_sec=60)
    if data is None:
        t = yf.Ticker(ticker)
        info = t.fast_info
        prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
        price = info.get("lastPrice")
        if price is None:
            raise HTTPException(404, f"查無股票代碼 {ticker}")
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
        data = {
            "ticker": ticker,
            "price": round(price, 2),
            "changePct": change_pct,
            "week52High": info.get("yearHigh"),
            "week52Low": info.get("yearLow"),
        }
        cache_set(cache_key, data)
    return data


@app.get("/us/history/{ticker}")
async def us_history(ticker: str, period: str = "6mo", interval: str = "1d"):
    """period: 1mo/3mo/6mo/1y/2y ; interval: 1d/1wk/1mo"""
    cache_key = f"us_hist_{ticker}_{period}_{interval}"
    data = cache_get(cache_key, ttl_sec=1800)
    if data is None:
        t = yf.Ticker(ticker)
        hist = t.history(period=period, interval=interval)
        if hist.empty:
            raise HTTPException(404, f"查無 {ticker} 的歷史資料")
        bars = [
            {
                "date": idx.strftime("%Y-%m-%d"),
                "open": round(row.Open, 2),
                "high": round(row.High, 2),
                "low": round(row.Low, 2),
                "close": round(row.Close, 2),
                "volume": int(row.Volume),
            }
            for idx, row in hist.iterrows()
        ]
        data = {"ticker": ticker, "bars": bars}
        cache_set(cache_key, data)
    return data


@app.get("/us/profile/{ticker}")
async def us_profile(ticker: str):
    cache_key = f"us_profile_{ticker}"
    data = cache_get(cache_key, ttl_sec=3600 * 6)
    if data is None:
        t = yf.Ticker(ticker)
        info = t.info
        data = {
            "ticker": ticker,
            "name": info.get("longName") or info.get("shortName"),
            "exchange": info.get("exchange"),
            "industry": info.get("industry"),
            "marketCap": info.get("marketCap"),
            "pe": info.get("trailingPE"),
            "eps": info.get("trailingEps"),
            "dividendYield": round(info.get("dividendYield") * 100, 2) if info.get("dividendYield") else None,
            "revenueGrowth": round(info.get("revenueGrowth") * 100, 2) if info.get("revenueGrowth") else None,
            "earningsGrowth": round(info.get("earningsGrowth") * 100, 2) if info.get("earningsGrowth") else None,
            "week52High": info.get("fiftyTwoWeekHigh"),
            "week52Low": info.get("fiftyTwoWeekLow"),
            "summary": info.get("longBusinessSummary"),
        }
        cache_set(cache_key, data)
    return data


@app.get("/us/news/{ticker}")
async def us_news(ticker: str, limit: int = 5):
    t = yf.Ticker(ticker)
    items = t.news or []
    out = []
    for n in items[:limit]:
        content = n.get("content", n)
        out.append({
            "title": content.get("title"),
            "source": (content.get("provider") or {}).get("displayName", "Yahoo Finance"),
            "date": content.get("pubDate", "")[:10],
            "summary": (content.get("summary") or "")[:120],
        })
    return {"ticker": ticker, "news": out}


@app.get("/us/analyst/{ticker}")
async def us_analyst(ticker: str):
    """分析師評等分布（最新一期）與12個月目標價區間，供投資判斷參考。"""
    cache_key = f"us_analyst_{ticker}"
    data = cache_get(cache_key, ttl_sec=3600 * 6)
    if data is None:
        t = yf.Ticker(ticker)
        rec_summary = None
        try:
            rec = t.recommendations
            if rec is not None and not rec.empty:
                latest = rec.iloc[0]
                rec_summary = {
                    "period": str(latest.get("period", "0m")),
                    "strongBuy": int(latest.get("strongBuy", 0)),
                    "buy": int(latest.get("buy", 0)),
                    "hold": int(latest.get("hold", 0)),
                    "sell": int(latest.get("sell", 0)),
                    "strongSell": int(latest.get("strongSell", 0)),
                }
        except Exception:
            rec_summary = None

        target = None
        try:
            pt = t.analyst_price_targets
            if pt:
                target = {
                    "current": pt.get("current"),
                    "low": pt.get("low"),
                    "high": pt.get("high"),
                    "mean": pt.get("mean"),
                    "median": pt.get("median"),
                }
        except Exception:
            target = None

        data = {"ticker": ticker, "recommendations": rec_summary, "priceTarget": target}
        cache_set(cache_key, data)
    return data


@app.get("/health")
async def health():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}
