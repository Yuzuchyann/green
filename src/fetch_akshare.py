#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AkShare 数据拉取（统一数据源）

设计目标：项目所有行情 / 估值 / 财务数据都从 AkShare 一个来源拿，
避免 Tushare + AkShare 混用导致的字段名、交易日历、复权方式不一致。

与 fetch_tushare.py 的差别：
  - Tushare 的 daily 接口支持传 trade_date，可以「按交易日」一次拉全市场；
  - AkShare 的行情 / 估值 / 财务接口基本都是「按股票」拉（stock_zh_a_hist 等），
    所以这里改成按股票循环。代价是调用次数更多，但完全免费、无需注册、无需积分，
    且数据口径统一，下游清洗代码不用改。

各接口映射（已按 akshare 1.18.x 实测）：
  - 股票列表：stock_info_a_code_name        -> stocks
  - 日线行情：stock_zh_a_daily（新浪源，绕开东方财富）-> daily（不复权，adj_factor=1.0；pct_chg 由收盘价计算）
  - 每日估值：stock_zh_valuation_baidu        -> daily_basic（总市值/市盈率/市净率）
  - 财务指标：stock_financial_analysis_indicator -> fina_indicator
  - 资产负债表：stock_balance_sheet_by_yearly_em  -> balancesheet（接口不稳，失败则跳过）

重要单位约定（和 Tushare 对齐，方便下游直接复用）：
  - daily.amount：AkShare 成交额单位是「元」，Tushare 是「千元」。本项目下游主要用
    close / pct_chg 算收益，amount 仅作参考，未做换算。
  - daily_basic.total_mv：valuation_baidu 的「总市值」单位是「亿元」，这里 ×10000
    转成「万元」，与 Tushare 的 total_mv（万元）对齐。
  - 复权：默认拉不复权价（adj_factor=1.0）。如需前复权收益，可改 fetch qfq 重新计算。

ann_date 说明：AkShare 免费接口不提供财报实际公告日，fina_indicator / balancesheet
的 ann_date 统一置 NULL，按报告期（end_date）对齐。这是已知局限，论文里必须披露。

用法：
    python src/fetch_akshare.py --limit-stocks 5          # 先跑 5 只，验证流程通不通
    python src/fetch_akshare.py --start 20240101 --end 20241231 --limit-stocks 20
    python src/fetch_akshare.py                            # 全市场（很慢，建议分批）
    python src/fetch_akshare.py --only price               # 只拉行情+估值
    python src/fetch_akshare.py --only fina                # 只拉财务
    python src/fetch_akshare.py --reset                    # 清空进度重跑（数据不清）
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import akshare as ak

import db
from config import START_DATE, END_DATE


# ---------------------------------------------------------------- 工具
def _ts_code(symbol: str) -> str:
    """把 AkShare 的纯数字代码转成 ts_code 格式（加 .SH/.SZ/.BJ）。"""
    symbol = str(symbol).strip().lower()
    symbol = symbol.replace("sh", "").replace("sz", "").replace("bj", "")
    if symbol.startswith(("6", "9")):
        return f"{symbol}.SH"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ"
    if symbol.startswith(("4", "8")):
        return f"{symbol}.BJ"
    return symbol  # 兜底


def _ymd(s) -> str:
    """把各种日期格式（2025-12-31 / 20251231）规整成 YYYYMMDD。"""
    s = str(s)
    for ch in ("-", "/", " ", ":"):
        s = s.replace(ch, "")
    return s[:8]


def _safe(fn, *args, retries: int = 3, **kwargs):
    """带指数退避的重试。AkShare 背后是公开财经网站，偶尔抽风，重试即可。"""
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)


def _progress(i: int, total: int, label: str):
    print(f"\r  {label}: {i}/{total} ({i / total * 100:5.1f}%)", end="", flush=True)


def _pick(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """只保留表里真实存在的列，避免 AkShare 版本差异导致 KeyError。"""
    return df[[c for c in cols if c in df.columns]]


# ---------------------------------------------------------------- 股票列表
def fetch_stock_list() -> int:
    if db.is_done("stock_list"):
        print("  股票列表：已存在，跳过")
        return 0
    print("  正在拉取股票列表 (AkShare stock_info_a_code_name) ...")
    try:
        df = _safe(ak.stock_info_a_code_name)
    except Exception as e:
        print(f"  股票列表拉取失败：{e}")
        return 0
    if df is None or df.empty:
        return 0
    # 不同 AkShare 版本列名可能是英文或中文，两种都兼容
    rename_map = {}
    for src, dst in (("code", "ts_code"), ("代码", "ts_code"),
                     ("name", "name"), ("名称", "name")):
        if src in df.columns:
            rename_map[src] = dst
    df = df.rename(columns=rename_map)
    df["ts_code"] = df["ts_code"].map(_ts_code)
    df["symbol"] = df["ts_code"].str.replace(r"\.\w+$", "", regex=True)
    df["market"] = df["ts_code"].str.extract(r"\.(\w+)$")
    # industry / list_date / is_soe 暂缺，留 NULL（与 is_soe 一致）
    with db.connect() as conn:
        n = db.upsert(conn, "stocks", _pick(df, ["ts_code", "symbol", "name", "market"]))
    db.mark_done("stock_list", n)
    print(f"  股票列表：{n:,} 家")
    return n


# ---------------------------------------------------------------- 每日估值
# valuation_baidu 一次只返回一个指标（date / value 两列），逐个拉再合并
VAL_INDICATORS = {
    "总市值": "total_mv",
    "市盈率": "pe",
    "市净率": "pb",
}


def _fetch_valuation(symbol: str, start: str, end: str) -> pd.DataFrame:
    """用 stock_zh_valuation_baidu 按指标逐个拉，再按日期合并。

    市值单位是「亿元」，这里 ×10000 转「万元」与下游对齐。
    """
    frames = []
    for ind, col in VAL_INDICATORS.items():
        try:
            d = _safe(ak.stock_zh_valuation_baidu, symbol=symbol,
                      indicator=ind, period="近五年")
        except Exception:
            continue
        if d is None or d.empty:
            continue
        d = d.rename(columns={"date": "trade_date", "value": col})
        d["trade_date"] = d["trade_date"].map(_ymd)
        frames.append(d[["trade_date", col]])
    if not frames:
        return pd.DataFrame(columns=["trade_date"])
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on="trade_date", how="outer")
    out = out.sort_values("trade_date")
    if "total_mv" in out:
        out["total_mv"] = pd.to_numeric(out["total_mv"], errors="coerce") * 10000.0
    return out[out["trade_date"].between(start, end)]


# ---------------------------------------------------------------- 单只股票：行情 + 估值
def _sina_symbol(symbol: str) -> str:
    """把纯数字代码转成新浪格式（sh600519 / sz000001 / bj8xxxxx）。"""
    symbol = symbol.replace("sh", "").replace("sz", "").replace("bj", "")
    if symbol.startswith(("6", "9")):
        return "sh" + symbol
    if symbol.startswith(("0", "3")):
        return "sz" + symbol
    if symbol.startswith(("4", "8")):
        return "bj" + symbol
    return symbol


def fetch_one_stock_prices(symbol: str, start: str, end: str) -> tuple:
    """拉一只股票的日线行情 + 每日估值，写进 daily / daily_basic。

    返回 (daily_rows, basic_rows)。
    行情用新浪源 stock_zh_a_daily（东方财富源在部分网络环境下连接不稳定）。
    """
    ts_code = _ts_code(symbol)
    daily_rows, basic_rows = 0, 0

    # 1) 日线行情（不复权）。新浪源不直接给涨跌幅，由收盘价计算。
    try:
        hist = _safe(ak.stock_zh_a_daily, symbol=_sina_symbol(symbol),
                     start_date=start, end_date=end, adjust="")
    except Exception as e:
        print(f"\n  {symbol} 行情拉取失败：{e}")
        return 0, 0

    if hist is not None and not hist.empty:
        h = hist.rename(columns={
            "date": "trade_date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "vol",
            "amount": "amount", "turnover": "turnover_rate",
        })
        h["trade_date"] = h["trade_date"].map(_ymd)
        h["ts_code"] = ts_code
        h["adj_factor"] = 1.0
        h["pre_close"] = None
        h = h.sort_values("trade_date").reset_index(drop=True)
        h["pct_chg"] = h["close"].pct_change() * 100.0  # 新浪源无涨跌幅，自行计算
        if "turnover_rate" in h:
            # 新浪换手率是小数（如 0.0026），转成百分比，与 Tushare 对齐
            h["turnover_rate"] = pd.to_numeric(h["turnover_rate"], errors="coerce") * 100.0
        h = h[h["trade_date"].between(start, end)]
        with db.connect() as conn:
            daily_rows = db.upsert(
                conn, "daily",
                _pick(h, ["ts_code", "trade_date", "open", "high", "low", "close",
                          "pre_close", "pct_chg", "vol", "amount", "adj_factor"]))

        # 2) 每日估值（市值 / PE / PB），与行情按日期合并
        val = _fetch_valuation(symbol, start, end)
        if not val.empty:
            i = h[["trade_date", "ts_code", "close", "turnover_rate"]].copy()
            i = i.merge(val, on="trade_date", how="left")
            i["circ_mv"] = None
            with db.connect() as conn:
                basic_rows = db.upsert(
                    conn, "daily_basic",
                    _pick(i, ["ts_code", "trade_date", "close", "turnover_rate",
                              "pe", "pe_ttm", "pb", "total_mv", "circ_mv"]))

    return daily_rows, basic_rows


# ---------------------------------------------------------------- 单只股票：财务
def fetch_one_stock_fina(symbol: str) -> tuple:
    """拉一只股票的财务指标 + 资产负债表，写进 fina_indicator / balancesheet。

    返回 (fina_rows, bs_rows)。
    """
    ts_code = _ts_code(symbol)
    rows_fi, rows_bs = 0, 0

    # 财务指标
    try:
        fi = _safe(ak.stock_financial_analysis_indicator, symbol=symbol)
    except Exception as e:
        print(f"\n  {symbol} 财务指标失败：{e}")
        fi = None
    if fi is not None and not fi.empty:
        f = fi.rename(columns={
            "日期": "end_date",
            "摊薄每股收益(元)": "eps",
            "净资产收益率(%)": "roe",
            "总资产报酬率(%)": "roa",
            "资产负债比率(%)": "debt_to_assets",
            "销售毛利率(%)": "grossprofit_margin",
            "销售净利率(%)": "netprofit_margin",
            "主营业务收入增长率(%)": "or_yoy",
            "净利润增长率(%)": "netprofit_yoy",
        })
        f["end_date"] = f["end_date"].map(_ymd)
        f["ts_code"] = ts_code
        f["ann_date"] = None  # 免费接口不提供公告日，按报告期对齐（已知局限）
        with db.connect() as conn:
            rows_fi = db.upsert(
                conn, "fina_indicator",
                _pick(f, ["ts_code", "end_date", "ann_date", "eps", "roa", "roe",
                          "debt_to_assets", "grossprofit_margin",
                          "netprofit_margin", "or_yoy", "netprofit_yoy"]))

    # 资产负债表：stock_balance_sheet_by_report_em 在当前 akshare 版本直接报错，
    # 改用 by_yearly_em；若仍不稳则跳过，不影响主流程（Size/BM 可暂用市值代理）。
    try:
        bs = _safe(ak.stock_balance_sheet_by_yearly_em, symbol=symbol)
    except Exception:
        bs = None
    if bs is not None and not bs.empty:
        b = bs.rename(columns={
            "报告期": "end_date",
            "资产总计": "total_assets",
            "负债合计": "total_liab",
            "归属母公司所有者权益合计": "total_hldr_eqy_exc_min_int",
        })
        b["end_date"] = b["end_date"].map(_ymd)
        b["ts_code"] = ts_code
        b["ann_date"] = None
        with db.connect() as conn:
            rows_bs = db.upsert(
                conn, "balancesheet",
                _pick(b, ["ts_code", "end_date", "ann_date", "total_assets",
                          "total_liab", "total_hldr_eqy_exc_min_int"]))

    return rows_fi, rows_bs


# ---------------------------------------------------------------- 取待处理代码
def _get_codes(limit: int) -> list:
    """从 stocks 表取代码（转成纯数字形式给 AkShare 用）。"""
    try:
        with db.connect(read_only=True) as conn:
            rows = conn.execute("SELECT ts_code FROM stocks").fetchall()
    except Exception:
        rows = []
    codes = [r[0].split(".")[0] for r in rows]
    if limit:
        codes = codes[:limit]
    return codes


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="AkShare 拉取 A 股行情与财务（统一数据源）")
    ap.add_argument("--start", default=START_DATE, help="起始日期 YYYYMMDD")
    ap.add_argument("--end", default=END_DATE, help="结束日期 YYYYMMDD")
    ap.add_argument("--limit-stocks", type=int, default=0,
                    help="只拉前 N 只股票，用来快速验证流程")
    ap.add_argument("--only", choices=("price", "fina", "all"), default="all")
    ap.add_argument("--reset", action="store_true",
                    help="清空同步进度后重跑（数据不清，只是重新拉一遍）")
    args = ap.parse_args()

    print("=" * 62)
    print("AkShare 数据拉取（统一数据源，免费 · 无需注册 · 无积分）")
    print("=" * 62)
    db.init_db()

    if args.reset:
        with db.connect() as conn:
            conn.execute("DELETE FROM sync_log")
        print("已清空同步进度\n")

    fetch_stock_list()
    codes = _get_codes(args.limit_stocks)
    print(f"\n待处理股票：{len(codes)} 只\n")

    if args.only in ("price", "all"):
        print("[1/2] 行情 + 估值")
        for i, sym in enumerate(codes, 1):
            task = f"ak_price@{sym}"
            if db.is_done(task):
                continue
            try:
                d, b = fetch_one_stock_prices(sym, args.start, args.end)
                db.mark_done(task, d + b)
            except Exception as e:
                print(f"\n  {sym} 行情失败：{e}")
                db.mark_done(task, 0)
            _progress(i, len(codes), "行情")
            time.sleep(0.3)  # 对数据源温柔一点
        print()

    if args.only in ("fina", "all"):
        print("[2/2] 财务")
        for i, sym in enumerate(codes, 1):
            task = f"ak_fina@{sym}"
            if db.is_done(task):
                continue
            try:
                f, b = fetch_one_stock_fina(sym)
                db.mark_done(task, f + b)
            except Exception as e:
                print(f"\n  {sym} 财务失败：{e}")
                db.mark_done(task, 0)
            _progress(i, len(codes), "财务")
            time.sleep(0.3)
        print()

    print("=" * 62)
    print("完成情况")
    print("=" * 62)
    for t in ("stocks", "daily", "daily_basic", "fina_indicator", "balancesheet"):
        print(f"  {t:<16} {db.row_count(t):>12,} 行")
    print()


if __name__ == "__main__":
    main()
