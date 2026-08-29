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


for fn in (test_returns_both_keys_always, test_mdna_extracted,
           test_mdna_not_table_of_contents, test_risk_factors_extracted,
           test_curly_apostrophe, test_truncates_long_sections):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
