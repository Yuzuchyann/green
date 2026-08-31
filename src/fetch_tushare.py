#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tushare 数据拉取

★ 最关键的一个设计：按「交易日」拉全市场，而不是按「股票」逐只拉。

    按股票拉：5000 只 × 8 年 = 4 万次调用 → 免费额度下要跑好几天
    按日期拉：8 年约 1950 个交易日 = 1950 次调用 → 十几分钟跑完

同样是拿全市场全部数据，差了 20 倍。Tushare 的 daily / adj_factor /
daily_basic 都支持传 trade_date 一次拿回当天所有股票，一定要用这个。

用法：
    python src/fetch_tushare.py --limit-days 5    # 先跑 5 天，验证流程通不通
    python src/fetch_tushare.py                   # 全量
    python src/fetch_tushare.py --only fina       # 只拉财务
    python src/fetch_tushare.py --reset           # 清空进度重跑（慎用）
"""

import argparse
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import tushare as ts

from config import get_tushare_token, START_DATE, END_DATE
import db


# ---------------------------------------------------------------- 限速与重试
class RateLimiter:
    """令牌桶限速。免费用户上限 200 次/分钟，这里设 180 留点余量。"""

    def __init__(self, max_calls: int = 180, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    def wait(self):
        now = time.time()
        while self.calls and now - self.calls[0] > self.period:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep_for = self.period - (now - self.calls[0]) + 0.2
            if sleep_for > 0:
                time.sleep(sleep_for)
            return self.wait()
        self.calls.append(time.time())


limiter = RateLimiter()


class NoPermission(Exception):
    """接口没权限（积分不够）。这类错误重试没意义，直接跳过。"""


def safe_call(fn, *args, retries: int = 3, **kwargs):
    """调 Tushare 接口，带限速与重试。"""
    for attempt in range(retries):
        try:
            limiter.wait()
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            # 积分不足 / 没有权限：不重试，直接抛给上层跳过
            if any(k in msg for k in ("权限", "积分", "抱歉", "没有访问")):
                raise NoPermission(msg)
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)  # 1s, 2s, 4s 退避


def _progress(i: int, total: int, label: str):
    print(f"\r  {label}: {i}/{total} ({i / total * 100:5.1f}%)", end="", flush=True)


# ---------------------------------------------------------------- 股票列表
def fetch_stock_list(pro) -> int:
    """拉全市场股票列表。数据量小，一次调用搞定。"""
    if db.is_done("stock_list"):
        print("  股票列表：已存在，跳过")
        return 0

    print("  正在拉取股票列表 ...")
    frames = []
    for status in ("L", "D", "P"):  # 上市 / 退市 / 暂停上市
        try:
            df = safe_call(
                pro.stock_basic,
                exchange="",
                list_status=status,
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date",
            )
            if df is not None and not df.empty:
                frames.append(df)
        except NoPermission as e:
            print(f"\n  stock_basic 无权限：{e}")
            return 0

    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    with db.connect() as conn:
        n = db.upsert(conn, "stocks", df)
    db.mark_done("stock_list", n)
    print(f"  股票列表：{n:,} 家")
    return n


def get_trade_dates(pro, start: str, end: str) -> list:
    """取交易日历。只保留开市日，避免对非交易日做无谓调用。"""
    df = safe_call(
        pro.trade_cal, exchange="SSE", start_date=start, end_date=end, is_open="1"
    )
    return sorted(df["cal_date"].tolist())


# ---------------------------------------------------------------- 逐日行情
# 每个任务：接口函数 → 目标表 → 需要的字段
DAILY_TASKS = [
    ("daily", "daily", "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"),
    ("adj_factor", "daily", "ts_code,trade_date,adj_factor"),
    ("daily_basic", "daily_basic",
     "ts_code,trade_date,close,turnover_rate,pe,pe_ttm,pb,total_mv,circ_mv"),
]


def fetch_one_day(pro, date: str, kinds: tuple) -> dict:
    """拉某一天的全部股票数据。

    adj_factor 和 daily 写进同一张表，靠主键 (ts_code, trade_date) 合并，
    所以不用自己再 join 一次。
    """
    result = {}
    for api_name, table, fields in DAILY_TASKS:
        if api_name not in kinds:
            continue
        task = f"{api_name}@{date}"
        if db.is_done(task):
            result[api_name] = "skip"
            continue
        try:
            df = safe_call(getattr(pro, api_name), trade_date=date, fields=fields)
        except NoPermission as e:
            # 第一次遇到就记下来，后面同类任务直接跳过，不再刷屏
            result[api_name] = f"无权限"
            db.mark_done(task, 0)
            continue
        if df is None or df.empty:
            db.mark_done(task, 0)
            result[api_name] = 0
            continue
        with db.connect() as conn:
            n = db.upsert(conn, table, df)
        db.mark_done(task, n)
        result[api_name] = n
    return result


# ---------------------------------------------------------------- 财务数据
# 财务接口按「报告期」拉，一次拿回全市场某个报告期的数据。
# 覆盖 2018Q1 – 2026Q4，共约 36 个报告期。
def _periods(start_year: int = 2018, end_year: int = 2026) -> list:
    return [f"{y}{m:02d}31" for y in range(start_year, end_year + 1)
            for m in (3, 6, 9, 12)]


FINA_TASKS = [
    ("fina_indicator", "fina_indicator",
     "ts_code,end_date,ann_date,eps,roa,roe,debt_to_assets,"
     "grossprofit_margin,netprofit_margin,or_yoy,netprofit_yoy"),
    ("balancesheet", "balancesheet",
     "ts_code,end_date,ann_date,total_assets,total_liab,total_hldr_eqy_exc_min_int"),
]


def fetch_financials(pro, periods: list) -> None:
    """拉财务数据。

    ⚠️ ann_date 一定要留着。年报的实际公布时间比报告期晚好几个月
    （2025 年报通常 2026 年 4 月才出），做回归时必须按 ann_date 对齐，
    否则就是在用未来信息解释过去 —— 前视偏差，一查一个准。
    """
    total = len(periods) * len(FINA_TASKS)
    done = 0
    for period in periods:
        for api_name, table, fields in FINA_TASKS:
            done += 1
            task = f"{api_name}@{period}"
            if db.is_done(task):
                _progress(done, total, "财务")
                continue
            try:
                df = safe_call(getattr(pro, api_name), period=period, fields=fields)
            except NoPermission:
                db.mark_done(task, 0)
                _progress(done, total, "财务")
                continue
            except Exception as e:
                print(f"\n  {api_name} {period} 失败：{e}")
                _progress(done, total, "财务")
                continue
            n = 0
            if df is not None and not df.empty:
                with db.connect() as conn:
                    n = db.upsert(conn, table, df)
            db.mark_done(task, n)
            _progress(done, total, "财务")
    print()


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="拉取 A 股行情与财务数据")
    ap.add_argument("--start", default=START_DATE, help="起始日期 YYYYMMDD")
    ap.add_argument("--end", default=END_DATE, help="结束日期 YYYYMMDD")
    ap.add_argument("--limit-days", type=int, default=0,
                    help="只拉最近 N 个交易日，用来快速验证流程")
    ap.add_argument("--only", choices=("price", "fina", "all"), default="all")
    ap.add_argument("--reset", action="store_true",
                    help="清空同步进度后重跑（数据不清，只是重新拉一遍）")
    args = ap.parse_args()

    print("=" * 62)
    print("Tushare 数据拉取")
    print("=" * 62)

    pro = ts.pro_api(get_tushare_token())
    path = db.init_db()
    print(f"数据库：{path}\n")

    if args.reset:
        with db.connect() as conn:
            conn.execute("DELETE FROM sync_log")
        print("已清空同步进度\n")

    # ---- 股票列表
    fetch_stock_list(pro)

    # ---- 行情：按交易日逐天拉
    if args.only in ("price", "all"):
        print("\n[1/2] 日线行情")
        dates = get_trade_dates(pro, args.start, args.end)
        if args.limit_days:
            dates = dates[-args.limit_days:]
        print(f"  交易日 {len(dates)} 天：{dates[0]} → {dates[-1]}")

        skipped = {}
        for i, d in enumerate(dates, 1):
            try:
                res = fetch_one_day(pro, d, ("daily", "adj_factor", "daily_basic"))
            except Exception as e:
                print(f"\n  {d} 失败：{e}")
                continue
            for k, v in res.items():
                if isinstance(v, str):
                    skipped[k] = v
            _progress(i, len(dates), "行情")
        print()

        if skipped:
            print("  以下接口无权限（需要更高积分），已跳过：")
            for k, v in skipped.items():
                print(f"    - {k}: {v}")
            print("  提示：daily 和 adj_factor 是基础接口，一般免费可用；")
            print("        daily_basic 需要 2000 积分。市值也可以自己用 股本×收盘价 算。")

    # ---- 财务
    if args.only in ("fina", "all"):
        print("\n[2/2] 财务数据")
        fetch_financials(pro, _periods())

    # ---- 汇总
    print("\n" + "=" * 62)
    print("完成情况")
    print("=" * 62)
    for t in ("stocks", "daily", "daily_basic", "fina_indicator", "balancesheet"):
        print(f"  {t:<16} {db.row_count(t):>12,} 行")
    print()


if __name__ == "__main__":
    main()
