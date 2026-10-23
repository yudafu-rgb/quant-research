"""
indicators.py
可解释的常见技术指标实现：EMA、SMA、ATR、RSI、布林带、ADX。

设计原则：
- 所有指标均只使用"当前及之前"的数据计算，不使用未来数据，避免未来函数/
  前视偏差（look-ahead bias）。
- 输入的 DataFrame 必须按时间升序排列，且至少包含 open/high/low/close 列
  （部分指标只需要close）。
- 指标在数据长度不足以计算完整窗口时，前面部分会自然产生 NaN，
  这是正常现象，调用方（strategy.py/backtest.py）需要自行处理，
  不能把NaN当作有效信号。
"""

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """简单移动平均线。"""
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    """
    指数移动平均线。
    使用 pandas 的 adjust=False，使其等价于标准的递归EMA公式，
    且只依赖历史数据，不引入未来信息。
    """
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """
    真实波幅 True Range：
    TR = max(high-low, |high-prev_close|, |low-prev_close|)
    第一行由于没有prev_close，TR直接取 high-low。
    """
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """
    平均真实波幅 ATR，使用Wilder平滑法（等价于 alpha = 1/window 的EMA）。
    """
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """
    相对强弱指数 RSI，使用Wilder平滑法。

    边界情况处理：
    - 当某个窗口内 avg_loss=0 且 avg_gain>0 时，RSI定义为100（持续上涨，无下跌）。
    - 当 avg_gain=0 且 avg_loss=0 时（价格完全不变），RSI定义为50（中性），
      避免除零产生NaN或无意义的无穷值。
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()

    rsi_values = pd.Series(index=series.index, dtype=float)

    both_zero = (avg_gain == 0) & (avg_loss == 0)
    loss_zero_gain_pos = (avg_loss == 0) & (avg_gain > 0)
    normal = ~both_zero & ~loss_zero_gain_pos

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_normal = 100 - (100 / (1 + rs))

    rsi_values[normal] = rsi_normal[normal]
    rsi_values[loss_zero_gain_pos] = 100.0
    rsi_values[both_zero] = 50.0
    rsi_values[avg_gain.isna() | avg_loss.isna()] = np.nan

    return rsi_values


def bollinger_bands(series: pd.Series, window: int = 20, num_std: float = 2.0):
    """
    布林带：中轨为SMA，上下轨为中轨 ± num_std * 滚动标准差。
    返回 (middle, upper, lower) 三个Series。
    """
    middle = sma(series, window)
    std = series.rolling(window=window, min_periods=window).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return middle, upper, lower


def adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """
    平均趋向指数 ADX，使用标准Wilder方法：
    1. 计算 +DM 和 -DM
    2. 计算 TR
    3. 对+DM、-DM、TR分别做Wilder平滑
    4. 计算 +DI = 100*smoothed(+DM)/smoothed(TR)，-DI同理
    5. DX = 100 * |+DI - -DI| / (+DI + -DI)
    6. ADX为DX的Wilder平滑
    """
    high = df["high"]
    low = df["low"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    tr = true_range(df)

    smoothed_tr = tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()

    plus_di = 100 * (smoothed_plus_dm / smoothed_tr.replace(0, np.nan))
    minus_di = 100 * (smoothed_minus_dm / smoothed_tr.replace(0, np.nan))

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum

    adx_values = dx.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    return adx_values


def compute_all_indicators(df: pd.DataFrame, strategy_cfg) -> pd.DataFrame:
    """
    汇总函数：在给定的OHLCV DataFrame基础上，追加以下指标列：
    ema_fast, ema_slow, atr, adx, rsi,
    bb_middle, bb_upper, bb_lower

    strategy_cfg 需提供属性：ema_fast, ema_slow, atr_window, adx_window,
                            rsi_window, bollinger_window, bollinger_std

    返回新的DataFrame（不修改原df），行顺序与输入一致。
    调用方在生成交易信号前，应确保指标非NaN的行才参与判断。
    """
    out = df.copy()

    out["ema_fast"] = ema(out["close"], strategy_cfg.ema_fast)
    out["ema_slow"] = ema(out["close"], strategy_cfg.ema_slow)
    out["atr"] = atr(out, strategy_cfg.atr_window)
    out["adx"] = adx(out, strategy_cfg.adx_window)
    out["rsi"] = rsi(out["close"], strategy_cfg.rsi_window)

    bb_mid, bb_up, bb_low = bollinger_bands(
        out["close"], strategy_cfg.bollinger_window, strategy_cfg.bollinger_std
    )
    out["bb_middle"] = bb_mid
    out["bb_upper"] = bb_up
    out["bb_lower"] = bb_low

    return out
