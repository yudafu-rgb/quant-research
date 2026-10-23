# quant-research

日线回测与观察池研究项目，不连接券商 / 交易所账户，不自动下单，不构成投资建议。
所有数据来自公开免费接口，可能延迟、限流或格式变化。

本仓库与 `quant-trading-engine` 并行运行、互不覆盖：

- `quant-trading-engine`：1 小时、每天 3 次、ATR 共振、多空、24 小时持有、前向信号观测。
- 本仓库：日线、每天 1 次、ADX 趋势 / 均值回归、只做多、最长持有 60 天、历史回测 + 观察池。

## 运行方式

```bash
pip install -r requirements.txt
python -m src.main
```

注意：必须用 `python -m src.main` 从仓库根目录运行，直接运行 `src/main.py`
会因为 `from src.config import ...` 找不到包而失败。

定时运行：GitHub Actions 每天 UTC 13:00 一次（北京时间 21:00，
多伦多夏令时 09:00、冬令时 08:00），也可手动 `workflow_dispatch` 触发。

## 策略要点（src/config.py 为准）

- 加密：CoinGecko 市值前 50 → Binance USDT 现货、日线、2020-01-01 起，
  手续费 0.1% / 边、滑点 0.05% / 边，单笔风险 1%、单资产上限 25%。
- 美股：维基百科 S&P 500 成分 → 市值前 20、yfinance 日线、2020-01-01 起，
  佣金 0、滑点 0.05% / 边，单笔风险 1%、单资产上限 25%。
- 趋势：ADX >= 25、EMA20 > EMA50、收盘价高于 EMA20。
- 均值回归：ADX < 25、RSI < 30、收盘价低于布林下轨。
- 出场：止损 2.0×ATR、止盈 3.0×ATR、追踪止损 1.5×ATR、60 天强制平仓兜底。
- 只做多，不做空。Sharpe 计算的无风险利率默认 0。

## 输出

- `results/trades_crypto.csv`、`results/trades_us_stock.csv`
- `results/equity_curve_<市场>_<标的>.csv`
- `results/crypto_universe_snapshot.csv`、`results/us_stock_dynamic_top20_snapshot.csv`
- `results/us_stock_final_watchlist_merged.csv`
- `results/run_summary_<run_id>.json`
- `results/logs/run_<日期>.log`

工作流只提交 `results/`，不提交 `data/` 的原始 OHLCV 和 SQLite 数据库，
避免每天全量提交撑大仓库。数据库 `data/quant_research.db` 由首次运行创建，
本地留存即可。

每次运行都从 2020 年起全量拉取历史数据，不读本地缓存增量，
因此首轮和每日运行都较重，工作流 timeout 设为 60 分钟。

## 已知限制（部署时实测）

- **ARK 链接已失效（2026-10-22 实测）：** 配置里的
  `ark-funds.com/.../ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv` 跳转后返回 404，
  ARK 层第一轮会为空，程序会记为 partial_failure 但不中断加密和美股回测。
  需人工核实到 ARK 当前官方地址后再更新 `src/config.py` 的 `ARKConfig`，
  不要编造新链接。
- **SEC 13F 默认不启用：** `SEC13FConfig.target_ciks` 默认是空的，
  需用户自行从 SEC EDGAR 官方全文检索确认目标机构的真实 CIK 后手动填入，
  不得编造。未配置时 13F 层直接跳过，不是失败。
- **幸存者 / 前视偏差：** 用“当前”市值前 50 / 前 20 做历史回测，
  无法还原每个历史时间点的真实排名，结果只能作为固定池历史参考，
  不能当作无偏历史 Top 池的结论。
- **数据源脆弱：** 维基百科 S&P 500 表、ARK 官方 CSV、SEC EDGAR 解析
  只要对方改版就可能失败，单个标的 / 数据源失败时程序会记录并跳过，
  不让整个程序崩溃，但两大市场资产池都失败时退出码为非零。
- **SEC User-Agent：** `SEC_USER_AGENT` 默认是占位邮箱
  `quant-research-project contact@example.com`，生产运行必须通过
  Actions Secret `SEC_USER_AGENT` 覆盖为真实联系邮箱，
  不要把真实邮箱写进公开仓库。

## 本仓库的修复记录（相对原交付文本）

原交付文本 `6-claude.txt` 拆分后有 5 个模块因重复 / 截断片段导致
`py_compile` 失败，已做机械修复（删没写完的第一份、留完整的第二份，
不改策略逻辑）：`backtest.py`、`database.py`、`metrics.py`、
`sec_13f.py`、`main.py`；`common.py` 原文本出现两次，
采用后一份改进版（logger 去重、run_id 带 uuid、429 / 5xx / 4xx 分开处理）。
修复后全部模块 `py_compile` 通过，并用合成日线数据做过回测冒烟测试
（能出交易、能算指标、能建库），合成数据不代表策略有效。
原文本缺 tests、README、工作流，本仓库补了 README 和工作流，tests 未补。
