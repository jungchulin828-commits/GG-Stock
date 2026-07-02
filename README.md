# AI Stock Dashboard 後端代理 — 部署說明

這個後端解決一個核心問題：瀏覽器（artifact）直接呼叫證交所或 Yahoo Finance
常會被 CORS 政策擋下。後端對後端的請求不受此限制，並統一補上允許跨網域的標頭。

## 提供的端點

| 端點 | 說明 |
|---|---|
| `GET /tw/quote/{code}` | 台股即時（當日）報價，例：`/tw/quote/2330` |
| `GET /tw/history/{code}?months=6` | 台股歷史日K（近N個月，最多12個月） |
| `GET /us/quote/{ticker}` | 美股即時報價，例：`/us/quote/AAPL` |
| `GET /us/history/{ticker}?period=6mo&interval=1d` | 美股歷史日K |
| `GET /us/profile/{ticker}` | 美股公司資料、本益比、殖利率、成長率、52週高低 |
| `GET /us/news/{ticker}` | 美股近期新聞 |
| `GET /health` | 健康檢查 |

資料來源：台股 = 證交所公開資訊；美股 = Yahoo Finance（透過 yfinance 套件）。
皆為公開免費資料，無需任何 API 金鑰。

---

## 一鍵部署（最省力）

這個 repo 已內含 `render.yaml`，支援 Render 的一鍵部署按鈕。

### 前置：把這 4 個檔案放上一個「公開」的 GitHub repo

1. 到 https://github.com 登入 →右上角 **+** → **New repository**
2. 取名（例如 `stock-dashboard-backend`），選 **Public**，按 **Create repository**
3. 在新 repo 頁面點 **uploading an existing file**，把
   `main.py`、`requirements.txt`、`render.yaml`、`README.md` 拖進去 → **Commit changes**

### 一鍵部署按鈕

把下面這行貼到你 repo 的 README（或直接用瀏覽器打開網址，
將 `<你的帳號>/<你的repo>` 換成實際的）：

```markdown
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/<你的帳號>/<你的repo>)
```

點按鈕後：
1. 若尚未登入 Render，先用 GitHub 帳號登入
2. Render 會自動讀取 `render.yaml`，顯示即將建立的服務
3. 按 **Deploy Blueprint**，等 2–3 分鐘

### 測試

部署完成後會得到一個網址，例如
`https://ai-stock-dashboard-proxy.onrender.com`。
瀏覽器打開 `你的網址/health`，看到 `{"status":"ok",...}` 即成功。
再測 `你的網址/tw/quote/2330` 確認台股報價正常。

**注意**：免費方案閒置 15 分鐘會休眠，下次請求需約 30–50 秒喚醒（正常現象）。

---

## 部署方式二：Railway.app

1. https://railway.app 註冊，New Project → Deploy from GitHub repo
2. 選擇同一個 repository，Railway 會自動偵測 Python 專案
3. 在 Settings → Deploy 設定 Start Command：
   `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. 部署完成後在 Settings → Networking 產生一組公開網址

---

## 部署後：接回 Dashboard

拿到部署網址後，把它貼到 Dashboard 的「自選股」分頁中新增的
「後端代理網址」欄位，Dashboard 會自動改用這個後端取得即時報價與
真實歷史K線，不再需要 Finnhub 金鑰或不穩定的搜尋備援。
