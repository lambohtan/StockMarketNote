# StockWatch

状态：候选股票池、Reddit 预抓取、单票研究管线可运行；macOS 状态栏宿主、独立定时任务和本地 Dashboard 已实现；跨股票排名 → Top10 → outbox → 手机投递仍未实现
截至：2026-08-31

**输出是研究材料，不是下单指令。**

---

## 一分钟版本

常驻运行（已安装在本机）：

```text
/Applications/StockWatch.app
```

启动后点击 macOS 状态栏的图表图标，可打开 Dashboard、启动/停止本地服务、
设置登录时启动或退出。Dashboard 内可单独调度、立即运行或停止每个任务。

命令行开发：

```bash
python3 -m venv .venv
.venv/bin/pip install -e vendor/TradingAgents

.venv/bin/python build_pool.py          # 建/更新今天的候选池
.venv/bin/python read_pool.py           # 看当前池子前 20 只
.venv/bin/python run_lean.py NVDA       # 研究其中一只，约 3 分钟
```

唯一需要配的凭证是 `FRED_API_KEY`（免费，只影响宏观判断）；配置方法见
[运行手册](docs/lean-pipeline.md#数据源与凭证)。

---

## 入口

| 命令 | 用途 |
| --- | --- |
| `build_pool.py` | 更新候选池：4 个公开热度来源 → Nasdaq 美股白名单 → Python 确定性排名 → SQLite 快照。不调用 LLM。 |
| `read_pool.py --top 20` | 读取当前池子（table/json/tickers/pool-file）。只读、不联网；其它应用可直接 `from lean.pool_store import load_latest_pool`。 |
| `prefetch_reddit.py --pool-file <FILE>` | Reddit 预抓取段：股票池 → 一周原始帖子/评论 → SQLite。抓取时不筛选、不调用 LLM。 |
| `run_lean.py <TICKER>` | **单票研究日常入口。**7 节点精简管线，约 3 分钟 / $0.20–0.26。 |
| `run_analysis.py <TICKER>` | 上游 12 节点原貌，含 Trader 与 Portfolio Manager，**会输出买卖指令**。仅供人工观察上游行为，不接入任何自动流程。 |
| `stockwatch_service.py` | macOS sidecar/本地开发入口：定时器、任务状态、日志和 `127.0.0.1` Dashboard。 |

诊断工具（`tools/`，不属于产品流程）：

| 命令 | 用途 |
| --- | --- |
| `tools/check_sources.py <TICKER>` | 数据源体检：14 个输入端逐个真实调用，报告可用/空/失败/缺凭证。 |
| `tools/stability_test.py <TICKER> -n 5` | 同配置重复跑，统计评级分布。单次评级不能当定论时用它验证。 |

---

## 目录

| 路径 | 内容 |
| --- | --- |
| `lean/` | 可运行实现：`pool*.py` 候选池、`reddit_prefetch.py` 预抓取、`pipeline.py` 等单票研究管线 |
| `config/pool.json` | 候选池运行配置（池子大小、筛选下限、来源权重） |
| `docs/` | 契约与运行手册，见下方文档索引 |
| `tests/` | 单元测试（离线，不联网） |
| `tools/` | 诊断脚本 |
| `stockwatch_app/` | 长期运行宿主：配置、运行账本、调度、任务适配、HTTP API 和 Dashboard |
| `macos/` | 原生 AppKit 状态栏宿主与 `Info.plist` |
| `scripts/build_macos_app.sh` | 构建/安装标准 `StockWatch.app` bundle |
| `vendor/TradingAgents/` | 上游原貌副本 + 6 处改动，清单见 [vendor/README.md](vendor/README.md) |
| `local-data/` | 池子数据库、缓存、报告和 memory log。**未被 Git 跟踪** |

---

## 三段已实现的管线

```text
build_pool.py            → pool.sqlite3 快照（全量排名，读时截断）
  ↓ read_pool.py --format pool-file
prefetch_reddit.py       → reddit-prefetch.sqlite3（一周原始帖子/评论，7 天 TTL）
  ↓ 尚未接线
run_lean.py <TICKER>     → 评级 + 理由 + 证据健康度
```

三段各自独立、各有 CLI 和 SQLite，可以按任意节奏分别运行。已知的接线缺口：
`run_lean.py` **尚未读取** Reddit 预抓取缓存；现场 RSS 路径也已于 2026-08-31 停用，
停用期间情绪分析师收到的是显式的来源不可用标记（不是空数据），并据此下调 confidence。

- 候选池：来源、打分、存储、读取、配置 → [候选股票池构建](docs/pool.md)
- Reddit 预抓取：CLI、缓存契约、限流 → [Reddit 预抓取接口](docs/reddit-prefetch.md)
- 单票研究：参数、实测数据、已知问题、未验证项 → [lean 运行手册](docs/lean-pipeline.md)
- macOS 状态栏应用、调度、本地 UI 和打包边界 → [macOS 应用设计与验收](docs/macos-app.md)

## macOS 应用默认调度

| 任务 | 默认 | 本地时间 | 说明 |
| --- | --- | --- | --- |
| 候选股票池 | 开 | 06:00，每天 | 不调用 LLM |
| Reddit 预抓取 | 开 | 06:30，每天 | 从最新 pool 快照读取 ticker；受持久限流约束 |
| Lean 分析师 | **关** | 07:00，工作日 | 可在 UI 启用；默认串行，避免未经确认的长时间 LLM 运行 |
| 本地报告发布 | 开 | 08:00，工作日 | 只生成本地 Markdown；不发手机 |

可在 Dashboard 修改任务开关、日程、运行日和所有受支持的 CLI 参数。
配置、运行账本、日志和应用数据位于 `~/Library/Application Support/StockWatch/`，
不向只读 `.app` 包内写数据。

重新构建：

```bash
scripts/build_macos_app.sh
scripts/build_macos_app.sh --install --migrate-local-data
```

---

## 目标流程（尚未打通）

```text
多来源股票池 → Python 确定性预筛 20–30 → research Agents → validated Research Verdict
→ Python 跨股票确定性排名 → Top10 → SQLite/outbox → 本地墙钟 08:00 投递
```

已实现头两段（股票池、预筛）和中间的 research Agents；通用本地调度器已由 macOS 宿主提供，但 **Research Verdict 结构化输出、
跨股票排名、Top10、outbox 和手机投递仍不存在**。研究管线目前产出的是给人读的文本，
不是给排名层消费的数据。

---

## 隐私与边界

`local-data/` 和 `.env` 被 Git 忽略。API key、真实通知 topic、持仓和 CSV 内容**永远不进入
被跟踪的文件**。研究与证据整理是范围；不下单、不连接自动交易执行、不承诺价格或收益预测。
数据缺失和来源失败必须可见，不能静默变成空成功或自动负分。

V1 的代码与文档已从仓库移除，只存在于 707d7de 及更早的 git 历史里；当初决定复用、改造还是替换的记录见[迁移矩阵](docs/migration.md)。

---

## 文档索引

已实现并实测：

- [候选股票池构建](docs/pool.md)
- [Reddit 预抓取接口](docs/reddit-prefetch.md)
- [lean 单票研究运行手册](docs/lean-pipeline.md)
- [macOS 状态栏应用](docs/macos-app.md)
- [测试目录](tests/README.md)

设计草案，尚未实现：

- [架构与组件边界](docs/architecture.md)
- [数据源与候选池契约](docs/data-sources.md)
- [Research Verdict 契约](docs/research-verdict.md)
- [确定性排名与 Top10](docs/ranking-top10.md)
- [报告、outbox 与 08:00 调度](docs/report-notify-schedule.md)
- [V1 到 V2 迁移矩阵（历史记录）](docs/migration.md)
- [分阶段验收门槛](docs/acceptance.md)
