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
import re
from pathlib import Path

from .policy import assert_no_directives

ROOT = Path(__file__).resolve().parent.parent
LLM_WORKDIR = ROOT / "packaging" / "llm-workdir"
DEFAULT_CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

# 深读长文（stage2 的四段叙述）的输出形状。摘要那张 schema 装不下它：
# `summary` 这个 key 与 SYSTEM 的「两句话以内」是一整套约定，长文走自己的 key。
LONG_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

_NUMBER_TOKEN = re.compile(r"(?<!\d)\d[\d,]*(?:\.\d+)?")

# 「带财务量级单位的数字」——深读长文的数字口径只管这一类（见
# _validate_numeric_magnitude 的 docstring）。
_MAGNITUDE_UNIT = re.compile(
    r"\s*(%|％|个百分点|百分点|个点|pt(?![a-zA-Z])|倍|万亿|亿|万|美元|美金|元)")


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


def _guard_or_log(text, store, guard_error=None):
    """
    过守卫；触发时记一条 source_health，而不是和"调用失败"混在一起静默吞掉。

    Task 7 修复轮 1（Important 提级为必修）：守卫是兜底，不是安全边界——对抗式
    绕过不可能穷尽。真正的保障不是把守卫堆到无懈可击，而是让"LLM 反复产出
    违规摘要"这件事可见。如果没有 store（比如调用方还没接入，或测试环境），
    退化为打印到 stderr——launchd 会把 stderr 写进 StandardErrorPath，
    不会真的消失。

    Task 7 修复轮 2：观测性代码绝不能自己成为新的失败点。上一轮加的
    store.log_health(...) 调用没包 try/except——sqlite3 撞锁、磁盘满、
    连接已关闭都会让它抛异常，而这里正好处在两处 summarize() 的
    try/except Exception **之外**，异常会直接冒泡把整条调用链崩掉。
    最坏的时机恰好是"LLM 持续产出违规摘要、最需要记录"的时候：如果这时候
    store 写入撞锁，"记录违规"这个动作反而会让当天的日报流水线整个崩掉——
    比修复前"静默吞掉、只是运维看不到信号"更糟。所以 log_health 本身也要
    当成一次可能失败的 I/O 来对待，失败就退化到 stderr，跟没有 store 时
    走同一条路，不能让"记日志"这件事本身有能力弄崩调用方。
    """
    try:
        if guard_error:
            raise ValueError(guard_error)
        return _guard(text)
    except ValueError as e:
        # detail 只记录策略类别/短语，不记录 prompt、原始材料、secret 或
        # 完整模型输出；日志本身不能成为第二条数据泄漏通道。
        detail = str(e)
        if store is not None:
            try:
                store.log_health("llm.guard", False, 0, detail)
                return ""
            except Exception:
                pass  # store 本身出故障，退化到 stderr，不能让记日志这件事把调用链崩掉
        print(f"[llm.guard] 守卫拦下 LLM 输出：{detail}", file=sys.stderr)
        return ""


def _runtime_fail(store, reason):
    """运行时失败只记录类型，避免把材料/密钥写进 source_health。"""
    detail = f"运行时失败：{reason}"
    if store is not None:
        try:
            store.log_health("llm.runtime", False, 0, detail)
            return
        except Exception:
            pass
    print(f"[llm.runtime] {detail}", file=sys.stderr)


def _extract_summary(value, allow_plain=True):
    """把 CLI/API 的结构化结果归一为 summary 字符串。

    CLI 旧版本仍可能把 ``result`` 作为纯文本返回，因此保留兼容开关；
    API 边界必须是同一份 JSON schema，调用时关闭纯文本回退。
    """
    if isinstance(value, dict):
        summary = value.get("summary")
        if isinstance(summary, str):
            return summary.strip()
        raise ValueError("结构化输出缺少 summary")
    if isinstance(value, str):
        # 兼容旧版 claude CLI 的 result=纯文本；新 schema 输出通常是 JSON
        # 字符串，优先解析成统一对象。
        raw = value.strip()
        if not raw:
            raise ValueError("结构化输出为空")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            if allow_plain:
                return raw
            raise ValueError("结构化输出不是 JSON")
        return _extract_summary(parsed, allow_plain=allow_plain)
    raise ValueError("结构化输出类型不正确")


def _parse_cli_output(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("CLI 外层输出不是对象")
    candidate = data.get("structured_output")
    if candidate is None:
        candidate = data.get("structuredOutput")
    if candidate is None:
        candidate = data.get("result")
    return _extract_summary(candidate)


def _number_tokens(text):
    return {m.group(0).replace(",", "") for m in _NUMBER_TOKEN.finditer(text or "")}


def _validate_numeric_provenance(summary, material):
    """每个阿拉伯数字 token 都必须能在输入材料中找到。"""
    source_numbers = _number_tokens(material)
    for token in _number_tokens(summary):
        if token not in source_numbers:
            raise ValueError("摘要数字未通过来源校验")


def _render_variants(value):
    """一个数值在中文叙述里可能被写成的几种字面形式（0/1/2/3 位小数，去尾零）。"""
    out = set()
    for nd in (0, 1, 2, 3):
        s = f"{value:.{nd}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        out.add(s.lstrip("-") or "0")
    return out


def _traceable_numbers(material):
    """材料里出现过的数值，连同它的百分比换算与常见小数位写法。

    材料是 facts 的 JSON dump（`0.94`），模型把它写成中文叙述时会写
    「94%」——这是同一个数字的另一种合法写法，不是新造的数字。所以来源集合
    必须把 ×100 / ÷100 与四种小数位写法一起展开，否则合法引用会被判成幻觉。
    """
    vals = set()
    for m in _NUMBER_TOKEN.finditer(material or ""):
        raw = m.group(0).replace(",", "")
        vals.add(raw)
        try:
            v = float(raw)
        except ValueError:
            continue
        for candidate in (v, v * 100.0, v / 100.0):
            vals.update(_render_variants(candidate))
    return vals


def _validate_numeric_magnitude(text, material):
    """深读长文的数字口径：只拦「材料里找不到的财务量级数字」。

    为什么不沿用 `_validate_numeric_provenance`（每个数字 token 都必须
    在材料里逐字出现）：那条规则是为「两句话以内的摘要」设计的，用在四段
    中文叙述上会把大量合法表述判成幻觉 —— 材料里是 `0.94`，模型写
    「同比增长 94%」；「命中 6/8」「三条待观察」「① ② ③」这类计数和序号
    在材料里根本不会逐字出现。而它的失败模式是**丢弃整段**：报告里只剩
    一句「LLM 深读失败」，看上去像是诚实的降级，实际上是校验器把好输出
    扔了 —— 这正是 P4 最终审查判为 Critical 的那类静默失败。

    这里改成：只有**带财务量级单位**（%、个百分点、倍、万/亿/万亿、美元）
    且在材料里找不到对应数值的数字才拒绝。理由是这类数字才是读者会当成
    财务事实读的东西；裸的小整数（条数、序号、名次、比分）不是数值结论，
    而且报告里所有**参与计分和展示的数字**（X/Y、六条判定明细、PE）全部
    由 render.py 从 py 的结果直接渲染，不经过这段叙述 —— 硬约束 2
    「LLM 不产任何数字」在计分链路上由 C2 的白名单保证，不靠这条校验。
    """
    allowed = _traceable_numbers(material)
    for m in _NUMBER_TOKEN.finditer(text or ""):
        if not _MAGNITUDE_UNIT.match(text[m.end():m.end() + 6]):
            continue
        raw = m.group(0).replace(",", "")
        if raw in allowed:
            continue
        try:
            if _render_variants(float(raw)) & allowed:
                continue
        except ValueError:
            pass
        raise ValueError("深读叙述里出现了材料中没有的量级数字")


def _cli_cmd(cfg, instruction, schema):
    """拼 claude -p 的命令行。schema 由调用方给 —— 输出形状不是一套。"""
    claude_bin = cfg.get("llm.claude_bin") or DEFAULT_CLAUDE_BIN
    return [
        claude_bin, "-p",
        "--append-system-prompt", instruction,
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False,
                                    separators=(",", ":")),
        # ⚠️ 不要加 --bare：它不读订阅登录
        # ⚠️ 不给任何工具权限：纯文本进，纯文本出
        "--permission-mode", "dontAsk",
    ]


def build_cli_cmd(cfg, instruction):
    """拼摘要路径（summarize）的命令行。单独抽出来是为了能测。

    schema 固定是 OUTPUT_SCHEMA —— 这条路是日报（P3）在走的，形状不动。
    """
    return _cli_cmd(cfg, instruction, OUTPUT_SCHEMA)


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
                _runtime_fail(store, "cli_nonzero")
                return ""
            result = _parse_cli_output(p.stdout)
        except subprocess.TimeoutExpired:
            _runtime_fail(store, "cli_timeout")
            return ""
        except (json.JSONDecodeError, ValueError):
            _runtime_fail(store, "cli_parse")
            return ""
        except Exception:
            _runtime_fail(store, "cli_exception")
            return ""
        try:
            _validate_numeric_provenance(result, material)
        except ValueError as e:
            return _guard_or_log(result, store, guard_error=str(e))
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
        _runtime_fail(store, "api_exception")
        return ""
    try:
        # API content must be a JSON object (or a JSON-encoded object), matching
        # the CLI schema; unlike the CLI compatibility path, plain text is not
        # accepted at this boundary.
        result = _extract_summary(result, allow_plain=False)
        _validate_numeric_provenance(result, material)
    except ValueError as e:
        if "数字" in str(e):
            return _guard_or_log(result, store, guard_error=str(e))
        _runtime_fail(store, "api_parse")
        return ""
    return _guard_or_log(result, store)


# ---------------------------------------------------------------------------
# 深读（P4）专用入口。
#
# 为什么不复用 summarize()：summarize() 的三条约定是**为日报摘要绑在一起**的
#   ① 无条件把 SYSTEM 拼在 instruction 前面，而 SYSTEM 第 5 条是「两句话以内」
#   ② CLI 的 --json-schema 固定为 {"summary": string} 且 additionalProperties
#      为 false，_extract_summary 只把 summary 字符串交回调用方
#   ③ 数字溯源要求输出里每个数字 token 都在材料里逐字出现
# 深读 stage1 要的是一个含 8 条判定 + 引用的 JSON 对象（②直接让它不可能回来），
# stage2 要的是四段带固定标题的中文（①把它砍成两句、③把整段丢掉）。
#
# 所以这里新增两个入口，**summarize() 的 SYSTEM / schema / 数字溯源一字不动**，
# 日报链路（P3）完全不受影响。两个入口沿用同一套语义：
#   - check_env 的 LLMConfigError 照旧冒泡，配置错误必须响亮失败
#   - 运行时失败记 llm.runtime 并返回空，调用方降级
#   - 输出仍然过 _guard_or_log 的指令性措辞守卫
# ---------------------------------------------------------------------------


def _coerce_object(value):
    """把 CLI/API 的结构化结果归一成 dict（不抽 summary —— 深读要整个对象）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("结构化输出为空")
        parsed = json.loads(raw)      # JSONDecodeError 是 ValueError 的子类
        if not isinstance(parsed, dict):
            raise ValueError("结构化输出不是对象")
        return parsed
    raise ValueError("结构化输出类型不正确")


def _parse_cli_structured(raw):
    """CLI 外层 JSON → 结构化对象。与 _parse_cli_output 的差别只在不抽 summary。"""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("CLI 外层输出不是对象")
    candidate = data.get("structured_output")
    if candidate is None:
        candidate = data.get("structuredOutput")
    if candidate is None:
        candidate = data.get("result")
    return _coerce_object(candidate)


def _raw_completion(cfg, material, instruction, schema, timeout, store):
    """跑一次 LLM，返回结构化输出对象；运行时失败返回 None（已记 source_health）。

    ``instruction`` **原样**当系统提示，不拼 SYSTEM —— 深读的两个 instruction
    自带完整要求（输出形状、判定口径、不给买卖建议），再叠一层「两句话以内」
    只会自相矛盾。
    """
    check_env(cfg)   # LLMConfigError 不在这里捕获，直接冒泡

    provider = cfg.get("llm.provider", "claude_cli")

    if provider == "claude_cli":
        LLM_WORKDIR.mkdir(parents=True, exist_ok=True)
        cmd = _cli_cmd(cfg, instruction, schema)
        try:
            p = subprocess.run(cmd, input=material, capture_output=True,
                               text=True, timeout=timeout, cwd=str(LLM_WORKDIR))
            if p.returncode != 0:
                _runtime_fail(store, "cli_nonzero")
                return None
            return _parse_cli_structured(p.stdout)
        except subprocess.TimeoutExpired:
            _runtime_fail(store, "cli_timeout")
            return None
        except (json.JSONDecodeError, ValueError):
            _runtime_fail(store, "cli_parse")
            return None
        except Exception:
            _runtime_fail(store, "cli_exception")
            return None

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
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
    except Exception:
        _runtime_fail(store, "api_exception")
        return None
    try:
        return _coerce_object(text)
    except ValueError:
        _runtime_fail(store, "api_parse")
        return None


def _string_values(obj):
    """结构化输出里所有字符串叶子值 —— 守卫要看的正是这些会进报告的文本。"""
    out = []

    def walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(obj)
    return "\n".join(out)


def complete_json(cfg, material, instruction, schema, dry_run=False,
                  timeout=180, store=None):
    """结构化输出入口（深读 stage1）。返回 **JSON 字符串**，失败返回空串。

    返回字符串而不是 dict，是为了让调用方的解析路径只有一条：stage1 本来
    就要处理「模型返回垃圾」的情况，多一条 dict 分支只会多一处未测代码。

    这里**不做数字溯源**：stage1 的输出是布尔判定加财报原文引用，引用本身
    就是从材料里抄的文本，对它做 token 级溯源只会因为截断/换行把整份判定
    丢掉，而这些判定里没有任何数字会进报告的数值链路（计分只认白名单里的
    两条布尔，见 deepread._clean_text_hits）。
    """
    if dry_run:
        return ""
    obj = _raw_completion(cfg, material, instruction, schema, timeout, store)
    if obj is None:
        return ""
    payload = _string_values(obj)
    if payload and _guard_or_log(payload, store) == "":
        return ""     # 引用里出现指令性措辞：整份判定作废，且已记 llm.guard
    return json.dumps(obj, ensure_ascii=False)


def complete_text(cfg, material, instruction, dry_run=False, timeout=180,
                  store=None):
    """长文输出入口（深读 stage2 的四段叙述）。失败返回空串。"""
    if dry_run:
        return f"[dry-run] 将把 {len(material)} 字材料交给 LLM 深读"
    obj = _raw_completion(cfg, material, instruction, LONG_TEXT_SCHEMA,
                          timeout, store)
    if obj is None:
        return ""
    text = obj.get("text")
    if not isinstance(text, str):
        # 兼容：模型偶尔会沿用摘要那张 schema 的 key。
        text = obj.get("summary")
    if not isinstance(text, str) or not text.strip():
        _runtime_fail(store, "empty_text")
        return ""
    text = text.strip()
    try:
        _validate_numeric_magnitude(text, material)
    except ValueError as e:
        return _guard_or_log(text, store, guard_error=str(e))
    return _guard_or_log(text, store)
