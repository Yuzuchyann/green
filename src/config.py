"""
配置模块

所有密钥（Tushare token、后续的大模型 API key）都从 .env 读取，绝不写死在代码里。
这是本项目最重要的一条安全约定：.env 已经在 .gitignore 里，推不上去。
"""

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"              # 下载的原始 PDF
PROCESSED_DIR = DATA_DIR / "processed"  # 数据库、解析后的文本
LOG_DIR = ROOT / "logs"

for _d in (DATA_DIR, RAW_DIR, PROCESSED_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = PROCESSED_DIR / "greenwash.db"


# ---------------------------------------------------------------- 研究区间
# 事件研究需要日线行情，面板回归需要年度财务。
# 起点定在 2018 年：再往前 ESG 披露太少，样本会碎。
START_DATE = os.getenv("START_DATE", "20180101")
END_DATE = os.getenv("END_DATE", "20261231")


# ---------------------------------------------------------------- 密钥
def _load_env() -> None:
    """加载 .env。装了 python-dotenv 就用它，没装就手写一个极简解析。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
        return
    except ImportError:
        pass
    # 回退：几行代码能解决的事，不值得强制多装一个包
    with env_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def get_tushare_token() -> str:
    """读取 Tushare token。拿不到就大声报错 —— 静默失败最难调试。"""
    _load_env()
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "没找到 TUSHARE_TOKEN。\n"
            "  1. 注册并登录 https://tushare.pro\n"
            "  2. 个人主页 → 接口 TOKEN → 复制\n"
            "  3. 把 .env.example 复制成 .env，写入：TUSHARE_TOKEN=你的token\n"
            "注意：.env 已在 .gitignore 中，不会被提交。"
        )
    return token
