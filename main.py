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


def _tw_quote_via_yf(code: str):
    """備援：證交所封鎖來源IP時，改用 Yahoo Finance 取得台股報價"""
    for suffix in (".TW", ".TWO"):
        try:
            t = yf.Ticker(f"{code}{suffix}")
            info = t.fast_info
            price = info.get("lastPrice")
            prev = info.get("previousClose")
            if price:
                return {
                    "code": code,
                    "name": None,
                    "price": round(float(price), 2),
                    "changePct": round((price - prev) / prev * 100, 2) if prev else None,
                    "volume": int(info.get("lastVolume") or 0),
                    "source": f"yahoo{suffix}",
                }
        except Exception:
            continue
    return None


@app.get("/tw/quote/{code}")
async def tw_quote(code: str):
    """單一台股即時（當日收盤）報價：價格、漲跌幅、成交量"""
    cache_key = "tw_all"
    data = cache_get(cache_key, ttl_sec=300)
    if data is None:
      try:
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
      except HTTPException:
        fb = _tw_quote_via_yf(code)
        if fb:
            return fb
        raise

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
    fb = _tw_quote_via_yf(code)
    if fb:
        return fb
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
    """個股歷史日K（優先證交所；來源IP被擋時自動改用 Yahoo Finance）"""
    months = max(1, min(months, 12))
    # --- 主來源：TWSE STOCK_DAY 逐月抓取 ---
    try:
        today = dt.date.today()
        bars = []
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StockDashboard/1.0)", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
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
                        y = int(y) + 1911
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
        if bars:
            return {"code": code, "bars": bars, "source": "twse"}
    except Exception:
        pass  # 進入備援

    # --- 備援一：FinMind（TaiwanStockPrice，官方級台股日K） ---
    try:
        rows = await finmind_get("TaiwanStockPrice", code, days_back=months * 31)
        if rows:
            bars = [
                {
                    "date": r["date"],
                    "open": float(r["open"]),
                    "high": float(r["max"]),
                    "low": float(r["min"]),
                    "close": float(r["close"]),
                    "volume": int(r.get("Trading_Volume") or 0),
                }
                for r in rows if r.get("close") is not None
            ]
            if bars:
                return {"code": code, "bars": bars, "source": "finmind"}
    except Exception:
        pass

    # --- 備援二：Yahoo Finance ---
    period = "6mo" if months <= 6 else "1y"
    for suffix in (".TW", ".TWO"):
        try:
            t = yf.Ticker(f"{code}{suffix}")
            hist = t.history(period=period, interval="1d")
            if hist.empty:
                continue
            bars = [
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": round(float(row.Open), 2),
                    "high": round(float(row.High), 2),
                    "low": round(float(row.Low), 2),
                    "close": round(float(row.Close), 2),
                    "volume": int(row.Volume),
                }
                for idx, row in hist.iterrows()
            ]
            return {"code": code, "bars": bars, "source": f"yahoo{suffix}"}
        except Exception:
            continue
    raise HTTPException(404, f"查無 {code} 的歷史資料（TWSE 與備援來源皆無法取得）")


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




# =================================================================
# FinMind（台股籌碼面：三大法人、融資融券、外資持股；並作為歷史日K備援）
# 免費 token 於 finmindtrade.com 註冊，設為 Render 環境變數 FINMIND_TOKEN
# =================================================================
import os
import datetime as _dt

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


async def finmind_get(dataset: str, stock_id: str, days_back: int = 40):
    token = os.environ.get("FINMIND_TOKEN", "")
    start = (_dt.date.today() - _dt.timedelta(days=days_back)).isoformat()
    params = {"dataset": dataset, "data_id": stock_id, "start_date": start}
    if token:
        params["token"] = token
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(FINMIND_URL, params=params)
    if r.status_code != 200:
        raise HTTPException(502, f"FinMind HTTP {r.status_code}: {r.text[:150]}")
    payload = r.json()
    if payload.get("status") != 200 and payload.get("msg") not in (None, "success"):
        raise HTTPException(502, f"FinMind 錯誤: {str(payload.get('msg'))[:150]}")
    return payload.get("data", [])


@app.get("/tw/chip/{code}")
async def tw_chip(code: str):
    """台股籌碼面：外資連買天數、投信5日買賣超、融資5日增減、外資持股比（FinMind）"""
    cache_key = f"tw_chip_{code}"
    data = cache_get(cache_key, ttl_sec=3600 * 4)
    if data is not None:
        return data

    result = {"code": code, "foreignStreak": None, "foreignNet5d": None,
              "trustNet5d": None, "marginChg5d": None, "foreignHold": None,
              "reasons": [], "score": None}

    # --- 三大法人買賣超 ---
    try:
        rows = await finmind_get("TaiwanStockInstitutionalInvestorsBuySell", code, 40)
        by_date = {}
        for r in rows:
            d = r.get("date")
            name = r.get("name", "")
            net = (r.get("buy", 0) or 0) - (r.get("sell", 0) or 0)
            by_date.setdefault(d, {})[name] = net
        dates = sorted(by_date.keys())
        # 外資連買/賣天數（Foreign_Investor，含 Foreign_Dealer_Self 合併）
        f_series = []
        for d in dates:
            v = by_date[d].get("Foreign_Investor", 0) + by_date[d].get("Foreign_Dealer_Self", 0)
            f_series.append(v)
        streak = 0
        for v in reversed(f_series):
            if v > 0 and streak >= 0:
                streak += 1
            elif v < 0 and streak <= 0:
                streak -= 1
            else:
                break
        result["foreignStreak"] = streak
        result["foreignNet5d"] = round(sum(f_series[-5:]) / 1000)  # 張
        t_series = [by_date[d].get("Investment_Trust", 0) for d in dates]
        result["trustNet5d"] = round(sum(t_series[-5:]) / 1000)  # 張
    except Exception:
        pass

    # --- 融資餘額變化 ---
    try:
        rows = await finmind_get("TaiwanStockMarginPurchaseShortSale", code, 20)
        bal = [r.get("MarginPurchaseTodayBalance") for r in rows if r.get("MarginPurchaseTodayBalance") is not None]
        if len(bal) >= 6 and bal[-6]:
            result["marginChg5d"] = round((bal[-1] - bal[-6]) / bal[-6] * 100, 1)
    except Exception:
        pass

    # --- 外資持股比 ---
    try:
        rows = await finmind_get("TaiwanStockShareholding", code, 12)
        for r in reversed(rows):
            ratio = r.get("ForeignInvestmentSharesRatio") or r.get("ForeignInvestmentShareRatio")
            if ratio is not None:
                result["foreignHold"] = round(float(ratio), 1)
                break
    except Exception:
        pass

    # --- 評分（與前端原邏輯一致） ---
    score = 0
    reasons = []
    fs = result["foreignStreak"]
    if fs is not None:
        if fs >= 5:
            score += 30; reasons.append(f"外資連續買超 {fs} 日")
        elif fs >= 3:
            score += 20; reasons.append(f"外資連續買超 {fs} 日")
        elif fs > 0:
            score += 10
        elif fs <= -3:
            reasons.append(f"外資連續賣超 {-fs} 日")
    tn = result["trustNet5d"]
    if tn is not None:
        if tn >= 1000:
            score += 25; reasons.append(f"投信近5日買超 {tn:,} 張")
        elif tn > 0:
            score += 15; reasons.append(f"投信近5日買超 {tn:,} 張")
        elif tn < -500:
            reasons.append("投信近5日站賣方")
    mc = result["marginChg5d"]
    if mc is not None:
        if mc <= -1.5:
            score += 25; reasons.append(f"融資近5日減 {-mc}%，籌碼趨於安定")
        elif mc < 0:
            score += 15
        elif mc > 3:
            reasons.append(f"融資近5日增 {mc}%，散戶籌碼偏多")
    fh = result["foreignHold"]
    if fh is not None:
        if fh >= 50:
            score += 20; reasons.append(f"外資持股 {fh}%，中長線結構穩固")
        elif fh >= 30:
            score += 12

    has_any = any(result[k] is not None for k in ("foreignStreak", "trustNet5d", "marginChg5d", "foreignHold"))
    result["score"] = min(100, score) if has_any else None
    result["reasons"] = reasons
    cache_set(cache_key, result)
    return result


# =================================================================
# 選股推薦（技術面：真實6個月日K後端計算；台股加籌碼面：FinMind）
# =================================================================
POOLS = {
    "TW": [
        ("2330.TW", "台積電"), ("2317.TW", "鴻海"), ("2454.TW", "聯發科"),
        ("2308.TW", "台達電"), ("2382.TW", "廣達"), ("3231.TW", "緯創"),
        ("2412.TW", "中華電"), ("2603.TW", "長榮"), ("2881.TW", "富邦金"),
        ("3008.TW", "大立光"), ("3661.TW", "世芯-KY"), ("6669.TW", "緯穎"),
    ],
    "US": [
        ("NVDA", "NVIDIA"), ("AAPL", "Apple"), ("MSFT", "Microsoft"),
        ("TSLA", "Tesla"), ("AMZN", "Amazon"), ("GOOGL", "Alphabet"),
        ("META", "Meta"), ("AVGO", "Broadcom"), ("AMD", "AMD"),
        ("INTC", "Intel"), ("JPM", "JPMorgan"), ("LLY", "Eli Lilly"),
    ],
}


def _tech_from_history(hist):
    """由真實歷史日K計算技術指標與評分。hist 需含 Close/Volume 欄位。"""
    closes = hist["Close"].dropna()
    vols = hist["Volume"].dropna()
    if len(closes) < 61:
        return None
    last = float(closes.iloc[-1])
    ma20 = float(closes.rolling(20).mean().iloc[-1])
    ma60 = float(closes.rolling(60).mean().iloc[-1])
    delta = closes.diff()
    gain = float(delta.clip(lower=0).rolling(14).mean().iloc[-1])
    loss = float((-delta.clip(upper=0)).rolling(14).mean().iloc[-1])
    rsi = 100.0 if loss == 0 else round(100 - 100 / (1 + gain / loss), 1)
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_bull = bool(dif.iloc[-1] > dea.iloc[-1])
    golden = bool(macd_bull and dif.iloc[-2] <= dea.iloc[-2])
    avg_vol = float(vols.rolling(20).mean().iloc[-1] or 0)
    vol_ratio = round(float(vols.iloc[-1]) / avg_vol, 2) if avg_vol else 1.0
    chg20 = round((last / float(closes.iloc[-21]) - 1) * 100, 1) if len(closes) > 21 else None

    score, reasons = 0, []
    if last > ma20:
        score += 20; reasons.append("股價站上月線（MA20）")
    if last > ma60:
        score += 15; reasons.append("股價站上季線（MA60）")
    if ma20 > ma60:
        score += 15; reasons.append("均線多頭排列（MA20 > MA60）")
    if golden:
        score += 20; reasons.append("MACD 黃金交叉")
    elif macd_bull:
        score += 12; reasons.append("MACD 維持多方")
    if 50 <= rsi <= 70:
        score += 15; reasons.append(f"RSI {rsi} 偏強且未過熱")
    elif rsi > 70:
        score += 5; reasons.append(f"RSI {rsi} 偏高，留意短線過熱")
    elif rsi >= 40:
        score += 8
    if vol_ratio >= 1.2:
        score += 15; reasons.append(f"量能放大（20日均量 {vol_ratio} 倍）")
    elif vol_ratio >= 0.9:
        score += 8

    return {
        "price": round(last, 2), "chg20d": chg20, "rsi": rsi,
        "aboveMA20": last > ma20, "aboveMA60": last > ma60,
        "macdBullish": macd_bull, "macdGolden": golden, "volRatio": vol_ratio,
        "techScore": min(100, score), "techReasons": reasons,
    }


@app.get("/screen/{market}")
async def screen(market: str):
    """掃描股票池：技術面（真實日K）+ 台股籌碼面（FinMind），綜合排序"""
    market = market.upper()
    if market not in POOLS:
        raise HTTPException(400, "market 需為 TW 或 US")
    cache_key = f"screen_{market}"
    data = cache_get(cache_key, ttl_sec=1800)
    if data is None:
        results = []
        for ticker, name in POOLS[market]:
            try:
                hist = yf.Ticker(ticker).history(period="6mo", interval="1d")
                if hist.empty:
                    continue
                tech = _tech_from_history(hist)
                if not tech:
                    continue
                row = {"ticker": ticker, "name": name, **tech,
                       "chipScore": None, "chipReasons": []}
                if market == "TW":
                    try:
                        chip = await tw_chip(ticker.replace(".TW", ""))
                        row["chipScore"] = chip.get("score")
                        row["chipReasons"] = chip.get("reasons", [])
                        row["chip"] = {k: chip.get(k) for k in
                                       ("foreignStreak", "foreignNet5d", "trustNet5d", "marginChg5d", "foreignHold")}
                    except Exception:
                        pass
                if row["chipScore"] is not None:
                    row["total"] = round(row["techScore"] * 0.5 + row["chipScore"] * 0.5)
                else:
                    row["total"] = row["techScore"]
                results.append(row)
            except Exception:
                continue
        results.sort(key=lambda r: r["total"], reverse=True)
        data = {"market": market, "results": results,
                "chipAvailable": market == "TW" and any(r["chipScore"] is not None for r in results)}
        cache_set(cache_key, data)
    return data


# =================================================================
# AI 分析（研究型：相對排序+理由；後端代理 Anthropic API）
# 金鑰設為 Render 環境變數 ANTHROPIC_API_KEY
# =================================================================
import json as _json

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-6")

AI_STYLE_RULES = (
    "分析風格：研究型。可以明確指出標的之間的相對強弱與排序理由（例如「五檔中X的訊號組合相對完整，因為...」），"
    "但不給出買賣指令。只根據提供的實際資料判斷；資料不足的面向必須明說「資料不足」而非猜測。"
    "禁用「必漲」「保證」「穩賺」等字眼。所有輸出使用繁體中文。"
)


async def _call_claude(prompt: str, max_tokens: int = 1500):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(503, "尚未設定 ANTHROPIC_API_KEY 環境變數（Render → Environment 新增後 redeploy）")
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(ANTHROPIC_URL, headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": AI_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        })
    if r.status_code != 200:
        raise HTTPException(502, f"AI 服務錯誤 HTTP {r.status_code}: {r.text[:200]}")
    payload = r.json()
    return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


def _extract_json(text: str):
    text = text.replace("```json", "").replace("```", "")
    start = text.find("{")
    if start < 0:
        raise HTTPException(502, "AI 回應中沒有結構化資料")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _json.loads(text[start:i + 1])
    raise HTTPException(502, "AI 回應的結構化資料不完整")


@app.get("/ai/screen/{market}")
async def ai_screen(market: str):
    """AI 盤面解讀（研究型：相對強弱排序與理由）"""
    market = market.upper()
    cache_key = f"ai_screen_{market}"
    cached = cache_get(cache_key, ttl_sec=3600)
    if cached:
        return cached
    scan = await screen(market)
    top = scan["results"][:5]
    if not top:
        raise HTTPException(404, "掃描結果為空")
    market_name = "台股" if market == "TW" else "美股"
    lines = []
    for i, r in enumerate(top):
        chip_txt = ""
        if r.get("chipScore") is not None:
            chip_txt = f"｜籌碼分{r['chipScore']}：{('、'.join(r['chipReasons'][:2]) or '中性')}"
        lines.append(
            f"{i+1}. {r['name']}({r['ticker']}) 綜合{r['total']}｜技術分{r['techScore']}："
            f"{('、'.join(r['techReasons'][:3]) or '訊號中性')}{chip_txt}｜RSI {r['rsi']}｜20日{r['chg20d']}%"
        )
    facts = "\n".join(lines)
    prompt = f"""你是客觀專業的{market_name}研究分析師。{AI_STYLE_RULES}

以下是系統以真實資料（6個月日K技術指標{'、FinMind法人籌碼' if market == 'TW' else ''}）掃描出的前五名：

{facts}

只回傳純JSON，不要其他文字：
{{"overview":"80字內：這批入選標的的共同特徵與目前盤面氛圍",
"ranking":"100字內：五檔的相對強弱排序判斷，指出訊號組合最完整的1-2檔與理由，以及排序靠後者弱在哪",
"picks":[{{"ticker":"代碼","comment":"40字內：該股訊號組合的意義與需留意之處"}}],
"caution":"50字內風險提醒，強調為統計性訊號判讀而非預測，僅供研究參考"}}
picks需涵蓋全部五檔。"""
    ai = _extract_json(await _call_claude(prompt, 1400))
    result = {"market": market, "ai": ai}
    cache_set(cache_key, result)
    return result


@app.get("/ai/stock/{market}/{code}")
async def ai_stock(market: str, code: str):
    """AI 個股研究報告（自動彙整真實資料生成）"""
    market = market.upper()
    cache_key = f"ai_stock_{market}_{code}"
    cached = cache_get(cache_key, ttl_sec=3600 * 4)
    if cached:
        return cached
    facts = []
    name = code
    if market == "TW":
        q = await tw_quote(code)
        name = q.get("name") or code
        facts.append(f"現價{q['price']}，當日{q.get('changePct')}%")
        try:
            h = await tw_history(code, months=6)
            import pandas as _pd
            tech = _tech_from_history(_pd.DataFrame({
                "Close": [b["close"] for b in h["bars"]],
                "Volume": [b["volume"] for b in h["bars"]],
            }))
            if tech:
                facts.append(
                    f"技術面：RSI {tech['rsi']}，20日漲跌{tech['chg20d']}%，"
                    f"{'站上' if tech['aboveMA20'] else '跌破'}月線、{'站上' if tech['aboveMA60'] else '跌破'}季線，"
                    f"MACD{'多方' if tech['macdBullish'] else '空方'}，技術分{tech['techScore']}"
                )
        except Exception:
            pass
        try:
            chip = await tw_chip(code)
            if chip.get("score") is not None:
                chip_bits = []
                if chip.get("foreignStreak") is not None:
                    s = chip["foreignStreak"]
                    chip_bits.append(f"外資連{'買' if s > 0 else '賣'}{abs(s)}日")
                if chip.get("trustNet5d") is not None:
                    chip_bits.append(f"投信5日{'買' if chip['trustNet5d'] >= 0 else '賣'}超{abs(chip['trustNet5d']):,}張")
                if chip.get("marginChg5d") is not None:
                    chip_bits.append(f"融資5日{'+' if chip['marginChg5d'] >= 0 else ''}{chip['marginChg5d']}%")
                if chip.get("foreignHold") is not None:
                    chip_bits.append(f"外資持股{chip['foreignHold']}%")
                facts.append("籌碼面：" + "，".join(chip_bits) + f"，籌碼分{chip['score']}")
        except Exception:
            pass
    else:
        q = await us_quote(code)
        facts.append(f"現價{q['price']}，當日{q.get('changePct')}%")
        try:
            p = await us_profile(code)
            name = p.get("name") or code
            facts.append(
                f"基本面：產業{p.get('industry')}，PE {p.get('pe')}，殖利率{p.get('dividendYield')}%，"
                f"營收YoY {p.get('revenueGrowth')}%，EPS YoY {p.get('earningsGrowth')}%"
            )
        except Exception:
            pass
        try:
            a = await us_analyst(code)
            rec, pt = a.get("recommendations"), a.get("priceTarget")
            if rec:
                facts.append(f"分析師：買進{rec['strongBuy'] + rec['buy']}/持有{rec['hold']}/賣出{rec['sell'] + rec['strongSell']}")
            if pt and pt.get("mean"):
                facts.append(f"目標價均值{pt['mean']}（區間{pt.get('low')}~{pt.get('high')}）")
        except Exception:
            pass
        try:
            n = await us_news(code, limit=4)
            titles = "；".join(x["title"] for x in n["news"] if x.get("title"))
            if titles:
                facts.append(f"近期新聞標題：{titles}")
        except Exception:
            pass

    facts_text = "。".join(facts)
    prompt = f"""你是客觀專業的股票研究分析師。{AI_STYLE_RULES}

根據以下真實市場資料，對 {name}（{code}）生成研究報告。

實際資料：{facts_text}

只回傳純JSON：
{{"summary":"100字內現況摘要",
"highlights":["亮點1（具體、基於資料）","亮點2","亮點3"],
"risks":[{{"title":"風險1","impact":"15字內影響"}},{{"title":"風險2","impact":"..."}},{{"title":"風險3","impact":"..."}}],
"scores":{{"technical":0,"chips":0,"fundamental":0,"sentiment":0,"overall":0,"reason":"25字內評分依據"}},
"conclusion":"80字內客觀結論，可指出此股目前的相對位置與適合的關注方式"}}
分數為0-100整數；沒有對應資料的面向給50並在reason註明資料不足。"""
    ai = _extract_json(await _call_claude(prompt, 1500))
    result = {"market": market, "code": code, "name": name, "ai": ai}
    cache_set(cache_key, result)
    return result


@app.get("/ai/watch")
async def ai_watch(codes: str):
    """AI 巡檢自選股：codes 為逗號分隔（台股數字代碼、美股ticker），指出最需注意的標的"""
    code_list = [c.strip().upper() for c in codes.split(",") if c.strip()][:12]
    if not code_list:
        raise HTTPException(400, "codes 不可為空")
    lines = []
    for c in code_list:
        is_tw = c.replace(".TW", "").isdigit()
        yf_code = (c.replace(".TW", "") + ".TW") if is_tw else c
        try:
            hist = yf.Ticker(yf_code).history(period="6mo", interval="1d")
            tech = _tech_from_history(hist) if not hist.empty else None
            if tech:
                lines.append(
                    f"{c}：現價{tech['price']}，20日{tech['chg20d']}%，RSI {tech['rsi']}，"
                    f"{'站上' if tech['aboveMA20'] else '跌破'}月線，MACD{'多方' if tech['macdBullish'] else '空方'}"
                    f"{'，MACD剛黃金交叉' if tech['macdGolden'] else ''}"
                    f"{'，量能放大' + str(tech['volRatio']) + '倍' if tech['volRatio'] >= 1.3 else ''}"
                )
            else:
                lines.append(f"{c}：資料不足")
        except Exception:
            lines.append(f"{c}：查詢失敗")
    facts = "\n".join(lines)
    prompt = f"""你是客觀專業的股票研究分析師。{AI_STYLE_RULES}

以下是使用者自選股的最新真實技術訊號，請巡檢並指出「最需要注意」的標的（訊號轉強、轉弱、或出現關鍵變化者優先）：

{facts}

只回傳純JSON：
{{"summary":"60字內整體巡檢結論",
"alerts":[{{"code":"代碼","level":"注意|轉強|轉弱","note":"30字內：具體訊號與建議的關注方式"}}],
"caution":"40字內提醒"}}
alerts 只列出2-4檔真正值得注意的，不必每檔都列。"""
    ai = _extract_json(await _call_claude(prompt, 1200))
    return {"codes": code_list, "ai": ai}


# =================================================================
# 前端靜態網站（同網域伺服，徹底避開 CORS）
# 將 static/index.html 與後端一起部署，任何裝置開網址即用
# =================================================================
import os
from fastapi.staticfiles import StaticFiles

if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
