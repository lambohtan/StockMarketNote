#!/usr/bin/env python3
"""stage1/stage2 与 sw/llm.py 的接线测试。运行：python3 tests/test_deepread_llm_wiring.py

**这个文件补的是 P4 唯一真正的测试盲区。**

`tests/test_deepread.py` 的 11 条断言全部注入 `caller=lambda *a, **k: ...`，
把真实的 LLM 适配器整个绕开了。于是「stage1/stage2 接在 `LM.summarize` 上，
而那个适配器的契约装不下它们要的输出」这件事，在测试全绿的情况下活到了
最终审查：
  - `summarize()` 无条件把 SYSTEM 拼在 instruction 前面，SYSTEM 第 5 条是
    「用中文，简体，**两句话以内**」——stage2 要的四段叙述被直接否掉
  - CLI 走 `--json-schema {"summary": string}` 且 additionalProperties 为
    false，`_extract_summary` 只把 summary 字符串交回——stage1 要的
    `{"hits":...,"quotes":...}` 在那条路径上不可能回来
  - `_validate_numeric_provenance` 一个数字 token 对不上就丢整段——材料里是
    `0.94`，模型写「营收同比增长 94%」，整段作废

这里**不注入 caller**，让 stage1/stage2 真的穿过 `sw/llm.py`，在 subprocess
那一层伪造 CLI 响应（手法照抄 P3 的 tests/test_llm.py）。不联网、不花钱。
"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import llm as LM
from sw.deepread import deepread as D

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


class Cfg:
    def get(self, key, default=None):
        return {"llm.provider": "claude_cli", "llm.model": "claude-opus-5",
                "llm.max_tokens": 2000,
                "llm.claude_bin": "/fake/claude"}.get(key, default)


class FakeStore:
    """只记 log_health，不碰真数据库。"""
    def __init__(self):
        self.calls = []

    def log_health(self, source, ok, latency_ms, detail=""):
        self.calls.append((source, ok, latency_ms, detail))


class Completed:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class FakeCLI:
    """替换 LM.subprocess.run：记录命令与输入，返回预设的 CLI 外层 JSON。"""
    def __init__(self, payload, wrap="structured_output", returncode=0):
        self.payload, self.wrap, self.returncode = payload, wrap, returncode
        self.cmds, self.inputs = [], []

    def __call__(self, cmd, input=None, capture_output=None, text=None,
                 timeout=None, cwd=None):
        self.cmds.append(cmd)
        self.inputs.append(input)
        if self.wrap == "structured_output":
            body = {"structured_output": self.payload}
        else:                     # 旧版 CLI：result 里放 JSON 字符串
            body = {"result": json.dumps(self.payload, ensure_ascii=False)}
        return Completed(self.returncode, json.dumps(body, ensure_ascii=False))


@contextlib.contextmanager
def fake_cli(fake):
    """挂上假 subprocess，并确保 claude_cli 模式的配置守卫不被环境里的 key 打断。"""
    old_run = LM.subprocess.run
    old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    LM.subprocess.run = fake
    try:
        yield fake
    finally:
        LM.subprocess.run = old_run
        if old_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = old_key


def instruction_of(cmd):
    return cmd[cmd.index("--append-system-prompt") + 1]


def schema_of(cmd):
    return json.loads(cmd[cmd.index("--json-schema") + 1])


STAGE1_PAYLOAD = {
    "hits": {"revenue_growth": True, "operating_cash_flow": True,
             "gross_margin": False, "low_correlation": False,
             "insider_filings": True, "rank_jump": True},
    "quotes": {"gross_margin": "we expect gross margin to normalize in the mid-70s"},
    "text_hits": {"guidance_direction": False, "no_new_risk": True},
    "text_quotes": {"guidance_direction": "we expect gross margin to normalize"},
}

NARRATIVE = ("发生了什么：数据中心营收同比增长 94%，管理层同时提到毛利率将回落。\n\n"
             "多头论点：订单能见度延伸到明年上半年。\n\n"
             "空头论点：新产品线初期成本推高，毛利率环比 -0.4pt。\n\n"
             "接下来盯什么：① 下季度指引 ② 库存水平 ③ 客户集中度")

MATERIAL = ('{"ticker": "NVDA", "原始数字": {"revenue_yoy": 0.94, '
            '"gross_margin_delta_pt": -0.4, "rank": 12}}')

PY_RESULT = {"hits": {"revenue_growth": True, "operating_cash_flow": True,
                      "gross_margin": True, "low_correlation": False,
                      "insider_filings": True, "rank_jump": True},
             "details": {k: k for k in ("revenue_growth", "operating_cash_flow",
                                        "gross_margin", "low_correlation",
                                        "insider_filings", "rank_jump")},
             "hit": 5, "total": 6}

FACTS = {"ticker": "NVDA", "revenue_yoy": 0.94, "gross_margin_delta_pt": -0.4,
         "rank": 12, "missing": []}


def test_stage1_round_trips_through_real_adapter():
    """不注入 caller：stage1 必须真的穿过 sw/llm.py 拿回结构化判定。

    这是 C1 的核心回归锁 —— 接回 LM.summarize 时，这条会得到全空判定。
    """
    print("\nstage1 端到端（不注入 caller）")
    fake = FakeCLI(STAGE1_PAYLOAD)
    with fake_cli(fake):
        r = D.stage1(Cfg(), "NVDA", MATERIAL, store=FakeStore())
    check("拿回 6 条对照判定", r["hits"]["gross_margin"], False)
    check("拿回 2 条文本判定", r["text_hits"], STAGE1_PAYLOAD["text_hits"])
    check("拿回原文引用", "mid-70s" in r["quotes"]["gross_margin"], True)

    cmd = fake.cmds[0]
    check("发出了一次 CLI 调用", len(fake.cmds), 1)
    check("instruction 里没有摘要专用的『两句话以内』",
          "两句话以内" in instruction_of(cmd), False)
    check("instruction 就是 stage1 的原文",
          instruction_of(cmd), D.STAGE1_INSTRUCTION)
    schema = schema_of(cmd)
    check("json-schema 不是摘要那张 {summary}",
          "summary" in schema.get("properties", {}), False)
    check("json-schema 要的是 hits/quotes/text_hits/text_quotes",
          sorted(schema["properties"]), ["hits", "quotes", "text_hits", "text_quotes"])


def test_stage1_accepts_legacy_result_wrapper():
    """旧版 CLI 把结构化结果塞在 result 里（JSON 字符串），也要能解出来。"""
    print("\nstage1 端到端：CLI 旧版 result 包装")
    with fake_cli(FakeCLI(STAGE1_PAYLOAD, wrap="result")):
        r = D.stage1(Cfg(), "NVDA", MATERIAL)
    check("result 包装也解得出", r["hits"]["revenue_growth"], True)


def test_stage2_long_narrative_survives_end_to_end():
    """四段叙述必须原样回来 —— 不被 SYSTEM 砍成两句，不被数字溯源整段丢掉。"""
    print("\nstage2 端到端（不注入 caller）：四段叙述 + 材料里的 94% 都要活着")
    fake = FakeCLI({"text": NARRATIVE})
    with fake_cli(fake):
        r = D.stage2(Cfg(), "NVDA", PY_RESULT, {"hits": {}, "quotes": {},
                                                "text_hits": STAGE1_PAYLOAD["text_hits"]},
                     FACTS, store=FakeStore())
    check("四段全在", r["narrative"].count("\n\n"), 3)
    for head in ("发生了什么：", "多头论点：", "空头论点：", "接下来盯什么："):
        check(f"含固定标题 {head!r}", head in r["narrative"], True)
    check("没有退化成『LLM 深读失败』", "LLM 深读失败" in r["narrative"], False)
    # 材料里是 0.94，叙述写 94% —— 旧的严格溯源会把整段丢掉
    check("材料里的 0.94 写成 94% 不被判成幻觉", "94%" in r["narrative"], True)

    cmd = fake.cmds[0]
    check("instruction 里没有『两句话以内』",
          "两句话以内" in instruction_of(cmd), False)
    check("走的是长文 schema", sorted(schema_of(cmd)["properties"]), ["text"])
    check("计分仍由 py + 白名单决定", (r["hit"], r["total"]), (6, 8))


def test_stage2_rejects_fabricated_magnitude():
    """模型引入材料里没有的量级数字 → 整段作废并记 llm.guard，不静默塞进报告。"""
    print("\nstage2 端到端：编造的量级数字被拦下并留痕")
    store = FakeStore()
    bad = NARRATIVE.replace("同比增长 94%", "同比增长 340%")
    with fake_cli(FakeCLI({"text": bad})):
        r = D.stage2(Cfg(), "NVDA", PY_RESULT, {"hits": {}, "quotes": {},
                                                "text_hits": {}},
                     FACTS, store=store)
    check("叙述退化成明写失败", "LLM 深读失败" in r["narrative"], True)
    check("记了一条 llm.guard",
          any(c[0] == "llm.guard" for c in store.calls), True)


def test_stage1_guard_blocks_directive_quotes():
    """引用里出现无归属的买卖指令 → 整份判定作废，且必须留痕。"""
    print("\nstage1 端到端：指令性措辞守卫仍然生效")
    store = FakeStore()
    payload = json.loads(json.dumps(STAGE1_PAYLOAD))
    payload["quotes"]["gross_margin"] = "建议买入该标的"
    with fake_cli(FakeCLI(payload)):
        r = D.stage1(Cfg(), "NVDA", MATERIAL, store=store)
    check("判定作废", r["hits"], {})
    check("记了一条 llm.guard",
          any(c[0] == "llm.guard" for c in store.calls), True)


def test_runtime_failure_degrades_and_logs():
    """CLI 非零退出 → 空结果 + llm.runtime，绝不抛给调用方。"""
    print("\n运行时失败：降级 + 留痕")
    store = FakeStore()
    with fake_cli(FakeCLI(STAGE1_PAYLOAD, returncode=1)):
        r = D.stage1(Cfg(), "NVDA", MATERIAL, store=store)
    check("空判定", r["hits"], {})
    check("记了 llm.runtime",
          any(c[0] == "llm.runtime" for c in store.calls), True)


def test_config_error_still_bubbles_through_new_entry():
    """新入口不能把配置错误吞掉 —— 那会在没人注意的情况下持续按 API 计费。"""
    print("\n配置错误：穿过新入口仍然冒泡")
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
    try:
        D.stage1(Cfg(), "NVDA", MATERIAL)
        ok = False
    except LM.LLMConfigError:
        ok = True
    except Exception:
        ok = False
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    check("stage1 的 LLMConfigError 冒泡", ok, True)


def test_summarize_behaviour_is_untouched():
    """P3 的日报链路一字不动：SYSTEM 仍拼、schema 仍是 {summary}、溯源仍严格。"""
    print("\n回归锁：summarize() 的既有行为不受本轮改动影响")
    cmd = LM.build_cli_cmd(Cfg(), "把材料压成一句话")
    check("摘要路径仍拼 SYSTEM 的『两句话以内』",
          "两句话以内" in LM.SYSTEM, True)
    check("摘要路径的 schema 仍是 {summary}",
          sorted(schema_of(cmd)["properties"]), ["summary"])
    check("摘要路径的 additionalProperties 仍为 false",
          schema_of(cmd)["additionalProperties"], False)

    fake = FakeCLI({"summary": "营收同比增长 94%"})
    with fake_cli(fake):
        with contextlib.redirect_stderr(io.StringIO()):
            out = LM.summarize(Cfg(), "材料：0.94", "压成一句话")
    check("严格溯源仍然丢弃对不上的数字（未改动）", out, "")
    check("摘要路径的 instruction 仍以 SYSTEM 开头",
          instruction_of(fake.cmds[0]).startswith(LM.SYSTEM), True)


for fn in (test_stage1_round_trips_through_real_adapter,
           test_stage1_accepts_legacy_result_wrapper,
           test_stage2_long_narrative_survives_end_to_end,
           test_stage2_rejects_fabricated_magnitude,
           test_stage1_guard_blocks_directive_quotes,
           test_runtime_failure_degrades_and_logs,
           test_config_error_still_bubbles_through_new_entry,
           test_summarize_behaviour_is_untouched):
    print(fn.__name__)
    fn()

print("\n❌ 失败：" + ", ".join(FAIL) if FAIL else "\n✅ 全部通过")
sys.exit(1 if FAIL else 0)
