"""
SQLite 数据库：建表与读写

只存结构化数据（行情、财务、报告索引）。原始 PDF 放 data/raw，不进数据库。

三条设计原则：
  1. 日期统一用 TEXT 存 'YYYYMMDD'，和 Tushare 返回格式对齐，也方便按字符串做范围查询
  2. 写入一律用 INSERT OR REPLACE，脚本重跑不会插出重复行
  3. sync_log 记录每张表的同步进度，中断后重跑自动跳过已完成部分
"""

import sqlite3
from contextlib import contextmanager

from config import DB_PATH

# ---------------------------------------------------------------- 表结构
SCHEMA = """
-- 公司基本信息
CREATE TABLE IF NOT EXISTS stocks (
    ts_code     TEXT PRIMARY KEY,   -- 000001.SZ
    symbol      TEXT,               -- 000001
    name        TEXT,
    area        TEXT,
    industry    TEXT,               -- 行业，用于行业固定效应与分组
    market      TEXT,               -- 主板 / 创业板 / 科创板
    list_date   TEXT,
    delist_date TEXT,
    is_soe      INTEGER,            -- 是否国企 1/0，NULL = 待补充
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 日线行情
-- 存不复权价 + 复权因子，前复权价由两者算出。
-- 为什么不直接存复权价：复权因子会随除权除息变动，只存结果的话
-- 每次更新都要重算全表，容易出错。
CREATE TABLE IF NOT EXISTS daily (
    ts_code     TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,               -- 不复权收盘价
    pre_close   REAL,
    pct_chg     REAL,               -- 涨跌幅 %
    vol         REAL,               -- 成交量（手）
    amount      REAL,               -- 成交额（千元）
    adj_factor  REAL,               -- 复权因子
    PRIMARY KEY (ts_code, trade_date)
);

-- 每日指标：市值与估值。Tobin's Q、BM 都要从这里出
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code        TEXT NOT NULL,
    trade_date     TEXT NOT NULL,
    close          REAL,
    turnover_rate  REAL,            -- 换手率
    pe             REAL,
    pe_ttm         REAL,
    pb             REAL,
    total_mv       REAL,            -- 总市值（万元）
    circ_mv        REAL,            -- 流通市值（万元）
    PRIMARY KEY (ts_code, trade_date)
);

-- 财务指标
-- ⚠️ 关键字段 ann_date（实际公告日）：
-- 2025 年的年报要到 2026 年 4 月才公布。如果你用 end_date 去对齐 2025 年的股价，
-- 就等于让市场"提前预知"了还没公布的财务数据 —— 这是前视偏差（look-ahead bias），
-- 实证研究里最容易被挑出来的硬伤。财务变量一律按 ann_date 之后再生效。
CREATE TABLE IF NOT EXISTS fina_indicator (
    ts_code            TEXT NOT NULL,
    end_date           TEXT NOT NULL,   -- 报告期，如 20251231
    ann_date           TEXT,            -- 公告日，做滞后对齐用
    eps                REAL,
    roa                REAL,            -- 总资产净利率
    roe                REAL,
    debt_to_assets     REAL,            -- 资产负债率 → Lev
    grossprofit_margin REAL,
    netprofit_margin   REAL,
    or_yoy             REAL,            -- 营收同比增长 → Growth
    netprofit_yoy      REAL,
    PRIMARY KEY (ts_code, end_date)
);

-- 资产负债表：算 Size = ln(总资产) 和账面价值
CREATE TABLE IF NOT EXISTS balancesheet (
    ts_code       TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    ann_date      TEXT,
    total_assets  REAL,                -- 总资产 → Size
    total_liab    REAL,                -- 总负债
    total_hldr_eqy_exc_min_int REAL,   -- 股东权益 → BM 的账面价值
    PRIMARY KEY (ts_code, end_date)
);

-- 报告索引：10 月爬巨潮 PDF 时用
-- announce_date 就是事件研究的事件日，务必抓准
CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code       TEXT NOT NULL,
    year          INTEGER NOT NULL,
    report_type   TEXT,                -- annual / sustainability
    title         TEXT,
    announce_date TEXT,                -- 披露日（事件日）
    pdf_url       TEXT,
    local_path    TEXT,
    is_scanned    INTEGER,             -- 是否扫描件 1/0，NULL = 未检测
    parse_status  TEXT DEFAULT 'pending',  -- pending / ok / failed
    UNIQUE(ts_code, year, report_type)
);

-- 同步进度：断点续传
CREATE TABLE IF NOT EXISTS sync_log (
    task       TEXT PRIMARY KEY,       -- 如 'daily@20260101'
    rows       INTEGER,
    done_at    TEXT DEFAULT (datetime('now','localtime'))
);
"""


# ---------------------------------------------------------------- 连接
@contextmanager
def connect(read_only: bool = False):
    """数据库连接。正常退出自动提交，异常自动回滚。"""
    if read_only:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")  # 读写不互相阻塞
    try:
        yield conn
        if not read_only:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> str:
    """建表。重复执行是安全的。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
    return str(DB_PATH)


# ---------------------------------------------------------------- 同步进度
def is_done(task: str) -> bool:
    """这个任务跑过了吗？用来做断点续传。"""
    try:
        with connect(read_only=True) as conn:
            return conn.execute(
                "SELECT 1 FROM sync_log WHERE task=?", (task,)
            ).fetchone() is not None
    except sqlite3.OperationalError:
        return False  # 表还没建


def mark_done(task: str, rows: int = 0) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_log(task, rows) VALUES(?,?)", (task, rows)
        )


# ---------------------------------------------------------------- 写入
def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def upsert(conn, table: str, df) -> int:
    """把 DataFrame 写进表。

    只写表里实际存在的列 —— Tushare 返回的字段比我们建的多，
    多出来的直接丢掉，免得字段名一变就崩。
    """
    if df is None or len(df) == 0:
        return 0
    valid = _columns(conn, table)
    df = df[[c for c in df.columns if c in valid]]
    if df.empty:
        return 0

    # NaN 转 None，SQLite 才认得这是 NULL
    records = df.astype(object).where(df.notna(), None).values.tolist()
    cols = ",".join(df.columns)
    marks = ",".join("?" * len(df.columns))
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", records
    )
    return len(df)


def row_count(table: str) -> int:
    try:
        with connect(read_only=True) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


if __name__ == "__main__":
    path = init_db()
    print(f"数据库已就绪：{path}")
    for t in ("stocks", "daily", "daily_basic", "fina_indicator",
              "balancesheet", "reports"):
        print(f"  {t:<16} {row_count(t):>10,} 行")
