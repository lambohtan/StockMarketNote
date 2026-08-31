# Research Verdict 契约
状态：DRAFT
实现状态：未实现
截至：2026-08-30

本文定义未来单票研究结果的结构边界；字段、验证器和 Agent adapter 都是未来接口，不是当前代码，也不表示任何研究流程已经可用。

## 结构化结果

未来每个 ticker 的研究结果应围绕下列字段设计：

```text
schema_version
ticker
market
as_of
status: ok | partial | failed
data_quality
evidence[]: source, locator, observed_at, fact, excerpt
market_analysis
fundamental_analysis
news_analysis
sentiment_analysis
bull_case
bear_case
research_summary
risk_review
uncertainties[]
model_provenance
```

`evidence[]` 中的每条事实必须有来源、定位、观察时间、可审计事实和适度摘录。`status` 表示研究结果是否可供未来资格判断；`data_quality` 和 `uncertainties[]` 必须让缺口可见。字段语义、版本和必填规则需要在后续设计冻结。

## 研究内容边界

允许的研究概念包括 market、fundamentals、news、sentiment、bull case、bear case、Research Manager 汇总和 risk review。Agent 可以整理证据与正反观点，但不产生交易计划或执行动作。

以下字段或等价 prose 被禁止：`action`、`buy`、`sell`、`position_size`、`target_price`，以及任何直接买卖指令、仓位建议或预测承诺。禁止项不能通过换一个自然语言标签绕过。

## 事实与未知

数值事实只能来自 Python/provider 证据及其 provenance，不能由模型臆造。缺少、冲突、过期或无法定位的事实必须保持 unknown，并通过 `data_quality` 或 `uncertainties[]` 表达；unknown 不等于 0，也不等于 negative。

自由文本只用于受边界约束的解释和研究摘要，不能直接成为排序键。未来验证器必须拒绝无 evidence 的数值、无来源的断言和越过禁止字段的结果。

## 验证与隔离

未来 `Research Verdict validator` 需要检查 schema version、ticker/market、一致的 `as_of`、status、evidence 定位、数据质量、模型 provenance 和禁止指令。Agent adapter 对每个 ticker 使用隔离实例或进程，因为上游 graph state 可变；一个 ticker 的上下文、失败或模型输出不能泄漏到另一个 ticker。

验证失败时，结果只能标为 `partial`/`failed` 或进入拒绝队列，不能为了凑 Top10 而转成合格对象。任何未通过验证的结构都必须在后续报告中保持失败可见。

## 未来实现界限

本契约不创建 Python 类型、数据库 schema、prompt、模型配置或 provider adapter。它只是后续设计可以细化的文档接口；当前仓库没有对应实现。
