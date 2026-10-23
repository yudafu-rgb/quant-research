"""
backtest.py
单资产历史回测引擎。

重要声明：本模块对每一个资产（加密货币交易对或美股代码）独立运行回测，
使用该资产专属的初始资金假设进行模拟。**这是"单资产独立回测"**，
不是统一账户、统一风险预算下的组合回测。如果把多个标的的权益曲线
简单相加或平均，并不代表真实的组合绩效（因为那样会隐含"每个标的都
各自拥有一份完整初始资金"的不现实假设）。本项目当前版本未实现
跨标的统一资金账户的组合回测，README中会明确说明这一限制。

回测纪律（严格遵守）：
1. 信号只能使用"当时已经走完"的K线（指标和信号计算全部基于收盘后数据）。
2. 收盘后确认的入场信号，最早在下一根K线的开盘价成交（不允许"信号当根
   K线收盘价"成交，避免未来函数）。
3. 持仓期间的止盈止损判断，只使用该根K线自身的OHLC（不使用未来K线）。
4. 若同一根K线内，最高价触及止盈、最低价同时也触及止损，采用保守规则：
   止损优先成交（无法得知盘中具体先后顺序时，假设最坏情况）。
5. 手续费和滑点在入场和出场时都会分别计入，影响实际成交价格和现金流。
6. 数据存在缺口（例如美股因停牌、退市导致某段时间无数据）时，本回测
   引擎会按实际存在的数据行顺序处理，不会伪造缺失的交易日，也不会
   跨越缺口做出任何"插值"假设。
7. 回测区间结束时若仍有未平仓仓位，会在结果中明确标记为"未平仓"，
   并可选地按最后一根K线收盘价强制平仓用于计算最终权益（会计入
   平仓时的手续费和滑点），同时在交易记录中标注退出原因为
   'forced_close_at_end'。
8. 美股数据来自yfinance，只包含其返回的真实交易日，不会把周末或
   休市日伪造成交易日。
9. 单个资产处理失败（数据错误、计算异常）会被上层调用方（main.py）
   捕获并记录，不会导致其它资产的回测被中断。
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
import pandas as pd
import numpy as np

from src.indicators import compute_all_indicators
from src.strategy import (
    generate_entry_signals,
    compute_initial_stop_and_target,
    update_trailing_stop,
    check_exit_conditions,
    calculate_position_size,
)
from src.common import get_logger

logger = get_logger("backtest")


@dataclass
class CostAssumptions:
    fee_pct: float
    slippage_pct: float

    def apply_buy_cost(self, price: float) -> float:
        """买入时的实际成交价（滑点使买入价更不利，即更高）。"""
        return price * (1 + self.slippage_pct)

    def apply_sell_cost(self, price: float) -> float:
        """卖出时的实际成交价（滑点使卖出价更不利，即更低）。"""
        return price * (1 - self.slippage_pct)

    def fee_amount(self, notional: float) -> float:
        return abs(notional) * self.fee_pct


@dataclass
class TradeRecord:
    symbol: str
    market: str
    strategy_type: str
    entry_time: pd.Timestamp
    entry_price: float
    quantity: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    is_win: Optional[bool] = None
    holding_bars: Optional[int] = None
    is_open: bool = True


@dataclass
class BacktestResult:
    symbol: str
    market: str
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: pd.DataFrame = None
    final_equity: float = 0.0
    initial_capital: float = 0.0
    open_position_at_end: bool = False
    data_points_used: int = 0
    error: Optional[str] = None


def _close_trade(
    trade: TradeRecord,
    exit_time,
    exit_price_raw: float,
    exit_reason: str,
    costs: CostAssumptions,
    is_sell_side: bool = True,
):
    """
    结算一笔交易：计算考虑滑点后的实际出场价、出场手续费、
    毛利润和净利润。
    """
    actual_exit_price = costs.apply_sell_cost(exit_price_raw) if is_sell_side else exit_price_raw
    exit_notional = actual_exit_price * trade.quantity
    exit_fee = costs.fee_amount(exit_notional)

    entry_notional = trade.entry_price * trade.quantity
    gross_pnl = (actual_exit_price - trade.entry_price) * trade.quantity
    net_pnl = gross_pnl - trade.entry_fee - exit_fee

    trade.exit_time = exit_time
    trade.exit_price = actual_exit_price
    trade.exit_reason = exit_reason
    trade.exit_fee = exit_fee
    trade.gross_pnl = gross_pnl
    trade.net_pnl = net_pnl
    trade.is_win = net_pnl > 0
    trade.is_open = False
    return trade


def run_single_asset_backtest(
    df: pd.DataFrame,
    symbol: str,
    market: str,
    strategy_cfg,
    initial_capital: float,
    fee_pct: float,
    slippage_pct: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
    force_close_open_position_at_end: bool = True,
) -> BacktestResult:
    """
    对单一资产执行完整的历史回测。

    参数：
    - df: 原始OHLCV数据，列至少包含 datetime, open, high, low, close, volume，
          必须按datetime升序排列，且datetime应为已完成K线的时间戳
          （即该行数据代表的K线周期已经结束）。
    - symbol: 资产代码（如 'BTC/USDT' 或 'AAPL'）
    - market: 'crypto' 或 'us_stock'
    - strategy_cfg: StrategyConfig 实例
    - initial_capital: 该资产独立回测所使用的初始资金假设
    - fee_pct, slippage_pct: 手续费和滑点比例
    - risk_per_trade_pct, max_position_pct: 风险仓位控制参数
    - force_close_open_position_at_end: 回测结束时是否按最后收盘价强制平仓
      （计入手续费滑点）。若False，则该笔仓位保留为"未平仓"状态，
      不计入已实现盈亏统计。

    返回 BacktestResult。若发生不可恢复的异常，result.error会被设置，
    其余字段可能为空，调用方应检查error字段。
    """
    if df is None or len(df) == 0:
        return BacktestResult(
            symbol=symbol, market=market, initial_capital=initial_capital,
            error="输入数据为空，无法回测。"
        )

    try:
        data = df.copy().reset_index(drop=True)
        data = data.sort_values("datetime").reset_index(drop=True)

        data = compute_all_indicators(data, strategy_cfg)
        data = generate_entry_signals(data, strategy_cfg)

        costs = CostAssumptions(fee_pct=fee_pct, slippage_pct=slippage_pct)

        cash = initial_capital
        position_qty = 0.0
        open_trade: Optional[TradeRecord] = None
        stop_loss_price = None
        take_profit_price = None
        trailing_stop_price = None
        holding_bars = 0

        trades: List[TradeRecord] = []
        equity_rows = []

        pending_entry_strategy_type = None  # 上一根K线收盘后确认、本根K线开盘执行

        n = len(data)
        for i in range(n):
            row = data.iloc[i]

            # ---------- 1. 若有挂起的入场信号，在本根K线开盘价成交 ----------
            if pending_entry_strategy_type is not None and position_qty == 0:
                entry_price_raw = row["open"]
                entry_atr = row["atr"]

                sl, tp = compute_initial_stop_and_target(
                    entry_price=entry_price_raw,
                    entry_atr=entry_atr,
                    strategy_type=pending_entry_strategy_type,
                    strategy_cfg=strategy_cfg,
                )

                if sl is not None:
                    qty = calculate_position_size(
                        equity=cash,
                        entry_price=entry_price_raw,
                        stop_loss_price=sl,
                        risk_per_trade_pct=risk_per_trade_pct,
                        max_position_pct=max_position_pct,
                    )

                    if qty > 0:
                        actual_entry_price = costs.apply_buy_cost(entry_price_raw)
                        entry_notional = actual_entry_price * qty
                        entry_fee = costs.fee_amount(entry_notional)

                        if entry_notional + entry_fee <= cash:
                            cash -= (entry_notional + entry_fee)
                            position_qty = qty
                            open_trade = TradeRecord(
                                symbol=symbol,
                                market=market,
                                strategy_type=pending_entry_strategy_type,
                                entry_time=row["datetime"],
                                entry_price=actual_entry_price,
                                quantity=qty,
                                entry_fee=entry_fee,
                            )
                            stop_loss_price = sl
                            take_profit_price = tp
                            trailing_stop_price = None
                            holding_bars = 0
                        else:
                            logger.warning(
                                f"{symbol}: 资金不足以按计算仓位开仓，跳过该信号。"
                            )
                    # qty<=0 表示风险不可控或资金不足，跳过本次信号
                # sl is None 表示ATR无效，跳过本次信号

                pending_entry_strategy_type = None

            # ---------- 2. 若持仓中，检查本根K线是否触发退出 ----------
            if position_qty > 0 and open_trade is not None:
                holding_bars += 1

                if open_trade.strategy_type == "trend":
                    trailing_stop_price = update_trailing_stop(
                        current_trailing_stop=trailing_stop_price,
                        current_close=row["close"],
                        current_atr=row["atr"],
                        strategy_cfg=strategy_cfg,
                    )

                should_exit, exit_reason = check_exit_conditions(
                    row=row,
                    strategy_type=open_trade.strategy_type,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    trailing_stop_price=trailing_stop_price,
                    strategy_cfg=strategy_cfg,
                    holding_days=holding_bars,
                )

                if should_exit:
                    if exit_reason == "stop_loss":
                        effective_stop = stop_loss_price
                        if trailing_stop_price is not None and not pd.isna(trailing_stop_price):
                            effective_stop = (
                                max(stop_loss_price, trailing_stop_price)
                                if stop_loss_price is not None
                                else trailing_stop_price
                            )
                        exit_price_raw = effective_stop
                    elif exit_reason == "take_profit":
                        exit_price_raw = take_profit_price
                    elif exit_reason == "mid_band_touch":
                        exit_price_raw = row["bb_middle"]
                    else:
                        # trend_invalid / rsi_recovered / max_holding_days
                        # 均按本根K线收盘价成交（因为是基于收盘后状态判断的退出）
                        exit_price_raw = row["close"]

                    open_trade.holding_bars = holding_bars
                    _close_trade(
                        open_trade,
                        exit_time=row["datetime"],
                        exit_price_raw=exit_price_raw,
                        exit_reason=exit_reason,
                        costs=costs,
                        is_sell_side=True,
                    )

                    exit_notional = open_trade.exit_price * open_trade.quantity
                    cash += exit_notional - open_trade.exit_fee

                    trades.append(open_trade)
                    open_trade = None
                    position_qty = 0.0
                    stop_loss_price = None
                    take_profit_price = None
                    trailing_stop_price = None
                    holding_bars = 0

            # ---------- 3. 若空仓，检查本根K线收盘后是否产生新信号 ----------
            if position_qty == 0 and pending_entry_strategy_type is None:
                if bool(row.get("entry_signal", False)):
                    pending_entry_strategy_type = row["entry_strategy_type"]

            # ---------- 4. 记录当根K线结束时的权益（mark-to-market） ----------
            if position_qty > 0:
                market_value = position_qty * row["close"]
            else:
                market_value = 0.0
            equity = cash + market_value
            equity_rows.append({
                "datetime": row["datetime"],
                "cash": cash,
                "position_qty": position_qty,
                "market_value": market_value,
                "equity": equity,
            })

        # ---------- 回测结束：处理未平仓仓位 ----------
        open_position_at_end = position_qty > 0

        if open_position_at_end and force_close_open_position_at_end and open_trade is not None:
            last_row = data.iloc[-1]
            open_trade.holding_bars = holding_bars
            _close_trade(
                open_trade,
                exit_time=last_row["datetime"],
                exit_price_raw=last_row["close"],
                exit_reason="forced_close_at_end",
                costs=costs,
                is_sell_side=True,
            )
            exit_notional = open_trade.exit_price * open_trade.quantity
            cash += exit_notional - open_trade.exit_fee
            trades.append(open_trade)

            if equity_rows:
                equity_rows[-1]["cash"] = cash
                equity_rows[-1]["position_qty"] = 0.0
                equity_rows[-1]["market_value"] = 0.0
                equity_rows[-1]["equity"] = cash

            position_qty = 0.0
            open_position_at_end = False

        elif open_position_at_end and open_trade is not None:
            # 保留为未平仓，不计入已实现盈亏，但在trades列表中以is_open=True记录，
            # 供报告层明确标注"未平仓"状态，不计入胜率/盈亏统计。
            open_trade.holding_bars = holding_bars
            trades.append(open_trade)

        equity_df = pd.DataFrame(equity_rows)
        final_equity = equity_df["equity"].iloc[-1] if len(equity_df) > 0 else initial_capital

        return BacktestResult(
            symbol=symbol,
            market=market,
            trades=trades,
            equity_curve=equity_df,
            final_equity=final_equity,
            initial_capital=initial_capital,
            open_position_at_end=open_position_at_end,
            data_points_used=n,
            error=None,
        )

    except Exception as e:
        logger.error(f"{symbol} 回测过程发生异常: {e}")
        return BacktestResult(
            symbol=symbol,
            market=market,
            initial_capital=initial_capital,
            error=str(e),
        )


def trades_to_dataframe(trades: List[TradeRecord]) -> pd.DataFrame:
    """
    将TradeRecord列表转换为DataFrame，便于写入CSV和数据库。
    未平仓交易（is_open=True）的exit相关字段将为None/NaN，
    报告和指标计算层需要据此过滤，不能把未平仓交易当作已实现盈亏统计。
    """
    rows = []
    for t in trades:
        rows.append({
            "symbol": t.symbol,
            "market": t.market,
            "strategy_type": t.strategy_type,
            "entry_time": t.entry_time,
            "entry_price": t.entry_price,
            "quantity": t.quantity,
            "exit_time": t.exit_time,
            "exit_price": t.exit_price,
            "exit_reason": t.exit_reason,
            "entry_fee": t.entry_fee,
            "exit_fee": t.exit_fee,
            "gross_pnl": t.gross_pnl,
            "net_pnl": t.net_pnl,
            "is_win": t.is_win,
            "holding_bars": t.holding_bars,
            "is_open": t.is_open,
        })
    if not rows:
        return pd.DataFrame(columns=[
            "symbol", "market", "strategy_type", "entry_time", "entry_price",
            "quantity", "exit_time", "exit_price", "exit_reason", "entry_fee",
            "exit_fee", "gross_pnl", "net_pnl", "is_win", "holding_bars", "is_open",
        ])
    return pd.DataFrame(rows)
