# TWSE Stock Bridge

本儲存庫維護 2002 中鋼與 3019 亞光的 TWSE 資料擷取、歷史資料整理與技術分析程式。

- 程式及詳細文件：[stock_bridge](stock_bridge/README.md)。
- 原始碼驗證：[TWSE Stock Bridge workflow](.github/workflows/twse-stock-bridge.yml)。
- 正式資料發布端：[JohnChenfromLargan/desktop-tutorial](https://github.com/JohnChenfromLargan/desktop-tutorial)。

正式發布流程使用本儲存庫的 `main` 分支。保留既有儲存庫名稱 `gpt-ai-assistant`，以維持發布流程及既有連結的相容性。

## 執行環境

Python 3.12；程式使用 Python 標準函式庫。

## 驗證

在儲存庫根目錄執行：

```bash
python stock_bridge/bridge.py --self-test
python stock_bridge/phase2_fixed.py --self-test
```

`main` 分支另包含報價快取層測試：

```bash
python stock_bridge/bridge_quote_layer.py --self-test
python stock_bridge/bridge_quote_layer_v2.py --self-test
```

資料格式、驗證條件與發布架構請參閱 [stock_bridge/README.md](stock_bridge/README.md)。
