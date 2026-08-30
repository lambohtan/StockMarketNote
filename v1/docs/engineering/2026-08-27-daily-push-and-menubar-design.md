# 设计：每日推送链路 + 菜单栏应用

2026-08-27 · 覆盖需求 #2 #3 #4 #5 #6 #7 #10
前置：[HANDOFF.md](../history/HANDOFF.md)（P1/P2 已完成）· [02-系统设计.md](../history/02-系统设计.md)

---

## 0. 这一版的优先级（用户原话）

> 「最关键还是发送信息到我手机每天。我不会每天打开这个 app。」

**推送是产品本体，菜单栏应用是附属品。** 这句话重排了整个路线：
先把推送内容做厚（P3→P4→P5），菜单栏应用最后做。

---

## 1. 已敲定的决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 面板形态 | 本地 FastAPI，**默认浏览器打开**（不内嵌 WebView） | 少一层依赖；用户不常开，不值得为"像个应用"付packaging 风险 |
| 成品度 | 双击即用的 `.app`，开机自启，**不做公证** | 只给自己用 |
| 打包方式 | **不用 py2app**。瘦 `.app` 壳 + 独立 venv + 版本目录符号链接 | 沿用本机 RentWatch 已验证的方案；py2app 冻结 pandas/numpy/PyObjC 是已知的坑 |
| 推送内容 | **只放代码和涨跌，不出现金额** | ntfy.sh 是公共服务器，内容明文经过 |
| LLM 认证 | adapter 双路，默认 `claude -p`，config 可切 API key | 订阅路线不额外花钱；留逃生口 |
| 实现顺序 | P3 → P4 → P5 → 菜单栏应用 | 见 §0 |
| **算与发分离** | 06:00 算完写进 outbox，08:00 单独的任务负责发 | 见 §2.1 —— 顺带解决了硬崩溃检测和半根 K 线两个问题 |
| 推送时间 | 每天 08:00 本机时间，跟随系统时区 | launchd 按墙钟触发，换时区自动跟随 |
| 周报 | 周二 06:30 算，与日报共用 08:00 那个推送出口 | 推送出口只有一个，改时间只动一个 plist |
| 约束表述 | 禁**指令性句式**，不禁词汇 | 用户澄清：「给我一个方向，不能要求我做什么」 |

---

## 2. 架构

**三个 launchd 任务 + 一个菜单栏应用。任务之间通过 SQLite 的 outbox 表解耦。**

```
com.lambo.stockwatch-compute-daily     每天 06:00
└─ run_daily.py       抓数 → 归因 → 恶化扫描 → 观察池增量 → LLM 摘要 → 日报
                      结果写 reports / alerts / outbox（未发送）
                      ⚠️ 这一步不推送

com.lambo.stockwatch-compute-weekly    周二 06:30
└─ run_weekly.py      评分卡排序 → 正反两面 → 对冲缺口 → 周报 → outbox（未发送）

com.lambo.stockwatch-notify            每天 08:00      ★ 唯一的推送出口
└─ run_notify.py      drain outbox 里所有 sent_at IS NULL 的条目 → ntfy → 标记已发
                      outbox 里今天什么都没有 → 推「今天没跑成」

com.lambo.stockwatch-agent             开机自启（RunAtLoad + open -g，抄 RentWatch）
└─ StockWatch.app  (LSUIElement=1，无 Dock 图标)
     └─ menubar.py (rumps)
          ├─ 图标：绿=今天跑过 / 黄=有 L2 / 红=有 L1 / 灰=今天没跑或失败
          ├─ 今日摘要（菜单里直接显示两三行）
          ├─ 打开面板 → 懒启动 uvicorn → open http://127.0.0.1:8787
          ├─ 立即运行一次
          └─ 退出
```

### 2.1 为什么把「算」和「发」拆开

这是本设计里最重要的一个结构决定，它一次解决四个问题：

1. **失败检测从异常处理变成结构性检测。**
   `try/except` 抓不到硬崩溃 —— OOM、被 kill、段错误、Python 解释器自己挂掉。
   但一个独立的看门狗任务照样会发现「该有的东西没有」。
   08:00 的 notify 任务看到 outbox 里今天没有 daily 条目，就推「今天没跑成 + 最后成功是哪天」。
2. **06:00 在开盘前**（美股本机时区 06:30 开盘），
   那时不存在当天的半根 K 线，§7 那个数据污染问题从根上消失。
3. **两小时缓冲。** 06:00 挂了可以 07:00 自动重试一次，
   用户 08:00 收到的仍然是正常报告，完全无感。
4. **改推送时间只动 notify 一个 plist**，计算时间不受影响，反之亦然。

### 2.2 outbox 表

```sql
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT,
  kind TEXT,           -- daily | weekly | l1 | failure
  priority TEXT,       -- ntfy 优先级：low / default / high / urgent
  title TEXT,
  body TEXT,
  sent_at TEXT,        -- NULL = 未发送
  attempts INTEGER DEFAULT 0,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_unsent ON outbox(sent_at, created_at);
```

原本散在 `alerts.pushed` 上的标志收敛到这里。白拿三件事：
发送失败自动重试、完整的推送审计记录、重复推送去重。

### 2.3 Mac 睡过头的竞态

launchd 唤醒后会补跑错过的任务，**但不保证顺序**。
如果 Mac 从 06:00 睡到 09:00，唤醒后 compute 和 notify 几乎同时补跑，
notify 可能先执行 → 看到空 outbox → 误报「今天没跑成」。

堵法：**`run_daily.py` 跑完后，若当前时间已过推送点，自己 drain 一次**；
没到点就留给 notify 任务。一行判断。

```python
if now_local().time() >= cfg.daily_push_time:
    notify.drain(store)     # 和 run_notify.py 调的是同一个函数
```

注意是**同一个函数**，不是复制一份逻辑 —— 否则又变成两套各错各的。

### 三条不可让步的约束

1. **调度只能是 launchd。** macOS 休眠时 cron 静默跳过；菜单栏应用自己计时则退出即停摆。
2. **应用不做任何计算。** 只读 SQLite、只 spawn 与命令行完全相同的脚本。
   「菜单栏点一下」和「凌晨自动跑」必须是同一条代码路径，否则会变成两套逻辑各错各的。
3. **FastAPI 懒启动。** 点「打开面板」才起 uvicorn 线程，之后常驻直到退出。

---

## 3. 每日流程（推送到手机，30 秒读完）

```
1  ingest            Form 4 / 13F / Reddit / 行情 全部每天入库（P1 已完成）
2  归因              每只持仓 60 日回归 = α + β_mkt·SPY + β_sector·行业ETF
3  过滤              残差 < 2σ 的持仓根本不进报告
4  找原因            残差 ≥ 2σ → 8-K → 财报日历 → 新闻 → 分析师 → 同行读数
                     都找不到就写「未找到明确原因」，不编故事
5  恶化扫描          三级分类，写 alerts 表
6  观察池增量        只报变化，不重算全部评分（详见下）
7  LLM 摘要          全流程唯一用 LLM 的地方
8  渲染 + 推送       日报一条 + 每个 L1 单独一条高优先级
```

### 为什么三个源都必须天天抓，但只有一部分天天报

| 源 | 更新节奏 | 每天抓？ | 每天报？ |
|---|---|---|---|
| Form 4 | 滞后 1–2 个交易日，天天有新的 | ✅ | ✅ 新的集中买入 |
| 13F | 滞后 45 天，季度申报，一年只在 2/5/8/11 月中旬爆发四次 | ✅ 必须天天查才不会错过那几天 | ✅ 新 cluster（≥2 家同季新建仓） |
| Reddit | ApeWisdom 无历史，**今天不存就永远没有** | ✅ 硬约束 | ✅ 排名跃升（50 名外→前 20） |
| 评分卡 | 一天动不了几分 | — | ❌ 放周报 |

### 三个信号源的用法（锁死，不许改）

- **Form 4**：申报后 5 日超额收益 +1.0%，**63 个交易日后中位数 −3.6%**
  → 是发现线索的**触发器**，不是持有理由
- **13F**：只认「≥2 家基金同季新建仓」的 cluster，单家忽略
- **Reddit**：**绝对排名前 10 的直接排除**（那时用户是接盘方）。
  有价值的只有变化率，且必须与基本面交叉验证，单独用价值接近零

---

## 4. 每周流程（周末，深度）

```
9   股票池完整评分卡排序        关注度 25 / 趋势 30 / 基本面 30 / 组合契合 15
10  头部候选正反两面（LLM）      强制含空头论点 —— 只给多头论点的不是分析，是推销
11  组合缺口 → 候选 → 边际风险贡献配比
```

**评分卡权重是拍脑袋定的，未经回测。代码里必须标注，报告里必须标注。**

### 需求 #4 的输出形式如何守住产品约束

给的是「加进去之后你的组合会变成什么样」，不是「买它」：

> 你科技敞口 65.8%，有效独立赌注数 2.15。
> 以下候选与你现有持仓的 60 日相关性低于 0.3。
> 若各配 3%，你的组合年化波动率从 56.7% 变为 X%，有效独立赌注数从 2.15 变为 Y。

陈述事实和后果，决策权 100% 在用户。

---

## 5. 组件

`★` = 本期新增，其余复用 P1/P2。

```
run_daily.py                 ★ 每日计算（06:00），只写 outbox 不推送
run_weekly.py                ★ 周报计算（周二 06:30），只写 outbox 不推送
run_notify.py                ★ 推送出口（08:00），drain outbox
menubar.py                   ★ rumps 菜单栏进程
webui/app.py                 ★ FastAPI 面板（懒启动）
sw/
├── analysis/attribution.py  ★ 归因引擎 —— P3 核心
├── analysis/scoring.py      ★ 评分卡（P4）
├── analysis/candidates.py   ★ 股票池三源汇总去重（P4）
├── analysis/hedge.py        ★ 缺口诊断 + 边际风险贡献配比（P5）
├── alerts.py                ★ 三级恶化扫描 + 提醒格式
├── notify.py                ★ ntfy 推送 + outbox drain（被 run_notify 和 run_daily 共用）
├── schedule.py              ★ 渲染并重载 plist（被 CLI 和设置窗口共用）
├── outbox.py                ★ 推送队列读写
├── llm.py                   ★ LLM adapter（双路）
└── daily_report.py          ★ 日报/周报渲染
packaging/
├── StockWatch.app/          ★ 瘦壳，Contents/MacOS/stockwatch 是 shell 脚本
├── com.lambo.stockwatch-compute-daily.plist
├── com.lambo.stockwatch-compute-weekly.plist
├── com.lambo.stockwatch-notify.plist
├── com.lambo.stockwatch-agent.plist
└── install.sh               ★ 建 venv、装依赖、软链切换、load plist
```

### 部署布局（抄 RentWatch）

```
~/Library/Application Support/StockWatch/
├── StockWatch.app
├── releases/release-<时间戳>/     代码
├── venvs/venv-<时间戳>/           独立 venv
├── current      → releases/...    符号链接原子切换
├── current-venv → venvs/...
├── data/stockwatch.db             ⚠️ 历史快照，永不删
├── logs/
└── config.yaml
```

---

## 6. LLM adapter（`sw/llm.py`）

```yaml
llm:
  provider: "claude_cli"     # 或 "api"
  model: "claude-opus-5"     # provider=api 时用
  max_tokens: 2000
```

### `claude_cli` 路线（默认）

调 `claude -p`，走 Pro 订阅。**四个坑**：

1. **不能加 `--bare`** —— bare 模式不读订阅登录，会要求 API key
2. **环境里绝不能有 `ANTHROPIC_API_KEY`** —— 一旦存在，Claude Code 改用 API 计费，
   而且是静默的。adapter 启动时若检测到该变量与 `provider: claude_cli` 并存，
   **必须报错退出**，不能默默烧钱
3. **launchd 不继承 shell PATH** —— plist 里写死 `/Users/lambo/.local/bin/claude` 的全路径
4. **cwd 决定加载什么上下文** —— 不加 `--bare` 时会读当前目录的 `CLAUDE.md`、hooks、
   skills、MCP。必须在一个**专用的空目录**里跑，指令通过 `--append-system-prompt` 传，
   否则会把整个开发环境的上下文和 token 成本带进来

调用形态：
```bash
claude -p "<材料>" \
  --append-system-prompt "<摘要指令>" \
  --output-format json --json-schema '<schema>'
```
不授予任何工具权限 —— 纯文本进，纯文本出。用 `--json-schema` 约束输出形状。

### `api` 路线

API key **只写在两个 compute plist 的 `EnvironmentVariables` 里**
（notify 任务不调 LLM，不需要），作用域仅限这些任务。**绝不写进 `~/.zshrc`** —— 那会让用户交互式使用 Claude Code
也变成按量付费。

---

## 7. 推送设计

全部经由 outbox，08:00 由 notify 任务统一发出：

| kind | Priority | 内容 |
|---|---|---|
| `daily` | default | 异动（代码 + 涨跌 + 归因拆解 + 原因）+ 观察池增量 |
| `l1` | urgent | 固定六段格式，见 §8。每条 L1 单独一条通知 |
| `weekly` | low | 一句话摘要 + 指向面板（仅周二） |
| `failure` | high | 「今天没跑成 + 最后一次成功是哪天 + 哪一步挂了」 |

**L1 是否即时推送**：默认**否** —— 统一在 08:00 随其他条目发出。
L1 的依据是上一交易日的收盘数据和 8-K，不是突发新闻，06:00 叫醒用户没有意义。
留一个开关 `notify.l1_immediate`（默认 `false`）给以后加盘中轮询时用。

### 内容红线

推送里**不出现**：金额、成本、仓位占比、账户总市值。
只出现：代码、百分比涨跌、σ 值、原因、事件类型。

理由：ntfy.sh 是公共服务器，topic 名是随机串（隐蔽性），不是加密。
知道用户在看 NVDA 是一回事，知道账户里有多少钱是另一回事。

Markdown 渲染在 iOS 客户端不保证，**正文按纯文本设计**。

### 推送时间与时区

| 任务 | 时间 | launchd 键 |
|---|---|---|
| compute-daily | 每天 **06:00** | `StartCalendarInterval{Hour:6, Minute:0}` |
| compute-daily 重试 | 每天 **07:00**（当天已成功则空跑退出） | `StartCalendarInterval{Hour:7, Minute:0}` |
| compute-weekly | **周二 06:30** | `StartCalendarInterval{Weekday:2, Hour:6, Minute:30}` |
| **notify** | 每天 **08:00** ← 用户能感知的唯一时间 | `StartCalendarInterval{Hour:8, Minute:0}` |

周二 08:00 那次 drain 会把日报和周报**一起发出去（两条通知）**，不需要单独的周报推送任务。

- 本机时区当前为 `America/Los_Angeles`。**launchd 的 `StartCalendarInterval`
  按本机墙钟时间触发**，所以用户换时区/出差，推送时间自动跟随 —— 这正是
  「读取系统时区」的需求，不需要写代码。
- 周二的 compute-weekly 排在 06:30，与 06:00 的 compute-daily 错开 30 分钟，
  避免两个写任务同时抢 SQLite 写锁。
- 报告正文里的市场时间**一律标注 ET**（美股时段固定 09:30–16:00 ET），
  同时给出本机时间换算，避免用户换时区后误读。

### 时段换算与半根 K 线（已降级为兜底）

美股在本机时区（`America/Los_Angeles`）是 **06:30–13:00 PDT**。

**计算跑在 06:00，也就是开盘前 30 分钟** —— 那时当天的日线 bar 还不存在，
数据天然干净。这是 §2.1 拆分带来的附带好处。

但仍然保留防御规则，因为盘前交易在某些情况下可能产生当日 bar，
而且以后如果有人把计算时间改到盘中，这条是唯一的防线：

> **丢弃任何日期等于「当前 ET 日期」、且该交易日尚未收盘（ET < 16:00）的 bar。**

把半天当一天喂进 60 日回归会污染 β 和残差 σ，**而且是静默的** —— 不会报错，
只会让结论悄悄变歪。所以这条要写成测试：
构造一个含当日半根 bar 的序列 → 断言被剔除。

报告正文里的市场时间**一律标注 ET**，同时给出本机时间换算，
避免用户换时区后误读。

### 设置窗口如何改时间

用户要求能在软件里改推送时间。实现方式：

```
config.yaml  schedule.daily_time / schedule.weekly_day / schedule.weekly_time
     ↓
sw/schedule.py  ── 渲染 plist ── launchctl bootout → bootstrap 重载
```

`sw/schedule.py` 从第一天就写成**可被 CLI 和 UI 共同调用的函数**，
菜单栏应用还没做的时候用命令行调，做好之后设置窗口调同一个函数。
不要等做 UI 时再补 —— 那会变成两套逻辑。

### 失败必须主动上报

用户不会每天开 app，所以**静默失败比报错危险得多**。
- 任何阶段异常都不中断后续，记进 `source_health`
- 但整体失败要主动推送 —— 没有消息不等于没事
- 连推送都发不出去时图标转灰，下次成功时补发「昨天失败了」

---

## 8. 提醒格式（固定六段，不许变）

```
发生了什么   →  数据  →  这类信号通常意味着什么  →  反面观点
→  你的持仓现状  →  接下来看什么
```

最后一段是关键：它把用户从「要不要卖」这个二选一，
转成「再收集三个信息」—— 这几乎总是更好的决策姿势。

---

## 9. 测试策略

### 把产品硬约束变成会报错的测试

约束的本意（用户原话）：**「整个软件只是给我一个方向，不能要求我做什么。」**

判别标准是**这句话在描述世界，还是在指挥用户** —— 描述可以，指挥不行。
「卖」这个字本身不禁，禁的是让用户去卖。CLAUDE.md 已按此重述。

```python
# 祈使 / 建议 / 假精确 —— 一律禁止
BANNED = ["建议买入", "建议卖出", "建议持有", "你应该", "你需要", "请立即",
          "赶紧", "务必", "该减仓", "该清仓", "止损设在", "目标价", "买入价"]
assert not any(p in body for p in BANNED)

# 客观陈述 —— 必须放行，这几条是回归测试的正样本
ALLOWED = ["内部人集中卖出",
           "这类信号历史上后续 6 个月出现财务重述的比例高于基准",
           "加入后你的组合波动率从 56.7% 变为 X%"]
assert all(is_accepted(p) for p in ALLOWED)

assert not re.search(r"\$[\d,]+", push_body)   # 推送里不出现金额
assert "接下来看什么" in alert_body              # 六段格式最后一段必须在
```

`ALLOWED` 那三条比 `BANNED` 更重要 —— 只测「禁了什么」很容易滑向一个
什么都不敢说的系统，那就失去价值了。正负样本都要测。

### 数值校准

沿用 `tests/test_analysis.py` 已确立的规矩：
**凡是一眼看不出对错的统计量，必须有已知解析解的合成数据做锚。**

- 归因引擎：构造已知 β 和已知残差的合成序列 → 验证能检出 2σ/4σ
- 边际风险贡献：等权等相关组合有解析解 → 逐点比对
- 评分卡：不做数值校准（权重本就是拍脑袋的），但要断言分数可分解、每一分能追溯

### 推送与调度

- `--dry-run` 只打印不发送。默认在测试里用 dry-run，**绝不真发**。
- **outbox 幂等**：同一条目 drain 两次只发一次（模拟 §2.3 的补跑竞态）
- **失败检测**：构造一个空 outbox → 断言 notify 产出 `kind=failure` 的推送
- **睡过头竞态**：模拟 notify 先于 compute 执行 → 断言不会误报失败
- **半根 K 线**：构造含当日未收盘 bar 的序列 → 断言被剔除
- **plist 渲染**：改 config 的时间 → 断言生成的 plist `StartCalendarInterval` 正确

---

## 10. 刻意的取舍与推迟

| 项 | 决定 | 理由 |
|---|---|---|
| L1 盘中实时 | **不做**。06:00 检出，08:00 随其他条目一起推 | L1 依据是上一交易日收盘数据和 8-K，不是突发新闻。以后加盘中轮询任务写进同一个 outbox 即可，不影响现有结构 |
| 原 P6 网页界面 | 并入菜单栏应用，不再单独一期 | 同一个 FastAPI |
| 代码签名/公证 | 不做 | 只给自己用 |
| 内嵌 WebView | 不做 | 用户不常开，不值得付 packaging 风险 |
| P7 信号有效性追踪 | 本期不做，但 `signals` / `signal_outcomes` **从第一天就要写入** | 半年后才有数据可回看，今天不写就永远没有 |

---

## 11. 已确认（2026-08-27）

1. **「卖」字约束** —— 用户澄清本意是「给方向，不要求我做什么」，
   与词汇无关。CLAUDE.md 已重述，测试改为禁**祈使/建议句式**并增加正样本。见 §9。
2. **推送时间** —— 每天本机时间 08:00，跟随系统时区；软件需提供设置窗口修改。见 §7。
3. **周报** —— 周二 06:30 计算，与日报共用 08:00 的推送出口。
4. **算与发分离** —— 用户提出：06:00 算完存好，08:00 再发。已采纳，见 §2.1。
   这个改动顺带解决了硬崩溃检测、半根 K 线、失败缓冲三个问题。

## 12. 本文档未覆盖

git 仓库尚未初始化（用户明确表示暂缓），故本文档未提交版本控制。
