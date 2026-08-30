# V2 未来源码目录
状态：DRAFT
实现状态：未实现
截至：2026-08-30

本文只描述未来源码目录的责任边界；这些路径和接口不是当前代码，目录中当前没有 V2 实现。

## 未来责任文件夹

- `candidate_pool/`：来源适配、ticker 标准化、来源健康和 20–30 确定性预筛。
- `agent_adapter/`：单 ticker 的隔离 Agent 研究边界与 provider 适配。
- `verdict/`：Research Verdict schema、evidence 校验、数据质量和禁止指令验证。
- `ranking/`：版本化配置、结构化 rank key、stable ties 和 Top N/10 资格规则。
- `reporting/`：aggregate top10 报告、安全渲染、正反观点、风险和 unknown 展示。
- `delivery/`：SQLite/outbox 概念、幂等事件和本机 08:00 投递边界。

以上文件夹均是后续设计的占位，当前不存在。未来实现前必须先通过 [验收门槛](../docs/acceptance.md)；本次不创建 `.py`、依赖清单、数据库 schema、scheduler、provider 配置或 Agent prompt。
