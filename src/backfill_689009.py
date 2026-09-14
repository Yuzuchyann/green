# -*- coding: utf-8 -*-
"""689009 (CDR) 一次性补数脚本：东财源拉日线 + 百度源估值，写 daily / daily_basic。"""
import sys, time, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "src")

import akshare as ak
import pandas as pd
import db
from fetch_akshare import _fetch_valuation, _ymd, _pick

START, END = "20180101", "20261231"
SYM, TS = "689009", "689009.SH"


def safe(fn, *a, retries=4, **k):
    for i in range(retries):
        try:
            return fn(*a, **k)
        except Exception as e:
            print(f"  第{i+1}次失败: {type(e).__name__}")
            if i == retries - 1:
                raise
            time.sleep(3 * (i + 1))


def main():
    h = safe(ak.stock_zh_a_hist, symbol=SYM, period="daily",
             start_date=START, end_date=END, adjust="")
    h = h.rename(columns={"日期": "trade_date", "开盘": "open", "最高": "high",
                          "最低": "low", "收盘": "close", "成交量": "vol",
                          "成交额": "amount", "涨跌幅": "pct_chg", "换手率": "turnover_rate"})
    h["trade_date"] = h["trade_date"].astype(str).map(_ymd)
    h["ts_code"] = TS
    h["adj_factor"] = 1.0
    h["pre_close"] = None
    h = h.sort_values("trade_date").reset_index(drop=True)
    h = h[h["trade_date"].between(START, END)]
    with db.connect() as conn:
        n1 = db.upsert(conn, "daily",
                       _pick(h, ["ts_code", "trade_date", "open", "high", "low", "close",
                                 "pre_close", "pct_chg", "vol", "amount", "adj_factor"]))
    print("daily 写入:", n1, "行")

    n2 = 0
    val = _fetch_valuation(SYM, START, END)
    if not val.empty:
        i = h[["trade_date", "ts_code", "close"]].copy()
        if "turnover_rate" in h:
            i["turnover_rate"] = h["turnover_rate"]
        i = i.merge(val, on="trade_date", how="left")
        i["circ_mv"] = None
        with db.connect() as conn:
            n2 = db.upsert(conn, "daily_basic",
                           _pick(i, ["ts_code", "trade_date", "close", "pe", "pe_ttm",
                                     "pb", "total_mv", "circ_mv", "turnover_rate"]))
        print("daily_basic 写入:", n2, "行")

    db.mark_done(f"ak_price@{SYM}", n1 + n2)
    print("sync_log 已标完成")


if __name__ == "__main__":
    main()
