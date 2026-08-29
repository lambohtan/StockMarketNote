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


for fn in (test_material_excludes_py_conclusions, test_stage1_parses_json,
           test_stage1_llm_failure_returns_empty, test_stage1_garbage_returns_empty,
           test_disagreements_listed, test_label_thresholds,
           test_label_thresholds_scale_with_shrunk_denominator,
           test_two_disagreements_lower_confidence,
           test_stage2_counts_text_hits_into_score,
           test_stage2_without_llm_still_produces_py_part,
           test_llm_config_error_bubbles_up):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
