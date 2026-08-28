"""
LLM adapter —— 全流程唯一用 LLM 的地方。

它只做两件事：① 读材料写摘要 ② 把结构化结果翻译成人话。
**不产生任何数字。** 报告里每个数字都来自确定性 Python，换模型不影响任何结论。

两条路：
  claude_cli —— 调 `claude -p`，走 Claude Pro 订阅，不额外花钱
  api        —— 调 Anthropic API，按量付费，费用可预测

⚠️ claude_cli 的四个坑（每一个都会静默出错）：
  1. 不能加 --bare —— bare 模式不读订阅登录，会要求 API key
  2. 环境里不能有 ANTHROPIC_API_KEY —— 一旦存在，Claude Code 静默改用 API 计费
  3. launchd 不继承 shell PATH —— 必须写 claude 的全路径
  4. cwd 决定加载什么上下文 —— 必须在专用空目录里跑
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from .alerts import assert_no_directives

ROOT = Path(__file__).resolve().parent.parent
LLM_WORKDIR = ROOT / "packaging" / "llm-workdir"
DEFAULT_CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")


class LLMConfigError(Exception):
    pass


def check_env(cfg):
    """启动时的配置守卫。宁可退出，也不静默烧钱或静默失败。"""
    provider = cfg.get("llm.provider", "claude_cli")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "claude_cli" and has_key:
        raise LLMConfigError(
            "llm.provider=claude_cli，但环境里存在 ANTHROPIC_API_KEY。\n"
            "Claude Code 检测到该变量会**静默改用 API 按量计费**而不是你的 Pro 订阅。\n"
            "解决：从这个任务的环境里移除该变量（检查 plist 的 EnvironmentVariables "
            "和 ~/.zshrc），或把 llm.provider 改成 api。")
    if provider == "api" and not has_key:
        raise LLMConfigError(
            "llm.provider=api，但环境里没有 ANTHROPIC_API_KEY。\n"
            "把 key 写进 launchd plist 的 EnvironmentVariables —— "
            "不要写进 ~/.zshrc，那会让你交互式使用 Claude Code 也变成按量付费。")
    if provider not in ("claude_cli", "api"):
        raise LLMConfigError(f"未知的 llm.provider: {provider!r}")


def _guard(text):
    """LLM 输出也要过指令性措辞守卫 —— 模型不知道我们的产品约束。"""
    assert_no_directives(text)
    return text


def _guard_or_log(text, store):
    """
    过守卫；触发时记一条 source_health，而不是和"调用失败"混在一起静默吞掉。

    Task 7 修复轮 1（Important 提级为必修）：守卫是兜底，不是安全边界——对抗式
    绕过不可能穷尽。真正的保障不是把守卫堆到无懈可击，而是让"LLM 反复产出
    违规摘要"这件事可见。如果没有 store（比如调用方还没接入，或测试环境），
    退化为打印到 stderr——launchd 会把 stderr 写进 StandardErrorPath，
    不会真的消失。
    """
    try:
        return _guard(text)
    except ValueError as e:
        detail = f"{e}｜摘要前100字：{text[:100]!r}"
        if store is not None:
            store.log_health("llm.guard", False, 0, detail)
        else:
            print(f"[llm.guard] 守卫拦下 LLM 输出：{detail}", file=sys.stderr)
        return ""


def build_cli_cmd(cfg, instruction):
    """拼 claude -p 的命令行。单独抽出来是为了能测。"""
    claude_bin = cfg.get("llm.claude_bin") or DEFAULT_CLAUDE_BIN
    return [
        claude_bin, "-p",
        "--append-system-prompt", instruction,
        "--output-format", "json",
        # ⚠️ 不要加 --bare：它不读订阅登录
        # ⚠️ 不给任何工具权限：纯文本进，纯文本出
        "--permission-mode", "dontAsk",
    ]


SYSTEM = (
    "你在为一个个人股票研究系统写摘要。规则：\n"
    "1. 只根据给你的材料写，材料里没有的一律不写，不要补充背景知识。\n"
    "2. 不要给任何建议、不要用祈使句、不要出现『建议』『应该』『赶紧』『目标价』。\n"
    "   你的任务是描述发生了什么，不是指挥用户做什么。\n"
    "3. 不要编造数字。材料里的数字原样引用，没有的就不写。\n"
    "4. 材料不足以得出结论时，直接说『材料不足以判断原因』。\n"
    "5. 用中文，简体，两句话以内。"
)


def summarize(cfg, material, instruction=None, dry_run=False, timeout=120, store=None):
    """
    把材料压成一两句人话。失败返回空字符串，绝不中断整个任务。

    ⚠️ 例外：check_env() 触发的 LLMConfigError **不会**被这里吞掉，会直接
    冒泡给调用方。这是有意设计，跟"LLM 调用失败返回空字符串"字面上有张力，
    但两者是不同性质的问题：
      - 配置错误（provider 冲突、缺 key、未知 provider）是开发/运维配置
        错误，必须尽早暴露、阻止任务继续——放过去不会"优雅降级"，只会变成
        "报告里莫名其妙少一段"，排查起来更难，还可能在没人注意的情况下
        持续按 API 计费。
      - 运行时失败（网络、超时、CLI 非零退出、JSON 解析失败等）才是"绝不
        中断整个任务"这条规则要覆盖的场景——这类失败是环境噪音，不代表
        配置本身错了，值得静默降级、明天再试。
    调用方（Task 9/10/11 的 run_daily.py 等）需要在最外层单独捕获
    LLMConfigError 并让任务失败得响亮，不能和其它异常一起吞掉。

    store：可选，传入后守卫触发（LLM 输出含指令性措辞）时会记一条
    source_health（source="llm.guard"），没传就退化为打印到 stderr。
    这跟"调用失败"是两回事——守卫触发说明 LLM 拿到材料并且回话了，只是
    回话内容违规，运维需要看到这个信号，而不是和网络超时之类的调用失败
    混在一起，一律变成空字符串、无迹可寻。
    """
    instruction = f"{SYSTEM}\n\n{instruction or ''}".strip()
    if dry_run:
        return f"[dry-run] 将把 {len(material)} 字材料交给 LLM 摘要"

    check_env(cfg)  # LLMConfigError 不在这里捕获，直接冒泡（见上方 docstring）
    provider = cfg.get("llm.provider", "claude_cli")

    if provider == "claude_cli":
        LLM_WORKDIR.mkdir(parents=True, exist_ok=True)
        cmd = build_cli_cmd(cfg, instruction)
        try:
            p = subprocess.run(cmd, input=material, capture_output=True,
                               text=True, timeout=timeout, cwd=str(LLM_WORKDIR))
            if p.returncode != 0:
                return ""
            data = json.loads(p.stdout)
            result = (data.get("result") or "").strip()
        except Exception:
            return ""
        return _guard_or_log(result, store)

    # provider == "api"
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg.get("llm.model", "claude-opus-5"),
            max_tokens=int(cfg.get("llm.max_tokens", 2000)),
            system=instruction,
            messages=[{"role": "user", "content": material}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        result = text.strip()
    except Exception:
        return ""
    return _guard_or_log(result, store)
