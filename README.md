# quant-research

日线回测与观察池研究项目，不自动下单，不构成投资建议。
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
  另外两个旧路径 `wp-content/uploads/funds-etf-csv/...` 和
  `etfs.ark-funds.com/...` 实测也是 404，ARK 层自动抓取目前会为空，
  程序会记为 partial_failure 但不中断加密和美股回测。
  需人工核实到 ARK 当前官方地址后再更新 `src/config.py` 的 `ARKConfig`，
  不要编造新链接。
- **ARK 当前选股快照（2026-10-22，已加进仓库）：**
  `results/ark_combined_holdings_2026-10-22.csv`，共 124 条，
  六只基金（ARKK / ARKQ / ARKW / ARKG / ARKF / ARKX）合计市值约 135.8 亿美元，
  按合计市值排序，前 10 名：TSLA、SPCX（SpaceX）、TEM、CRCL、NVDA、PLTR、
  COIN、CRSP、OpenAI（无公开代码）、TWST。
  来源是第三方追踪站 ARK Invest Daily Trades Tracker 的 2026-10-22 合计持仓页，
  该站自述其数据来自 ARK 每日持仓文件；这不是 ARK 官方 CSV，
  也不是分基金权重，合计口径不能直接拆成单基金权重。
  下一轮自动运行若 ARK 抓取仍失败，不会覆盖这份带日期的快照；
  若将来 ARK 官方抓取恢复，会另写 `results/ark_holdings_snapshot.csv`。
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

## 2026-10-22 晚第二次修复（首轮运行失败后查到的）

首轮手动运行 run 39889793372 在 28 秒内失败，`Run quant research` 步骤失败，
`Commit results` 被跳过，所以没有任何 results 被提交。根因不是网络，
是 `main.py` 与 `database.py` / `config.py` 对不上：

- `main.py` 调用 `database.create_run(run_id, started_at, ...)`，
  但 `database.create_run` 只接受 `(run_id, code_version, config_summary)`，
  多余的 `started_at` 会直接 TypeError，程序在数据库初始化后立刻 `sys.exit(1)`。
- `main.py` 调用 `database.finalize_run(..., finished_at=...)`，
  但 `finalize_run` 只接受 `(run_id, status, error_message)`，同理会失败。
- `main.py` 大量用 `RESULTS_DIR / "xxx.csv"`，但 `config.py` 里
  `RESULTS_DIR` / `DATA_DIR` 是 `os.path.join` 返回的字符串，
  字符串不能用 `/` 运算，跑到第一张结果表就会 TypeError。
- `main.py` 调用 `database.get_watchlist_members_df(run_id, market="us_stock")`，
  但原函数只接受 `run_id`，合并观察池那步永远报错（被 try 包住，不致命，
  但 `us_stock_final_watchlist_merged.csv` 永远出不来）。

本次已修：`main.py` 改用 `os.path.join`，`create_run` / `finalize_run`
按数据库签名调用，`ark` 的 `source_detail` 由 `fund` 改为 `fund_code`，
`database.get_watchlist_members_df` 增加 `market` 过滤参数。
修复后本地验证 `create_run` / `finalize_run` / `get_watchlist_members_df`
调用均通过，`main.py` 再次 `py_compile` 通过。
注意：这次修复只解决“跑不起来”，不解决 ARK 官方 CSV 404 和
美股 500 只市值逐个抓取耗时长的问题，下一轮仍可能因数据源慢而耗时较长。

## 2026-10-23 第三次修复（第二轮 0 资产假 success 后）

第二轮 run 39892259580 显示 success，但实际 0 个资产处理：
加密 Binance 在 Actions 跑者上 451，美股 Wikipedia 在 Actions 上 403，
ARK 在 Actions 上 403（本机为 404）。本次修法：

- 加密：ccxt Binance 改走 `https://data-api.binance.vision/api/v3`
  （公开行情端点，与 api.binance.com 同一套现货数据，无需 Key），
  并在 ccxt 失败时直接调 vision 的 exchangeInfo / klines 接口作备用，
  load_markets 失败时不再直接退回只有 BTC/ETH 的默认池，
  而是先用 vision exchangeInfo 拿 USDT 现货对集合再映射 CoinGecko 前 50。
  注意：本机 VM 的 Python 请求到 vision 会报 SSL WRONG_VERSION_NUMBER
  （代理问题，curl 能通），所以本机 Python 测 vision 会失败，
  真正的验证在 Actions 第三轮。
- 美股：`common.py` 的 HTTP 默认 User-Agent 改为可识别的
  `quant-research/0.1.0 (research; ...)`（原来是 python-requests 默认 UA，
  Wikipedia 常因此 403）；`us_stock_data.py` 的 S&P 500 成分表
  在 HTML 页面失败时改走 Wikipedia 官方 API parse 接口拿同一页表格，
  本机已验证两种路径都能拿到 503 条。
- success 假阳性：`main.py` 的彻底失败判定改为“0 个资产处理也算彻底失败”，
  一市场为 0 资产记 partial_failure，两市场都为 0 记 failed；
  工作流的 Commit results 加了 `if: always()`，即使主程序 exit 1，
  run_summary 和日志也会被提交，不会像首轮那样看不到结果。
