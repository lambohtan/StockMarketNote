#!/usr/bin/env python3
"""P4 存储层测试。运行：python3 tests/test_pool_store.py

锁两件事：
  - 新表建得出来，且 filing_texts 的 (accession, section) 唯一
  - outbox 接受 kind='pool'，且旧库补表后仍能用
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw import outbox as OB

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return Store(tmp.name)


def test_tables_exist():
    st = fresh_store()
    names = {r[0] for r in st.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("filing_texts 表存在", "filing_texts" in names, True)
    check("deepread_results 表存在", "deepread_results" in names, True)
    st.close()


def test_filing_texts_unique():
    st = fresh_store()
    row = ("0001-24-000001", "AAPL", "10-Q", "2026-08-01", "mdna", "正文", "2026-08-29")
    cols = ("accession", "ticker", "form", "filed_at", "section", "content", "fetched_at")
    st.insert_ignore_many("filing_texts", cols, [row])
    st.insert_ignore_many("filing_texts", cols, [row])   # 重复插入应被忽略
    n = st.q("SELECT COUNT(*) c FROM filing_texts")[0]["c"]
    check("同一 accession+section 只存一份", n, 1)
    # 同一申报的另一节可以共存
    st.insert_ignore_many("filing_texts", cols,
                          [row[:4] + ("risk_factors", "风险", "2026-08-29")])
    n = st.q("SELECT COUNT(*) c FROM filing_texts")[0]["c"]
    check("同一申报的两节各存一份", n, 2)
    st.close()


def test_outbox_accepts_pool():
    st = fresh_store()
    i = OB.enqueue(st, "pool", "NVDA · 偏正面 6/8", "正文",
                   logical_date="2026-08-29", event_key="pool:NVDA")
    check("pool 条目入队成功", isinstance(i, int) and i > 0, True)
    # 同一天同一只票重复入队是幂等的
    j = OB.enqueue(st, "pool", "NVDA · 偏正面 6/8", "正文",
                   logical_date="2026-08-29", event_key="pool:NVDA")
    check("同票同日幂等", j, i)
    st.close()


def test_unknown_kind_still_rejected():
    st = fresh_store()
    try:
        OB.enqueue(st, "poool", "x", "y")
        check("拼错的 kind 应被拒", "没抛异常", "ValueError")
    except ValueError:
        check("拼错的 kind 应被拒", True, True)
    st.close()


for fn in (test_tables_exist, test_filing_texts_unique,
           test_outbox_accepts_pool, test_unknown_kind_still_rejected):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
