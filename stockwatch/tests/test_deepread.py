#!/usr/bin/env python3
"""双路判定测试。运行：python3 tests/test_deepread.py

三条铁律，逐条锁死：
  1. stage1 的输入里不能出现 py 的结论（否则 LLM 顺着抄，交叉验证失效）
  2. 分歧 ≥2 条时标签必须带「可信度低」
  3. LLM 挂了不抛异常，只出 py 部分（但 LLMConfigError 是配置错误，必须冒泡）
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.deepread import deepread as D
from sw import llm as LM

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


class Cfg:
    def get(self, key, default=None):
        return {"llm.model": "claude-opus-5"}.get(key, default)


FACTS = {"ticker": "NVDA", "revenue_yoy": 0.94, "operating_cash_flow": 1e10,
         "gross_margin_delta_pt": -0.4, "avg_corr_to_holdings": 0.71,
         "form4_filings_30d": 4, "rank": 12, "rank_prev": 50, "rank_delta": 38,
         "pe": 52.3, "forward_pe": 31.8, "missing": []}

PY_RESULT = {"hits": {"revenue_growth": True, "operating_cash_flow": True,
                      "gross_margin": True, "low_correlation": False,
                      "insider_filings": True, "rank_jump": True},
             "details": {k: k for k in ("revenue_growth", "operating_cash_flow",
                                        "gross_margin", "low_correlation",
                                        "insider_filings", "rank_jump")},
             "hit": 5, "total": 6}

LLM_JSON = json.dumps({
    "hits": {"revenue_growth": True, "operating_cash_flow": True,
             "gross_margin": False, "low_correlation": False,
             "insider_filings": True, "rank_jump": True},
    "quotes": {"gross_margin": "we expect gross margin to normalize in the mid-70s"},
    "text_hits": {"guidance_direction": False, "no_new_risk": True},
    "text_quotes": {"guidance_direction": "we expect gross margin to normalize"},
}, ensure_ascii=False)


def test_material_excludes_py_conclusions():
    m = D.build_material("NVDA", {"mdna": "Revenue up 94%", "risk_factors": "R"},
                         [{"title": "t", "text": "n", "full": True}], FACTS)
    check("材料含财报正文", "Revenue up 94%" in m, True)
    check("材料含原始数字", "0.94" in m or "94" in m, True)
    for word in ("命中", "未命中", "偏正面", "hit", "criteria_hit"):
        check(f"材料不含 py 结论 {word!r}", word in m, False)


def test_stage1_parses_json():
    r = D.stage1(Cfg(), "NVDA", "材料", caller=lambda *a, **k: LLM_JSON)
    check("解析出 6 条对照判定", r["hits"]["gross_margin"], False)
    check("解析出文本判定", r["text_hits"]["guidance_direction"], False)
    check("解析出引用", "mid-70s" in r["quotes"]["gross_margin"], True)


def test_stage1_llm_failure_returns_empty():
    r = D.stage1(Cfg(), "NVDA", "材料", caller=lambda *a, **k: "")
    check("LLM 返回空不抛异常", r["hits"], {})
    check("文本判定也空", r["text_hits"], {})


def test_stage1_garbage_returns_empty():
    r = D.stage1(Cfg(), "NVDA", "材料", caller=lambda *a, **k: "不是 JSON")
    check("垃圾输出不抛异常", r["hits"], {})


def test_disagreements_listed():
    llm = json.loads(LLM_JSON)
    d = D.disagreements(PY_RESULT, {"hits": llm["hits"], "quotes": llm["quotes"],
                                    "text_hits": llm["text_hits"]})
    keys = [x["key"] for x in d]
    check("毛利率被列为分歧", "gross_margin" in keys, True)
    check("只有一条分歧", len(d), 1)
    check("两边结论都带上", (d[0]["py"], d[0]["llm"]), (True, False))


def test_label_thresholds():
    check("6/8 偏正面", D.label_for(6, 8, 0)["label"], "偏正面")
    check("5/8 中性", D.label_for(5, 8, 0)["label"], "中性")
    check("3/8 中性", D.label_for(3, 8, 0)["label"], "中性")
    check("2/8 偏负面", D.label_for(2, 8, 0)["label"], "偏负面")


def test_label_thresholds_scale_with_shrunk_denominator():
    """数据缺失时分母缩小，比例阈值不变。"""
    check("3/4 仍是偏正面", D.label_for(3, 4, 0)["label"], "偏正面")
    check("1/4 是偏负面", D.label_for(1, 4, 0)["label"], "偏负面")


def test_two_disagreements_lower_confidence():
    check("1 条分歧无附注", D.label_for(6, 8, 1)["note"], "")
    check("2 条分歧标可信度低",
          "可信度低" in D.label_for(6, 8, 2)["note"], True)


def test_stage2_counts_text_hits_into_score():
    llm = {"hits": json.loads(LLM_JSON)["hits"],
           "quotes": {}, "text_hits": {"guidance_direction": False, "no_new_risk": True}}
    r = D.stage2(Cfg(), "NVDA", PY_RESULT, llm, FACTS,
                 caller=lambda *a, **k: "发生了什么：营收大涨。")
    check("py 5 + llm 文本 1 = 6", r["hit"], 6)
    check("分母 6 + 2 = 8", r["total"], 8)
    check("标签", r["label"], "偏正面")
    check("模型名入结果", r["model"], "claude-opus-5")


def test_stage2_without_llm_still_produces_py_part():
    r = D.stage2(Cfg(), "NVDA", PY_RESULT, {"hits": {}, "quotes": {}, "text_hits": {}},
                 FACTS, caller=lambda *a, **k: "")
    check("LLM 挂了仍出结果", r["hit"], 5)
    check("分母只剩 py 的 6", r["total"], 6)
    check("叙述里写明失败", "LLM 深读失败" in r["narrative"], True)


def test_stage2_total_bounded_by_text_criteria_even_with_extraneous_llm_keys():
    """C2 不变量：LLM 绝不能控制计分的分子和分母。

    修复前 `text_hits` 未按 TEXT_CRITERIA 做 key 白名单也未强制 bool——
    实测多返回 4 个自造 key 能把 5/6 变成 11/12「偏正面」。这里断言
    total 永远不超过 py.total + len(TEXT_CRITERIA)，且额外 key 一个都
    不进最终的 text_hits（不进计分、不进渲染）。
    """
    llm = {
        "hits": {}, "quotes": {},
        "text_hits": {
            "guidance_direction": False, "no_new_risk": True,
            # 模型自造的 4 个 key：不在 TEXT_CRITERIA 白名单里
            "extra_bullish_1": True, "extra_bullish_2": True,
            "extra_bullish_3": True, "extra_bullish_4": True,
        },
    }
    r = D.stage2(Cfg(), "NVDA", PY_RESULT, llm, FACTS,
                caller=lambda *a, **k: "发生了什么：略。")
    check("分母不超过 py.total + len(TEXT_CRITERIA)",
          r["total"] <= PY_RESULT["total"] + len(D.TEXT_CRITERIA), True)
    check("分母精确等于 6+2，自造 key 一个都没混进分母",
          r["total"], PY_RESULT["total"] + len(D.TEXT_CRITERIA))
    check("text_hits 只剩白名单两个 key，自造 key 被丢弃",
          sorted(r["text_hits"].keys()), sorted(D.TEXT_KEYS))
    check("py 5 + llm 文本 1（no_new_risk）= 6，自造 key 没进分子",
          r["hit"], PY_RESULT["hit"] + 1)


def test_stage2_rejects_stringy_truthiness_in_text_hits():
    """字符串 "false"/"no" 在 Python 里非空即真，绝不能被当成命中。"""
    llm = {"hits": {}, "quotes": {},
           "text_hits": {"guidance_direction": "false", "no_new_risk": "no"}}
    r = D.stage2(Cfg(), "NVDA", PY_RESULT, llm, FACTS,
                caller=lambda *a, **k: "发生了什么：略。")
    check("字符串真值不算命中，两条都记 unknown 缩分母", r["hit"], PY_RESULT["hit"])
    check("分母不含这两条（记 unknown，不是未命中）", r["total"], PY_RESULT["total"])
    check("text_hits 两条都归一成 None", list(r["text_hits"].values()), [None, None])


def test_llm_config_error_bubbles_up():
    """配置错误（provider 冲突/缺 key/未知 provider）绝不能被 stage1/stage2 吞成空结果。

    裁定 1：_call() 必须显式重新抛出 LM.LLMConfigError，放在通用
    except Exception 之前 —— 否则配置错误和运行时失败混在一起静默降级，
    排查更难，还可能在没人注意的情况下持续按 API 计费。
    """
    def boom(*a, **k):
        raise LM.LLMConfigError("llm.provider=claude_cli 但环境里有 ANTHROPIC_API_KEY")

    try:
        D.stage1(Cfg(), "NVDA", "材料", caller=boom)
        ok = False
    except LM.LLMConfigError:
        ok = True
    except Exception:
        ok = False
    check("stage1: LLMConfigError 冒泡而不是被吞成空结果", ok, True)

    try:
        D.stage2(Cfg(), "NVDA", PY_RESULT, {"hits": {}, "quotes": {}, "text_hits": {}},
                 FACTS, caller=boom)
        ok = False
    except LM.LLMConfigError:
        ok = True
    except Exception:
        ok = False
    check("stage2: LLMConfigError 冒泡而不是被吞成空结果", ok, True)



def test_tiny_denominator_gets_no_directional_label():
    """分母太小就不给倾向标签 —— 否则是循环论证。

    2026-08-29 实测：MAGA / VTI 只有「热度跃升」一条可判，1/1 = 1.0 直接顶到
    0.75 阈值之上拿到「偏正面」。而那唯一命中的一条**正是它们进池的原因**：
    进池因为热度涨，标签偏正面也因为热度涨，同一个事实用了两遍。
    比例阈值天然让分母越小标签越极端，方向正好反了。
    """
    check("1/1 不给方向", D.label_for(1, 1, 0)["label"], "数据不足")
    check("1/1 说明可判条数", "1 条" in D.label_for(1, 1, 0)["note"], True)
    check("2/2 仍不给方向", D.label_for(2, 2, 0)["label"], "数据不足")
    check("3/3 才开始给", D.label_for(3, 3, 0)["label"], "偏正面")
    check("3 条里命中 0 也给方向", D.label_for(0, 3, 0)["label"], "偏负面")


def test_fund_gets_no_directional_label():
    """ETF/基金不给标签：营收、现金流、毛利率对它们根本不适用。"""
    r = D.label_for(5, 6, 0, is_fund=True)
    check("基金不给方向", r["label"], "基金")
    check("基金注明原因", "不适用" in r["note"], True)


def test_stage2_passes_fund_flag_through():
    llm = {"hits": {}, "quotes": {}, "text_hits": {}}
    facts_fund = dict(FACTS, is_fund=True)
    r = D.stage2(Cfg(), "VTI", PY_RESULT, llm, facts_fund,
                 caller=lambda *a, **k: "发生了什么：略。")
    check("stage2 认得基金", r["label"], "基金")
    check("四段叙述照常出", "发生了什么" in r["narrative"], True)

for fn in (test_material_excludes_py_conclusions, test_stage1_parses_json,
           test_stage1_llm_failure_returns_empty, test_stage1_garbage_returns_empty,
           test_disagreements_listed, test_label_thresholds,
           test_label_thresholds_scale_with_shrunk_denominator,
           test_two_disagreements_lower_confidence,
           test_stage2_counts_text_hits_into_score,
           test_tiny_denominator_gets_no_directional_label,
           test_fund_gets_no_directional_label,
           test_stage2_passes_fund_flag_through,
           test_stage2_without_llm_still_produces_py_part,
           test_stage2_total_bounded_by_text_criteria_even_with_extraneous_llm_keys,
           test_stage2_rejects_stringy_truthiness_in_text_hits,
           test_llm_config_error_bubbles_up):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
