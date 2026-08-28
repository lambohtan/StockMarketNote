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


def test_attribution_quotes_allowed():
    print("\n修复轮 1 · 转述第三方观点必须放行（判别标准：同一句话有没有归属标记）")
    ALLOWED_QUOTES = [
        "该分析师维持「建议持有」评级",
        "高盛下调目标价至 $180",          # 同时含金额，用 render_alert（完整版可含金额）测
        "厂商建议零售价上调 8%",
        "该买家此前已持有该公司 5% 股份",
        "审计整改函中建议加强内部控制",
        "公司披露其外汇套期保值止损位",
    ]
    for s in ALLOWED_QUOTES:
        try:
            AL.assert_no_directives(s)
            print(f"  ✅ 放行：{s}")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤(转述) {s}")

    # 「高盛下调目标价至 $180」走完整版 render_alert（可含金额），
    # 确认它作为 counterpoint 字段出现时整条提醒也能正常渲染，不被拦截。
    alert = {
        "ticker": "WXYZ", "level": "L2", "category": "价格异动",
        "facts": "个股独立部分超出常规波动范围",
        "data": "股价当日 -3.2%（个股独立部分 -2.8%，2.6σ）",
        "base_rate": "该类申报的历史基准率本系统尚未收录",
        "counterpoint": "高盛下调目标价至 $180",
        "position": "占卫星仓 3.1%",
        "next_steps": ["同行业其他公司同期的读数", "下一次财报日期"],
    }
    try:
        AL.render_alert(alert, holding=None)
        print("  ✅ 含「目标价」的转述作为 counterpoint 时整条提醒正常渲染")
    except ValueError as e:
        print(f"  ❌ 误伤：render_alert 整条崩了 —— {e}")
        FAIL.append("误伤(转述) render_alert 含目标价转述")


def test_money_formats_banned():
    print("\n修复轮 1 · 金额守卫必须认得中文/USD 记法，不能只认 $ 前缀")
    from sw.notify import assert_no_money
    MONEY_SAMPLES = [
        "$1,234.56",
        "$ 1234",
        "1,234 美元",
        "1234美元",
        "50 万美元",
        "3.2 亿美元",
        "50万美金",
        "USD 1,234",
        "1234 USD",
    ]
    for s in MONEY_SAMPLES:
        try:
            assert_no_money(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"金额漏网 {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")


def test_percent_and_sigma_not_money():
    print("\n修复轮 1 · 百分比和 σ 值不能被金额守卫误伤（这是推送正文的主要内容）")
    from sw.notify import assert_no_money
    SAFE_SAMPLES = [
        "股价当日 -8.7%",
        "个股独立部分 4.1σ",
        "市盈率 24.5",
        "占卫星仓 4.9%",
        "波动率从 56.7% 变为 61.2%",
    ]
    for s in SAFE_SAMPLES:
        try:
            assert_no_money(s)
            print(f"  ✅ 放行：{s}")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤(金额) {s}")


def _make_portfolio():
    """两只持仓：AAA 市值 1000，BBB 市值 3000，权重分别是 25% / 75%。"""
    from sw.analysis.portfolio import Portfolio, Holding
    h1 = Holding(ticker="AAA", market_value=1000.0, cost_basis=900.0)
    h2 = Holding(ticker="BBB", market_value=3000.0, cost_basis=2500.0)
    return Portfolio(snapshot_date="2026-08-27", holdings=[h1, h2], cash=0.0)


def _attr(ticker, z, level, ret=-0.05, mkt=-0.01, sector=-0.01, idio=-0.03):
    """构造一条 Task 4 形状的归因结果。"""
    return {"ticker": ticker, "d": "2026-08-27", "ret": ret, "mkt_part": mkt,
            "sector_part": sector, "idio": idio, "sigma": 0.02, "z": z,
            "level": level, "beta_mkt": 1.0, "beta_sector": 1.0, "r2": 0.5,
            "n": 60, "skipped_days": 0}


def test_scan_l1_from_8k_item():
    print("\n修复轮 1 · scan()：8-K 命中 L1 item 触发 L1，position 反映组合权重")
    portfolio = _make_portfolio()
    causes = {"AAA": [{"source": "8-K", "summary": "8-K：CFO 离职",
                        "item": "5.02", "is_l1": True}]}
    out = AL.scan(store=None, portfolio=portfolio, attributions=[], causes_by_ticker=causes)
    levels = {a["ticker"]: a["level"] for a in out}
    check("AAA 因 8-K L1 item 升级为 L1", levels.get("AAA"), "L1")
    aaa = next((a for a in out if a["ticker"] == "AAA"), None)
    check("position 反映权重 25.0%", aaa["position"] if aaa else None, "占组合 25.0%")


def test_scan_l1_from_extreme_drop():
    print("\n修复轮 1 · scan()：残差 ≤ -4σ 的单日下跌触发 L1")
    portfolio = _make_portfolio()
    attributions = [_attr("BBB", z=-4.5, level="extreme")]
    causes = {"BBB": []}
    out = AL.scan(store=None, portfolio=portfolio, attributions=attributions,
                  causes_by_ticker=causes)
    levels = {a["ticker"]: a["level"] for a in out}
    check("BBB 因 z=-4.5 升级为 L1", levels.get("BBB"), "L1")


def test_scan_large_gain_not_l1():
    print("\n修复轮 1 · scan()：大涨（z ≥ +4）不升级为 L1（不对称设计，锁住方向）")
    portfolio = _make_portfolio()
    attributions = [_attr("BBB", z=4.5, level="extreme",
                           ret=0.09, mkt=0.01, sector=0.02, idio=0.06)]
    causes = {"BBB": []}
    out = AL.scan(store=None, portfolio=portfolio, attributions=attributions,
                  causes_by_ticker=causes)
    levels = {a["ticker"]: a["level"] for a in out}
    check("z=+4.5 不是 L1", levels.get("BBB") != "L1", True)


def test_scan_anomaly_without_l1_cause_is_l2():
    print("\n修复轮 1 · scan()：level=anomaly 且无 L1 原因 → L2")
    portfolio = _make_portfolio()
    attributions = [_attr("AAA", z=-2.5, level="anomaly")]
    causes = {"AAA": [{"source": "新闻", "summary": "无关新闻",
                        "item": None, "is_l1": False}]}
    out = AL.scan(store=None, portfolio=portfolio, attributions=attributions,
                  causes_by_ticker=causes)
    levels = {a["ticker"]: a["level"] for a in out}
    check("AAA 是 L2", levels.get("AAA"), "L2")


def test_scan_normal_not_included():
    print("\n修复轮 1 · scan()：level=normal 完全不出现在结果里")
    portfolio = _make_portfolio()
    attributions = [_attr("AAA", z=-0.5, level="normal")]
    causes = {"AAA": []}
    out = AL.scan(store=None, portfolio=portfolio, attributions=attributions,
                  causes_by_ticker=causes)
    tickers = {a["ticker"] for a in out}
    check("AAA 不在结果里", "AAA" in tickers, False)


def test_scan_z_none_no_crash():
    print("\n修复轮 1 · scan()：归因数据缺失（z=None）不崩，也不误判级别")
    portfolio = _make_portfolio()
    # 8-K 触发 L1，但当天归因因样本不足返回 None —— 不能因为拼 data 字符串崩掉
    attributions = [{"ticker": "AAA", "ret": None, "mkt_part": None, "sector_part": None,
                      "idio": None, "z": None, "level": None, "skipped_days": 5}]
    causes = {"AAA": [{"source": "8-K", "summary": "8-K：破产程序启动",
                        "item": "1.03", "is_l1": True}]}
    try:
        out = AL.scan(store=None, portfolio=portfolio, attributions=attributions,
                       causes_by_ticker=causes)
        check("z=None 时不崩且仍因 8-K 触发 L1", out[0]["level"] if out else None, "L1")
    except Exception as e:
        print(f"  ❌ 崩了：{e!r}")
        FAIL.append(f"scan() z=None 崩溃: {e!r}")


def test_attribution_exemption_is_per_sentence_not_whole_text():
    print("\n修复轮 2 · 归属豁免必须按句子边界判断，不能被别的句子的归属标记蹭到")
    # 用真实的 _counterpoint() 构造：同行读数（裸露的「目标价」，本句没有归属标记）
    # + 新闻转述（有「报道」标记），用「；」拼接——这正是 _counterpoint() 真实会
    # 产出的形状，也是 Task 7 接入 LLM 后 counterpoint 字段的典型来源。
    causes = [
        {"source": "同行读数", "summary": "同行零售商本季度也遭遇打击，目标价 200 美元",
         "item": None, "is_l1": False},
        {"source": "新闻", "summary": "有分析人士评论称行业整体承压",
         "item": None, "is_l1": False},
    ]
    cp = AL._counterpoint(causes)
    expected = ("同行零售商本季度也遭遇打击，目标价 200 美元；"
                "另有报道：有分析人士评论称行业整体承压")
    check("_counterpoint() 拼接形状符合预期", cp, expected)
    try:
        AL.assert_no_directives(cp)
        print(f"  ❌ 漏网：前半句裸露的「目标价」蹭到了后半句的「报道」标记 —— {cp!r}")
        FAIL.append("跨句蹭标记漏网")
    except ValueError:
        print("  ✅ 拦下：前半句「目标价」在本句内没有归属标记，不受后半句「报道」影响")


def test_transactional_price_phrases():
    print("\n修复轮 2 · 买入价/卖出价/建议减/该卖出 的同类误伤压力测试")
    ALLOWED = [
        "该笔交易的买入价区间为 45 至 48 美元",
        "审计函建议减少对单一供应商的依赖",
        "该卖出方为公司前董事",
    ]
    for s in ALLOWED:
        try:
            AL.assert_no_directives(s)
            print(f"  ✅ 放行：{s}")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤(交易转述) {s}")

    BANNED = [
        "你的买入价应该设在 180",
        "建议减仓至 3%",
    ]
    for s in BANNED:
        try:
            AL.assert_no_directives(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"漏网(交易) {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")


def test_extra_marker_is_scoped_not_global():
    print("\n修复轮 2 · 「交易」类专属标记只对买入价/卖出价生效，不能被建议买入借用")
    # 「建议买入」是高风险短语——如果「交易」这种专属标记被错误地放进通用表，
    # 这句会被误放行，等于系统自己在下单指令还蹭着「交易」两个字免检。
    s = "建议买入该交易标的 NVDA"
    try:
        AL.assert_no_directives(s)
        print(f"  ❌ 漏网：「交易」标记被「建议买入」借用了 —— {s!r}")
        FAIL.append(f"漏网(专属标记越界) {s}")
    except ValueError:
        print(f"  ✅ 拦下：{s!r}（「交易」不是「建议买入」的有效归属标记）")


def test_short_markers_do_not_leak_via_substring():
    print("\n修复轮 3 · 短标记不能被无关词的子串命中而放行真指令（复审实测漏网）")
    # 「据」「上调」「下调」「审计」「评级」「机构」「报道」这些单字/两字标记，
    # 会被"根据""数据""占据""以上调整""以下调整""复审计划""批评级别"
    # "有机构成""通报道歉"这类跟第三方转述毫无关系的词命中——一旦命中，
    # 真指令就会蹭着这个"标记"逃逸。
    MUST_BAN = [
        "根据模型评分，建议买入该标的NVDA",
        "根据历史数据，建议卖出该持仓",
        "占据主导地位后，建议减仓以锁定收益",
        "依据回测结果，建议加仓",
    ]
    for s in MUST_BAN:
        try:
            AL.assert_no_directives(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"漏网(短标记子串) {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")

    # 放行方向：确认硬化后的完整搭配仍然认得真正的转述场景，
    # 也确认"根据""数据"这两个词本身没有被错误地变成禁用词
    # （最后两条故意含"根据""数据"但不含任何指令性短语）。
    MUST_ALLOW = [
        "据报道该公司考虑出售非核心资产",
        "据悉该公司正在评估战略选择",
        "该分析师维持「建议持有」评级",     # 回归：仍认得"分析师"+"维持"
        "高盛下调目标价至 $180",           # 回归：仍认得"下调目标"
        "审计函建议减少对单一供应商的依赖",  # 回归：仍认得"审计函"/"审计整改"
        "根据审计报告，公司披露了三项整改事项",
        "数据显示行业库存处于五年高位",
    ]
    for s in MUST_ALLOW:
        try:
            AL.assert_no_directives(s)
            print(f"  ✅ 放行：{s}")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤(短标记硬化) {s}")


def test_noun_suffix_exemption_for_gai_mai():
    print("\n修复轮 3 · 该买/该卖出：名词性后缀豁免同时锁住两个方向")
    MUST_ALLOW = [
        "该卖出方为公司前董事",
        "该买家此前已持有该公司 5% 股份",
    ]
    for s in MUST_ALLOW:
        try:
            AL.assert_no_directives(s)
            print(f"  ✅ 放行：{s}")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤(名词后缀) {s}")

    MUST_BAN = [
        "该卖出这些质地一般的仓位了",
        "该买这只票",
    ]
    for s in MUST_BAN:
        try:
            AL.assert_no_directives(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"漏网(名词后缀) {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")


def test_comma_bypass_position_based_exemption():
    print("\n修复轮 4 · 逗号绕过：归属标记必须出现在豁免短语之前（位置判据，不是同句判据）")
    # 复审实测：逗号是中文财经写作里最常见的标点，_SENTENCE_SPLIT 之前不切逗号，
    # 一个逗号就能把最直白的指令和一个毫不相关的归属词连在一句里，整句放行。
    MUST_BAN = [
        "建议减仓，据悉近期宏观数据走弱",
        "建议加仓，据悉行业景气度回升",
        "建议卖出该持仓，另据分析师观点行业承压",
        "目标价 300 美元，建议买入，评级机构同步表态",
        "建议买入 NVDA，据报道行业景气度回升",
    ]
    for s in MUST_BAN:
        try:
            AL.assert_no_directives(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"漏网(逗号绕过) {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")

    # 放行方向：归属在前、内容在后，即使隔着逗号（甚至跨切分片段）也要放行——
    # 这是新闻转述的常见写法，不能因为治漏网就连这个也拦下。
    MUST_ALLOW = [
        "高盛表示，目标价 180 美元",
        "据分析师报告，建议买入评级维持不变",   # 归属在前一个片段，豁免短语在后一个片段
        "该分析师维持「建议持有」评级",         # 回归：无逗号的原始形态仍要放行
        "据报道，多家券商上调目标价",
    ]
    for s in MUST_ALLOW:
        try:
            AL.assert_no_directives(s)
            print(f"  ✅ 放行：{s}")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤(逗号绕过) {s}")


def test_position_field_is_validated_too():
    print("\n修复轮 5 · position 字段也要单独校验（复审列出的五个字段之一，此前没有测试覆盖）")
    # position 目前的实际取值都是 scan() 算出来的纯数字百分比，不会带指令性
    # 措辞；但 render_alert() 的裁定明确把 position 列进了要逐字段校验的
    # 字段里。这条测试补上此前没有覆盖到的检查点——不依赖 position 现在
    # 恰好安全，而是确认"哪怕以后 position 的取值来源变了"，这道检查也在。
    alert = {
        "ticker": "WXYZ", "level": "L2", "category": "价格异动",
        "facts": "个股独立部分超出常规波动范围",
        "data": "股价当日 -3.2%（个股独立部分 -2.8%，2.6σ）",
        "base_rate": "该类申报的历史基准率本系统尚未收录",
        "counterpoint": "本次未检索到明确的反面材料",
        "position": "该减仓了，占卫星仓 3.1%",   # ← 故意在 position 里塞裸指令
        "next_steps": ["同行业其他公司同期的读数"],
    }
    try:
        AL.render_alert(alert)
        print("  ❌ 漏网：render_alert() 没有拦下 position 字段里的裸指令")
        FAIL.append("漏网(position字段) render_alert")
    except ValueError:
        print("  ✅ 拦下：render_alert() 正确校验了 position 字段")


def test_comprehensive_ab_matrix():
    """
    验收证据：把前四轮累积的全部正负样本合到一起，一次性双向压测。
    A 组（必须放行，客观陈述/合法转述）和 B 组（必须拦下，指令性措辞）
    同时零失败，才能说这个模块真的同时守住了两个方向。
    """
    print("\n修复轮 4 · 收尾：完整 A/B 双向压测（验收证据）")

    A_ALLOW = [
        # --- 基线：客观陈述 ---
        "内部人集中卖出，排除 10b5-1 预设计划后仍有 3 笔",
        "该公司 CFO 于 08/27 离职，未披露继任安排",
        "这类信号历史上后续 6 个月出现财务重述的比例高于基准",
        "加入后你的组合年化波动率从 56.7% 变为 61.2%",
        "股价当日 -8.7%，其中个股独立部分 -9.1%（4.1σ）",
        "接下来看什么：① 继任公告 ② Q3 财报是否延期 ③ 审计师是否变动",
        "公司同日重申了 Q3 指引",
        # --- round1：转述第三方 ---
        "该分析师维持「建议持有」评级",
        "高盛下调目标价至 $180",
        "厂商建议零售价上调 8%",
        "该买家此前已持有该公司 5% 股份",
        "审计整改函中建议加强内部控制",
        "公司披露其外汇套期保值止损位",
        # --- round2：交易价格区间 / 该卖出方 ---
        "该笔交易的买入价区间为 45 至 48 美元",
        "审计函建议减少对单一供应商的依赖",
        "该卖出方为公司前董事",
        # --- round3：短标记硬化后仍要认得的场景 + 根据/数据本身不是禁用词 ---
        "据报道该公司考虑出售非核心资产",
        "据悉该公司正在评估战略选择",
        "根据审计报告，公司披露了三项整改事项",
        "数据显示行业库存处于五年高位",
        # --- round4：逗号 + 归属在前 ---
        "高盛表示，目标价 180 美元",
        "据分析师报告，建议买入评级维持不变",
        "据报道，多家券商上调目标价",
    ]

    B_BAN = [
        # --- 基线：直白指令 ---
        "建议买入 NVDA",
        "建议卖出该持仓",
        "你应该减少科技股敞口",
        "赶紧处理这个仓位",
        "该减仓了",
        "止损设在 $180",
        "目标价 $250",
        "务必在财报前调整",
        # --- round2：无归属的豁免短语 / 专属标记越界 ---
        "你的买入价应该设在 180",
        "建议减仓至 3%",
        "建议买入该交易标的 NVDA",
        # --- round3：短标记子串碰撞 / 该买卖名词后缀 ---
        "根据模型评分，建议买入该标的NVDA",
        "根据历史数据，建议卖出该持仓",
        "占据主导地位后，建议减仓以锁定收益",
        "依据回测结果，建议加仓",
        "该卖出这些质地一般的仓位了",
        "该买这只票",
        # --- round4：逗号绕过 ---
        "建议减仓，据悉近期宏观数据走弱",
        "建议加仓，据悉行业景气度回升",
        "建议卖出该持仓，另据分析师观点行业承压",
        "目标价 300 美元，建议买入，评级机构同步表态",
        "建议买入 NVDA，据报道行业景气度回升",
    ]

    # round2 的跨句蹭标记回归：用真实的 _counterpoint() 构造，不是手写字面量。
    leak_causes = [
        {"source": "同行读数", "summary": "同行零售商本季度也遭遇打击，目标价 200 美元",
         "item": None, "is_l1": False},
        {"source": "新闻", "summary": "有分析人士评论称行业整体承压",
         "item": None, "is_l1": False},
    ]
    B_BAN.append(AL._counterpoint(leak_causes))

    print(f"\n  A 组（必须放行，共 {len(A_ALLOW)} 条）")
    a_fail = 0
    for s in A_ALLOW:
        try:
            AL.assert_no_directives(s)
            print(f"    ✅ {s}")
        except ValueError as e:
            print(f"    ❌ 误伤 —— {s!r}：{e}")
            FAIL.append(f"验收误伤 {s}")
            a_fail += 1

    print(f"\n  B 组（必须拦下，共 {len(B_BAN)} 条）")
    b_fail = 0
    for s in B_BAN:
        try:
            AL.assert_no_directives(s)
            print(f"    ❌ 漏网 —— {s!r}")
            FAIL.append(f"验收漏网 {s}")
            b_fail += 1
        except ValueError:
            print(f"    ✅ {s}")

    print(f"\n  验收结果（字符串级）：A 组 {len(A_ALLOW)-a_fail}/{len(A_ALLOW)} 零误伤，"
          f"B 组 {len(B_BAN)-b_fail}/{len(B_BAN)} 零漏网")

    # 修复轮 5：跨字段攻击必须被拦下（render_alert / render_alert_push
    # 级别，不是字符串级别的 assert_no_directives）——这是这轮修的漏洞，
    # 收尾验收必须把它并进同一份表里，不能只在单独的测试函数里各自声称。
    print("\n  跨字段攻击（必须拦下，render_alert / render_alert_push 各一次）")
    cross_field_alert = {
        "ticker": "WXYZ", "level": "L2", "category": "价格异动",
        "facts": "个股独立部分超出常规波动范围",
        "data": "股价当日 -3.2%（个股独立部分 -2.8%，2.6σ）",
        "base_rate": "据分析师报告，历史上类似跌幅后续承压。",
        "counterpoint": "本次未检索到明确的反面材料",
        "position": "占卫星仓 3.1%",
        "next_steps": ["建议减仓，等待财报明朗"],
    }
    cross_fail = 0
    for fn, name in [(AL.render_alert, "render_alert(跨字段攻击)"),
                      (AL.render_alert_push, "render_alert_push(跨字段攻击)")]:
        try:
            fn(cross_field_alert)
            print(f"    ❌ 漏网 —— {name}")
            FAIL.append(f"验收漏网(跨字段) {name}")
            cross_fail += 1
        except ValueError:
            print(f"    ✅ {name}")

    print("\n  单字段归属在前（必须放行，render_alert / render_alert_push 各一次）")
    single_field_alert = {
        "ticker": "WXYZ", "level": "L2", "category": "价格异动",
        "facts": "个股独立部分超出常规波动范围",
        "data": "股价当日 -3.2%（个股独立部分 -2.8%，2.6σ）",
        "base_rate": "该类申报的历史基准率本系统尚未收录，以下判断请以原始申报文件为准。",
        "counterpoint": "据报道，多家券商上调目标价",
        "position": "占卫星仓 3.1%",
        "next_steps": ["同行业其他公司同期的读数", "下一次财报的日期与市场一致预期"],
    }
    single_fail = 0
    for fn, name in [(AL.render_alert, "render_alert(单字段归属)"),
                      (AL.render_alert_push, "render_alert_push(单字段归属)")]:
        try:
            fn(single_field_alert)
            print(f"    ✅ {name}")
        except ValueError as e:
            print(f"    ❌ 误伤 —— {name}：{e}")
            FAIL.append(f"验收误伤(单字段) {name}")
            single_fail += 1

    print(f"\n  最终验收结果：字符串级 A 组 {len(A_ALLOW)-a_fail}/{len(A_ALLOW)} 零误伤，"
          f"B 组 {len(B_BAN)-b_fail}/{len(B_BAN)} 零漏网；"
          f"跨字段攻击 {2-cross_fail}/2 全部拦下；"
          f"单字段归属 {2-single_fail}/2 全部放行")


def test_cross_field_attribution_leak():
    print("\n修复轮 5 · 跨字段攻击：一个字段的归属标记不能为另一个字段的指令背书")
    # 复审的攻击串——它的形状就是六段格式本身：base_rate 里有合法归属
    # （"据分析师报告"），next_steps 里是一句裸指令。round4 的位置判据是
    # "字段内"的语义，如果拼接后整体校验，base_rate 的归属会为
    # next_steps 里毫不相关的指令背书。
    alert = {
        "ticker": "WXYZ", "level": "L2", "category": "价格异动",
        "facts": "个股独立部分超出常规波动范围",
        "data": "股价当日 -3.2%（个股独立部分 -2.8%，2.6σ）",
        "base_rate": "据分析师报告，历史上类似跌幅后续承压。",
        "counterpoint": "本次未检索到明确的反面材料",
        "position": "占卫星仓 3.1%",
        "next_steps": ["建议减仓，等待财报明朗"],
    }
    for fn, name in [(AL.render_alert, "render_alert"),
                      (AL.render_alert_push, "render_alert_push")]:
        try:
            fn(alert)
            print(f"  ❌ 漏网：{name}() 没有拦下跨字段攻击")
            FAIL.append(f"漏网(跨字段) {name}")
        except ValueError:
            print(f"  ✅ 拦下：{name}() 正确识别跨字段攻击（不被 base_rate 的归属背书）")


def test_single_field_attribution_still_allowed():
    print("\n修复轮 5 · 单字段内归属在前仍要放行（逐字段校验不能矫枉过正）")
    # counterpoint 单独作为一个字段时，"据报道，多家券商上调目标价"
    # 本身归属在前、合法，其余字段没有任何裸露的豁免短语——整条应该
    # 正常渲染，不应该被"逐字段更严格"误伤成处处过敏。
    alert = {
        "ticker": "WXYZ", "level": "L2", "category": "价格异动",
        "facts": "个股独立部分超出常规波动范围",
        "data": "股价当日 -3.2%（个股独立部分 -2.8%，2.6σ）",
        "base_rate": "该类申报的历史基准率本系统尚未收录，以下判断请以原始申报文件为准。",
        "counterpoint": "据报道，多家券商上调目标价",
        "position": "占卫星仓 3.1%",
        "next_steps": ["同行业其他公司同期的读数", "下一次财报的日期与市场一致预期"],
    }
    for fn, name in [(AL.render_alert, "render_alert"),
                      (AL.render_alert_push, "render_alert_push")]:
        try:
            fn(alert)
            print(f"  ✅ 放行：{name}() 正常渲染")
        except ValueError as e:
            print(f"  ❌ 误伤：{name}() —— {e}")
            FAIL.append(f"误伤(单字段) {name}")


if __name__ == "__main__":
    test_banned_directives()
    test_allowed_factual_statements()
    test_six_section_format()
    test_push_version_has_no_money()
    test_level_from_8k_item()
    test_attribution_quotes_allowed()
    test_money_formats_banned()
    test_percent_and_sigma_not_money()
    test_scan_l1_from_8k_item()
    test_scan_l1_from_extreme_drop()
    test_scan_large_gain_not_l1()
    test_scan_anomaly_without_l1_cause_is_l2()
    test_scan_normal_not_included()
    test_scan_z_none_no_crash()
    test_attribution_exemption_is_per_sentence_not_whole_text()
    test_transactional_price_phrases()
    test_extra_marker_is_scoped_not_global()
    test_short_markers_do_not_leak_via_substring()
    test_noun_suffix_exemption_for_gai_mai()
    test_comma_bypass_position_based_exemption()
    test_cross_field_attribution_leak()
    test_single_field_attribution_still_allowed()
    test_position_field_is_validated_too()
    test_comprehensive_ab_matrix()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
