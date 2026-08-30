# V1 审计与历史资料索引

本表区分归档代码、历史设计、已有限制的实现和本地审计证据。`current replacement or controlling document` 指当前应遵循的归档入口或仓库级约束，不把历史材料重新当作现行规范。

| 新路径 | 原路径 | 主题 | 状态 | 当前替代或控制文档 |
| --- | --- | --- | --- | --- |
| [`v1/docs/history/00-调研报告.md`](history/00-调研报告.md) | `00-调研报告.md` | 初始调研与数据源、工具选型 | 历史设计 | [`v1/README.md`](../README.md)；V2 方向由仓库重组设计控制 |
| [`v1/docs/history/01-产品定位与偏好档案.md`](history/01-产品定位与偏好档案.md) | `01-产品定位与偏好档案.md` | 产品定位和用户偏好 | 历史设计 | [`v1/README.md`](../README.md)与根 [`CLAUDE.md`](../../CLAUDE.md) |
| [`v1/docs/history/02-系统设计.md`](history/02-系统设计.md) | `02-系统设计.md` | 归因、提醒、想法流和旧路线 | 历史设计 | [`v1/README.md`](../README.md)；V2 约束见仓库重组设计 |
| [`v1/docs/history/03-需求可行性与架构.md`](history/03-需求可行性与架构.md) | `03-需求可行性与架构.md` | 可行性评估与旧架构 | 历史设计 | [`v1/README.md`](../README.md)；当前跨版本约束见根 [`CLAUDE.md`](../../CLAUDE.md) |
| [`v1/docs/history/HANDOFF.md`](history/HANDOFF.md) | `HANDOFF.md` | 阶段交接、已知问题和历史验证 | 历史设计 | [`v1/README.md`](../README.md)；证据等级由 V1 总览控制 |
| [`v1/docs/history/CLAUDE-v1.md`](history/CLAUDE-v1.md) | `CLAUDE.md` | 旧版项目指令 | superseded | 根 [`CLAUDE.md`](../../CLAUDE.md) |
| [`v1/docs/history/stockwatch-README-v1.md`](history/stockwatch-README-v1.md) | `stockwatch/README.md` | 旧安装、运行和 P2 说明 | 历史设计 | [`v1/README.md`](../README.md) |
| [`v1/docs/engineering/2026-08-27-daily-push-and-menubar-design.md`](engineering/2026-08-27-daily-push-and-menubar-design.md) | `docs/superpowers/specs/2026-08-27-daily-push-and-menubar-design.md` | P3 日报、投递和菜单栏设计 | implemented-with-limitations | [`v1/README.md`](../README.md)；未来 V2 文档由仓库重组设计控制 |
| [`v1/docs/engineering/2026-08-27-p3-daily-push.md`](engineering/2026-08-27-p3-daily-push.md) | `docs/superpowers/plans/2026-08-27-p3-daily-push.md` | P3 实施计划与验收框架 | implemented-with-limitations | [`v1/README.md`](../README.md)；当前未验证项见 Task 1 报告 |
| [`v1/docs/engineering/2026-08-28-p4-股票池深读-design.md`](engineering/2026-08-28-p4-股票池深读-design.md) | `docs/superpowers/specs/2026-08-28-p4-股票池深读-design.md` | P4 股票池与逐票深读设计 | implemented-with-limitations | [`v1/README.md`](../README.md)；缺少跨股票 Top 10 |
| [`v1/docs/engineering/2026-08-29-p4-probe-findings.md`](engineering/2026-08-29-p4-probe-findings.md) | `docs/superpowers/plans/2026-08-29-p4-probe-findings.md` | P4 数据和正文探测结论 | historical design | [`v1/README.md`](../README.md)与 P4 代码入口 |
| [`v1/docs/engineering/2026-08-29-p4-股票池深读.md`](engineering/2026-08-29-p4-股票池深读.md) | `docs/superpowers/plans/2026-08-29-p4-股票池深读.md` | P4 逐票深读实施计划 | implemented-with-limitations | [`v1/README.md`](../README.md)；未来跨股票排名不在 V1 |
| `v1/local-data/audit/sdd/2026-08-27-p3-daily-push/` | `.superpowers/sdd/2026-08-27-p3-daily-push/` | P3 ledger、task reports 和 review diffs | local audit evidence | [`v1/README.md`](../README.md)；仅作历史证据 |

## 明确归档为历史的内容

以下内容即使出现在旧文档或未更新的 checkbox 中，也不代表当前能力或当前规范：100-point scorecard、旧 `core/jobs/web` 目录布局、旧 Reddit exclusion rule，以及未勾选的 implementation boxes。它们由上表对应的历史设计或实施计划保留，仅用于追溯；当前 V1 口径以 [`v1/README.md`](../README.md)和根 [`CLAUDE.md`](../../CLAUDE.md)为准。
