"""推送/LLM 共用的安全策略。

此模块只依赖 stdlib，避免 alerts、notify、llm 之间形成循环导入。金额和
指令守卫在真正 send() 前再次执行，因此所有通知 kind 都走同一条边界。
"""
import re


_CN_CURRENCY_UNITS = r"(?:人民币|港元|港币|日元|欧元|英镑|美元|美金|元)"
_ISO_CODES = r"(?:USD|RMB|CNY|HKD|EUR|GBP|JPY|CNH)"
_QTY = r"(?:十万|百万|千万|十亿|百亿|千亿|万亿|十|百|千|万|亿)?"
_CN_DIGITS = r"[零〇一二两三四五六七八九十百千万亿]+"
_NUMBER = r"\d[\d,]*(?:\.\d+)?"

# 只匹配紧邻币种的数额；百分比、sigma、排名和普通统计数字不命中。
MONEY_RE = re.compile(
    rf"\$\s*{_NUMBER}"
    rf"|[€£¥￥₩₹]\s*{_NUMBER}"
    rf"|{_NUMBER}\s*[€£¥￥₩₹]"
    rf"|{_NUMBER}\s*{_QTY}\s*{_CN_CURRENCY_UNITS}"
    rf"|{_CN_DIGITS}\s*{_QTY}\s*{_CN_CURRENCY_UNITS}"
    rf"|{_CN_CURRENCY_UNITS}\s*(?:{_NUMBER}|{_CN_DIGITS}\s*{_QTY})"
    rf"|{_ISO_CODES}\s*{_NUMBER}"
    rf"|{_NUMBER}\s*{_ISO_CODES}",
    re.IGNORECASE,
)


BANNED_PHRASES = [
    "你应该", "你需要", "你必须", "请立即", "赶紧", "务必",
    "该减仓", "该清仓", "止损设在",
]

ATTRIBUTION_EXEMPT_PHRASES = [
    "建议买入", "建议卖出", "建议持有", "目标价", "买入价", "卖出价",
    "建议减", "建议加",
]

ATTRIBUTION_MARKERS = [
    "分析师", "券商", "报告称", "维持", "重申", "发行人", "公司称", "表示，",
    "据报道", "据悉", "据透露", "据了解", "据称", "据知情人士",
    "有报道", "报道称", "媒体报道", "新闻报道", "相关报道", "独家报道",
    "上调目标", "上调评级", "上调至", "上调预期",
    "下调目标", "下调评级", "下调至", "下调预期",
    "审计师", "审计报告", "审计意见", "审计整改", "审计函", "接受审计",
    "评级机构", "信用评级", "主体评级", "评级展望", "给予评级", "维持评级",
    "机构投资者", "金融机构", "监管机构", "机构预测", "机构观点", "机构持仓",
]

_EXTRA_MARKERS = {
    "买入价": ["交易", "协议", "收购"],
    "卖出价": ["交易", "协议", "收购"],
}
_NOUN_SUFFIX = "方|家|人|者|股东|公司|机构|基金|集团|企业"
_DIRECTIVE_UNLESS_FOLLOWED_BY_NOUN = [
    re.compile(rf"该买(?!{_NOUN_SUFFIX})"),
    re.compile(rf"该卖出(?!{_NOUN_SUFFIX})"),
]
_HARD_SENTENCE_MARKS = "。！？"
_SOFT_CLAUSE_MARKS = "，,；;\n"
_MAX_SOFT_CLAUSE_GAP = 1


def _start_positions(text, needle):
    positions = []
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return positions
        positions.append(i)
        start = i + 1


def _has_marker_before(text, pos, markers):
    for marker in markers:
        for marker_pos in _start_positions(text, marker):
            if marker_pos >= pos:
                continue
            gap = text[marker_pos:pos]
            if any(ch in _HARD_SENTENCE_MARKS for ch in gap):
                continue
            if sum(ch in _SOFT_CLAUSE_MARKS for ch in gap) > _MAX_SOFT_CLAUSE_GAP:
                continue
            return True
    return False


def assert_no_money(text):
    """金额命中即拒绝，避免公共 ntfy 服务器收到账户金额。"""
    match = MONEY_RE.search(text or "")
    if match:
        raise ValueError(f"推送正文里出现金额 {match.group(0)!r}")


def assert_no_directives(text):
    """拒绝指挥用户的句式，保留带明确归属的客观转述。"""
    value = text or ""
    for phrase in BANNED_PHRASES:
        if phrase in value:
            raise ValueError(f"输出里出现指令性措辞 {phrase!r}")
    for pattern in _DIRECTIVE_UNLESS_FOLLOWED_BY_NOUN:
        match = pattern.search(value)
        if match:
            raise ValueError(f"输出里出现指令性措辞 {match.group(0)!r}")
    for phrase in ATTRIBUTION_EXEMPT_PHRASES:
        markers = ATTRIBUTION_MARKERS + _EXTRA_MARKERS.get(phrase, [])
        for pos in _start_positions(value, phrase):
            if not _has_marker_before(value, pos, markers):
                raise ValueError(f"输出里出现指令性措辞 {phrase!r}")
