"""财报正文：章节抽取与缓存。

**为什么只抽 MD&A 和风险因素两节**：10-Q 全文动辄几十万字符，绝大部分是
数字表格和会计政策样板文 —— 数字我们自己从 yfinance 算（硬约束：数值必须
确定性），样板文没有信息量。MD&A 是公司自己解释「这季度发生了什么」的地方，
风险因素的**新增项**是管理层被迫披露的坏消息，两者信噪比最高。

**为什么取最后一次出现的标题**：10-Q 开头的目录里有一模一样的标题行，
取第一次命中会把整个目录当成正文抽走。
"""
import re

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
