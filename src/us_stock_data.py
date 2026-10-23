"""
us_stock_data.py
美股数据模块：

1. 从公开的 Wikipedia S&P 500 成分股表格获取候选股票池。
2. 用 yfinance 获取市值数据，排序后选取市值前 N 作为"动态观察池"。
3. 处理 Wikipedia 代码（如 BRK.B）与 Yahoo Finance 代码（BRK-B）之间的格式差异。

重要限制（必须在README中同步说明）：
- 本模块获取的是"当前" S&P 500 成分股列表，不是历史上某一时点的成分股。
  使用当前成分股对历史区间做回测，会产生严重的幸存者偏差：
  历史上已被剔除、退市、合并、破产的公司不会出现在当前名单中。
- 因此本模块产出的"动态市值前20观察池"不能被宣称为无偏的历史市值前20回测，
  只能作为当前时点的观察池/研究对象。
- yfinance 为非官方免费数据接口，可能出现限流、字段缺失或暂时性故障；
  这些情况会被记录日志并尽量跳过对应标的，不会导致整体程序崩溃。
"""

import io
import time
import pandas as pd
import yfinance as yf

from src.config import USStockConfig, HTTP_MAX_RETRIES, HTTP_BACKOFF_BASE_SECONDS
from src.common import get_logger, http_get_with_retry, utc_now_iso

logger = get_logger("us_stock_data")


def fetch_sp500_candidate_table(wikipedia_url: str) -> pd.DataFrame:
    """
    从 Wikipedia 获取 S&P 500 成分股表格。
    返回列：wiki_symbol, security_name, gics_sector
    失败时抛出异常。
    先走 HTML 页面（已带正常 User-Agent，见 common.py 默认 UA），
    若被 403 挡住（GitHub Actions 跑者 IP 常见），再走 Wikipedia 官方 API
    的 parse 接口拿同一页的 HTML 表格解析，两者都是同一页内容。
    """
    try:
        resp = http_get_with_retry(wikipedia_url, logger=logger)
        tables = pd.read_html(io.StringIO(resp.text))
    except Exception as e:
        logger.warning(f"Wikipedia HTML 页面获取失败（{e}），改走官方 API parse 接口")
        api_url = "https://en.wikipedia.org/w/api.php"
        api_resp = http_get_with_retry(
            api_url,
            params={
                "action": "parse",
                "page": "List_of_S&P_500_companies",
                "prop": "text",
                "format": "json",
                "formatversion": "2",
            },
            logger=logger,
        )
        payload = api_resp.json()
        html_text = payload.get("parse", {}).get("text", "")
        if not html_text:
            raise ValueError(f"Wikipedia API 未返回页面 HTML，原始错误: {e}")
        tables = pd.read_html(io.StringIO(html_text))

    target = None
    for t in tables:
        cols_lower = [str(c).lower() for c in t.columns]
        if any("symbol" in c for c in cols_lower):
            target = t
            break

    if target is None:
        raise ValueError("未能在Wikipedia页面中定位到包含Symbol列的成分股表格，"
                          "页面结构可能已变化，请人工检查。")

    col_map = {}
    for c in target.columns:
        cl = str(c).lower()
        if "symbol" in cl:
            col_map[c] = "wiki_symbol"
        elif "security" in cl:
            col_map[c] = "security_name"
        elif "gics sector" in cl or cl == "gics sector":
            col_map[c] = "gics_sector"

    target = target.rename(columns=col_map)
    keep_cols = [c for c in ["wiki_symbol", "security_name", "gics_sector"] if c in target.columns]
    df = target[keep_cols].copy()
    df["wiki_symbol"] = df["wiki_symbol"].astype(str).str.strip()
    df = df.dropna(subset=["wiki_symbol"]).drop_duplicates(subset=["wiki_symbol"])
    df = df.reset_index(drop=True)
    logger.info(f"Wikipedia S&P500候选股票池获取成功，共{len(df)}条。")
    return df


def wiki_symbol_to_yahoo_symbol(wiki_symbol: str) -> str:
    """
    将Wikipedia风格代码转换为Yahoo Finance风格代码。
    例如：BRK.B -> BRK-B, BF.B -> BF-B
    若无点号，直接原样返回。
    """
    return wiki_symbol.replace(".", "-")


def fetch_market_caps(
    yahoo_symbols: list,
    max_retries: int = HTTP_MAX_RETRIES,
    backoff_base: float = HTTP_BACKOFF_BASE_SECONDS,
) -> pd.DataFrame:
    """
    使用yfinance逐个获取股票的市值信息（marketCap字段）。
    返回列：yahoo_symbol, market_cap, fetched_at
    单个标的失败会重试有限次数，最终仍失败则记录为market_cap=None并继续，
    不中断整体流程。
    """
    rows = []
    fetched_at = utc_now_iso()

    for sym in yahoo_symbols:
        market_cap = None
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                ticker = yf.Ticker(sym)
                info = ticker.fast_info
                market_cap = getattr(info, "market_cap", None)
                if market_cap is None:
                    info_full = ticker.info
                    market_cap = info_full.get("marketCap")
                break
            except Exception as e:
                last_err = e
                sleep_s = backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    f"获取 {sym} 市值失败（第{attempt}次）: {e}，{sleep_s:.1f}s后重试"
                )
                time.sleep(sleep_s)
        if market_cap is None:
            logger.error(f"{sym} 市值获取最终失败，记为缺失: {last_err}")

        rows.append({
            "yahoo_symbol": sym,
            "market_cap": market_cap,
            "fetched_at": fetched_at,
        })
        time.sleep(0.5)  # 简单限速，避免触发yfinance限流

    return pd.DataFrame(rows)


def build_us_dynamic_top_n_universe(cfg: USStockConfig) -> pd.DataFrame:
    """
    构建美股"动态市值前N"观察池。
    返回列：symbol, wiki_symbol, security_name, market_cap, market_cap_rank,
           source, fetched_at
    任一环节失败会抛出异常，由调用方（main.py）决定如何处理整体运行状态。
    """
    candidates = fetch_sp500_candidate_table(cfg.wikipedia_sp500_url)
    candidates["yahoo_symbol"] = candidates["wiki_symbol"].apply(wiki_symbol_to_yahoo_symbol)

    cap_df = fetch_market_caps(candidates["yahoo_symbol"].tolist())
    merged = candidates.merge(cap_df, on="yahoo_symbol", how="left")

    valid = merged.dropna(subset=["market_cap"]).copy()
    missing_count = len(merged) - len(valid)
    if missing_count > 0:
        logger.warning(f"{missing_count} 个标的市值缺失，已从排序中剔除。")

    valid = valid.sort_values("market_cap", ascending=False).reset_index(drop=True)
    top_n = valid.head(cfg.top_n).copy()
    top_n["market_cap_rank"] = range(1, len(top_n) + 1)
    top_n["source"] = "us_dynamic_top20"
    top_n = top_n.rename(columns={"yahoo_symbol": "symbol"})

    result = top_n[[
        "symbol", "wiki_symbol", "security_name", "market_cap",
        "market_cap_rank", "source", "fetched_at"
    ]].copy()

    logger.info(f"美股动态市值前{cfg.top_n}观察池构建完成，实际{len(result)}条。")
    return result


def fetch_ohlcv_history(
    symbol: str,
    start_date: str,
    end_date: str = None,
) -> pd.DataFrame:
    """
    使用yfinance获取指定美股代码的历史日线OHLCV数据。
    返回列：datetime, open, high, low, close, volume
    yfinance的 auto_adjust=True 会自动处理拆股和分红调整价格；
    若该标的已退市或代码不存在，yfinance通常返回空表，这里会抛出异常
    由调用方记录日志并跳过。

    限制说明：
    - 对于已经退市、被收购、合并的历史标的，yfinance可能完全无法获取数据，
      这是免费数据源的已知限制，必须在README中说明。
    - 只保留yfinance返回的有效交易日数据，不会伪造周末或休市日为交易日。
    """
    ticker = yf.Ticker(symbol)
    df = ticker.history(
        start=start_date,
        end=end_date,
        auto_adjust=True,
        actions=False,
    )
    if df is None or df.empty:
        raise ValueError(f"{symbol} 未获取到任何历史OHLCV数据（可能已退市或代码错误）")

    df = df.reset_index()
    df = df.rename(columns={
        "Date": "datetime",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    })
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime")
    df = df.reset_index(drop=True)
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def fetch_all_us_stock_data(cfg: USStockConfig):
    """
    构建美股动态观察池并逐一获取历史数据。
    返回 (universe_df, {symbol: ohlcv_df}, errors_dict)
    若观察池构建本身失败（如Wikipedia页面结构变化），异常会向上抛出，
    由main.py决定是否以非零状态退出；单个标的历史数据获取失败则只跳过该标的。
    """
    universe_df = build_us_dynamic_top_n_universe(cfg)

    data = {}
    errors = {}

    for _, row in universe_df.iterrows():
        symbol = row["symbol"]
        try:
            df = fetch_ohlcv_history(symbol, start_date=cfg.start_date)
            if len(df) < 30:
                raise ValueError(f"数据量过少({len(df)}条)")
            data[symbol] = df
            logger.info(f"{symbol} 历史数据获取成功，共{len(df)}条。")
        except Exception as e:
            logger.error(f"{symbol} 数据获取失败，跳过: {e}")
            errors[symbol] = str(e)

    return universe_df, data, errors

