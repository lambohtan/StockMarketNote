#!/usr/bin/env python3
"""财报章节抽取测试。运行：python3 tests/test_filings_text.py

关键陷阱：目录里也有同样的标题。抽正文必须取**最后一次**出现，
取第一次会把整个目录当成 MD&A。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.sources import filings_text as FT

FAIL = []
FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "sample_10q.txt").read_text(encoding="utf-8")

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_returns_both_keys_always():
    out = FT.extract_sections("完全不相干的文本")
    check("永远返回两个 key", sorted(out.keys()), ["mdna", "risk_factors"])
    check("抽不到时是空串", out["mdna"], "")


def test_mdna_extracted():
    out = FT.extract_sections(FIXTURE)
    check("MD&A 抽到了正文", "Revenue increased 94%" in out["mdna"], True)
    check("MD&A 抓到指引句",
          "gross margin to normalize" in out["mdna"], True)


def test_mdna_not_table_of_contents():
    out = FT.extract_sections(FIXTURE)
    check("MD&A 没把目录抓进来", "TABLE OF CONTENTS" in out["mdna"], False)
    check("MD&A 在 Item 3 处截断",
          "Interest rate risk" in out["mdna"], False)


def test_risk_factors_extracted():
    out = FT.extract_sections(FIXTURE)
    check("风险因素抽到了正文",
          "material weakness" in out["risk_factors"], True)
    check("风险因素在 Item 1B 处截断",
          "Unresolved Staff Comments" in out["risk_factors"], False)


def test_curly_apostrophe():
    """撇号是弯引号时也要命中 —— EDGAR 正文里两种都出现。"""
    text = FIXTURE.replace("Management's", "Management’s")
    out = FT.extract_sections(text)
    check("弯撇号也命中", "Revenue increased 94%" in out["mdna"], True)


def test_truncates_long_sections():
    long_text = FIXTURE.replace(
        "Revenue increased 94% year over year.",
        "Revenue increased 94% year over year. " + "填充。" * 60000)
    out = FT.extract_sections(long_text)
    check("超长章节被截断", len(out["mdna"]) <= FT.MAX_SECTION_CHARS, True)


# I9（便宜的一半）：10-K 里 Item 1A 在前、MD&A（Item 7，这里的固件用
# Item 2 简化模拟同样的结构问题）在后，MD&A 正文里"see Item 1A. Risk
# Factors"这类后置交叉引用常见——_slice 取最后一次命中会落在这里，而
# _RISK_END 在它之后找不到 Item 1B/2，于是把文档尾部一大段当成
# risk_factors 抽出来。本轮只做检测：给 detail 加一层合理性检查。
_CROSS_REF_10K = (
    "Item 1A. Risk Factors\n"
    "Real risk factor content goes here.\n"
    "Item 1B. Unresolved Staff Comments\n"
    "None.\n"
    "Item 2. Management's Discussion and Analysis\n"
    "We faced various challenges, see Item 1A. Risk Factors for details.\n"
    "Item 3. Quantitative"
)


def test_extraction_warnings_flags_cross_reference():
    """最后一次命中落在句中交叉引用上时，detail 至少要能报出可疑，
    不能和干净抽取一样 ok=True、detail=''。"""
    warnings = FT.extraction_warnings(_CROSS_REF_10K)
    check("risk_factors 抽取被标记为可疑",
          any("risk_factors" in w for w in warnings), True)


def test_extraction_warnings_silent_on_clean_heading():
    """真实固件里独立成行的标题（不是句中引用）不该被误判为可疑。"""
    warnings = FT.extraction_warnings(FIXTURE)
    check("干净抽取没有警告", warnings, [])


for fn in (test_returns_both_keys_always, test_mdna_extracted,
           test_mdna_not_table_of_contents, test_risk_factors_extracted,
           test_curly_apostrophe, test_truncates_long_sections,
           test_extraction_warnings_flags_cross_reference,
           test_extraction_warnings_silent_on_clean_heading):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
