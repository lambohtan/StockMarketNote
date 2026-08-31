# Reddit 预抓取接口

状态：CLI、缓存、筛选和只读接口已实现；Trading Agent 接线与真实定时任务尚未实现

## 边界

`prefetch_reddit.py` 是股票池阶段与 Trading Agent 之间的独立数据阶段。它只访问
Reddit RSS、保存一周原始帖子/评论样本并写本地 SQLite；不调用筛选器、不调用 LLM、
不运行 Trading Agent、
不安装 launchd，也不发送通知。

```text
当天股票池
  → prefetch_reddit.py（一周原始 RSS、顺序请求、跨进程限流）
  → local-data/tradingagents/reddit-prefetch.sqlite3（7 天 TTL）
  → load_cached_reddit()（只读、读时 Python 筛选最多 24 条、绝不联网）
  → 未来接入 Sentiment Agent
```

## 股票池输入

定时任务可以传 JSON：

```json
{
  "tickers": [
    "AMD",
    {"ticker": "NVDA", "aliases": ["Nvidia"]},
    "MSFT"
  ]
}
```

也接受逐行/逗号分隔文本，或直接把 ticker 写在命令行。重复 ticker 会稳定去重，
别名会合并；非法路径字符会在联网前拒绝。

## 调用

先用 dry-run 校验输入和估算最坏时间：

```bash
.venv/bin/python prefetch_reddit.py \
  --pool-file local-data/today-pool.json \
  --analysis-date 2026-08-31 \
  --as-of 2026-08-31T14:00:00Z \
  --dry-run
```

真实预抓取：

```bash
.venv/bin/python prefetch_reddit.py \
  --pool-file local-data/today-pool.json \
  --analysis-date 2026-08-31 \
  --as-of 2026-08-31T14:00:00Z \
  --json-summary
```

默认每只股票：

- 顺序搜索 `wallstreetbets`、`stocks`、`investing`（组合搜索 RSS 实测会返回假空）；
- 搜索时间窗口是一周（168 小时）；
- 保存三个 RSS 搜索返回的全部去重帖子，不再限制 10 篇；
- 每篇一次 RSS 请求最多 20 条评论（Reddit 实际可能返回更少）；
- 抓取阶段不删 bot、不去近似重复、不评分、不精选，`comments_selected` 始终是 0；
- 同一帖子在一周内被多个 ticker 命中时复用原始评论缓存；
- 请求间隔默认 30 秒，并进一步服从 `Retry-After` / `X-Ratelimit-Reset`。

这里的“全部”是 **RSS 返回的全部**，不是 Reddit 全站全量。每个 subreddit 搜索 RSS 最多请求
100 篇；如果返回数碰到 100，快照会记录 `search_limit_reached=true`，不声称数据未截断。

退出码：`0` 为无抓取降级完成，`1` 为部分成功/失败，`2` 为输入/配置错误。单只股票
失败不会停止后面的股票；搜索失败不会用空结果覆盖它之前的成功快照。

一天的业务键由 `--analysis-date` 显式指定，`--as-of` 则是 UTC 证据截止时间；两者分开可避免
洛杉矶晚间运行跨过 UTC 日界时写错日期。重复运行时请传入完全相同的两个值。程序会跳过
已经完成的 ticker，并复用帖子缓存，
所以进程中断后可以从未完成的 ticker 继续；`--force` 才会强制重抓。当前恢复粒度是 ticker，
不是单个 subreddit 请求。

请不要并发启动两个相同股票池任务。请求间隔在跨进程锁下是安全的，但当前没有整个任务级互斥锁，
并发任务可能做重复工作。

## 读取时 Python 筛选

筛选器只在 `load_cached_reddit()` 被调用时运行，不会改写 SQLite 原始快照，也不调用 LLM。
它会删除 removed/deleted、常见 bot、纯链接/过短文本，执行精确
及 token-Jaccard 近似去重，限制同一作者最多两条，并按 ticker/别名、财务词、数字、
因果表达和 RSS 原始顺序评分。最后按帖子轮询选择，避免单一帖子占满 24 条。

RSS 不提供点赞或总评论数。缓存中的 `comments_seen` 是本次 RSS 读到的样本数，不能解释
为帖子的总评论数或社区参与度。

## 缓存与读取

默认缓存位于：

```text
local-data/tradingagents/reddit-prefetch.sqlite3
```

`local-data/` 已被 Git 忽略。schema v2 的 `raw_snapshots` 保存一周原始帖子和评论样本，
`thread_cache` 用于跨 ticker 复用。默认每次运行删除 7 天前的原始快照和帖子缓存。
网络失败只记录在运行状态中；不会覆盖同日、同查询配置已有的成功原始快照。
旧 schema v1 保留不删，loader 会明确标成 `legacy`，不会冒充完整原始数据。

未来 Sentiment Agent 使用：

```python
from lean.reddit_prefetch import load_cached_reddit

evidence = load_cached_reddit(
    "NVDA",
    "2026-08-31",
    max_age_hours=168,
    window_hours=168,
    max_comments=24,
)
prompt_block = evidence.text
```

这个函数只读 SQLite，每次从原始数据重新选择最多 24 条；不足 24 时返回实际数量并标记
`limited`。缓存缺失返回明确的 `REDDIT CACHE MISS`，缓存过期返回 `STALE`，两种情况都不会
现场请求 Reddit。

未来接入 Agent 时，必须把帖子和评论当作不可信的外部证据，在 prompt 中明确禁止其中的指令改写
Agent 任务或系统规则。本轮没有做 Agent 接线。

## 当前未完成

- 尚未把 `load_cached_reddit()` 接入 `lean/sentiment.py` 或上游完整版情绪分析师；
- 尚未创建或加载真实 macOS LaunchAgent；
- 长任务中断后可跳过相同 `--as-of` 下已完成的 ticker，但尚未恢复到 ticker 内的精确请求游标；
- 尚未完成真实多 ticker、跨睡眠/唤醒的定时验收。
