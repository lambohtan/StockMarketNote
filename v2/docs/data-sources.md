# V2 数据源与候选池契约
状态：DRAFT
实现状态：未实现
截至：2026-08-30

本文规定未来数据适配器、候选池和凭证边界；字段、路径和接口都是未来契约，不是当前代码，也不表示来源已经接入。

## 候选来源契约

每条候选来源记录至少包含以下字段。标准化后的记录必须保留来源级 provenance，而不是只保留一个 ticker 字符串。

| 字段 | 未来含义 |
| --- | --- |
| `ticker` | 规范化的股票代码 |
| `market` | 市场标识 |
| `exchange` | 交易所标识 |
| `source` | 产生该记录的来源名称 |
| `observed_at` | 来源观察时间，带明确时区或 UTC 语义 |
| `reason` | 进入候选池的可审计理由 |
| `freshness` | 相对于分析日期的时效判断 |
| `source_health` | 成功、部分成功、失败或未知的来源健康状态 |

标准化器需要处理重复、市场冲突、来源间不一致和缺失字段，并保留原始定位供后续 evidence 使用。来源健康不是排名分数；它首先决定记录能否进入下一阶段。

## V1 输入（历史参考）

V1 归档中可追溯的输入包括：Fidelity CSV、yfinance、ApeWisdom、SEC EDGAR、filing text 和 news text。它们是 V2 设计的历史参考，不表示任何 V2 provider adapter 已经存在；V1 的归档说明见 [V1 总览](../../v1/README.md)。

## 已知 V1 缺陷

- EDGAR ticker mapping 可能为空，不能把缺失映射当作全市场覆盖。
- 候选股票的价格历史可能不完整，不能用空历史伪装为完整事实。
- partial-source success 可能掩盖单个来源失败，V2 必须显式传播 source health 并 fail closed。

这些缺陷在未来数据契约、覆盖率测试和 live provider 验收中必须单独核对；本文件不读取或复制 V1 私有运行数据。

## Fail-closed 语义

source、network 或 parser 失败不是一个空的成功股票池。失败必须保留原因、来源和时间，并阻止依赖该来源的阶段把结果当作完整输入。只有明确的、按契约定义的“没有匹配对象”才可以是成功的空集合；未知、未抓取和解析失败必须区分。

候选并集可以合并多个健康来源，但不能用一个来源的成功掩盖另一个来源的失败。后续预筛需知道覆盖率和 freshness，不能把未知值自动变为零或负面分数。

## 凭证边界

未来最小配置只允许在部署环境提供以下之一：一个 LLM provider credential，或一个本地模型 endpoint。SEC provider 需要真实 contact email；该邮箱只在受保护运行时配置，不进入 tracked Markdown。V1 的 yfinance 和 ApeWisdom 不需要 key，但这不代表未来 provider 没有条款或限流要求。

按选定 provider 可选的凭证包括 `FRED_API_KEY`、`ALPHA_VANTAGE_API_KEY`、`TUSHARE_TOKEN` 和 `FINNHUB_API_KEY`。是否使用、权限、费率和许可证必须在后续 provider 评估中确定。

密钥、真实通知 topic、持仓、CSV 内容和生成报告内容永远不进入 tracked YAML 或 Markdown。示例只能使用占位符；本次不创建 provider 配置或凭证文件。

## 后续验证边界

每个来源必须分别验证身份映射、字段覆盖、观察时间、freshness、网络/解析失败、速率限制和许可证。静态契约检查不能代替 network/provider 验收；上述路径和接口在后续设计完成前均保持为未来边界。
