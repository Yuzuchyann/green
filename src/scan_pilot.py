"""扫描件比例试点：抽样 N 家公司，下载其 2025 年报 PDF，检测是否为扫描件（无文本层）。

为什么需要它
------------
路径 C（文本 / 大模型法）要解析年报正文。若年报是扫描件（图片版，无文本层），
必须先 OCR 才能提取文字。扫描件比例直接决定 OCR 工作量与预算，
是 RESEARCH_PLAN 中 2026-10 月份明确要求的交付物「扫描件比例报告」。

判定逻辑
--------
取 PDF 前若干页，统计每页可提取文本字符数，求页均字符数 mean_chars：
    mean_chars >= 200  ->  文本版（可直接提取）
    mean_chars <  200  ->  扫描件（需 OCR）
（文本版年报页均字符通常数百上千，扫描件接近 0，两者分布几乎不重叠。）

产出
----
    data/processed/scan_pilot.csv    逐家明细（断点续跑依据）
    data/raw/reports/*.pdf           下载的年报原件
    reports/scan_ratio_report.md     汇总报告

用法
----
    python src/scan_pilot.py --sample 10      # 小样本先验证
    python src/scan_pilot.py --sample 100     # 正式试点（计划要求 100 家）
"""
import argparse
import csv
import random
import re
import sqlite3
import time
from pathlib import Path

import akshare as ak
import requests

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "processed" / "greenwash.db"
RAW = ROOT / "data" / "raw" / "reports"
CSV_OUT = ROOT / "data" / "processed" / "scan_pilot.csv"
REPORT = ROOT / "reports" / "scan_ratio_report.md"

TEXT_THRESHOLD = 200  # 页均字符数阈值，>= 视为文本版
FIELDS = ["symbol", "name", "ts_code", "title", "ann_date", "pdf_url",
          "pages", "mean_chars", "verdict", "status", "err"]


def load_done():
    """已完成（status=ok）的股票，重跑时跳过。"""
    done = set()
    if CSV_OUT.exists():
        with open(CSV_OUT, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "ok":
                    done.add(row["symbol"])
    return done


def pick_stocks(limit, seed):
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select ts_code, symbol, name from stocks where symbol is not null"
    ).fetchall()
    con.close()
    random.Random(seed).shuffle(rows)
    return rows[:limit]


def fetch_annual_report(symbol):
    """返回 (标题, 公告日, announcementId)。查不到返回 (None,None,None)。"""
    df = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=symbol, market="沪深京", category="年报",
        start_date="20260101", end_date="20260731",
    )
    if df is None or df.empty:
        return None, None, None
    title_series = df["公告标题"].astype(str)
    cand = df[title_series.str.contains("年度报告")]
    cand = cand[~title_series.loc[cand.index].str.contains("摘要|英文|取消|更正")]
    if cand.empty:
        return None, None, None
    row = cand.iloc[0]
    link = str(row["公告链接"])
    m_id = re.search(r"announcementId=(\d+)", link)
    m_dt = re.search(r"announcementTime=([\d\-]+)", link)
    return (str(row["公告标题"]),
            m_dt.group(1) if m_dt else None,
            m_id.group(1) if m_id else None)


def download_pdf(mid, dt, symbol):
    """下载年报 PDF 到本地，返回 (路径, 错误信息)。"""
    if not mid or not dt:
        return None, "缺少 announcementId/Time"
    url = f"http://static.cninfo.com.cn/finalpage/{dt}/{mid}.PDF"
    path = RAW / f"{symbol}_{mid}.pdf"
    if path.exists() and path.stat().st_size > 1000:
        return path, None
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or r.content[:4] != b"%PDF":
            return None, f"HTTP {r.status_code}"
        path.write_bytes(r.content)
        return path, None
    except Exception as e:
        return None, type(e).__name__


def probe_text_layer(path, max_pages=10):
    """返回 (总页数, 页均字符数)。"""
    doc = fitz.open(str(path))
    n = doc.page_count
    lens = []
    for i in range(min(max_pages, n)):
        try:
            lens.append(len(doc[i].get_text("text").strip()))
        except Exception:
            lens.append(0)
    doc.close()
    return n, (sum(lens) / len(lens) if lens else 0)


def write_report():
    """读 CSV 明细，生成汇总报告 reports/scan_ratio_report.md。"""
    rows = []
    with open(CSV_OUT, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    ok = [r for r in rows if r["status"] == "ok"]
    scan = [r for r in ok if r["verdict"] == "scan"]
    text = [r for r in ok if r["verdict"] == "text"]
    fails = [r for r in rows if r["status"] != "ok"]

    ratio = (len(scan) / len(ok) * 100) if ok else 0
    chars = sorted(float(r["mean_chars"]) for r in ok) if ok else [0]
    med_chars = chars[len(chars) // 2]

    fail_reasons = {}
    for r in fails:
        key = r["status"] + ((": " + r["err"]) if r["err"] else "")
        fail_reasons[key] = fail_reasons.get(key, 0) + 1

    lines = [
        "# 扫描件比例报告（试点）",
        "",
        "> 交付物对应 RESEARCH_PLAN 中 **2026-10 月**：「100 家试点数据库 + 扫描件比例报告」。",
        "> 本报告只回答一个问题：**年报里有多少是无法直接提取文字的扫描件**，",
        "> 它决定路径 C（文本 / 大模型法）的 OCR 工作量与预算。",
        "",
        "## 一、结论",
        "",
        f"- 有效样本 **{len(ok)} 家**，其中**扫描件 {len(scan)} 家，占比 {ratio:.1f}%**；"
        f"文本版 {len(text)} 家（{100 - ratio:.1f}%）。",
        f"- 判定阈值：页均可提取字符数 ≥ {TEXT_THRESHOLD} 视为文本版（扫描件与文本版分布几乎不重叠）。",
        f"- 有效样本页均字符数中位数：**{med_chars:.0f}**。",
        "",
        "## 二、方法",
        "",
        "1. **抽样**：从本地 `greenwash.db` 的 `stocks` 表（全市场 5,553 只）随机抽样，seed 固定可复现。",
        "2. **定位年报**：AkShare `stock_zh_a_disclosure_report_cninfo`（巨潮资讯），取「2025 年年度报告」"
        "正文（排除摘要 / 英文版 / 更正版）。",
        "3. **下载**：巨潮静态资源直链 `static.cninfo.com.cn/finalpage/{公告日}/{公告ID}.PDF`。",
        "4. **检测**：PyMuPDF 取前 10 页，统计每页可提取字符数，求页均。",
        "5. **判定**：页均 ≥ 200 → 文本版；< 200 → 扫描件（需 OCR）。",
        "",
        "## 三、明细",
        "",
        "| 代码 | 名称 | 页数 | 页均字符 | 判定 |",
        "|---|---|---|---|---|",
    ]
    for r in sorted(ok, key=lambda x: float(x["mean_chars"])):
        flag = "扫描件" if r["verdict"] == "scan" else "文本版"
        lines.append(f"| {r['symbol']} | {r['name']} | {r['pages']} | {r['mean_chars']} | {flag} |")

    lines += [
        "",
        "## 四、失败与缺失（如实记录）",
        "",
    ]
    if fail_reasons:
        lines.append("| 原因 | 家数 |")
        lines.append("|---|---|")
        for k, v in sorted(fail_reasons.items(), key=lambda x: -x[1]):
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("无失败。")
    lines += [
        "",
        "## 五、局限（后续必须处理）",
        "",
        "1. 样本为**全市场随机抽样**，尚未限定为「已披露可持续发展报告的公司」"
        "（该名单待中证 / 巨潮采集后确定），比例为核心样本提供**先验估计**，不等于最终总体比例。",
        "2. 仅检测**前 10 页**，若个别年报前部为图片而后部为文本（混合型），可能被误判为扫描件；"
        "后续扩大检测页数复核。",
        "3. 本报告只覆盖**年报**；路径 C 还需要可持续发展报告本身，其扫描件比例需另行统计。",
        "",
        f"> 生成脚本：`src/scan_pilot.py`　|　明细数据：`data/processed/scan_pilot.csv`",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return len(ok), len(scan), ratio, len(fails)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=100, help="抽样家数")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，保证可复现")
    ap.add_argument("--force", action="store_true", help="忽略已完成记录，全部重跑")
    args = ap.parse_args()

    if fitz is None:
        raise SystemExit("缺少 PyMuPDF：请先 pip install pymupdf")

    RAW.mkdir(parents=True, exist_ok=True)
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    done = set() if args.force else load_done()
    stocks = pick_stocks(args.sample, args.seed)
    todo = [s for s in stocks if s[1] not in done]
    print(f"抽样 {len(stocks)} 家，已完成 {len(stocks) - len(todo)} 家，本轮待处理 {len(todo)} 家")

    need_header = (not CSV_OUT.exists()) or CSV_OUT.stat().st_size == 0
    f = open(CSV_OUT, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=FIELDS)
    if need_header:
        w.writeheader()

    t0 = time.time()
    for i, (ts_code, symbol, name) in enumerate(todo, 1):
        rec = dict.fromkeys(FIELDS, "")
        rec.update({"symbol": symbol, "name": name, "ts_code": ts_code})
        try:
            title, dt, mid = fetch_annual_report(symbol)
            if not mid:
                rec.update({"status": "no_report", "err": "巨潮未查到2025年报"})
                w.writerow(rec); f.flush(); continue
            rec.update({"title": title, "ann_date": dt or "",
                        "pdf_url": f"http://static.cninfo.com.cn/finalpage/{dt}/{mid}.PDF"})
            path, err = download_pdf(mid, dt, symbol)
            if path is None:
                rec.update({"status": "dl_fail", "err": err})
                w.writerow(rec); f.flush(); continue
            pages, mean = probe_text_layer(path)
            rec.update({"pages": pages, "mean_chars": round(mean, 1),
                        "verdict": "text" if mean >= TEXT_THRESHOLD else "scan",
                        "status": "ok"})
        except Exception as e:
            rec.update({"status": "error", "err": str(e)[:120]})
        w.writerow(rec); f.flush()
        print(f"[{i}/{len(todo)}] {symbol} {name} -> {rec['status']} "
              f"{rec.get('verdict','')} chars={rec.get('mean_chars','')}", flush=True)
        time.sleep(0.3)
    f.close()
    print(f"本轮耗时 {time.time() - t0:.0f}s")

    n_ok, n_scan, ratio, n_fail = write_report()
    print(f"报告已生成：{REPORT}")
    print(f"有效 {n_ok} 家 | 扫描件 {n_scan} 家 ({ratio:.1f}%) | 失败 {n_fail} 家")


if __name__ == "__main__":
    main()
