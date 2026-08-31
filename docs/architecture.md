# 架构与组件边界
状态：DRAFT
实现状态：前三段已实现，其余仍是设计占位
截至：2026-08-31

本文描述端到端架构的组件边界。下表之外的组件都是设计占位，不是当前代码。

## 实现状态对照

| 组件 | 当前状态 | 实现位置 |
| --- | --- | --- |
| Source adapters | 已实现并实测（4 个热度来源 + Nasdaq 上市白名单） | `lean/pool.py`，见[候选池文档](pool.md) |
| Ticker normalizer and candidate union | 已实现并实测 | `lean/pool.py` |
| Deterministic 20–30 prefilter | 已实现并实测（确定性打分 + 读时截断，池子大小可配） | `lean/pool.py`、`lean/pool_store.py` |
| Per-ticker Agent adapter | 部分实现：可产出给人读的研究文本，不是结构化结果 | `lean/pipeline.py`，见[运行手册](lean-pipeline.md) |
| macOS 运行宿主与通用 Job scheduler | 已实现：状态栏、防重入调度、运行账本、停止和本地 Dashboard | `macos/`、`stockwatch_app/`，见[macOS 应用](macos-app.md) |
| Local report publisher | 已实现：汇总现有文章为本地 Markdown；不等于 Top10/outbox/送达 | `stockwatch_app/tasks.py` |
| Research Verdict validator | 未实现 | — |
| Deterministic cross-ticker ranker | 未实现 | — |
| Top 10 renderer | 未实现 | — |
| SQLite/outbox | 未实现（候选池另有自己的 SQLite 快照存储） | — |
| Notifier at 08:00 local time | 未实现 | — |

Reddit 证据预抓取是 Source adapters 之外的独立数据段，已实现，见[预抓取接口](reddit-prefetch.md)。

## 端到端数据流

```text
Source adapters
→ ticker normalizer and candidate union
→ deterministic 20–30 prefilter
→ per-ticker Agent adapter
→ Research Verdict validator
→ deterministic cross-ticker ranker
→ Top 10 renderer
→ SQLite/outbox
→ notifier at 08:00 local time
```

预筛和排名都由 Python 的确定性边界负责；Agent 只处理已经限定的单票证据。每个 ticker 必须使用隔离的 Agent 实例或进程，因为上游图状态是可变的，不能在多个 ticker 之间共享隐式状态。

## 组件契约

### Source adapters

- 职责：从已批准的来源读取候选与证据，附带来源健康、观察时间和新鲜度信息。
- 输入：来源配置、公开或经授权的数据响应、来源时间戳。
- 输出：带 provenance 的原始候选记录，供标准化器消费。
- 失败边界：网络、认证、限流、格式和解析失败必须报告为来源失败；失败不能伪装为空股票池或成功结果。

### Ticker normalizer and candidate union

- 职责：规范 ticker、market、exchange，去重并保留每个来源的理由和健康状态。
- 输入：各 Source adapter 的候选记录。
- 输出：候选并集及其来源证据、freshness 和冲突标记。
- 失败边界：身份歧义、市场冲突或必需字段缺失的记录进入拒绝/待调查状态，不可静默合并。

### Deterministic 20–30 prefilter

- 职责：按未来单独批准并版本化的 Python 配置，从候选并集确定性选出约 20–30 个研究对象。
- 输入：标准化候选并集、来源健康、可复现的数值事实和配置版本。
- 输出：有序或带稳定键的预筛清单，以及未入选原因。
- 失败边界：不得调用 LLM 生成排序键；数据不足不能自动填零或自动负分；配置未批准时只能保留草案，不得当作当前行为。

### Per-ticker Agent adapter

- 职责：为一个 ticker 创建隔离的研究 Agent 边界，并把 market、fundamentals、news、sentiment、bull/bear、Research Manager、risk review 研究材料限制在给定证据内。
- 输入：单 ticker、结构化事实、允许的 filing/news 材料和来源定位。
- 输出：待验证的研究结果和模型 provenance，不直接输出排名分数或交易指令。
- 失败边界：每个 ticker 的 Agent 实例或进程隔离；超时、上下文不足、模型失败或越界文本必须产生 partial/failed 结果，不能被当作合格研究对象。

### Research Verdict validator

- 职责：校验结构、证据引用、数据质量、禁止指令和未知项，形成可供后续确定性处理的 validated Research Verdict。
- 输入：单票 Agent 结果、Python/provider 事实和 evidence[]。
- 输出：符合[Research Verdict 契约](research-verdict.md)的结构化结果，包含 `status`、质量和不确定性。
- 失败边界：字段缺失、证据不可定位、数值无法追溯或出现交易指令时，结果必须标为 partial/failed 或拒绝，不能降级为可排名文本。

### Deterministic cross-ticker ranker

- 职责：只用经批准的结构化字段，对合格研究对象执行跨股票确定性排名。
- 输入：多个 validated Research Verdict、版本化排名配置和稳定的数值事实。
- 输出：带排名输入、数据质量、证据完整度和稳定 tie-break 的有序结果。
- 失败边界：LLM 自由文本不得直接进入 rank key；权重或缺失值规则未批准时不得宣称存在最终排名。

### Top 10 renderer

- 职责：把排名结果渲染成一个 aggregate top10 报告，保留正反观点、风险、未知项和证据质量。
- 输入：ranker 的有序结果及分析日期。
- 输出：安全的报告数据和渲染文本；不足十个合格对象时标记 `Top N/10`。
- 失败边界：不得用失败、未研究或缺证据对象填满十位；不得加入买卖、仓位或目标价指令。

### SQLite/outbox

- 职责：未来持久化候选、Verdict、报告和每分析日唯一的投递事件。
- 输入：经过验证的结果、报告内容和幂等事件键。
- 输出：可审计的存储记录与待投递 outbox 事件。
- 失败边界：写入或事务失败必须可见并保留重试状态；未提交的事件不能被报告为已投递。

### Notifier at 08:00 local time

- 职责：以本机 local-wall-clock 的 08:00 为交付目标，消费幂等 outbox 事件并记录投递尝试。
- 输入：outbox 事件、未来经过授权的通知配置和本机时钟。
- 输出：投递尝试、结果和失败可见性记录。
- 失败边界：不得把计划时间当成真实手机送达；不得在文档或代码中写入真实 topic。20–30 ticker 的实际运行基准完成前，不选择计算开始时间。

## 第三方与排除项

V2 只保留研究概念和经过许可证核实的最小适配边界，不复制 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 或 [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) 源码。Trader、Portfolio Manager、交易计划、自动下单、直接买卖指令均被排除；TradingAgents-CN 的 `app/`、`frontend/` 和 Web/application stack 也被排除。

## 未来边界的状态

上述路径、接口、进程隔离和故障语义要先进入后续设计、测试和验收门槛，之后才可能成为代码契约。本次仓库重组不创建 Python、数据库、provider 配置、scheduler 或 Agent 实现。
