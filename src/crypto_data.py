"""
crypto_data.py
加密货币数据模块：
1. 通过 CoinGecko 免费公开接口获取当前市值排名前 N 的币种。
2. 将币种映射到 Binance 现货中实际可用的 USDT 交易对。
3. 通过 ccxt 获取该交易对的历史 OHLCV 日线数据。

重要限制（务必在README中同步说明）：
- 使用"当前"市值前 N 的币种列表进行历史回测，存在幸存者偏差 /
  前视偏差：免费数据源无法还原每一个历史时间点真实的市值前 N 排名。
- 本模块不保证任何交易对在整个回测区间内都有数据；数据不足或映射失败的
  币种会被记录日志并跳过，不会导致整个程序崩溃。
"""

import time
import pandas as pd
import ccxt

from src.config import CryptoConfig, COINGECKO_MAX_CALLS_PER_MINUTE
from src.common import get_logger, http_get_with_retry, utc_now_iso

logger = get_logger("crypto_data")

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

# CoinGecko 币种 symbol 与 Binance 交易对基础资产之间常见的命名差异。
# 此映射表仅覆盖已知的特殊情况，其余币种默认直接使用其 CoinGecko symbol
# 大写形式作为 Binance 基础资产代码尝试匹配。
SYMBOL_OVERRIDES = {
    "btc": "BTC",
    "eth": "ETH",
}

_last_cg_call_ts = 0.0
_cg_call_interval = 60.0 / max(COINGECKO_MAX_CALLS_PER_MINUTE, 1)


def _rate_limit_coingecko():
    global _last_cg_call_ts
    now = time.time()
    elapsed = now - _last_cg_call_ts
    if elapsed < _cg_call_interval:
        time.sleep(_cg_call_interval - elapsed)
    _last_cg_call_ts = time.time()


def fetch_top_n_market_cap_coins(top_n: int) -> pd.DataFrame:
    """
    从 CoinGecko 获取当前市值排名前 top_n 的币种列表。
    返回列：coingecko_id, symbol, name, market_cap_rank, fetched_at
    失败时抛出异常，调用方决定如何处理（例如回退到默认币种池）。
    """
    results = []
    per_page = min(top_n, 250)
    page = 1
    while len(results) < top_n:
        _rate_limit_coingecko()
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false",
        }
        resp = http_get_with_retry(
            COINGECKO_MARKETS_URL, params=params, logger=logger
        )
        batch = resp.json()
        if not batch:
            break
        results.extend(batch)
        page += 1
        if page > 5:
            break

    fetched_at = utc_now_iso()
    rows = []
    for item in results[:top_n]:
        rows.append({
            "coingecko_id": item.get("id"),
            "symbol": (item.get("symbol") or "").lower(),
            "name": item.get("name"),
            "market_cap_rank": item.get("market_cap_rank"),
            "fetched_at": fetched_at,
        })
    df = pd.DataFrame(rows)
    logger.info(f"CoinGecko 获取市值前{top_n}币种成功，实际获取{len(df)}条。")
    return df


def map_to_binance_usdt_symbol(cg_symbol: str, exchange: ccxt.Exchange):
    """
    将 CoinGecko 的币种 symbol 映射为 Binance 现货中的 USDT 交易对，
    例如 'btc' -> 'BTC/USDT'。
    若映射不存在于交易所市场列表中，返回 None。
    """
    base = SYMBOL_OVERRIDES.get(cg_symbol, cg_symbol.upper())
    candidate = f"{base}/USDT"
    if candidate in exchange.markets:
        return candidate
    return None



BINANCE_VISION_PUBLIC_URL = "https://data-api.binance.vision/api/v3"

def create_exchange(exchange_id: str):
    """创建 ccxt 交易所实例。Binance 默认 api.binance.com 在 GitHub Actions
    跑者上返回 451 受限地区，改走 data-api.binance.vision（公开行情端点，
    与 api.binance.com 同一套现货数据，无需 API Key）。"""
    exchange = getattr(ccxt, exchange_id)()
    if exchange_id == "binance":
        try:
            exchange.urls["api"]["public"] = BINANCE_VISION_PUBLIC_URL
        except Exception:
            pass
    return exchange


def _binance_spot_usdt_symbols_via_vision() -> set:
    """直接调用 data-api.binance.vision 的 exchangeInfo，拿到现货 USDT 交易对集合。
    用于 ccxt load_markets 失败时的备用映射，避免退回只有 BTC/ETH 的默认池。"""
    import requests
    resp = requests.get(
        f"{BINANCE_VISION_PUBLIC_URL}/exchangeInfo",
        headers={"User-Agent": "quant-research/0.1.0"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    out = set()
    for s in data.get("symbols", []):
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            out.add(f"{s.get('baseAsset')}/USDT")
    return out


def _fetch_ohlcv_via_vision_direct(symbol: str, since_iso_utc: str) -> pd.DataFrame:
    """直接调用 data-api.binance.vision 的 klines 接口，绕过 ccxt。
    symbol 如 BTC/USDT -> BTCUSDT，interval 1d，分页拉取。"""
    import requests
    base, quote = symbol.split("/")
    pair = f"{base}{quote}"
    # since_iso_utc 如 2020-01-01T00:00:00Z -> ms
    import datetime
    dt = datetime.datetime.fromisoformat(since_iso_utc.replace("Z", "+00:00"))
    start_ms = int(dt.timestamp() * 1000)
    all_rows = []
    cursor = start_ms
    for _ in range(200):
        resp = requests.get(
            f"{BINANCE_VISION_PUBLIC_URL}/klines",
            params={"symbol": pair, "interval": "1d", "startTime": cursor, "limit": 1000},
            headers={"User-Agent": "quant-research/0.1.0"},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_rows.extend(batch)
        last_close_ms = batch[-1][6]
        if last_close_ms <= cursor:
            break
        cursor = last_close_ms + 1
        if len(batch) < 1000:
            break
    if not all_rows:
        raise ValueError(f"{symbol} 直接 klines 接口未返回任何数据")
    df = pd.DataFrame(
        all_rows,
        columns=["timestamp_ms", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"],
    )
    df["datetime"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def build_crypto_universe(cfg: CryptoConfig) -> pd.DataFrame:
    """
    构建加密货币回测资产池：
    - 若 use_dynamic_top_n=False，直接使用 cfg.default_symbols。
    - 若为 True，尝试从 CoinGecko 获取前 top_n 并映射到 Binance USDT 交易对；
      获取失败时记录日志并回退到 default_symbols，不中断整体运行。
    返回 DataFrame 列：symbol, source, coingecko_id, market_cap_rank, fetched_at
    """
    exchange = create_exchange(cfg.exchange_id)
    vision_symbols = None
    try:
        exchange.load_markets()
    except Exception as e:
        logger.error(f"加载{cfg.exchange_id}交易市场失败: {e}")
        # 备用：直接用 vision exchangeInfo 拿交易对集合，不退回默认池
        try:
            vision_symbols = _binance_spot_usdt_symbols_via_vision()
            logger.info(f"vision exchangeInfo 备用映射成功，共{len(vision_symbols)}个 USDT 现货对")
        except Exception as e2:
            logger.error(f"vision exchangeInfo 备用映射也失败: {e2}")
            exchange = None

    if not cfg.use_dynamic_top_n or (exchange is None and vision_symbols is None):
        rows = [{
            "symbol": s,
            "source": "default_symbols",
            "coingecko_id": None,
            "market_cap_rank": None,
            "fetched_at": utc_now_iso(),
        } for s in cfg.default_symbols]
        return pd.DataFrame(rows)

    try:
        cg_df = fetch_top_n_market_cap_coins(cfg.top_n)
    except Exception as e:
        logger.error(f"CoinGecko 获取市值前{cfg.top_n}失败: {e}，回退到默认币种池。")
        rows = [{
            "symbol": s,
            "source": "default_symbols_fallback",
            "coingecko_id": None,
            "market_cap_rank": None,
            "fetched_at": utc_now_iso(),
        } for s in cfg.default_symbols]
        return pd.DataFrame(rows)

    rows = []
    for _, r in cg_df.iterrows():
        if vision_symbols is not None:
            base = SYMBOL_OVERRIDES.get(r["symbol"], r["symbol"].upper())
            binance_symbol = f"{base}/USDT" if f"{base}/USDT" in vision_symbols else None
        else:
            binance_symbol = map_to_binance_usdt_symbol(r["symbol"], exchange)
        if binance_symbol is None:
            logger.warning(
                f"币种 {r['coingecko_id']} ({r['symbol']}) 在 Binance 无对应"
                f" USDT 现货交易对，跳过。"
            )
            continue
        rows.append({
            "symbol": binance_symbol,
            "source": "coingecko_top_n_dynamic",
            "coingecko_id": r["coingecko_id"],
            "market_cap_rank": r["market_cap_rank"],
            "fetched_at": r["fetched_at"],
        })

    if not rows:
        logger.error("动态市值前N币种全部映射失败，回退到默认币种池。")
        rows = [{
            "symbol": s,
            "source": "default_symbols_fallback",
            "coingecko_id": None,
            "market_cap_rank": None,
            "fetched_at": utc_now_iso(),
        } for s in cfg.default_symbols]

    return pd.DataFrame(rows)


def fetch_ohlcv_history(
    symbol: str,
    exchange_id: str,
    timeframe: str,
    since_iso_utc: str,
    logger_override=None,
) -> pd.DataFrame:
    """
    通过 ccxt 从指定交易所获取某交易对自 since_iso_utc 起的全部可用历史日线数据。
    返回列：datetime, open, high, low, close, volume
    若该交易对无数据或请求失败，抛出异常，由调用方决定跳过并记录日志。
    """
    log = logger_override or logger
    try:
        exchange = create_exchange(exchange_id)
        exchange.load_markets()
    except Exception as e:
        log.warning(f"ccxt 加载市场失败，改走 vision 直接 klines 接口: {e}")
        return _fetch_ohlcv_via_vision_direct(symbol, since_iso_utc)

    since_ms = exchange.parse8601(since_iso_utc)
    all_rows = []
    limit = 1000
    max_iterations = 200  # 安全上限，避免死循环

    cursor = since_ms
    for _ in range(max_iterations):
        try:
            batch = exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=cursor, limit=limit
            )
        except Exception as e:
            log.warning(f"ccxt 获取 {symbol} OHLCV 失败，改走 vision 直接 klines 接口: {e}")
            return _fetch_ohlcv_via_vision_direct(symbol, since_iso_utc)

        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts == cursor:
            break
        cursor = last_ts + 1
        time.sleep(exchange.rateLimit / 1000.0)
        if len(batch) < limit:
            break

    if not all_rows:
        raise ValueError(f"{symbol} 未获取到任何OHLCV数据")

    df = pd.DataFrame(
        all_rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"]
    )
    df["datetime"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.drop(columns=["timestamp_ms"])
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime")
    df = df.reset_index(drop=True)
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def fetch_all_crypto_data(cfg: CryptoConfig):
    """
    构建资产池并逐一获取历史数据。
    返回 (universe_df, {symbol: ohlcv_df}, errors_dict)
    单个标的失败不影响其他标的继续处理。
    """
    universe_df = build_crypto_universe(cfg)
    data = {}
    errors = {}

    for _, row in universe_df.iterrows():
        symbol = row["symbol"]
        try:
            df = fetch_ohlcv_history(
                symbol=symbol,
                exchange_id=cfg.exchange_id,
                timeframe=cfg.timeframe,
                since_iso_utc=cfg.start_date_utc,
            )
            if len(df) < 30:
                raise ValueError(f"数据量过少({len(df)}条)，可能上市时间太短")
            data[symbol] = df
            logger.info(f"{symbol} 历史数据获取成功，共{len(df)}条。")
        except Exception as e:
            logger.error(f"{symbol} 数据获取失败，跳过: {e}")
            errors[symbol] = str(e)

    return universe_df, data, errors
