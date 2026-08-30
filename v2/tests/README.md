# V2 未来测试目录
状态：DRAFT
实现状态：未实现
截至：2026-08-30

本文只列出未来测试与 fixture 的责任边界；路径、测试入口和 live checks 都是未来接口，不是当前代码，当前没有 V2 测试实现。

## 未来 fixture 与测试类别

- source normalization、candidate union、freshness 和 source health；
- fail-closed behavior，包括 network、source 和 parser failure 不变成空成功；
- per-ticker Agent contract、隔离状态和 Research Verdict schema；
- directive guard，拒绝 action、buy、sell、position_size、target_price 及其等价指令；
- deterministic ranking、20–30 预筛、unknown/zero/negative 区分；
- stable ties，最终按 normalized ticker ordering；
- report safety，包括 evidence quality、bull/bear、risk review、unknowns 和 Top N/10；
- outbox idempotency，每个分析日期只保留一个 aggregate top10 事件；
- schedule rendering，包括 local-wall-clock 08:00 目标和实测基准占位；
- separately authorized live checks，分别覆盖 network/provider、真实 macOS launchd 和手机投递，不用静态测试冒充这些证据。

这些类别不会在本次任务中生成 Python、依赖、数据库 schema、provider 配置或真实通知。未来测试必须链接到[分阶段验收门槛](../docs/acceptance.md)，并明确 verified、historical、draft、untested 状态。
