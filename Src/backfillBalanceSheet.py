"""
回填 balancesheet 表：全市场资产负债表三大科目（2018 年起全部报告期）

数据源：新浪 stock_financial_report_sina(symbol="资产负债表")，免费、免积分。
为什么不用东财：akshare 1.18.94 的两个东财财报接口在本机内部报错（week02 已实测）。

目标表（week01 定的 schema，不动）：
  balancesheet(ts_code, end_date, ann_date, total_assets, total_liab, total_hldr_eqy_exc_min_int)
  主键 (ts_code, end_date)

为什么只取这三列：
  Size = ln(总资产)、Lev = 总负债/总资产、BM 的账面价值 = 股东权益，
  研究要的控制变量只需要这三个，全表 150 列没必要塞进 SQLite。

四条纪律（都是踩过坑换来的，别改回去）：
  1. 空表 / 缺关键列 = **失败**，绝不 mark_done —— 九月「假完成」教训：
     新浪限流时会返回一个空 DataFrame，如果照常标记完成，这些公司就永久缺数据且不报错。
  2. 逐家独立提交：每家一个事务，进程被杀最多丢当前这一家，进度不依赖进程存活。
  3. 权益字段三种口径，必须别名兼容匹配（单点观测推断 schema 会导致静默 0%）。
  4. 零套接字处理：数值列可能是 '--' 或空串，统一 to_numeric(coerce)；
     三者全 NaN 的行不写入（写了也是 NULL 占主键），单独计数。

用法：
  python Src/backfillBalanceSheet.py --limit 20   # 小样本试跑
  python Src/backfillBalanceSheet.py              # 全市场，约 1 小时
"""

import argparse
import csv
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "Data" / "Processed" / "GREENWASH.db"
OUT_CSV = ROOT / "Data" / "Processed" / "balance_fill.csv"

START_DATE = "20171231"   # 年报 2017-12-31 于 2018 年披露，可作为 2018 年初的存量变量

# ⚠️ 公告日期的最大可信滞后天数（踩坑得来，别改）
# 新浪 balance sheet 的「公告日期」列对**年报行是错的**——
# 它给的是未来某一期的披露日（如 20251231 给 20260828，间隔 242 天），
# 而季报行全部正确。跨沪/深/北 4 家实测，年报行 100% 错位。
# 法定披露上限：一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30，
# 换算成滞后天数最多约 120 天。留一点余量取 150，正好精确拦掉所有错位年报。
MAX_ANN_LAG = 150

# 必需科目：缺任一个就判定该次响应不合格
REQ_COLS = ["资产总计", "负债合计"]
# 股东权益：新浪在不同股票上口径不一，命中任一即可
EQUITY_COLS = [
    "所有者权益(或股东权益)合计",
    "股东权益",
    "归属于母公司股东权益合计",
    "所有者权益合计",
    "股东权益合计",
]


def sina_symbol(ts_code: str) -> str:
    """600000.SH -> sh600000"""
    code, mkt = ts_code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt, "sh") + code


def norm_date(v) -> str:
    """统一成 YYYYMMDD 字符串；认不出来返回空串。"""
    if v is None:
        return ""
    s = str(v).strip()
    if re.fullmatch(r"\d{8}", s):
        return s
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return ""


def credible_ann(end: str, ann: str) -> str:
    """公告日只有在「不早于报告期、滞后不超过 MAX_ANN_LAG 天」时才采信。

    宁可留空，也不让一个看起来合理其实是错的日期进库 ——
    NULL 是显式的"我不知道"，错的日期是伪装的"我知道"，后者会直接毁掉滞后对齐。
    """
    if not end or not ann:
        return ""
    try:
        gap = (datetime.strptime(ann, "%Y%m%d") - datetime.strptime(end, "%Y%m%d")).days
    except ValueError:
        return ""
    return ann if 0 <= gap <= MAX_ANN_LAG else ""


def fetch_with_retry(ts_code: str, tries: int = 3):
    """带退避重试。返回 DataFrame；任何异常都往上抛，由调用方记 fail。"""
    last = None
    for i in range(tries):
        try:
            df = ak.stock_financial_report_sina(
                stock=sina_symbol(ts_code), symbol="资产负债表")
            if df is None:
                raise ValueError("返回 None")
            if len(df) == 0:
                raise ValueError("返回空表（疑似限流）")
            return df
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def done_set() -> set:
    """已完成的股票（rows>0 才算数，rows=0 视为未完成，防止假完成）。"""
    con = sqlite3.connect(DB)
    try:
        return {r[0] for r in con.execute(
            "select task from sync_log where task like 'bs@%' and rows > 0")}
    finally:
        con.close()


WRITE_BS = ("INSERT OR REPLACE INTO balancesheet "
            "(ts_code, end_date, ann_date, total_assets, total_liab, "
            "total_hldr_eqy_exc_min_int) VALUES (?,?,?,?,?,?)")
WRITE_LOG = "INSERT OR REPLACE INTO sync_log(task, rows) VALUES(?,?)"


def _exec(con, sql, rows, tries: int = 4):
    """带退避重试的写入。

    ⚠️ 血泪：第一轮全市场跑到第 617 家时，`sync_log` 写入突然抛
    OperationalError("attempt to write a readonly database")（磁盘 603G 空闲、
    文件权限正常，属瞬时故障）。当时的代码没有包异常，**整个循环当场死掉**，
    后面 4900 家全部没跑。所以写库也要重试，且整个 process 不许往外抛。
    """
    last = None
    for i in range(tries):
        try:
            con.executemany(sql, rows) if isinstance(rows[0], (tuple, list)) \
                else con.execute(sql, rows)
            con.commit()
            return
        except sqlite3.Error as e:
            last = e
            try:
                con.rollback()
            except sqlite3.Error:
                pass
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"写入失败（重试 {tries} 次）: {last}")


def process(con, ts_code: str) -> dict:
    """拉一家、写一家、标一家。返回统计字典。

    整个函数不往外抛异常：任何意外都记成 fail，循环继续。
    一家公司出问题不该让后面几千家陪葬。
    """
    rec = {"ts_code": ts_code, "status": "", "periods": 0, "bad_ann": 0,
           "written": 0, "skipped_nan": 0, "equity_col": "", "seconds": 0.0, "err": ""}
    t0 = time.time()
    try:
        return _process_inner(con, ts_code, rec, t0)
    except Exception as e:  # noqa: BLE001
        rec.update(status="fail", seconds=round(time.time() - t0, 2),
                   err=f"{type(e).__name__}: {str(e)[:110]}")
        return rec


def _process_inner(con, ts_code: str, rec: dict, t0: float) -> dict:
    df = fetch_with_retry(ts_code)

    cols = list(df.columns)
    missing = [c for c in REQ_COLS if c not in cols]
    if missing:
        rec.update(status="fail", seconds=round(time.time() - t0, 2),
                   err="缺列:" + ",".join(missing))
        return rec

    equity = next((e for e in EQUITY_COLS if e in cols), "")
    rec["equity_col"] = equity

    df = df.copy()
    df["_end"] = df["报告日"].map(norm_date)
    raw_ann = (df["公告日期"].map(norm_date) if "公告日期" in cols
               else pd.Series([""] * len(df), index=df.index))
    df["_ann"] = [credible_ann(e, a) for e, a in zip(df["_end"], raw_ann)]
    def num(series):
        # 新浪偶有 '--' / 空串 / 带逗号的字符型数字，统一强转；转不动的变 NaN 而不是悄悄变成 0
        return pd.to_numeric(series, errors="coerce")

    df["_ta"] = num(df["资产总计"])
    df["_tl"] = num(df["负债合计"])
    df["_eq"] = num(df[equity]) if equity else float("nan")

    df = df[df["_end"] >= START_DATE]
    rec["periods"] = len(df)
    # 非空但被判定不可信的公告日要计数（口径与写入范围一致）—— 绝大部分是错位年报
    rec["bad_ann"] = sum(1 for e, a in zip(df["_end"], raw_ann.loc[df.index])
                         if a and not credible_ann(e, a))

    def val(x):
        return None if pd.isna(x) else float(x)

    rows, skipped = [], 0
    for _, r in df.iterrows():
        # 三者全缺的行不写：主键占了却全是 NULL，后续 COUNT 会被误当成有数据
        if pd.isna(r["_ta"]) and pd.isna(r["_tl"]) and pd.isna(r["_eq"]):
            skipped += 1
            continue
        rows.append((ts_code, r["_end"], r["_ann"] or None,
                     val(r["_ta"]), val(r["_tl"]), val(r["_eq"])))
    rec["skipped_nan"] = skipped

    if rows:
        _exec(con, WRITE_BS, rows)
    rec["written"] = len(rows)

    # rows 为 0（该股在 2018 后确实没有任何一期）也标记完成，但记 0，
    # 便于后续单独复核；真正的失败不会走到这里。
    _exec(con, WRITE_LOG, [(f"bs@{ts_code}", len(rows))])

    rec.update(status="ok", seconds=round(time.time() - t0, 2))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 家（0=全部）")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    ap.add_argument("--shuffle", action="store_true", help="随机抽样而非按代码顺序")
    ap.add_argument("--sleep", type=float, default=0.15, help="每家之间的间隔秒数")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    stocks = con.execute(
        "select ts_code from stocks where ts_code is not null order by ts_code").fetchall()
    con.close()
    targets = [r[0] for r in stocks]
    if args.shuffle:
        random.Random(args.seed).shuffle(targets)
    done = done_set()
    todo = [t for t in targets if f"bs@{t}" not in done]
    print(f"全市场 {len(targets)} 家｜已完成 {len(targets)-len(todo)} 家｜待跑 {len(todo)} 家")
    if args.limit:
        todo = todo[:args.limit]
        print(f"本次限量 {len(todo)} 家")

    t_all = time.time()
    results = []
    # 全程复用一条连接：之前每家开关三次数据库连接，跑到 600 多家时疑似句柄
    # 抖动，直接触发 readonly。一条连接 + WAL 更稳，也更快。
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    try:
        for i, ts in enumerate(todo, 1):
            rec = process(con, ts)
            results.append(rec)
            if i % 100 == 0 or i == len(todo):
                el = time.time() - t_all
                ok = sum(1 for r in results if r["status"] == "ok")
                print(f"[{i}/{len(todo)}] ok={ok} fail={i-ok} "
                      f"累计 {el/60:.1f}min 预计剩余 {(el/i)*(len(todo)-i)/60:.1f}min",
                      flush=True)
            time.sleep(args.sleep)
    finally:
        con.close()

    fields = ["ts_code", "status", "periods", "written", "bad_ann",
              "skipped_nan", "equity_col", "seconds", "err"]
    # 追加写：分批跑时后一批不会覆盖前一批的记录
    new_file = not OUT_CSV.exists()
    with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(results)

    ok = [r for r in results if r["status"] == "ok"]
    fail = [r for r in results if r["status"] != "ok"]
    con = sqlite3.connect(DB)
    total_rows = con.execute("select count(*) from balancesheet").fetchone()[0]
    firms = con.execute("select count(distinct ts_code) from balancesheet").fetchone()[0]
    ann_ok = con.execute(
        "select count(*) from balancesheet where ann_date is not null and ann_date!=''").fetchone()[0]
    con.close()
    bad_ann = sum(r.get("bad_ann", 0) for r in results)
    print(f"\n=== 完成 ===")
    print(f"成功 {len(ok)} 家｜失败 {len(fail)} 家｜耗时 {(time.time()-t_all)/60:.1f} 分钟")
    print(f"balancesheet 现共 {total_rows:,} 行，覆盖 {firms:,} 家公司")
    print(f"其中 ann_date 非空 {ann_ok:,} 行（{ann_ok/max(total_rows,1)*100:.1f}%）")
    print(f"被判不可信而留空的公告日 {bad_ann:,} 行（错位年报，属数据源缺陷）")
    if fail:
        print("\n失败清单（前 15 家）：")
        for r in fail[:15]:
            print(f"  {r['ts_code']}  {r['err']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
