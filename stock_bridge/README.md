# TWSE Stock Bridge

這個目錄提供 `2002 中鋼` 與 `3019 亞光` 的固定資料橋接，目標是讓 ChatGPT 排程只讀取一個穩定 URL，不再自行組合 TWSE MIS、STOCK_DAY 或 OpenAPI 動態網址。

## 第一階段 MVP

目前實作：

- TWSE MIS 盤中即時報價，每檔最多 8 次取樣、每次使用唯一 cache-buster。
- 僅接受同一筆 MIS `msgArray` 中的 `z + d + t/tlong` 作為有效成交紀錄。
- TWSE `STOCK_DAY` 月度 OHLCV 歷史資料快取。
- 初次建立時向前抓取足量月份，目標保留至少 780 個有效交易日，後續只更新最近月份。
- TWSE OpenAPI `STOCK_DAY_ALL` 交叉檢查最近完成交易日，並可補上月度頁尚未更新的最近一日。
- 260 日與 750 日筆數狀態分開輸出。
- 產生固定檔案 `stock_bridge/latest.json`。

目前**尚未**把技術指標與 10% 潛力判斷搬到 GitHub。`latest.json` 會明確標示 `bridge.analysis_ready=false`，避免第一階段資料被誤當成已完成的投資訊號資料。

## 固定資料 URL

合併到 `main` 並完成第一次成功 workflow 後，排程應固定讀取：

`https://raw.githubusercontent.com/JohnChenfromLargan/gpt-ai-assistant/main/stock_bridge/latest.json`

這個 URL 不需要日期、股票代號或 cache-buster，因此可直接逐字寫入 ChatGPT 排程規則。

## 更新時程

GitHub Actions 使用 UTC cron，固定於台北時間週一至週五：

- 09:20
- 10:20
- 11:20

更新資料。ChatGPT 排程仍維持 09:30、10:30、11:30，因此正常情況下有約 10 分鐘資料準備緩衝。

> GitHub scheduled Actions 可能延遲，所以 ChatGPT 端仍應保留 `generated_at` 不超過 15 分鐘的 freshness Gate。

## JSON 主要欄位

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-08-27T10:20:00+08:00",
  "bridge": {
    "transport_ready": true,
    "analysis_ready": false,
    "phase": "MVP_DATA_BRIDGE"
  },
  "quotes": {
    "2002": {"status": "PASS"},
    "3019": {"status": "PASS"}
  },
  "history": {
    "2002": {
      "count": 780,
      "history_gate": "PASS",
      "long_term_750_ready": true,
      "price_adjustment_status": "PENDING_PHASE_2",
      "analysis_ready": false
    }
  }
}
```

## Gate 定義

第一階段只驗證「資料輸送」：

- Quote `PASS`：今天、15 分鐘內、有效正數成交價，且 `z+d+t/tlong` 來自同一筆 MIS 物件。
- History `history_gate=PASS`：最近完成交易日新鮮、OHLCV 完整且至少 260 筆。
- `long_term_750_ready=true`：至少 750 筆。
- `analysis_ready=false`：代表除權息調整與正式指標尚未完成，不能據此產生 10% 投資訊號。

## 第二階段

資料橋接驗收後再加入：

1. TWSE 歷史除權息／減資／分割處理與驗證。
2. MA5/20/60/120/240。
3. RSI(14)。
4. MACD(12,26,9)。
5. ATR(14)。
6. 10～240 日分層所需的歷史報酬／波動統計。
7. 將 `analysis_ready` 提升為 `true` 的完整 Gate。

投資訊號本身仍由 ChatGPT 排程依既有的 10% 潛力與「目前時機確認」規則判斷。
