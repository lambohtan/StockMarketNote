# 报告、outbox 与 08:00 调度
状态：DRAFT
实现状态：未实现
截至：2026-08-31

本文描述未来报告和本地投递的接口边界；`SQLite/outbox`、scheduler、notifier 和任何 topic 都不是当前代码或当前运行配置。Reddit 数据段已有可被未来 scheduler 调用的
`prefetch_reddit.py` CLI 和独立 SQLite 缓存，但这不等于通用 scheduler、launchd 或
08:00 交付链已经实现；边界见 [Reddit 预抓取接口](reddit-prefetch.md)。

## 报告与事件

- 每个分析日期生成一个 aggregate `top10` 报告，而不是一组互不聚合的单票分数。
- 每个分析日期生成一个幂等 outbox 事件；事件键、分析日期和报告版本必须支持重复运行去重。
- 报告展示排名输入、配置版本、数据质量、evidence completeness、bull case、bear case、risk review 和 uncertainties/unknowns。
- 报告不得包含 action、buy、sell、position size、target price、直接买卖指令或预测承诺；研究观点不等于执行建议。
- 报告应显示实际 `Top 10/10` 或 `Top N/10` 语义，不得以失败或未研究对象补齐。

## 08:00 交付目标

08:00 是本机 local-wall-clock 的交付目标，不是已经通过的送达事实。计算开始时间必须等未来对 20–30 个 ticker 的真实运行耗时完成基准后再确定，不能从目标送达时间倒推成当前配置。

未来调度应区分：分析日期、计算开始、重试窗口、outbox 建立、通知尝试和实际送达结果。时区、夏令时、机器休眠、网络不可用和重试都必须在后续设计中定义。

## V1 概念的受限复用

V1 的 outbox/notify 概念可作为未来参考，但只有在后续迁移设计和 live acceptance 通过后才可重新采用。V1 的历史调度路径不构成 V2 scheduler；本次不加载、修改或验证真实 macOS launchd，也不发送真实 ntfy/手机通知。

通知配置必须在受保护运行时提供，真实 notification topic 不进入 tracked 文档。失败、重试、幂等冲突和未送达必须可见；outbox 中的事件不能仅因计划时间到达就标为 delivered。

## 未来接口

未来可能需要 report artifact、analysis-date idempotency key、outbox state、delivery attempt 和 local-wall-clock policy 等接口，但本次不创建 schema、Python 模块、scheduler 配置或 provider/通知凭证。这些都是未来边界，不是当前代码。
