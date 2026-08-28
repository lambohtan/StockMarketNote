#!/usr/bin/env python3
"""LLM adapter 的测试。运行：python3 tests/test_llm.py

⚠️ 不真调 LLM。测的是配置守卫和命令行拼装 —— 这两处错了会静默烧钱。

最重要的一条：provider=claude_cli 时环境里若存在 ANTHROPIC_API_KEY，
Claude Code 会**静默改用 API 计费**而不是订阅。必须启动时就报错退出。
"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import llm as LM

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)

def check_raises(name, fn, exc):
    try:
        fn()
    except exc:
        print(f"  ✅ {name}")
        return
    except Exception as e:
        print(f"  ❌ {name}: 抛了 {type(e).__name__}，期望 {exc.__name__}")
    else:
        print(f"  ❌ {name}: 没抛异常")
    FAIL.append(name)


class Cfg:
    def __init__(self, provider="claude_cli", model="claude-opus-5"):
        self._d = {"llm.provider": provider, "llm.model": model,
                   "llm.max_tokens": 2000,
                   "llm.claude_bin": "/Users/lambo/.local/bin/claude"}
    def get(self, k, d=None):
        return self._d.get(k, d)


def test_api_key_conflict_is_fatal():
    print("\n配置守卫：claude_cli 模式下存在 API key 必须报错")
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        check_raises("claude_cli + API key → 报错",
                     lambda: LM.check_env(Cfg("claude_cli")), LM.LLMConfigError)
        # api 模式下有 key 是正常的
        LM.check_env(Cfg("api"))
        print("  ✅ api 模式 + API key → 放行")
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_api_mode_without_key_is_fatal():
    print("\n配置守卫：api 模式缺 key 必须报错")
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        check_raises("api 模式无 key → 报错",
                     lambda: LM.check_env(Cfg("api")), LM.LLMConfigError)
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_cli_cmd_shape():
    print("\n命令行拼装")
    cmd = LM.build_cli_cmd(Cfg("claude_cli"), "把材料压成一句话")
    check("走全路径（launchd 不继承 PATH）",
          cmd[0], "/Users/lambo/.local/bin/claude")
    check("有 -p", "-p" in cmd, True)
    check("绝不能有 --bare（它不读订阅登录）", "--bare" in cmd, False)
    check("带 append-system-prompt", "--append-system-prompt" in cmd, True)
    check("输出格式为 json", "json" in cmd, True)


def test_dry_run_returns_placeholder_and_calls_nothing():
    print("\ndry-run 不真调")
    out = LM.summarize(Cfg("claude_cli"), "材料若干", "压成一句话", dry_run=True)
    check("返回非空字符串", isinstance(out, str) and len(out) > 0, True)
    check("标注了 dry-run", "dry-run" in out, True)


def test_unknown_provider_is_fatal():
    print("\n配置守卫：未知 provider 必须报错（变异测试轮 4 发现的覆盖缺口）")
    check_raises("provider='deepseek' → 报错",
                 lambda: LM.check_env(Cfg("deepseek")), LM.LLMConfigError)


def test_output_passes_directive_guard():
    print("\nLLM 输出也要过指令性措辞守卫")
    from sw.alerts import assert_no_directives
    check_raises("含指令的输出被拦下",
                 lambda: LM._guard("建议卖出该持仓"), ValueError)
    LM._guard("内部人集中卖出，共 3 笔")
    print("  ✅ 客观陈述放行")


def test_cross_sentence_attack_on_guard():
    """
    Task 7 验收条件：Task 6 上限轮裁定的已知残留 —— 单字段内「先归属、
    远处夹带指令」的长句能绕过 assert_no_directives（归属标记远早于豁免
    短语、且中间跨过一个完整句号，或隔了好几个逗号分句）。

    这里用「模拟 LLM 摘要输出的形状」（两句话以内：先转述一个归属来源，
    再滑出到指令语气）红队 LM._guard()——它就是 summarize() 真正会走的
    守卫路径。加固前跑这条测试会全部漏网（见 task-7-report.md 的过程记录）；
    sw/alerts.py 改用"子句邻接"（软分隔符个数上限 + 硬分隔符不能跨）之后，
    下面全部样本必须被拦下。

    修复轮 1：审查者指出第一版"字符距离上限"是摆设（把常数从 20 改到 100
    两个测试套件都全绿），并且用"归属，无关填充，无关填充，真指令"这种
    多逗号分句的攻击绕了过去——后两条就是那次审查构造的真实绕过样本，
    补进来防止同样的绕过再次发生。
    """
    print("\n跨句攻击压测：模拟 LLM 摘要输出（两句话以内，先归属后指令）")
    ATTACKS = [
        # brief 里点名的规范案例：远距离归属为跨句指令背书
        "据悉，公司二季度营收增长强劲，好于市场预期，行业前景向好。建议买入 NVDA。",
        "分析师指出，公司基本面稳健，估值具备吸引力，长期逻辑清晰。建议加仓。",
        "据报道，该公司近期治理表现良好，行业情绪偏暖。建议买入。",
        "据悉，管理层近期表态积极，行业需求边际改善。建议卖出该持仓。",
        "有报道称公司基本面良好。建议买入该标的。",
        # 铺垫极短的变体：纯字符距离判据挡不住，必须靠句末标点判据补
        "据悉大涨。建议买入。",
        "据悉行业景气度回升。建议加仓。",
        # 修复轮 1 · 审查者构造的真实绕过：没有句号，纯靠多个逗号分句拉开
        # 距离——验证的是"软分隔符个数上限"这条判据，不是句号判据
        "据悉，市场情绪偏紧张，建议加仓",
        "据悉，公司近期出现明显异动，情绪偏紧张，建议加仓",
    ]
    for s in ATTACKS:
        try:
            LM._guard(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"跨句攻击漏网 {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")


class FakeStore:
    """测试用假 store：只记录 log_health 的调用，不碰真数据库。"""
    def __init__(self):
        self.calls = []

    def log_health(self, source, ok, latency_ms, detail=""):
        self.calls.append((source, ok, latency_ms, detail))


def test_guard_violation_is_logged():
    """
    Task 7 修复轮 1（Important 提级为必修）：守卫触发的 ValueError 之前被
    summarize() 外层的 except Exception 静默吞掉、退化成空字符串，没有任何
    日志。如果 LLM 持续产出违规摘要，运维侧完全看不到信号。

    _guard_or_log() 是这个问题的修法：触发时记一条 source_health
    （source="llm.guard"，ok=False，detail 里带被拦短语和摘要前 100 字），
    没有 store 就退化为打印到 stderr——两条路径都不能崩、也不能真的沉默。
    """
    print("\n守卫触发必须可见：记 source_health，而不是静默吞掉")
    store = FakeStore()
    out = LM._guard_or_log("建议卖出该持仓", store)
    check("触发时返回空字符串（不中断任务）", out, "")
    check("恰好记了一条 source_health", len(store.calls), 1)
    if store.calls:
        source, ok, latency_ms, detail = store.calls[0]
        check("source 是 llm.guard", source, "llm.guard")
        check("ok=False", ok, False)
        check("detail 里带着被拦短语", "建议卖出" in detail, True)

    print("\n没有 store 时退化为打印到 stderr（launchd 会写进 StandardErrorPath）")
    out2 = LM._guard_or_log("建议买入 NVDA", None)
    check("退化路径也返回空字符串", out2, "")

    print("\n客观陈述不触发日志")
    store2 = FakeStore()
    out3 = LM._guard_or_log("内部人集中卖出，共 3 笔", store2)
    check("客观陈述原样返回", out3, "内部人集中卖出，共 3 笔")
    check("没有记 source_health", len(store2.calls), 0)


if __name__ == "__main__":
    test_api_key_conflict_is_fatal()
    test_api_mode_without_key_is_fatal()
    test_unknown_provider_is_fatal()
    test_cli_cmd_shape()
    test_dry_run_returns_placeholder_and_calls_nothing()
    test_output_passes_directive_guard()
    test_cross_sentence_attack_on_guard()
    test_guard_violation_is_logged()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
