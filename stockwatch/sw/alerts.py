"""
三级恶化提醒。

CLAUDE.md 的硬性约束：**不要求用户做任何事。系统给方向，不下指令。**
判别标准 —— 这句话在描述世界，还是在指挥用户？描述可以，指挥不行。
「卖」这个字本身不禁：「内部人集中卖出」是客观事实，是合法输出。

六段格式最后一段「接下来看什么」是整个设计的关键：
它把用户从「要不要卖」这个二选一，转成「再收集三个信息」。

## 指令性措辞守卫的设计（修复轮 1，句子边界/该买卖修复轮 2，标记硬化修复轮 3）

禁的是「系统在指挥用户」，不是「文本里出现某些字」。早期版本按纯子串匹配，
会把「转述第三方说了/做了什么」这类合法陈述也拦下 ——「该分析师维持『建议持有』
评级」「高盛下调目标价」都是客观转述，恰恰是 counterpoint（反面观点）字段的
典型内容；把这些也拦下，系统就会变成什么都不敢说，反而违背了产品目标。

所以分两类处理：

1. `BANNED_PHRASES`：明确指向用户的祈使句（"你应该""该减仓""止损设在"等），
   任何情况下都不放行 —— 这类短语几乎不会出现在合法的转述语境里。
2. `ATTRIBUTION_EXEMPT_PHRASES`：本身可能是转述也可能是指挥的短语
   （"建议买入""建议卖出""建议持有""目标价""买入价""卖出价""建议减"）——
   只有当**同一句话**里没有归属标记时才判定为系统自己在下指令。按句子
   （用 。！？；;\n 切分）逐句判断，不是整段判断，避免一句里有归属就把
   整段一起豁免掉。

   修复轮 2 教训：`_counterpoint()` 用「；」拼接 peer 读数和新闻转述，是
   Task 7 接入 LLM 后 counterpoint 字段的典型形状——早期切分符没算上中文
   分号，会让前半句裸露的受限短语蹭到后半句的归属标记。所以分号必须算
   句子边界，这是治根的修法，不是只在 `_counterpoint()` 这一个调用点上
   打补丁（以后别处再用「；」拼接又会漏）。

3. `_EXTRA_MARKERS`：极少数豁免短语（"买入价""卖出价"）除了通用归属标记，
   还认「交易""协议""收购」这类描述已完成并购/交易的词——这类词专属于
   这两个短语，不进入通用 `ATTRIBUTION_MARKERS`，因为如果让"建议买入"这类
   高风险短语也认"交易"，会被"建议买入该交易标的 NVDA"这种句子钻空子
   （提到"交易"不代表就是转述第三方，可能只是系统自己在指挥用户买某个
   跟"交易"沾边的标的）。专属标记只解决"买入价/卖出价"这两个描述历史
   成交价格区间的场景，不放大到全体豁免短语。

「该买」「该卖出」这两个短语本身有歧义：「该」既可能是「应该」的省略，
也可能是「那个/这笔」的指示代词（如「该买家」「该卖出方」是并购报道里
指代交易对手方的标准用语）。

修复轮 2 曾把这两个短语整体删掉（不禁），理由是避免误伤「该卖出方为公司
前董事」；但这样处理只顾了误伤方向，漏了「该卖出这些质地一般的仓位了」
这种真指令会完全不受拦截——同类句式「该减仓」还在禁用表里，一个拦一个
不拦也不自洽。

修复轮 3 改用**名词性后缀豁免**同时满足两个方向（`_DIRECTIVE_UNLESS_
FOLLOWED_BY_NOUN`）：「该买」「该卖出」后面紧跟 方/家/人/者/股东/公司/
机构/基金/集团/企业 这类名词性后缀时，判定为指示代词，放行；后面不是
这些词，就按「应该」的省略处理，拦下。这个机制不依赖归属标记（转述场景
未必会指名道姓说"据xx报道"），而是看短语本身的语法角色。
"""
import re

from .analysis.causes import L1_ITEMS
from .notify import MONEY_RE as _MONEY

# 明确指向用户的祈使句。任何情况下都不放行 —— 这类措辞几乎不会出现在
# 合法的转述语境里，不需要归属豁免。
BANNED_PHRASES = [
    "你应该", "你需要", "你必须", "请立即", "赶紧", "务必",
    "该减仓", "该清仓", "止损设在",
]

# 这几条本身经常出现在"转述第三方在做什么/说什么"的合法陈述里
# （分析师评级动作、审计整改函引述、并购交易的历史成交价区间等），
# 单独按子串匹配会把转述也拦下。
# "建议加"修复轮 3 重新收编：round1 曾因"审计整改函中建议加强内部控制"
# 误伤而整体删除，但这样"建议加仓"这种真指令就完全不受拦截——用归属
# 豁免同时满足两个方向（有"审计整改"这类标记才放行）。
ATTRIBUTION_EXEMPT_PHRASES = [
    "建议买入", "建议卖出", "建议持有", "目标价", "买入价", "卖出价",
    "建议减", "建议加",
]

# 归属标记：同一句话里出现这些词，说明是在转述第三方，不是系统自己在下指令。
# 所有豁免短语通用。
#
# 修复轮 3 教训：单字或两字标记极易被无关词的子串命中，命中之后指令句就会
# 蹭到"归属"而逃逸——这比误伤更危险（误伤最多是话说得束手束脚，漏网是
# 硬性约束被绕过）。逐条复查后把下面这些换成了完整搭配，复查结论见
# task-6-report.md 修复轮 3 部分：
#   "据"   → 根据/数据/占据/依据/证据 都会命中，换成"据报道""据悉"等
#   "报道" → "通报道歉""预报道路"这类无关拼接会命中，换成"有报道""报道称"等
#   "上调" → "以上调整方案"会命中，换成"上调目标""上调评级""上调至"等
#   "下调" → "以下调整方案"会命中，换成"下调目标""下调评级""下调至"等
#   "审计" → "复审计划"会命中，换成"审计师""审计报告""审计函"等
#   "评级" → "批评级别"会命中，换成"评级机构""信用评级"等
#   "机构" → "有机构成"（有机构成变化）会命中，换成"机构投资者""金融机构"等
# "分析师""券商""报告称""维持""重申""发行人""公司称"复查后判断风险可接受，
# 保留原样（理由同样列在报告里，不在这里重复）。
ATTRIBUTION_MARKERS = [
    "分析师", "券商", "报告称", "维持", "重申", "发行人", "公司称",
    "据报道", "据悉", "据透露", "据了解", "据称", "据知情人士",
    "有报道", "报道称", "媒体报道", "新闻报道", "相关报道", "独家报道",
    "上调目标", "上调评级", "上调至", "上调预期",
    "下调目标", "下调评级", "下调至", "下调预期",
    "审计师", "审计报告", "审计意见", "审计整改", "审计函", "接受审计",
    "评级机构", "信用评级", "主体评级", "评级展望", "给予评级", "维持评级",
    "机构投资者", "金融机构", "监管机构", "机构预测", "机构观点", "机构持仓",
]

# 专属标记：只对指定的豁免短语额外生效，不进入通用表，避免被别的高风险
# 短语（如"建议买入"）借用而削弱原有保护。
_EXTRA_MARKERS = {
    "买入价": ["交易", "协议", "收购"],
    "卖出价": ["交易", "协议", "收购"],
}

# 句子边界：中英文句号/问号/感叹号/分号 + 换行。分号必须算边界——
# `_counterpoint()` 用「；」拼接不同来源的陈述，两段之间不该互相借用归属标记。
_SENTENCE_SPLIT = re.compile(r"[。！？；;\n]")

# "该买""该卖出"修复轮 3 重新收编：round2 曾因"该卖出方为公司前董事"这类
# 并购报道用语误伤而整体删除，但这样"该卖出这些质地一般的仓位了"这种真
# 指令就完全不受拦截。用"名词性后缀豁免"同时满足两个方向：后面紧跟
# 方/家/人/者/股东/公司/机构/基金/集团/企业 时，"该买/该卖出"是指示代词
# （指代交易对手方），放行；后面不是这些词，就是"应该"的省略，拦下。
_NOUN_SUFFIX = "方|家|人|者|股东|公司|机构|基金|集团|企业"
_DIRECTIVE_UNLESS_FOLLOWED_BY_NOUN = [
    re.compile(rf"该买(?!{_NOUN_SUFFIX})"),
    re.compile(rf"该卖出(?!{_NOUN_SUFFIX})"),
]


def assert_no_directives(text):
    """指令性措辞守卫。运行时和测试都用它 —— LLM 输出也要过这一关。"""
    t = text or ""
    for p in BANNED_PHRASES:
        if p in t:
            raise ValueError(
                f"输出里出现指令性措辞 {p!r}。"
                f"系统给方向，不下指令 —— 改写成客观陈述（描述世界，而不是指挥用户）")

    for pat in _DIRECTIVE_UNLESS_FOLLOWED_BY_NOUN:
        m = pat.search(t)
        if m:
            raise ValueError(
                f"输出里出现指令性措辞 {m.group(0)!r}（后面不是名词性后缀，视为"
                f"「应该」的省略，而不是指代交易对手方的指示代词）")

    for sentence in _SENTENCE_SPLIT.split(t):
        for p in ATTRIBUTION_EXEMPT_PHRASES:
            if p not in sentence:
                continue
            markers = ATTRIBUTION_MARKERS + _EXTRA_MARKERS.get(p, [])
            if not any(m in sentence for m in markers):
                raise ValueError(
                    f"输出里出现指令性措辞 {p!r}（该句没有归属标记，视为系统自己在下"
                    f"指令）。如果是转述第三方，请在同一句话里带上归属信息"
                    f"（如「分析师」「据报道」「据悉」等）")


def level_for_item(item):
    """8-K item 编号 → 级别。不在 L1 名单里的返回 None。"""
    return "L1" if item in L1_ITEMS else None


def scan(store, portfolio, attributions, causes_by_ticker):
    """
    生成提醒列表。

    L1：8-K 命中 L1 item，或残差 ≥ 4σ 的单日下跌
    L2：残差 ≥ 2σ（进日报，不单独推送）
    L3：本期不实现（属于周报范畴）
    """
    out = []
    by_ticker = {a["ticker"]: a for a in attributions}

    for tk, causes in (causes_by_ticker or {}).items():
        attr = by_ticker.get(tk)
        l1_causes = [c for c in causes if c.get("is_l1")]
        z = (attr or {}).get("z")

        level = None
        if l1_causes:
            level = "L1"
        elif z is not None and z <= -4.0:
            level = "L1"          # 只有大跌升 L1；大涨用不着打断用户
        elif attr and attr.get("level") in ("anomaly", "extreme"):
            level = "L2"
        if not level:
            continue

        h = portfolio.get(tk) if portfolio else None
        w = None
        if portfolio and h:
            ws = portfolio.weights()
            w = ws.get(tk)

        facts = "；".join(c["summary"] for c in (l1_causes or causes)[:3]) \
                or "未找到明确原因"
        # 归因字段可能因样本不足等原因是 None（skipped_days）——
        # 8-K 触发的 L1 不依赖归因数据，不能因为拼这个字符串崩掉。
        data = ""
        if attr and all(attr.get(k) is not None
                         for k in ("ret", "mkt_part", "sector_part", "idio", "z")):
            data = (f"当日 {attr['ret']*100:+.1f}%"
                    f"（大盘 {attr['mkt_part']*100:+.1f}%，"
                    f"行业 {attr['sector_part']*100:+.1f}%，"
                    f"个股独立 {attr['idio']*100:+.1f}%，{attr['z']:+.1f}σ）")
        elif attr:
            data = "当日归因数据不完整（样本不足或数据缺失），无法拆分大盘/行业/个股部分。"

        out.append({
            "ticker": tk,
            "level": level,
            "category": (l1_causes[0]["source"] if l1_causes else "价格异动"),
            "facts": facts,
            "data": data,
            "base_rate": _base_rate(l1_causes),
            "counterpoint": _counterpoint(causes),
            "position": (f"占组合 {w*100:.1f}%" if w is not None else "—"),
            "next_steps": _next_steps(l1_causes, tk),
        })
    return out


def _base_rate(l1_causes):
    """「这类信号通常意味着什么」。只写有据可依的，没有就说没有。"""
    if not l1_causes:
        return ("个股独立部分超出常规波动范围。单日残差本身不预示方向 —— "
                "它只说明市场认为发生了无法用大盘和行业解释的事。")
    item = l1_causes[0].get("item")
    table = {
        "5.02": "高管无预告离职且无继任安排，历史上后续 6 个月出现财务重述或"
                "业绩不及预期的比例高于基准；但相当一部分最终证明是个人原因。",
        "4.02": "公司自己声明前期财报不可信，是会计问题中最严重的一类信号。",
        "4.01": "更换审计师，非自愿更换的信息含量高于自愿更换。",
        "1.03": "破产或接管程序启动。",
        "2.06": "重大资产减值，通常意味着此前的收购或投资未达预期。",
        "1.05": "重大网络安全事件，影响范围往往在数周后才明朗。",
    }
    return table.get(item, "该类申报的历史基准率本系统尚未收录，"
                           "以下判断请以原始申报文件为准。")


def _counterpoint(causes):
    """反面观点。找不到就诚实说明，不硬凑。"""
    news = [c for c in causes if c["source"] == "新闻"]
    peer = [c for c in causes if c["source"] == "同行读数"]
    parts = []
    if peer:
        parts.append(peer[0]["summary"])
    if news:
        parts.append(f"另有报道：{news[0]['summary']}")
    return "；".join(parts) or "本次未检索到明确的反面材料 —— 这不代表不存在，只代表没找到。"


def _next_steps(l1_causes, ticker):
    """把「要不要卖」转成「再收集三个信息」。"""
    item = l1_causes[0].get("item") if l1_causes else None
    table = {
        "5.02": ["继任公告的时间和人选背景", "下一季财报是否延期", "审计师是否随后变动"],
        "4.02": ["重述涉及的具体科目和期间", "审计师是否出具保留意见", "是否触发退市审核"],
        "4.01": ["新审计师的规模与行业经验", "前任审计师的离任函内容", "是否伴随管理层变动"],
    }
    return table.get(item, [
        f"{ticker} 是否有尚未公开的申报（关注未来 4 个工作日的 8-K）",
        "同行业其他公司同期的读数",
        "下一次财报的日期与市场一致预期",
    ])


def render_alert(alert, holding=None):
    """六段格式的完整 Markdown（面板和报告用，可含金额）。"""
    steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(alert["next_steps"], 1))
    pos = alert.get("position", "—")
    if holding is not None and holding.cost_basis is not None:
        pos = (f"{pos}；成本 ${holding.cost_basis:,.2f}，"
               f"市值 ${holding.market_value:,.2f}")
    md = (
        f"### 【{alert['level']}】{alert['ticker']} · {alert['category']}\n\n"
        f"**发生了什么**　{alert['facts']}\n\n"
        f"**数据**　{alert['data']}\n\n"
        f"**这类信号通常意味着什么**　{alert['base_rate']}\n\n"
        f"**反面观点**　{alert['counterpoint']}\n\n"
        f"**你的持仓现状**　{pos}\n\n"
        f"**接下来看什么**\n{steps}\n"
    )
    assert_no_directives(md)
    return md


def render_alert_push(alert):
    """
    推送版：剥离一切金额（ntfy.sh 是公共服务器）。
    返回 (title, body)。
    """
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(alert["next_steps"][:3], 1))
    title = f"【{alert['level']}】{alert['ticker']} · {alert['category']}"
    body = (
        f"发生了什么\n{alert['facts']}\n\n"
        f"数据\n{alert['data']}\n\n"
        f"这类信号通常意味着什么\n{alert['base_rate']}\n\n"
        f"反面观点\n{alert['counterpoint']}\n\n"
        f"接下来看什么\n{steps}"
    )
    # 兜底：把任何漏网的金额抹掉，绝不让它出网
    body = _MONEY.sub("[金额见面板]", body)
    title = _MONEY.sub("", title)
    assert_no_directives(body)
    return title, body
