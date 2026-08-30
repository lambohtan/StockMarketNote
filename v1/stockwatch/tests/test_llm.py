#!/usr/bin/env python3
"""LLM adapter 的测试。运行：python3 tests/test_llm.py

⚠️ 不真调 LLM。测的是配置守卫和命令行拼装 —— 这两处错了会静默烧钱。

最重要的一条：provider=claude_cli 时环境里若存在 ANTHROPIC_API_KEY，
Claude Code 会**静默改用 API 计费**而不是订阅。必须启动时就报错退出。
"""
import sys, os, io, contextlib
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


class BrokenStore:
    """测试用坏 store：log_health 本身会抛异常，模拟 sqlite3 撞锁/磁盘满/
    连接已关闭。这些都是真实会发生的 store 故障，不是凭空假设。"""
    def log_health(self, *a, **kw):
        raise RuntimeError("模拟 store 已关闭 / 磁盘故障")


def test_guard_or_log_survives_broken_store():
    """
    Task 7 修复轮 2 · Important 1：store.log_health(...) 之前没有任何
    try/except，而 summarize() 里两处调用 _guard_or_log 的地方都在各自的
    try/except Exception **之外**——store 写入失败（sqlite3 撞锁、磁盘满、
    连接已关闭）会直接冒泡，违反 docstring 明确承诺的"绝不中断整个任务"。

    最坏的时机恰好是"LLM 持续产出违规摘要、正需要记日志"的时候：这时候
    如果 store 写入撞锁，"记录违规"这个动作反而会让当天的日报流水线整个
    崩掉——比修复前"静默吞掉、只是运维看不到信号"更糟。

    修法：log_health 本身也当成一次可能失败的 I/O，失败就退化到 stderr，
    跟没有 store 时走同一条路。这条测试确认这个退化真的发生、真的不崩。
    """
    print("\n观测性代码本身不能成为新的失败点：store.log_health 抛异常也不能崩")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        out = LM._guard_or_log("建议卖出该持仓", BrokenStore())
    check("store 故障时仍返回空字符串（不崩）", out, "")
    stderr_text = buf.getvalue()
    check("store 故障退化到 stderr 里有 llm.guard 标记", "llm.guard" in stderr_text, True)
    check("stderr 里带着被拦短语", "建议卖出" in stderr_text, True)


def test_summarize_survives_broken_store_end_to_end():
    """
    同一个问题在 summarize() 层面的端到端复现——只测 _guard_or_log() 内部
    测不出"summarize() 本身还会不会被这层异常波及"，因为 bug 恰恰在于
    summarize() 里调用 _guard_or_log() 的两处（claude_cli 分支、api 分支）
    都在各自的 try/except Exception 之外。

    这里 monkeypatch 假的 subprocess.run（不真调 LLM，符合任务约束），逼
    summarize() 走到 claude_cli 分支的守卫触发路径，配上 BrokenStore，
    断言 summarize() 整体依然乖乖返回空字符串，而不是把异常甩给调用方。
    """
    print("\nsummarize() 端到端：store 故障不能让整条调用链崩溃")

    class FakeCompleted:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, cwd=None):
        import json as _json
        return FakeCompleted(0, _json.dumps({"result": "建议卖出该持仓"}))

    old_run = LM.subprocess.run
    LM.subprocess.run = fake_run
    try:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = LM.summarize(Cfg("claude_cli"), "材料若干", "压成一句话",
                                store=BrokenStore())
        check("summarize() 在 store 故障时仍返回空字符串（不崩）", out, "")
        check("summarize() 端到端也退化到了 stderr", "llm.guard" in buf.getvalue(), True)
    finally:
        LM.subprocess.run = old_run


def test_stderr_fallback_has_content():
    """
    Task 7 修复轮 2 · Important 2：复审的变异把 `else: print(..., file=
    sys.stderr)` 改成 `else: pass`，测试全绿——因为此前的测试只断言了
    返回值是空字符串，从没真正捕获过 stderr 里到底有没有内容。这里用
    redirect_stderr 真的抓一次输出，确认没有 store 时的退化路径真的打印
    了东西（而且打印到的确实是 stderr，不是 stdout），不能是空动作。
    """
    print("\n无 store 时 stderr 兜底路径必须真的有输出，不能是空动作")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        out = LM._guard_or_log("建议买入 NVDA", None)
    check("无 store 时仍返回空字符串", out, "")
    stderr_text = buf.getvalue()
    check("stderr 里有 llm.guard 标记", "llm.guard" in stderr_text, True)
    check("stderr 里带着被拦短语", "建议买入" in stderr_text, True)


if __name__ == "__main__":
    test_api_key_conflict_is_fatal()
    test_api_mode_without_key_is_fatal()
    test_unknown_provider_is_fatal()
    test_cli_cmd_shape()
    test_dry_run_returns_placeholder_and_calls_nothing()
    test_output_passes_directive_guard()
    test_cross_sentence_attack_on_guard()
    test_guard_violation_is_logged()
    test_guard_or_log_survives_broken_store()
    test_summarize_survives_broken_store_end_to_end()
    test_stderr_fallback_has_content()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
