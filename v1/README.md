# StockWatch V1

## 状态与证据口径

- **代码已存在**：归档树中有可定位的实现文件，例如 [`run_ingest.py`](stockwatch/run_ingest.py) 和 [`run_pool.py`](stockwatch/run_pool.py)；这证明代码在归档中，不证明迁移后的入口可运行。
- **历史测试声称**：旧审计材料记录过测试结果；它们是历史证据，不是本次迁移后的运行验收。参见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。
- **归档运行态证据**：真实数据库、上传文件、报告和审计材料以忽略的本地快照保留；Task 1 记录了快照的只读检查和计数，参见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。
- **未验证**：没有足够的当前验收证据，或只存在设计/静态材料；相关入口和设计可在 [V1 工程资料](docs/engineering/) 中追溯。
- **已知缺陷**：已有明确数据缺口、部署缺口或实现边界；具体项目列在[已知限制](#已知限制)并链接到对应归档代码或审计材料。

V1 是[冻结归档](../docs/superpowers/specs/2026-08-30-v1-v2-repository-reorganization-design.md)。上述证据等级描述归档事实，不表示 V1 在新路径下需要保持可执行。

## 产品范围

V1 是分析工具：它读取持仓快照和外部资料，生成组合、日报、股票池与逐票深读材料；它不下单，也不输出直接买入或卖出指令。范围约束可追溯到[跨版本约束](../CLAUDE.md)和归档的[产品定位](docs/history/01-产品定位与偏好档案.md)。

## 实际架构

Fidelity CSV 和其他来源 → SQLite → 组合分析、日报和股票池流程 → reports / outbox / notify。对应入口和边界分别见 [`run_ingest.py`](stockwatch/run_ingest.py)、[`run_portfolio.py`](stockwatch/run_portfolio.py)、[`run_daily.py`](stockwatch/run_daily.py)、[`run_pool.py`](stockwatch/run_pool.py)、[`run_notify.py`](stockwatch/run_notify.py)，以及 [`Store`](stockwatch/sw/store.py)、[`outbox`](stockwatch/sw/outbox.py) 和 [`notify`](stockwatch/sw/notify.py)。

## P1 数据底座

P1 的采集入口是 [`run_ingest.py`](stockwatch/run_ingest.py)，来源适配器位于 [`sw/sources`](stockwatch/sw/sources/)（包括价格、ETF、Reddit、新闻和 EDGAR），SQLite 持久化由 [`Store`](stockwatch/sw/store.py) 负责。Task 1 的 2026-08-30 只读数据库快照、完整性检查和表计数见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)；该快照是归档运行态证据，不是迁移后服务验收。

## P2 组合体检

组合体检实现位于 [`analysis/portfolio.py`](stockwatch/sw/analysis/portfolio.py)、[`analysis/returns.py`](stockwatch/sw/analysis/returns.py)、[`analysis/risk.py`](stockwatch/sw/analysis/risk.py) 和 [`analysis/report.py`](stockwatch/sw/analysis/report.py)；归档的 [portfolio 样例](local-data/reports/portfolio_2026-08-27.md)保留在忽略的本地 reports 快照中。TWR 需要交易流水，且行情和数据新鲜度受归档样例与当前来源覆盖限制；这些限制及历史样例证据见 [旧版 StockWatch 说明](docs/history/stockwatch-README-v1.md) 和 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。

## P3 日报与投递基础

日报、提醒、政策、outbox、通知和调度实现分别见 [`daily_report.py`](stockwatch/sw/daily_report.py)、[`alerts.py`](stockwatch/sw/alerts.py)、[`policy.py`](stockwatch/sw/policy.py)、[`outbox.py`](stockwatch/sw/outbox.py)、[`notify.py`](stockwatch/sw/notify.py) 和 [`schedule.py`](stockwatch/sw/schedule.py)。P3 的设计与实现审计作为本地忽略证据保留在 [P3 SDD 进度](local-data/audit/sdd/2026-08-27-p3-daily-push/progress.md)，工程来源见 [P3 计划](docs/engineering/2026-08-27-p3-daily-push.md)；这些材料支持历史实现声明，不是当前服务验收。真实 launchd 安装和当前手机投递未验证，见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。

## P4 股票池与逐票深读

逐票深读和候选池实现位于 [`deepread.py`](stockwatch/sw/deepread/deepread.py)、[`pool.py`](stockwatch/sw/deepread/pool.py)、[`facts.py`](stockwatch/sw/deepread/facts.py) 和 [`render.py`](stockwatch/sw/deepread/render.py)；归档的 [pool 样例](local-data/reports/pool_2026-08-29.md)保留在忽略的本地 reports 快照中。P4 目前以逐票结果为主，缺少跨股票的统一 Top 10 排名层；该边界见 [P4 设计](docs/engineering/2026-08-28-p4-股票池深读-design.md) 和[仓库重组设计](../docs/superpowers/specs/2026-08-30-v1-v2-repository-reorganization-design.md)。

## 测试与样例证据

Task 1 在 2026-08-30 记录了 **27 个** direct-run 测试文件；这是归档快照中的文件计数，不是本次重组后运行测试的当前验收，见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。P3 审计曾记录历史测试声称，但这些记录不等同于真实 launchd、持续 LLM 或手机投递已经通过当前验收；参见 [P3 本地审计目录](local-data/audit/sdd/2026-08-27-p3-daily-push/)和同一份 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。

## 已知限制

- 生产环境 CIK→ticker 映射仍为空或不完整，且候选股票价格历史可能缺失；相关采集边界见 [`edgar_src.py`](stockwatch/sw/sources/edgar_src.py) 和 [`prices.py`](stockwatch/sw/sources/prices.py)。
- 归档 SQLite 运行态与最新代码 schema 不完全一致；只读快照和限制说明见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)及 [`store.py`](stockwatch/sw/store.py)。
- 依赖声明不完整，归档依赖文件仅作为历史材料保留，见 [`requirements.txt`](stockwatch/requirements.txt)。
- 没有 `run_weekly.py` 入口；归档入口清单可从 [`v1/stockwatch`](stockwatch/)核对。
- V1 未在新路径下重新部署或运行；真实 launchd 和当前手机投递未验证，见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。

## 本地数据

所有私有运行时资料都位于被 [`.gitignore`](../.gitignore) 忽略的 `local-data/` 下：`config/config.yaml`、`database/`（SQLite 主库及 sidecar）、`uploads/`、`reports/`、`audit/sdd/` 和 `audit/tooling/`。这里只说明目录边界，不列出私有配置、账户、持仓、通知 topic 或报告内容；Task 1 的文件计数与数据库身份见 [Task 1 安全门报告](../.superpowers/sdd/2026-08-30-v1-v2-repository-reorganization/task-1-report.md)。

## 历史资料

历史文档、P3/P4 工程计划和本地审计材料的路径映射见 [AUDIT-INDEX.md](docs/AUDIT-INDEX.md)。
