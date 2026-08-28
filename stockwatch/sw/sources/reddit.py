"""ApeWisdom —— Reddit 讨论热度。免费、无需 key。
只存原始排名，变化率在分析层算（因为 API 只给 24h 前的值，更长周期要靠我们自己的历史）。"""
import requests
from datetime import date
from .base import SourceResult, timed

BASE = "https://apewisdom.io/api/v1.0/filter/{f}/page/{p}"

@timed("apewisdom")
def fetch_reddit(filters=("all-stocks", "wallstreetbets"), top_n=100, d=None):
    d = d or date.today().isoformat()
    rows, errs = [], []
    for f in filters:
        got = 0
        for page in (1, 2):
            if got >= top_n:
                break
            try:
                r = requests.get(BASE.format(f=f, p=page), timeout=20)
                r.raise_for_status()
                for x in r.json().get("results", []):
                    if got >= top_n:
                        break
                    rows.append((d, f, x.get("ticker"),
                                 _i(x.get("rank")), _i(x.get("mentions")),
                                 _i(x.get("upvotes")), _i(x.get("rank_24h_ago")),
                                 _i(x.get("mentions_24h_ago"))))
                    got += 1
            except Exception as e:
                errs.append(f"{f}p{page}:{type(e).__name__}")
                break
    return SourceResult(source="apewisdom", rows=len(rows), data=rows,
                        ok=len(rows) > 0, detail=",".join(errs[:4]))

def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
