"""LLM provider backed by the local `claude` CLI (Claude Code) instead of an HTTP API.

Why this exists
---------------
TradingAgents talks to every provider through LangChain chat models. The
`claude` CLI is not an HTTP API: it is a one-shot subprocess that takes a
prompt on stdin and prints a JSON envelope on stdout. This module wraps that
subprocess in a `BaseChatModel` so the rest of the framework — the LangGraph
tool loop, `bind_tools`, `with_structured_output` — keeps working unchanged.

Three invocation modes map onto the three things the framework asks for:

  text        no `--json-schema`; the CLI's `result` string is the answer.
              Used by the bull/bear researchers and the risk debators, which
              want long free-form markdown.
  tools       a union schema that lets the model either request tool calls or
              emit a final answer. This substitutes for native tool-calling,
              which the CLI does not expose. Used by the market, news, and
              fundamentals analysts.
  structured  the Pydantic schema is handed straight to `--json-schema`.
              Used by the Research Manager, Trader, Portfolio Manager, and
              Sentiment Analyst.

CLI flags, and why each one is there
------------------------------------
  --system-prompt         REPLACES Claude Code's own (very large) system
                          prompt. With `--append-system-prompt` instead, every
                          call ships ~28k tokens of agent scaffolding that the
                          analyst prompts neither need nor want. Measured:
                          28k -> 0.8k input tokens per call.
  --tools ""              No built-in tools. Tool use is driven by the union
                          schema above, not by Claude Code's own tool loop.
  --safe-mode             Ignores CLAUDE.md, skills, hooks, plugins and MCP.
                          Without it the *host project's* CLAUDE.md leaks into
                          every analyst call.
  --no-session-persistence  These are throwaway calls; do not litter ~/.claude.
  --permission-mode dontAsk  Nothing may block on a prompt in a batch run.
  cwd=<empty dir>         Claude Code loads context from the working directory.
                          Run it somewhere with nothing in it.

NOT `--bare`: bare mode refuses OAuth and demands ANTHROPIC_API_KEY, which
defeats the entire point of routing through the subscription.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from .base_client import BaseLLMClient

DEFAULT_CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")
DEFAULT_WORKDIR = Path.home() / ".tradingagents" / "claude-cli-workdir"
DEFAULT_TIMEOUT = 900


class ClaudeCLIError(RuntimeError):
    """The claude CLI failed in a way the caller cannot paper over."""


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------

@dataclass
class CallRecord:
    """What one `claude` subprocess cost, in tokens and in seconds.

    `wall_s` minus `api_s` is Claude Code's process startup, which is charged
    once per call and is invisible in the API-reported duration. On a run with
    many short calls it dominates, so it is tracked separately rather than
    folded into the model's time.
    """

    node: str
    mode: str
    model: str
    # What the CLI actually billed, e.g. {"claude-sonnet-5"}. Usually the alias
    # resolved; occasionally Claude Code also charges a small haiku call of its
    # own, which only shows up here and not in the requested model name.
    models_used: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    # 一次 CLI 调用内部的 API 往返次数。Claude Code 用 --json-schema 时会先让
    # 模型发结构化工具调用、再收一轮，所以 prompt 会被重发。输入 token 比
    # prompt 本身大好几倍，原因就在这里，不记下来账对不上。
    iterations: int = 1
    api_s: float = 0.0
    wall_s: float = 0.0

    @property
    def startup_s(self) -> float:
        return max(0.0, self.wall_s - self.api_s)


@dataclass
class RunLedger:
    """Collects `CallRecord`s across every agent in one run."""

    records: list[CallRecord] = field(default_factory=list)

    def add(self, record: CallRecord) -> None:
        self.records.append(record)

    def by_node(self) -> dict[str, list[CallRecord]]:
        grouped: dict[str, list[CallRecord]] = {}
        for record in self.records:
            grouped.setdefault(record.node, []).append(record)
        return grouped

    def total(self, attr: str) -> float:
        return sum(getattr(r, attr) for r in self.records)


def _node_from_metadata(metadata: Any, fallback: str = "") -> str:
    """Pull the LangGraph node name out of run metadata, if it is there."""
    if isinstance(metadata, dict):
        name = metadata.get("langgraph_node")
        if isinstance(name, str) and name:
            return name
    return fallback or "?"


# ---------------------------------------------------------------------------
# JSON Schema plumbing
# ---------------------------------------------------------------------------

# Keys Pydantic emits that carry no constraint the model needs, and that some
# schema validators reject outright.
_DROP_KEYS = {"title", "default", "$schema", "additionalProperties"}


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Resolve local ``$ref``s and normalise a Pydantic schema for the CLI.

    Pydantic puts enums and nested models in ``$defs`` and points at them with
    ``$ref``. Inlining them keeps the schema self-contained. Every object is
    also pinned to ``additionalProperties: false`` with *all* properties marked
    required — optional fields stay expressible because Pydantic already types
    them as ``anyOf: [..., {"type": "null"}]``, and an explicit null round-trips
    back to ``None``.
    """
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        ref = node["$ref"]
        name = ref.rsplit("/", 1)[-1]
        target = _inline_refs(defs.get(name, {}), defs)
        # Sibling keys (notably `description`) override the referenced body.
        extra = {k: v for k, v in node.items() if k != "$ref" and k not in _DROP_KEYS}
        return {**target, **extra}

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("$defs", *_DROP_KEYS):
            continue
        out[key] = _inline_refs(value, defs)

    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


def schema_for_cli(model: type[BaseModel]) -> dict[str, Any]:
    """Turn a Pydantic model into a schema the ``--json-schema`` flag accepts."""
    raw = model.model_json_schema()
    return _inline_refs(raw, raw.get("$defs", {}))


# The union schema that stands in for native tool-calling. `arguments_json` is a
# *string* rather than a nested object on purpose: an open-ended object has no
# schema the validator can check, and OpenAI's own function-calling wire format
# takes the same approach.
TOOL_PROTOCOL_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments_json": {
                        "type": "string",
                        "description": "Arguments as a JSON object encoded in a string.",
                    },
                },
                "required": ["name", "arguments_json"],
                "additionalProperties": False,
            },
        },
        "final_answer": {"type": "string"},
    },
    "required": ["tool_calls", "final_answer"],
    "additionalProperties": False,
}

_TOOL_PROTOCOL_INSTRUCTIONS = """

=== TOOL PROTOCOL ===
You cannot call tools directly. Instead you emit a JSON object matching the
output schema, and the runtime executes the tools for you and calls you again
with the results.

To request tools: fill "tool_calls" with one or more entries and set
"final_answer" to an empty string. Each entry is
{"name": "<exact tool name>", "arguments_json": "<arguments as a JSON object encoded in a string>"}.
Example: {"name": "get_stock_data", "arguments_json": "{\\"symbol\\": \\"NVDA\\", \\"start_date\\": \\"2026-01-01\\", \\"end_date\\": \\"2026-06-01\\"}"}

To finish: set "tool_calls" to [] and put your complete final report in
"final_answer". Do not stop until you have gathered the data you need.
Never invent data you have not received from a tool result.

Available tools:
{tools_json}
"""


# ---------------------------------------------------------------------------
# Message -> transcript
# ---------------------------------------------------------------------------

def _text_of(message: BaseMessage) -> str:
    """Flatten message content to a string; providers may hand back block lists."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def split_messages(messages: list[BaseMessage]) -> tuple[str, str]:
    """Split a message list into (system prompt, stdin transcript).

    The transcript is labelled prose rather than JSON because it frequently
    carries raw tool payloads — OHLCV CSVs run to tens of thousands of
    characters, and re-encoding them as JSON strings only burns tokens.
    """
    system_parts: list[str] = []
    transcript: list[str] = []

    for message in messages:
        text = _text_of(message)
        if isinstance(message, SystemMessage):
            system_parts.append(text)
        elif isinstance(message, ToolMessage):
            name = getattr(message, "name", None) or "tool"
            transcript.append(f"### TOOL RESULT [{name}]\n{text}")
        elif isinstance(message, AIMessage):
            calls = message.tool_calls or []
            if calls:
                requested = ", ".join(c.get("name", "?") for c in calls)
                transcript.append(f"### YOUR PREVIOUS TURN\nYou requested: {requested}")
            if text:
                transcript.append(f"### YOUR PREVIOUS TURN\n{text}")
        elif isinstance(message, HumanMessage):
            transcript.append(f"### USER\n{text}")
        else:
            transcript.append(text)

    return "\n\n".join(p for p in system_parts if p), "\n\n".join(transcript)


def _coerce_messages(value: Any) -> list[BaseMessage]:
    """Accept the several shapes LangChain callers pass to ``invoke``."""
    if isinstance(value, str):
        return [HumanMessage(content=value)]
    if isinstance(value, BaseMessage):
        return [value]
    if hasattr(value, "to_messages"):
        return list(value.to_messages())
    if isinstance(value, list):
        out: list[BaseMessage] = []
        for item in value:
            if isinstance(item, BaseMessage):
                out.append(item)
            elif isinstance(item, tuple) and len(item) == 2:
                role, content = item
                out.append(
                    SystemMessage(content=content) if role == "system"
                    else AIMessage(content=content) if role in ("ai", "assistant")
                    else HumanMessage(content=content)
                )
            else:
                out.append(HumanMessage(content=str(item)))
        return out
    return [HumanMessage(content=str(value))]


# ---------------------------------------------------------------------------
# The chat model
# ---------------------------------------------------------------------------

class ChatClaudeCLI(BaseChatModel):
    """LangChain chat model that shells out to the local ``claude`` CLI."""

    model: str = "sonnet"
    claude_bin: str = DEFAULT_CLAUDE_BIN
    timeout: int = DEFAULT_TIMEOUT
    workdir: str = str(DEFAULT_WORKDIR)
    max_attempts: int = 2
    verbose_cli: bool = False
    ledger: Any = None
    # 追加在系统提示最末尾的输出约束。放最后是有意的：上游分析师的 prompt 要求
    # "very detailed"、"as much detail as possible"，同一件事上后出现的指令赢。
    brevity: str = ""
    # 忽略 bind_tools 绑定的工具，强制走纯文本一次成文。预取模式用它——证据
    # 已经在 prompt 里了，再留着工具只会让模型多跑几轮把上下文重发一遍。
    ignore_tools: bool = False
    # 当调用方自己驱动流程（不经 LangGraph）时，run metadata 里没有节点名。
    # 调用前设置这个字段，账目里才不会全是 "?"。
    node_label: str = ""

    @property
    def _llm_type(self) -> str:
        return "claude_cli"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model, "claude_bin": self.claude_bin}

    # -- public LangChain surface -------------------------------------------

    def bind_tools(self, tools, **kwargs: Any):
        converted = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=converted, **kwargs)

    def with_structured_output(self, schema, include_raw: bool = False, **kwargs: Any):
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise NotImplementedError(
                "ChatClaudeCLI.with_structured_output only supports Pydantic models"
            )
        json_schema = schema_for_cli(schema)

        def _run(value: Any, config: Any = None) -> BaseModel:
            messages = _coerce_messages(value)
            system, transcript = split_messages(messages)
            metadata = config.get("metadata") if isinstance(config, dict) else None
            payload = self._call_cli(
                system=system + _structured_instructions(),
                stdin=transcript,
                json_schema=json_schema,
                node=_node_from_metadata(metadata, self.node_label),
                mode="structured",
            )
            return schema.model_validate(payload)

        return RunnableLambda(_run)

    # -- BaseChatModel hook --------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tools = None if self.ignore_tools else kwargs.get("tools")
        system, transcript = split_messages(messages)
        node = _node_from_metadata(getattr(run_manager, "metadata", None), self.node_label)

        if tools:
            system = system + _TOOL_PROTOCOL_INSTRUCTIONS.replace(
                "{tools_json}", json.dumps(tools, ensure_ascii=False, indent=2)
            )
            payload = self._call_cli(
                system=system, stdin=transcript,
                json_schema=TOOL_PROTOCOL_SCHEMA, node=node, mode="tools",
            )
            message = _tool_payload_to_message(payload)
        else:
            text = self._call_cli(
                system=system, stdin=transcript,
                json_schema=None, node=node, mode="text",
            )
            message = AIMessage(content=text)

        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- subprocess ----------------------------------------------------------

    def _build_cmd(self, json_schema: dict[str, Any] | None) -> list[str]:
        cmd = [
            self.claude_bin,
            "-p",
            "--model", self.model,
            "--output-format", "json",
            "--tools", "",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode", "dontAsk",
        ]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema, ensure_ascii=False)]
        return cmd

    def _call_cli(
        self,
        system: str,
        stdin: str,
        json_schema: dict[str, Any] | None,
        node: str = "?",
        mode: str = "text",
    ) -> Any:
        """Run the CLI once (with retries) and return `structured_output` or `result`."""
        check_no_api_key()
        workdir = Path(self.workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        if self.brevity:
            system = f"{system}\n\n{self.brevity}".strip()
        cmd = self._build_cmd(json_schema)
        if system:
            cmd += ["--system-prompt", system]

        last_error: str = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            started = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    input=stdin or "Proceed.",
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=str(workdir),
                )
            except subprocess.TimeoutExpired:
                last_error = f"timed out after {self.timeout}s"
                continue

            if proc.returncode != 0:
                last_error = f"exit {proc.returncode}: {proc.stderr.strip()[:400]}"
                continue

            try:
                envelope = json.loads(proc.stdout)
            except json.JSONDecodeError:
                last_error = f"stdout was not JSON: {proc.stdout[:300]}"
                continue

            if envelope.get("is_error"):
                last_error = f"CLI reported error: {str(envelope.get('result'))[:400]}"
                continue

            record = self._record(envelope, node, mode, time.time() - started)
            if self.ledger is not None:
                self.ledger.add(record)
            if self.verbose_cli:
                print(
                    f"    [{record.node}] {record.model}  "
                    f"in {record.input_tokens + record.cache_read + record.cache_write} "
                    f"out {record.output_tokens} tok  "
                    f"{record.wall_s:.0f}s (启动 {record.startup_s:.0f}s)",
                    flush=True,
                )

            if json_schema is None:
                result = envelope.get("result")
                if isinstance(result, str) and result.strip():
                    return result.strip()
                last_error = "empty result text"
                continue

            payload = envelope.get("structured_output")
            if payload is None:
                # Older/edge CLI builds put the JSON in `result` as a string.
                raw = envelope.get("result")
                if isinstance(raw, str):
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = None
            if payload is None:
                last_error = "no structured_output in CLI envelope"
                continue
            return payload

        raise ClaudeCLIError(
            f"claude CLI failed after {self.max_attempts} attempts ({last_error})"
        )


    def _record(self, envelope: dict[str, Any], node: str, mode: str,
                wall_s: float) -> CallRecord:
        """Read the CLI's usage block into a CallRecord.

        `input_tokens` counts only *uncached* input. The bulk of a long analyst
        prompt normally lands in `cache_read_input_tokens`, so reporting
        `input_tokens` alone understates real usage by orders of magnitude.
        """
        usage = envelope.get("usage") or {}
        return CallRecord(
            node=node,
            mode=mode,
            model=self.model,
            models_used=tuple(sorted((envelope.get("modelUsage") or {}).keys())),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_write=int(usage.get("cache_creation_input_tokens") or 0),
            cost_usd=float(envelope.get("total_cost_usd") or 0.0),
            iterations=max(1, len(usage.get("iterations") or []) or int(envelope.get("num_turns") or 1)),
            api_s=float(envelope.get("duration_ms") or 0) / 1000.0,
            wall_s=wall_s,
        )


def _structured_instructions() -> str:
    return (
        "\n\nRespond with a single JSON object matching the required output "
        "schema. Use only the evidence already present in this prompt."
    )


def _tool_payload_to_message(payload: dict[str, Any]) -> AIMessage:
    """Turn the union-schema payload into an AIMessage the tool loop understands."""
    raw_calls = payload.get("tool_calls") or []
    tool_calls = []
    for call in raw_calls:
        name = call.get("name")
        if not name:
            continue
        raw_args = call.get("arguments_json") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        tool_calls.append(
            {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:16]}", "type": "tool_call"}
        )

    if tool_calls:
        return AIMessage(content="", tool_calls=tool_calls)
    return AIMessage(content=payload.get("final_answer") or "")


def check_no_api_key() -> None:
    """Refuse to run when ANTHROPIC_API_KEY is set.

    Claude Code silently switches from the subscription to metered API billing
    when it sees this variable. A batch of 11 agent calls per ticker is exactly
    the situation where "silently" turns expensive, so this fails loudly
    instead. Set STOCKWATCH_ALLOW_API_KEY=1 to accept the charges deliberately.
    """
    if os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("STOCKWATCH_ALLOW_API_KEY"):
        raise ClaudeCLIError(
            "ANTHROPIC_API_KEY is set. Claude Code would silently bill the API "
            "instead of using your subscription. Unset it, or set "
            "STOCKWATCH_ALLOW_API_KEY=1 to proceed anyway."
        )


# ---------------------------------------------------------------------------
# Provider client
# ---------------------------------------------------------------------------

_PASSTHROUGH_KWARGS = ("timeout", "claude_bin", "workdir", "max_attempts", "verbose_cli")


class ClaudeCLIClient(BaseLLMClient):
    """Factory-facing wrapper, matching the other providers' shape."""

    provider = "claude_cli"

    def get_llm(self) -> Any:
        kwargs: dict[str, Any] = {"model": self.model}
        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs and self.kwargs[key] is not None:
                kwargs[key] = self.kwargs[key]
        env_bin = os.environ.get("CLAUDE_CLI_BIN")
        if env_bin and "claude_bin" not in kwargs:
            kwargs["claude_bin"] = env_bin
        return ChatClaudeCLI(**kwargs)

    def validate_model(self) -> bool:
        # Model names are CLI aliases ("opus", "sonnet", "haiku") or full model
        # ids; the CLI is the authority on what it accepts, not a local list.
        return True
