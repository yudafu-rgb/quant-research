"""
main.py
项目主运行入口，整合以下流程：

1. 初始化数据库、生成本次运行的run_id。
2. 加密货币模块：构建资产池、获取OHLCV、运行回测、计算指标。
3. 美股模块：构建动态市值前N观察池、获取OHLCV、运行回测、计算指标。
4. ARK每日持仓：获取并保存，标记来源 ark_daily（不参与回测，只作为观察池）。
5. SEC 13F季度参考层：获取并保存，标记来源 13f_quarterly_reference
   （不参与回测，只作为观察池参考；若用于未来扩展回测，须遵守
   filter_usable_as_of_date的时间纪律）。
6. 合并美股最终观察池（us_dynamic_top20 + ark_daily + 13f_quarterly_reference），
   保留各来源标签，写入数据库和结果文件。
7. 将所有结果写入 results/ 目录（CSV/JSON）和SQLite数据库。
8. 根据各步骤成功/失败情况，决定最终进程退出码：
   - 全部关键步骤成功 -> 0
   - 部分资产/部分数据源失败但核心流程完成 -> 0（记录为partial_failure，
     但不影响已完成部分的产出）
   - 核心流程（如数据库初始化、两个市场的资产池构建全部失败）-> 非零退出

本脚本不执行任何真实交易、不连接券商/交易所账户，仅做公开数据获取、
历史回测模拟与结果记录。
"""

import os
import sys
import json
import traceback
from datetime import datetime, timezone

import pandas as pd

from src.config import (
    RESULTS_DIR,
    DATA_DIR,
    CryptoConfig,
    USStockConfig,
    ARKConfig,
    SEC13FConfig,
    StrategyConfig,
    RISK_FREE_RATE_ANNUAL,
)
from src.common import get_logger, generate_run_id, utc_now_iso
from src import database
from src.crypto_data import fetch_all_crypto_data
from src.us_stock_data import fetch_all_us_stock_data
from src.ark_holdings import fetch_all_ark_holdings
from src.sec_13f import fetch_all_13f_reference_data
from src.backtest import run_single_asset_backtest, trades_to_dataframe
from src.metrics import compute_performance_metrics, metrics_to_flat_rows

logger = get_logger("main")

CODE_VERSION = "0.1.0"


def _save_df_csv(df: pd.DataFrame, filepath):
    try:
        df.to_csv(filepath, index=False)
        logger.info(f"已保存: {filepath}")
    except Exception as e:
        logger.error(f"保存文件失败 {filepath}: {e}")


def _run_market_backtests(
    market_name: str,
    universe_df: pd.DataFrame,
    ohlcv_data: dict,
    fetch_errors: dict,
    strategy_cfg: StrategyConfig,
    initial_capital: float,
    fee_pct: float,
    slippage_pct: float,
    risk_per_trade_pct: float,
    max_position_pct: float,
    run_id: str,
):
    """
    对给定市场的全部标的分别执行"单资产独立回测"，并计算绩效指标。
    返回 (all_trades_df, all_metrics_rows, backtest_errors)
    单个标的回测失败不会中断其它标的的处理。
    """
    all_trades_frames = []
    all_metrics_rows = []
    backtest_errors = dict(fetch_errors)

    for symbol, df in ohlcv_data.items():
        try:
            result = run_single_asset_backtest(
                df=df,
                symbol=symbol,
                market=market_name,
                strategy_cfg=strategy_cfg,
                initial_capital=initial_capital,
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
                risk_per_trade_pct=risk_per_trade_pct,
                max_position_pct=max_position_pct,
                force_close_open_position_at_end=True,
            )

            if result.error is not None:
                logger.error(f"[{market_name}] {symbol} 回测失败: {result.error}")
                backtest_errors[symbol] = result.error
                continue

            trades_df = trades_to_dataframe(result.trades)
            if len(trades_df) > 0:
                all_trades_frames.append(trades_df)

            metrics = compute_performance_metrics(
                symbol=symbol,
                market=market_name,
                equity_curve=result.equity_curve,
                trades_df=trades_df,
                initial_capital=initial_capital,
                raw_price_df=df,
                risk_free_rate_annual=RISK_FREE_RATE_ANNUAL,
                open_position_at_end=result.open_position_at_end,
            )
            metric_rows = metrics_to_flat_rows(metrics, run_id)
            all_metrics_rows.extend(metric_rows)

            equity_path = os.path.join(RESULTS_DIR, f"equity_curve_{market_name}_{symbol.replace('/', '-')}.csv")
            _save_df_csv(result.equity_curve, equity_path)

            logger.info(
                f"[{market_name}] {symbol} 回测完成: "
                f"总收益率={metrics.total_return_pct}, "
                f"交易数={metrics.num_trades}, 胜率={metrics.win_rate_pct}"
            )

        except Exception as e:
            logger.error(f"[{market_name}] {symbol} 回测过程异常: {e}\n{traceback.format_exc()}")
            backtest_errors[symbol] = str(e)

    if all_trades_frames:
        combined_trades_df = pd.concat(all_trades_frames, ignore_index=True)
    else:
        combined_trades_df = pd.DataFrame()

    return combined_trades_df, all_metrics_rows, backtest_errors


def run_crypto_pipeline(run_id: str):
    """
    执行完整的加密货币模块流程。
    返回 (success: bool, summary: dict)
    """
    cfg = CryptoConfig()
    summary = {"market": "crypto", "errors": {}}

    try:
        universe_df, ohlcv_data, fetch_errors = fetch_all_crypto_data(cfg)
    except Exception as e:
        logger.error(f"加密货币资产池/数据获取发生严重错误: {e}\n{traceback.format_exc()}")
        summary["errors"]["universe_build"] = str(e)
        return False, summary

    universe_path = os.path.join(RESULTS_DIR, "crypto_universe_snapshot.csv")
    _save_df_csv(universe_df, universe_path)

    watchlist_rows = []
    metadata_rows = []
    fetched_at = utc_now_iso()
    for _, row in universe_df.iterrows():
        symbol = row["symbol"]
        watchlist_rows.append({
            "symbol": symbol,
            "market": "crypto",
            "source_tag": row.get("source", "unknown"),
            "fetched_at": row.get("fetched_at", fetched_at),
            "report_date": None,
            "filing_date": None,
            "source_detail": f"coingecko_rank={row.get('market_cap_rank')}",
        })
        if symbol in ohlcv_data:
            df = ohlcv_data[symbol]
            metadata_rows.append({
                "symbol": symbol,
                "market": "crypto",
                "start_date": str(df["datetime"].iloc[0]),
                "end_date": str(df["datetime"].iloc[-1]),
                "num_rows": len(df),
                "data_source": f"ccxt_{cfg.exchange_id}",
                "fetched_at": fetched_at,
                "data_updated_at": fetched_at,
            })
            ohlcv_path = os.path.join(DATA_DIR, f"crypto_{symbol.replace('/', '-')}.csv")
            _save_df_csv(df, ohlcv_path)

    database.insert_watchlist_members(run_id, watchlist_rows)
    database.insert_market_data_metadata(run_id, metadata_rows)

    strategy_cfg = StrategyConfig()
    trades_df, metric_rows, backtest_errors = _run_market_backtests(
        market_name="crypto",
        universe_df=universe_df,
        ohlcv_data=ohlcv_data,
        fetch_errors=fetch_errors,
        strategy_cfg=strategy_cfg,
        initial_capital=cfg.initial_capital_usdt,
        fee_pct=cfg.fee_pct,
        slippage_pct=cfg.slippage_pct,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        max_position_pct=cfg.max_position_pct,
        run_id=run_id,
    )

    database.insert_trades(run_id, "crypto", trades_df, watchlist_source_tag="crypto_backtest")
    database.insert_metrics(run_id, metric_rows)

    _save_df_csv(trades_df, os.path.join(RESULTS_DIR, "trades_crypto.csv"))
    if metric_rows:
        with open(os.path.join(RESULTS_DIR, "metrics_crypto.json"), "w", encoding="utf-8") as f:
            json.dump(metric_rows, f, ensure_ascii=False, indent=2, default=str)

    summary["errors"] = backtest_errors
    summary["num_assets_processed"] = len(ohlcv_data)
    summary["num_assets_failed"] = len(backtest_errors)
    return True, summary


def run_us_stock_pipeline(run_id: str):
    """
    执行完整的美股模块流程：动态市值前N观察池构建、回测、
    合并ARK每日持仓与SEC 13F季度参考层到最终观察池。
    返回 (success: bool, summary: dict)
    """
    cfg = USStockConfig()
    summary = {"market": "us_stock", "errors": {}}

    try:
        universe_df, ohlcv_data, fetch_errors = fetch_all_us_stock_data(cfg)
    except Exception as e:
        logger.error(f"美股动态观察池构建发生严重错误: {e}\n{traceback.format_exc()}")
        summary["errors"]["universe_build"] = str(e)
        universe_df = pd.DataFrame()
        ohlcv_data = {}
        fetch_errors = {}

    if len(universe_df) > 0:
        _save_df_csv(universe_df, os.path.join(RESULTS_DIR, "us_stock_dynamic_top20_snapshot.csv"))

    watchlist_rows = []
    metadata_rows = []
    fetched_at = utc_now_iso()

    for _, row in universe_df.iterrows():
        symbol = row["symbol"]
        watchlist_rows.append({
            "symbol": symbol,
            "market": "us_stock",
            "source_tag": "us_dynamic_top20",
            "fetched_at": row.get("fetched_at", fetched_at),
            "report_date": None,
            "filing_date": None,
            "source_detail": f"market_cap_rank={row.get('market_cap_rank')}",
        })
        if symbol in ohlcv_data:
            df = ohlcv_data[symbol]
            # us_stock_data.fetch_ohlcv_history 返回 datetime 列（与加密口径一致，见 crypto 分支），
            # 之前误写 df["date"] 导致 run 4 在 20 只全部取到 1711 条后仍 KeyError: 'date' 整体失败
            date_col = "datetime" if "datetime" in df.columns else "date"
            metadata_rows.append({
                "symbol": symbol,
                "market": "us_stock",
                "start_date": str(df[date_col].iloc[0]),
                "end_date": str(df[date_col].iloc[-1]),
                "num_rows": len(df),
                "data_source": "yfinance",
                "fetched_at": fetched_at,
                "data_updated_at": fetched_at,
            })
            ohlcv_path = os.path.join(DATA_DIR, f"us_stock_{symbol}.csv")
            _save_df_csv(df, ohlcv_path)

    database.insert_market_data_metadata(run_id, metadata_rows)

    strategy_cfg = StrategyConfig()
    trades_df, metric_rows, backtest_errors = _run_market_backtests(
        market_name="us_stock",
        universe_df=universe_df,
        ohlcv_data=ohlcv_data,
        fetch_errors=fetch_errors,
        strategy_cfg=strategy_cfg,
        initial_capital=cfg.initial_capital_usd,
        fee_pct=cfg.fee_pct,
        slippage_pct=cfg.slippage_pct,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        max_position_pct=cfg.max_position_pct,
        run_id=run_id,
    )

    database.insert_trades(run_id, "us_stock", trades_df, watchlist_source_tag="us_stock_backtest")
    database.insert_metrics(run_id, metric_rows)

    _save_df_csv(trades_df, os.path.join(RESULTS_DIR, "trades_us_stock.csv"))
    if metric_rows:
        with open(os.path.join(RESULTS_DIR, "metrics_us_stock.json"), "w", encoding="utf-8") as f:
            json.dump(metric_rows, f, ensure_ascii=False, indent=2, default=str)

    summary["errors"].update(backtest_errors)
    summary["num_assets_processed"] = len(ohlcv_data)
    summary["num_assets_failed"] = len(backtest_errors)

    # ---- ARK 每日持仓（独立于回测，仅作为观察池来源）----
    ark_cfg = ARKConfig()
    try:
        ark_df, ark_errors = fetch_all_ark_holdings(ark_cfg)
    except Exception as e:
        logger.error(f"ARK持仓获取发生严重错误: {e}\n{traceback.format_exc()}")
        ark_df = pd.DataFrame()
        ark_errors = {"ark_fatal": str(e)}

    if len(ark_df) > 0:
        _save_df_csv(ark_df, os.path.join(RESULTS_DIR, "ark_holdings_snapshot.csv"))
        ark_fetched_at = utc_now_iso()
        ark_watchlist_rows = []
        for _, row in ark_df.iterrows():
            ark_watchlist_rows.append({
                "symbol": row.get("ticker"),
                "market": "us_stock",
                "source_tag": "ark_daily",
                "fetched_at": row.get("fetched_at", ark_fetched_at),
                "report_date": None,
                "filing_date": None,
                "source_detail": f"fund={row.get('fund_code')}",
            })
        database.insert_watchlist_members(run_id, ark_watchlist_rows)
    else:
        logger.warning(
            "ARK 持仓数据为空（可能是官方CSV URL或格式已变化），"
            "请人工检查 ARKConfig 中配置的URL与解析逻辑。本次运行不会"
            "伪造ARK持仓数据。"
        )
    summary["ark_errors"] = ark_errors

    # ---- SEC 13F 季度参考层（独立于回测，仅作为观察池参考）----
    sec_cfg = SEC13FConfig()
    try:
        sec_df, sec_errors = fetch_all_13f_reference_data(sec_cfg)
    except Exception as e:
        logger.error(f"SEC 13F数据获取发生严重错误: {e}\n{traceback.format_exc()}")
        sec_df = pd.DataFrame()
        sec_errors = {"sec_fatal": str(e)}

    if len(sec_df) > 0:
        _save_df_csv(sec_df, os.path.join(RESULTS_DIR, "sec_13f_holdings_snapshot.csv"))
        sec_watchlist_rows = []
        for _, row in sec_df.iterrows():
            sec_watchlist_rows.append({
                "symbol": row.get("ticker", "UNMAPPED"),
                "market": "us_stock",
                "source_tag": "13f_quarterly_reference",
                "fetched_at": row.get("fetched_at", utc_now_iso()),
                "report_date": row.get("report_date"),
                "filing_date": row.get("filing_date"),
                "source_detail": f"filer_cik={row.get('cik')}",
            })
        database.insert_watchlist_members(run_id, sec_watchlist_rows)
    else:
        logger.info(
            "SEC 13F 数据为空：可能是因为未配置 CIK 列表（target_ciks为空），"
            "或所有申报获取失败。请参阅 README 配置说明。"
        )
    summary["sec_13f_errors"] = sec_errors

    # ---- 合并最终美股观察池（保留各来源标签，从数据库读出后合并导出）----
    try:
        full_watchlist_df = database.get_watchlist_members_df(run_id=run_id, market="us_stock")
        _save_df_csv(full_watchlist_df, os.path.join(RESULTS_DIR, "us_stock_final_watchlist_merged.csv"))
    except Exception as e:
        logger.error(f"合并最终美股观察池失败: {e}")

    return True, summary


def main():
    run_id = generate_run_id()
    started_at = utc_now_iso()
    logger.info(f"========== 本次运行开始 run_id={run_id} started_at={started_at} ==========")

    overall_status = "success"
    error_message = None
    config_summary = {
        "code_version": CODE_VERSION,
        "risk_free_rate_annual": RISK_FREE_RATE_ANNUAL,
    }

    try:
        database.init_db()
    except Exception as e:
        logger.critical(f"数据库初始化失败，无法继续: {e}\n{traceback.format_exc()}")
        sys.exit(1)

    try:
        database.create_run(
            run_id=run_id,
            code_version=CODE_VERSION,
            config_summary=config_summary,
        )
    except Exception as e:
        logger.critical(f"创建运行记录失败，无法继续: {e}\n{traceback.format_exc()}")
        sys.exit(1)

    run_summaries = {}
    critical_failure = False

    try:
        crypto_success, crypto_summary = run_crypto_pipeline(run_id)
        run_summaries["crypto"] = crypto_summary
        if not crypto_success:
            overall_status = "partial_failure"
    except Exception as e:
        logger.error(f"加密货币模块整体异常: {e}\n{traceback.format_exc()}")
        run_summaries["crypto"] = {"fatal_error": str(e)}
        overall_status = "partial_failure"

    try:
        us_success, us_summary = run_us_stock_pipeline(run_id)
        run_summaries["us_stock"] = us_summary
        if not us_success:
            overall_status = "partial_failure"
    except Exception as e:
        logger.error(f"美股模块整体异常: {e}\n{traceback.format_exc()}")
        run_summaries["us_stock"] = {"fatal_error": str(e)}
        overall_status = "partial_failure"

    # 判断是否两大核心市场模块都彻底失败（关键步骤完全失败才算critical）
    # 2026-10-23 修正：只看 fatal_error / universe_build 会漏掉“0 个资产处理”
    # 的情况（第二轮就是 0 资产却被记成 success），所以 0 资产也算彻底失败。
    crypto_processed = run_summaries.get("crypto", {}).get("num_assets_processed", 0) or 0
    us_processed = run_summaries.get("us_stock", {}).get("num_assets_processed", 0) or 0
    crypto_totally_failed = (
        "fatal_error" in run_summaries.get("crypto", {})
        or "universe_build" in run_summaries.get("crypto", {}).get("errors", {})
        or crypto_processed == 0
    )
    us_totally_failed = (
        "fatal_error" in run_summaries.get("us_stock", {})
        or "universe_build" in run_summaries.get("us_stock", {}).get("errors", {})
        or us_processed == 0
    )
    if (crypto_totally_failed or us_totally_failed) and not (crypto_totally_failed and us_totally_failed):
        overall_status = "partial_failure"
    if crypto_totally_failed and us_totally_failed:
        critical_failure = True
        overall_status = "failed"
        error_message = "加密货币与美股模块核心资产池构建均失败，本次运行视为失败。"

    finished_at = utc_now_iso()
    try:
        database.finalize_run(
            run_id=run_id,
            status=overall_status,
            error_message=error_message,
        )
    except Exception as e:
        logger.error(f"写入运行结束状态失败: {e}")

    summary_path = os.path.join(RESULTS_DIR, f"run_summary_{run_id}.json")
    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "run_id": run_id,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "status": overall_status,
                    "error_message": error_message,
                    "details": run_summaries,
                },
                f,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        logger.info(f"运行汇总已保存: {summary_path}")
    except Exception as e:
        logger.error(f"保存运行汇总文件失败: {e}")

    logger.info(
        f"========== 本次运行结束 run_id={run_id} status={overall_status} "
        f"finished_at={finished_at} =========="
    )

    if critical_failure:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
