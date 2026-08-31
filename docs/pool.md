# 候选股票池构建（pool 阶段）
状态：来源解析、筛选、确定性排名、SQLite 池子存储与只读接口已实现并实测；定时任务与接入 Top10 产品线尚未实现
截至：2026-08-31

`build_pool.py` 是 V2 产品线的第一段：从公开热度来源挖出当天讨论度和交易活跃度最高的
美股，规范化后输出确定性排序的候选池。它不调用 LLM、不下单、不产生买卖指令。

```text
build_pool.py（更新，随时可跑）
  公开热度来源（4 个 fetch 单元）
    → Nasdaq 美股上市白名单（规范化 + 「只要美股」的硬判据）
    → Python 确定性筛选与打分
    → local-data/pool/pool.sqlite3 写入一个不可变快照，再移动 latest 指针

read_pool.py / load_latest_pool()（读取，随时可读、不联网）
    → 最新快照的前 N 只 → Reddit 预抓取、Agent 分析或任何其它应用
```

池子一直在：更新和读取完全解耦。更新失败不会动到已有池子，读取端也永远看不到写了一半的池子。

## 来源

| fetch 单元 | 打分来源 | 权重 | 提供什么 | 实测 |
| --- | --- | --- | --- | --- |
| `apewisdom:all-stocks` | 同名 | 0.35 | 全 Reddit 聚合提及数与 24h 前基线 | 2026-08-31 可用 |
| `apewisdom:wallstreetbets` | 同名 | 0.20 | 单区散户热度 | 2026-08-31 可用 |
| `stocktwits:trending` | 同名 | 0.25 | trending 排序、watchlist 数、instrument_class | 2026-08-31 可用 |
| `nasdaq:marketmovers` | `nasdaq:dollar-volume` | 0.15 | 成交额最活跃 | 2026-08-31 可用 |
| | `nasdaq:advanced` | 0.05 | 涨幅榜 | 2026-08-31 可用 |

Reddit `hot.json`（403）、Yahoo trending/screener（429）和 SEC `company_tickers.json`（403，
且需要真实 contact email UA）在同一次实测中不可用，未接入。个股级 Reddit 证据由
[Reddit 预抓取](reddit-prefetch.md) 单独负责，本阶段不重复抓取。

## 白名单与筛选

Nasdaq screener 按 nasdaq / nyse / amex 三次调用构成上市白名单（实测约 7100 行），它同时提供
`exchange`、`country`、价格和市值。筛选顺序与排除理由：

- `not_us_listed`：不在白名单里。ETF 不在 stocks screener 中，因此 SPY、QQQ、VOO、SOXL 等在此被剔除。
- `instrument_<kind>`：白名单名称判定为 warrant、right、unit、preferred、note、fund。合伙企业
  common units（如 ET）会按 `instrument_unit` 一并剔除。
- `below_price_floor` / `below_market_cap_floor`：默认 3 美元、3 亿美元，可用 `--min-price`、
  `--min-market-cap` 调整。价格或市值**未知时保留**，不把未知折算成零。

美国上市的 ADR（TSM、BABA 等）保留，`country` 字段在溯源里如实记录。

## 打分

确定性算术，没有模型参与：

```text
score = Σ_source  权重 × max(0, 1 − (rank − 1) / 深度)          深度：ape 100、stocktwits 30、nasdaq 10
      + 0.03 × (命中来源数 − 1)                                  跨来源印证
      + min(0.15, 0.06 × log2(24h 提及增长倍数))                  仅当倍数 > 1
```

排序键为 `(−score, −mentions, ticker)`，因此同分稳定按字母序，重复运行结果一致。24h 基线缺失时
动量记为未知（`None`），不记为零。

`--out` 导出的 JSON 可直接交给 `prefetch_reddit.py --pool-file`（已实测 dry-run 通过），
同名 `-provenance.json` 按[数据源契约](data-sources.md)保留 `ticker / market / exchange /
sources / observed_at / reasons / freshness / source_health / score` 以及全部排除记录；
数据库里存的是同一套字段。

## 池子存储

数据库：`local-data/pool/pool.sqlite3`（WAL，未跟踪，可用 `--db` 或 `POOL_DB_PATH` 改路径）。

| 表 | 内容 |
| --- | --- |
| `pool_runs` | 每次更新一行：run_id、as_of、分析日、写入时间、`status`（complete/degraded）、失败来源、候选总数、阈值与配置快照 |
| `pool_entries` | 该次更新的**全量**排名（实测约 120 只），每只带 score、sources、reasons、observed_at、freshness、source_health、mentions、动量、价格、市值 |
| `pool_sources` | 每个 fetch 单元的健康与返回量 |
| `pool_exclusions` | 被剔除的代码及原因 |
| `pool_meta` | `latest_run_id`、`latest_complete_run_id` 指针和 schema 版本 |

三条关键语义：

- **一次更新 = 一个事务**。整份快照写完才移动 `latest` 指针，读取端不会读到半个池子。
- **存全量、读时截断**。`top_n` 只在读取时生效，所以改池子大小对**已经跑好的**池子立即生效，不用重跑。
- **降级快照照样是最新池子，但带标记**。某个来源失败时快照仍写入并成为最新，`status=degraded` 且
  `degraded_sources` 列出失败来源；需要「四个来源全健康」的调用方传 `--require-complete`，
  会自动回退到最近一次完整快照。

旧快照按 `keep_runs`（默认 30）修剪，但 `latest` 和 `latest_complete` 指向的快照永不删除。

## 更新池子

```bash
# 标准更新：抓取 → 排名 → 写入数据库（读 config/pool.json）
.venv/bin/python build_pool.py

# 显式时间戳 + 机器可读输出
.venv/bin/python build_pool.py \
  --as-of 2026-08-31T14:00:00Z --analysis-date 2026-08-31 --json

# 临时改参数只影响这一次，不改配置文件
.venv/bin/python build_pool.py --top 30 --min-price 5

# 只跑不落库（体检用）；额外导出一份 JSON 文件
.venv/bin/python build_pool.py --no-store
.venv/bin/python build_pool.py --out /tmp/pool.json
```

退出码：`0` 全部来源应答；`1` 池子已写入但至少一个来源失败（降级）；`2` 上市白名单不可用或参数非法，
**不写数据库、不写文件**，上一份池子原样保留。

## 读取池子

命令行（只读，不联网，不会创建数据库）：

```bash
.venv/bin/python read_pool.py                      # 表格，前 top_n 只
.venv/bin/python read_pool.py --top 30             # 临时看更多
.venv/bin/python read_pool.py --all --format json  # 全量 + 完整溯源
.venv/bin/python read_pool.py --format tickers     # 一行一个代码，好管道
.venv/bin/python read_pool.py --require-complete   # 只要四源全健康的快照
```

退出码：`0` 有池子且新鲜；`1` 池子已返回但超过 `max_age_hours`（默认 36 小时）未更新——**列表照样输出**，
由调用方决定用不用；`2` 没有符合条件的池子（还没建过，或 `--require-complete` 但从没有过完整快照）。

交给 Reddit 预抓取：

```bash
.venv/bin/python read_pool.py --top 20 --format pool-file > today-pool.json
.venv/bin/python prefetch_reddit.py \
  --pool-file today-pool.json --analysis-date 2026-08-31
```

其它 Python 应用直接调用（同一份数据，无需经过文件）：

```python
import sys; sys.path.insert(0, "/path/to/StockMarketNote")   # 仓库根
from lean.pool_store import load_latest_pool

snapshot = load_latest_pool(limit=20)          # require_complete=True 只要完整快照
if snapshot is None:
    ...                                        # 没有池子；这不等于「今天没有值得看的股票」
elif snapshot.is_stale(36):
    ...                                        # 池子太旧，先跑一次 build_pool.py
else:
    tickers = snapshot.tickers()               # ['NVDA', 'MU', ...]
    snapshot.status, snapshot.degraded_sources # 'degraded' 时说明哪个来源失败
    snapshot.entries[0].reasons                # 该股入池的逐来源可审计理由
```

`load_latest_pool()` 永不联网、永不写库，数据库不存在时返回 `None` 而不是创建空库。

## 配置

配置文件：`config/pool.json`（可跟踪，无密钥）。优先级 **CLI 参数 > 环境变量 > 配置文件 > 内置默认**。

| 字段 | 环境变量 | 默认 | 含义 |
| --- | --- | --- | --- |
| `top_n` | `POOL_TOP_N` | 20 | 池子大小（读取时截断，改完立刻生效） |
| `min_price` | `POOL_MIN_PRICE` | 3.0 | 最低股价，未知价格不剔除 |
| `min_market_cap` | `POOL_MIN_MARKET_CAP` | 3e8 | 最低市值，未知市值不剔除 |
| `keep_runs` | `POOL_KEEP_RUNS` | 30 | 数据库保留最近几次快照 |
| `max_age_hours` | `POOL_MAX_AGE_HOURS` | 36 | 超过多久算陈旧（读取端退出码 1） |
| `db_path` | `POOL_DB_PATH` | `local-data/pool/pool.sqlite3` | 数据库路径 |
| `weights` | — | 见上文打分 | 各来源权重，可只覆盖其中几个 |

改池子大小的三种方式，效果立刻生效、不需要重跑更新：

```bash
# 1. 永久改：编辑 config/pool.json 的 "top_n"
# 2. 单次改：
.venv/bin/python read_pool.py --top 50
# 3. 给某个调用方改：
POOL_TOP_N=10 .venv/bin/python read_pool.py --format tickers
```

配置文件里的未知字段、非法数值和未知权重来源都会直接报错退出（码 2），不会被静默忽略。
`--config /path/to/other.json` 或 `POOL_CONFIG_PATH` 可换整份配置。

## 以后接定时任务

调度器尚未接入，本次不创建 launchd plist 或 crontab 条目。将来接的时候，更新命令就是上面那条
`build_pool.py`：它不依赖 cwd（路径全部由 `__file__` 解析），失败时保留旧池子，退出码 0/1/2 已足够让
调度器区分「正常 / 降级 / 未更新」。分析任务侧不需要跟更新对齐时间——随时 `read_pool.py` 取当前池子即可。

## Fail-closed 语义

- 上市白名单任一交易所抓取失败 → 抛 `ListingUnavailableError`，不产出池子、不写数据库：无法核验
  「是美股」就不给池子，上一份池子保持不变。
- 热度来源失败 → 该 fetch 单元记 `failed`，`complete=false`，退出码 1，失败来源名写进产出文件；
  下游必须按部分输入处理，不能把抓取失败读成「没人讨论」。
- 来源正常响应但确实没有匹配行 → `empty`，属于成功，`complete` 不变。

## 边界

freshness 只在 Nasdaq movers 有来源声明时间（按 ET −04:00 解析）时给 `declared_intraday` /
`declared_stale`；ApeWisdom 和 StockTwits 不返回观察时间，一律记 `fetch_time_only`，不冒充精度。
热度不是质量：本阶段只回答「今天在被讨论和交易」，不含任何买卖含义。
