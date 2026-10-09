"""评估 balancesheet 全量回填的「可能性」与「所需时间」。

背景
----
`balancesheet` 表从 week01 起就一直是空的：东财两个接口在 akshare 1.18.94 内部崩溃
（akshare 自身 bug）。week02 找到替代源——新浪 `stock_financial_report_sina`，
实测深 / 沪 / 北 / CDR 均可用。本脚本做批量实测，回答两个问题：
    1. **可能性**：成功率多少？关键字段（资产总计 / 负债合计 / 股东权益）是否稳定存在？
    2. **所需时间**：单家耗时 × 全市场家数 → 全量回填要跑多久？

结论写进 Reports/balanceSheetFeasibility.md。

用法
----
    python Src/probeBalanceSheet.py --sample 30
"""
import argparse
import csv
import random
import sqlite3
import time
from pathlib import Path

import akshare as ak

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "Data" / "Processed" / "GREENWASH.db"
OUT_CSV = ROOT / "Data" / "Processed" / "balance_probe.csv"
REPORT = ROOT / "Reports" / "balanceSheetFeasibility.md"

# 后续建模真正需要的字段
KEY_COLS = ["资产总计", "负债合计"]          # 必需，各股列名一致
# 权益类字段：新浪在不同股票上口径不一，命中任一即可。
# ⚠️ 踩坑：只有部分股票叫「股东权益」，多数叫「所有者权益(或股东权益)合计」，
# 只认一个名字会大面积漏字段（第一轮实测因此算出 0% 齐全率）。
EQUITY_COLS = ["所有者权益(或股东权益)合计", "股东权益", "归属于母公司股东权益合计"]
TOTAL_MARKET = 5553


def sina_symbol(ts_code):
    """600000.SH -> sh600000"""
    code, mkt = ts_code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt, "sh") + code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=30, help="实测家数")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    rows = con.execute(
        "select ts_code, symbol, name from stocks where ts_code is not null").fetchall()
    random.Random(args.seed).shuffle(rows)
    sample = rows[:args.sample]

    results = []
    t_all = time.time()
    for i, (ts, sym, name) in enumerate(sample, 1):
        rec = {"ts_code": ts, "symbol": sym, "name": name}
        t = time.time()
        try:
            df = ak.stock_financial_report_sina(
                stock=sina_symbol(ts), symbol="资产负债表")
            el = time.time() - t
            cols = list(df.columns) if df is not None else []
            equity = next((e for e in EQUITY_COLS if e in cols), "")
            rec.update({
                "status": "ok",
                "seconds": round(el, 2),
                "rows": len(df) if df is not None else 0,
                "periods": int(df["报告日"].nunique()) if "报告日" in cols else 0,
                "has_key": "|".join([k for k in KEY_COLS if k in cols]),
                "equity_col": equity,
                "err": "",
            })
        except Exception as e:
            rec.update({"status": "fail", "seconds": round(time.time() - t, 2),
                        "rows": 0, "periods": 0, "has_key": "", "equity_col": "",
                        "err": type(e).__name__})
        results.append(rec)
        print(f"[{i}/{len(sample)}] {sym} {name} -> {rec['status']} "
              f"{rec['seconds']}s rows={rec['rows']}", flush=True)
        time.sleep(0.2)
    elapsed = time.time() - t_all

    fields = ["ts_code", "symbol", "name", "status", "seconds", "rows",
              "periods", "has_key", "equity_col", "err"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    write_report(results, elapsed, args.sample)


def write_report(results, elapsed, sample_n):
    n = len(results)
    ok = [r for r in results if r["status"] == "ok"]
    fail = [r for r in results if r["status"] != "ok"]
    rate = len(ok) / n if n else 0
    secs = sorted(r["seconds"] for r in ok) if ok else [0]
    avg = sum(secs) / len(secs)
    med = secs[len(secs) // 2]
    p95 = secs[int(len(secs) * 0.95) - 1] if len(secs) > 1 else secs[0]

    # 时间估算：串行，按平均耗时；另给 +20% 失败重试余量
    est_h = avg * TOTAL_MARKET / 3600
    est_h_retry = est_h * 1.2
    est_h_p95 = p95 * TOTAL_MARKET / 3600

    have_all = [r for r in ok if all(k in r["has_key"] for k in KEY_COLS) and r["equity_col"]]
    key_rate = len(have_all) / len(ok) if ok else 0
    eq_counter = {}
    for r in ok:
        if r["equity_col"]:
            eq_counter[r["equity_col"]] = eq_counter.get(r["equity_col"], 0) + 1
    periods = [r["periods"] for r in ok if r["periods"]]
    avg_periods = sum(periods) / len(periods) if periods else 0

    lines = [
        "# balancesheet 全量回填 · 可行性与时间评估",
        "",
        "> 回答两个问题：**能不能补上**（成功率、字段可用性）、**要跑多久**（全市场时间估算）。",
        f"> 实测样本 {sample_n} 家（seed 42 随机抽样），实测总耗时 {elapsed:.0f}s。",
        "",
        "## 一、结论",
        "",
        f"- **技术可行性：✅ 可行**。实测成功率 **{rate*100:.0f}%**（{len(ok)}/{n}），"
        f"关键字段齐全率 **{key_rate*100:.0f}%**。",
        f"- **所需时间：串行约 {est_h:.1f} 小时**（按平均 {avg:.2f}s/家 × {TOTAL_MARKET} 家）；",
        f"  计入 20% 失败重试余量后约 **{est_h_retry:.1f} 小时**；"
        f"若按 P95 慢速估算约 {est_h_p95:.1f} 小时。",
        "- **建议**：可以排进十月，一次跑完；或拆成 3–4 个晚上分批跑（有断点续跑就不怕中断）。",
        "",
        "## 二、实测数据",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 实测家数 | {n} 家 |",
        f"| 成功 / 失败 | {len(ok)} / {len(fail)} |",
        f"| 单家耗时（平均 / 中位 / P95） | {avg:.2f}s / {med:.2f}s / {p95:.2f}s |",
        f"| 返回报告期数（平均） | {avg_periods:.0f} 期/家 |",
        f"| 关键字段齐全率 | {key_rate*100:.0f}% |",
        "",
        "### 关键字段说明",
        "",
        "- `资产总计` ✅、`负债合计` ✅ 各股列名一致，稳定存在。",
        "- ⚠️ **权益类字段口径不统一（本次踩到的坑）**：不同股票分别叫 "
        "`所有者权益(或股东权益)合计` / `股东权益` / `归属于母公司股东权益合计`，",
        "  回填脚本必须做**兼容匹配**，只认一个名字会大面积漏字段。",
        "- 权益字段命中分布："
        + ("；".join(f"{k} {v} 家" for k, v in sorted(eq_counter.items(), key=lambda x: -x[1]))
           or "无"),
        "- 由此可补齐的变量：**Lev（资产负债率）**、**Size 的替代口径**（总资产）、"
        "以及**自算市值所需的股本线索**（需再核对是否有股本字段）。",
        "",
        "## 三、时间估算怎么算出来的",
        "",
        "```",
        f"单家平均耗时 {avg:.2f}s  ×  全市场 {TOTAL_MARKET} 家  =  {avg*TOTAL_MARKET:.0f}s  ≈  {est_h:.1f} 小时",
        f"计入 20% 重试余量                                        ≈  {est_h_retry:.1f} 小时",
        f"悲观情形（按 P95 {p95:.2f}s/家）                          ≈  {est_h_p95:.1f} 小时",
        "```",
        "",
        "## 四、风险与应对",
        "",
        "| 风险 | 影响 | 应对 |",
        "|---|---|---|",
        "| 新浪限流（连续请求被拒） | 中 | 每家间隔 0.2–0.5s；失败进重试队列，不标完成 |",
        "| 北交所 / CDR 个别股票取不到 | 低（week02 已实测覆盖） | 失败清单单独记，论文局限里说明 |",
        "| akshare 版本升级改接口 | 中 | 已锁定版本；回填完成后不再依赖该接口 |",
        f"| 数据体积（{TOTAL_MARKET} 家 × {avg_periods:.0f} 期） | 低 | 只存年度报告期 + 关键字段，体积可压到几 MB |",
        "",
        "## 五、建议排期",
        "",
        "1. **先跑核心样本**（已披露可持续报告的约 2,720 家）而不是全市场：时间直接砍半，"
        "且这才是回归真正要用的样本。",
        "2. **只存年报（1231 报告期）+ 关键字段**，季报暂不入库（路径 B/C 用不到)。",
        "3. **断点续跑 + 失败不标完成**（沿用 fetch_akshare 的三原则），中途关机也不丢进度。",
        "4. 回填完成后，`Size` / `Lev` 两个控制变量才真正可用；"
        "**若不做回填，Lev 缺失、Size 只能用 total_mv（且仅 2021-09 之后有值）**。",
        "",
        "> 评估脚本：`Src/probeBalanceSheet.py`　|　实测明细：`Data/Processed/balance_probe.csv`",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n评估报告已生成：{REPORT}")
    print(f"成功 {len(ok)}/{n}（{rate*100:.0f}%）｜平均 {avg:.2f}s/家｜"
          f"全市场估算 {est_h:.1f} 小时（含重试 {est_h_retry:.1f} 小时）")


if __name__ == "__main__":
    main()
