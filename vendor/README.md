# Vendored 上游

## TradingAgents

- 来源：<https://github.com/TauricResearch/TradingAgents>
- 版本：`a33fd4c0f134485a43553a2c23a63cb14adbd88f`（2026-07-18，upstream `v0.3.1`）
- 许可证：Apache License 2.0（见 `TradingAgents/LICENSE`）—— 已核实，允许改编与再分发，
  条件是保留版权声明并标注改动。本文件即改动说明。

搬运时对上游做的改动：

### LLM 后端（换成本地 claude CLI）

| 文件 | 改动 |
| --- | --- |
| `llm_clients/claude_cli_client.py` | **新增。** 把 `claude -p` 包成 LangChain chat model，含 tool-calling 模拟、结构化输出、用量记账。 |
| `llm_clients/factory.py` | 增加 `claude_cli` 分支，三行。 |
| `llm_clients/validators.py` | 把 `claude_cli` 加进 `_ANY_MODEL_PROVIDERS`（模型名由 CLI 判定，不查本地表）。 |

### 数据源缺陷修复（`dataflows/reddit.py`，三处）

原实现让 Reddit 的抓取失败读起来像"没有帖子"，情绪分析师据此写出"社区无人讨论"
——把限流当成了市场事实。这违反本仓库 `CLAUDE.md` 第 3 条。

| 改动 | 原因 |
| --- | --- |
| `_fetch_subreddit_rss` 失败返回 `None` 而非 `[]` | 原来失败与"确实没帖子"都返回 `[]`，下游无法区分。 |
| 逐板块与聚合两处渲染点区分失败与空 | 失败时明确输出 `<FETCH FAILED …>` 并提示模型降低置信度。 |
| 新增 `_fetch_combined`：一次站内搜索覆盖全部板块 + 20 秒最小间隔 | 实测 Reddit 按 IP 突发限流，窗口内**只有第一发**能成功（1 秒、8 秒间隔都 429，25 秒恢复）。原实现每票请求三个板块、间隔 1 秒，后两个必然失败。合并查询把请求量降到 1/3，实测零 429，且召回更好。 |

进程内的 20 秒间隔**不跨进程**；未来若并行跑多只票，需要改成文件锁。

### 其他

删除了 `assets/`（3.6 MB README 截图，与运行无关）。

**上游的 agent、prompt、编排图一个字没改**——包括 Trader、Portfolio Manager 和
买卖评级。`v2/lean/` 是在上游之外另写的编排，不修改上游图。

### 为什么 LLM 后端要换

上游走各家 HTTP API，按量计费。`claude` CLI 走 Claude Pro/Max 订阅。适配层把
CLI 的「一次性子进程」包装成 LangChain chat model，代价是 tool-calling 要自己
用一张 union JSON schema 模拟——CLI 不暴露原生 tool use。细节见
`claude_cli_client.py` 的模块 docstring。
