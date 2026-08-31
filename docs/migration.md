# V1 到 V2 迁移矩阵
状态：DRAFT
实现状态：未实现
截至：2026-08-30

本文只记录未来迁移决策；路径、接口和复用关系不是当前代码，也不是当前 V2 实现。V1 的代码与文档已从仓库移除，只存在于 707d7de 及更早的 git 历史里。

## 决策表

| V1 capability | decision | reason | future validation |
| --- | --- | --- | --- |
| source-result concept | reuse | 来源结果、状态和 provenance 是可复用的边界概念 | 用多来源 fixture 验证字段、freshness 和 source health |
| filing/news material fetch | reuse | 申报材料和新闻材料可作为 evidence 输入 | 验证定位、观察时间、缓存和 provider 条款 |
| policy guard | reuse | 现有安全边界可作为禁止交易指令的参考 | 用 directive guard 和绕过措辞 fixture 验证 |
| outbox/notify concepts | reuse | 幂等事件和失败可见性适合未来交付层 | 先完成迁移设计，再做真实 provider/手机投递验收 |
| candidate pool | adapt | V2 需要多来源并集、规范化和 20–30 确定性预筛 | 验证 CIK 映射、价格覆盖、来源健康和 fail-closed |
| facts | adapt | 事实必须携带证据质量、时间和 unknown 语义 | 用 provider fixture 与冲突/缺失事实测试 |
| LLM boundary | adapt | LLM 只形成受证据约束的研究 prose 和 verdict | 验证字段契约、provenance、隔离实例和禁止指令 |
| rendering | adapt | 需要一个 aggregate top10 报告并显示 Top N/10 | 验证安全文本、证据质量、未知项和不足十项输出 |
| Store schema | adapt | V2 需要承载 Verdict、排名输入、报告和幂等 outbox | 先冻结领域模型，再做 SQLite 迁移和只读/写入测试 |
| schedule | adapt | 08:00 是 local-wall-clock 送达目标，计算时间需实测后决定 | 做 20–30 ticker 基准、时区/休眠和 launchd 验收 |
| fixed two-call deepread orchestration | replace | V2 需要每票隔离研究并在跨股票层统一排名 | 用多 ticker 隔离、失败传播和批处理测试 |
| single-ticker score presentation | replace | V2 报告需要稳定的跨股票 Top10 语义 | 验证 rank key、stable ties 和 Top N/10 |
| Trader/Portfolio Manager decisions | exclude | 产品只做研究和风险审查，不做交易决策 | 静态 directive guard；不连接券商或下单路径 |
| TradingAgents-CN Web stack | exclude | `app/`、`frontend/`、Web/application stack、用户系统和专有服务不在范围 | 代码审查确认没有复制或依赖这些组件 |
| automatic order execution | exclude | 不下单、不提供买卖指令或交易执行接口 | 静态审查和集成边界测试 |
| EDGAR mapping | validate first | V1 存在空或不完整的 CIK→ticker 映射 | network/provider 和覆盖率验收 |
| candidate price coverage | validate first | 候选股票的历史价格可能不完整 | 对真实 20–30 ticker 做覆盖率与缺失传播验收 |
| provider licenses/APIs | validate first | API 条款、许可证、费率和限流尚未批准 | 逐 provider 审核并做最小授权连接测试 |
| latency | validate first | 不能在没有真实批处理数据时决定计算开始时间 | 实测多 ticker、Agent 隔离和重试耗时 |
| rate limits | validate first | 多来源和多 Agent 会改变请求压力 | provider sandbox/approved live checks |
| live notification delivery | validate first | 静态 outbox 不等于真实手机送达 | 单独授权后做 macOS 与手机投递验收 |

## 迁移原则

复用只表示概念可作为输入，不表示可以直接搬运 V1 入口、配置或运行时数据。改造和替换必须先有 V2 设计、契约测试和隐私审查；排除项不能因上游框架默认包含就重新进入范围。

TradingAgents 和 TradingAgents-CN 仅可作为外部研究概念参考；不复制源码，不把 CN `app/`、`frontend/` 或 Web stack 纳入仓库，并在任何适配前独立核实许可证。

本文件不执行迁移、不修改 V1 运行数据、不创建 V2 Python 或数据库。所有表格条目都是未来边界和待验证事项。
