# TWSE Stock Bridge

本目錄維護 `2002 中鋼` 與 `3019 亞光` 的 TWSE 資料擷取、歷史整理與技術分析程式。正式股票排程不直接組合 TWSE 動態網址，而是讀取固定 GitHub 發布檔案。

## 正式架構

```text
TWSE MIS / STOCK_DAY / OpenAPI / 公司行動資料
                    ↓
JohnChenfromLargan/gpt-ai-assistant
（程式原始碼與 Source CI）
                    ↓
JohnChenfromLargan/desktop-tutorial
（非 fork 的固定發布端）
                    ↓
stock_bridge/latest.json
                    ↓
ChatGPT 股票排程
```

正式發布位置：

- repository：`JohnChenfromLargan/desktop-tutorial`
- branch：`main`
- 主資料：`stock_bridge/latest.json`
- 最近有效成交快取：`stock_bridge/quote_cache.json`
- 發布證據：`stock_bridge/publisher.json`
- 歷史快取：`stock_bridge/history/2002.json`、`stock_bridge/history/3019.json`
- 按需刷新入口：`stock_bridge/trigger.json`

本 source repository 不再擔任正式資料發布端；`.github/workflows/twse-stock-bridge.yml` 只執行 self-test 與整合驗證，不 commit 市場資料。

## Quote Layer v2

正式入口為 `bridge_quote_layer_v2.py`。

### MIS 擷取策略

- 將 2002 與 3019 放在同一個 `getStockInfo.jsp` 請求中，取得同一伺服器快照。
- 不再固定每 5 秒取樣，改用去相位（dephased）間隔：`1.7、2.3、2.9、1.9、2.6、2.1、3.1、1.8` 秒循環。
- 最多 30 輪、約 68 秒；兩檔均取得近期有效成交後提前停止。
- 每次請求使用唯一 cache-buster。
- 僅接受同一個 MIS `msgArray` 物件中的完整：
  - `c`
  - `n`
  - 數值型 `z`
  - `d`
  - `t` 或 `tlong`
- 若 `z="-"`，不得用下列欄位代替成交價：
  - 五檔 `a／b`
  - 累積量 `v`
  - 當盤量 `tv`
  - `pz／ps`
  - 開盤、最高、最低、昨收或買賣中間價

### 最近有效成交快取

`bridge_quote_layer.py` 提供經驗證成交的序列化與 Gate 共用函式；v2 將資料保存到發布端的 `quote_cache.json`。

快取只可在以下條件全部成立時讓 quote Gate PASS：

1. 原紀錄確定來自同一個 MIS `msgArray` 物件。
2. 股票代號與名稱正確。
3. 交易日期為今天的台灣交易日。
4. 原始成交時間距目前不超過 15 分鐘。
5. 沿用原始成交時間，不得以新的 MIS 揭示時間覆寫。

若超過 15 分鐘或屬前一交易日，只能是 `FAIL_STALE_TRADE`；沒有合法紀錄則維持 `FAIL_NO_RECENT_TRADE` 或 `FAIL_NETWORK`。

## 歷史與分析

- TWSE `STOCK_DAY`：逐月 OHLCV，兩檔各維護至少 750 個交易日。
- TWSE OpenAPI `STOCK_DAY_ALL`：交叉確認最近完成交易日。
- TWSE 公司行動資料：除權息、減資與價格斷層調整。
- 固定回歸 Gate：2002 中鋼 `2026-07-24` 除息事件。
- 技術指標：MA5／20／60／120／240、RSI(14)、MACD(12,26,9)、Signal、Histogram、ATR(14)。
- 統計尺度：10、20、22、40、60、120、240 日報酬與價格區間。

資料橋接只準備資料，不做買入、賣出、減碼或 10% 潛力結論。

## 發布與刷新

`desktop-tutorial` 的正式 Host workflow：

- 主要排程：台北時間週一至週五 09:18、10:18、11:18。
- 備援排程：09:23、10:23、11:23。
- ChatGPT 排程：09:30、10:30、11:30。
- 若 cron 未即時更新，ChatGPT 排程只更新固定的 `stock_bridge/trigger.json` 一次，以 `push` 事件要求刷新，並等待 `publisher.json` 與 `latest.json` 更新。

GitHub cron 不是唯一依賴；固定 trigger 是正式按需備援路徑。

## Gate 分離

下游必須分開判定：

1. **Bridge freshness**
   - `schema_version == "2.0"`
   - `market_date` 為今天
   - `generated_at` 距執行時間不超過 15 分鐘
   - `bridge.analysis_ready == true`

2. **個股 quote Gate**
   - `quotes[symbol].status == "PASS"`
   - `price／trade_date／trade_time` 完整
   - `same_msgarray_verified == true`
   - 成交時間重新以排程執行時間檢查，不超過 15 分鐘

3. **個股 history Gate**
   - `history_gate == "PASS"`
   - `path_status == "PASS"`
   - `freshness_status == "PASS"`
   - `count >= 260`
   - `integrity_error_count == 0`
   - `price_adjustment_status == "PASS"`
   - 121～240 日另要求 `count >= 750` 與 `long_term_750_ready == true`

4. **個股 analysis Gate**
   - `analytics[symbol].analysis_ready == true`
   - 公司行動 Gate 通過
   - 所需技術指標與期間統計存在

`bridge.transport_ready=false` 只表示目前沒有任何股票取得可用盤中成交，不等於 Bridge 過期，也不得連帶把 history Gate 改為失敗。

## 測試

```bash
python stock_bridge/bridge.py --self-test
python stock_bridge/bridge_quote_layer.py --self-test
python stock_bridge/bridge_quote_layer_v2.py --self-test
python stock_bridge/phase2_fixed.py --self-test
```

Production Host workflow 另驗證：

- v2 combined/dephased 模式已啟用。
- quote cache schema 與 15 分鐘上限。
- PASS quote 必須具有同源 `z+d+t/tlong` 證據。
- 750 日、公司行動、除權息調整與技術分析 Gate。

## 職責分工

GitHub Bridge：

1. 取得與驗證 TWSE 資料。
2. 維護最近有效成交與歷史快取。
3. 處理公司行動價格斷層。
4. 計算技術指標與多期間統計。
5. 發布固定且可稽核的 JSON 契約。

ChatGPT 股票排程：

1. 驗證 Bridge freshness 與個股 quote/history/analysis Gate。
2. 依 10～240 日尺度評估至少 ±10% 的保守價格空間。
3. 以至少兩種獨立方法支持目標價。
4. 對 61～240 日加入基本面、估值、產業或重大公告依據。
5. 確認當下進出場時機、訊號衝突與通知去重。
6. 保留 `PIPELINE_STATE` 與資料異常通知機制。
