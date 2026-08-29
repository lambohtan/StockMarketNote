"""
日报渲染。两套输出：

  render_markdown  完整版，存 reports 表和 reports/ 目录，可含金额
  render_push      推送版，经 ntfy.sh 公共服务器，**绝不含金额**

⭐ 最重要的设计：残差在正常范围的持仓**根本不出现在报告里**。
日报一半的价值来自它不说什么 —— 每天列出 26 只票的涨跌等于没有信息。
"""
import re

from .alerts import assert_no_directives, render_alert
from .notify import assert_no_money, MONEY_RE

# 截断收尾用：字符串末尾如果留下一个没有配对「]」的裸「[」，说明是
# reason[:80] 字符截断把占位符 [金额见面板] 切碎了（见 render_push 的
# Task 9 收尾说明），删掉这个残缺片段，不让手机锁屏上出现无意义的尾巴。
_DANGLING_BRACKET_RE = re.compile(r"\[[^\]]*$")


def _drop_dangling_bracket(s):
    return _DANGLING_BRACKET_RE.sub("", s)


def _movers(ctx):
    """只挑出异动的，按 |z| 降序。"""
    xs = [a for a in ctx["attributions"] if a.get("level") in ("anomaly", "extreme")]
    return sorted(xs, key=lambda a: -abs(a.get("z") or 0))


def _fmt_line(a, causes):
    reason = "未找到明确原因"
    if causes:
        reason = causes[0]["summary"]
    return (
        f"**{a['ticker']}**　{a['ret']*100:+.1f}%\n"
        f"　　其中大盘 {a['mkt_part']*100:+.1f}%，"
        f"行业 {a['sector_part']*100:+.1f}%，"
        f"个股独立 {a['idio']*100:+.1f}%（{a['z']:+.1f}σ）\n"
        f"　　原因：{reason}"
    )


def render_markdown(ctx):
    movers = _movers(ctx)
    total = len(ctx["attributions"])
    quiet = total - len(movers)
    L = [f"# 每日简报 · {ctx['d']}", ""]

    if movers:
        L.append(f"## 持仓异动（{len(movers)} 项，其余 {quiet} 只无异常）")
        L.append("")
        for a in movers:
            L.append(_fmt_line(a, ctx["causes_by_ticker"].get(a["ticker"])))
            L.append("")
    elif total == 0:
        # 修复轮 1 Minor：没有归因数据和「持仓都正常」是两件事，混成一句
        # 会让人误以为系统跑过归因、结果一切正常——实际可能是持仓快照
        # 缺失或行情数据不足，这种情况要显式指向排查方向。
        L += ["## 持仓异动", "",
              "今天没有可归因的持仓数据 —— 可能是持仓快照缺失或行情数据"
              "不足，请检查 `source_health`。", ""]
    else:
        L += [f"## 持仓异动", "",
              f"今天 {total} 只持仓**全部无异常** —— "
              f"个股独立部分都在 2σ 以内，涨跌可由大盘和行业解释。", ""]

    if ctx.get("alerts"):
        L += ["## 需要注意的信号", ""]
        for al in ctx["alerts"]:
            L.append(render_alert(al, holding=None))
            L.append("")

    if ctx.get("watchlist_events"):
        L += ["## 观察池", ""]
        for e in ctx["watchlist_events"]:
            L.append(f"- {e}")
        L.append("")

    m = ctx.get("market") or {}
    if m:
        bits = []
        if m.get("spy_vs_200d") is not None:
            bits.append(f"SPY 位于 200 日线{'上方' if m['spy_vs_200d'] >= 0 else '下方'}"
                        f" {abs(m['spy_vs_200d'])*100:.1f}%")
        if m.get("vix") is not None:
            bits.append(f"VIX {m['vix']:.1f}")
        if bits:
            L += ["## 市场环境", "", "　".join(bits), ""]

    L += ["---", "",
          "> 归因用过去 60 个交易日回归：个股收益 = α + β_市场×SPY + β_行业×行业ETF。",
          "> 残差在 2σ 以内的持仓不在本报告中出现。**未经回测验证，不预测涨跌。**",
          "> 本工具为个人研究用途，所有产出不构成投资建议。"]
    md = "\n".join(L)
    assert_no_directives(md)
    return md


def render_push(ctx):
    """
    推送版：短、无金额、能在手机锁屏上读完要点。

    ⚠️ 本函数不渲染 ctx['alerts'] —— L1 提醒由 run_daily.py::emit() 用
    alerts.render_alert_push() 单独入队为独立通知（priority=urgent），
    见设计文档 §7「每条 L1 单独一条通知」。日报正文只放异动摘要，不重复
    L1 的内容。

    ⚠️ 修复轮 3：金额一律先替换成占位符再进正文，而不是抛异常 ——
    推送是本系统的产品本体，为一个金额丢掉当天整份日报，代价远大于
    显示占位符。ctx 里凡是外部/LLM 生成、长度不受控的文本字段
    （causes 的 reason、watchlist_events 的每一条）都在拼进正文前先过
    MONEY_RE.sub 替换（有截断的话，替换必须在截断之前，见修复轮 2）。
    assert_no_money(body) 保留作最终防线，拦替换没覆盖到的写法，
    两者不冲突：替换是第一道，兜底是最后一道。
    """
    movers = _movers(ctx)
    total = len(ctx["attributions"])
    title = f"StockWatch {ctx['d']}　异动 {len(movers)}/{total}"

    L = []
    if movers:
        for a in movers[:5]:
            causes = ctx["causes_by_ticker"].get(a["ticker"]) or []
            reason = causes[0]["summary"] if causes else "未找到明确原因"
            # 修复轮 2 Minor：金额替换必须在截断之前做。causes 的 summary
            # 长度不受控（新闻/8-K/LLM 摘要），如果先截到 80 字符再校验，
            # 金额短语恰好跨在第 80 字符边界上时会被切碎——比如
            # 「…累计成本约 50万」|「美元…」，截断后只剩「50万」，没有
            # 币种单位就不匹配 MONEY_RE，裸数量词就原样进了推送正文。
            # 在完整字符串上先替换、再截断，就不存在「切碎金额短语绕过
            # 校验」这个窗口。assert_no_money(body) 仍然保留作为最终防线，
            # 不能因为加了这行替换就把它去掉。
            reason = MONEY_RE.sub("[金额见面板]", reason)
            # Task 9 收尾：占位符本身长度不受控（"[金额见面板]" 6 字），如果
            # 恰好跨在第 80 字符截断边界上，会被切成 "[金额" 这种没有配对
            # 右括号的残尾，锁屏上显示出来毫无意义。这不是安全问题——
            # assert_no_money 不会因为多出一个 "[" 就误判，真实金额也不会
            # 因为这一步而漏判——纯粹是显示质量问题，截断后统一把这类
            # 残缺方括号片段去掉。
            reason = _drop_dangling_bracket(reason[:80])
            L.append(f"{a['ticker']} {a['ret']*100:+.1f}%"
                     f"（个股独立 {a['idio']*100:+.1f}%，{a['z']:+.1f}σ）\n"
                     f"  {reason}")
        if len(movers) > 5:
            L.append(f"…另有 {len(movers)-5} 项，详见面板")
    elif total == 0:
        # 同 render_markdown 的 Minor 修复：没有数据不能说成「全部无异常」。
        L.append("今天没有可归因的持仓数据，详见面板。")
    else:
        L.append(f"{total} 只持仓全部无异常，涨跌可由大盘和行业解释。")

    if ctx.get("watchlist_events"):
        L.append("")
        L.append("观察池")
        for e in ctx["watchlist_events"][:3]:
            # 同 reason 字段的处理：先替换金额再拼正文，不对 watchlist
            # 事件里出现的金额抛异常——见上方 docstring 的策略说明。
            L.append(f"  {MONEY_RE.sub('[金额见面板]', e)}")

    body = "\n".join(L)
    assert_no_money(body)
    assert_no_money(title)
    assert_no_directives(body)
    return title, body
