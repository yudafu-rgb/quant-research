"""
csmom.py
横截面动量（Cross-Sectional Momentum, CSMOM）组合回测模块。

与现有趋势/均值回归策略（strategy.py + backtest.py，按单资产独立本金回测）
并列的组合级策略，区别在于：

- 组合级统一账户：全部资产共享同一笔初始资金，不再是每只资产各用
  一份完整本金再把曲线拼接（那种拼接曲线不是可执行组合，见 README 已知限制）。
- 每月再平衡：每月最后一个交易日收盘后，按过去 lookback_days 个交易日
  的累计收益对资产池排序，取前 top_n 只等权持有；其余资产权重为 0。
- 只做多、不做空（allow_short=False，当前实现不含做空逻辑，与项目
  现货多头口径一致）。
- 执行纪律（与 backtest.py 一致）：信号在收盘后确认，最早在下一根
  K 线开盘成交（T+1 开盘），不假设在信号收盘价成交。
- 成本：与 backtest.py 的 CostAssumptions 同一口径，单边手续费 fee_pct
  加单边滑点 slippage_pct。组合再平衡的成本按换手名义额计算：
  cost = sum(|目标权重 - 当前权重|) * 调仓前组合价值 * (fee_pct + slippage_pct)，
  其中 sum(|Δ权重|) 同时包含买入腿和卖出腿的名义额，因此 cost 已包含
  双边交易的手续费与滑点，是一阶等价的保守近似（与逐笔按成交价加滑点
  的 backtest.py 口径在数量级上一致，差异仅来自开盘价与收盘价的价差）。
- 单只上限：等权权重若超过 max_position_pct，则该只按上限持有，
  剩余资金留作现金（不挪给其他标的，避免权重定义含糊）。
- 热身期：信号日之前必须至少有 lookback_days 根历史 K 线，否则该月
  不调仓、保持现金；缺失价格的标的不参与当月排序（不向前填补造数据，
  仅对已持有标的的日内市值按最近可用收盘价计算，即 forward-fill 仅用于
  市值展示，不用于信号排序）。

幸存者偏差警告（必须读）：
- 本模块本身不解决资产池偏差。日常流水线传入的资产池是“当前市值前 N”
  （见 us_stock_data.py 的已知限制），用它跑历史区间，本质上是用今天的
  赢家名单回测过去，结果会被高估，只能作为观察池研究，不能宣称为
  无偏的历史回测。
- 无偏的研究版本在 2026-10-23 已用 2020-01-01 时点成分股单独验证
  （本地 backtrader/gpt7/，未随日常流水线运行）：时点 Top20 池
  （2020-01-02 收盘价 × 当前股本的代理市值排序）横截面动量
  （126 日、前 5 只、月调仓、quant 成本 0.1%+0.05%/边）全期
  +247.01%，训练（至 2022 年底）+38.16%，样本外（2023 年起）+151.22%，
  双倍成本仍 +223.85%；时点全池（451 只、前 10 只）全期 +221.07%。
  日常流水线跑的是当前池版本，数字与上述时点版本不可直接比较。

本模块不执行真实交易，仅做公开数据历史回测模拟。
"""

import numpy as np
import pandas as pd

from src.backtest import CostAssumptions
from src.common import get_logger
from src.metrics import (
    compute_annualized_volatility,
    compute_cagr,
    compute_max_drawdown,
    compute_sharpe_ratio,
)

logger = get_logger("csmom")


def run_csmom_portfolio_backtest(ohlcv_data, csmom_cfg):
    """对一组 OHLCV 数据做横截面动量组合回测。

    参数：
    - ohlcv_data: dict[str, DataFrame]，每个 DataFrame 至少含
      datetime, open, close 列，按 datetime 升序排列，且每行代表
      一根已完成 K 线。
    - csmom_cfg: CSMOMConfig 实例（见 src/config.py）。

    返回 dict，含：
    - equity_df: DataFrame[datetime, equity]
    - weights_df: DataFrame[rebalance_date, symbol, weight, signal_date]
    - metrics: dict（总收益、CAGR（365 日口径，见 metrics.py）、
      交易日年化（252 日口径，便于与本地研究对比）、最大回撤、
      Sharpe（CAGR 口径与均值口径各一）、调仓次数、总成本、换手）
    - skipped: list[str]，因缺失开盘价而某月未成交的记录
    - error: str | None，失败时非空且其余字段为空
    """
    symbols = sorted(ohlcv_data.keys())
    if not symbols:
        return {"error": "资产池为空，无法运行横截面动量回测。"}

    close_map = {}
    open_map = {}
    for sym in symbols:
        df = ohlcv_data[sym]
        if df is None or len(df) == 0:
            continue
        df = df.sort_values("datetime").drop_duplicates(subset=["datetime"])
        idx = pd.to_datetime(df["datetime"])
        close_map[sym] = pd.Series(df["close"].values, index=idx, dtype=float)
        open_map[sym] = pd.Series(df["open"].values, index=idx, dtype=float)

    symbols = sorted(close_map.keys())
    if not symbols:
        return {"error": "全部资产 OHLCV 数据为空，无法运行横截面动量回测。"}

    close_df = pd.DataFrame(close_map).sort_index()
    open_df = pd.DataFrame(open_map).sort_index()
    # 市值展示用 forward-fill（仅用于持仓市值，不用于信号排序）
    close_ffill = close_df.ffill()

    costs = CostAssumptions(
        fee_pct=csmom_cfg.fee_pct, slippage_pct=csmom_cfg.slippage_pct
    )
    cost_per_side = csmom_cfg.fee_pct + csmom_cfg.slippage_pct
    lookback = int(csmom_cfg.lookback_days)
    top_n = int(csmom_cfg.top_n)
    max_w = float(csmom_cfg.max_position_pct)

    # 月末信号日：每月最后一个交易日（按 close_df 的日期索引分组）
    periods = close_df.index.to_period("M")
    signal_positions = []
    for _, group in pd.DataFrame({"pos": range(len(close_df))}, index=close_df.index).groupby(periods):
        signal_positions.append(int(group["pos"].iloc[-1]))
    signal_positions = [p for p in signal_positions if p >= lookback]
    exec_map = {p + 1: p for p in signal_positions if p + 1 < len(close_df)}

    cash = float(csmom_cfg.initial_capital_usd)
    shares = {s: 0.0 for s in symbols}
    equity_values = []
    weight_rows = []
    skipped = []
    total_cost = 0.0
    turnover_sum = 0.0
    n_rebalances = 0

    for i in range(len(close_df)):
        if i in exec_map:
            sig = exec_map[i]
            # 排序：仅用信号日及之前已完成的数据，缺失者不参与
            mom = {}
            for s in symbols:
                c_now = close_df.iloc[sig][s]
                c_old = close_df.iloc[sig - lookback][s]
                if pd.notna(c_now) and pd.notna(c_old) and c_old != 0:
                    mom[s] = float(c_now / c_old - 1.0)
            ranked = sorted(mom.keys(), key=lambda s: mom[s], reverse=True)[:top_n]
            raw_w = 1.0 / top_n if top_n > 0 else 0.0
            w_each = min(raw_w, max_w)
            target = {s: w_each for s in ranked}
            # 当前组合在执行日开盘价下的价值与权重；开盘价缺失时用
            # 前一交易日收盘价估算（不用当日收盘价，避免引入当日未来信息）
            def _exec_price(s):
                px = open_df.iloc[i][s]
                if pd.notna(px):
                    return float(px)
                if i > 0 and pd.notna(close_ffill.iloc[i - 1][s]):
                    return float(close_ffill.iloc[i - 1][s])
                return None

            value_before = cash + sum(
                shares[s] * _exec_price(s)
                for s in symbols
                if shares[s] > 0 and _exec_price(s) is not None
            )
            if value_before <= 0:
                logger.warning("横截面动量组合价值<=0，停止调仓。")
                break
            current_w = {}
            for s in symbols:
                px = _exec_price(s)
                current_w[s] = (shares[s] * px / value_before) if px is not None else 0.0
            traded = sum(abs(target.get(s, 0.0) - current_w[s]) for s in symbols)
            cost = traded * value_before * cost_per_side
            total_cost += cost
            turnover_sum += traded
            n_rebalances += 1
            value_after = value_before - cost
            new_shares = {}
            for s in symbols:
                if s in target:
                    px = open_df.iloc[i][s]
                    if pd.isna(px):
                        skipped.append(
                            f"{close_df.index[i].date()} {s} 缺失开盘价，该月权重留作现金"
                        )
                        new_shares[s] = 0.0
                        continue
                    new_shares[s] = target[s] * value_after / px
                else:
                    new_shares[s] = 0.0
            shares = new_shares
            # 现金 = 调仓后价值 - 已按目标权重买入的市值（缺失开盘价者未买入，自动留作现金）
            invested = sum(
                shares[s] * open_df.iloc[i][s]
                for s in symbols
                if shares[s] > 0 and pd.notna(open_df.iloc[i][s])
            )
            cash = value_after - invested
            for s in ranked:
                weight_rows.append({
                    "rebalance_date": str(close_df.index[i].date()),
                    "signal_date": str(close_df.index[sig].date()),
                    "symbol": s,
                    "weight": target[s],
                    "momentum_lookback_days": lookback,
                })
        # 当日收盘市值（缺失收盘价用最近可用价）
        equity = cash + sum(
            shares[s] * close_ffill.iloc[i][s]
            for s in symbols
            if shares[s] > 0 and pd.notna(close_ffill.iloc[i][s])
        )
        equity_values.append(equity)

    equity_series = pd.Series(equity_values, index=close_df.index, dtype=float)
    if len(equity_series) < 2 or equity_series.iloc[0] <= 0:
        return {"error": "权益曲线不足，无法计算横截面动量指标。"}

    initial = float(csmom_cfg.initial_capital_usd)
    final = float(equity_series.iloc[-1])
    total_return = final / initial - 1.0
    num_calendar_days = (equity_series.index[-1] - equity_series.index[0]).days
    cagr_365 = compute_cagr(initial, final, float(num_calendar_days)) if num_calendar_days > 0 else None
    # 交易日年化（便于与本地研究对比，本地年化按 252 个交易日计）
    n_bars = len(equity_series)
    annual_trading = (final / initial) ** (252.0 / n_bars) - 1.0 if n_bars > 0 else None
    max_dd = compute_max_drawdown(equity_series)
    ann_vol = compute_annualized_volatility(equity_series, bars_per_year=252)
    sharpe_cagr = compute_sharpe_ratio(cagr_365, ann_vol, 0.02) if cagr_365 is not None else None
    rets = equity_series.pct_change().fillna(0.0)
    sharpe_mean = (
        float((rets.mean() * 252 - 0.02) / (rets.std() * np.sqrt(252)))
        if rets.std() > 0 else None
    )

    metrics = {
        "initial_capital": initial,
        "final_equity": final,
        "total_return_pct": round(float(total_return * 100), 2),
        "cagr_pct_365d": round(float(cagr_365 * 100), 2) if cagr_365 is not None else None,
        "annual_pct_trading_days_252d": round(float(annual_trading * 100), 2) if annual_trading is not None else None,
        "max_drawdown_pct": round(float(max_dd * 100), 2),
        "annualized_volatility_pct": round(float(ann_vol * 100), 2) if ann_vol is not None else None,
        "sharpe_ratio_cagr_based": round(float(sharpe_cagr), 3) if sharpe_cagr is not None else None,
        "sharpe_ratio_mean_based": round(float(sharpe_mean), 3) if sharpe_mean is not None else None,
        "risk_free_rate_annual": 0.02,
        "num_rebalances": n_rebalances,
        "total_trading_costs": round(float(total_cost), 2),
        "turnover_sum": round(float(turnover_sum), 3),
        "avg_turnover_per_rebalance": round(float(turnover_sum / n_rebalances), 3) if n_rebalances else None,
        "start_date": str(equity_series.index[0].date()),
        "end_date": str(equity_series.index[-1].date()),
        "num_bars": int(n_bars),
        "lookback_days": lookback,
        "top_n": top_n,
        "max_position_pct": max_w,
        "fee_pct": csmom_cfg.fee_pct,
        "slippage_pct": csmom_cfg.slippage_pct,
        "allow_short": False,
        "note": (
            "组合级统一账户、月末信号次日开盘成交、只做多。日常流水线资产池为"
            "当前市值前 N，历史回测存在幸存者偏差（见模块 docstring）；"
            "无偏时点版本见 README 横截面动量一节。"
        ),
    }

    return {
        "equity_df": pd.DataFrame({"datetime": equity_series.index, "equity": equity_series.values}),
        "weights_df": pd.DataFrame(weight_rows),
        "metrics": metrics,
        "skipped": skipped,
        "error": None,
    }
