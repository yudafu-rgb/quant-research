#!/usr/bin/env python3
"""CSMOM 美股前瞻账本（forward ledger）。

只做一件事：把 quant-research 日常流水线里已经算好的美股 CSMOM
每月信号，记成“选股 -> 下一调仓结算”的前瞻账本，并算胜率。

口径（与 src/csmom.py 回测完全一致，不新编信号、不改回测）：
- 选股 = results/csmom_us_stock_weights.csv 里最新一期信号
  （signal_date 月末、rebalance_date 次一交易日、前 5 只、各 20%、只做多、126 天动量）。
- 入场价 = rebalance_date 当日开盘价（T+1 开盘成交）。开盘价缺失的标的
  在回测里本来就没买入（权重留作现金），账本里记为 skipped，不计入选股。
- 结算价 = 下一期信号的 rebalance_date 当日开盘价；开盘价缺失时
  按回测 _exec_price 的估值口径，用前一交易日收盘价代替。
- 收益 = 结算价 / 入场价 - 1（开盘价对开盘价，成本未扣，账本里明写）。
- 账本只记最新一期，不回填历史：2020 年以来的 75 期是回测历史，
  不是前瞻选股，回填会把样本内成绩冒充成前瞻胜率。
- 胜率 = 已结算选股中收益 > 0 的占比；n < 10 时 insufficient=true，
  看板和简报只写“数据不足”，不宣称模型有效。

运行位置：GitHub 工作流在 python -m src.main 之后、提交 results/ 之前
运行本脚本。价格文件来自本轮流水线刚下载的 data/us_stock_<symbol>.csv
（datetime/open/close 列），账本文件写回 results/csmom_forward_ledger.json，
由工作流原有的 git add results/ 一并提交，下轮跑时从仓库取回续记。
本脚本只用标准库，不依赖 src，避免在 runner 上引入额外依赖。

失败即退出非零：账本没记对时不要静默提交一个错账本；
工作流的提交步骤是 if: always()，本轮回测结果仍会提交，
但任务状态会标失败，便于发现。
"""

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_CSV = os.path.join(ROOT, "results", "csmom_us_stock_weights.csv")
DATA_DIR = os.path.join(ROOT, "data")
LEDGER_JSON = os.path.join(ROOT, "results", "csmom_forward_ledger.json")
MIN_N_FOR_WINRATE = 10


def read_weights(path):
    """返回按 (signal_date, rebalance_date) 分组的权重行。"""
    groups = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            groups[(row["signal_date"], row["rebalance_date"])].append(row)
    return groups


def read_price_series(symbol):
    """返回 {date_str: (open, close)}，日期取 datetime 列的日期部分。

    文件名与流水线一致：data/us_stock_<symbol>.csv。
    """
    path = os.path.join(DATA_DIR, "us_stock_%s.csv" % symbol)
    series = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = str(row["datetime"])[:10]
            try:
                o = float(row["open"]) if row["open"] not in ("", None) else None
            except (TypeError, ValueError):
                o = None
            try:
                c = float(row["close"]) if row["close"] not in ("", None) else None
            except (TypeError, ValueError):
                c = None
            if o is not None or c is not None:
                series[d] = (o, c)
    return series


def exec_price(series, target_date):
    """回测 _exec_price 口径：当日开盘价；缺失则用前一交易日收盘价。

    返回 (price, price_date, used_fallback)。当日开盘和前日收盘
    都拿不到时返回 (None, None, False)。
    """
    dates = sorted(series)
    if target_date in series and series[target_date][0] is not None:
        return series[target_date][0], target_date, False
    earlier = [d for d in dates if d < target_date]
    if earlier:
        prev = earlier[-1]
        if series[prev][1] is not None:
            return series[prev][1], prev, True
    return None, None, False


def main():
    if not os.path.exists(WEIGHTS_CSV):
        print("ERROR: weights file not found: %s" % WEIGHTS_CSV, file=sys.stderr)
        return 1
    groups = read_weights(WEIGHTS_CSV)
    if not groups:
        print("ERROR: weights file is empty", file=sys.stderr)
        return 1
    signal_keys = sorted(groups)
    latest_key = signal_keys[-1]
    latest_signal_date, latest_rebalance_date = latest_key
    latest_rows = groups[latest_key]

    if os.path.exists(LEDGER_JSON):
        with open(LEDGER_JSON, encoding="utf-8") as f:
            ledger = json.load(f)
    else:
        ledger = {"model": "csmom_us_stock", "picks": [], "skipped": []}
    picks = ledger.setdefault("picks", [])
    skipped = ledger.setdefault("skipped", [])
    known = {(p["signal_date"], p["symbol"]) for p in picks}

    # 1. 记录最新一期（去重：同一 signal_date + symbol 只记一次）。
    n_new = 0
    for row in latest_rows:
        symbol = row["symbol"]
        if (latest_signal_date, symbol) in known:
            continue
        try:
            series = read_price_series(symbol)
        except FileNotFoundError:
            skipped.append({
                "signal_date": latest_signal_date,
                "rebalance_date": latest_rebalance_date,
                "symbol": symbol,
                "reason": "price file missing",
            })
            continue
        if latest_rebalance_date in series and series[latest_rebalance_date][0] is not None:
            entry_price = series[latest_rebalance_date][0]
        else:
            # 回测里开盘缺失 = 没买入，记 skipped，不计入选股。
            skipped.append({
                "signal_date": latest_signal_date,
                "rebalance_date": latest_rebalance_date,
                "symbol": symbol,
                "reason": "open price missing on rebalance date",
            })
            continue
        picks.append({
            "signal_date": latest_signal_date,
            "rebalance_date": latest_rebalance_date,
            "symbol": symbol,
            "weight": float(row["weight"]),
            "entry_price": entry_price,
            "entry_price_date": latest_rebalance_date,
            "evaluated": False,
            "exit_signal_date": None,
            "exit_rebalance_date": None,
            "exit_price": None,
            "exit_price_date": None,
            "exit_price_fallback": False,
            "ret": None,
        })
        known.add((latest_signal_date, symbol))
        n_new += 1

    # 2. 结算到期的选股：有更晚一期信号时，用该期的调仓日开盘价结算。
    n_eval = 0
    for p in picks:
        if p["evaluated"]:
            continue
        later = [k for k in signal_keys if k[0] > p["signal_date"]]
        if not later:
            continue
        exit_signal_date, exit_rebalance_date = sorted(later)[0]
        try:
            series = read_price_series(p["symbol"])
        except FileNotFoundError:
            continue
        price, price_date, fallback = exec_price(series, exit_rebalance_date)
        if price is None or p["entry_price"] in (None, 0):
            continue
        p["evaluated"] = True
        p["exit_signal_date"] = exit_signal_date
        p["exit_rebalance_date"] = exit_rebalance_date
        p["exit_price"] = price
        p["exit_price_date"] = price_date
        p["exit_price_fallback"] = fallback
        p["ret"] = price / p["entry_price"] - 1.0
        n_eval += 1

    # 3. 汇总：n < 10 时只报数据不足。
    evaluated = [p for p in picks if p["evaluated"]]
    wins = sum(1 for p in evaluated if p["ret"] is not None and p["ret"] > 0)
    n = len(evaluated)
    avg_ret = (sum(p["ret"] for p in evaluated) / n) if n else None
    ledger["summary"] = {
        "latest_signal_date": latest_signal_date,
        "latest_rebalance_date": latest_rebalance_date,
        "latest_symbols": [r["symbol"] for r in latest_rows],
        "n_picks_total": len(picks),
        "n_pending": len(picks) - n,
        "n_evaluated": n,
        "wins": wins,
        "win_rate": (wins / n) if n >= MIN_N_FOR_WINRATE else None,
        "avg_ret": avg_ret,
        "insufficient": n < MIN_N_FOR_WINRATE,
        "return_basis": "open-to-open price return, costs excluded",
        "holding": "one monthly rebalance cycle (signal month-end, executed next trading day open)",
    }
    ledger["last_updated"] = date.today().isoformat()

    with open(LEDGER_JSON, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    print("ledger: latest signal %s rebalance %s, new picks %d, newly evaluated %d, "
          "total picks %d, evaluated %d, pending %d" % (
              latest_signal_date, latest_rebalance_date, n_new, n_eval,
              len(picks), n, len(picks) - n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
