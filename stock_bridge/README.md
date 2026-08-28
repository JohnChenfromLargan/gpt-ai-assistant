# TWSE Stock Bridge

這個目錄提供 `2002 中鋼` 與 `3019 亞光` 的固定資料橋接，讓 ChatGPT 股票排程讀取固定 GitHub 資料檔，不再自行組合 TWSE MIS、STOCK_DAY 或 OpenAPI 動態網址。

## 正式狀態

Phase 1 與 Phase 2 已部署到 `main` 並通過 GitHub Actions 真實 TWSE 整合測試：

- TWSE MIS 盤中即時報價，每檔最多 8 次取樣、每次使用唯一 cache-buster。
- 僅接受同一筆 MIS `msgArray` 中的 `z + d + t/tlong` 作為有效成交紀錄。
- TWSE `STOCK_DAY` 月度 OHLCV 歷史快取，目前每檔保留約 788 個有效交易日。
- TWSE OpenAPI `STOCK_DAY_ALL` 交叉檢查最近完成交易日。
- TWSE 除權息與減資資料驗證；歷史 OHLC 以官方參考價／前收盤價進行 back-adjustment。
- 2002 中鋼 2026-07-24 除息事件設為固定回歸 Gate，避免公司行動來源或日期解析失效卻誤判 PASS。
- MA5/20/60/120/240。
- RSI(14)、MACD(12,26,9)、MACD Signal／Histogram、ATR(14)。
- 10、20、22、40、60、120、240 日歷史報酬分布與目前 20/60/120/240 日價格區間。
- `bridge.transport_ready=true` 與 `bridge.analysis_ready=true` 才允許下游排程使用分析資料。

資料橋接本身不做買入／賣出判斷；10% 潛力、目前時機確認、基本面交叉檢查與通知去重仍由 ChatGPT 排程執行。

## 固定資料位置

ChatGPT 排程目前透過已連結 GitHub repository 讀取：

- repository：`JohnChenfromLargan/gpt-ai-assistant`
- ref：`main`
- 主資料：`stock_bridge/latest.json`
- 完整歷史：`stock_bridge/history/2002.json`、`stock_bridge/history/3019.json`

公開 raw URL 為：

`https://raw.githubusercontent.com/JohnChenfromLargan/gpt-ai-assistant/main/stock_bridge/latest.json`

正式排程優先使用 GitHub connected app 讀取 repository file，以避免 Web 動態 URL／allowlist 問題。

## 更新時程

GitHub Actions 使用 UTC cron，固定於台北時間週一至週五：

- 09:20
- 10:20
- 11:20

ChatGPT 股票排程維持 09:30、10:30、11:30，因此正常情況下有約 10 分鐘資料準備緩衝。

> GitHub scheduled Actions 可能延遲，因此 ChatGPT 端仍以 `generated_at` 距執行時間不超過 15 分鐘作為 Bridge freshness Gate，且個股成交時間也會重新計算 15 分鐘時效。

## JSON Gate

`latest.json` 目前使用 schema `2.0`。下游正式分析至少要求：

- `source.primary == "TWSE"`
- `bridge.transport_ready == true`
- `bridge.analysis_ready == true`
- `history[symbol].history_gate == "PASS"`
- `history[symbol].count >= 260`
- 121～240 日長期分析另要求 `count >= 750` 與 `long_term_750_ready == true`
- `price_adjustment_status == "PASS"`
- `analytics[symbol].analysis_ready == true`
- `corporate_actions.status == "PASS"`
- `corporate_actions.fetch_error_count == 0`
- `unresolved_discontinuities` 為空
- 所需 MA／RSI／MACD／ATR 欄位存在

個股 quote Gate 另外要求今日有效成交且相對 ChatGPT 排程實際執行時間不超過 15 分鐘；不得只沿用 Bridge 產生當下的 `age_seconds`。

## 公司行動資料

目前官方來源：

- 除權息：TWSE `rwd/zh/exRight/TWT49U`
- 減資恢復買賣：TWSE `exchangeReport/TWTAUU`

TWSE 日期可能以 `115年07月24日` 等民國中文格式回傳，Bridge 已納入解析與回歸測試。

## 職責分工

GitHub Bridge：

1. 取得與驗證 TWSE 資料。
2. 維護歷史快取。
3. 處理公司行動價格斷層。
4. 計算技術指標與多期間統計。
5. 提供固定、可驗證的 JSON 資料契約。

ChatGPT 股票排程：

1. 驗證 Bridge freshness 與個股 quote/history/analysis Gate。
2. 依 10～240 日時間尺度評估至少 ±10% 的保守價格空間。
3. 以至少兩種獨立方法支持目標價。
4. 61～240 日另加入基本面／估值／產業／重大公告依據。
5. 確認當下進出場時機、訊號衝突與避免重複通知。
6. 保留 `PIPELINE_STATE` 與資料異常通知機制。
