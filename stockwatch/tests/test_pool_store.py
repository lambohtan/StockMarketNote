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
from sw import notify as NT

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


def test_notify_tags_covers_pool_kind():
    """notify.TAGS 缺 'pool' 键时 TAGS.get(row['kind']) 退化成 None——
    不影响发送，但推送在 ntfy 客户端里没有图标，跟其它 kind 观感不一致。"""
    check("TAGS 里有 pool 键", "pool" in NT.TAGS, True)


def test_busy_timeout_set():
    """P4 在 06:45 插进一个会跑几分钟的新写入者，夹在 weekly 06:30 与
    retry 07:00 之间；sqlite3 默认 busy_timeout=0，撞锁立即报
    'database is locked'，不会等对方提交完。"""
    st = fresh_store()
    v = st.conn.execute("PRAGMA busy_timeout").fetchone()[0]
    check("busy_timeout 已设置为非零", v > 0, True)
    st.close()


def test_deepread_results_has_disagreements_detail_column():
    """I5：disagreements 列只是计数，分歧的具体内容存进这一列。"""
    st = fresh_store()
    cols = {r[1] for r in st.conn.execute("PRAGMA table_info(deepread_results)")}
    check("disagreements_detail 列存在", "disagreements_detail" in cols, True)
    st.close()


def test_old_deepread_results_table_gets_backfilled_column():
    """老库在这条修复落地前就建过 deepread_results（没有这一列）——
    CREATE TABLE IF NOT EXISTS 遇到已存在的表会跳过，得靠 _migrate() 补列，
    否则老库上跑新代码会在 INSERT 时报"列数不对"。
    """
    import sqlite3
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE deepread_results (
          d TEXT NOT NULL, ticker TEXT NOT NULL, source TEXT NOT NULL,
          py_hits TEXT NOT NULL, llm_hits TEXT NOT NULL,
          disagreements INTEGER NOT NULL, score_hit INTEGER NOT NULL,
          score_total INTEGER NOT NULL, label TEXT NOT NULL,
          narrative TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY (d, ticker)
        )
    """)
    conn.commit()
    conn.close()
    st = Store(tmp.name)   # 触发 _migrate()
    cols = {r[1] for r in st.conn.execute("PRAGMA table_info(deepread_results)")}
    check("旧库补上了 disagreements_detail 列", "disagreements_detail" in cols, True)
    st.close()


for fn in (test_tables_exist, test_filing_texts_unique,
           test_outbox_accepts_pool, test_unknown_kind_still_rejected,
           test_notify_tags_covers_pool_kind,
           test_busy_timeout_set, test_deepread_results_has_disagreements_detail_column,
           test_old_deepread_results_table_gets_backfilled_column):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
