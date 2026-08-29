"""财报正文：章节抽取与缓存。

**为什么只抽 MD&A 和风险因素两节**：10-Q 全文动辄几十万字符，绝大部分是
数字表格和会计政策样板文 —— 数字我们自己从 yfinance 算（硬约束：数值必须
确定性），样板文没有信息量。MD&A 是公司自己解释「这季度发生了什么」的地方，
风险因素的**新增项**是管理层被迫披露的坏消息，两者信噪比最高。

**为什么取最后一次出现的标题**：10-Q 开头的目录里有一模一样的标题行，
取第一次命中会把整个目录当成正文抽走。
"""
import re
from datetime import datetime

from .base import SourceResult, timed

# 抽出来的单节上限。超过这个长度的 MD&A 极少见，且再长也塞不进一次 LLM 调用。
MAX_SECTION_CHARS = 60000

# 章节标题在各家公司的写法不统一：Item 后可能没空格，撇号可能是弯的 ’，
# 分隔符可能是句点、冒号或各种破折号。
_MDNA_START = re.compile(
    r"item\s*2\s*[.\-–—:]?\s*management[’'`]?s\s+discussion", re.I)
_MDNA_END = re.compile(r"item\s*3\s*[.\-–—:]?\s*quantitative", re.I)
_RISK_START = re.compile(r"item\s*1a\s*[.\-–—:]?\s*risk\s+factors", re.I)
_RISK_END = re.compile(r"item\s*(1b|2)\s*[.\-–—:]?\s*\w", re.I)


def _slice(text, start_re, end_re):
    """取最后一次出现的标题到下一个章节标题之间的正文。"""
    starts = list(start_re.finditer(text))
    if not starts:
        return ""
    s = starts[-1].start()
    end = end_re.search(text, s + 10)
    seg = text[s:end.start()] if end else text[s:]
    seg = re.sub(r"[ \t ]+", " ", seg)
    seg = re.sub(r"\n{3,}", "\n\n", seg).strip()
    return seg[:MAX_SECTION_CHARS]


def extract_sections(text):
    """从 10-Q/10-K 正文抽出两节。永远返回两个 key，抽不到就是空串。"""
    text = text or ""
    return {"mdna": _slice(text, _MDNA_START, _MDNA_END),
            "risk_factors": _slice(text, _RISK_START, _RISK_END)}


SECTIONS = ("mdna", "risk_factors")


def cached(store, accession):
    """按 accession 读缓存。两节都不在就返回 None。"""
    rows = store.q("SELECT section, content FROM filing_texts WHERE accession=?",
                   (accession,))
    if not rows:
        return None
    return {r["section"]: r["content"] for r in rows}


def save(store, accession, ticker, form, filed_at, sections):
    """只追加。同一 accession+section 重复写入被主键忽略。"""
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(accession, ticker, form, filed_at, name, sections.get(name, ""), now)
            for name in SECTIONS]
    store.insert_ignore_many(
        "filing_texts",
        ("accession", "ticker", "form", "filed_at", "section", "content", "fetched_at"),
        rows)


def previous(store, ticker, before_filed_at):
    """上一期申报的两节，用于「风险因素是否新增重大项」的对比。没有就 None。"""
    row = store.q(
        "SELECT accession FROM filing_texts WHERE ticker=? AND filed_at<? "
        "ORDER BY filed_at DESC LIMIT 1", (ticker, before_filed_at))
    if not row:
        return None
    return cached(store, row[0]["accession"])


def _edgar_fetcher(email, ticker):
    """生产取数：拿最近一份 10-Q，没有就退回 10-K。

    ``text`` 故意包成零参 callable（``lambda: latest.text()``）而不是直接调用
    ``latest.text()`` —— 后者会在函数返回前就把完整正文（13 万到 27 万字符）
    下载下来，即使 ``fetch()`` 随后发现命中缓存也已经白下载一次。包成
    callable 之后，真正的网络传输被推迟到 ``fetch()`` 确认缓存未命中之后
    才发生。这里 ``latest`` 是在 for 循环里、return 之前绑定的局部变量，
    没有闭包延迟绑定的坑（不是在循环结束后才创建 lambda）。
    """
    from edgar import Company, set_identity
    set_identity(email)
    company = Company(ticker)
    for form in ("10-Q", "10-K"):
        filings = company.get_filings(form=form)
        if filings is None:
            continue
        latest = filings.latest(1)
        if latest is None:
            continue
        return {"accession": str(getattr(latest, "accession_no", "") or
                                getattr(latest, "accession_number", "")),
                "form": form,
                "filed_at": str(getattr(latest, "filing_date", ""))[:10],
                "text": lambda: latest.text()}
    raise LookupError(f"{ticker} 没有 10-Q/10-K")


@timed("edgar.filing_text")
def fetch(email, ticker, store, fetcher=None):
    """取一只票最近一份财报的两节正文；命中缓存就不重新抽取。

    ``fetcher`` 是注入点，签名 ``(email, ticker) -> {accession, form, filed_at, text}``。
    缓存判定放在拿到 accession 之后、取正文之前 —— accession 是申报的唯一
    标识，只有出现新申报才值得重新抽取正文。``text`` 允许是字符串，也允许
    是零参 callable：只有确认缓存未命中时才会调用它取真正的正文，这样命中
    缓存的路径完全不产生本任务里最贵的那次网络往返（下载整份 10-Q 正文）。
    """
    meta = (fetcher or _edgar_fetcher)(email, ticker)
    accession = meta["accession"]
    hit = cached(store, accession)
    if hit is not None:
        return SourceResult(source="edgar.filing_text", ok=True, rows=len(hit),
                            data={"accession": accession, "form": meta["form"],
                                  "filed_at": meta["filed_at"],
                                  "sections": hit, "cached": True},
                            detail="缓存命中")
    text = meta.get("text")
    text = text() if callable(text) else text
    sections = extract_sections(text or "")
    save(store, accession, ticker, meta["form"], meta["filed_at"], sections)
    empty = [k for k in SECTIONS if not sections.get(k)]
    return SourceResult(
        source="edgar.filing_text", ok=True, rows=len(SECTIONS) - len(empty),
        data={"accession": accession, "form": meta["form"],
              "filed_at": meta["filed_at"], "sections": sections, "cached": False},
        detail=("未抽到：" + ",".join(empty)) if empty else "")
