# StockWatch

个人股票研究分析系统。当前进度：**P1 数据底座 + P2 组合分析**。

## 安装

```bash
cd stockwatch
pip3 install -r requirements.txt
```

`config.yaml` 里 `identity.sec_email` 已填你的邮箱 —— SEC 要求真实联系方式，
用占位地址可能被限流。

## 首次运行

从 Fidelity 导出 Positions CSV（Accounts → Positions → 右上角下载），然后：

```bash
python3 run_ingest.py --dry-run --positions ~/Downloads/Portfolio_Positions_XXX.csv
```

`--dry-run` 只解析不写库。**先看它把你的 CSV 解析成什么样** —— 每行的
ticker / 数量 / 市值 / 成本都会打印出来，确认无误再去掉 `--dry-run` 正式入库。

```bash
python3 run_ingest.py --positions ~/Downloads/Portfolio_Positions_XXX.csv
```

之后每天只需要：

```bash
python3 run_ingest.py
```

（沿用最近一次持仓快照，只更新行情、Reddit、EDGAR）

## 已实现

- SQLite 存储层，**写入即快照、只追加不覆盖** —— 保证以后能回测自己的信号
- Fidelity CSV 容错解析（自动跳过现金、待结算、结尾免责声明）
- 数据源适配器：yfinance 行情/元数据、ApeWisdom、SEC EDGAR
- 每个数据源的健康状态入库（`source_health` 表）—— yfinance 静默失败比报错更危险
- 财报日期多路降级取数（绕开 pandas 3.x 的兼容问题）

## 目录

```
sw/store.py        SQLite schema 与读写
sw/fidelity.py     CSV 解析
sw/sources/        数据源适配器，每个都可单独替换
run_ingest.py      每日抓取入口（之后由 launchd 调度）
data/stockwatch.db 数据库（不要删，历史快照全在这）
```

## 下一步（P2）

组合分析：真实收益率、行业分布、有效持仓数、相关性矩阵、与 VOO/QQQ 重叠度、历史压力测试。

## P2 · 组合体检报告

```bash
# 用库里最近一次持仓快照
python3 run_portfolio.py

# 带交易流水（Fidelity Accounts → Activity → 下载最近 12 个月），才能算 TWR
python3 run_portfolio.py --activity ~/Downloads/History_for_Account_XXX.csv

# 先干跑，核对流水解析结果（强烈建议第一次这么做）
python3 run_portfolio.py --dry-run --activity ~/Downloads/History_for_Account_XXX.csv

# 完全离线，只用库里已有数据
python3 run_portfolio.py --no-fetch
```

报告写到 `reports/portfolio_<日期>.md`，同时存进 `reports` 表。

报告包含九节：概览 / 收益（简单 + TWR）/ 持仓明细 / 集中度 / 行业分布（含 ETF 穿透）/
相关性与波动 / 与指数重叠 / 历史压力测试 / 局限说明。

### 两个收益口径的区别

| | 含义 | 需要什么 |
|---|---|---|
| 持有期简单收益 | (市值−成本)/成本，Fidelity 给的那个数 | Positions CSV |
| **时间加权 TWR** | 剔除资金进出时点，唯一能和 SPY/QQQ 公平比较的口径 | **Activity CSV** |

下跌途中不断加仓会拉低成本，让简单收益看起来比实际体验好。TWR 不受这个影响。

### 压力测试的读法

区间收益看端点，**最大回撤才是过程中最深处** —— 区间端点是人为选的日期。
用的是**当前权重**放回历史，是「如果那时我持有现在这些票」的假想推演，不是预测。
情景配置在 `config.yaml` 的 `stress_scenarios`，可以自己加。

## 测试

```bash
python3 tests/test_analysis.py
```

分析层的数值校准测试 —— 每个统计量都对着一个已知解析解的合成数据验证。
**新增任何统计指标都要在这里加一条**：一个看起来聪明但错误的数字，比没有数字有害得多
（有效独立赌注数的第一版就是这么被发现是坏的）。

## 环境变量

- `STOCKWATCH_DB` —— 覆盖数据库路径。用测试库验证时用，避免污染真实快照历史。

## 已知边界

- ETF 穿透只能拿到**前十大成分**（yfinance 限制），报告里标注了覆盖率
- 相关性 / 波动率 / β 都是时变的，报告里标注了窗口长度
- 评分与情景假设**未经回测验证**，是纪律工具不是 alpha 来源
