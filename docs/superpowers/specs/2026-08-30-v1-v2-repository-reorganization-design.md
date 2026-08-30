# StockWatch V1 归档与 V2 起稿设计

状态：已确认，待实施计划

确认日期：2026-08-30

适用仓库：`/Users/lambo/Desktop/projects/股票市场`

## 1. 目标

本次工作只整理仓库并为 V2 建立文档骨架，不实现 V2 业务功能。

完成后：

1. 当前代码、测试、历史文档和本地运行数据统一归入 `v1/`。
2. `v1/README.md` 成为唯一的 V1 能力总览，明确证据等级和已知限制。
3. 旧规格、计划和审计材料继续保留，但不再与当前规范混在一起。
4. `v2/` 包含完整的设计文档骨架以及未来源码、测试目录说明。
5. 根目录只保留跨版本导航、共同约束、Git 配置和仓库级设计记录。

## 2. 已确认的关键决策

| 决策 | 结论 |
|---|---|
| V1 物理边界 | 代码、测试、文档和工具整体迁入 `v1/` |
| V1 运行状态 | 冻结归档，不保证迁移后入口、配置或 launchd 可运行 |
| 本地运行数据 | 迁入 `v1/local-data/`，继续由 Git 忽略 |
| V2 起稿深度 | 创建完整文档骨架及源码、测试目录说明，不写 Python 实现 |
| 顶层布局 | `v1/` 与 `v2/` 同级 |
| 第三方 Agent 框架 | 只记录可复用研究能力，不复制 TradingAgents 或 TradingAgents-CN 全仓库 |
| V2 产品边界 | 不下单，不输出买卖指令，不把 LLM 自由文本直接用作排序分数 |

## 3. 当前仓库事实

### 3.1 已存在的主要能力

当前 `stockwatch/` 已包含以下代码路径：

- `run_ingest.py`：Fidelity CSV、价格、社区热度和 SEC 数据采集入口。
- `run_portfolio.py`：组合分析和报告。
- `run_daily.py`：日报、归因、风险和提醒。
- `run_pool.py`：候选池、材料下载、Python 与 LLM 双路深读。
- `run_notify.py`：outbox 通知出口。
- `sw/store.py`、`sw/outbox.py`、`sw/notify.py`、`sw/schedule.py`：SQLite、可靠投递和 launchd 配置基础。
- `sw/deepread/`：V1 股票池与逐票深读实现。
- `tests/`：当前实现对应的 direct-run 测试文件和 fixture。

### 3.2 不能宣称已完成的部分

- 当前没有跨股票的确定性 Top 10 排名层。
- 当前股票池报告以逐票结果为主，不是一份统一的 Top 10 日报。
- TradingAgents 研究流程尚未接入本地项目。
- 真实 launchd、持续 LLM 调用和手机通知没有当前验收证据。
- 当前 SQLite 运行态与最新代码 schema 不完全一致。
- EDGAR 全市场 CIK 到 ticker 映射、候选股票价格覆盖等仍有已知缺口。

### 3.3 文档问题

当前根目录和 `docs/superpowers/` 同时包含早期调研、过时路线、已作废评分卡、已实施规格和未更新 checkbox。`CLAUDE.md`、`HANDOFF.md`、`stockwatch/README.md` 的阶段描述也不一致。因此 V1 总览必须按实际证据重写，旧文档只作为历史来源。

## 4. 目标目录

```text
股票市场/
├── README.md
├── CLAUDE.md
├── .gitignore
├── docs/
│   └── superpowers/
│       ├── specs/
│       └── plans/
├── v1/
│   ├── README.md
│   ├── docs/
│   │   ├── history/
│   │   ├── engineering/
│   │   └── AUDIT-INDEX.md
│   ├── stockwatch/
│   ├── tools/
│   └── local-data/
│       ├── config/
│       ├── database/
│       ├── uploads/
│       ├── reports/
│       └── audit/
└── v2/
    ├── README.md
    ├── docs/
    │   ├── architecture.md
    │   ├── data-sources.md
    │   ├── research-verdict.md
    │   ├── ranking-top10.md
    │   ├── report-notify-schedule.md
    │   ├── migration.md
    │   └── acceptance.md
    ├── src/
    │   └── README.md
    └── tests/
        └── README.md
```

`docs/superpowers/` 只保存本次仓库重组的规格和实施计划。V1 以前的 P3/P4 工程资料移入 `v1/docs/engineering/`。

## 5. 文件迁移规则

### 5.1 历史文档

以下文档移入 `v1/docs/history/`，保留原文，不把过时描述改写成当前事实：

- `00-调研报告.md`
- `01-产品定位与偏好档案.md`
- `02-系统设计.md`
- `03-需求可行性与架构.md`
- `HANDOFF.md`
- 旧 `stockwatch/README.md`

根目录 `CLAUDE.md` 改为跨版本共同约束，不再记录 P1/P2/P3/P4 进度。旧版内容中具有历史价值的阶段描述由 V1 总览和历史文档索引引用。

### 5.2 工程资料

现有 `docs/superpowers/specs/` 与 `docs/superpowers/plans/` 中属于 P3/P4 的资料移入 `v1/docs/engineering/`。文件名保留日期和主题，以便追溯 Git 历史。

`.superpowers/sdd/` 是本地过程材料，移入 `v1/local-data/audit/sdd/`，继续忽略，不把大量 review diff 纳入正式文档。

### 5.3 源码和工具

- 当前 `stockwatch/` 中除真实配置和运行数据外的代码、测试、依赖及 packaging 结构移入 `v1/stockwatch/`。
- 根目录 `tools/` 移入 `v1/tools/`。
- 不为新路径修复 import、README 命令、launchd 或配置寻找逻辑。
- `v1/README.md` 必须明确写明该版本是冻结归档。

### 5.4 配置和本地数据

真实运行数据统一放入 `v1/local-data/`：

| 当前内容 | 目标位置 |
|---|---|
| `stockwatch/config.yaml` | `v1/local-data/config/config.yaml` |
| `stockwatch/data/stockwatch.db*` | `v1/local-data/database/` |
| `stockwatch/uploads/*.csv` | `v1/local-data/uploads/` |
| `stockwatch/reports/*.md` | `v1/local-data/reports/` |
| `.superpowers/sdd/` | `v1/local-data/audit/sdd/` |
| 本地工具设置 | `v1/local-data/audit/tooling/` |

`v1/stockwatch/config.example.yaml` 只保留字段结构和安全占位值，不包含真实 SEC 邮箱、ntfy topic、API key 或其他私人配置。

当前 `stockwatch/config.yaml` 已被 Git 跟踪。实施时不能用会让目标文件继续保持 tracked 状态的普通 rename 流程：必须先把真实内容安全迁入忽略目录，再把旧路径的删除作为 Git 变更记录，并确认新目标没有出现在 `git ls-files`。脱敏后的 `config.example.yaml` 作为新的跟踪文件加入。

把当前配置从工作树移入忽略目录不会清除 Git 历史中的旧值。重写 Git 历史不属于本次范围；如需清除历史凭证，应单独评估，并优先轮换相关值。

### 5.5 Git 忽略规则

`.gitignore` 至少应覆盖：

- `v1/local-data/`
- 任意位置的 `*.csv`
- SQLite 主库及 `-wal`、`-shm`
- 生成报告和临时缓存
- Python 缓存

实施后用 `git check-ignore` 和 `git ls-files` 双重验证本地数据未被跟踪。

## 6. V1 总览设计

`v1/README.md` 是唯一的 V1 能力总览，使用以下证据等级：

- **代码已存在**：当前归档代码中可以直接定位到实现。
- **历史测试声称**：旧报告或测试记录声称通过，本次没有重新证明运行态。
- **当前运行曾验证**：存在真实样例、数据库或历史运行记录，但不代表迁移后仍可运行。
- **未验证**：没有足够证据，或只完成静态设计。
- **已知缺陷**：已有明确反例、数据缺口或部署缺口。

V1 总览包含：

1. 产品范围与非目标。
2. 实际代码结构与数据流。
3. P1 数据底座。
4. P2 组合体检。
5. P3 日报、提醒、outbox 与调度基础。
6. P4 股票池与逐票深读。
7. 测试和样例证据。
8. 当前运行态和本地数据说明。
9. 已知限制与不可宣称事项。
10. 历史资料索引。

`v1/docs/AUDIT-INDEX.md` 为所有历史文档提供新路径、原主题、状态和替代规范。旧评分卡、旧目录设计和未更新 checkbox 只能作为历史记录，不能作为当前能力说明。

## 7. V2 目标与文档设计

### 7.1 目标流程

```text
多来源股票代码
→ ticker 标准化、去重和来源健康检查
→ Python 确定性预筛约 20–30 只
→ 精简版多 Agent 研究
→ 结构化 Research Verdict
→ Python 跨股票确定性排序
→ Top 10 报告
→ 每天本机时间 08:00 发送
```

### 7.2 Agent 能力边界

V2 只计划采用以下研究能力：

- 市场与技术分析
- 基本面分析
- 新闻分析
- 情绪分析
- 多头与空头研究员辩论
- Research Manager 汇总
- 风险审查

V2 明确排除：

- Trader 交易计划
- Portfolio Manager 的 Buy/Sell 决策
- 自动下单和券商交易接口
- TradingAgents-CN 的 Web、用户系统、MongoDB、Redis、前端、专有调度和通知模块
- 将第三方仓库整体复制到项目中

第三方框架只作为可替换的研究引擎参考。V2 必须拥有自己的领域模型、数据适配器、批处理、排名、报告和投递边界。

### 7.3 文档职责

| 文档 | 职责 |
|---|---|
| `v2/README.md` | V2 目标、非目标、状态、文档索引和当前缺口 |
| `architecture.md` | 端到端流程、组件边界、数据流及第三方 Agent 隔离方式 |
| `data-sources.md` | 股票池来源、ticker 标准化、provenance、freshness、覆盖率、失败语义和 API key 清单 |
| `research-verdict.md` | Agent 输入输出结构、证据、正反观点、风险、未知项、数据质量和禁止字段 |
| `ranking-top10.md` | 20–30 预筛、确定性排名原则、缺失值、稳定 tie-break 和 Top N/10 语义 |
| `report-notify-schedule.md` | Top 10 聚合报告、outbox、08:00 发送目标、幂等和失败可见性 |
| `migration.md` | V1 能力的复用、改造、淘汰和待验证分类 |
| `acceptance.md` | 数据源、Agent、排名、报告、通知、调度的分阶段验收门槛 |

所有 V2 文档顶部统一声明：

```text
状态：DRAFT
实现状态：未实现
截至：2026-08-30
```

V2 文档使用以下状态标签：

- `[现状·已验证]`
- `[目标·未实现]`
- `[未来代码·占位]`
- `[待验证]`
- `[已废弃]`
- `[验收门槛]`

排序权重、具体模型和实际计算启动时间不在本次重组中决定。文档必须把它们标记为后续 V2 设计任务，而不是含糊地写成已经存在。08:00 是送达目标；计算时间应在测得 20–30 只股票的真实耗时后确定。

### 7.4 源码和测试占位

`v2/src/README.md` 只说明未来可能包含候选池、Agent adapter、Research Verdict、确定性排名、报告和投递模块。`v2/tests/README.md` 只列出未来 fixture、契约测试、排序测试、幂等测试和 live 验收边界。

本次不得在 `v2/` 下创建 `.py` 文件、依赖清单、数据库 schema、Agent prompt 或可执行入口。

## 8. 安全与错误处理

### 8.1 迁移前检查

1. 确认 Git 工作树干净并记录 HEAD。
2. 记录跟踪文件和被忽略文件清单。
3. 记录 SQLite 主库、WAL、SHM 的大小和校验值。
4. 确认没有进程正在写 SQLite。
5. 记录 CSV、报告和本地审计材料，避免漏迁。

### 8.2 停止条件

出现以下任一情况时停止迁移，不覆盖目标：

- 无法确认 SQLite 没有写入者。
- SQLite 主库和 sidecar 无法作为一组处理。
- 目标路径已存在不同内容。
- 移动前后校验不一致。
- 任一跟踪文件无法在目标清单中定位。
- 发现未分类的私人配置或凭证。

### 8.3 明确禁止的副作用

- 不加载、卸载或修改真实 launchd。
- 不运行联网抓取、LLM 或真实 ntfy 推送。
- 不输出或复制真实邮箱、ntfy topic、持仓内容或 API key。
- 不重写 Git 历史。
- 不丢失原始材料内容；路径迁移造成旧路径消失属于本设计的一部分。

## 9. 验收标准

### 9.1 目录与完整性

- 根目录只保留项目导航、跨版本约束、Git 配置和仓库级设计记录。
- 所有原跟踪文件都能在 V1 或根目录的新位置找到。
- 所有计划中的 V1/V2 文档和目录说明存在。
- Markdown 内部链接和索引无断链。

### 9.2 本地数据

- `v1/local-data/` 被 Git 忽略。
- `git ls-files` 不包含 `v1/local-data/` 下任何文件。
- SQLite 通过只读完整性检查。
- SQLite 主库、WAL、SHM 的迁移前后校验一致。
- 脱敏示例配置不含真实私人值。

### 9.3 V1

- V1 Python 文件通过语法编译。
- 不要求 V1 在新路径下可执行。
- V1 总览中的能力均有证据等级和来源路径。
- V1 总览明确列出运行态、部署、数据和测试限制。

### 9.4 V2

- 七份 V2 主题文档和 V2 入口文档全部存在。
- 每份文档均明确标注 `DRAFT` 和 `未实现`。
- `v2/src/` 与 `v2/tests/` 只有说明文档，没有 Python 实现。
- 文档不宣称 Agent、排名、Top 10、08:00 推送或外部服务已经完成。

### 9.5 工程验收

- 文档中没有未决占位符、真实 API key 或私人配置值。
- 展示 `git diff --stat`、重命名摘要和经过脱敏的主要 diff。
- 根据当前安装的 Codex 版本检查 `luna_worker` 配置格式。
- 实际调用 `luna_worker` 完成一个受限验收任务，确认自定义 Agent 配置有效。

## 10. 本次不做

- 不实现 V2 Python 代码。
- 不安装或复制 TradingAgents。
- 不确定具体模型、排序权重或付费 API。
- 不接入新的股票池来源。
- 不创建或迁移生产调度。
- 不发送通知。
- 不证明 V1 在新路径可运行。
- 不处理 Git 历史中的旧配置值。

## 11. 完成定义

本次完成只表示：仓库已经按 V1/V2 分区，V1 历史和本地数据得到安全归档，V1 能力被诚实总结，V2 文档与目录骨架可供后续设计使用。

它不表示 V2 Agent、Top 10、08:00 推送或任何外部服务已经实现或验收。
