# StockWatch 项目交接文档

交接时间：2026-08-28 · 更新：2026-08-27 P2 完成
状态：P1 数据底座 + P2 组合分析已完成，**已用真实 Fidelity CSV 验证跑通**
接手方：编码 agent。**这份文档是自包含的，不需要之前的对话记录。**

---

## 0. 三十秒版本

给一位个人投资者做一个**每天自动运行的股票研究分析系统**。
它抓数据、算指标、发报告到手机。**它不下单，也不告诉用户买或卖。**

- 语言 Python，本地跑在 macOS，SQLite 存储，launchd 调度，ntfy 推送，LiteLLM 接大模型
- 已完成 P1（数据底座）+ P2（组合分析），下一步 P3（自动化闭环）
- 用户是 Apple 的后端 Java 工程师，技术沟通可以直接、不用铺垫

---

## 1. 用户与背景

| | |
|---|---|
| 券商 | Fidelity（**无零售 API**，靠手动导出 CSV） |
| 资金 | 卫星仓每月约 $1,000，明确定位为风险资本，追求超额收益 |
| 核心仓 | VOO 自动定投，独立运行，**不在本系统管理范围** |
| 持仓 | 美股为主，集中在科技单股 + VOO/QQQ |
| 员工持股 | 无（不持有 Apple RSU/ESPP） |
| 交易风格 | 买入持有为主，在意短期资本利得税，倾向持有超过 12 个月 |
| 明确不做 | 杠杆、期权、杠杆 ETF |

---

## 2. ⚠️ 绝对不能违反的产品原则

**这几条在之前的对话中被踩过坑并明确纠正过，请严格遵守：**

### 2.1 这是分析工具，不是交易系统，更不是规则引擎

用户明确说过："我要的不是一个自动投资软件，而是一个股票分析和提醒的 AI，只是给我做报告，不要要求我赶紧卖或者赶紧买。"

- ❌ 不要生成"建议买入"/"建议卖出"这类指令
- ❌ 不要给目标价（"建议买入价 $187.5" 是假精确）
- ❌ 不要设置任何强制约束用户行为的规则（仓位上限、持仓数量、止损线全部已废除）
- ❌ 不要在代码里内置"违反规则就阻止"的逻辑
- ✅ 输出的是**事实、数据、正反两面的论点**，决策权 100% 在用户

**唯一例外**：持仓出现实质性恶化时明确提醒（用户说"如果我持股可以卖点止损都是好的"）。
但格式必须是：**发生了什么 → 数据 → 这类信号通常意味着什么 → 反面观点 → 你的持仓现状 → 接下来该看什么**。
最后一项把用户从"要不要卖"转成"再收集三个信息"。**任何提醒里都不出现"卖"字。**

### 2.2 所有数值计算必须是确定性代码，LLM 只做最后一公里

- 指标、相关性、评分、异动检测 → 全部 Python，可复现
- LLM 只负责：① 读财报/新闻/8-K 写摘要 ② 把结构化结果翻译成人话
- 理由：换模型不影响任何数字；报告里每个数字都能追溯到具体计算，而不是"模型说的"

### 2.3 写入即快照，只追加不覆盖

每天的原始数据带日期入库，永不删除。ApeWisdom 不提供历史数据，今天不存就永远没有。
这决定了这个项目一年后是"能自我改进的系统"还是"每天发通知的玩具"。

### 2.4 用 launchd，不要用 cron

macOS 的 cron 在机器休眠时会**静默跳过**任务，不补跑不报错。
launchd 的 `StartCalendarInterval` 在唤醒后会补跑错过的任务。这是硬性要求。

### 2.5 诚实边界

系统不预测涨跌。FINSABER 研究（arXiv:2505.07078，20 年 + 100 标的系统回测）结论：
LLM 选股策略在长周期宽标的下无稳定超额收益，且牛市过度保守、熊市过度激进。
"潜力"在本系统中的定义是"在一组可复核的标准下更符合用户偏好"，不是"它会涨"。
所有评分模型都必须标注"未经回测验证，是纪律工具不是 alpha 来源"。

---

## 3. 用户的 10 条需求（原文）

1. 分析我现在持有的股票
2. 从 Form 4、13F、Reddit 热度建立一个股票池
3. 分析股票池里哪个有潜力、哪个市场好
4. 根据我现在持有的股票，给我推荐股票（**对抗风险**：科技股太多了该配点什么，怎么配比）
5. 持仓里出现可以卖掉的股票时提醒我
6. 整个操作自动完成，每天一次定时任务，ntfy 推送到手机
7. 这是一个可以挂在 macOS 上长期运行的软件
8. 可以链接任何大语言模型进行分析
9. 接入适用的 skill
10. 网页界面做设置、上传最新 Fidelity 数据

**全部评估为可行**，无需砍功能。

---

## 4. ✅ 已实测验证的事实（2026-08-27 在用户 Mac 上跑通）

环境：macOS · Python 3.13.15 · pandas 3.0.5 · numpy 2.5.2 · yfinance 1.7.0 · edgartools 5.53.0

| 能力 | 状态 | 实测结果 |
|---|---|---|
| yfinance 批量日线 | ✅ | 251 天 × 5 只，0.9 秒 |
| yfinance 行业分类 | ✅ | AAPL → Technology / Consumer Electronics |
| yfinance 新闻 | ✅ | 10 条 |
| yfinance 分析师评级 | ✅ | 4 行 |
| yfinance 季度利润表 | ✅ | 5 个季度 |
| yfinance 内部人交易 | ✅ | 76 行 |
| **yfinance 财报日历** | ❌ | **见 §5 已知问题** |
| SEC EDGAR 8-K | ✅ | 取到 5 条 |
| SEC EDGAR Form 4 | ✅ | 取到 5 条 |
| SEC EDGAR 13F-HR | ✅ | 取到 3 条 |
| ApeWisdom all-stocks | ✅ | 974 只，前三 NVDA(783)/MRVL(568)/MU(393) |
| ApeWisdom wallstreetbets | ✅ | 731 只 |
| ApeWisdom 24h 变化率字段 | ✅ | `rank_24h_ago` / `mentions_24h_ago` 都存在 |
| ntfy | ⏸ | 用户未配置 topic，未测 |
| LiteLLM | ⏸ | 用户未配置任何 API key，未测 |

### 实测的相关性与波动率（验证需求 #4 的对冲逻辑成立）

```
NVDA vs AAPL   +0.108        年化波动率：
NVDA vs XOM    -0.198          NVDA 38.0%   XOM 25.7%   AAPL 25.1%
NVDA vs KO     -0.294          KO   18.7%   SPY 12.8%
```

对冲逻辑成立。但注意 NVDA/AAPL 仅 +0.108 明显偏低（历史通常 0.4–0.6），
说明当前市场处于强分化 regime，**这些数字是时变的，不要写死任何假设**。

---

## 5. ⚠️ 已知问题

### 5.1 ~~财报日历在 pandas 3.x 下失败~~ ✅ 已解决（2026-08-27 实测）

`tools/probe_earnings.py` 跑完，**四条路全部可用**（yfinance 1.7.0 + pandas 3.0.5）：

| 方式 | 结果 |
|---|---|
| `tk.calendar` | ✅ `Earnings Date: [datetime.date(2026, 10, 29)]` —— 直接给前瞻日期，首选 |
| `tk.get_earnings_dates(limit=8)` | ✅ 25×3 DataFrame，含 EPS 预估/实际/超预期 |
| `tk.earnings_dates` | ✅ 同上 |
| `tk.info.earningsTimestamp` | ✅ |
| SEC EDGAR 8-K | ✅ |

`next_earnings()` 的四路降级不用改，第一路就通。

### 5.2 版本前沿风险

pandas 3.0 / numpy 2.5 / Python 3.13 都很新，可能还有其他兼容问题。
逃生口：把 pandas 降到 2.x。写代码时避免依赖 pandas 的边缘特性。

### 5.3 ~~Fidelity CSV 解析未经真实文件验证~~ ✅ 已验证（2026-08-27）

用户提供的真实文件放在 `stockwatch/uploads/`：

| 文件 | 结果 |
|---|---|
| `Portfolio_Positions_Aug-28-2026.csv` | ✅ 26 只股票 + SPAXX 现金全部正确解析，**跳过 0 行**，`ALIASES` 无需改动 |
| `History_for_Account_Z35501796.csv` | ⚠️ 解析正确，但**只覆盖 2026-07-30 → 08-25（11 条、20 个交易日）** |

真实列名（与之前的假设一致，别再改 ALIASES）：

```
Positions: Account number,Account name,Symbol,Description,Quantity,Last price,
           Last price change,Current value,Today's gain/loss dollar,...,
           Cost basis total,Average cost basis,Type          （每行结尾多一个逗号）
Activity:  Run Date,Action,Symbol,Description,Type,Price ($),Quantity,
           Commission ($),Fees ($),Accrued Interest ($),Amount ($),
           Cash Balance ($),Settlement Date                  （Price 在 Quantity 之前）
```

**已修的解析 bug**：`REINVESTMENT FIDELITY GOVERNMENT MONEY MARKET (SPAXX)`
命中 `BUY_KW` 里的 "REINVESTMENT"，被当成一笔买入。货币基金 sweep 不是投资决策，
混进现金流会污染 TWR。已加 `CASH_TICKERS` 前置判断，新增 `cash_sweep` 分类。

**仍待用户处理**：流水窗口太短，TWR 无意义。需要重新导出至少 12 个月。

### 5.4 SEC identity 必须是真实邮箱

SEC 要求 User-Agent 带真实联系方式，占位地址可能被限流甚至封 IP。
`config.yaml` 里已填 `lambohtan@gmail.com`。

---

## 6. 已完成的代码（P1 数据底座）

```
股票市场/
├── 00-调研报告.md                Fidelity API 调研、开源项目、数据源比价
├── 01-产品定位与偏好档案.md      ⭐ 产品边界，先读这个
├── 02-系统设计.md                归因引擎、三级恶化提醒、想法流的详细设计
├── 03-需求可行性与架构.md        10 条需求逐条评估 + 架构决策
├── HANDOFF.md                    本文件
├── tools/
│   ├── check_env.py              环境与数据源检查（已跑通，20/23）
│   └── probe_earnings.py         财报日历取数探测（待跑）
└── stockwatch/                   ⭐ 代码在这里
    ├── README.md
    ├── requirements.txt
    ├── config.yaml               基准、行业 ETF 映射、ntfy topic、LLM 模型
    ├── run_ingest.py             P1 每日抓取入口
    ├── run_portfolio.py          ⭐ P2 组合体检报告入口
    ├── reports/                  生成的 Markdown 报告
    ├── data/stockwatch.db        SQLite（历史快照全在这，不要删）
    └── sw/
        ├── config.py             YAML 配置读取（支持 STOCKWATCH_DB 环境变量覆盖）
        ├── store.py              ⭐ SQLite schema + 读写
        ├── fidelity.py           Positions CSV 容错解析
        ├── fidelity_activity.py  Activity/History CSV 解析（TWR 用）
        ├── sources/
        │   ├── base.py           SourceResult + @timed 装饰器，统一健康上报
        │   ├── prices.py         yfinance 行情（含区间取数）/元数据/财报日期
        │   ├── reddit.py         ApeWisdom
        │   ├── edgar_src.py      8-K / Form 4 / 13F
        │   └── etf.py            ETF 前十大成分 + 行业权重（穿透分析用）
        └── analysis/             ⭐ P2 组合分析层（纯确定性计算，LLM 不参与）
            ├── portfolio.py      持仓合并、权重、行业分布
            ├── risk.py           相关性/波动率/β/有效持仓数/分散化比率
            ├── overlap.py        与指数重叠度 + ETF 穿透敞口
            ├── stress.py         历史压力测试（区间收益 + 组合层最大回撤）
            ├── returns.py        简单收益 + 时间加权收益率 TWR
            └── report.py         Markdown 渲染
```

### 数据库表

`positions` `prices` `meta` `fundamentals` `reddit_rank` `edgar_filings`
`signals` `signal_outcomes` `alerts` `reports` `llm_calls` `source_health`

完整 schema 见 `sw/store.py`。**`signals` + `signal_outcomes` 是信号有效性追踪的基础，
即使 P7 才用到，从第一天就要往里写。**

---

## 7. 待办路线（P2 → P7）

| 期 | 内容 | 对应需求 | 验收标准 |
|---|---|---|---|
| ~~**P2**~~ ✅ | 组合分析 | #1 | ~~真实收益率（时间加权）、行业分布、有效持仓数 1/Σwᵢ²、两两相关性矩阵、与 QQQ 前十大重叠度、历史压力测试~~ **全部达成，见 §11** |
| **P3** | 自动化闭环 | #5 #6 #7 | 归因引擎（60 日回归拆解个股收益 = α + β_mkt·SPY + β_sector·行业ETF，残差 >2σ 触发找原因）+ 三级恶化提醒 + launchd plist + ntfy 推送 |
| **P4** ✅ | 股票池深读 | #2 #3 | 三源汇总去重 + py/LLM 双路判定 + 交叉验证倾向标签（**评分卡草案已作废，实际做法见本节下方**） |
| **P5** | 风险对冲 | #4 | 诊断 → 缺口 → 候选 → 边际风险贡献配比 |
| **P6** | Web UI | #10 | FastAPI 只监听 127.0.0.1，设置 + 上传 CSV + 报告浏览 |
| **P7** | 自我改进 | 追加 | 信号有效性追踪（30/60/90 天回看）+ 影子组合 |

**顺序理由**：P3 排在股票池之前，因为"每天自动告诉你持仓出了什么事"是最快见效的价值。
P6 放后面 —— 前期直接改 `config.yaml` 更快。

### P3 归因引擎的关键设计（已在 02 文档详述）

日报只报**异常**，不报常规波动。残差在正常范围的持仓**根本不出现在报告里** ——
日报一半的价值来自它不说什么。找原因的顺序：8-K → 财报日历 → 新闻 → 分析师调整 →
同行读数 → 都找不到就老实写"未找到明确原因"，**不编故事**。

### P4 评分卡（草案，100 分）—— ❌ 已作废

~~关注度 25（13F cluster 10 / Form 4 净买入 10 / 社区热度动能 5）~~
~~趋势 30（趋势结构 10 / 相对强度 10 / 回调买点 10）~~
~~基本面 30（增长 10 / 盈利质量 10 / 估值分位 10）~~
~~组合契合 15（相关性 5 / 重叠度 5 / 额度空间 5）~~

2026-08-28 用户明确表示这不是他要的东西：一张隐藏权重的打分卡不比
"LLM 帮我读财报和新闻、告诉我这票在我的标准下算不算符合"更有用。
**这版草案整个作废**，完整替代设计见
`docs/superpowers/specs/2026-08-28-p4-股票池深读-design.md`。

### P4 实际做了什么（已完成，2026-08-29）

入口 `run_pool.py`，独立于 `run_daily.py`，排在它之后跑（等 ingest 把
当日价格/Reddit 排名/EDGAR 申报写进库，不是为了读它的计算结果）。

1. **三个来源汇总成股票池**（`sw/deepread/pool.py`）：持仓异动（就地
   调用 `analysis/attribution.attribute()` 重算，`anomaly`/`extreme`
   两档全收、不占名额）+ ApeWisdom 热度前 20（**含前 10**，接盘风险
   改成在报告里直接列排名 + 排名变化，不再像 `watchlist.py` 那样隐藏
   排除）+ Form 4 申报集中（近 3 日 ≥2 份的票）。新票按信号强度只取
   3 只/天，持仓异动不占这个名额。
2. **材料层**：EDGAR 10-Q/10-K 的 MD&A + 风险因素两节，按 accession
   缓存，同一份申报不会重复下载（`sw/sources/filings_text.py`）；新闻
   正文抓取，抓不到就如实降级成标题、标 `full=False`（`sw/sources/
   news_full.py`）；原始数字采集——yfinance 季度营收/现金流/毛利率、
   与持仓的 60 日相关性、Form 4 申报数、热度排名，取不到一律 `None`
   不用 0 冒充（`sw/deepread/facts.py`）。
3. **双路判定 + 倾向标签**（`sw/deepread/criteria.py` + `sw/deepread/
   deepread.py`）：py 六条确定性硬指标全部用 Python 算；LLM 独立判
   一遍同样六条（仅用于对照，不参与计分）+ 自己专属的两条文本判定
   （管理层指引方向、风险因素是否新增重大项，这两条**才**参与计分）。
   最终计分是 `py 的 6 条 + LLM 文本判定的 2 条 = X/8`，py 与 LLM 的
   分歧逐条列出两边理由，不隐藏。四段固定叙述（发生了什么/多头论点/
   空头论点/接下来盯什么）由 stage2 汇总两路结论生成。全部产出标注
   「标准未经回测验证，是纪律工具不是涨跌预测」。
4. **出口**：`reports/pool_<日期>.md` 全文报告 + 一只票一条 ntfy 推送
   （`kind='pool'`，幂等 `event_key=pool:<ticker>`）；`deepread_results`
   表按天按票只追加写入，带模型名（换模型时能区分是模型变了还是公司
   变了），是 P7「信号有效性追踪」的数据基础。

**LLM 接线**：deepread 没有复用日报走的 `sw.llm.summarize()`——那条路径
无条件拼 `SYSTEM`（第 5 条「两句话以内」）、CLI schema 固定为
`{"summary": string}`，装不下 stage1 要的结构化判定和 stage2 要的四段
长文。`sw/llm.py` 新增了两个平行入口 `complete_json()`（stage1）/
`complete_text()`（stage2），`summarize()` 本身零改动，P3 日报链路的
15 个测试不受影响。

**已知限制（本期 park，下一期再做）**：

- **Form 4 全市场信号在生产里不可得**：`edgar_src.CIK_TICKER_MAP` 生产
  里是空的，`run_ingest.ingest_edgar` 全市场扫描行没有 ticker，只有按
  持仓逐个查的行才带 ticker。结果是「Form 4 申报集中」这条来源实际上
  退化成了「持仓里近 3 日有 ≥2 份申报的票」——新票永远进不了这条来源。
  根治要么填 CIK map，要么在 pool 侧对候选票按需补查 EDGAR。
- **新票的「与持仓相关性」永远 unknown**：ingest 的行情 universe 只有
  持仓 + 基准 ETF，新票没有价格历史算不出相关性；而这条标准恰恰只对
  新票才有意义（持仓票的相关性用户自己大概心里有数）。
- 两条根因相同：**P4 的 6 条标准隐含假设 EDGAR/prices 覆盖全市场，
  而 P1/P3 的 ingest 只覆盖持仓**。下一期做数据源设计时，探测清单里
  应固定包含一条「这个字段对新票可得吗」——这次是最终复审时才发现的，
  代价是这两条标准对新票系统性地记 `unknown`（缩分母，不会误判成
  「不符合」，但也没有真的判过）。
- `facts.financials()`（yfinance 季度财报数值核心）没有自动化测试直接
  覆盖——`test_facts.py` 的四条测试都注入假的 `fin` 函数，行名一旦对不
  上（比如 yfinance 改了 `"Total Revenue"` 这个 key）会永久静默为
  `None`，测试也测不出来。
- 10-K 风险因素抽取有一类已知但未彻底修的风险：MD&A 正文里
  「see Item 1A. Risk Factors」这类后置交叉引用，可能让「取最后一次
  命中」（Task 1/3 确认的必要行为，目录也会命中一次）落在引用而不是
  真实标题上。本轮只加了廉价的合理性检查（`filings_text.
  extraction_warnings()`），把可疑抽取报进 `detail`，**没有改抽取
  逻辑本身**——需要一份真实的、带交叉引用的 10-K 固件才能验证修复
  行为，本期没有这个固件，留给下一次碰这个文件的人。
- launchd 与真实 ntfy 推送尚未验收：`sw/schedule.py --dry-run` 的渲染
  校验过了，但真正的 `launchctl bootstrap` 安装、真实推送、真实 LLM
  调用需要单独授权，不在这轮代码交付范围内。

### 三个信号源的正确用法（重要）

- **Form 4**：滞后仅 1–2 个交易日，最快。但研究显示申报后 5 日超额收益 +1.0%，
  **63 个交易日后中位数转为 -3.6%** → 是发现线索的触发器，不是长期持有理由
- **13F**：滞后 45 天，只报多头股票仓位。只用"≥2 家基金同季新建仓"的 cluster 信号，单家忽略
- **Reddit**：**绝对排名前 10 的直接排除**（那时用户是接盘方）。有价值的是变化率 ——
  从 50 名开外冲进前 20。必须与基本面交叉验证，单独用价值接近零

---

## 8. 下一步立即要做的

**代码已经就绪，卡在两份真实 CSV 上。**

```bash
cd 股票市场/stockwatch

# 1. Fidelity → Accounts → Positions → 下载。先干跑核对解析
python3 run_ingest.py --dry-run --positions ~/Downloads/Portfolio_Positions_XXX.csv
python3 run_ingest.py --positions ~/Downloads/Portfolio_Positions_XXX.csv

# 2. Fidelity → Accounts → Activity → 最近 12 个月 → 下载。同样先干跑
python3 run_portfolio.py --dry-run --activity ~/Downloads/History_for_Account_XXX.csv

# 3. 出报告
python3 run_portfolio.py --activity ~/Downloads/History_for_Account_XXX.csv
```

`--dry-run` 会打印 **detected_columns（原始列名）**。如果和
`sw/fidelity.py::ALIASES` / `sw/fidelity_activity.py::ALIASES` 对不上，改这两个字典即可。
流水解析还会打印「未能分类的动作」，需要往 `BUY_KW/SELL_KW/DIV_KW/FLOW_KW` 补关键词。

---

## 9. 需求 #9（skill）的定位提醒

调研到的：[tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)（2.7k star，50+ skills，其中 5 个免 API）、
[agiprolabs/claude-trading-skills](https://github.com/agiprolabs/claude-trading-skills)（67 个）、
[yennanliu/InvestSkill](https://github.com/yennanliu/InvestSkill)。

**但这些是交互式的**（用户和 Claude 对话时用），而本系统是无人值守的定时任务。
两者不是替代关系：软件负责每天抓数算指标推送（确定性代码），skill 负责用户收到推送后想深挖时用。
**建议 P4 之后再评估，先装 50 个 skill 只会让人分不清哪个在起作用。**

---

## 10. 数据源速查

| 源 | 用途 | 成本 | 风险 |
|---|---|---|---|
| SEC EDGAR (`edgartools`) | 8-K / Form 4 / 13F | $0 | 官方，稳定。需真实邮箱 identity |
| yfinance | 行情/基本面/新闻/评级/内部人 | $0 | 非官方，随时可能变。已做适配器隔离 |
| ApeWisdom | Reddit 热度 | $0 | 无需 key。端点 `apewisdom.io/api/v1.0/filter/{all-stocks\|wallstreetbets\|stocks}/page/{n}` |
| FMP 免费档 | 备选基本面 | $0 | 250 次/天，EOD |
| Alpha Vantage | 备选行情 | $0 起 | 免费档限制紧，有官方 MCP |
| ntfy | 推送 | $0 | 有 iOS app，一条 POST 就能发 |
| LiteLLM | LLM 网关 | 按模型 | 换模型改 `config.yaml` 一行 |

---

---

## 11. P2 已完成的内容（2026-08-27）

### 入口

`run_portfolio.py` → 生成 `reports/portfolio_<日期>.md`，同时写入 `reports` 表。
参数：`--activity`（流水，算 TWR）/ `--snapshot-date` / `--corr-window`（默认 60）/
`--no-fetch`（离线）/ `--dry-run`（只校验流水解析）/ `--out`。

### 报告九节

| 节 | 内容 |
|---|---|
| 1 概览 | 市值、现金占比、账户 |
| 2 收益 | 持有期简单收益 **+** 时间加权 TWR（年化、波动率、回撤、vs SPY/QQQ） |
| 3 持仓明细 | 权重/市值/成本/浮盈亏/行业，核心仓打 ⚙️ 标记 |
| 4 集中度 | **有效持仓数 1/Σwᵢ²**、HHI、top1/3/5，全部 + 剔除核心仓两个视角 |
| 5 行业分布 | 直接持仓 / **穿透 ETF 后** / QQQ 参照，三列对比 |
| 6 相关性与波动 | 相关性矩阵、加权平均相关性、组合 σ、β、**分散化比率 DR** |
| 7 指数重叠 | vs QQQ/VOO/SPY 前十大 + **穿透后单票真实敞口** |
| 8 压力测试 | 4 个情景的区间收益 **+ 组合层最大回撤 + 最低点日期** |
| 9 局限 | 固定段落，逐条写明这份报告不能回答什么 |

### 几个刻意的设计决定

1. **TWR 的口径是股票仓，不含现金。** V=当日股票市值，CF=当日买入(+)/卖出(−)，
   r=(V−CF)/V_prev−1，连乘。价格用复权价，所以分红已隐含，现金分红不再单独计入
   （避免重复）。这样不需要重建现金余额，口径仍然干净。
2. **交易流水的股数要做拆股换算。** 复权价是按当前股数口径回溯的，
   而流水里记的是当时的股数。不换算，拆股前的持仓市值会差好几倍。
   `returns.split_factors()` 从 yfinance 取拆股记录做换算。
3. **TWR 的最大回撤算在 TWR 指数上，不是市值序列上。**
   每月定投会把市值推高，用市值算的回撤是被新资金掩盖的假数字（实测差了一倍：-11.1% vs -20.2%）。
4. **压力测试同时给区间收益和最大回撤。** 区间端点是人为选的日期
   —— 2018Q4 如果窗口停在 12-26（当天 +5% 反弹），会比真实低点浅一大截。
5. **缺数据的票用行业 ETF 代理，并在报告里逐只标出**；连代理都没有的从权重中剔除
   并重新归一，否则会被当成 0 收益、低估跌幅。

### 用真实数据跑出来的第一份报告

`reports/portfolio_2026-08-27.md` —— 26 只持仓、$9,130 股票市值 + $754 现金。
几个值得记住的数字：

- 名义 26 只 → 有效持仓数 17.2 → **有效独立赌注数 2.15**
- 组合年化波动率 **56.7%**（SPY 同期 14.0%），β **3.08**
- 60 日相关性 ≥0.7 的组合有 20 对，MU↔SNDK 0.88、AMD↔INTC 0.83
- 压力测试：2022 情景 -42.7%，2025-04 情景 -34.4%

### 验证情况

- ✅ P1 全链路真实跑通并入库
- ✅ P2 用**真实 Positions CSV** 端到端跑通，报告九节全渲染
- ✅ 分析层有校准测试：`python3 tests/test_analysis.py`
- ⚠️ **TWR 仍未真正验证** —— 真实流水只有 20 个交易日，需要 12 个月的导出

### ⚠️ 一个差点发出去的坏指标（教训）

「有效独立赌注数」第一版用 Meucci 基于 PCA 的 ENB
（`exp(−Σpᵢlnpᵢ)`，pᵢ 为各主成分的方差贡献占比）。它在真实组合上给出 **1.21**，
看起来极有洞察力，差点就那么发出去了。

用合成数据校准才发现指标是坏的：**25 个完全独立的资产它只报 13.5**（应为 25），
ρ=0.3 时报 1.01（应为约 3）。原因是等权组合天然对齐第一主成分，
97% 的方差会落在 PC1 上，与实际有几个独立风险源无关
—— Meucci 本人为此提出了 minimum-torsion 变换。

改用 **DR²**（分散化比率的平方），它与解析解 `1/(ρ+(1−ρ)/n)` 在 ρ∈[0,0.9] 上逐点吻合。

**教训已固化成规矩**：凡是「一眼看不出对不对」的统计量，
必须有一个已知解析解的合成数据做锚，测试写在 `tests/test_analysis.py`。
一个看起来很聪明但错误的数字，比没有数字有害得多。

### 实现中修掉的其他真实缺陷

1. **TWR 最大回撤算错了位置** —— 原本算在市值序列上，每月定投把市值推高，
   掩盖真实回撤。实测差近一倍（-11.1% 假 vs -20.2% 真）。改为算在 TWR 指数上。
2. **短窗口不做年化** —— 20 个交易日的 +11.6% 曾被外推成「年化 +298.9%」。
   现在低于 126 个交易日一律不年化，并在报告里显著告警。
3. **新股会静默压缩整个相关性面板** —— 原本用「所有票都有数据的交易日」做 inner join，
   只要有一只 2026-06 上市的 SPCX（53 天），其余 25 只两年的历史就全没了。
   改成用 SPY 定交易日历、剔除历史不足的票并在报告中列出。
4. **压力测试只报端点收益会骗人** —— 2018Q4 窗口若停在 12-26（当天 +5% 反弹），
   比真实低点浅一大截。现在同时给最大回撤和最低点日期，窗口末日改到 12-24。
5. **代理覆盖率必须和真实覆盖率分开报** —— 2018Q4 情景里 45.7% 的权重是行业 ETF 代理
   （SPCX/PLTR/COIN 等当时都不存在）。低于 60% 时报告显著标注
   「这一行更接近当年那些行业跌了多少，而不是你这个组合跌了多少」。

### 开发时的验证方法

```bash
python3 tests/test_analysis.py          # 分析层校准测试
export STOCKWATCH_DB=/tmp/test.db       # 用测试库，不污染真实快照历史
```

---

*本项目为个人研究工具，所有产出不构成投资建议。*
