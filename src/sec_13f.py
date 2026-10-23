"""
sec_13f.py
SEC EDGAR 13F 季度参考层模块。

重要说明：
- 13F 数据是机构持仓的"季度快照"，且披露存在最长约45天的滞后。
  本模块获取到的任何持仓信息，只能在其"申报日期"（filing_date）之后
  被回测或策略逻辑使用，禁止把未来才披露的信息提前用于历史信号判断。
- 13F 不包含空头仓位、期权完整风险敞口，也不代表基金的全部头寸
  （例如可能不包括某些豁免披露的仓位），只能作为"季度参考层"，
  不能当作每日实时持仓使用。
- 本模块不编造任何CIK（机构的SEC中央索引号）。默认的目标机构列表为空，
  用户必须自行前往 SEC EDGAR 官网 (https://www.sec.gov/cgi-bin/browse-edgar)
  查找并在 src/config.py 的 SEC13FConfig.target_ciks 中配置，格式示例见该文件注释。
- 股票代码（ticker）映射：13F申报中通常只提供证券名称和CUSIP，不直接提供
  交易所代码。本模块不会猜测ticker；若无法通过可靠方式映射，会将
  ticker标记为 "UNMAPPED"，调用方和数据库都应保留该标记，不能冒充
  已确认的代码。
- 所有数据库和结果文件中，来源标签统一使用 `13f_quarterly_reference`，
  与 `ark_daily`、`us_dynamic_top20` 严格区分。
"""

import time
import pandas as pd

from src.config import SEC13FConfig, SEC_USER_AGENT, SEC_MAX_CALLS_PER_SECOND
from src.common import get_logger, http_get_with_retry, utc_now_iso

logger = get_logger("sec_13f")

_last_sec_call_ts = 0.0
_sec_call_interval = 1.0 / max(SEC_MAX_CALLS_PER_SECOND, 1)


def _rate_limit_sec():
    global _last_sec_call_ts
    now = time.time()
    elapsed = now - _last_sec_call_ts
    if elapsed < _sec_call_interval:
        time.sleep(_sec_call_interval - elapsed)
    _last_sec_call_ts = time.time()


def _build_sec_headers():
    """
    构建符合SEC EDGAR要求的请求头。
    SEC要求请求方在User-Agent中提供真实的联系方式（如邮箱），
    本项目通过环境变量 SEC_USER_AGENT 配置，不在代码中硬编码任何
    个人真实身份信息；用户必须自行设置为自己的联系方式。
    """
    return {"User-Agent": SEC_USER_AGENT}


def fetch_filer_submissions(cik: str) -> dict:
    """
    通过 SEC EDGAR submissions API 获取某机构的全部申报记录元数据。
    cik 应为用户在 config 中配置的10位数字字符串（不足位数时会自动补零）。
    返回原始JSON解析后的dict。
    失败时抛出异常，调用方记录日志并跳过该机构。
    """
    cik_padded = str(cik).strip().zfill(10)
    url = SEC13FConfig.edgar_submissions_url_template.format(cik=cik_padded)

    _rate_limit_sec()
    resp = http_get_with_retry(url, headers=_build_sec_headers(), logger=logger)
    return resp.json()


def extract_13f_filing_list(submissions_json: dict) -> pd.DataFrame:
    """
    从submissions JSON中筛选出表单类型为 13F-HR（含修订版）的申报记录。
    返回列：form_type, filing_date, report_date, accession_number
    """
    filings = submissions_json.get("filings", {}).get("recent", {})
    if not filings:
        return pd.DataFrame(columns=[
            "form_type", "filing_date", "report_date", "accession_number"
        ])

    df = pd.DataFrame({
        "form_type": filings.get("form", []),
        "filing_date": filings.get("filingDate", []),
        "report_date": filings.get("reportDate", []),
        "accession_number": filings.get("accessionNumber", []),
    })

    df = df[df["form_type"].astype(str).str.upper().str.startswith("13F")]
    df = df.reset_index(drop=True)
    return df


def fetch_13f_holdings_for_accession(cik: str, accession_number: str) -> pd.DataFrame:
    """
    获取某一次具体13F申报的持仓明细（通过EDGAR的infotable XML/文档索引）。

    注意：SEC 13F 信息表的具体文档结构可能因申报年份、申报人使用的
    填表系统不同而有差异。本实现采用EDGAR提供的申报文档索引JSON，
    尝试定位并解析标准化的 information table；若解析失败，会抛出
    明确异常，不会返回伪造或残缺但标注为"正常"的数据。

    返回列：security_name, cusip, shares, value_usd, ticker（可能为'UNMAPPED'）
    """
    cik_padded = str(cik).strip().zfill(10)
    accession_nodash = accession_number.replace("-", "")
    index_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik_padded}&type=13F&dateb=&owner=include&count=40"
        f"&output=atom"
    )

    # 直接定位标准信息表文档（常见文件名模式），若不存在则需要人工检查。
    doc_url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik_padded)}/"
        f"{accession_nodash}/infotable.xml"
    )

    _rate_limit_sec()
    try:
        resp = http_get_with_retry(doc_url, headers=_build_sec_headers(), logger=logger)
    except Exception as e:
        raise ValueError(
            f"无法获取CIK={cik} accession={accession_number} 的13F信息表文档"
            f"（尝试路径: {doc_url}）。SEC文档路径/格式可能与本实现假设不一致，"
            f"请人工在 {index_url} 核实具体文档结构。原始错误: {e}"
        )

    try:
        tables = pd.read_xml(resp.content)
    except Exception as e:
        raise ValueError(
            f"13F信息表XML解析失败（CIK={cik}, accession={accession_number}）："
            f"{e}，请人工核实文档结构是否变化。"
        )

    if tables is None or tables.empty:
        raise ValueError(
            f"13F信息表为空（CIK={cik}, accession={accession_number}），"
            f"请人工核实。"
        )

    col_map = {}
    for c in tables.columns:
        cl = str(c).lower()
        if "nameofissuer" in cl:
            col_map[c] = "security_name"
        elif "cusip" in cl:
            col_map[c] = "cusip"
        elif "sshprnamt" in cl:
            col_map[c] = "shares"
        elif cl == "value":
            col_map[c] = "value_usd_thousands"

    tables = tables.rename(columns=col_map)
    required = ["security_name", "cusip"]
    for col in required:
        if col not in tables.columns:
            raise ValueError(
                f"13F信息表缺少必要字段 {col}（CIK={cik}, accession={accession_number}），"
                f"文档结构可能与预期不同，请人工核实。"
            )

    out = pd.DataFrame()
    out["security_name"] = tables["security_name"]
    out["cusip"] = tables["cusip"]
    out["shares"] = pd.to_numeric(tables.get("shares"), errors="coerce")
    value_thousands = pd.to_numeric(tables.get("value_usd_thousands"), errors="coerce")
    out["value_usd"] = value_thousands * 1000.0 if value_thousands is not None else None

    # 本实现不猜测ticker。CUSIP到ticker的映射需要额外的可信数据源
    # （例如付费数据库或用户自行维护的映射表），默认标记为UNMAPPED。
    out["ticker"] = "UNMAPPED"

    out = out.reset_index(drop=True)
    return out


def fetch_all_13f_reference_data(cfg: SEC13FConfig):
    """
    遍历配置中的目标机构CIK列表，获取其13F申报列表和持仓明细。

    返回 (holdings_df, errors_dict)
    holdings_df 列：
      filer_name, cik, report_date, filing_date, accession_number,
      security_name, cusip, shares, value_usd, ticker,
      source, fetched_at

    若 cfg.target_ciks 为空字典，直接返回空表并记录警告日志，
    提示用户需要自行配置CIK，不会抛出异常中断整体运行。
    """
    if not cfg.target_ciks:
        logger.warning(
            "SEC13FConfig.target_ciks 为空，未配置任何机构CIK，"
            "跳过13F季度参考层获取。请参考README和SEC EDGAR官网"
            "(https://www.sec.gov/cgi-bin/browse-edgar) 自行查找并配置CIK。"
        )
        return pd.DataFrame(columns=[
            "filer_name", "cik", "report_date", "filing_date",
            "accession_number", "security_name", "cusip", "shares",
            "value_usd", "ticker", "source", "fetched_at"
        ]), {}

    all_rows = []
    errors = {}
    fetched_at = utc_now_iso()

    for filer_name, cik in cfg.target_ciks.items():
        try:
            submissions = fetch_filer_submissions(cik)
            filing_list = extract_13f_filing_list(submissions)

            if filing_list.empty:
                logger.warning(f"{filer_name}(CIK={cik}) 未找到任何13F申报记录。")
                continue

            # 只处理最新的一条申报记录，避免单次运行请求量过大，
            # 且13F本身滞后约45天，季度参考意义已足够。
            latest = filing_list.sort_values("filing_date", ascending=False).iloc[0]

            try:
                holdings = fetch_13f_holdings_for_accession(
                    cik, latest["accession_number"]
                )
            except Exception as e:
                logger.error(
                    f"{filer_name}(CIK={cik}) 持仓明细获取失败: {e}"
                )
                errors[filer_name] = str(e)
                continue

            holdings["filer_name"] = filer_name
            holdings["cik"] = cik
            holdings["report_date"] = latest["report_date"]
            holdings["filing_date"] = latest["filing_date"]
            holdings["accession_number"] = latest["accession_number"]
            holdings["source"] = "13f_quarterly_reference"
            holdings["fetched_at"] = fetched_at

            all_rows.append(holdings)
            logger.info(
                f"{filer_name}(CIK={cik}) 13F持仓获取成功，"
                f"report_date={latest['report_date']}, "
                f"filing_date={latest['filing_date']}, 共{len(holdings)}条持仓。"
            )

        except Exception as e:
            logger.error(f"{filer_name}(CIK={cik}) 13F申报列表获取失败: {e}")
            errors[filer_name] = str(e)

    if all_rows:
        holdings_df = pd.concat(all_rows, ignore_index=True)
    else:
        holdings_df = pd.DataFrame(columns=[
            "filer_name", "cik", "report_date", "filing_date",
            "accession_number", "security_name", "cusip", "shares",
            "value_usd", "ticker", "source", "fetched_at"
        ])

    holdings_df = holdings_df[[
        "filer_name", "cik", "report_date", "filing_date",
        "accession_number", "security_name", "cusip", "shares",
        "value_usd", "ticker", "source", "fetched_at"
    ]]

    return holdings_df, errors


def filter_usable_as_of_date(holdings_df: pd.DataFrame, as_of_date: str) -> pd.DataFrame:
    """
    回测纪律辅助函数：给定某个回测时点 as_of_date（'YYYY-MM-DD'），
    只返回 filing_date <= as_of_date 的13F持仓记录。

    这是防止未来数据泄漏的关键函数：在历史回测的任意时点，
    策略只能"看到"在该时点之前已经公开申报（filing_date已过）的13F信息，
    而不能使用报告期（report_date）更早但申报日期（filing_date）
    还在未来的记录。

    若 holdings_df 为空或缺少 filing_date 列，返回空表。
    """
    if holdings_df is None or holdings_df.empty or "filing_date" not in holdings_df.columns:
        return pd.DataFrame(columns=holdings_df.columns if holdings_df is not None else [])

    df = holdings_df.copy()
    df["filing_date"] = pd.to_datetime(df["filing_date"], errors="coerce")
    cutoff = pd.to_datetime(as_of_date)
    usable = df[df["filing_date"] <= cutoff].copy()
    return usable.reset_index(drop=True)
