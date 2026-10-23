"""
ark_holdings.py
ARK Invest 每日持仓观察池模块。

重要说明（必须与13F模块严格区分）：
- ARK 官方公开 CSV 反映的是"每日"（通常为前一交易日收盘后更新）持仓快照，
  属于"观察池"数据，不是经过审计的权威申报文件。
- 本模块的数据与 SEC 13F 季度申报数据在时间频率、权威性、滞后性上完全不同，
  不得在数据库或报告中混用同一来源标签。ARK数据标签为 `ark_daily`。
- ARK 官网的CSV下载链接和列结构可能随时变化。本模块在下载或解析失败时，
  会抛出明确异常并提示人工检查官方URL/格式，绝不会静默返回空持仓或
  伪造"数据正常"的假象。
- 配置文件中的URL (src/config.py 的 ARKConfig.fund_csv_urls) 应视为
  "当前已知可用"的链接，需要用户在实际运行失败时自行到ARK官网核实更新。
"""

import io
import pandas as pd

from src.config import ARKConfig
from src.common import get_logger, http_get_with_retry, utc_now_iso

logger = get_logger("ark_holdings")

# ARK CSV 常见列名与标准化列名之间的映射。
# 由于ARK官方CSV列名在历史上出现过变化，这里列出已知的若干种可能写法，
# 解析时按顺序尝试匹配，匹配不到关键列则认为解析失败。
COLUMN_CANDIDATES = {
    "ticker": ["ticker", "stock_ticker", "symbol"],
    "company": ["company", "name", "company_name"],
    "shares": ["shares", "shares_held", "share"],
    "market_value": ["market value ($)", "market_value", "market value", "marketvalue"],
    "weight": ["weight (%)", "weight", "weight(%)"],
    "date": ["date", "as_of_date"],
}


def _find_column(df_columns, candidates):
    normalized = {str(c).strip().lower(): c for c in df_columns}
    for cand in candidates:
        if cand in normalized:
            return normalized[cand]
    return None


def fetch_single_fund_holdings(fund_code: str, csv_url: str) -> pd.DataFrame:
    """
    下载并解析单个ARK基金的每日持仓CSV。
    返回标准化列：fund_code, ticker, company, shares, market_value, weight,
                 report_date, fetched_at, source_url

    若下载失败或CSV结构无法识别关键列（至少需要ticker列），将抛出异常，
    调用方应将该基金标记为失败并记录错误，不能静默忽略。
    """
    resp = http_get_with_retry(csv_url, logger=logger)
    content = resp.content

    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as e:
        raise ValueError(
            f"ARK基金{fund_code} CSV解析失败，官方格式可能已变化，"
            f"请人工检查URL: {csv_url}，原始错误: {e}"
        )

    if df.empty:
        raise ValueError(
            f"ARK基金{fund_code} CSV内容为空，请人工检查官方数据源: {csv_url}"
        )

    ticker_col = _find_column(df.columns, COLUMN_CANDIDATES["ticker"])
    if ticker_col is None:
        raise ValueError(
            f"ARK基金{fund_code} CSV中未找到股票代码列，官方格式可能已变化，"
            f"当前列名: {list(df.columns)}，请人工检查URL: {csv_url}"
        )

    company_col = _find_column(df.columns, COLUMN_CANDIDATES["company"])
    shares_col = _find_column(df.columns, COLUMN_CANDIDATES["shares"])
    mv_col = _find_column(df.columns, COLUMN_CANDIDATES["market_value"])
    weight_col = _find_column(df.columns, COLUMN_CANDIDATES["weight"])
    date_col = _find_column(df.columns, COLUMN_CANDIDATES["date"])

    fetched_at = utc_now_iso()

    out = pd.DataFrame()
    out["ticker"] = df[ticker_col].astype(str).str.strip()
    out["company"] = df[company_col].astype(str).str.strip() if company_col else None
    out["shares"] = pd.to_numeric(df[shares_col], errors="coerce") if shares_col else None
    out["market_value"] = (
        pd.to_numeric(
            df[mv_col].astype(str).str.replace(r"[\$,]", "", regex=True),
            errors="coerce",
        )
        if mv_col else None
    )
    out["weight"] = (
        pd.to_numeric(
            df[weight_col].astype(str).str.replace("%", "", regex=False),
            errors="coerce",
        )
        if weight_col else None
    )
    out["report_date"] = df[date_col].astype(str) if date_col else None

    out = out.dropna(subset=["ticker"])
    out = out[out["ticker"].str.len() > 0]
    out = out[~out["ticker"].str.upper().isin(["NAN", "NONE", ""])]

    out["fund_code"] = fund_code
    out["fetched_at"] = fetched_at
    out["source_url"] = csv_url

    out = out.reset_index(drop=True)
    logger.info(f"ARK基金{fund_code}持仓解析成功，共{len(out)}条持仓。")
    return out[[
        "fund_code", "ticker", "company", "shares", "market_value",
        "weight", "report_date", "fetched_at", "source_url"
    ]]


def fetch_all_ark_holdings(cfg: ARKConfig):
    """
    遍历配置中的所有ARK基金CSV链接，逐一下载解析。
    返回 (holdings_df, errors_dict)

    单个基金解析失败不会影响其它基金的处理，但会在errors_dict中
    明确记录，main.py应将这些错误写入日志和运行记录，不能假装
    所有基金都成功获取。
    """
    all_rows = []
    errors = {}

    for fund_code, url in cfg.fund_csv_urls.items():
        try:
            df = fetch_single_fund_holdings(fund_code, url)
            all_rows.append(df)
        except Exception as e:
            logger.error(f"ARK基金{fund_code}持仓获取失败: {e}")
            errors[fund_code] = str(e)

    if all_rows:
        holdings_df = pd.concat(all_rows, ignore_index=True)
    else:
        holdings_df = pd.DataFrame(columns=[
            "fund_code", "ticker", "company", "shares", "market_value",
            "weight", "report_date", "fetched_at", "source_url"
        ])
        logger.error(
            "所有ARK基金持仓获取均失败，返回空表。"
            "这不代表持仓为空，而是数据获取失败，请检查errors字典并人工核实官方链接。"
        )

    return holdings_df, errors

