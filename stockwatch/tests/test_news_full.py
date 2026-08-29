#!/usr/bin/env python3
"""新闻正文抓取测试。运行：python3 tests/test_news_full.py

现实约束：一部分新闻站会挡爬虫或要 JS。取不到正文时必须**如实降级成标题**
并标 full=False，绝不能让 LLM 以为自己读了全文。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.sources import news_full as NF

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


# 用于剥标签测试的最小样本。
HTML = """<html><head><title>t</title>
<script>var x = "不该出现的脚本";</script>
<style>.a{color:red}</style></head>
<body><h1>NVDA 财报</h1><p>数据中心营收同比增长 94%。</p>
<p>管理层预计毛利率回落。</p></body></html>"""

# 拟真长文（抽取后 >= MIN_BODY_CHARS=200），用于验证「取到正文就标 full=True」。
# 抽取后实测 267 字符，跨过 200 字阈值；与下方 HTML_PAYWALL 一高一低夹住边界。
HTML_LONG = """<html><head><title>t</title>
<script>var x = "不该出现的脚本";</script>
<style>.a{color:red}</style></head>
<body><h1>NVDA 财报</h1>
<p>数据中心营收同比增长 94%，主要受益于新一代 AI 加速卡的强劲需求。公司披露，
云计算厂商的订单能见度已经延伸到明年上半年，供应链紧张的状况仍未完全缓解。</p>
<p>管理层预计毛利率回落，原因是新产品线的初期成本较高，叠加部分区域的关税
与出口限制带来的额外支出。首席财务官在电话会议上表示，公司仍将维持较高的
资本开支，用于扩产与新一代芯片的研发投入。</p>
<p>分析师在会后普遍关注下一季度的库存水平与客户集中度问题，部分买方机构
认为估值已经反映了大部分乐观预期，短期内股价对指引的敏感度会明显上升。</p>
</body></html>"""

# 订阅墙常见的短提示语，抽取后远低于 200 字，用于锁住阈值的低侧边界。
HTML_PAYWALL = "<html><body>请订阅后阅读</body></html>"


def test_html_to_text_strips_script_and_style():
    t = NF.html_to_text(HTML)
    check("脚本被剥掉", "不该出现的脚本" in t, False)
    check("样式被剥掉", "color:red" in t, False)
    check("正文留下了", "数据中心营收同比增长 94%" in t, True)
    check("标签被剥掉", "<p>" in t, False)


def test_fetch_uses_full_text_when_available():
    def opener(url, timeout=10):
        return HTML_LONG
    items = [{"summary": "NVDA 财报", "url": "http://x/1"}]
    r = NF.fetch("NVDA", max_items=1, opener=opener,
                 headlines=lambda tk, n: items)
    check("取数成功", r.ok, True)
    check("拿到正文", "毛利率回落" in r.data[0]["text"], True)
    check("标记为全文", r.data[0]["full"], True)


def test_fetch_degrades_to_title_on_error():
    """一条失败、另一条正常 —— 整体仍报 ok（部分降级不是系统性失败）。"""
    def opener(url, timeout=10):
        if url.endswith("/1"):
            raise TimeoutError("站点挡了")
        return HTML_LONG
    items = [{"summary": "NVDA 财报", "url": "http://x/1"},
             {"summary": "NVDA 财报二条", "url": "http://x/2"}]
    r = NF.fetch("NVDA", max_items=2, opener=opener,
                 headlines=lambda tk, n: items)
    check("部分降级时整体仍报 ok", r.ok, True)
    check("失败那条降级成标题", r.data[0]["text"], "NVDA 财报")
    check("失败那条如实标记不是全文", r.data[0]["full"], False)
    check("成功那条标记为全文", r.data[1]["full"], True)


def test_fetch_all_degraded_reports_not_ok():
    """全部条目都降级成标题 —— 新闻站全面挡爬虫，不能仍报 ok=True。

    修复前这里恒为 ok=True，source_health 记的是「健康」，全面挡爬虫这种
    系统性失败会完全隐形；这条测试把回归退回旧行为就会转红。
    """
    def opener(url, timeout=10):
        raise TimeoutError("站点全面挡了")
    items = [{"summary": "NVDA 财报", "url": "http://x/1"}]
    r = NF.fetch("NVDA", max_items=1, opener=opener,
                 headlines=lambda tk, n: items)
    check("全部降级时报 ok=False", r.ok, False)
    check("仍然如实降级成标题（不是连数据都不给）", r.data[0]["text"], "NVDA 财报")


def test_short_body_counts_as_not_full():
    """墙后页面通常返回几十个字的提示语，不能当成正文。"""
    def opener(url, timeout=10):
        return HTML_PAYWALL
    items = [{"summary": "NVDA 财报", "url": "http://x/1"}]
    r = NF.fetch("NVDA", max_items=1, opener=opener,
                 headlines=lambda tk, n: items)
    check("过短正文不算全文", r.data[0]["full"], False)


def test_no_url_degrades():
    items = [{"summary": "只有标题", "url": ""}]
    r = NF.fetch("NVDA", max_items=1, opener=None,
                 headlines=lambda tk, n: items)
    check("没有 URL 也能出结果", r.data[0]["text"], "只有标题")
    check("标记不是全文", r.data[0]["full"], False)


for fn in (test_html_to_text_strips_script_and_style,
           test_fetch_uses_full_text_when_available,
           test_fetch_degrades_to_title_on_error,
           test_fetch_all_degraded_reports_not_ok,
           test_short_body_counts_as_not_full, test_no_url_degrades):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
