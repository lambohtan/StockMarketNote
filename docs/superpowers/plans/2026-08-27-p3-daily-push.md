# P3 每日推送链路 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每天早上 8:00 收到一条手机推送，告诉你持仓里哪几只出现了无法用大盘和行业解释的异动、原因是什么，以及有没有需要注意的恶化信号。

**Architecture:** 三个 launchd 任务通过 SQLite 的 `outbox` 表解耦 —— compute 在开盘前算完只入队不发送，notify 到点了统一 drain 队列。这样硬崩溃能被独立的看门狗任务发现，改推送时间不影响计算时间。所有数值计算是确定性 Python，LLM 只在最后把找到的材料压成一句人话。

**Tech Stack:** Python 3.13 · pandas 3.0 · numpy 2.5 · yfinance 1.7 · edgartools · SQLite (stdlib) · launchd · ntfy · Claude Code CLI (`claude -p`) 或 Anthropic API

**Spec:** [docs/superpowers/specs/2026-08-27-daily-push-and-menubar-design.md](../specs/2026-08-27-daily-push-and-menubar-design.md)

**不在本计划范围内:** P4 的完整评分卡（关注度/趋势/基本面/组合契合四维打分）、P5 对冲配比、菜单栏应用与 FastAPI 面板。本计划的日报里「观察池」只做**增量事件**（Task 11）：Reddit 排名跃升与 Form 4 内部人买入，两者的数据 P1 已在每天入库。13F cluster 检测属于 P4，但本计划会把 13F-HR 加进抓取范围让数据先攒起来 —— 与 Reddit 同理，早一天开始攒就早一天能回看。

## Global Constraints

以下约束来自 `CLAUDE.md` 和设计文档，**每个任务都隐含包含本节**：

- **不要求用户做任何事。** 判别标准：这句话在描述世界，还是在指挥用户？描述可以，指挥不行。禁止祈使句和建议（「建议买入/卖出」「你应该」「赶紧」「该减仓」「止损设在」「目标价」「买入价」），但「卖」这个字本身不禁 —— 「内部人集中卖出」是合法输出。
- **所有数值计算用确定性 Python，LLM 只做摘要和翻译。** 报告里每个数字都要能追溯到具体计算。
- **写入即快照，只追加不覆盖。** 不得 DELETE 或 UPDATE 历史数据行。
- **推送正文里不出现金额** —— 无 `$` 数字、无成本、无仓位占比、无账户总市值。ntfy.sh 是公共服务器。
- **数据源失败不中断整个任务**，记入 `source_health` 表。
- **评分模型必须标注「未经回测验证」。**
- 注释和文档用中文，代码标识符用英文。
- Python 3.13 / pandas 3.0，避免依赖 pandas 边缘特性。
- SQLite 用 stdlib `sqlite3`，不引 ORM。
- 数据源一律走 `sw/sources/` 适配器，实现 `SourceResult` 接口并上报健康状态。
- 测试用 `python3 tests/test_xxx.py` 直接跑（项目现有惯例，不引 pytest），失败时 `sys.exit(1)`。
- **凡是一眼看不出对错的统计量，必须有已知解析解的合成数据做锚**（见 `tests/test_analysis.py` 确立的规矩）。

**关于提交:** 本项目尚未 `git init`（用户明确表示暂缓）。各任务末尾的「提交」步骤暂时替换为「跑全量测试确认绿」。若中途初始化了 git，把这些步骤换回 `git add` + `git commit`。

**工作目录:** 所有命令在 `股票市场/stockwatch/` 下执行，除非另有说明。测试用 `STOCKWATCH_DB` 指向临时库，**绝不污染 `data/stockwatch.db` 的真实快照历史**。

---

## File Structure

| 文件 | 职责 | 任务 |
|---|---|---|
| `sw/market_time.py` | ET 交易日历、判断收盘、剔除未收盘的半根 K 线 | 1 |
| `sw/outbox.py` | 推送队列的读写与幂等 | 2 |
| `sw/notify.py` | ntfy 发送 + drain 队列 + 缺报告时的失败上报 | 3 |
| `sw/analysis/attribution.py` | 双因子回归归因，算残差 σ 和 z 值 | 4 |
| `sw/analysis/causes.py` | 按固定顺序找异动原因，找不到就承认 | 5 |
| `sw/alerts.py` | 三级恶化扫描 + 六段格式 + 指令性措辞守卫 | 6 |
| `sw/llm.py` | LLM adapter，`claude_cli` / `api` 双路 | 7 |
| `sw/daily_report.py` | 日报 Markdown + 推送正文（两套，后者无金额） | 8 |
| `run_daily.py` | 每日计算主流程，只入队不发送 | 9 |
| `run_notify.py` | 推送出口，drain outbox | 10 |
| `sw/schedule.py` | 渲染并重载 plist，被 CLI 和以后的设置窗口共用 | 10 |
| `packaging/*.plist` | 三个 launchd 任务定义 | 10 |
| `sw/analysis/watchlist.py` | 观察池增量：Reddit 跃升 + Form 4 申报集中 | 11 |
| `sw/store.py` | 加 `outbox` 表（修改） | 2 |
| `run_ingest.py` | 剔除半根 K 线（1）、13F-HR 加进抓取范围（11） | 1, 11 |

---

### Task 1: 交易日历与半根 K 线剔除

整条链路的地基。归因回归喂进去的每一根 bar 都要先过这一关。

**Files:**
- Create: `sw/market_time.py`
- Test: `tests/test_market_time.py`

**Interfaces:**
- Consumes: 无（纯函数，只依赖 stdlib）
- Produces:
  - `ET: ZoneInfo` — 美东时区常量
  - `now_et() -> datetime`
  - `et_today() -> date`
  - `is_trading_day(d: date) -> bool`
  - `session_closed(d: date, now: datetime | None = None) -> bool`
  - `drop_incomplete_bars(rows: list[tuple], now: datetime | None = None) -> tuple[list[tuple], list[str]]`
    rows 形如 `(d, ticker, close, volume)`，返回 `(保留的行, 被剔除的日期列表)`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_market_time.py`：

```python
#!/usr/bin/env python3
"""交易日历与半根 K 线剔除的测试。运行：python3 tests/test_market_time.py

为什么这个模块值得单独测：把未收盘的半根 K 线当成一整天喂进 60 日回归，
会污染 β 和残差 σ，而且是**静默的** —— 不报错，只让结论悄悄变歪。
这类 bug 靠肉眼永远发现不了，只能靠测试挡住。
"""
import sys
from datetime import datetime, date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from zoneinfo import ZoneInfo
from sw import market_time as MT

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_trading_day():
    print("\n交易日判定")
    check("2026-08-27 周四是交易日", MT.is_trading_day(date(2026, 8, 27)), True)
    check("2026-08-29 周六不是", MT.is_trading_day(date(2026, 8, 29)), False)
    check("2026-08-30 周日不是", MT.is_trading_day(date(2026, 8, 30)), False)


def test_session_closed():
    print("\n收盘判定（ET 16:00 为界）")
    et = ZoneInfo("America/New_York")
    # 当天 09:00 ET —— 还没开盘，今天这根 bar 不完整
    now = datetime(2026, 8, 27, 9, 0, tzinfo=et)
    check("当天 09:00 ET，当天未收盘", MT.session_closed(date(2026, 8, 27), now), False)
    # 当天 15:59 ET —— 盘中，仍未收盘
    now = datetime(2026, 8, 27, 15, 59, tzinfo=et)
    check("当天 15:59 ET，当天未收盘", MT.session_closed(date(2026, 8, 27), now), False)
    # 当天 16:00 ET —— 收盘
    now = datetime(2026, 8, 27, 16, 0, tzinfo=et)
    check("当天 16:00 ET，当天已收盘", MT.session_closed(date(2026, 8, 27), now), True)
    # 过去的日子永远算收盘
    check("昨天永远算已收盘", MT.session_closed(date(2026, 8, 26), now), True)


def test_drop_incomplete_bars():
    print("\n半根 K 线剔除")
    et = ZoneInfo("America/New_York")
    rows = [
        ("2026-08-25", "AAPL", 300.0, 1e6),
        ("2026-08-26", "AAPL", 305.0, 1e6),
        ("2026-08-27", "AAPL", 310.0, 3e5),   # ← 今天，盘中的半根
    ]
    # 本机 06:00 PT = 09:00 ET，开盘前，今天这根必须被剔除
    now = datetime(2026, 8, 27, 9, 0, tzinfo=et)
    kept, dropped = MT.drop_incomplete_bars(rows, now)
    check("剔除后剩 2 行", len(kept), 2)
    check("被剔除的是今天", dropped, ["2026-08-27"])
    check("保留的最后一天是 08-26", kept[-1][0], "2026-08-26")

    # 收盘后跑，今天这根是完整的，必须保留
    now = datetime(2026, 8, 27, 16, 30, tzinfo=et)
    kept, dropped = MT.drop_incomplete_bars(rows, now)
    check("收盘后 3 行全留", len(kept), 3)
    check("收盘后无剔除", dropped, [])

    # 空输入不能炸
    check("空输入返回空", MT.drop_incomplete_bars([], now), ([], []))


if __name__ == "__main__":
    test_trading_day()
    test_session_closed()
    test_drop_incomplete_bars()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_market_time.py
```

预期：`ModuleNotFoundError: No module named 'sw.market_time'`

- [ ] **Step 3: 写最小实现**

创建 `sw/market_time.py`：

```python
"""
美股交易日历与时间判定。

存在的理由：计算任务跑在本机 06:00（美股本机时区 06:30 开盘），
理论上不会碰到当日 bar。但盘前交易在某些情况下会让 yfinance 返回当日的半根 K 线，
而且以后谁把计算时间改到盘中，这里就是唯一的防线。

把半天当一天喂进 60 日回归会污染 β 和残差 σ，**且是静默的**。
"""
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)          # 常规时段收盘，ET


def now_et():
    return datetime.now(ET)


def et_today():
    return now_et().date()


def is_trading_day(d):
    """周一到周五。⚠️ 不含美股假日表 —— 假日当天没有 bar，
    下游按「没有数据」处理即可，不需要在这里判。"""
    return d.weekday() < 5


def session_closed(d, now=None):
    """d 这个交易日是否已经收盘。过去的日子恒为 True。"""
    now = now or now_et()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now = now.astimezone(ET)
    if d < now.date():
        return True
    if d > now.date():
        return False
    return now.time() >= MARKET_CLOSE


def drop_incomplete_bars(rows, now=None):
    """
    rows: [(d, ticker, close, volume), ...]，d 是 ISO 日期字符串。
    剔除所有「日期对应的交易日尚未收盘」的行。
    返回 (保留的行, 被剔除的日期升序列表)。
    """
    now = now or now_et()
    kept, dropped = [], set()
    for r in rows:
        d = date.fromisoformat(r[0])
        if session_closed(d, now):
            kept.append(r)
        else:
            dropped.add(r[0])
    return kept, sorted(dropped)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_market_time.py
```

预期：`✅ 全部通过`

- [ ] **Step 5: 接进价格入库路径**

修改 `run_ingest.py`，在写 `prices` 表之前过一道剔除。找到这一段：

```python
    res = P.fetch_prices(sorted(universe), period=CFG.get("history.price_period", "2y"))
    print(f"  {'✅' if res.ok else '❌'} 价格 {res.rows} 行 ({res.latency_ms}ms) {res.detail}")
    if res.ok and not a.dry_run:
        st.upsert_many("prices", ["d","ticker","close","volume"], res.data)
```

改成：

```python
    res = P.fetch_prices(sorted(universe), period=CFG.get("history.price_period", "2y"))
    print(f"  {'✅' if res.ok else '❌'} 价格 {res.rows} 行 ({res.latency_ms}ms) {res.detail}")
    if res.ok:
        from sw.market_time import drop_incomplete_bars
        res.data, dropped = drop_incomplete_bars(res.data)
        if dropped:
            print(f"  ⚠️ 剔除未收盘的当日 bar：{dropped}（避免污染回归）")
    if res.ok and not a.dry_run:
        st.upsert_many("prices", ["d","ticker","close","volume"], res.data)
```

- [ ] **Step 6: 跑全量测试确认绿**

```bash
python3 tests/test_market_time.py && python3 tests/test_analysis.py
```

预期：两个都 `✅ 全部通过`

---

### Task 2: outbox 推送队列

「算」和「发」之间的唯一接口。做完这个任务，计算任务就有地方放结果了。

**Files:**
- Modify: `sw/store.py`（在 `SCHEMA` 字符串里加建表语句）
- Create: `sw/outbox.py`
- Test: `tests/test_outbox.py`

**Interfaces:**
- Consumes: `sw.store.Store`（已有：`.q(sql, args)`、`.tx()`、`.conn`）
- Produces:
  - `enqueue(store, kind, title, body, priority="default") -> int` 返回新条目 id
  - `unsent(store) -> list[dict]` 按 created_at 升序
  - `mark_sent(store, entry_id) -> None`
  - `mark_failed(store, entry_id, err) -> None` attempts +1，记 last_error
  - `has_kind_on(store, kind, d) -> bool` 某天是否已入队过某类条目
  - 合法的 kind：`"daily" | "weekly" | "l1" | "failure"`
  - 合法的 priority：`"low" | "default" | "high" | "urgent"`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_outbox.py`：

```python
#!/usr/bin/env python3
"""outbox 队列测试。运行：python3 tests/test_outbox.py

重点测幂等 —— Mac 睡过头后 launchd 会补跑任务且不保证顺序，
drain 被调用两次是常态，不能因此把同一条推送发两遍。
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw import outbox as OB

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def test_enqueue_and_unsent():
    print("\n入队与读取")
    st = fresh_store()
    check("空队列", OB.unsent(st), [])
    i1 = OB.enqueue(st, "daily", "标题A", "正文A")
    i2 = OB.enqueue(st, "l1", "标题B", "正文B", priority="urgent")
    rows = OB.unsent(st)
    check("两条未发送", len(rows), 2)
    check("按入队顺序", [r["id"] for r in rows], [i1, i2])
    check("priority 存对了", rows[1]["priority"], "urgent")
    st.close()


def test_idempotent_send():
    print("\n幂等：标记已发后不再出现在未发送队列")
    st = fresh_store()
    i1 = OB.enqueue(st, "daily", "T", "B")
    OB.mark_sent(st, i1)
    check("标记后队列为空", OB.unsent(st), [])
    OB.mark_sent(st, i1)          # 重复标记不能炸
    check("重复标记仍为空", OB.unsent(st), [])
    st.close()


def test_mark_failed_keeps_in_queue():
    print("\n失败的条目留在队列里等重试")
    st = fresh_store()
    i1 = OB.enqueue(st, "daily", "T", "B")
    OB.mark_failed(st, i1, "网络超时")
    rows = OB.unsent(st)
    check("仍在未发送队列", len(rows), 1)
    check("attempts 加到 1", rows[0]["attempts"], 1)
    check("记下错误原因", rows[0]["last_error"], "网络超时")
    OB.mark_failed(st, i1, "又超时")
    check("attempts 加到 2", OB.unsent(st)[0]["attempts"], 2)
    st.close()


def test_has_kind_on():
    print("\n某天是否已有某类条目（失败检测要用）")
    st = fresh_store()
    check("还没有 daily", OB.has_kind_on(st, "daily", "2026-08-27"), False)
    OB.enqueue(st, "daily", "T", "B", created_at="2026-08-27T06:05:00")
    check("有了 daily", OB.has_kind_on(st, "daily", "2026-08-27"), True)
    check("换一天没有", OB.has_kind_on(st, "daily", "2026-08-28"), False)
    check("换个 kind 没有", OB.has_kind_on(st, "weekly", "2026-08-27"), False)
    # 已发送的也算「今天有过」—— 否则重复入队失败通知
    i = OB.unsent(st)[0]["id"]
    OB.mark_sent(st, i)
    check("已发送的仍算今天有过", OB.has_kind_on(st, "daily", "2026-08-27"), True)
    st.close()


if __name__ == "__main__":
    test_enqueue_and_unsent()
    test_idempotent_send()
    test_mark_failed_keeps_in_queue()
    test_has_kind_on()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_outbox.py
```

预期：`ModuleNotFoundError: No module named 'sw.outbox'`

- [ ] **Step 3: 加建表语句**

修改 `sw/store.py`，在 `SCHEMA` 字符串里、`CREATE TABLE IF NOT EXISTS source_health` 那段**之后**、`CREATE INDEX` 那几行**之前**，插入：

```sql
-- 推送队列：「算」和「发」之间的唯一接口。
-- compute 任务只入队不发送；notify 任务到点了统一 drain。
-- 拆开的好处见设计文档 §2.1 —— 硬崩溃能被独立任务发现，try/except 做不到。
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT,
  kind TEXT,           -- daily | weekly | l1 | failure
  priority TEXT,       -- low | default | high | urgent
  title TEXT,
  body TEXT,
  sent_at TEXT,        -- NULL = 未发送
  attempts INTEGER DEFAULT 0,
  last_error TEXT
);
```

并在 `CREATE INDEX` 那一组里加一行：

```sql
CREATE INDEX IF NOT EXISTS ix_outbox_unsent ON outbox(sent_at, created_at);
```

同时把 `outbox` 加进 `Store.stats()` 的表名列表里：

```python
        for t in ["positions", "prices", "meta", "reddit_rank",
                  "edgar_filings", "signals", "alerts", "reports",
                  "outbox", "source_health"]:
```

- [ ] **Step 4: 写 outbox 模块**

创建 `sw/outbox.py`：

```python
"""
推送队列。

「算」和「发」之间的唯一接口：compute 任务只入队，notify 任务只出队。
这样即使计算任务硬崩溃（OOM / 被 kill / 段错误），
独立的 notify 任务照样能发现「今天该有的条目没有」并上报 ——
try/except 抓不到这类失败。
"""
from datetime import datetime

KINDS = ("daily", "weekly", "l1", "failure")
PRIORITIES = ("low", "default", "high", "urgent")


def enqueue(store, kind, title, body, priority="default", created_at=None):
    """入队一条待发送的推送，返回新条目 id。"""
    if kind not in KINDS:
        raise ValueError(f"未知 kind: {kind}，合法值 {KINDS}")
    if priority not in PRIORITIES:
        raise ValueError(f"未知 priority: {priority}，合法值 {PRIORITIES}")
    ts = created_at or datetime.now().isoformat(timespec="seconds")
    with store.tx() as c:
        cur = c.execute(
            "INSERT INTO outbox (created_at,kind,priority,title,body,attempts) "
            "VALUES (?,?,?,?,?,0)", (ts, kind, priority, title, body))
        return cur.lastrowid


def unsent(store):
    """所有未发送的条目，按入队时间升序。"""
    return store.q("SELECT * FROM outbox WHERE sent_at IS NULL "
                   "ORDER BY created_at, id")


def mark_sent(store, entry_id):
    """标记已发送。重复调用是安全的 —— 补跑竞态下这会真实发生。"""
    with store.tx() as c:
        c.execute("UPDATE outbox SET sent_at=? WHERE id=? AND sent_at IS NULL",
                  (datetime.now().isoformat(timespec="seconds"), entry_id))


def mark_failed(store, entry_id, err):
    """发送失败：attempts +1，记下原因，条目留在队列里等下次重试。"""
    with store.tx() as c:
        c.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                  (str(err)[:500], entry_id))


def has_kind_on(store, kind, d):
    """某天是否入队过某类条目（发没发都算）。失败检测靠这个判断。"""
    r = store.conn.execute(
        "SELECT 1 FROM outbox WHERE kind=? AND substr(created_at,1,10)=? LIMIT 1",
        (kind, d)).fetchone()
    return r is not None
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 tests/test_outbox.py
```

预期：`✅ 全部通过`

- [ ] **Step 6: 确认真实库能平滑升级**

`CREATE TABLE IF NOT EXISTS` 会在已有库上自动补表，不影响历史数据。验证：

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from sw.store import Store; from sw.config import CFG
s = Store(CFG.db_path)
print({k: v for k, v in s.stats().items()})
assert s.stats()['outbox'] == 0
assert s.stats()['prices'] > 0, '历史数据不能丢'
print('✅ 真实库升级正常，历史数据完好')"
```

---

### Task 3: ntfy 发送与队列 drain

做完这个任务，端到端的推送就通了 —— 手动往 outbox 塞一条就能收到手机通知。

**Files:**
- Create: `sw/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `sw.outbox`（Task 2 的 `unsent` / `mark_sent` / `mark_failed` / `has_kind_on` / `enqueue`）、`sw.config.CFG`（已有 `.ntfy_url` / `.ntfy_topic` 属性）
- Produces:
  - `send(cfg, title, body, priority="default", tags=None, dry_run=False) -> tuple[bool, str]`
  - `drain(store, cfg, dry_run=False, today=None) -> dict`
    返回 `{"sent": int, "failed": int, "failure_reported": bool}`
  - `MONEY_RE` — 检测金额的正则，供 Task 8 复用
  - `assert_no_money(text) -> None` 正文含金额时抛 `ValueError`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_notify.py`：

```python
#!/usr/bin/env python3
"""推送与 drain 的测试。运行：python3 tests/test_notify.py

⚠️ 全部用 dry_run，绝不真发。

三个重点：
  1. 金额守卫 —— ntfy.sh 是公共服务器，正文里不能出现账户金额
  2. 失败检测 —— 今天没有 daily 条目要主动上报，这是硬崩溃的唯一防线
  3. 补跑竞态 —— notify 先于 compute 执行时不能误报失败
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw import outbox as OB
from sw import notify as NT

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)

def check_raises(name, fn, exc=ValueError):
    try:
        fn()
    except exc:
        print(f"  ✅ {name}: 正确抛出 {exc.__name__}")
        return
    except Exception as e:
        print(f"  ❌ {name}: 抛了 {type(e).__name__}，期望 {exc.__name__}")
    else:
        print(f"  ❌ {name}: 没抛异常")
    FAIL.append(name)


class FakeCfg:
    ntfy_url = "https://ntfy.sh/test-topic"
    ntfy_topic = "test-topic"
    def get(self, k, d=None):
        return d


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def test_money_guard():
    print("\n金额守卫（ntfy.sh 是公共服务器）")
    check_raises("市值被拦下", lambda: NT.assert_no_money("持仓市值 $9,130"))
    check_raises("小数金额被拦下", lambda: NT.assert_no_money("成本 $440.51"))
    check_raises("带空格的被拦下", lambda: NT.assert_no_money("浮盈 $ 916"))
    # 百分比和 σ 值必须放行 —— 这才是推送的主要内容
    NT.assert_no_money("NVDA -8.7%，个股独立 -9.1%（2.4σ）")
    NT.assert_no_money("科技敞口 65.8%，有效独立赌注 2.15")
    print("  ✅ 百分比与 σ 值正常放行")


def test_drain_sends_and_marks():
    print("\ndrain 发送并标记")
    st, cfg = fresh_store(), FakeCfg()
    OB.enqueue(st, "daily", "标题", "正文 -8.7%", created_at="2026-08-27T06:05:00")
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("发出 1 条", r["sent"], 1)
    check("队列清空", OB.unsent(st), [])
    # 再 drain 一次不能重发
    r2 = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("重复 drain 不重发", r2["sent"], 0)
    st.close()


def test_failure_reported_when_no_daily():
    print("\n失败检测：今天没有 daily 条目就要上报")
    st, cfg = fresh_store(), FakeCfg()
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("上报了失败", r["failure_reported"], True)
    check("失败通知被发出", r["sent"], 1)
    kinds = [x["kind"] for x in st.q("SELECT kind FROM outbox")]
    check("入队的是 failure", kinds, ["failure"])
    # 同一天再 drain 不能重复上报
    r2 = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("同一天不重复上报", r2["failure_reported"], False)
    st.close()


def test_no_false_failure_when_daily_exists():
    print("\n补跑竞态：有 daily 条目时绝不误报失败")
    st, cfg = fresh_store(), FakeCfg()
    OB.enqueue(st, "daily", "T", "B -1.2%", created_at="2026-08-27T06:05:00")
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("没有误报", r["failure_reported"], False)
    # 已发送过的 daily 也不该触发失败上报
    r2 = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("已发送后仍不误报", r2["failure_reported"], False)
    st.close()


def test_money_in_queue_is_rejected_not_sent():
    print("\n队列里混进金额时：拒发并记错，不能静默发出去")
    st, cfg = fresh_store(), FakeCfg()
    i = OB.enqueue(st, "daily", "T", "你的持仓市值 $9,130",
                   created_at="2026-08-27T06:05:00")
    r = NT.drain(st, cfg, dry_run=True, today="2026-08-27")
    check("没有发出", r["sent"], 0)
    check("计为失败", r["failed"], 1)
    rows = OB.unsent(st)
    check("仍在队列里", len(rows), 1)
    check("记下了原因", "金额" in (rows[0]["last_error"] or ""), True)
    st.close()


if __name__ == "__main__":
    test_money_guard()
    test_drain_sends_and_marks()
    test_failure_reported_when_no_daily()
    test_no_false_failure_when_daily_exists()
    test_money_in_queue_is_rejected_not_sent()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_notify.py
```

预期：`ModuleNotFoundError: No module named 'sw.notify'`

- [ ] **Step 3: 写实现**

创建 `sw/notify.py`：

```python
"""
ntfy 推送 + outbox drain。

⚠️ ntfy.sh 是公共服务器，topic 名是随机串（隐蔽性，不是加密）。
任何知道 topic 的人都能读到推送内容，所以正文里**绝不出现账户金额**。
知道用户在看 NVDA 是一回事，知道账户里有多少钱是另一回事。
"""
import re
import requests

from . import outbox as OB
from .market_time import et_today

# 匹配 $ 后面跟数字（允许中间有空格、逗号、小数点）
MONEY_RE = re.compile(r"\$\s*\d[\d,]*(\.\d+)?")

TAGS = {
    "daily": "chart_with_upwards_trend",
    "weekly": "calendar",
    "l1": "rotating_light",
    "failure": "x",
}


def assert_no_money(text):
    """推送正文的金额守卫。命中就抛，绝不静默发出去。"""
    m = MONEY_RE.search(text or "")
    if m:
        raise ValueError(
            f"推送正文里出现金额 {m.group(0)!r} —— ntfy.sh 是公共服务器，"
            f"账户金额不能出现在推送里（详情放本地面板）")


def send(cfg, title, body, priority="default", tags=None, dry_run=False):
    """发一条 ntfy。返回 (ok, detail)。"""
    url = cfg.ntfy_url
    if not url:
        return False, "config.yaml 的 notify.ntfy_topic 是空的"
    assert_no_money(body)
    assert_no_money(title)
    if dry_run:
        print(f"[dry-run] → {url}\n  [{priority}] {title}\n  {body[:300]}")
        return True, "dry-run"
    try:
        headers = {
            "Title": (title or "StockWatch").encode("utf-8"),
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = tags
        r = requests.post(url, data=(body or "").encode("utf-8"),
                          headers=headers, timeout=20)
        if r.ok:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def drain(store, cfg, dry_run=False, today=None):
    """
    把队列里所有未发送的条目发出去。

    发送前先做失败检测：今天如果**从来没有**入队过 daily 条目，
    说明计算任务没跑成（可能是硬崩溃，try/except 抓不到），
    入队一条 failure 通知。用户不会每天开 app，
    所以「没有消息」绝不能等于「没事」。
    """
    today = today or et_today().isoformat()
    failure_reported = False

    if not OB.has_kind_on(store, "daily", today):
        last = store.q("SELECT substr(created_at,1,10) d FROM outbox "
                       "WHERE kind='daily' ORDER BY created_at DESC LIMIT 1")
        last_ok = last[0]["d"] if last else "从未成功过"
        OB.enqueue(store, "failure",
                   f"StockWatch {today} 没跑成",
                   f"今天的计算任务没有产出日报。\n"
                   f"最后一次成功：{last_ok}\n"
                   f"排查：查看 logs/ 目录，或在菜单栏里点「立即运行一次」。",
                   priority="high")
        failure_reported = True

    sent = failed = 0
    for row in OB.unsent(store):
        try:
            ok, detail = send(cfg, row["title"], row["body"],
                              priority=row["priority"] or "default",
                              tags=TAGS.get(row["kind"]),
                              dry_run=dry_run)
        except ValueError as e:
            # 金额守卫命中：计为失败留在队列里，绝不静默发出去
            ok, detail = False, str(e)
        if ok:
            OB.mark_sent(store, row["id"])
            sent += 1
        else:
            OB.mark_failed(store, row["id"], detail)
            failed += 1
        store.log_health("ntfy", ok, 0, f"{row['kind']}: {detail}")

    return {"sent": sent, "failed": failed, "failure_reported": failure_reported}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_notify.py
```

预期：`✅ 全部通过`

- [ ] **Step 5: 端到端真发一条**

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from sw.config import CFG
from sw import notify as NT
ok, d = NT.send(CFG, 'StockWatch Task 3 自测',
                'outbox 链路已打通。这条是真发的，手机应该收到。', priority='default')
print(('✅' if ok else '❌'), d)"
```

预期：手机上收到通知。收不到就先检查 ntfy app 是否订阅了 `stockwatch-xk41vohfuult2z0nk69t`。

- [ ] **Step 6: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

预期：全部 `✅ 全部通过`

---

### Task 4: 归因引擎

P3 的核心。回答「这只票今天动了 5%，是大盘的事、行业的事，还是它自己的事」。

**Files:**
- Create: `sw/analysis/attribution.py`
- Test: `tests/test_attribution.py`

**Interfaces:**
- Consumes: `sw.store.Store.price_history`、`sw.analysis.risk.price_panel`（Task 无关，已存在）
- Produces:
  - `ols2(y, x1, x2) -> dict` 返回 `{"alpha","b1","b2","resid","r2","n"}`
  - `attribute(store, ticker, sector_etf, market_etf="SPY", window=60, start=None) -> dict | None`
    返回 `{"ticker","d","ret","mkt_part","sector_part","idio","sigma","z","beta_mkt","beta_sector","r2","n"}`
    数据不足返回 `None`
  - `classify(z) -> str` 返回 `"normal" | "anomaly" | "extreme"`（2σ / 4σ 为界）
  - `ANOMALY_Z = 2.0`、`EXTREME_Z = 4.0`、`MIN_OBS = 40`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_attribution.py`：

```python
#!/usr/bin/env python3
"""归因引擎的校准测试。运行：python3 tests/test_attribution.py

沿用 tests/test_analysis.py 确立的规矩：
凡是一眼看不出对错的统计量，必须有已知解析解的合成数据做锚。

这里构造「已知 β、已知残差」的序列，验证引擎能把它们还原出来，
并且能在注入冲击时检出 2σ / 4σ。
"""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from sw.analysis import attribution as AT

FAIL = []

def check(name, got, want, tol):
    ok = got is not None and abs(got - want) <= tol
    print(f"  {'✅' if ok else '❌'} {name}: 得到 "
          f"{'None' if got is None else round(got, 4)}，期望 {round(want, 4)} ±{tol}")
    if not ok:
        FAIL.append(name)

def check_eq(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_ols2_recovers_known_betas():
    """构造 y = 0.0004 + 1.30·mkt + 0.75·sector + 噪声，看能不能还原。"""
    print("\n双因子回归还原已知 β")
    rng = np.random.default_rng(11)
    n = 400
    mkt = rng.normal(0, 0.010, n)
    sec = rng.normal(0, 0.008, n)
    noise = rng.normal(0, 0.004, n)
    y = 0.0004 + 1.30 * mkt + 0.75 * sec + noise

    r = AT.ols2(y, mkt, sec)
    check("β_市场（构造 1.30）", r["b1"], 1.30, 0.05)
    check("β_行业（构造 0.75）", r["b2"], 0.75, 0.06)
    check("α（构造 0.0004）", r["alpha"], 0.0004, 0.0005)
    check("残差标准差（构造 0.004）", float(np.std(r["resid"], ddof=1)), 0.004, 0.0005)


def test_detects_injected_shock():
    """最后一天注入一个 3σ 的个股冲击，必须被检出为 anomaly。"""
    print("\n注入冲击的检出")
    rng = np.random.default_rng(7)
    n = 200
    sigma = 0.004
    mkt = rng.normal(0, 0.010, n)
    sec = rng.normal(0, 0.008, n)
    y = 1.20 * mkt + 0.60 * sec + rng.normal(0, sigma, n)
    y[-1] += 3.0 * sigma                      # ← 3σ 的个股独立冲击

    r = AT.ols2(y, mkt, sec)
    z = r["resid"][-1] / np.std(r["resid"][:-1], ddof=1)
    check("最后一天的 z 值约为 3", float(z), 3.0, 0.6)
    check_eq("被分类为 anomaly", AT.classify(float(z)), "anomaly")


def test_classify_thresholds():
    print("\n阈值分类")
    check_eq("1.5σ 是常规波动", AT.classify(1.5), "normal")
    check_eq("-1.5σ 也是常规", AT.classify(-1.5), "normal")
    check_eq("2.5σ 是异动", AT.classify(2.5), "anomaly")
    check_eq("-2.5σ 也是异动", AT.classify(-2.5), "anomaly")
    check_eq("4.5σ 是极端", AT.classify(4.5), "extreme")
    check_eq("-4.5σ 也是极端", AT.classify(-4.5), "extreme")


def test_decomposition_adds_up():
    """收益必须能拆成三块且加得回去 —— 报告里每个数字都要可追溯。"""
    print("\n收益分解的自洽性")
    rng = np.random.default_rng(3)
    n = 150
    mkt = rng.normal(0, 0.010, n)
    sec = rng.normal(0, 0.008, n)
    y = 0.0002 + 1.10 * mkt + 0.90 * sec + rng.normal(0, 0.003, n)
    r = AT.ols2(y, mkt, sec)
    # 最后一天：ret ≈ alpha + b1·mkt + b2·sec + resid
    recon = r["alpha"] + r["b1"] * mkt[-1] + r["b2"] * sec[-1] + r["resid"][-1]
    check("分解后能还原当日收益", float(recon), float(y[-1]), 1e-9)


def test_insufficient_data_returns_none():
    print("\n数据不足时诚实返回 None")
    rng = np.random.default_rng(1)
    short = rng.normal(0, 0.01, 5)
    r = AT.ols2(short, short, short)
    check_eq("样本太少返回 None", r, None)


if __name__ == "__main__":
    test_ols2_recovers_known_betas()
    test_detects_injected_shock()
    test_classify_thresholds()
    test_decomposition_adds_up()
    test_insufficient_data_returns_none()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_attribution.py
```

预期：`ImportError: cannot import name 'attribution'`

- [ ] **Step 3: 写实现**

创建 `sw/analysis/attribution.py`：

```python
"""
归因引擎 —— 本系统最有价值的部分。

要回答的问题：我这只票今天动了 5%，是大盘的事、行业的事，还是它自己的事？
人工查这个每只票要花 15–20 分钟，而且经常查不到。

    个股当日收益 = α + β_市场 × SPY收益 + β_行业 × 行业ETF收益 + 残差

残差在正常范围 → 报告里**不提**。日报一半的价值来自它不说什么。
残差 > 2σ → 标为异动，触发找原因（见 causes.py）
残差 > 4σ → 升级为 L1

⚠️ 未经回测验证。这是描述工具，不预测涨跌。
"""
import numpy as np

ANOMALY_Z = 2.0
EXTREME_Z = 4.0
MIN_OBS = 40          # 少于这个样本量不给结论，宁可不说


def ols2(y, x1, x2):
    """
    二元 OLS：y = α + b1·x1 + b2·x2 + ε

    返回 {"alpha","b1","b2","resid","r2","n"}；样本不足返回 None。
    用 lstsq 而不是手写正规方程 —— 前者在因子高度共线时数值更稳
    （SPY 和 XLK 的相关性经常在 0.9 以上，这不是假设，是常态）。
    """
    y = np.asarray(y, float)
    x1 = np.asarray(x1, float)
    x2 = np.asarray(x2, float)
    n = min(len(y), len(x1), len(x2))
    if n < MIN_OBS:
        return None
    y, x1, x2 = y[-n:], x1[-n:], x2[-n:]
    X = np.column_stack([np.ones(n), x1, x2])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1 - np.sum(resid ** 2) / ss_tot) if ss_tot > 0 else None
    return {"alpha": float(coef[0]), "b1": float(coef[1]), "b2": float(coef[2]),
            "resid": resid, "r2": r2, "n": n}


def classify(z):
    """按 |z| 分三档。注意用绝对值 —— 暴涨和暴跌都值得解释。"""
    if z is None:
        return "normal"
    a = abs(z)
    if a >= EXTREME_Z:
        return "extreme"
    if a >= ANOMALY_Z:
        return "anomaly"
    return "normal"


def _returns(store, ticker, start):
    rows = store.q("SELECT d, close FROM prices WHERE ticker=? AND d>=? ORDER BY d",
                   (ticker, start))
    ds = [r["d"] for r in rows]
    px = np.array([float(r["close"]) for r in rows], float)
    if len(px) < 2:
        return [], np.zeros(0)
    return ds[1:], px[1:] / px[:-1] - 1.0


def attribute(store, ticker, sector_etf, market_etf="SPY", window=60, start=None):
    """
    对单只票做归因。返回最后一个交易日的拆解结果，数据不足返回 None。

    sector_etf 为 None 时（行业未知、或是 ETF 本身）退化为单因子，
    b2 记 0 —— 不要为了凑双因子硬塞一个不相关的代理。
    """
    if start is None:
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=int(window * 2.2) + 60)).isoformat()

    d_t, r_t = _returns(store, ticker, start)
    d_m, r_m = _returns(store, market_etf, start)
    if len(r_t) == 0 or len(r_m) == 0:
        return None

    if sector_etf:
        d_s, r_s = _returns(store, sector_etf, start)
    else:
        d_s, r_s = d_m, np.zeros(len(r_m))

    # 只保留三者都有报价的交易日，避免个别停牌日制造假跳空
    common = sorted(set(d_t) & set(d_m) & set(d_s))
    if len(common) < MIN_OBS:
        return None
    common = common[-window:]
    mt, mm, ms = dict(zip(d_t, r_t)), dict(zip(d_m, r_m)), dict(zip(d_s, r_s))
    y = np.array([mt[d] for d in common], float)
    x1 = np.array([mm[d] for d in common], float)
    x2 = np.array([ms[d] for d in common], float)

    fit = ols2(y, x1, x2)
    if fit is None:
        return None

    resid = fit["resid"]
    # ⚠️ σ 要用「除当日以外」的残差算 —— 否则今天这个大冲击会把 σ 自己抬高，
    # 把 4σ 事件压成 2σ，越是极端的事件越检不出来。
    sigma = float(np.std(resid[:-1], ddof=1)) if len(resid) > 2 else None
    z = (float(resid[-1]) / sigma) if sigma else None

    return {
        "ticker": ticker,
        "d": common[-1],
        "ret": float(y[-1]),
        "mkt_part": float(fit["b1"] * x1[-1]),
        "sector_part": float(fit["b2"] * x2[-1]),
        "idio": float(fit["alpha"] + resid[-1]),
        "sigma": sigma,
        "z": z,
        "level": classify(z),
        "beta_mkt": fit["b1"],
        "beta_sector": fit["b2"],
        "r2": fit["r2"],
        "n": fit["n"],
        "sector_etf": sector_etf,
        "market_etf": market_etf,
    }

```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_attribution.py
```

预期：`✅ 全部通过`

- [ ] **Step 5: 用真实数据跑一遍看合不合理**

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from sw.store import Store; from sw.config import CFG
from sw.analysis import portfolio as PF, attribution as AT
st = Store(CFG.db_path); p = PF.build(st, CFG)
smap = (CFG.get('benchmarks.sectors') or {})
rs = []
for h in p.holdings:
    r = AT.attribute(st, h.ticker, smap.get(h.sector) if h.sector else None)
    if r:
        r['sector'] = h.sector
        rs.append(r)
rs.sort(key=lambda r: -abs(r['z'] or 0))
print(f\"{'代码':<6}{'当日':>8}{'大盘':>8}{'行业':>8}{'个股':>8}{'z':>7}  {'R²':>5}  级别\")
for r in rs[:12]:
    print(f\"{r['ticker']:<6}{r['ret']*100:>7.2f}%{r['mkt_part']*100:>7.2f}%\"
          f\"{r['sector_part']*100:>7.2f}%{r['idio']*100:>7.2f}%{r['z'] or 0:>7.2f}\"
          f\"  {r['r2'] or 0:>5.2f}  {r['level']}\")
print(f'\n共 {len(rs)} 只归因成功，其中非 normal 的 {sum(1 for r in rs if r[\"level\"]!=\"normal\")} 只')"
```

预期：三块加起来接近当日收益；大部分票是 `normal`（这是对的 —— 异动本来就该少）。
若**所有**票都是 anomaly，说明 σ 算错了，回头检查 Step 3 里「除当日外算 σ」那段。

- [ ] **Step 6: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

---

### Task 5: 找原因

异动检出之后回答「为什么」。**找不到就承认找不到，绝不编故事。**

**Files:**
- Create: `sw/analysis/causes.py`
- Test: `tests/test_causes.py`

**Interfaces:**
- Consumes: `edgar_filings` 表（已有，`run_ingest.py` 每天写入）、`sw.sources.prices.next_earnings`（已有）、yfinance 新闻与分析师评级
- Produces:
  - `L1_ITEMS: dict[str, str]` 8-K item 编号 → 中文含义
  - `find_causes(store, ticker, d, peers=None, max_news=3) -> list[dict]`
    每条 `{"source","summary","url","item","is_l1"}`，按可靠性降序；找不到返回 `[]`
  - `peer_readthrough(attributions, ticker, sector) -> dict | None`
    返回 `{"n_peers","n_moving","median_z"}`，用于区分「行业性事件」和「公司自己的事」

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_causes.py`：

```python
#!/usr/bin/env python3
"""找原因的测试。运行：python3 tests/test_causes.py

核心断言是**顺序**和**诚实**：
  - 8-K 必须排在新闻前面（官方申报比媒体转述可靠）
  - 什么都没找到时返回空列表，绝不编一个看似合理的理由
"""
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.analysis import causes as CS

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Store(tmp.name)


def seed_filing(st, ticker, d, form, items, acc):
    st.upsert_many("edgar_filings",
                   ["accession", "filed_at", "ticker", "cik", "form",
                    "items", "url", "raw_json", "seen_at"],
                   [(acc, d, ticker, "0000000", form, items,
                     f"https://sec.gov/{acc}",
                     json.dumps({"company": ticker}), d)])


def test_finds_8k_and_marks_l1():
    print("\n8-K Item 5.02 应被识别为 L1")
    st = fresh_store()
    seed_filing(st, "WXYZ", "2026-08-27", "8-K", "5.02", "acc-1")
    got = CS.find_causes(st, "WXYZ", "2026-08-27", peers=None, max_news=0)
    check("找到 1 条", len(got), 1)
    check("来源是 8-K", got[0]["source"], "8-K")
    check("标为 L1", got[0]["is_l1"], True)
    check("item 记对", got[0]["item"], "5.02")
    check("含中文释义", "高管" in got[0]["summary"], True)
    st.close()


def test_non_l1_item_not_marked():
    print("\n普通 8-K item 不该被标成 L1")
    st = fresh_store()
    seed_filing(st, "ABCD", "2026-08-27", "8-K", "7.01", "acc-2")
    got = CS.find_causes(st, "ABCD", "2026-08-27", peers=None, max_news=0)
    check("找到 1 条", len(got), 1)
    check("不是 L1", got[0]["is_l1"], False)
    st.close()


def test_honest_empty_when_nothing_found():
    print("\n什么都没找到时必须返回空，不许编")
    st = fresh_store()
    got = CS.find_causes(st, "NOPE", "2026-08-27", peers=None, max_news=0)
    check("返回空列表", got, [])
    st.close()


def test_only_looks_at_recent_window():
    print("\n只看异动日前后的窗口，不能把上个月的 8-K 当今天的原因")
    st = fresh_store()
    seed_filing(st, "ABCD", "2026-07-01", "8-K", "5.02", "acc-old")
    got = CS.find_causes(st, "ABCD", "2026-08-27", peers=None, max_news=0)
    check("一个月前的不算", got, [])
    st.close()


def test_peer_readthrough_distinguishes_sector_event():
    print("\n同行读数：区分行业性事件和公司自己的事")
    attrs = [
        {"ticker": "A", "sector": "Technology", "z": 2.4},
        {"ticker": "B", "sector": "Technology", "z": 2.1},
        {"ticker": "C", "sector": "Technology", "z": 2.6},
        {"ticker": "D", "sector": "Energy", "z": 0.2},
    ]
    r = CS.peer_readthrough(attrs, "A", "Technology")
    check("同行数（不含自己）", r["n_peers"], 2)
    check("同样在动的同行数", r["n_moving"], 2)
    # 全行业都在动 → 更像行业性事件
    check("判定为行业性", r["looks_sector_wide"], True)

    attrs2 = [
        {"ticker": "A", "sector": "Technology", "z": 3.0},
        {"ticker": "B", "sector": "Technology", "z": 0.1},
        {"ticker": "C", "sector": "Technology", "z": -0.3},
    ]
    r2 = CS.peer_readthrough(attrs2, "A", "Technology")
    check("只有自己在动 → 不是行业性", r2["looks_sector_wide"], False)


if __name__ == "__main__":
    test_finds_8k_and_marks_l1()
    test_non_l1_item_not_marked()
    test_honest_empty_when_nothing_found()
    test_only_looks_at_recent_window()
    test_peer_readthrough_distinguishes_sector_event()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_causes.py
```

预期：`ImportError: cannot import name 'causes'`

- [ ] **Step 3: 写实现**

创建 `sw/analysis/causes.py`：

```python
"""
找异动原因。

顺序是按**可靠性**排的，不是按方便程度：
  1. SEC 8-K       —— 4 个工作日内必须申报，按 item 编号分类，最硬的信源
  2. 财报日历      —— 是不是刚出财报
  3. 新闻          —— 媒体转述，可能失真
  4. 分析师调整    —— 滞后，且常常是跟随价格而非引导价格
  5. 同行读数      —— 区分「行业性事件」和「公司自己的事」
  6. 都找不到      —— 老实写「未找到明确原因」

⚠️ 第 6 条是硬性要求。编一个看似合理的理由比承认不知道有害得多 ——
用户会拿它当决策依据。
"""
import json
from datetime import date, timedelta

# 触发 L1 的 8-K item。来源：02-系统设计.md
L1_ITEMS = {
    "4.02": "前期财报不可信（会计问题，最严重的信号之一）",
    "4.01": "更换审计师",
    "1.03": "破产 / 接管",
    "2.06": "重大资产减值",
    "5.02": "董事或高管变动（CEO/CFO 离职尤其重要）",
    "1.05": "重大网络安全事件",
}

ITEM_MEANING = dict(L1_ITEMS, **{
    "2.02": "披露季度业绩",
    "7.01": "公司自愿披露（Regulation FD）",
    "8.01": "其他事件",
    "5.07": "股东投票结果",
    "1.01": "签订重大协议",
    "2.01": "完成收购或资产处置",
})

FILING_WINDOW_DAYS = 4      # 异动日前后几天内的申报才算相关


def _window(d, back=FILING_WINDOW_DAYS):
    dd = date.fromisoformat(d)
    return (dd - timedelta(days=back)).isoformat(), (dd + timedelta(days=1)).isoformat()


def find_causes(store, ticker, d, peers=None, max_news=3):
    """返回按可靠性降序的原因列表。找不到返回 []。"""
    out = []
    lo, hi = _window(d)

    # ---------- 1. 8-K ----------
    rows = store.q(
        "SELECT * FROM edgar_filings WHERE ticker=? AND form='8-K' "
        "AND filed_at>=? AND filed_at<=? ORDER BY filed_at DESC",
        (ticker, lo, hi))
    for r in rows:
        items = [i.strip() for i in (r["items"] or "").split(",") if i.strip()]
        is_l1 = any(i in L1_ITEMS for i in items)
        desc = "；".join(ITEM_MEANING.get(i, f"Item {i}") for i in items) or "未标注 item"
        out.append({
            "source": "8-K",
            "summary": f"{r['filed_at']} 提交 8-K：{desc}",
            "url": r["url"] or "",
            "item": items[0] if items else None,
            "is_l1": is_l1,
        })

    # ---------- 2. 财报日历 ----------
    try:
        from ..sources.prices import next_earnings
        ed = next_earnings(ticker)
        if ed and str(ed)[:10] >= lo and str(ed)[:10] <= hi:
            out.append({"source": "财报", "summary": f"财报日 {str(ed)[:10]}",
                        "url": "", "item": None, "is_l1": False})
    except Exception:
        pass        # 数据源失败不中断，符合全局约束

    # ---------- 3. 新闻 ----------
    if max_news > 0:
        try:
            import warnings; warnings.filterwarnings("ignore")
            import yfinance as yf
            for n in (yf.Ticker(ticker).news or [])[:max_news]:
                c = n.get("content") or n
                title = c.get("title") or ""
                if not title:
                    continue
                link = ((c.get("canonicalUrl") or {}).get("url")
                        if isinstance(c.get("canonicalUrl"), dict) else c.get("link")) or ""
                out.append({"source": "新闻", "summary": title,
                            "url": link, "item": None, "is_l1": False})
        except Exception:
            pass

    # ---------- 4. 同行读数 ----------
    if peers:
        pr = peers
        if pr and pr.get("n_peers", 0) > 0:
            if pr["looks_sector_wide"]:
                out.append({
                    "source": "同行读数",
                    "summary": f"同行业另有 {pr['n_moving']}/{pr['n_peers']} 只同样出现异动"
                               f"，更像行业性事件而非公司自身的事",
                    "url": "", "item": None, "is_l1": False})
            else:
                out.append({
                    "source": "同行读数",
                    "summary": f"同行业另外 {pr['n_peers']} 只均无异动，"
                               f"这次波动集中在该公司自身",
                    "url": "", "item": None, "is_l1": False})
    return out


def peer_readthrough(attributions, ticker, sector, z_thresh=2.0):
    """
    同行是不是也在动。
    attributions: [{"ticker","sector","z"}, ...]（整个组合的归因结果）
    """
    if not sector:
        return None
    peers = [a for a in attributions
             if a.get("sector") == sector and a.get("ticker") != ticker]
    if not peers:
        return {"n_peers": 0, "n_moving": 0, "looks_sector_wide": False}
    moving = [a for a in peers if abs(a.get("z") or 0) >= z_thresh]
    return {
        "n_peers": len(peers),
        "n_moving": len(moving),
        # 过半同行同时异动 → 更像行业性事件
        "looks_sector_wide": len(moving) * 2 >= len(peers) and len(moving) > 0,
    }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_causes.py
```

预期：`✅ 全部通过`

- [ ] **Step 5: 用真实 EDGAR 数据抽查**

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from sw.store import Store; from sw.config import CFG
from sw.analysis import causes as CS
st = Store(CFG.db_path)
rows = st.q(\"SELECT DISTINCT ticker, filed_at FROM edgar_filings WHERE form='8-K' AND ticker<>'' ORDER BY filed_at DESC LIMIT 5\")
for r in rows:
    got = CS.find_causes(st, r['ticker'], r['filed_at'], max_news=0)
    print(f\"{r['ticker']:<6} {r['filed_at']}  →  {len(got)} 条\")
    for g in got:
        print(f\"    [{g['source']}]{' ⚠️L1' if g['is_l1'] else ''} {g['summary'][:70]}\")"
```

预期：能对上真实的 8-K，item 编号被翻译成中文。

- [ ] **Step 6: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

---

### Task 6: 三级恶化扫描与六段格式

产品约束落地的地方。这个任务的测试比实现更重要。

**Files:**
- Create: `sw/alerts.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `sw.analysis.causes.L1_ITEMS`、Task 4 的归因结果、`sw.analysis.portfolio.Holding`
- Produces:
  - `BANNED_PHRASES: list[str]`
  - `assert_no_directives(text) -> None` 含指令性措辞时抛 `ValueError`
  - `scan(store, portfolio, attributions, causes_by_ticker) -> list[dict]`
    每条 `{"ticker","level","category","facts","data","base_rate","counterpoint","position","next_steps"}`
  - `render_alert(alert, holding) -> str` 六段格式的 Markdown
  - `render_alert_push(alert) -> tuple[str, str]` 推送版（无金额）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_alerts.py`：

```python
#!/usr/bin/env python3
"""恶化提醒的测试。运行：python3 tests/test_alerts.py

⭐ 这个文件是产品约束的执行者，不是风格检查。

CLAUDE.md：「不要求用户做任何事。系统给方向，不下指令。」
判别标准是**这句话在描述世界，还是在指挥用户** —— 描述可以，指挥不行。

所以正样本比负样本更重要：只测「禁了什么」很容易滑向一个什么都不敢说的系统，
那就失去价值了。ALLOWED 里那几句必须能通过。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import alerts as AL

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def test_banned_directives():
    print("\n负样本：指令性措辞必须被拦下")
    BANNED_SAMPLES = [
        "建议买入 NVDA",
        "建议卖出该持仓",
        "你应该减少科技股敞口",
        "赶紧处理这个仓位",
        "该减仓了",
        "止损设在 $180",
        "目标价 $250",
        "务必在财报前调整",
    ]
    for s in BANNED_SAMPLES:
        try:
            AL.assert_no_directives(s)
            print(f"  ❌ 漏网：{s!r}")
            FAIL.append(f"漏网 {s}")
        except ValueError:
            print(f"  ✅ 拦下：{s!r}")


def test_allowed_factual_statements():
    print("\n正样本：客观陈述必须放行（比负样本更重要）")
    ALLOWED_SAMPLES = [
        "内部人集中卖出，排除 10b5-1 预设计划后仍有 3 笔",
        "该公司 CFO 于 08/27 离职，未披露继任安排",
        "这类信号历史上后续 6 个月出现财务重述的比例高于基准",
        "加入后你的组合年化波动率从 56.7% 变为 61.2%",
        "股价当日 -8.7%，其中个股独立部分 -9.1%（4.1σ）",
        "接下来看什么：① 继任公告 ② Q3 财报是否延期 ③ 审计师是否变动",
        "公司同日重申了 Q3 指引",
    ]
    for s in ALLOWED_SAMPLES:
        try:
            AL.assert_no_directives(s)
            print(f"  ✅ 放行：{s[:40]}…")
        except ValueError as e:
            print(f"  ❌ 误伤：{s!r} —— {e}")
            FAIL.append(f"误伤 {s}")


def test_six_section_format():
    print("\n六段格式")
    alert = {
        "ticker": "WXYZ", "level": "L1", "category": "8-K",
        "facts": "8-K Item 5.02：CFO 于 08/27 离职，即刻生效，未披露继任安排",
        "data": "股价当日 -8.7%（个股独立部分 -9.1%，4.1σ）",
        "base_rate": "CFO 无预告离职且无继任安排，历史上后续 6 个月出现财务重述"
                     "或业绩不及预期的比例明显高于基准；但相当一部分最终证明是个人原因",
        "counterpoint": "公司同日重申了 Q3 指引；离职生效日与财报窗口无重叠",
        "position": "占卫星仓 4.9%",
        "next_steps": ["继任公告的时间和人选背景", "Q3 财报是否延期", "审计师是否变动"],
    }
    md = AL.render_alert(alert, holding=None)
    for seg in ["发生了什么", "数据", "这类信号通常", "反面观点", "你的持仓", "接下来看什么"]:
        check(f"包含「{seg}」", seg in md, True)
    AL.assert_no_directives(md)
    print("  ✅ 整段通过指令性措辞检查")


def test_push_version_has_no_money():
    print("\n推送版不能出现金额")
    from sw.notify import assert_no_money
    alert = {
        "ticker": "WXYZ", "level": "L1", "category": "8-K",
        "facts": "8-K Item 5.02：CFO 离职",
        "data": "当日 -8.7%（个股独立 -9.1%，4.1σ）",
        "base_rate": "历史上后续 6 个月重述比例高于基准",
        "counterpoint": "公司同日重申 Q3 指引",
        "position": "成本 $558.87，市值 $683.94，占卫星仓 4.9%",   # ← 故意塞金额
        "next_steps": ["继任公告", "Q3 财报是否延期"],
    }
    title, body = AL.render_alert_push(alert)
    assert_no_money(body)      # 抛异常就说明推送版没过滤掉金额
    assert_no_money(title)
    check("标题带级别", "L1" in title, True)
    check("标题带代码", "WXYZ" in title, True)
    print("  ✅ 推送版已剥离金额")


def test_level_from_8k_item():
    print("\n8-K item → 级别")
    check("4.02 是 L1", AL.level_for_item("4.02"), "L1")
    check("5.02 是 L1", AL.level_for_item("5.02"), "L1")
    check("7.01 不是 L1", AL.level_for_item("7.01"), None)


if __name__ == "__main__":
    test_banned_directives()
    test_allowed_factual_statements()
    test_six_section_format()
    test_push_version_has_no_money()
    test_level_from_8k_item()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_alerts.py
```

预期：`ModuleNotFoundError: No module named 'sw.alerts'`

- [ ] **Step 3: 写实现**

创建 `sw/alerts.py`：

```python
"""
三级恶化提醒。

CLAUDE.md 的硬性约束：**不要求用户做任何事。系统给方向，不下指令。**
判别标准 —— 这句话在描述世界，还是在指挥用户？描述可以，指挥不行。
「卖」这个字本身不禁：「内部人集中卖出」是客观事实，是合法输出。

六段格式最后一段「接下来看什么」是整个设计的关键：
它把用户从「要不要卖」这个二选一，转成「再收集三个信息」。
"""
import re

from .analysis.causes import L1_ITEMS

# 指令性措辞。禁的是句式，不是词汇。
BANNED_PHRASES = [
    "建议买入", "建议卖出", "建议持有", "建议减", "建议加",
    "你应该", "你需要", "你必须", "请立即", "赶紧", "务必",
    "该减仓", "该清仓", "该买", "该卖出",
    "止损设在", "止损位", "目标价", "买入价", "卖出价", "建议价",
]

_MONEY = re.compile(r"\$\s*\d[\d,]*(\.\d+)?")


def assert_no_directives(text):
    """指令性措辞守卫。运行时和测试都用它 —— LLM 输出也要过这一关。"""
    t = text or ""
    for p in BANNED_PHRASES:
        if p in t:
            raise ValueError(
                f"输出里出现指令性措辞 {p!r}。"
                f"系统给方向，不下指令 —— 改写成客观陈述（描述世界，而不是指挥用户）")


def level_for_item(item):
    """8-K item 编号 → 级别。不在 L1 名单里的返回 None。"""
    return "L1" if item in L1_ITEMS else None


def scan(store, portfolio, attributions, causes_by_ticker):
    """
    生成提醒列表。

    L1：8-K 命中 L1 item，或残差 ≥ 4σ 的单日下跌
    L2：残差 ≥ 2σ（进日报，不单独推送）
    L3：本期不实现（属于周报范畴）
    """
    out = []
    by_ticker = {a["ticker"]: a for a in attributions}

    for tk, causes in (causes_by_ticker or {}).items():
        attr = by_ticker.get(tk)
        l1_causes = [c for c in causes if c.get("is_l1")]
        z = (attr or {}).get("z")

        level = None
        if l1_causes:
            level = "L1"
        elif z is not None and z <= -4.0:
            level = "L1"          # 只有大跌升 L1；大涨用不着打断用户
        elif attr and attr.get("level") in ("anomaly", "extreme"):
            level = "L2"
        if not level:
            continue

        h = portfolio.get(tk) if portfolio else None
        w = None
        if portfolio and h:
            ws = portfolio.weights()
            w = ws.get(tk)

        facts = "；".join(c["summary"] for c in (l1_causes or causes)[:3]) \
                or "未找到明确原因"
        data = ""
        if attr:
            data = (f"当日 {attr['ret']*100:+.1f}%"
                    f"（大盘 {attr['mkt_part']*100:+.1f}%，"
                    f"行业 {attr['sector_part']*100:+.1f}%，"
                    f"个股独立 {attr['idio']*100:+.1f}%，{attr['z']:+.1f}σ）")

        out.append({
            "ticker": tk,
            "level": level,
            "category": (l1_causes[0]["source"] if l1_causes else "价格异动"),
            "facts": facts,
            "data": data,
            "base_rate": _base_rate(l1_causes),
            "counterpoint": _counterpoint(causes),
            "position": (f"占组合 {w*100:.1f}%" if w is not None else "—"),
            "next_steps": _next_steps(l1_causes, tk),
        })
    return out


def _base_rate(l1_causes):
    """「这类信号通常意味着什么」。只写有据可依的，没有就说没有。"""
    if not l1_causes:
        return ("个股独立部分超出常规波动范围。单日残差本身不预示方向 —— "
                "它只说明市场认为发生了无法用大盘和行业解释的事。")
    item = l1_causes[0].get("item")
    table = {
        "5.02": "高管无预告离职且无继任安排，历史上后续 6 个月出现财务重述或"
                "业绩不及预期的比例高于基准；但相当一部分最终证明是个人原因。",
        "4.02": "公司自己声明前期财报不可信，是会计问题中最严重的一类信号。",
        "4.01": "更换审计师，非自愿更换的信息含量高于自愿更换。",
        "1.03": "破产或接管程序启动。",
        "2.06": "重大资产减值，通常意味着此前的收购或投资未达预期。",
        "1.05": "重大网络安全事件，影响范围往往在数周后才明朗。",
    }
    return table.get(item, "该类申报的历史基准率本系统尚未收录，"
                           "以下判断请以原始申报文件为准。")


def _counterpoint(causes):
    """反面观点。找不到就诚实说明，不硬凑。"""
    news = [c for c in causes if c["source"] == "新闻"]
    peer = [c for c in causes if c["source"] == "同行读数"]
    parts = []
    if peer:
        parts.append(peer[0]["summary"])
    if news:
        parts.append(f"另有报道：{news[0]['summary']}")
    return "；".join(parts) or "本次未检索到明确的反面材料 —— 这不代表不存在，只代表没找到。"


def _next_steps(l1_causes, ticker):
    """把「要不要卖」转成「再收集三个信息」。"""
    item = l1_causes[0].get("item") if l1_causes else None
    table = {
        "5.02": ["继任公告的时间和人选背景", "下一季财报是否延期", "审计师是否随后变动"],
        "4.02": ["重述涉及的具体科目和期间", "审计师是否出具保留意见", "是否触发退市审核"],
        "4.01": ["新审计师的规模与行业经验", "前任审计师的离任函内容", "是否伴随管理层变动"],
    }
    return table.get(item, [
        f"{ticker} 是否有尚未公开的申报（关注未来 4 个工作日的 8-K）",
        "同行业其他公司同期的读数",
        "下一次财报的日期与市场一致预期",
    ])


def render_alert(alert, holding=None):
    """六段格式的完整 Markdown（面板和报告用，可含金额）。"""
    steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(alert["next_steps"], 1))
    pos = alert.get("position", "—")
    if holding is not None and holding.cost_basis is not None:
        pos = (f"{pos}；成本 ${holding.cost_basis:,.2f}，"
               f"市值 ${holding.market_value:,.2f}")
    md = (
        f"### 【{alert['level']}】{alert['ticker']} · {alert['category']}\n\n"
        f"**发生了什么**　{alert['facts']}\n\n"
        f"**数据**　{alert['data']}\n\n"
        f"**这类信号通常意味着什么**　{alert['base_rate']}\n\n"
        f"**反面观点**　{alert['counterpoint']}\n\n"
        f"**你的持仓现状**　{pos}\n\n"
        f"**接下来看什么**\n{steps}\n"
    )
    assert_no_directives(md)
    return md


def render_alert_push(alert):
    """
    推送版：剥离一切金额（ntfy.sh 是公共服务器）。
    返回 (title, body)。
    """
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(alert["next_steps"][:3], 1))
    title = f"【{alert['level']}】{alert['ticker']} · {alert['category']}"
    body = (
        f"发生了什么\n{alert['facts']}\n\n"
        f"数据\n{alert['data']}\n\n"
        f"这类信号通常意味着什么\n{alert['base_rate']}\n\n"
        f"反面观点\n{alert['counterpoint']}\n\n"
        f"接下来看什么\n{steps}"
    )
    # 兜底：把任何漏网的金额抹掉，绝不让它出网
    body = _MONEY.sub("[金额见面板]", body)
    title = _MONEY.sub("", title)
    assert_no_directives(body)
    return title, body
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_alerts.py
```

预期：`✅ 全部通过`。若「正样本」那组有误伤，**改 `BANNED_PHRASES` 而不是改测试** ——
一个什么都不敢说的系统没有价值。

- [ ] **Step 5: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

---

### Task 7: LLM adapter（双路）

全流程唯一用 LLM 的地方。它只做摘要和翻译，不产生任何数字。

**Files:**
- Create: `sw/llm.py`
- Create: `packaging/llm-workdir/.gitkeep`（专用空目录，见下）
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `sw.config.CFG`、`sw.alerts.assert_no_directives`
- Produces:
  - `LLMConfigError(Exception)`
  - `check_env(cfg) -> None` 配置与环境冲突时抛 `LLMConfigError`
  - `summarize(cfg, material, instruction, dry_run=False) -> str`
  - `build_cli_cmd(cfg, instruction) -> list[str]` 便于测试命令行拼装

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_llm.py`：

```python
#!/usr/bin/env python3
"""LLM adapter 的测试。运行：python3 tests/test_llm.py

⚠️ 不真调 LLM。测的是配置守卫和命令行拼装 —— 这两处错了会静默烧钱。

最重要的一条：provider=claude_cli 时环境里若存在 ANTHROPIC_API_KEY，
Claude Code 会**静默改用 API 计费**而不是订阅。必须启动时就报错退出。
"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import llm as LM

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)

def check_raises(name, fn, exc):
    try:
        fn()
    except exc:
        print(f"  ✅ {name}")
        return
    except Exception as e:
        print(f"  ❌ {name}: 抛了 {type(e).__name__}，期望 {exc.__name__}")
    else:
        print(f"  ❌ {name}: 没抛异常")
    FAIL.append(name)


class Cfg:
    def __init__(self, provider="claude_cli", model="claude-opus-5"):
        self._d = {"llm.provider": provider, "llm.model": model,
                   "llm.max_tokens": 2000,
                   "llm.claude_bin": "/Users/lambo/.local/bin/claude"}
    def get(self, k, d=None):
        return self._d.get(k, d)


def test_api_key_conflict_is_fatal():
    print("\n配置守卫：claude_cli 模式下存在 API key 必须报错")
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        check_raises("claude_cli + API key → 报错",
                     lambda: LM.check_env(Cfg("claude_cli")), LM.LLMConfigError)
        # api 模式下有 key 是正常的
        LM.check_env(Cfg("api"))
        print("  ✅ api 模式 + API key → 放行")
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_api_mode_without_key_is_fatal():
    print("\n配置守卫：api 模式缺 key 必须报错")
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        check_raises("api 模式无 key → 报错",
                     lambda: LM.check_env(Cfg("api")), LM.LLMConfigError)
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_cli_cmd_shape():
    print("\n命令行拼装")
    cmd = LM.build_cli_cmd(Cfg("claude_cli"), "把材料压成一句话")
    check("走全路径（launchd 不继承 PATH）",
          cmd[0], "/Users/lambo/.local/bin/claude")
    check("有 -p", "-p" in cmd, True)
    check("绝不能有 --bare（它不读订阅登录）", "--bare" in cmd, False)
    check("带 append-system-prompt", "--append-system-prompt" in cmd, True)
    check("输出格式为 json", "json" in cmd, True)


def test_dry_run_returns_placeholder_and_calls_nothing():
    print("\ndry-run 不真调")
    out = LM.summarize(Cfg("claude_cli"), "材料若干", "压成一句话", dry_run=True)
    check("返回非空字符串", isinstance(out, str) and len(out) > 0, True)
    check("标注了 dry-run", "dry-run" in out, True)


def test_output_passes_directive_guard():
    print("\nLLM 输出也要过指令性措辞守卫")
    from sw.alerts import assert_no_directives
    check_raises("含指令的输出被拦下",
                 lambda: LM._guard("建议卖出该持仓"), ValueError)
    LM._guard("内部人集中卖出，共 3 笔")
    print("  ✅ 客观陈述放行")


if __name__ == "__main__":
    test_api_key_conflict_is_fatal()
    test_api_mode_without_key_is_fatal()
    test_cli_cmd_shape()
    test_dry_run_returns_placeholder_and_calls_nothing()
    test_output_passes_directive_guard()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_llm.py
```

预期：`ModuleNotFoundError: No module named 'sw.llm'`

- [ ] **Step 3: 建专用工作目录**

`claude -p` 不加 `--bare` 时会读取**当前目录**的 `CLAUDE.md`、hooks、skills、MCP 配置。
在项目目录里跑会把整个开发环境的上下文和 token 成本带进来。

```bash
mkdir -p packaging/llm-workdir && touch packaging/llm-workdir/.gitkeep
```

- [ ] **Step 4: 写实现**

创建 `sw/llm.py`：

```python
"""
LLM adapter —— 全流程唯一用 LLM 的地方。

它只做两件事：① 读材料写摘要 ② 把结构化结果翻译成人话。
**不产生任何数字。** 报告里每个数字都来自确定性 Python，换模型不影响任何结论。

两条路：
  claude_cli —— 调 `claude -p`，走 Claude Pro 订阅，不额外花钱
  api        —— 调 Anthropic API，按量付费，费用可预测

⚠️ claude_cli 的四个坑（每一个都会静默出错）：
  1. 不能加 --bare —— bare 模式不读订阅登录，会要求 API key
  2. 环境里不能有 ANTHROPIC_API_KEY —— 一旦存在，Claude Code 静默改用 API 计费
  3. launchd 不继承 shell PATH —— 必须写 claude 的全路径
  4. cwd 决定加载什么上下文 —— 必须在专用空目录里跑
"""
import json
import os
import subprocess
from pathlib import Path

from .alerts import assert_no_directives

ROOT = Path(__file__).resolve().parent.parent
LLM_WORKDIR = ROOT / "packaging" / "llm-workdir"
DEFAULT_CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")


class LLMConfigError(Exception):
    pass


def check_env(cfg):
    """启动时的配置守卫。宁可退出，也不静默烧钱或静默失败。"""
    provider = cfg.get("llm.provider", "claude_cli")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "claude_cli" and has_key:
        raise LLMConfigError(
            "llm.provider=claude_cli，但环境里存在 ANTHROPIC_API_KEY。\n"
            "Claude Code 检测到该变量会**静默改用 API 按量计费**而不是你的 Pro 订阅。\n"
            "解决：从这个任务的环境里移除该变量（检查 plist 的 EnvironmentVariables "
            "和 ~/.zshrc），或把 llm.provider 改成 api。")
    if provider == "api" and not has_key:
        raise LLMConfigError(
            "llm.provider=api，但环境里没有 ANTHROPIC_API_KEY。\n"
            "把 key 写进 launchd plist 的 EnvironmentVariables —— "
            "不要写进 ~/.zshrc，那会让你交互式使用 Claude Code 也变成按量付费。")
    if provider not in ("claude_cli", "api"):
        raise LLMConfigError(f"未知的 llm.provider: {provider!r}")


def _guard(text):
    """LLM 输出也要过指令性措辞守卫 —— 模型不知道我们的产品约束。"""
    assert_no_directives(text)
    return text


def build_cli_cmd(cfg, instruction):
    """拼 claude -p 的命令行。单独抽出来是为了能测。"""
    claude_bin = cfg.get("llm.claude_bin") or DEFAULT_CLAUDE_BIN
    return [
        claude_bin, "-p",
        "--append-system-prompt", instruction,
        "--output-format", "json",
        # ⚠️ 不要加 --bare：它不读订阅登录
        # ⚠️ 不给任何工具权限：纯文本进，纯文本出
        "--permission-mode", "dontAsk",
    ]


SYSTEM = (
    "你在为一个个人股票研究系统写摘要。规则：\n"
    "1. 只根据给你的材料写，材料里没有的一律不写，不要补充背景知识。\n"
    "2. 不要给任何建议、不要用祈使句、不要出现『建议』『应该』『赶紧』『目标价』。\n"
    "   你的任务是描述发生了什么，不是指挥用户做什么。\n"
    "3. 不要编造数字。材料里的数字原样引用，没有的就不写。\n"
    "4. 材料不足以得出结论时，直接说『材料不足以判断原因』。\n"
    "5. 用中文，简体，两句话以内。"
)


def summarize(cfg, material, instruction=None, dry_run=False, timeout=120):
    """把材料压成一两句人话。失败返回空字符串，绝不中断整个任务。"""
    instruction = f"{SYSTEM}\n\n{instruction or ''}".strip()
    if dry_run:
        return f"[dry-run] 将把 {len(material)} 字材料交给 LLM 摘要"

    check_env(cfg)
    provider = cfg.get("llm.provider", "claude_cli")

    if provider == "claude_cli":
        LLM_WORKDIR.mkdir(parents=True, exist_ok=True)
        cmd = build_cli_cmd(cfg, instruction)
        try:
            p = subprocess.run(cmd, input=material, capture_output=True,
                               text=True, timeout=timeout, cwd=str(LLM_WORKDIR))
            if p.returncode != 0:
                return ""
            data = json.loads(p.stdout)
            return _guard((data.get("result") or "").strip())
        except Exception:
            return ""

    # provider == "api"
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=cfg.get("llm.model", "claude-opus-5"),
            max_tokens=int(cfg.get("llm.max_tokens", 2000)),
            system=instruction,
            messages=[{"role": "user", "content": material}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _guard(text.strip())
    except Exception:
        return ""
```

- [ ] **Step 5: 在 config.yaml 里加 provider**

修改 `stockwatch/config.yaml` 的 `llm:` 段，在 `model:` 之前加两行：

```yaml
llm:
  # claude_cli = 调 claude -p 走 Pro 订阅，不额外花钱但占用订阅额度
  # api        = 调 Anthropic API 按量付费，key 只写进 launchd plist
  provider: "claude_cli"
  claude_bin: "/Users/lambo/.local/bin/claude"
  model: "claude-opus-5"
  max_tokens: 2000
```

- [ ] **Step 6: 跑测试确认通过**

```bash
python3 tests/test_llm.py
```

预期：`✅ 全部通过`

- [ ] **Step 7: 真调一次验证订阅路线可用**

```bash
python3 -c "
import sys, os; sys.path.insert(0,'.')
assert not os.environ.get('ANTHROPIC_API_KEY'), '先把 ANTHROPIC_API_KEY 从环境里去掉'
from sw.config import CFG
from sw import llm as LM
out = LM.summarize(CFG,
    '材料：AAPL 于 2026-08-27 提交 8-K，Item 5.02，CFO 离职，未披露继任安排。'
    '当日股价 -8.7%，个股独立部分 -9.1%（4.1σ）。',
    '把上述材料压成两句话，说明发生了什么。')
print('LLM 返回:', repr(out))
assert out, '返回为空 —— 检查 claude CLI 是否已登录（跑 claude 交互一次）'
print('✅ claude -p 订阅路线可用')"
```

预期：返回一两句中文描述。返回空说明 `claude` 未登录，先在终端里跑一次 `claude` 完成登录。

- [ ] **Step 8: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

---

### Task 8: 日报渲染

两套输出：面板用的完整 Markdown，推送用的精简版（无金额）。

**Files:**
- Create: `sw/daily_report.py`
- Test: `tests/test_daily_report.py`

**Interfaces:**
- Consumes: Task 4 归因结果、Task 5 原因、Task 6 提醒、`sw.notify.assert_no_money`、`sw.alerts.assert_no_directives`
- Produces:
  - `render_markdown(ctx) -> str` 完整日报（存 `reports` 表和 `reports/` 目录）
  - `render_push(ctx) -> tuple[str, str]` 推送版 `(title, body)`
  - ctx 结构：`{"d","attributions","alerts","causes_by_ticker","watchlist_events","market","portfolio"}`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_daily_report.py`：

```python
#!/usr/bin/env python3
"""日报渲染测试。运行：python3 tests/test_daily_report.py

两条核心断言：
  1. 残差正常的持仓**根本不出现** —— 日报一半的价值来自它不说什么
  2. 推送版不含金额 —— ntfy.sh 是公共服务器
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import daily_report as DR
from sw.notify import assert_no_money
from sw.alerts import assert_no_directives

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def make_ctx():
    return {
        "d": "2026-08-27",
        "attributions": [
            {"ticker": "ABCD", "ret": 0.062, "mkt_part": 0.008, "sector_part": 0.021,
             "idio": 0.033, "z": 2.4, "level": "anomaly", "sector": "Technology",
             "beta_mkt": 1.4, "beta_sector": 0.9, "r2": 0.55, "n": 60},
            {"ticker": "QUIET", "ret": 0.004, "mkt_part": 0.003, "sector_part": 0.001,
             "idio": 0.000, "z": 0.1, "level": "normal", "sector": "Technology",
             "beta_mkt": 1.0, "beta_sector": 0.8, "r2": 0.7, "n": 60},
            {"ticker": "ALSOQUIET", "ret": -0.002, "mkt_part": -0.001,
             "sector_part": -0.001, "idio": 0.0, "z": -0.2, "level": "normal",
             "sector": "Energy", "beta_mkt": 0.6, "beta_sector": 0.9,
             "r2": 0.6, "n": 60},
        ],
        "causes_by_ticker": {
            "ABCD": [{"source": "8-K", "summary": "2026-08-27 提交 8-K：披露季度业绩",
                      "url": "https://sec.gov/x", "item": "2.02", "is_l1": False}],
        },
        "alerts": [],
        "watchlist_events": ["3 家基金本季新建仓 MNOP", "LMNO 讨论量从 78 名升至 19 名"],
        "market": {"spy_vs_200d": 0.042, "vix": 16.8},
        "portfolio_weights": {"ABCD": 0.11, "QUIET": 0.07, "ALSOQUIET": 0.03},
        "n_holdings": 3,
    }


def test_quiet_holdings_are_absent():
    print("\n残差正常的持仓不该出现（日报一半的价值来自它不说什么）")
    md = DR.render_markdown(make_ctx())
    check("异动的 ABCD 出现", "ABCD" in md, True)
    check("平静的 QUIET 不出现", "QUIET" in md.replace("ALSOQUIET", ""), False)
    check("有「其余 N 只无异常」的交代", "无异常" in md, True)


def test_push_has_no_money_and_no_directives():
    print("\n推送版：无金额、无指令")
    ctx = make_ctx()
    title, body = DR.render_push(ctx)
    assert_no_money(body)
    assert_no_money(title)
    assert_no_directives(body)
    check("标题含日期", "2026-08-27" in title, True)
    check("正文含异动代码", "ABCD" in body, True)
    check("正文含观察池", "MNOP" in body, True)
    print("  ✅ 推送版通过金额与指令双重检查")


def test_decomposition_shown():
    print("\n必须展示三段拆解，每个数字可追溯")
    md = DR.render_markdown(make_ctx())
    for frag in ["大盘", "行业", "个股独立", "σ"]:
        check(f"含「{frag}」", frag in md, True)


def test_empty_day_is_still_valid():
    print("\n全无异动的一天也要能生成报告，不能崩")
    ctx = make_ctx()
    for a in ctx["attributions"]:
        a["level"], a["z"] = "normal", 0.1
    ctx["causes_by_ticker"] = {}
    ctx["watchlist_events"] = []
    md = DR.render_markdown(ctx)
    title, body = DR.render_push(ctx)
    check("Markdown 非空", len(md) > 50, True)
    check("推送正文非空", len(body) > 10, True)
    check("说明今天无异动", "无异常" in md or "无异动" in md, True)


if __name__ == "__main__":
    test_quiet_holdings_are_absent()
    test_push_has_no_money_and_no_directives()
    test_decomposition_shown()
    test_empty_day_is_still_valid()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_daily_report.py
```

预期：`ModuleNotFoundError: No module named 'sw.daily_report'`

- [ ] **Step 3: 写实现**

创建 `sw/daily_report.py`：

```python
"""
日报渲染。两套输出：

  render_markdown  完整版，存 reports 表和 reports/ 目录，可含金额
  render_push      推送版，经 ntfy.sh 公共服务器，**绝不含金额**

⭐ 最重要的设计：残差在正常范围的持仓**根本不出现在报告里**。
日报一半的价值来自它不说什么 —— 每天列出 26 只票的涨跌等于没有信息。
"""
from .alerts import assert_no_directives, render_alert, render_alert_push
from .notify import assert_no_money


def _movers(ctx):
    """只挑出异动的，按 |z| 降序。"""
    xs = [a for a in ctx["attributions"] if a.get("level") in ("anomaly", "extreme")]
    return sorted(xs, key=lambda a: -abs(a.get("z") or 0))


def _fmt_line(a, causes):
    reason = "未找到明确原因"
    if causes:
        reason = causes[0]["summary"]
    return (
        f"**{a['ticker']}**　{a['ret']*100:+.1f}%\n"
        f"　　其中大盘 {a['mkt_part']*100:+.1f}%，"
        f"行业 {a['sector_part']*100:+.1f}%，"
        f"个股独立 {a['idio']*100:+.1f}%（{a['z']:+.1f}σ）\n"
        f"　　原因：{reason}"
    )


def render_markdown(ctx):
    movers = _movers(ctx)
    total = len(ctx["attributions"])
    quiet = total - len(movers)
    L = [f"# 每日简报 · {ctx['d']}", ""]

    if movers:
        L.append(f"## 持仓异动（{len(movers)} 项，其余 {quiet} 只无异常）")
        L.append("")
        for a in movers:
            L.append(_fmt_line(a, ctx["causes_by_ticker"].get(a["ticker"])))
            L.append("")
    else:
        L += [f"## 持仓异动", "",
              f"今天 {total} 只持仓**全部无异常** —— "
              f"个股独立部分都在 2σ 以内，涨跌可由大盘和行业解释。", ""]

    if ctx.get("alerts"):
        L += ["## 需要注意的信号", ""]
        for al in ctx["alerts"]:
            L.append(render_alert(al, holding=None))
            L.append("")

    if ctx.get("watchlist_events"):
        L += ["## 观察池", ""]
        for e in ctx["watchlist_events"]:
            L.append(f"- {e}")
        L.append("")

    m = ctx.get("market") or {}
    if m:
        bits = []
        if m.get("spy_vs_200d") is not None:
            bits.append(f"SPY 位于 200 日线{'上方' if m['spy_vs_200d'] >= 0 else '下方'}"
                        f" {abs(m['spy_vs_200d'])*100:.1f}%")
        if m.get("vix") is not None:
            bits.append(f"VIX {m['vix']:.1f}")
        if bits:
            L += ["## 市场环境", "", "　".join(bits), ""]

    L += ["---", "",
          "> 归因用过去 60 个交易日回归：个股收益 = α + β_市场×SPY + β_行业×行业ETF。",
          "> 残差在 2σ 以内的持仓不在本报告中出现。**未经回测验证，不预测涨跌。**",
          "> 本工具为个人研究用途，所有产出不构成投资建议。"]
    md = "\n".join(L)
    assert_no_directives(md)
    return md


def render_push(ctx):
    """推送版：短、无金额、能在手机锁屏上读完要点。"""
    movers = _movers(ctx)
    total = len(ctx["attributions"])
    title = f"StockWatch {ctx['d']}　异动 {len(movers)}/{total}"

    L = []
    if movers:
        for a in movers[:5]:
            causes = ctx["causes_by_ticker"].get(a["ticker"]) or []
            reason = causes[0]["summary"] if causes else "未找到明确原因"
            L.append(f"{a['ticker']} {a['ret']*100:+.1f}%"
                     f"（个股独立 {a['idio']*100:+.1f}%，{a['z']:+.1f}σ）\n"
                     f"  {reason[:80]}")
        if len(movers) > 5:
            L.append(f"…另有 {len(movers)-5} 项，详见面板")
    else:
        L.append(f"{total} 只持仓全部无异常，涨跌可由大盘和行业解释。")

    if ctx.get("watchlist_events"):
        L.append("")
        L.append("观察池")
        for e in ctx["watchlist_events"][:3]:
            L.append(f"  {e}")

    body = "\n".join(L)
    assert_no_money(body)
    assert_no_money(title)
    assert_no_directives(body)
    return title, body
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_daily_report.py
```

预期：`✅ 全部通过`

- [ ] **Step 5: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

---

### Task 9: run_daily.py 主流程

把前八个任务串起来。**只入队，不发送。**

**Files:**
- Create: `run_daily.py`
- Test: `tests/test_run_daily.py`

**Interfaces:**
- Consumes: 全部前置任务
- Produces:
  - `build_context(store, cfg, portfolio, window=60) -> dict` 供测试直接调用
  - `main()` CLI 入口，支持 `--dry-run` / `--skip-ingest` / `--force`
  - 退出码：0 成功；1 失败（launchd 会记进 StandardErrorPath）
  - 副作用：写 `reports` 表、`reports/daily_<日期>.md`、`outbox`（kind=daily 及每条 l1）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_run_daily.py`：

```python
#!/usr/bin/env python3
"""主流程测试。运行：python3 tests/test_run_daily.py

用合成价格建一个临时库，跑完整流程，验证：
  - 产出 daily 条目进 outbox
  - 已经成功过的当天再跑不重复入队（07:00 重试任务会依赖这个）
  - 过了推送点时自己 drain（睡过头竞态的堵法）
"""
import sys, tempfile, math
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from sw.store import Store
from sw import outbox as OB
import run_daily as RD

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def seeded_store(anomaly=True):
    """造一个含 SPY / XLK / ABCD 的库。ABCD 最后一天注入 3σ 冲击。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    st = Store(tmp.name)
    rng = np.random.default_rng(5)
    n = 120
    days = []
    d = date(2026, 3, 2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)

    mkt = rng.normal(0, 0.009, n)
    sec = rng.normal(0, 0.007, n)
    sigma = 0.004
    idio = rng.normal(0, sigma, n)
    if anomaly:
        idio[-1] += 3.2 * sigma
    tgt = 1.2 * mkt + 0.7 * sec + idio

    def to_rows(tk, rets):
        px, rows = 100.0, []
        for dd, r in zip(days, rets):
            px *= (1 + r)
            rows.append((dd, tk, px, 1e6))
        return rows

    rows = to_rows("SPY", mkt) + to_rows("XLK", sec) + to_rows("ABCD", tgt)
    st.upsert_many("prices", ["d", "ticker", "close", "volume"], rows)
    st.upsert_many("meta", ["ticker", "sector", "industry", "name",
                            "market_cap", "updated_at"],
                   [("ABCD", "Technology", "Semis", "ABCD Inc", 1e11, "2026-08-27")])
    st.upsert_many("positions",
                   ["snapshot_date", "ticker", "description", "quantity",
                    "last_price", "market_value", "cost_basis_total", "avg_cost",
                    "total_gain", "asset_type", "account", "loaded_at"],
                   [(days[-1], "ABCD", "ABCD Inc", 10, 100.0, 1000.0, 800.0,
                     80.0, 200.0, "equity", "TEST", days[-1])])
    return st, days[-1]


class Cfg:
    def __init__(self):
        self._d = {
            "benchmarks.market": "SPY",
            "benchmarks.sectors": {"Technology": "XLK"},
            "llm.provider": "claude_cli",
            "portfolio.core_tickers": [],
            "portfolio.exclude_tickers": [],
            "schedule.push_time": "08:00",
        }
    def get(self, k, d=None):
        return self._d.get(k, d)
    ntfy_url = "https://ntfy.sh/test"
    ntfy_topic = "test"


def test_anomaly_produces_daily_entry():
    print("\n有异动时产出 daily 条目")
    st, last_d = seeded_store(anomaly=True)
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    check("检出 1 只异动", len([a for a in ctx["attributions"]
                              if a["level"] != "normal"]), 1)
    n = RD.emit(st, Cfg(), ctx, dry_run=False)
    check("入队 1 条 daily", OB.has_kind_on(st, "daily", ctx["d"]), True)
    st.close()


def test_no_anomaly_still_produces_entry():
    print("\n无异动也要产出条目 —— 否则 notify 会误报失败")
    st, last_d = seeded_store(anomaly=False)
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    check("没有异动", [a for a in ctx["attributions"] if a["level"] != "normal"], [])
    RD.emit(st, Cfg(), ctx, dry_run=False)
    check("仍然入队 daily", OB.has_kind_on(st, "daily", ctx["d"]), True)
    st.close()


def test_rerun_is_idempotent():
    print("\n当天重跑不重复入队（07:00 重试任务依赖这个）")
    st, last_d = seeded_store()
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    RD.emit(st, Cfg(), ctx, dry_run=False)
    RD.emit(st, Cfg(), ctx, dry_run=False)
    n = len(st.q("SELECT id FROM outbox WHERE kind='daily'"))
    check("只有 1 条 daily", n, 1)
    st.close()


def test_force_allows_rerun():
    print("\n--force 可以强制重跑")
    st, last_d = seeded_store()
    ctx = RD.build_context(st, Cfg(), snapshot_date=last_d, use_llm=False)
    RD.emit(st, Cfg(), ctx, dry_run=False)
    RD.emit(st, Cfg(), ctx, dry_run=False, force=True)
    n = len(st.q("SELECT id FROM outbox WHERE kind='daily'"))
    check("有 2 条 daily", n, 2)
    st.close()


if __name__ == "__main__":
    test_anomaly_produces_daily_entry()
    test_no_anomaly_still_produces_entry()
    test_rerun_is_idempotent()
    test_force_allows_rerun()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_run_daily.py
```

预期：`ModuleNotFoundError: No module named 'run_daily'`

- [ ] **Step 3: 写实现**

创建 `run_daily.py`：

```python
#!/usr/bin/env python3
"""
每日计算主流程（launchd 06:00 调这个）。

⚠️ 这个脚本**只入队，不发送**。推送由 run_notify.py 在 08:00 统一负责。
拆开的理由见设计文档 §2.1：08:00 那个独立任务发现「今天没有 daily 条目」
就能上报失败 —— 哪怕本脚本是被 OOM killer 干掉的，try/except 抓不到那种情况。

用法：
  python3 run_daily.py                    # 正常跑
  python3 run_daily.py --dry-run          # 全流程但不写库不入队
  python3 run_daily.py --skip-ingest      # 跳过抓数，用库里现有数据
  python3 run_daily.py --force            # 当天已跑过也重新入队
"""
import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw import outbox as OB, notify as NT, alerts as AL, llm as LM
from sw import daily_report as DR
from sw.analysis import portfolio as PF, attribution as AT, causes as CS
from sw.market_time import et_today, now_et


def log(m):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {m}", flush=True)


def build_context(store, cfg, snapshot_date=None, window=60, use_llm=True):
    """跑完全部确定性计算，返回渲染用的 ctx。这一层不写库、不入队，方便测试。"""
    p = PF.build(store, cfg, snapshot_date)
    sector_map = cfg.get("benchmarks.sectors") or {}
    market_etf = cfg.get("benchmarks.market", "SPY")

    # 1. 归因
    attributions = []
    for h in p.holdings:
        etf = sector_map.get(h.sector) if h.sector else None
        try:
            r = AT.attribute(store, h.ticker, etf, market_etf, window)
        except Exception as e:
            log(f"  ⚠️ {h.ticker} 归因失败：{type(e).__name__}: {e}")
            r = None
        if r:
            r["sector"] = h.sector
            attributions.append(r)

    d = attributions[0]["d"] if attributions else et_today().isoformat()

    # 2. 只给异动的找原因 —— 正常的不查，省时间也省额度
    causes_by_ticker = {}
    for a in attributions:
        if a["level"] == "normal":
            continue
        peers = CS.peer_readthrough(attributions, a["ticker"], a.get("sector"))
        try:
            causes_by_ticker[a["ticker"]] = CS.find_causes(
                store, a["ticker"], a["d"], peers=peers)
        except Exception as e:
            log(f"  ⚠️ {a['ticker']} 找原因失败：{type(e).__name__}: {e}")
            causes_by_ticker[a["ticker"]] = []

    # 3. 恶化扫描
    alerts = AL.scan(store, p, attributions, causes_by_ticker)

    # 4. LLM 摘要 —— 全流程唯一用 LLM 的地方
    if use_llm and causes_by_ticker:
        for tk, cs in causes_by_ticker.items():
            if not cs:
                continue
            material = "\n".join(f"[{c['source']}] {c['summary']}" for c in cs)
            s = LM.summarize(cfg, material,
                             f"下面是 {tk} 今日异动的相关材料，压成一句话说明发生了什么。")
            if s:
                cs.insert(0, {"source": "摘要", "summary": s, "url": "",
                              "item": None, "is_l1": False})

    return {
        "d": d,
        "attributions": attributions,
        "causes_by_ticker": causes_by_ticker,
        "alerts": alerts,
        "watchlist_events": [],      # P4 填充；本期留空
        "market": {},                # 可选，缺失时报告自动省略该节
        "portfolio_weights": p.weights(),
        "n_holdings": len(p.holdings),
    }


def emit(store, cfg, ctx, dry_run=False, force=False):
    """渲染 + 写库 + 入队。返回入队条目数。"""
    if not force and OB.has_kind_on(store, "daily", ctx["d"]):
        log(f"  {ctx['d']} 已经入过队，跳过（用 --force 强制重跑）")
        return 0

    md = DR.render_markdown(ctx)
    title, body = DR.render_push(ctx)
    if dry_run:
        log("─" * 60); print(md); log("─" * 60)
        log(f"[dry-run] 推送标题：{title}"); print(body)
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    store.upsert_many("reports", ["d", "kind", "body_md", "created_at"],
                      [(ctx["d"], "daily", md, now)])
    out = Path(__file__).resolve().parent / "reports" / f"daily_{ctx['d']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    n = 0
    OB.enqueue(store, "daily", title, body, priority="default"); n += 1
    for al in ctx["alerts"]:
        if al["level"] != "L1":
            continue
        t, b = AL.render_alert_push(al)
        OB.enqueue(store, "l1", t, b, priority="urgent"); n += 1

    # alerts 表也留一份，供面板和以后的 P7 回看
    store.upsert_many("alerts", ["d", "ticker", "level", "category", "body_md"],
                      [(ctx["d"], a["ticker"], a["level"], a["category"],
                        AL.render_alert(a)) for a in ctx["alerts"]])
    log(f"  ✅ 入队 {n} 条，报告写入 {out.name}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--window", type=int, default=60)
    a = ap.parse_args()

    st = Store(CFG.db_path)
    try:
        LM.check_env(CFG)          # 配置错了立刻退出，不要跑完才发现
    except LM.LLMConfigError as e:
        log(f"❌ LLM 配置有问题：{e}")
        st.close()
        return 1

    try:
        if not a.skip_ingest:
            log("1/3 抓数")
            import run_ingest
            sys.argv = ["run_ingest.py"] + (["--dry-run"] if a.dry_run else [])
            run_ingest.main()

        log("2/3 计算")
        ctx = build_context(st, CFG, window=a.window)
        log(f"  {len(ctx['attributions'])} 只归因成功，"
            f"{sum(1 for x in ctx['attributions'] if x['level'] != 'normal')} 只异动，"
            f"{len(ctx['alerts'])} 条提醒")

        log("3/3 渲染与入队")
        emit(st, CFG, ctx, dry_run=a.dry_run, force=a.force)

        # 睡过头的堵法：已经过了推送点就自己 drain 一次。
        # 注意调的是 notify.drain 这**同一个函数**，不是复制一份逻辑。
        push_at = str(CFG.get("schedule.push_time", "08:00"))
        if not a.dry_run and datetime.now().strftime("%H:%M") >= push_at:
            log(f"  当前已过推送点 {push_at}，立即 drain 一次")
            r = NT.drain(st, CFG)
            log(f"  drain: 发出 {r['sent']} 条，失败 {r['failed']} 条")

        st.close()
        return 0
    except Exception:
        log("❌ 主流程异常：\n" + traceback.format_exc())
        st.log_health("run_daily", False, 0, traceback.format_exc()[-400:])
        st.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_run_daily.py
```

预期：`✅ 全部通过`

- [ ] **Step 5: 用真实库干跑一次**

```bash
python3 run_daily.py --dry-run --skip-ingest
```

预期：打印完整日报 Markdown + 推送正文，不写任何东西。
**人工检查这几点**：异动数量是否合理（不该 26 只全是异动）、原因是否对得上、推送正文里有没有 `$`。

- [ ] **Step 6: 真跑一次并确认入队**

```bash
python3 run_daily.py --skip-ingest
python3 -c "
import sys; sys.path.insert(0,'.')
from sw.store import Store; from sw.config import CFG
st = Store(CFG.db_path)
for r in st.q('SELECT id,kind,priority,title,sent_at FROM outbox ORDER BY id'):
    print(f\"  #{r['id']} [{r['kind']}/{r['priority']}] {r['title'][:60]}  sent={r['sent_at']}\")"
```

预期：至少一条 `kind=daily`、`sent_at=None` 的条目。

- [ ] **Step 7: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

---

### Task 10: 推送出口、调度渲染与安装

最后一环。做完这个，每天早上 8:00 手机就会响。

**Files:**
- Create: `run_notify.py`
- Create: `sw/schedule.py`
- Create: `packaging/install.sh`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: `sw.notify.drain`、`sw.config.CFG`
- Produces:
  - `sw.schedule.render_plist(label, args, calendar, workdir, env=None) -> str`
  - `sw.schedule.plans(cfg, python_bin, workdir) -> list[dict]` 三个任务的定义
  - `sw.schedule.apply(cfg, python_bin, workdir, dry_run=False) -> list[str]`
    写 `~/Library/LaunchAgents/*.plist` 并 `launchctl bootout` + `bootstrap`
  - `run_notify.py` CLI，支持 `--dry-run`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_schedule.py`：

```python
#!/usr/bin/env python3
"""plist 渲染测试。运行：python3 tests/test_schedule.py

只测渲染，不真的 load —— 那会改动用户的 LaunchAgents。
"""
import sys, plistlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import schedule as SC

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


class Cfg:
    def __init__(self, **kw):
        self._d = {"schedule.compute_daily": "06:00",
                   "schedule.compute_retry": "07:00",
                   "schedule.compute_weekly": "06:30",
                   "schedule.weekly_day": "tuesday",
                   "schedule.push_time": "08:00",
                   "llm.provider": "claude_cli"}
        self._d.update(kw)
    def get(self, k, d=None):
        return self._d.get(k, d)


def test_plist_is_valid_and_has_calendar():
    print("\nplist 渲染")
    xml = SC.render_plist("com.lambo.test", ["/usr/bin/python3", "x.py"],
                          {"Hour": 6, "Minute": 0}, "/tmp/wd")
    d = plistlib.loads(xml.encode("utf-8"))
    check("Label 正确", d["Label"], "com.lambo.test")
    check("时间正确", d["StartCalendarInterval"], {"Hour": 6, "Minute": 0})
    check("RunAtLoad 关闭", d.get("RunAtLoad", False), False)
    check("有工作目录", d["WorkingDirectory"], "/tmp/wd")
    check("有日志路径", "StandardErrorPath" in d, True)


def test_weekday_mapping():
    print("\n星期映射（launchd: 0/7=周日, 1=周一, 2=周二）")
    check("tuesday → 2", SC.WEEKDAYS["tuesday"], 2)
    check("monday → 1", SC.WEEKDAYS["monday"], 1)
    check("sunday → 0", SC.WEEKDAYS["sunday"], 0)


def test_three_plans_from_config():
    print("\n从 config 生成三个任务")
    ps = SC.plans(Cfg(), "/usr/bin/python3", "/tmp/wd")
    labels = [p["label"] for p in ps]
    check("四个任务（日/重试/周/推送）", len(ps), 4)
    check("含 compute-daily", "com.lambo.stockwatch-compute-daily" in labels, True)
    check("含 notify", "com.lambo.stockwatch-notify" in labels, True)
    daily = [p for p in ps if p["label"].endswith("compute-daily")][0]
    check("日报 06:00", daily["calendar"], {"Hour": 6, "Minute": 0})
    notify = [p for p in ps if p["label"].endswith("notify")][0]
    check("推送 08:00", notify["calendar"], {"Hour": 8, "Minute": 0})
    weekly = [p for p in ps if p["label"].endswith("compute-weekly")][0]
    check("周报 周二 06:30", weekly["calendar"],
          {"Weekday": 2, "Hour": 6, "Minute": 30})


def test_no_api_key_leaked_in_cli_mode():
    print("\nclaude_cli 模式下 plist 不能带 API key")
    ps = SC.plans(Cfg(), "/usr/bin/python3", "/tmp/wd")
    for p in ps:
        xml = SC.render_plist(p["label"], p["args"], p["calendar"],
                              "/tmp/wd", env=p.get("env"))
        check(f"{p['label'][-14:]} 无 API key",
              "ANTHROPIC_API_KEY" in xml, False)


if __name__ == "__main__":
    test_plist_is_valid_and_has_calendar()
    test_weekday_mapping()
    test_three_plans_from_config()
    test_no_api_key_leaked_in_cli_mode()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_schedule.py
```

预期：`ModuleNotFoundError: No module named 'sw.schedule'`

- [ ] **Step 3: 写 run_notify.py**

```python
#!/usr/bin/env python3
"""
推送出口（launchd 08:00 调这个）—— 系统里唯一会真正发出通知的地方。

它也是失败看门狗：队列里今天没有 daily 条目，就说明计算任务没跑成
（可能是被 OOM killer 干掉这类 try/except 抓不到的情况），主动推一条告知。
用户不会每天开 app，「没有消息」绝不能等于「没事」。

用法：
  python3 run_notify.py              # 正常发
  python3 run_notify.py --dry-run    # 只打印不发
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw import notify as NT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    st = Store(CFG.db_path)
    r = NT.drain(st, CFG, dry_run=a.dry_run)
    print(f"[{datetime.now().isoformat(timespec='seconds')}] "
          f"发出 {r['sent']} 条，失败 {r['failed']} 条"
          + ("，已上报计算任务失败" if r["failure_reported"] else ""))
    st.close()
    return 0 if r["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 写 sw/schedule.py**

```python
"""
launchd 任务的渲染与加载。

⚠️ 必须用 launchd 不能用 cron —— macOS 休眠时 cron 会静默跳过任务，
不补跑也不报错。launchd 的 StartCalendarInterval 在唤醒后会补跑。

⚠️ StartCalendarInterval 按**本机墙钟时间**触发，所以用户换时区、出差，
推送时间自动跟随。这正是「读取系统时区」的需求，不需要额外代码。

本模块被 CLI 和以后的设置窗口**共用同一个函数** —— 不要在做 UI 时另写一份。
"""
import os
import plistlib
import subprocess
from pathlib import Path

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PREFIX = "com.lambo.stockwatch"

# launchd 约定：0 和 7 都是周日，1 是周一
WEEKDAYS = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3,
            "thursday": 4, "friday": 5, "saturday": 6}


def _hm(s, default=(6, 0)):
    try:
        h, m = str(s).split(":")
        return int(h), int(m)
    except Exception:
        return default


def render_plist(label, args, calendar, workdir, env=None, logs_dir=None):
    """生成一个 launchd plist 的 XML 文本。"""
    logs = Path(logs_dir) if logs_dir else Path(workdir) / "logs"
    d = {
        "Label": label,
        "ProgramArguments": list(args),
        "StartCalendarInterval": dict(calendar),
        "RunAtLoad": False,
        "WorkingDirectory": str(workdir),
        "StandardOutPath": str(logs / f"{label}.out.log"),
        "StandardErrorPath": str(logs / f"{label}.err.log"),
        "ProcessType": "Background",
    }
    if env:
        d["EnvironmentVariables"] = dict(env)
    return plistlib.dumps(d).decode("utf-8")


def plans(cfg, python_bin, workdir):
    """从 config 生成全部任务定义。"""
    dh, dm = _hm(cfg.get("schedule.compute_daily", "06:00"))
    rh, rm = _hm(cfg.get("schedule.compute_retry", "07:00"))
    wh, wm = _hm(cfg.get("schedule.compute_weekly", "06:30"))
    ph, pm = _hm(cfg.get("schedule.push_time", "08:00"), (8, 0))
    wd = WEEKDAYS.get(str(cfg.get("schedule.weekly_day", "tuesday")).lower(), 2)

    env = None
    # provider=api 时才把 key 注进任务环境；claude_cli 模式下绝不能带，
    # 否则 Claude Code 会静默改用 API 计费而不是订阅。
    if cfg.get("llm.provider", "claude_cli") == "api":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            env = {"ANTHROPIC_API_KEY": key}

    return [
        {"label": f"{PREFIX}-compute-daily",
         "args": [python_bin, str(Path(workdir) / "run_daily.py")],
         "calendar": {"Hour": dh, "Minute": dm}, "env": env},
        {"label": f"{PREFIX}-compute-retry",
         "args": [python_bin, str(Path(workdir) / "run_daily.py")],
         "calendar": {"Hour": rh, "Minute": rm}, "env": env},
        {"label": f"{PREFIX}-compute-weekly",
         "args": [python_bin, str(Path(workdir) / "run_weekly.py")],
         "calendar": {"Weekday": wd, "Hour": wh, "Minute": wm}, "env": env},
        {"label": f"{PREFIX}-notify",
         "args": [python_bin, str(Path(workdir) / "run_notify.py")],
         "calendar": {"Hour": ph, "Minute": pm}, "env": None},
    ]


def apply(cfg, python_bin, workdir, dry_run=False, skip_missing=True):
    """写 plist 并重载。返回已处理的 label 列表。"""
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    (Path(workdir) / "logs").mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    done = []
    for p in plans(cfg, python_bin, workdir):
        script = Path(p["args"][1])
        if skip_missing and not script.exists():
            print(f"  ⏭  {p['label']}：{script.name} 还不存在，跳过")
            continue
        xml = render_plist(p["label"], p["args"], p["calendar"],
                           workdir, env=p.get("env"))
        target = LAUNCH_AGENTS / f"{p['label']}.plist"
        if dry_run:
            print(f"--- {target}\n{xml}")
            done.append(p["label"])
            continue
        target.write_text(xml, encoding="utf-8")
        # bootout 允许失败（首次安装时本来就没加载过）
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{p['label']}"],
                       capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"  {'✅' if ok else '❌'} {p['label']} "
              f"{p['calendar']}{'' if ok else '  ' + r.stderr.strip()[:120]}")
        done.append(p["label"])
    return done


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sw.config import CFG
    dry = "--dry-run" in sys.argv
    apply(CFG, sys.executable, str(Path(__file__).resolve().parent.parent),
          dry_run=dry)
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python3 tests/test_schedule.py
```

预期：`✅ 全部通过`

- [ ] **Step 6: 干跑看生成的 plist**

```bash
python3 sw/schedule.py --dry-run
```

预期：打印四个 plist 的 XML。**人工核对**：`StartCalendarInterval` 的时间对不对、
`claude_cli` 模式下有没有混进 `ANTHROPIC_API_KEY`。

- [ ] **Step 7: 真装上去**

```bash
python3 sw/schedule.py
launchctl list | grep stockwatch
```

预期：列出已加载的任务（`run_weekly.py` 尚未创建，那一条会被跳过，属正常）。

- [ ] **Step 8: 端到端演练**

```bash
# 清掉今天的队列重来一遍
python3 run_daily.py --skip-ingest --force
python3 run_notify.py --dry-run     # 先干跑确认内容
python3 run_notify.py               # 真发，手机应该收到
```

预期：手机收到一条 `StockWatch <日期> 异动 N/M`。

- [ ] **Step 9: 验证失败上报也能工作**

```bash
python3 -c "
import sys, tempfile; sys.path.insert(0,'.')
from sw.store import Store
from sw.config import CFG
from sw import notify as NT
tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False); tmp.close()
st = Store(tmp.name)                    # 空库 = 模拟计算任务硬崩溃
r = NT.drain(st, CFG, dry_run=True)
assert r['failure_reported'], '空队列没有上报失败！'
print('✅ 失败上报正常')"
```

- [ ] **Step 10: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

预期：此时共八个测试文件；做完 Task 11 后是九个。

---

### Task 11: 观察池增量事件

补上 spec 每日流程的第 6 步。**只报变化，不算评分** —— 完整评分卡是 P4。

**Files:**
- Create: `sw/analysis/watchlist.py`
- Modify: `run_ingest.py`（抓取范围加 `13F-HR`）
- Modify: `run_daily.py`（`build_context` 里填 `watchlist_events`）
- Test: `tests/test_watchlist.py`

**Interfaces:**
- Consumes: `reddit_rank` 表（P1 已每天入库，含 `rank` / `rank_24h_ago`）、`edgar_filings` 表（form=`4`）
- Produces:
  - `EXCLUDE_TOP_N = 10`、`JUMP_INTO = 20`、`JUMP_FROM = 50`
  - `reddit_jumps(store, d, source="all-stocks") -> list[dict]`
  - `insider_buy_clusters(store, d, days=3, min_filers=2) -> list[dict]`
  - `collect(store, d, held_tickers) -> list[str]` 汇总成日报用的人话列表

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_watchlist.py`：

```python
#!/usr/bin/env python3
"""观察池增量事件的测试。运行：python3 tests/test_watchlist.py

规则来自 HANDOFF「三个信号源的正确用法」，这里逐条锁死：
  - Reddit 绝对排名前 10 的**直接排除** —— 那时候你是接盘方
  - 有价值的是变化率：从 50 名开外冲进前 20
  - Form 4 只认「多个申报人集中买入」，单笔忽略
"""
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw.store import Store
from sw.analysis import watchlist as WL

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def fresh_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    return Store(tmp.name)


def seed_reddit(st, d, rows):
    """rows: [(ticker, rank, rank_24h_ago)]"""
    st.upsert_many("reddit_rank",
                   ["d", "source", "ticker", "rank", "mentions", "upvotes",
                    "rank_24h_ago", "mentions_24h_ago"],
                   [(d, "all-stocks", t, r, 100, 50, r24, 40)
                    for t, r, r24 in rows])


def test_reddit_jump_detected():
    print("
Reddit 排名跃升")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [
        ("JUMPY", 12, 78),     # 78 → 12：从 50 名开外冲进前 20 ✓
        ("STEADY", 25, 27),    # 几乎没动 ✗
        ("SLOW", 45, 60),      # 进步了但没进前 20 ✗
    ])
    got = {x["ticker"] for x in WL.reddit_jumps(st, "2026-08-27")}
    check("只有 JUMPY 入选", got, {"JUMPY"})
    st.close()


def test_reddit_top10_excluded():
    print("
绝对排名前 10 直接排除（那时候你是接盘方）")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [
        ("HOT", 3, 80),        # 冲得猛，但已经是第 3 名 → 排除
        ("OK", 15, 70),        # 进前 20 但不在前 10 → 保留
    ])
    got = {x["ticker"] for x in WL.reddit_jumps(st, "2026-08-27")}
    check("HOT 被排除", "HOT" in got, False)
    check("OK 保留", "OK" in got, True)
    st.close()


def test_reddit_missing_prior_rank_is_skipped():
    print("
没有前值时不猜")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [("NEW", 15, None)])
    check("跳过", WL.reddit_jumps(st, "2026-08-27"), [])
    st.close()


def test_insider_cluster_needs_multiple_filers():
    print("
Form 4：单笔忽略，多个申报人才算 cluster")
    st = fresh_store()
    rows = []
    # ABCD 三个不同 accession（视作三个申报）→ 算 cluster
    for i in range(3):
        rows.append((f"acc-a{i}", "2026-08-26", "ABCD", "111", "4", "",
                     "u", json.dumps({"company": "ABCD"}), "2026-08-27"))
    # WXYZ 只有一笔 → 忽略
    rows.append(("acc-w0", "2026-08-26", "WXYZ", "222", "4", "",
                 "u", json.dumps({"company": "WXYZ"}), "2026-08-27"))
    st.upsert_many("edgar_filings",
                   ["accession", "filed_at", "ticker", "cik", "form",
                    "items", "url", "raw_json", "seen_at"], rows)
    got = {x["ticker"] for x in WL.insider_buy_clusters(st, "2026-08-27",
                                                        days=3, min_filers=2)}
    check("ABCD 入选", "ABCD" in got, True)
    check("WXYZ 不入选", "WXYZ" in got, False)
    st.close()


def test_collect_marks_held_tickers():
    print("
已持仓的票要标出来 —— 含义完全不同")
    st = fresh_store()
    seed_reddit(st, "2026-08-27", [("NVDA", 15, 70), ("OTHER", 18, 66)])
    lines = WL.collect(st, "2026-08-27", held_tickers={"NVDA"})
    joined = "\n".join(lines)
    check("NVDA 标了「已持仓」", "已持仓" in joined, True)
    check("两条都在", len(lines) >= 2, True)
    st.close()


if __name__ == "__main__":
    test_reddit_jump_detected()
    test_reddit_top10_excluded()
    test_reddit_missing_prior_rank_is_skipped()
    test_insider_cluster_needs_multiple_filers()
    test_collect_marks_held_tickers()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python3 tests/test_watchlist.py
```

预期：`ImportError: cannot import name 'watchlist'`

- [ ] **Step 3: 写实现**

创建 `sw/analysis/watchlist.py`：

```python
"""
观察池增量事件 —— 只报变化，不算评分（完整评分卡是 P4）。

三个信号源的用法按 HANDOFF 锁死，不许改：

**Reddit**：绝对排名前 10 的**直接排除** —— 榜单前十的时候你是接盘方。
有价值的只有变化率：从 50 名开外冲进前 20。单独用价值接近零，
必须与基本面交叉验证，所以这里只把它当「值得看一眼」的线索，不打分。

**Form 4**：滞后仅 1–2 个交易日，是最快的信号。但研究显示申报后 5 日
超额收益 +1.0%，**63 个交易日后中位数转为 −3.6%** ——
它是发现线索的触发器，不是长期持有的理由。这句话要出现在报告里。

**13F**：滞后 45 天，只认「≥2 家基金同季新建仓」的 cluster。
本期只把 13F-HR 加进抓取范围让数据攒起来，cluster 检测属于 P4。
"""

EXCLUDE_TOP_N = 10      # 绝对排名进前 10 的直接排除
JUMP_INTO = 20          # 冲进前 20 才算跃升
JUMP_FROM = 50          # 且此前在 50 名开外

FORM4_NOTE = ("内部人申报后 5 日超额收益中位数约 +1.0%，"
              "但 63 个交易日后转为约 −3.6% —— 这是线索触发器，不是持有理由")


def reddit_jumps(store, d, source="all-stocks"):
    """讨论热度跃升的票。返回 [{"ticker","rank","prev","delta"}]。"""
    rows = store.q(
        "SELECT ticker, rank, rank_24h_ago FROM reddit_rank "
        "WHERE d=? AND source=? AND rank IS NOT NULL", (d, source))
    out = []
    for r in rows:
        rank, prev = r["rank"], r["rank_24h_ago"]
        if prev is None:
            continue                       # 没有前值就不猜
        if rank <= EXCLUDE_TOP_N:
            continue                       # 已在前 10 → 你是接盘方
        if rank <= JUMP_INTO and prev > JUMP_FROM:
            out.append({"ticker": r["ticker"], "rank": rank,
                        "prev": prev, "delta": prev - rank})
    return sorted(out, key=lambda x: -x["delta"])


def insider_buy_clusters(store, d, days=3, min_filers=2):
    """
    最近几天里有多个 Form 4 申报的票。

    ⚠️ 本期只按**申报笔数**判断，不解析买卖方向和金额 ——
    edgar_filings 表存的是申报元数据，不含交易明细。
    解析明细属于 P4，届时要区分买入/卖出和 10b5-1 预设计划。
    报告措辞因此只说「内部人申报集中」，不说「内部人买入」。
    """
    from datetime import date, timedelta
    lo = (date.fromisoformat(d) - timedelta(days=days)).isoformat()
    rows = store.q(
        "SELECT ticker, COUNT(DISTINCT accession) n FROM edgar_filings "
        "WHERE form='4' AND ticker<>'' AND filed_at>=? AND filed_at<=? "
        "GROUP BY ticker HAVING n>=? ORDER BY n DESC", (lo, d, min_filers))
    return [{"ticker": r["ticker"], "filings": r["n"]} for r in rows]


def collect(store, d, held_tickers=None):
    """汇总成日报里那几行人话。已持仓的要标出来 —— 含义完全不同。"""
    held = held_tickers or set()
    out = []

    for x in reddit_jumps(store, d)[:3]:
        tag = "（已持仓）" if x["ticker"] in held else ""
        out.append(f"{x['ticker']}{tag} 社区讨论量从第 {x['prev']} 名升至第 {x['rank']} 名")

    cl = insider_buy_clusters(store, d)[:3]
    for x in cl:
        tag = "（已持仓）" if x["ticker"] in held else ""
        out.append(f"{x['ticker']}{tag} 近 3 日有 {x['filings']} 份 Form 4 内部人申报")
    if cl:
        out.append(FORM4_NOTE)

    return out
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python3 tests/test_watchlist.py
```

预期：`✅ 全部通过`

- [ ] **Step 5: 把 13F-HR 加进抓取范围**

修改 `run_ingest.py`，找到这一行：

```python
        eres = E.fetch_filings(CFG.get("identity.sec_email"), forms=("8-K","4"))
```

改成：

```python
        # 13F-HR 本期不做 cluster 检测（属 P4），但先把数据攒起来 ——
        # 和 Reddit 同理，早一天开始攒就早一天能回看
        eres = E.fetch_filings(CFG.get("identity.sec_email"),
                               forms=("8-K", "4", "13F-HR"))
```

- [ ] **Step 6: 接进 run_daily.py**

修改 `run_daily.py` 的 `build_context`，把这一行：

```python
        "watchlist_events": [],      # P4 填充；本期留空
```

改成：

```python
        "watchlist_events": _watchlist(store, d, {h.ticker for h in p.holdings}),
```

并在文件顶部的 import 区加上 `watchlist as WL`：

```python
from sw.analysis import portfolio as PF, attribution as AT, causes as CS, watchlist as WL
```

再在 `build_context` 上方加这个小包装（数据源失败不中断整个任务）：

```python
def _watchlist(store, d, held):
    try:
        return WL.collect(store, d, held)
    except Exception as e:
        log(f"  ⚠️ 观察池汇总失败：{type(e).__name__}: {e}")
        return []
```

- [ ] **Step 7: 真跑一次看观察池有没有内容**

```bash
python3 run_daily.py --dry-run --skip-ingest | sed -n '/观察池/,/^$/p'
```

预期：列出 Reddit 跃升和 Form 4 申报集中的票。
**如果是空的**，先确认库里今天有 `reddit_rank` 数据：

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from sw.store import Store; from sw.config import CFG
st = Store(CFG.db_path)
print(st.q('SELECT d, COUNT(*) n FROM reddit_rank GROUP BY d ORDER BY d DESC LIMIT 3'))"
```

只有一天的快照是正常的 —— `rank_24h_ago` 由 ApeWisdom 直接提供，一天也能算跃升。

- [ ] **Step 8: 跑全量测试确认绿**

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t" | tail -2; done
```

预期：九个测试文件全部 `✅ 全部通过`。

---

## 完成标准

全部任务做完后，以下每一条都应成立：

- [ ] `for t in tests/test_*.py; do python3 "$t"; done` 全绿
- [ ] `launchctl list | grep stockwatch` 列出 compute-daily / compute-retry / notify
- [ ] `python3 run_daily.py --skip-ingest --force && python3 run_notify.py` 手机收到推送
- [ ] 推送正文里没有任何 `$` 金额
- [ ] 日报里残差 < 2σ 的持仓**不出现**，且有「其余 N 只无异常」的交代
- [ ] 空库跑 `run_notify.py` 会推「今天没跑成」
- [ ] `reports/daily_<日期>.md` 已生成，`outbox` 表有对应记录且 `sent_at` 非空
- [ ] 日报里有「观察池」一节，且 Reddit 绝对排名前 10 的票**不出现**在里面
- [ ] `edgar_filings` 表里开始出现 `form='13F-HR'` 的行（数据攒起来了，检测留给 P4）

## 交付后的第一个观察窗口

连续跑一周，每天检查：

1. **异动比例是否合理** —— 26 只里每天有 1–4 只异动是正常的。
   如果天天十几只，说明 σ 估计有问题（大概率是 Task 4 Step 3 里「除当日外算 σ」那段）。
2. **「未找到明确原因」的比例** —— 超过一半说明找原因的链路太弱，
   需要在 P4 里补数据源，而不是让 LLM 去猜。
3. **推送有没有变成噪音** —— 如果开始习惯性忽略，就该提高 `ANOMALY_Z` 阈值。

这三条的观察结果决定 P4 怎么做。
