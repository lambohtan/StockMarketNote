# 测试目录
状态：候选股票池与 Reddit 预抓取的单元测试已实现；其余产品线测试仍为 DRAFT
截至：2026-08-31

全部离线运行，不联网、不调用 LLM。

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_pool*.py'
.venv/bin/python -m unittest tests/test_reddit_prefetch.py
```

| 文件 | 覆盖 |
| --- | --- |
| `test_pool.py` | ticker 规范化、上市白名单解析、ETF/权证/优先股剔除、流动性下限、未知值不折零、跨来源打分与稳定破平、单来源失败标记 degraded、空但成功与失败的区分、白名单不可用时 fail-closed 不产出池子、CLI 退出码 |
| `test_pool_store.py` | 配置优先级与非法值、快照原子写入、全量存储与读时截断（改池子大小免重跑）、降级快照带标记成为最新、`--require-complete` 回退到最近完整快照、缺库返回 None 且不建库、陈旧判定、修剪不删 latest 指针、两个 CLI 的退出码 |
| `test_reddit_prefetch.py` | 股票池校验、Atom 解析、schema v2 原始快照、抓取阶段零筛选、读时最多 24 条、全原始帖子参与选择、7 天 TTL/清理、query hash 隔离、v1 legacy 回退、失败不覆盖成功、真只读 stale/miss、JSON 输出、断点复用与跨进程限流 |

真实网络、macOS 调度、手机投递和 Agent 接线不属于单元测试范围，必须单独授权后实测，
不能用静态测试冒充这些证据。

## 尚未覆盖（等对应功能实现）

Research Verdict schema 与 directive guard、跨股票确定性排名与 Top N/10 语义、
outbox 幂等、调度渲染。这些类别必须链接到[分阶段验收门槛](../docs/acceptance.md)，
并明确 verified / historical / draft / untested 状态。
