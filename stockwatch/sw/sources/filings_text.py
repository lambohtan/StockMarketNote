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


def _looks_like_cross_reference(text, pos):
    """I9（便宜的一半）：判断 `pos` 处的标题命中像不像句中交叉引用。

    10-K 里 Item 1A 在前、Item 7 MD&A 在后，MD&A 正文里"see Item 1A. Risk
    Factors"这类**后置**交叉引用很常见。`_slice` 取的是**最后一次**命中
    （Task 1/3 已确认的必要行为——目录也会命中一次，取第一次会把目录当
    成正文），但当最后一次命中恰好落在这种交叉引用上时，`_RISK_END`
    （`item (1b|2)`）在 10-K 里都排在这次命中**之前**、搜不到，于是把
    正文尾部一大段当成 risk_factors 抽出来。

    这里只做一层**廉价的合理性检查**，不改抽取逻辑本身（需要真实 10-K
    固件才能验证抽取行为，见 HANDOFF 已知限制）：真实章节标题通常独占
    一行（前面是换行或文档开头），交叉引用通常嵌在同一行的句子里、
    紧跟小写单词或逗号（"see Item 1A..."、"discussed in Item 1A,..."）。
    命中就返回 True，供调用方在 detail 里报出「疑似交叉引用」，
    不会让这种情况看起来和干净抽取一样 ok。
    """
    prefix = text[max(0, pos - 40):pos]
    tail = prefix.rstrip()
    if not tail:
        return False       # 文档开头，没有前文可疑
    if re.search(r"\n\s*$", prefix):
        return False        # 紧跟换行，像独立标题行
    return bool(re.search(r"[a-z,]\s*$", tail))


def extract_sections(text):
    """从 10-Q/10-K 正文抽出两节。永远返回两个 key，抽不到就是空串。"""
    text = text or ""
    return {"mdna": _slice(text, _MDNA_START, _MDNA_END),
            "risk_factors": _slice(text, _RISK_START, _RISK_END)}


def extraction_warnings(text):
    """I9：抽取是否疑似落在交叉引用上，而不是真实章节标题。

    只做检测、不改抽取逻辑（本轮只做"便宜的一半"）；返回值供 `fetch()`
    拼进 `detail`，让这种可疑抽取在 source_health 里可见，而不是和干净
    抽取一样报 `ok=True, detail=""`。
    """
    text = text or ""
    warnings = []
    for label, start_re in (("mdna", _MDNA_START), ("risk_factors", _RISK_START)):
        starts = list(start_re.finditer(text))
        if starts and _looks_like_cross_reference(text, starts[-1].start()):
            warnings.append(f"{label} 抽取起点疑似交叉引用而非章节标题")
    return warnings


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
    """生产取数：10-Q 和 10-K 各取一份最新的，按 filed_at 取更新的那份。

    I7 修复：原实现 ``for form in ("10-Q", "10-K")`` 只要存在过任何 10-Q
    就直接返回它——公司刚发完年报（10-K）时，最近一份 10-Q 可能是半年前
    的旧文件，会拿到比最新 10-K 更旧的材料；而 accession 没变，第二天
    再跑会命中缓存、``ok=True cached=True``，从外面完全看不出材料是旧的。
    改成两种 form 都各取一次 latest，比较 ``filed_at`` 取真正更新的那份。

    ``text`` 故意包成零参 callable（``lambda f=latest: f.text()``）而不是
    直接调用 ``latest.text()`` —— 后者会在函数返回前就把完整正文（13 万到
    27 万字符）下载下来，即使 ``fetch()`` 随后发现命中缓存也已经白下载
    一次。包成 callable 之后，真正的网络传输被推迟到 ``fetch()`` 确认
    缓存未命中之后才发生。默认参数 ``f=latest`` 把每次循环迭代的 ``latest``
    显式绑定进 lambda，避免闭包延迟绑定坑（两个 lambda 都在循环体内、
    立即用当次迭代的值绑定默认参数，不依赖循环结束后的最终值）。
    """
    from edgar import Company, set_identity
    set_identity(email)
    company = Company(ticker)
    candidates = []
    for form in ("10-Q", "10-K"):
        filings = company.get_filings(form=form)
        if filings is None:
            continue
        latest = filings.latest(1)
        if latest is None:
            continue
        candidates.append({
            "accession": str(getattr(latest, "accession_no", "") or
                            getattr(latest, "accession_number", "")),
            "form": form,
            "filed_at": str(getattr(latest, "filing_date", ""))[:10],
            "text": (lambda f=latest: f.text()),
        })
    if not candidates:
        raise LookupError(f"{ticker} 没有 10-Q/10-K")
    return max(candidates, key=lambda c: c["filed_at"])


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
    if not accession:
        # I8：accession 是 filing_texts 的主键。空串是合法主键值——如果
        # 放行到 cached()/save()，第二只票拿到空 accession 时会直接命中
        # 第一只票用空 accession 存的缓存，把别家公司的 MD&A 当成自己的
        # 返回，还标 cached=True，从外面完全看不出跨票污染。宁可让这只票
        # 这次直接失败（@timed 装饰器会转成 ok=False 的 SourceResult，
        # 跟其它取数失败走同一条降级路径），也不进缓存层。
        raise ValueError(f"{ticker} 拿到的申报没有 accession，拒绝写入缓存（避免跨票污染）")
    hit = cached(store, accession)
    if hit is not None:
        return SourceResult(source="edgar.filing_text", ok=True, rows=len(hit),
                            data={"accession": accession, "form": meta["form"],
                                  "filed_at": meta["filed_at"],
                                  "sections": hit, "cached": True},
                            detail="缓存命中")
    text = meta.get("text")
    text = text() if callable(text) else text
    text = text or ""
    sections = extract_sections(text)
    save(store, accession, ticker, meta["form"], meta["filed_at"], sections)
    empty = [k for k in SECTIONS if not sections.get(k)]
    # I9：即使两节都抽到了内容，也可能是抽错了地方（最后一次命中落在
    # 交叉引用上）——detail 至少要能报出这种可疑情况，不能和干净抽取一样
    # 什么都不说。
    notes = ([f"未抽到：{','.join(empty)}"] if empty else []) + extraction_warnings(text)
    return SourceResult(
        source="edgar.filing_text", ok=True, rows=len(SECTIONS) - len(empty),
        data={"accession": accession, "form": meta["form"],
              "filed_at": meta["filed_at"], "sections": sections, "cached": False},
        detail="；".join(notes))
