"""按固定路径加载 `.env`，不依赖当前工作目录。

上游在 `tradingagents/__init__.py` 里调 `load_dotenv(find_dotenv(usecwd=True))`
—— 从 **cwd** 往上找。从仓库根运行时能读到，从别处运行就读不到。
将来接 launchd 定时跑时 cwd 不可控，这是历史上踩过的坑。

所以这里改成按绝对路径找仓库根的 `.env`。用 ``override=False``，
命令行/launchd 显式传入的环境变量始终优先于文件。
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_PATHS = (REPO_ROOT / ".env",)


def load_env() -> list[Path]:
    """加载找到的 `.env`，返回实际读取的路径列表（可能为空）。"""
    loaded: list[Path] = []
    try:
        from dotenv import load_dotenv
    except ImportError:
        return loaded
    for path in SEARCH_PATHS:
        if path.is_file():
            load_dotenv(path, override=False)
            loaded.append(path)
    return loaded


def credential_report() -> str:
    """一行凭证状态，用于启动时打印。绝不打印 key 本身。"""
    optional = ("FRED_API_KEY", "ALPHA_VANTAGE_API_KEY")
    marks = [f"{name}{'✓' if os.environ.get(name) else '✗'}" for name in optional]
    return " ｜ ".join(marks)
