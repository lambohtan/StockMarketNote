"""双路判定：py 与 LLM 各判一遍，分歧逐条暴露。

**stage1 的材料里绝不能出现 py 的结论。** 给了它就会顺着抄，
两路判定退化成一路，交叉验证失去意义 —— 这是这个模块最重要的一条。

**标签用比例阈值不用绝对条数**：数据缺失时分母会缩小（4/6 而不是 4/8），
用绝对条数会让缺数据的票系统性偏负面。

关于 ``_call()`` 里两处偏离原始设计草稿的地方：

1. ``LM.LLMConfigError``（provider 配置冲突、缺 API key、未知 provider）
   必须重新抛出，绝不能和运行时失败一起被吞成空字符串。这跟
   ``sw/llm.py`` 里 ``summarize()`` docstring 的设计是同一条：配置错误
   放过去不会"优雅降级"，只会变成报告里莫名其妙少一段，排查更难，
   还可能在没人注意的情况下持续按 API 计费。
2. 判断 ``caller`` 是否接受 ``store`` 关键字参数，用
   ``inspect.signature`` 显式检查，不用"先按新签名调、TypeError 就退回
   旧签名再调一次"这种写法。后者会把调用体内部真实的 TypeError（比如
   prompt 拼接写错、材料里混进了 None）跟"签名不兼容"混在一起，吞掉真错误，
   还可能重复调用一次外部 LLM。
"""
import inspect
import json

from .criteria import CRITERIA, DISCLAIMER
from .. import llm as LM

POSITIVE_RATIO = 0.75      # 6/8
NEGATIVE_RATIO = 0.375     # 3/8
DISAGREE_WARN = 2

TEXT_CRITERIA = [
    {"key": "guidance_direction", "label": "管理层指引未转弱"},
    {"key": "no_new_risk", "label": "风险因素无新增重大项"},
]

STAGE1_INSTRUCTION = """你在读一家美股公司的财报正文与新闻。只输出 JSON，不要解释。

格式：
{"hits": {"revenue_growth": true/false/null, "operating_cash_flow": ...,
          "gross_margin": ..., "low_correlation": ..., "insider_filings": ...,
          "rank_jump": ...},
 "quotes": {"<上面任一 key>": "支持该判断的财报原文片段"},
 "text_hits": {"guidance_direction": true/false/null, "no_new_risk": true/false/null},
 "text_quotes": {"guidance_direction": "原文片段", "no_new_risk": "原文片段"}}

判定口径：
- revenue_growth: 营收同比增速是否 ≥ 20%
- operating_cash_flow: 经营现金流是否为正
- gross_margin: 毛利率环比是否未恶化
- low_correlation: 这只票的驱动因素是否与大型科技股不同
- insider_filings: 近期内部人申报是否密集
- rank_jump: 社区讨论热度是否明显上升
- guidance_direction: 管理层指引**未**转弱为 true，转弱为 false
- no_new_risk: 风险因素**无**新增重大项为 true，有新增为 false

材料里没有依据的一律填 null，不要猜。
每条 true/false 都要在 quotes 里给出原文片段；给不出原文的改填 null。
不要给出买卖建议、目标价或仓位意见。"""

STAGE2_INSTRUCTION = """你在写一份个人投资研究笔记的一只股票小节。用中文写四段，
每段以固定标题开头，段间空一行：

发生了什么：（两三句，只讲事实）
多头论点：（材料里支持正面的依据）
空头论点：（材料里支持负面的依据）
接下来盯什么：（三条待观察的具体信息，编号 ① ② ③）

硬性要求：
- 不要给出买卖建议、目标价、仓位比例，不要用祈使句指挥读者
- 不要写任何美元金额
- 只使用给定材料里的数字，不要自己算新数字
- 如果 py 与 LLM 的判定有分歧，在「发生了什么」里点明分歧在哪一条"""


def build_material(ticker, sections, news, facts):
    """组 stage1 的输入。**只放原始材料与原始数字，不放任何判定结论。**"""
    parts = [f"公司代码：{ticker}", "", "== 原始数字 =="]
    labels = {
        "revenue_yoy": "最近一季营收同比",
        "operating_cash_flow": "最近一季经营现金流",
        "gross_margin_delta_pt": "毛利率环比变化(百分点)",
        "avg_corr_to_holdings": "与用户现有持仓的 60 日平均相关性",
        "form4_filings_30d": "近 30 天 Form 4 申报份数",
        "rank": "社区热度排名", "rank_prev": "24 小时前排名",
        "pe": "当前 PE", "forward_pe": "forward PE",
    }
    for key, label in labels.items():
        v = facts.get(key)
        parts.append(f"{label}：{'未知' if v is None else v}")

    parts += ["", "== 财报 MD&A ==", (sections or {}).get("mdna") or "（未取到）",
              "", "== 财报风险因素 ==", (sections or {}).get("risk_factors") or "（未取到）",
              "", "== 上一期风险因素 ==",
              (sections or {}).get("prev_risk_factors") or "（无上一期可比）",
              "", "== 新闻 =="]
    for item in (news or []):
        mark = "" if item.get("full") else "（只有标题）"
        parts.append(f"- {item.get('title', '')}{mark}\n{item.get('text', '')}")
    return "\n".join(parts)


def _accepts_store(fn):
    """裁定 2：显式看签名，不靠 except TypeError 试错。

    ``lambda *a, **k: ...`` 这类接受任意关键字参数的签名，
    ``inspect.signature`` 能正确识别为"接受 store"。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        # 拿不到签名（比如某些 C 扩展可调用对象），保守按"接受"处理，
        # 交给真实调用的结果说话，而不是在这里猜错、悄悄换一种调法。
        return True
    if "store" in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call(cfg, caller, material, instruction, store):
    fn = caller if caller is not None else LM.summarize
    try:
        if _accepts_store(fn):
            return fn(cfg, material, instruction, store=store) or ""
        return fn(cfg, material, instruction) or ""
    except LM.LLMConfigError:
        # 裁定 1：配置错误必须冒泡，不能和运行时失败一起被吞成空字符串。
        raise
    except Exception:
        return ""


def stage1(cfg, ticker, material, store=None, caller=None):
    """LLM 独立判定。解析失败一律退化成空判定，绝不抛异常（LLMConfigError 除外）。"""
    raw = _call(cfg, caller, material, STAGE1_INSTRUCTION, store)
    empty = {"hits": {}, "quotes": {}, "text_hits": {}, "text_quotes": {}}
    if not raw:
        return empty
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return empty
    if not isinstance(obj, dict):
        return empty
    return {"hits": obj.get("hits") or {}, "quotes": obj.get("quotes") or {},
            "text_hits": obj.get("text_hits") or {},
            "text_quotes": obj.get("text_quotes") or {}}


def disagreements(py_result, llm_result):
    """py 与 LLM 在同一条上给出相反结论的条目。任一方为 None 不算分歧。"""
    out = []
    llm_hits = (llm_result or {}).get("hits") or {}
    quotes = (llm_result or {}).get("quotes") or {}
    labels = {c["key"]: c["label"] for c in CRITERIA}
    for c in CRITERIA:
        key = c["key"]
        p, l = py_result["hits"].get(key), llm_hits.get(key)
        if p is None or l is None or bool(p) == bool(l):
            continue
        out.append({"key": key, "label": labels[key], "py": bool(p), "llm": bool(l),
                    "py_detail": py_result["details"].get(key, ""),
                    "llm_quote": quotes.get(key, "")})
    return out


def label_for(hit, total, n_disagree):
    """比例阈值：≥0.75 偏正面，<0.375 偏负面，中间中性。"""
    if total <= 0:
        return {"label": "数据不足", "note": "没有任何一条标准可判定"}
    ratio = hit / total
    label = ("偏正面" if ratio >= POSITIVE_RATIO
             else "偏负面" if ratio < NEGATIVE_RATIO else "中性")
    note = (f"py 与 LLM 有 {n_disagree} 条分歧，此标签可信度低"
            if n_disagree >= DISAGREE_WARN else "")
    return {"label": label, "note": note}


def stage2(cfg, ticker, py_result, llm_result, facts, store=None, caller=None):
    """汇总两路结论 + 写四段。计分 = py 的 6 条 + LLM 的 2 条文本判定。"""
    text_hits = (llm_result or {}).get("text_hits") or {}
    known_text = {k: v for k, v in text_hits.items() if v is not None}
    hit = py_result["hit"] + sum(1 for v in known_text.values() if v)
    total = py_result["total"] + len(known_text)
    dis = disagreements(py_result, llm_result)
    tag = label_for(hit, total, len(dis))

    material = json.dumps({
        "ticker": ticker,
        "py_判定": {k: py_result["details"].get(k) for k in py_result["hits"]},
        "py_命中": py_result["hits"],
        "llm_判定": (llm_result or {}).get("hits") or {},
        "llm_文本判定": text_hits,
        "llm_引用": (llm_result or {}).get("quotes") or {},
        "分歧": [{"条目": d["label"], "py": d["py"], "llm": d["llm"]} for d in dis],
        "原始数字": {k: v for k, v in facts.items() if k != "missing"},
    }, ensure_ascii=False)
    narrative = _call(cfg, caller, material, STAGE2_INSTRUCTION, store)
    if not narrative:
        narrative = ("LLM 深读失败，本节只有确定性计算的部分。"
                     "四段叙述缺失，不代表没有值得看的东西。")

    return {"ticker": ticker, "label": tag["label"], "note": tag["note"],
            "hit": hit, "total": total, "narrative": narrative,
            "disagreements": dis, "text_hits": text_hits,
            "text_quotes": (llm_result or {}).get("text_quotes") or {},
            "model": cfg.get("llm.model", "claude-opus-5"),
            "disclaimer": DISCLAIMER}
