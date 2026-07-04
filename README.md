# GG Stock — AI 股票研究儀表板

台股 + 美股的 AI 研究工具：選股推薦（技術面 + 籌碼面評分）、個股研究（真實K線、
籌碼、分析師評等）、自選股清單與 AI 巡檢。前後端同一服務部署，任何裝置開網址即用。

**免責聲明**：本專案內容僅供研究參考，不構成投資建議。

---

## 架構

```
瀏覽器（手機/平板/電腦）
   │  同網域，無 CORS 問題
   ▼
FastAPI（Render.com）
   ├─ static/index.html   ← 前端（單檔，無建置流程）
   ├─ /tw/*  台股：報價、歷史K線、籌碼
   ├─ /us/*  美股：報價、歷史、基本面、分析師、新聞
   ├─ /screen/*  選股掃描（後端計算真實技術指標）
   └─ /ai/*  AI 分析（後端代理 Anthropic API）
   │
   ▼
FinMind ／ Yahoo Finance(yfinance) ／ TWSE
```

## 檔案

| 檔案 | 用途 |
|---|---|
| `main.py` | 全部後端邏輯（資料抓取、指標計算、評分、AI 端點、靜態伺服） |
| `static/index.html` | 全部前端（三分頁 UI、K線圖、AI 呈現），無需編譯 |
| `requirements.txt` | Python 依賴 |
| `render.yaml` | Render 一鍵部署設定 |

## API 端點

| 端點 | 說明 |
|---|---|
| `GET /health` | 健康檢查 |
| `GET /tw/quote/{code}` | 台股報價（TWSE → Yahoo 備援） |
| `GET /tw/history/{code}?months=6` | 台股日K（TWSE → FinMind → Yahoo） |
| `GET /tw/chip/{code}` | 台股籌碼：外資連買、投信/外資5日買賣超、融資增減、外資持股（FinMind） |
| `GET /us/quote|history|profile|news|analyst/{ticker}` | 美股各項（yfinance） |
| `GET /screen/{TW\|US}` | 掃描股票池：技術分（真實日K計算）＋台股籌碼分，綜合排序 |
| `GET /ai/screen/{market}` | AI 盤面解讀（研究型：相對強弱排序＋理由） |
| `GET /ai/stock/{market}/{code}` | AI 個股研究報告 |
| `GET /ai/watch?codes=2330,AAPL` | AI 巡檢自選股，標注轉強/轉弱/注意 |

## 部署（Render.com 免費方案）

1. Fork 或上傳本 repo（需 Public）
2. 一鍵部署：`https://render.com/deploy?repo=https://github.com/<帳號>/<repo>`
3. **設定環境變數**（Render → 服務 → Environment）：

| 變數 | 必要性 | 取得方式 |
|---|---|---|
| `FINMIND_TOKEN` | 台股籌碼功能需要 | finmindtrade.com 免費註冊 |
| `ANTHROPIC_API_KEY` | 所有 AI 功能需要 | console.anthropic.com（按用量計費） |
| `AI_MODEL` | 選填 | 預設 `claude-sonnet-4-6` |

4. 部署完成後開 `https://你的網址.onrender.com` 即為完整儀表板
5. 修改程式後需手動觸發：Manual Deploy → Deploy latest commit（`render.yaml` 設 `autoDeploy: false`）

## 維護必讀：踩過的坑

這些是開發過程實際遇到並解決的問題，改程式前先讀：

1. **證交所（TWSE）封鎖海外機房 IP**。從 Render 呼叫 `www.twse.com.tw` 或
   `openapi.twse.com.tw` 會收到安全阻擋頁（HTML 而非 JSON）。所以台股資料
   的備援鏈是 TWSE → FinMind → Yahoo，實際上多半由 FinMind/Yahoo 供應。
   不要移除備援邏輯。
2. **yfinance `fast_info` 欄位是 camelCase**（`lastPrice`、`previousClose`、
   `yearHigh`），不是 snake_case。
3. **FinMind 免費額度有限**（註冊後約 600 次/小時），所以籌碼資料快取 4 小時、
   掃描結果快取 30 分鐘。調整快取前先評估額度。
4. **Render 免費方案閒置 15 分鐘會休眠**，喚醒需 30–50 秒。前端的等待提示
   文案是配合這個行為寫的。
5. **AI 呼叫是實際花費**（每份報告約 NT$0.3–1）。所以 AI 全部按鈕觸發、
   結果快取（個股報告 4 小時、盤面解讀 1 小時）。不要改成自動生成。
6. **AI 風格為「研究型」**：可做相對強弱排序與理由，不給買賣指令、不用保證
   性字眼、資料不足須明說。這些規則寫在 `main.py` 的 `AI_STYLE_RULES`，
   修改提示詞時保留。
7. 瀏覽器直連金融資料源會被 CORS 擋，這正是本專案採用後端代理架構的原因。
   不要嘗試把資料抓取搬回前端。

## 自訂

- **調整選股池**：改 `main.py` 的 `POOLS`（台股代碼帶 `.TW` 後綴）
- **調整評分權重**：`screen()` 內 `techScore * 0.5 + chipScore * 0.5`
- **調整 AI 模型**：設環境變數 `AI_MODEL`
