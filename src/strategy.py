"""
strategy.py
策略信号与仓位管理模块。

策略框架说明：
- 市场状态划分：
    趋势状态（trend）：ADX >= adx_trend_threshold
    震荡状态（range）：ADX <  adx_trend_threshold
- 趋势策略（多头）：EMA_fast > EMA_slow，且收盘价高于EMA_fast，且处于趋势状态。
- 均值回归策略（多头）：RSI < rsi_oversold，且收盘价低于布林下轨，且处于震荡状态。
- 本项目只做多，不做空（allow_short 配置项默认False，当前实现未包含做空逻辑）。

避免未来数据泄漏的关键原则：
- 本模块产生的所有信号列（entry_signal_trend / entry_signal_range /
  exit_signal_trend_invalid 等）均只使用"当前这一根已经走完的K线"的指标值，
  不使用未来数据。
- 信号列本身代表"收盘后确认"的信号。实际回测执行时（backtest.py），
  必须将该信号在下一根K线才允许成交（开盘价），本模块不负责执行时点的推移，
  只负责生成信号。
"""

import numpy as np
import pandas as pd


def classify_market_regime(df: pd.DataFrame, strategy_cfg) -> pd.Series:
    """
    根据ADX判断市场状态。
    返回Series，取值为 'trend' / 'range' / NaN（当ADX尚不可用时）。
    """
    regime = pd.Series(index=df.index, dtype=object)
    valid = df["adx"].notna()
    regime[valid & (df["adx"] >= strategy_cfg.adx_trend_threshold)] = "trend"
    regime[valid & (df["adx"] < strategy_cfg.adx_trend_threshold)] = "range"
    return regime


def generate_entry_signals(df: pd.DataFrame, strategy_cfg) -> pd.DataFrame:
    """
    在带有指标的DataFrame基础上，追加以下列：
    - regime: 'trend' / 'range' / None
    - entry_signal_trend: bool，趋势策略多头入场信号
    - entry_signal_range: bool，均值回归策略多头入场信号
    - entry_signal: bool，只要任一策略触发即为True（同一时间只允许一种策略生效，
      若同时触发，优先采用趋势策略，因为其状态判定条件更明确；这是保守且
      确定性的规则，避免信号冲突歧义）。
    - entry_strategy_type: 'trend' / 'range' / None，记录具体是哪种策略触发。

    所有判断仅依赖该行自身（即"当前已完成K线"）的指标值，不引用未来行。
    """
    out = df.copy()
    out["regime"] = classify_market_regime(out, strategy_cfg)

    indicators_ready = (
        out["ema_fast"].notna()
        & out["ema_slow"].notna()
        & out["adx"].notna()
        & out["rsi"].notna()
        & out["bb_lower"].notna()
    )

    trend_cond = (
        indicators_ready
        & (out["regime"] == "trend")
        & (out["ema_fast"] > out["ema_slow"])
        & (out["close"] > out["ema_fast"])
    )

    range_cond = (
        indicators_ready
        & (out["regime"] == "range")
        & (out["rsi"] < strategy_cfg.rsi_oversold)
        & (out["close"] < out["bb_lower"])
    )

    out["entry_signal_trend"] = trend_cond
    out["entry_signal_range"] = range_cond
    out["entry_signal"] = trend_cond | range_cond

    strategy_type = pd.Series(index=out.index, dtype=object)
    strategy_type[trend_cond] = "trend"
    strategy_type[~trend_cond & range_cond] = "range"
    out["entry_strategy_type"] = strategy_type

    return out


def trend_exit_condition_invalid(row) -> bool:
    """
    趋势退出条件之一：趋势条件失效。
    即 EMA_fast <= EMA_slow，或收盘价跌破EMA_fast。
    row 需包含 ema_fast, ema_slow, close 字段（可以是DataFrame的一行，
    也可以是具有同名属性的对象/Series）。
    """
    if pd.isna(row["ema_fast"]) or pd.isna(row["ema_slow"]):
        return False
    return (row["ema_fast"] <= row["ema_slow"]) or (row["close"] <= row["ema_fast"])


def range_exit_condition_rsi_recovered(row, strategy_cfg) -> bool:
    """
    均值回归退出条件之一：RSI恢复至可配置阈值以上。
    """
    if pd.isna(row["rsi"]):
        return False
    return row["rsi"] >= strategy_cfg.rsi_exit_recovery


def range_exit_condition_mid_band_touch(row) -> bool:
    """
    均值回归退出条件之一：触及布林中轨。
    """
    if pd.isna(row["bb_middle"]):
        return False
    return row["close"] >= row["bb_middle"]


def compute_initial_stop_and_target(
    entry_price: float,
    entry_atr: float,
    strategy_type: str,
    strategy_cfg,
):
    """
    计算入场时刻的初始止损价和止盈价（仅用于趋势策略；
    均值回归策略的止盈以"触及布林中轨"为主，此处仍返回一个ATR止损作为
    兜底风险控制，止盈价返回None表示不使用固定止盈价，由退出条件函数判断）。

    趋势策略：
      stop_loss  = entry_price - atr_stop_loss_mult * entry_atr
      take_profit = entry_price + atr_take_profit_mult * entry_atr

    均值回归策略：
      stop_loss  = entry_price - atr_stop_loss_mult * entry_atr
      take_profit = None（使用布林中轨/RSI恢复作为退出条件）

    若 entry_atr 为 NaN 或 <=0，返回 (None, None)，调用方应将该情况视为
    无法安全开仓并跳过该笔信号（风险不可控）。
    """
    if entry_atr is None or pd.isna(entry_atr) or entry_atr <= 0:
        return None, None

    stop_loss = entry_price - strategy_cfg.atr_stop_loss_mult * entry_atr

    if strategy_type == "trend":
        take_profit = entry_price + strategy_cfg.atr_take_profit_mult * entry_atr
    else:
        take_profit = None

    return stop_loss, take_profit


def update_trailing_stop(
    current_trailing_stop: float,
    current_close: float,
    current_atr: float,
    strategy_cfg,
) -> float:
    """
    趋势策略的ATR追踪止损更新逻辑（仅在持仓期间、针对多头仓位调用）：
    新的追踪止损 = max(当前追踪止损, 当前收盘价 - atr_trailing_stop_mult * 当前ATR)
    即追踪止损只能上移，不能下移，这是标准的多头追踪止损规则。

    若 current_atr 为 NaN，返回原有的 current_trailing_stop 不变。
    """
    if current_atr is None or pd.isna(current_atr):
        return current_trailing_stop

    candidate = current_close - strategy_cfg.atr_trailing_stop_mult * current_atr
    if current_trailing_stop is None:
        return candidate
    return max(current_trailing_stop, candidate)


def calculate_position_size(
    equity: float,
    entry_price: float,
    stop_loss_price: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
) -> float:
    """
    基于ATR止损距离的风险仓位计算（多头）：

    1. 单笔风险金额 = equity * risk_per_trade_pct
    2. 每单位风险 = entry_price - stop_loss_price（必须 > 0，否则无法计算仓位）
    3. 按风险计算的数量 = 单笔风险金额 / 每单位风险
    4. 名义仓位上限 = equity * max_position_pct
    5. 最终数量 = min(按风险计算数量, 名义仓位上限 / entry_price)

    若 entry_price <= stop_loss_price（止损价不低于入场价，风险距离非正），
    或任何输入非法（<=0），返回0.0，表示不开仓，这是保守且安全的处理方式，
    避免产生负数或无穷大的仓位。
    """
    if equity <= 0 or entry_price <= 0:
        return 0.0
    if stop_loss_price is None or pd.isna(stop_loss_price):
        return 0.0
    if entry_price <= stop_loss_price:
        return 0.0

    risk_amount = equity * risk_per_trade_pct
    per_unit_risk = entry_price - stop_loss_price
    qty_by_risk = risk_amount / per_unit_risk

    max_notional = equity * max_position_pct
    qty_by_cap = max_notional / entry_price

    final_qty = min(qty_by_risk, qty_by_cap)
    return max(final_qty, 0.0)


def check_exit_conditions(
    row,
    strategy_type: str,
    stop_loss_price: float,
    take_profit_price: float,
    trailing_stop_price: float,
    strategy_cfg,
    holding_days: int,
):
    """
    检查某一根已完成K线是否触发退出条件（不含成交撮合逻辑，撮合在backtest.py中）。

    返回 (should_exit: bool, exit_reason: str or None)

    退出原因取值：
      'stop_loss'        止损（ATR止损或追踪止损，视哪个先被触发；
                          若同一根K线同时触及止损与止盈，止损优先，
                          这是backtest.py执行层面的规则，这里仅做条件判断）
      'take_profit'       止盈（仅趋势策略有固定止盈价）
      'trend_inval

'take_profit'       止盈（仅趋势策略有固定止盈价）
      'trend_invalid'      趋势条件失效（仅趋势策略）
      'mid_band_touch'     触及布林中轨（仅均值回归策略）
      'rsi_recovered'      RSI恢复至阈值以上（仅均值回归策略）
      'max_holding_days'   达到最大持仓天数（通用兜底规则，避免策略因
                           条件判定异常而无限期持仓不退出）
      None                 不触发任何退出条件

    注意：本函数只负责"条件判断"，不负责判断盘中OHLC具体以哪个价格成交，
    具体成交价格的确定（例如止损是按止损价成交还是按当根K线最低价成交）
    由backtest.py按回测纪律统一处理。
    """
    if strategy_cfg.max_holding_days is not None and holding_days >= strategy_cfg.max_holding_days:
        return True, "max_holding_days"

    if strategy_type == "trend":
        effective_stop = stop_loss_price
        if trailing_stop_price is not None and not pd.isna(trailing_stop_price):
            effective_stop = max(stop_loss_price, trailing_stop_price) if stop_loss_price is not None else trailing_stop_price

        if effective_stop is not None and row["low"] <= effective_stop:
            return True, "stop_loss"

        if take_profit_price is not None and not pd.isna(take_profit_price):
            if row["high"] >= take_profit_price:
                return True, "take_profit"

        if trend_exit_condition_invalid(row):
            return True, "trend_invalid"

        return False, None

    elif strategy_type == "range":
        if stop_loss_price is not None and not pd.isna(stop_loss_price):
            if row["low"] <= stop_loss_price:
                return True, "stop_loss"

        if range_exit_condition_mid_band_touch(row):
            return True, "mid_band_touch"

        if range_exit_condition_rsi_recovered(row, strategy_cfg):
            return True, "rsi_recovered"

        return False, None

    else:
        return False, None
