# 分阶段验收门槛
状态：DRAFT
实现状态：未实现
截至：2026-08-30

本文定义未来 V2 的验收顺序；门槛、测试路径和 live checks 都是未来接口，不是当前代码。V2 在后续计划逐门满足前保持未实现状态。

## Gate 1：契约冻结

| 验收面 | 未来门槛 |
| --- | --- |
| 静态测试 | 校验 schema、禁止交易指令、状态标记、unknown 语义、相对链接和脱敏文档；确认没有 V2 Python、provider 配置或数据库 schema。 |
| network/provider | 不适用；静态通过不能替代未来来源和模型 provider 验收。 |
| 真实 macOS launchd | 不适用；不加载、不安装、不修改真实 LaunchAgent。 |
| 手机投递 | 不适用；不发送真实通知，不把 topic 放入 tracked 文档。 |
| 退出条件 | 产品边界、数据契约、Verdict 字段、排名规则和隐私边界由后续设计明确批准。 |

## Gate 2：数据与确定性预筛

| 验收面 | 未来门槛 |
| --- | --- |
| 静态测试 | source normalization、candidate union、字段完整性、freshness、source health、fail-closed 和 20–30 预筛的 fixture 测试。 |
| network/provider | 用获授权的 provider 验证 EDGAR mapping、候选价格覆盖、限流、解析失败和真实响应时间；来源失败不能成为空成功。 |
| 真实 macOS launchd | 尚不验收 launchd；此门只证明数据阶段的可复现行为，不能宣称本机调度已通过。 |
| 手机投递 | 尚不验收手机；没有 outbox 事件就不能宣称交付。 |
| 退出条件 | 多来源覆盖与失败语义达到后续批准阈值，且预筛输入/输出可审计、可复现。 |

## Gate 3：Agent adapter 与 validated Research Verdict

| 验收面 | 未来门槛 |
| --- | --- |
| 静态测试 | 单票 Agent contract、进程/实例隔离、schema validator、evidence 定位、model provenance、partial/failed 状态和 directive guard 测试。 |
| network/provider | 在获授权的 LLM provider 或本地 endpoint 上验证超时、上下文不足、模型失败、真实 evidence 读取和 provider 条款；不把自由文本直接传给 ranker。 |
| 真实 macOS launchd | 不在本门加载 launchd；仅记录未来运行时需要的资源/超时证据。 |
| 手机投递 | 不在本门发送；Verdict 通过不等于报告或手机已送达。 |
| 退出条件 | 每个 ticker 的 Verdict 可独立验证，失败可见，且没有 Trader/Portfolio Manager 或买卖指令内容。 |

## Gate 4：确定性排名

| 验收面 | 未来门槛 |
| --- | --- |
| 静态测试 | 20–30 预筛、版本化配置、unknown/zero/negative 区分、stable ties、normalized ticker 末级排序、evidence/data-quality 保留和 Top N/10 fixture 测试。 |
| network/provider | 使用获授权的真实或 sandbox 数据确认数值事实来源、字段覆盖和 provider freshness；不得用 live 响应掩盖静态规则缺陷。 |
| 真实 macOS launchd | 不在本门验收 launchd；排名通过不表示本机能按时计算。 |
| 手机投递 | 不在本门验收；Top10 结果必须先成为待投递报告，不能假报已送达。 |
| 退出条件 | 对同一输入重复运行得到相同顺序；至少十个合格对象才输出 Top10，否则输出准确的 Top N/10。 |

## Gate 5：报告与幂等 outbox

| 验收面 | 未来门槛 |
| --- | --- |
| 静态测试 | aggregate top10 报告、rank inputs、证据质量、bull/bear、risk review、unknowns、禁止指令、analysis-date 幂等键和重复事件测试。 |
| network/provider | 在获授权的通知 sandbox 或 provider 上验证失败、重试、超时和响应记录；真实 topic 不写入仓库。 |
| 真实 macOS launchd | 仍不加载真实 launchd；仅可在后续调度门确认 outbox 触发方式。 |
| 手机投递 | 静态事件状态不是手机送达；需要另行授权的真实投递测试，且必须记录 attempt/result。 |
| 退出条件 | 每个分析日期至多一个幂等 top10 事件，失败可重试且未送达可见。 |

## Gate 6：调度与通知

| 验收面 | 未来门槛 |
| --- | --- |
| 静态测试 | local-wall-clock 08:00 目标、时区/夏令时、重试窗口、机器休眠和通知状态渲染测试；计算开始时间只来自实测基准。 |
| network/provider | 对 20–30 ticker 完整链路做获授权的运行基准，验证 provider 速率限制、失败恢复和通知服务响应。 |
| 真实 macOS launchd | 经明确授权后，在真实 macOS launchd 上验证安装、触发、日志、退出码、休眠恢复和卸载；本仓库重组阶段不做此事。 |
| 手机投递 | 经明确授权后，用受保护 topic 验证手机实际收到一次、重复事件不重复送达、失败状态可见；不能用 API 200 代替手机证据。 |
| 退出条件 | 真实 launchd 触发链和手机投递证据分别满足，且 08:00 只作为交付目标被验证。 |

## Gate 7：shadow run 与 cutover

| 验收面 | 未来门槛 |
| --- | --- |
| 静态测试 | 影子运行报告、旧新结果对账、缺失/失败/unknown 审计、回滚和 cutover 清单测试。 |
| network/provider | 连续获授权 shadow run，记录来源覆盖、Agent 失败、排名稳定性、延迟和 provider 成本/限流。 |
| 真实 macOS launchd | 在 Gate 6 通过后才可进行受控 shadow launchd；必须保留日志和停止/回滚证据。 |
| 手机投递 | 先 shadow/no-op，再经授权进行受控手机投递；不得自动扩大通知对象或使用未审计 topic。 |
| 退出条件 | 连续运行达到后续批准周期，所有失败可解释，cutover/rollback 由人确认；在此之前 V2 仍是文档草案。 |

## 证据口径

静态测试、network/provider、真实 macOS launchd 和手机投递是四类独立证据，任何一类不能替代另一类。未来报告必须标注 verified、historical、draft、untested 的状态；本次没有执行网络、LLM、launchd、ntfy 或手机验收。
