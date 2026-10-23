"""
database.py
SQLite 数据持久化模块。

表结构设计（与README保持一致）：

1. runs
   记录每次运行的元信息：运行ID、开始/结束时间、代码/策略版本、
   配置摘要、状态（running/success/partial_failure/failed）、错误信息。

2. market_data_metadata
   记录每个资产每次数据抓取的元信息：资产、市场、时间范围、数据源、
   采集时间、数据更新时间。

3. watchlist_members
   记录观察池成员：资产、市场、观察池来源标签
   （us_dynamic_top20 / ark_daily / 13f_quarterly_reference /
     crypto_top_n_dynamic / default_symbols 等）、抓取时间、报告期
   （适用于13F）、申报日期（适用于13F）、来源说明。

4. trades
   逐笔交易记录：市场、资产、策略信号来源（trend/range）、
   观察池来源标签、入场/出场时间、入场/出场价格、数量、费用、滑点、
   单笔盈亏、胜负、持仓时长、退出原因。

5. metrics
   绩效指标：运行ID、市场、资产或'PORTFOLIO'、指标名称、指标值、
   统计区间（period_start, period_end）。

幂等性设计：
- trades表使用 (run_id, symbol, market, entry_time, exit_time) 作为
  唯一约束，防止同一次run_id下重复插入完全相同的交易记录。
- market_data_metadata 和 watchlist_members 以 (run_id, symbol, market,
  source) 作为唯一键，避免重复运行写入冗余的重复行。
- metrics 表以 (run_id, market, symbol, metric_name) 作为唯一键。
- 所有写入操作均在单个事务内完成；任一步失败会整体回滚，不会留下
  半写入的脏数据。
"""

import sqlite3
import json
import contextlib
from datetime import datetime, timezone

from src.config import DB_PATH
from src.common import get_logger

logger = get_logger("database")


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        code_version TEXT,
        config_summary TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_data_metadata (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        market TEXT NOT NULL,
        start_date TEXT,
        end_date TEXT,
        num_rows INTEGER,
        data_source TEXT,
        fetched_at TEXT,
        data_updated_at TEXT,
        UNIQUE(run_id, symbol, market, data_source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        market TEXT NOT NULL,
        source_tag TEXT NOT NULL,
        fetched_at TEXT,
        report_date TEXT,
        filing_date TEXT,
        source_detail TEXT,
        UNIQUE(run_id, symbol, market, source_tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        market TEXT NOT NULL,
        symbol TEXT NOT NULL,
        strategy_type TEXT,
        watchlist_source_tag TEXT,
        entry_time TEXT,
        entry_price REAL,
        exit_time TEXT,
        exit_price REAL,
        quantity REAL,
        entry_fee REAL,
        exit_fee REAL,
        slippage_pct REAL,
        net_pnl REAL,
        is_win INTEGER,
        holding_bars INTEGER,
        exit_reason TEXT,
        is_open INTEGER,
        UNIQUE(run_id, symbol, market, entry_time, exit_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        market TEXT NOT NULL,
        symbol TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        metric_value REAL,
        period_start TEXT,
        period_end TEXT,
        UNIQUE(run_id, market, symbol, metric_name)
    )
    """,
]


@contextlib.contextmanager
def get_connection():
    """
    获取SQLite连接的上下文管理器，自动开启外键约束，
    使用完毕后自动关闭。首次调用时如果数据库文件不存在，
    sqlite3会自动创建空文件；表结构由 init_db() 显式创建。
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """
    初始化数据库：创建全部表（若不存在）。
    应在main.py运行开始时调用一次。
    """
    with get_connection() as conn:
        cur = conn.cursor()
        for stmt in SCHEMA_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
    logger.info(f"数据库初始化完成，路径: {DB_PATH}")


def create_run(run_id: str, code_version: str, config_summary: dict) -> None:
    """
    在runs表中插入一条新的运行记录，状态初始为'running'。
    若run_id已存在（理论上不应发生，run_id应为唯一生成），
    使用INSERT OR IGNORE避免抛出异常中断主流程。
    """
    started_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO runs (run_id, started_at, code_version, config_summary, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (run_id, started_at, code_version, json.dumps(config_summary, ensure_ascii=False)),
        )
        conn.commit()


def finalize_run(run_id: str, status: str, error_message: str = None) -> None:
    """
    更新runs表中的运行结束时间、最终状态和错误信息（如有）。
    status应为 'success' / 'partial_failure' / 'failed' 之一。
    """
    ended_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE runs SET ended_at = ?, status = ?, error_message = ?
            WHERE run_id = ?
            """,
            (ended_at, status, error_message, run_id),
        )
        conn.commit()


def insert_market_data_metadata(run_id: str, rows: list) -> None:
    """
    rows: list of dict，每个dict包含键：
      symbol, market, start_date, end_date, num_rows, data_source,
      fetched_at, data_updated_at
    使用 INSERT OR IGNORE 配合唯一约束实现幂等；同一run_id重复调用
    不会产生重复行。
    """
    if not rows:
        return
    with get_connection() as conn:
        cur = conn.cursor()
        for r in rows:
            cur.execute(
                """
                INSERT OR IGNORE INTO market_data_metadata
                (run_id, symbol, market, start_date, end_date, num_rows,
                 data_source, fetched_at, data_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, r.get("symbol"), r.get("market"),
                    r.get("start_date"), r.get("end_date"), r.get("num_rows"),
                    r.get("data_source"), r.get("fetched_at"), r.get("data_updated_at"),
                ),
            )
        conn.commit()


def insert_watchlist_members(run_id: str, rows: list) -> None:
    """
    rows: list of dict，每个dict包含键：
      symbol, market, source_tag, fetched_at, report_date, filing_date,
      source_detail
    """
    if not rows:
        return
    with get_connection() as conn:
        cur = conn.cursor()
        for r in rows:
            cur.execute(
                """
                INSERT OR IGNORE INTO watchlist_members
                (run_id, symbol, market, source_tag, fetched_at,
                 report_date, filing_date, source_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, r.get("symbol"), r.get("market"), r.get("source_tag"),
                    r.get("fetched_at"), r.get("report_date"), r.get("filing_date"),
                    r.get("source_detail"),
                ),
            )
        conn.commit()


def insert_trades(run_id: str, market: str, trades_df, watchlist_source_tag: str = None) -> None:
    """
    将 backtest.trades_to_dataframe() 产生的 trades_df 写入数据库。
    trades_df 的列应与 src/backtest.py 中 trades_to_dataframe() 的输出一致。

    使用 (run_id, symbol, market, entry_time, exit_time) 唯一约束实现幂等：
    同一run_id下重复调用不会产生重复交易记录。

    未平仓交易（is_open=True）的exit_time为None；SQLite的UNIQUE约束
    对包含NULL的列组合视为互不相等，因此理论上未平仓交易重复插入
    仍可能产生重复行。这里额外在应用层做一次按(symbol, entry_time, is_open)
    的去重保护。
    """
    if trades_df is None or len(trades_df) == 0:
        return

    with get_connection() as conn:
        cur = conn.cursor()
        for _, row in trades_df.iterrows():
            is_open = bool(row.get("is_open"))

            if is_open:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM trades
                    WHERE run_id = ? AND symbol = ? AND market = ?
                      AND entry_time = ? AND is_open = 1
                    """,
                    (run_id, row.get("symbol"), market, str(row.get("entry_time"))),
                )
                exists = cur.fetchone()[0] > 0
                if exists:
                    continue

            cur.execute(
                """
                INSERT OR IGNORE INTO trades
                (run_id, market, symbol, strategy_type, watchlist_source_tag,
                 entry_time, entry_price, exit_time, exit_price, quantity,
                 entry_fee, exit_fee, slippage_pct, net_pnl, is_win,
                 holding_bars, exit_reason, is_open)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    market,
                    row.get("symbol"),
                    row.get("strategy_type"),
                    watchlist_source_tag,
                    str(row.get("entry_time")) if row.get("entry_time") is not None else None,
                    row.get("entry_price"),
                    str(row.get("exit_time")) if row.get("exit_time") is not None else None,
                    row.get("exit_price"),
                    row.get("quantity"),
                    row.get("entry_fee"),
                    row.get("exit_fee"),
                    None,  # slippage_pct 已体现在entry_price/exit_price的实际成交价中，
                           # 此列保留用于未来可能需要单独记录滑点比例的场景
                    row.get("net_pnl"),
                    (1 if row.get("is_win") is True else (0 if row.get("is_win") is False else None)),
                    row.get("holding_bars"),
                    row.get("exit_reason"),
                    1 if is_open else 0,
                ),
            )
        conn.commit()


def insert_metrics(run_id: str, metric_rows: list) -> None:
    """
    metric_rows: list of dict，来自 src/metrics.py 的
    metrics_to_flat_rows() 函数输出，每个dict包含键：
      run_id, market, symbol, metric_name, metric_value,
      period_start, period_end

    使用 (run_id, market, symbol, metric_name) 唯一约束实现幂等。
    """
    if not metric_rows:
        return
    with get_connection() as conn:
        cur = conn.cursor()
        for r in metric_rows:
            cur.execute(
                """
                INSERT OR IGNORE INTO metrics
                (run_id, market, symbol, metric_name, metric_value,
                 period_start, period_end)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.get("run_id"), r.get("market"), r.get("symbol"),
                    r.get("metric_name"), r.get("metric_value"),
                    r.get("period_start"), r.get("period_end"),
                ),
            )
        conn.commit()


def get_all_trades_df(run_id: str = None):
    """
    辅助查询函数：获取trades表中的全部记录，可选按run_id过滤。
    返回pandas DataFrame，供离线分析或测试使用。
    """
    import pandas as pd

    query = "SELECT * FROM trades"
    params = ()
    if run_id is not None:
        query += " WHERE run_id = ?"
        params = (run_id,)

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    return df


def get_all_metrics_df(run_id: str = None):
    """
    辅助查询函数：获取metrics表中的全部记录，可选按run_id过滤。
    """
    import pandas as pd

    query = "SELECT * FROM metrics"
    params = ()
    if run_id is not None:
        query += " WHERE run_id = ?"
        params = (run_id,)

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    return df


def get_watchlist_members_df(run_id: str = None):
    """
    辅助查询函数：获取watchlist_members表中的全部记录，可选按run_id过滤。
    """
    import pandas as pd

    query = "SELECT * FROM watchlist_members"
    params = ()
    if run_id is not None:
        query += " WHERE run_id = ?"
        params = (run_id,)

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    return df
