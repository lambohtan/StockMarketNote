"""报告与推送渲染。

**金额替换必须在截断之前**（daily_report 修复轮 2 已踩过这个坑：
先截断会把 `$1,234` 切成 `$1,2` 之类的残尾，正则匹配不到就漏进推送）。
"""
from ..policy import MONEY_RE
from .criteria import CRITERIA
from .deepread import TEXT_CRITERIA

MAX_PUSH_BYTES = 3800      # ntfy 单条上限 4096 字节，留出头部余量
SOURCE_LABEL = {"holding_anomaly": "持仓异动", "holding_event": "持仓事件",
                "apewisdom": "社区热度", "form4": "Form 4 申报"}

# 「数据不足」「基金」不是倾向，渲染时不能套「在你的标准下：X」的句式 ——
# 那会读成一个方向判断。
NON_DIRECTIONAL = ("数据不足", "基金")


_ELLIPSIS = "…"


def _clip(text, limit=MAX_PUSH_BYTES):
    """按字节截断，不切坏 UTF-8。

    截断长度要先扣掉末尾要追加的省略号占用的字节数——先按 `limit` 截、
    再拼省略号，会让最终字节数超出 `limit`（省略号在 UTF-8 里占 3 字节，
    原始截断刚好卡在上限时会溢出 1~3 字节，push 上限就名存实亡了）。
    """
    b = (text or "").encode("utf-8")
    if len(b) <= limit:
        return text
    suffix = _ELLIPSIS.encode("utf-8")
    b = b[:max(0, limit - len(suffix))]
    return b.decode("utf-8", errors="ignore").rstrip() + _ELLIPSIS



def _label_line(item):
    """标签行。非方向性标签（数据不足 / 基金）不套「在你的标准下」的句式。"""
    score = f"（命中 {item['hit']}/{item['total']}）"
    if item["label"] in NON_DIRECTIONAL:
        return f"**{item['label']}**{score}"
    return f"**在你的标准下：{item['label']}**{score}"


def _hit_lines(item):
    """把 py 六条与 LLM 两条摊开成人话。

    LLM 那两条走 `TEXT_CRITERIA` 白名单 + 严格布尔（与 `deepread.stage2` 的
    计分口径同源）：模型自造的 key 不进渲染，字符串 `"false"` 不算命中。
    否则报告里会出现连标签都没有的条目，而它已经把分母顶高了。
    """
    py = item["py"]
    labels = {c["key"]: c["label"] for c in CRITERIA}
    hit, miss, unknown = [], [], []
    for key, value in py["hits"].items():
        text = f"{labels.get(key, key)}（{py['details'].get(key, '')}）"
        # 三态路由：True → 命中，False → 未命中，None → 数据缺失。
        # ⚠️ 不能简化成 `not value`：那会把 None（数据缺失）并进「未命中」，
        # 正是 spec §6 明令禁止的「把取不到数伪装成不符合」。
        (hit if value else miss if value is False else unknown).append(text)
    raw_text_hits = item.get("text_hits") or {}
    for c in TEXT_CRITERIA:
        key = c["key"]
        if key not in raw_text_hits:
            continue
        value = raw_text_hits[key]
        if value is not True and value is not False:
            value = None          # 非布尔一律当「未判定」，不当命中
        quote = (item.get("text_quotes") or {}).get(key, "")
        text = c["label"] + (f"（原文：{quote[:80]}）" if quote else "")
        (hit if value else miss if value is False else unknown).append(text)
    return hit, miss, unknown


def render_report(d, items):
    """一天一份 markdown，每只票一节。"""
    L = [f"# 股票池深读 · {d}", "",
         f"今日深读 {len(items)} 只。深读名单 = 持仓异动全选 + 新票按信号强度取前几只。", ""]
    if not items:
        L += ["今天没有票进入深读名单。", "",
              "这不代表市场没事 —— 只代表没有触发入池条件的信号。", ""]
    for item in items:
        facts = item.get("facts") or {}
        hit, miss, unknown = _hit_lines(item)
        note = f" · {item['note']}" if item.get("note") else ""
        L += [f"### {item['ticker']} · 入池原因：{item.get('reason', '')}"
              f"（{SOURCE_LABEL.get(item.get('source'), item.get('source', ''))}）", "",
              _label_line(item) + note, ""]
        if hit:
            L.append("命中：" + " · ".join(hit))
        if miss:
            L.append("未命中：" + " · ".join(miss))
        if unknown:
            L.append("数据缺失（不计入分母）：" + " · ".join(unknown))
        rank, prev, delta = facts.get("rank"), facts.get("rank_prev"), facts.get("rank_delta")
        if rank is not None:
            L.append(f"社区热度：当前第 {rank} 名"
                     + (f"，24h 前第 {prev} 名（{delta:+d}）" if delta is not None else ""))
        # 只印取到的那个：两个都拼在一行时，缺失的一侧会字面打出 "None"。
        pe_parts = []
        if facts.get("pe") is not None:
            pe_parts.append(f"当前 PE {facts['pe']}")
        if facts.get("forward_pe") is not None:
            pe_parts.append(f"forward PE {facts['forward_pe']}")
        if pe_parts:
            L.append(" / ".join(pe_parts) + "（原始数字，不参与判定）")
        L.append("")
        for dis in item.get("disagreements") or []:
            L += [f"⚠️ py 与 LLM 分歧：{dis['label']}",
                  f"- py：{dis['py_detail']} → 判{'命中' if dis['py'] else '未命中'}",
                  f"- LLM：{dis['llm_quote'] or '（未给引用）'} → "
                  f"判{'命中' if dis['llm'] else '未命中'}", ""]
        L += [item["narrative"], "",
              f"_{item['disclaimer']}（判定模型：{item['model']}）_", "", "---", ""]
    L += ["", "本报告为个人研究工具的输出，不构成投资建议。"]
    return "\n".join(L)


def render_push(item):
    """一只票一条推送。金额先替换再截断。"""
    title = f"{item['ticker']} · {item['label']} {item['hit']}/{item['total']}"
    hit, miss, _ = _hit_lines(item)
    first = (item.get("narrative") or "").split("\n\n")[0]
    parts = [item.get("reason", "")]
    if hit:
        parts.append("命中：" + " · ".join(hit))
    if miss:
        parts.append("未命中：" + " · ".join(miss))
    if item.get("note"):
        parts.append("⚠️ " + item["note"])
    for dis in item.get("disagreements") or []:
        parts.append(f"⚠️ 分歧：{dis['label']}（py 与 LLM 结论相反）")
    parts += [first, f"全文见 pool_{item.get('d', '')}.md", item["disclaimer"]]
    body = "\n".join(p for p in parts if p)
    # 顺序不能反：先替换金额，再截断 —— 先截断会把长金额切成残尾，
    # 正则匹配不到，金额就漏进推送了（daily_report 修复轮 2 的坑）。
    body = MONEY_RE.sub("[金额见报告]", body)
    return title, _clip(body)
