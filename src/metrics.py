"""
metrics.py
绩效指标计算模块。

设计原则：
- 所有指标在交易数量不足、数据不足、全部盈利或全部亏损等边界情况下，
  必须返回明确的、可解释的默认值（如0、None或带说明的字符串），
  不得抛出未处理异常或输出误导性的NaN让使用者误判。
- 指标定义（必须与README中的文字说明保持一致）：
    总收益率 = (期末权益 / 期初权益) - 1
    CAGR（年化复合增长率）= (期末权益/期初权益)^(365/实际天数) - 1
    最大回撤 = 权益曲线从历史最高点回落的最大比例
    胜率 = 盈利交易数 / 已平仓交易总数
    平均盈利 = 所有盈利交易净盈亏的平均值
    平均亏损 = 所有亏损交易净盈亏的平均值（负数）
    平均盈亏比 = 平均盈利 / |平均亏损|
    单笔平均期望盈亏 = 所有已平仓交易净盈亏的平均值
    年化波动率 = 日收益率标准差 * sqrt(252)（美股）或 sqrt(365)（加密货币，
                 因为加密货币市场全年无休市）
    Sharpe比率 = (年化收益率 - 无风险利率) / 年化波动率
                （无风险利率假设在 src/config.py 的 RISK_FREE_RATE_ANNUAL 中配置，
                 默认0.0，代表简化假设，不代表真实无风险利率）
"""

from dataclasses import dataclass, asdict
from typing import Optional
import numpy as np
import pandas as pd


@dataclass
class PerformanceMetrics:
    symbol: str
    market: str
    total_return_pct: Optional[float] = None
    cagr_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    num_trades: int = 0
    num_closed_trades: int = 0
    num_open_trades: int = 0
    win_rate_pct: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    avg_win_loss_ratio: Optional[float] = None
    expectancy_per_trade: Optional[float] = None
    annualized_volatility_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    risk_free_rate_annual: float = 0.0
    buy_and_hold_total_return_pct: Optional[float] = None
    excess_return_vs_buy_and_hold_pct: Optional[float] = None
    total_trading_costs: Optional[float] = None
    initial_capital: Optional[float] = None
    final_equity: Optional[float] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    num_bars: int = 0
    open_position_at_end: bool = False
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def compute_max_drawdown(equity_series: pd.Series) -> float:
    """
    计算最大回撤（以正数百分比表示，如 0.25 表示回撤25%）。
    equity_series长度不足2时返回0.0。
    """
    if equity_series is None or len(equity_series) < 2:
        return 0.0
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max.replace(0, np.nan)
    max_dd = drawdown.min()
    if pd.isna(max_dd):
        return 0.0
    return abs(float(max_dd))


def compute_cagr(initial_capital: float, final_equity: float, num_days: float) -> Optional[float]:
    """
    计算年化复合增长率。
    - initial_capital <= 0 或 num_days <= 0 时返回None（数据不足，无法计算）。
    - final_equity <= 0 时（理论上不应发生，除非全部亏光）返回 -1.0（即-100%），
      代表本金归零，这是真实且应被报告的结果，而不是隐藏为NaN。
    """
    if initial_capital is None or initial_capital <= 0 or num_days is None or num_days <= 0:
        return None
    if final_equity <= 0:
        return -1.0
    total_return_multiple = final_equity / initial_capital
    years = num_days / 365.0
    if years <= 0:
        return None
    cagr = total_return_multiple ** (1.0 / years) - 1.0
    return float(cagr)


def compute_annualized_volatility(equity_series: pd.Series, bars_per_year: int) -> Optional[float]:
    """
    基于权益曲线的日/逐K线收益率计算年化波动率。
    数据点不足2个时返回None（无法计算标准差）。
    """
    if equity_series is None or len(equity_series) < 2:
        return None
    returns = equity_series.pct_change().dropna()
    if len(returns) < 2:
        return None
    std = returns.std(ddof=1)
    if pd.isna(std):
        return None
    return float(std * np.sqrt(bars_per_year))


def compute_sharpe_ratio(
    cagr: Optional[float],
    annualized_volatility: Optional[float],
    risk_free_rate_annual: float,
) -> Optional[float]:
    """
    Sharpe比率 = (年化收益率 - 无风险利率) / 年化波动率。
    若年化波动率为None、0或CAGR为None，返回None（无法计算），
    不返回inf或NaN误导使用者。
    """
    if cagr is None or annualized_volatility is None or annualized_volatility == 0:
        return None
    return float((cagr - risk_free_rate_annual) / annualized_volatility)


def compute_trade_based_metrics(trades_df: pd.DataFrame) -> dict:
    """
    基于逐笔交易记录计算：胜率、平均盈利、平均亏损、盈亏比、期望值、
    已平仓/未平仓交易数、总交易成本。

    trades_df 应包含列：net_pnl, is_win, is_open, entry_fee, exit_fee
    （未平仓交易的net_pnl/is_win应为NaN/None，本函数会自动过滤）。

    边界情况处理：
    - 无任何交易：全部指标返回0或None，并在note中说明。
    - 有交易但全部未平仓（closed=0）：同上。
    - 全部盈利（无亏损交易）：avg_loss返回None（不除零），
      avg_win_loss_ratio也返回None，并在结果note中说明"无亏损样本，
      盈亏比无法计算"。
    - 全部亏损（无盈利交易）：avg_win返回None，avg_win_loss_ratio返回None，
      并说明"无盈利样本"。
    """
    result = {
        "num_trades": 0,
        "num_closed_trades": 0,
        "num_open_trades": 0,
        "win_rate_pct": None,
        "avg_win": None,
        "avg_loss": None,
        "avg_win_loss_ratio": None,
        "expectancy_per_trade": None,
        "total_trading_costs": None,
        "note": None,
    }

    if trades_df is None or len(trades_df) == 0:
        result["note"] = "无任何交易记录（可能是数据不足或信号在回测区间内未触发），各交易类指标不适用。"
        return result

    result["num_trades"] = len(trades_df)

    closed = trades_df[trades_df["is_open"] == False].copy()
    open_trades = trades_df[trades_df["is_open"] == True].copy()

    result["num_closed_trades"] = len(closed)
    result["num_open_trades"] = len(open_trades)

    total_fees = trades_df["entry_fee"].fillna(0).sum() + trades_df["exit_fee"].fillna(0).sum()
    result["total_trading_costs"] = float(total_fees)

    if len(closed) == 0:
        result["note"] = "所有交易均处于未平仓状态，无法计算胜率/盈亏比等已实现绩效指标。"
        return result

    wins = closed[closed["net_pnl"] > 0]
    losses = closed[closed["net_pnl"] < 0]
    breakeven = closed[closed["net_pnl"] == 0]

    result["win_rate_pct"] = float(len(wins) / len(closed) * 100.0)
    result["expectancy_per_trade"] = float(closed["net_pnl"].mean())

    notes = []

    if len(wins) > 0:
        result["avg_win"] = float(wins["net_pnl"].mean())
    else:
        notes.append("无盈利交易样本，平均盈利无法计算。")

    if len(losses) > 0:
        result["avg_loss"] = float(losses["net_pnl"].mean())
    else:
        notes.append("无亏损交易样本，平均亏损无法计算。")

    if result["avg_win"] is not None and result["avg_loss"] is not None and result["avg_loss"] != 0:
        result["avg_win_loss_ratio"] = float(abs(result["avg_win"] / result["avg_loss"]))
    else:
        notes.append("因缺少盈利或亏损样本（或平均亏损为0），平均盈亏比无法计算。")

    if len(breakeven) > 0:
        notes.append(f"另有{len(breakeven)}笔交易净盈亏恰好为0，计入已平仓交易但不计入盈利或亏损样本。")

    if notes:
        result["note"] = " ".join(notes)

    return result


def compute_buy_and_hold_return(df: pd.DataFrame) -> Optional[float]:
    """
    计算买入并持有基准的总收益率：使用数据首行开盘价买入，
    末行收盘价卖出（不计手续费滑点，仅作为基准参照，README中会说明
    这是简化的基准，不代表真实可执行的买入持有交易成本）。
    数据不足2行时返回None。
    """
    if df is None or len(df) < 2:
        return None
    first_price = df["open"].iloc[0]
    last_price = df["close"].iloc[-1]
    if first_price is None or pd.isna(first_price) or first_price <= 0:
        return None
    return float((last_price / first_price) - 1.0)


def compute_performance_metrics(
    symbol: str,
    market: str,
    equity_curve: pd.DataFrame,
    trades_df: pd.DataFrame,
    initial_capital: float,
    raw_price_df: pd.DataFrame,
    risk_free_rate_annual: float,
    open_position_at_end: bool,
    bars_per_year: Optional[int] = None,
) -> PerformanceMetrics:
    """
    汇总计算单一资产的全部绩效指标。

    bars_per_year: 若为None，自动根据market判断：
        'crypto' -> 365（全年无休市）
        'us_stock' -> 252（美股年均交易日数的常用近似值）
    """
    if bars_per_year is None:
        bars_per_year = 365 if market == "crypto" else 252

    metrics = PerformanceMetrics(
        symbol=symbol,
        market=market,
        initial_capital=initial_capital,
        risk_free_rate_annual=risk_free_rate_annual,
        open_position_at_end=open_position_at_end,
    )

    if equity_curve is None or len(equity_curve) == 0:
        metrics.note = "权益曲线为空，数据可能不足，无法计算收益类指标。"
        trade_metrics = compute_trade_based_metrics(trades_df)
        for k, v in trade_metrics.items():
            setattr(metrics, k, v)
        return metrics

    equity_series = equity_curve["equity"]
    final_equity = float(equity_series.iloc[-1])
    metrics.final_equity = final_equity

    start_dt = equity_curve["datetime"].iloc[0]
    end_dt = equity_curve["datetime"].iloc[-1]
    metrics.start_date = str(start_dt)
    metrics.end_date = str(end_dt)
    metrics.num_bars = len(equity_curve)

    num_days = (pd.to_datetime(end_dt) - pd.to_datetime(start_dt)).total_seconds() / 86400.0

    if initial_capital is not None and initial_capital > 0:
        metrics.total_return_pct = float((final_equity / initial_capital - 1.0) * 100.0)
    else:
        metrics.total_return_pct = None

    cagr = compute_cagr(initial_capital, final_equity, num_days)
    metrics.cagr_pct = cagr * 100.0 if cagr is not None else None

    metrics.max_drawdown_pct = float(compute_max_drawdown(equity_series) * 100.0)

    annualized_vol = compute_annualized_volatility(equity_series, bars_per_year)
    metrics.annualized_volatility_pct = (
        annualized_vol * 100.0 if annualized_vol is not None else None
    )

    sharpe = compute_sharpe_ratio(cagr, annualized_vol, risk_free_rate_annual)
    metrics.sharpe_ratio = sharpe

    bh_return = compute_buy_and_hold_return(raw_price_df)
    metrics.buy_and_hold_total_return_pct = (
        bh_return * 100.0 if bh_return is not None else None
    )

    if metrics.total_return_pct is not None and metrics.buy_and_hold_total_return_pct is not None:
        metrics.excess_return_vs_buy_and_hold_pct = (
            metrics.total_return_pct - metrics.buy_and_hold_total_return_pct
        )
    else:
        metrics.excess_return_vs_buy_and_hold_pct = None

    trade_metrics = compute_trade_based_metrics(trades_df)
    for k, v in trade_metrics.items():
        setattr(metrics, k, v)

    if cagr is None and metrics.note is None:
        metrics.note = "持仓时间过短或数据不足，CAGR无法计算。"

    return metrics


def metrics_to_flat_rows(metrics: PerformanceMetrics, run_id: str) -> list:
    """
    将 PerformanceMetrics 转换为适合写入数据库 `metrics` 表的
    扁平化行列表：每个指标名称对应一行。

    返回每行字典包含：run_id, market, symbol, metric_name, metric_value,
                      period_start, period_end
    说明性文字字段（note）不计入数值型metric_value，单独以字符串形式存储
    （metric_value为None时，调用方应在数据库层允许该字段为NULL）。
    """
    d = metrics.to_dict()
    non_metric_keys = {"symbol", "market", "start_date", "end_date", "note"}
    rows = []
    for key, value in d.items():
        if key in non_metric_keys:
            continue
        rows.append({
            "run_id": run_id,
            "market": metrics.market,
            "symbol": metrics.symbol,
            "metric_name": key,
            "metric_value": value if isinstance(value, (int, float)) and not isinstance(value, bool) else (
                1.0 if value is True else (0.0 if value is False else None)
            ),
            "period_start": metrics.start_date,
            "period_end": metrics.end_date,
        })
    if metrics.note:
        rows.append({
            "run_id": run_id,
            "market": metrics.market,
            "symbol": metrics.symbol,
            "metric_name": "note",
            "metric_value": None,
            "period_start": metrics.start_date,
            "period_end": metrics.end_date,
        })
    return rows
