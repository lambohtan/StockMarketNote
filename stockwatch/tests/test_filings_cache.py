#!/usr/bin/env python3
"""财报正文缓存测试。运行：python3 tests/test_filings_cache.py

用户要求：读过的财报不重复读，只有出现新申报才重新读。
这里用一个会计数的假 fetcher 锁死「第二次不发请求」。

裁定补丁：fetcher 契约放宽为惰性取正文 —— fetch() 只在缓存未命中时才会
真正取正文（text 可以是零参 callable）。test_cache_hit_does_not_pull_text
锁死这一点：缓存命中时，连正文的 callable 都不该被调用一次。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.sources import filings_text as FT

FAIL = []
CALLS = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return Store(tmp.name)


def fake_fetcher(accession, filed_at, body):
    """返回一个假的 EDGAR 取数函数，并记录调用次数。"""
    def f(email, ticker):
        CALLS.append(ticker)
        return {"accession": accession, "form": "10-Q",
                "filed_at": filed_at, "text": body}
    return f


BODY_V1 = ("Item 1A. Risk Factors\nOld risk.\n"
           "Item 1B. Unresolved\nNone.\n"
           "Item 2. Management's Discussion and Analysis\nRevenue up 40%.\n"
           "Item 3. Quantitative")
BODY_V2 = BODY_V1.replace("Revenue up 40%", "Revenue up 12%")


def test_first_fetch_hits_network_and_caches():
    CALLS.clear()
    st = fresh_store()
    r = FT.fetch("a@b.com", "NVDA", st, fetcher=fake_fetcher("acc-1", "2026-08-01", BODY_V1))
    check("首次取数成功", r.ok, True)
    check("首次走了网络", len(CALLS), 1)
    check("不是缓存", r.data["cached"], False)
    check("抽到 MD&A", "Revenue up 40%" in r.data["sections"]["mdna"], True)
    n = st.q("SELECT COUNT(*) c FROM filing_texts")[0]["c"]
    check("两节都入库", n, 2)
    st.close()


def test_second_fetch_same_accession_uses_cache():
    CALLS.clear()
    st = fresh_store()
    f = fake_fetcher("acc-1", "2026-08-01", BODY_V1)
    FT.fetch("a@b.com", "NVDA", st, fetcher=f)
    CALLS.clear()
    r = FT.fetch("a@b.com", "NVDA", st, fetcher=f)
    check("第二次仍然成功", r.ok, True)
    check("第二次标记为缓存", r.data["cached"], True)
    check("第二次没下载正文", len(CALLS), 1)   # 只查了 accession，没取 text
    check("内容一致", "Revenue up 40%" in r.data["sections"]["mdna"], True)
    st.close()


def test_new_accession_refetches():
    CALLS.clear()
    st = fresh_store()
    FT.fetch("a@b.com", "NVDA", st, fetcher=fake_fetcher("acc-1", "2026-08-01", BODY_V1))
    r = FT.fetch("a@b.com", "NVDA", st, fetcher=fake_fetcher("acc-2", "2026-11-01", BODY_V2))
    check("新申报重新抽取", "Revenue up 12%" in r.data["sections"]["mdna"], True)
    check("新申报不是缓存", r.data["cached"], False)
    n = st.q("SELECT COUNT(*) c FROM filing_texts")[0]["c"]
    check("两期共四节", n, 4)
    st.close()


def test_previous_returns_prior_filing():
    st = fresh_store()
    FT.fetch("a@b.com", "NVDA", st, fetcher=fake_fetcher("acc-1", "2026-08-01", BODY_V1))
    FT.fetch("a@b.com", "NVDA", st, fetcher=fake_fetcher("acc-2", "2026-11-01", BODY_V2))
    prev = FT.previous(st, "NVDA", "2026-11-01")
    check("上一期取到了", "Revenue up 40%" in prev["mdna"], True)
    check("最早一期没有上一期", FT.previous(st, "NVDA", "2026-08-01"), None)
    st.close()


def test_fetch_failure_is_not_fatal():
    st = fresh_store()
    def boom(email, ticker):
        raise RuntimeError("EDGAR 挂了")
    r = FT.fetch("a@b.com", "NVDA", st, fetcher=boom)
    check("失败返回 ok=False", r.ok, False)
    check("失败不抛异常", isinstance(r.detail, str), True)
    st.close()


def test_cache_hit_does_not_pull_text():
    """裁定的锁：命中缓存时，text 的 callable 一次都不该被调用。

    生产的 _edgar_fetcher 在拿到 accession 之前就已经把整份 10-Q 正文
    (latest.text()) 取回来了 —— 13 万到 27 万字符，是整条链路最贵的
    网络往返。裁定要求把「取正文」变成惰性：只有缓存未命中时才调用。
    这里用一个「被调用就记账」的可调用对象模拟 latest.text，
    第二次 fetch（缓存命中）之后它的调用次数必须仍是 0。
    """
    text_calls = []

    def lazy_text():
        text_calls.append(1)
        return BODY_V1

    def fetcher(email, ticker):
        return {"accession": "acc-lazy", "form": "10-Q",
                "filed_at": "2026-08-01", "text": lazy_text}

    st = fresh_store()
    r1 = FT.fetch("a@b.com", "NVDA", st, fetcher=fetcher)
    check("首次（未命中）成功", r1.ok, True)
    check("首次（未命中）调用了一次 lazy_text", len(text_calls), 1)
    check("首次不是缓存", r1.data["cached"], False)

    text_calls.clear()
    r2 = FT.fetch("a@b.com", "NVDA", st, fetcher=fetcher)
    check("第二次（命中缓存）成功", r2.ok, True)
    check("第二次标记为缓存", r2.data["cached"], True)
    check("缓存命中时完全不调用 lazy_text", len(text_calls), 0)
    st.close()


for fn in (test_first_fetch_hits_network_and_caches,
           test_second_fetch_same_accession_uses_cache,
           test_new_accession_refetches, test_previous_returns_prior_filing,
           test_fetch_failure_is_not_fatal, test_cache_hit_does_not_pull_text):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
