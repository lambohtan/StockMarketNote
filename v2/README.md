# StockWatch V2
状态：DRAFT
实现状态：未实现
截至：2026-08-30

本文只定义未来 V2 的文档边界；其中的路径、接口和流程不是当前代码，也不表示对应功能已经存在。

## 目标流程

V2 的目标产品流是：

```text
多来源股票池 → Python 确定性预筛 20–30 → research Agents → validated Research Verdict → Python 跨股票确定性排名 → Top10 → SQLite/outbox → 本地墙钟 08:00 投递
```

Python 负责候选池标准化、确定性预筛和跨股票排名；research Agents 只在每只股票的证据边界内形成研究材料，随后由结构化验证器产生 Research Verdict。所有环节都属于后续设计，当前没有可运行的 V2 入口。

## 当前状态

这是 documentation-only 的 V2 起稿。当前不存在 V2 Python、数据库、scheduler、provider 配置、Agent prompt、可执行入口或真实投递设置。文档中提到的目录和接口是未来代码边界，不是现有实现。

## 产品边界

- 研究与证据整理是范围；不下单、不连接自动交易执行，也不输出直接买入或卖出指令。
- 不承诺价格预测、收益预测或投资结果；Top10 只表示按未来批准的确定性规则整理出的研究对象。
- 研究概念仅包括 market、fundamentals、news、sentiment、bull/bear、Research Manager 和 risk review。
- 明确排除 Trader、Portfolio Manager 的交易决策、自动下单以及任何把自由文本变成交易指令的路径。
- 数据缺失、来源失败和验证失败必须可见，不能静默变成空成功或自动负分。

## 与 V1 的关系

V1 是[冻结归档](../v1/README.md)。V1 能力到 V2 的复用、改造、替换、排除和待验证项目见[迁移矩阵](docs/migration.md)。V2 不要求 V1 在新路径下继续可运行，也不读取或复制 V1 的私有运行时资料。

## 文档索引

- [架构与组件边界](docs/architecture.md)
- [数据源与候选池契约](docs/data-sources.md)
- [Research Verdict 契约](docs/research-verdict.md)
- [确定性排名与 Top10](docs/ranking-top10.md)
- [报告、outbox 与 08:00 调度](docs/report-notify-schedule.md)
- [V1 到 V2 迁移矩阵](docs/migration.md)
- [分阶段验收门槛](docs/acceptance.md)
- [未来源码目录说明](src/README.md)
- [未来测试目录说明](tests/README.md)

## 第三方边界

可参考上游的研究编排概念：[TradingAgents](https://github.com/TauricResearch/TradingAgents) 与 [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN)。本仓库不复制任一上游源码、应用或整仓库；只有经许可证核实且与范围相符的 open-core 函数，或抽象的研究概念，才可能在后续设计中适配。

TradingAgents-CN 的 `app/`、`frontend/`、Web/application stack、用户系统、数据库、专有调度和通知模块均不在 V2 范围内。具体许可证、版本、API 和可适配范围必须在后续实现前单独验证；本文件不构成许可结论。
