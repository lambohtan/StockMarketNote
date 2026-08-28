#!/usr/bin/env python3
"""恶化提醒的测试。运行：python3 tests/test_alerts.py

⭐ 这个文件是产品约束的执行者，不是风格检查。

CLAUDE.md：「不要求用户做任何事。系统给方向，不下指令。」
判别标准是**这句话在描述世界，还是在指挥用户** —— 描述可以，指挥不行。

所以正样本比负样本更重要：只测「禁了什么」很容易滑向一个什么都不敢说的系统，
那就失去价值了。ALLOWED 里那几句必须能通过。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import alerts as AL

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_banned_directives():
    print("\n负样本：指令性措辞必须被拦下")
    BANNED_SAMPLES = [
        "建议买入 NVDA",
        "建议卖出该持仓",
        "你应该减少科技股敞口",
        "赶紧处理这个仓位",
        "该减仓了",
        "止损设在 $180",
        "目标价 $250",
        "务必在财报前调整",
    ]
    for s in BANNED_SAMPLES:
        try:
            AL.assert_no_directives(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"漏网 {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")


def test_allowed_factual_statements():
    print("\n正样本：客观陈述必须放行（比负样本更重要）")
    ALLOWED_SAMPLES = [
        "内部人集中卖出，排除 10b5-1 预设计划后仍有 3 笔",
        "该公司 CFO 于 08/27 离职，未披露继任安排",
        "这类信号历史上后续 6 个月出现财务重述的比例高于基准",
        "加入后你的组合年化波动率从 56.7% 变为 61.2%",
        "股价当日 -8.7%，其中个股独立部分 -9.1%（4.1σ）",
        "接下来看什么：① 继任公告 ② Q3 财报是否延期 ③ 审计师是否变动",
        "公司同日重申了 Q3 指引",
    ]
    for s in ALLOWED_SAMPLES:
        try:
            AL.assert_no_directives(s)
            print(f"  ✅ 放行：{s[:40]}…")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤 {s}")


def test_six_section_format():
    print("\n六段格式")
    alert = {
        "ticker": "WXYZ", "level": "L1", "category": "8-K",
        "facts": "8-K Item 5.02：CFO 于 08/27 离职，即刻生效，未披露继任安排",
        "data": "股价当日 -8.7%（个股独立部分 -9.1%，4.1σ）",
        "base_rate": "CFO 无预告离职且无继任安排，历史上后续 6 个月出现财务重述"
                     "或业绩不及预期的比例明显高于基准；但相当一部分最终证明是个人原因",
        "counterpoint": "公司同日重申了 Q3 指引；离职生效日与财报窗口无重叠",
        "position": "占卫星仓 4.9%",
        "next_steps": ["继任公告的时间和人选背景", "Q3 财报是否延期", "审计师是否变动"],
    }
    md = AL.render_alert(alert, holding=None)
    for seg in ["发生了什么", "数据", "这类信号通常", "反面观点", "你的持仓", "接下来看什么"]:
        check(f"包含「{seg}」", seg in md, True)
    AL.assert_no_directives(md)
    print("  ✅ 整段通过指令性措辞检查")


def test_push_version_has_no_money():
    print("\n推送版不能出现金额")
    from sw.notify import assert_no_money
    alert = {
        "ticker": "WXYZ", "level": "L1", "category": "8-K",
        # ← 故意在会进入推送正文的字段（facts）里塞金额：
        #   如果只在 position 里塞（position 本来就不会进入 push 正文），
        #   剥离逻辑被删掉也测不出来 —— 这里验证的是剥离本身真的生效。
        "facts": "8-K Item 5.02：CFO 离职，协议约定遣散费 $500,000",
        "data": "当日 -8.7%（个股独立 -9.1%，4.1σ）",
        "base_rate": "历史上后续 6 个月重述比例高于基准",
        "counterpoint": "公司同日重申 Q3 指引",
        "position": "成本 $558.87，市值 $683.94，占卫星仓 4.9%",   # 这个字段本就不进推送正文
        "next_steps": ["继任公告", "Q3 财报是否延期"],
    }
    title, body = AL.render_alert_push(alert)
    assert_no_money(body)      # 抛异常就说明推送版没过滤掉金额（facts 里的 $500,000 漏网了）
    assert_no_money(title)
    check("标题带级别", "L1" in title, True)
    check("标题带代码", "WXYZ" in title, True)
    print("  ✅ 推送版已剥离金额")


def test_level_from_8k_item():
    print("\n8-K item → 级别")
    check("4.02 是 L1", AL.level_for_item("4.02"), "L1")
    check("5.02 是 L1", AL.level_for_item("5.02"), "L1")
    check("7.01 不是 L1", AL.level_for_item("7.01"), None)


if __name__ == "__main__":
    test_banned_directives()
    test_allowed_factual_statements()
    test_six_section_format()
    test_push_version_has_no_money()
    test_level_from_8k_item()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
