import os, yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

class Config:
    def __init__(self, path=None):
        p = Path(path) if path else ROOT / "config.yaml"
        with open(p, "r", encoding="utf-8") as f:
            self._d = yaml.safe_load(f)

    def get(self, dotted, default=None):
        cur = self._d
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def db_path(self):
        # 环境变量优先，方便用测试库跑验证而不污染真实快照历史
        env = os.environ.get("STOCKWATCH_DB")
        p = Path(env) if env else Path(self.get("data.db_path", "data/stockwatch.db"))
        return p if p.is_absolute() else ROOT / p

    @property
    def ntfy_topic(self):
        # 环境变量优先：topic 名等同于密码，不该硬编码进要进 git 的文件
        return os.environ.get("STOCKWATCH_NTFY_TOPIC") or self.get("notify.ntfy_topic", "")

    @property
    def ntfy_url(self):
        t = self.ntfy_topic
        if not t:
            return None
        return f'{self.get("notify.ntfy_server", "https://ntfy.sh").rstrip("/")}/{t}'

    @property
    def uploads_dir(self):
        p = Path(self.get("data.uploads_dir", "uploads"))
        return p if p.is_absolute() else ROOT / p

CFG = Config()
