# P4 探测结论（2026-08-29）

探测脚本：`tools/probe_p4.py`（实测运行，联网访问 SEC EDGAR 与 yfinance，无 mock）。
运行环境：edgartools 5.53.0 · yfinance 1.7.0 · pandas（项目环境）· tiktoken 0.14.0（仅用于估算 token 数，见第 3 节说明）。

本文档第 1、2 节主表（章节抽取、现金流命中）来自脚本的 `probe_edgar()` / `probe_cashflow()`
（`=== 1. ===` / `=== 2. ===` 两段输出）。第 2 节的完整行名列表和第 3 节的 token 估算
最初是修复轮之前用临时脚本手工跑出来的、无法从当时提交的 `probe_p4.py` 复现 ——
审查指出后已把它们收编成脚本里正式的 `probe_cashflow_full_index()` 和 `probe_tokens()`
两个函数（对应下面输出里的 `=== 2b. ===` / `=== 3. ===` 两段），现在跑
`python3 tools/probe_p4.py` 会打印出与本文档完全一致的数字，不再需要额外的手工步骤。

## 1. EDGAR 章节抽取

brief 里给的正则和取数写法（`Company(tk).get_filings(form="10-Q")` → `filings.latest(1)` →
`.text()`）**原样可用，没有做任何 API 层面的调整** —— `latest(1)` 返回的对象直接有
`.text()` 方法，取到的是最新一期 10-Q 的纯文本正文。

| 票 | 全文长度（字符） | MD&A 标题命中次数 | MD&A 抽出长度 | 风险因素标题命中次数 | 风险因素抽出长度 |
|---|---|---|---|---|---|
| AAPL | 128,898 | 2 | 27,024 | 2 | 19,107 |
| NVDA | 208,389 | 2 | 46,369 | 2 | 34,697 |
| MU   | 269,346 | 2 | 40,795 | 4 | 108,907 |

抽出内容真实片段（`repr()` 原样贴出，未做任何美化）：

- AAPL MD&A 开头：`'Item 2. Management’s Discussion and Analysis of Financial Condition and Results '`
- AAPL 风险因素开头：`'Item 1A. Risk Factors\n\nThe Company’s business, reputation, results of operations'`
- NVDA MD&A 开头：`'Item 2. Management’s Discussion and Analysis of Financial Condition and Results '`
- NVDA 风险因素开头：`'Item 1A. Risk Factors\n\nOther than the risk factors listed below, there have been'`
- MU MD&A 开头：`'ITEM 2. MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS '`
- MU 风险因素开头：`'ITEM 1A. RISK FACTORS\n\nIn addition to the factors discussed elsewhere in this Fo'`

**MU 风险因素命中 4 次为什么、抽出的 108,907 字符是不是抽错了 —— 已核实，不是 bug：**
MU 的 10-Q 正文里，「Item 1A」这几个字先后出现在：(1) 目录（TOC）、(2) Part I 正文里
一句交叉引用（"...Item 1A. Risk Factors in this Quarterly Report on Form 10-Q."）、
(3) 真正的 Part II Item 1A 标题本身。脚本取 `finditer` 的**最后一次命中**（`starts[-1]`），
天然跳过了 TOC 和交叉引用，定位到真正的标题上，逻辑没问题。
抽出的 108,907 字符经人工抽样核对（每隔 15,000 字符抽样一段），从头到尾都是风险因素正文
（供应链、网络安全、关税、竞争、库存过剩等条目），一直到 `RISK_END` 命中的
`'ITEM 2. UNREGISTERED SALES OF EQUITY SECURITIES AND USE OF P...'`（Part II Item 2，
不是 Part I 的 MD&A Item 2）才结束 —— 边界正确。Micron 这份 10-Q 的风险因素章节本身就写得
比 AAPL/NVDA 长得多（约是 AAPL 的 5.7 倍），这是真实差异，不是抽取错误。

**结论**：抽取率 3/3。☑ 正则可用（三只票 MD&A 和风险因素都精确定位到标题、边界正确，
且开头文本经人工核对确认是真实章节正文，不是目录或交叉引用）。不需要改用 edgartools
结构化接口，也不需要降级。唯一需要注意：命中次数不是稳定的 1 次或 2 次
（AAPL/NVDA 是 2 次，MU 是 4 次），**生产代码必须取 `finditer` 结果的最后一个匹配**，
不能假设只有一次命中或直接取第一个。

## 2. yfinance 季度现金流表

三只票 `yf.Ticker(tk).quarterly_cashflow` 均**成功取到**，无异常、无空表。

| 票 | 形状（行×列） | 经营现金流命中的行名 | 最近一期值（`.iloc[0]`，美元） |
|---|---|---|---|
| AAPL | 46 × 6 | `Operating Cash Flow`, `Cash Flow From Continuing Operating Activities` | 34,369,000,000.0 |
| NVDA | 49 × 6 | `Operating Cash Flow`, `Cash Flow From Continuing Operating Activities` | 50,344,000,000.0 |
| MU   | 41 × 6 | `Operating Cash Flow`, `Cash Flow From Continuing Operating Activities` | 25,388,000,000.0 |

三只票的行名完全一致：都同时存在 `Operating Cash Flow`（首选，取值即为经营现金流本身）
和 `Cash Flow From Continuing Operating Activities`（同义备份行）。brief 里用
`"operating" in i.lower() and "cash" in i.lower()` 做模糊匹配会同时命中这两行，
生产代码（Task 6）里应**优先精确匹配 `"Operating Cash Flow"`**，命中即用，
不要用 `hit[0]`（模糊匹配结果的顺序取决于 DataFrame 原始行序，不保证 `Operating Cash Flow`
排在前面 —— 实测三只票里它确实排在前面，但不应依赖这个巧合）。

三只票完整行名列表（`probe_cashflow_full_index()` 输出，供 Task 6 参考，
已确认三者都稳定包含 `Operating Cash Flow`；重跑脚本可复现下列内容）：

<details>
<summary>AAPL（46 行）</summary>

Free Cash Flow, Repurchase Of Capital Stock, Repayment Of Debt, Issuance Of Debt,
Capital Expenditure, Income Tax Paid Supplemental Data, End Cash Position,
Beginning Cash Position, Changes In Cash, Financing Cash Flow,
Cash Flow From Continuing Financing Activities, Net Other Financing Charges,
Cash Dividends Paid, Common Stock Dividend Paid, Net Common Stock Issuance,
Common Stock Payments, Net Issuance Payments Of Debt, Net Short Term Debt Issuance,
Short Term Debt Payments, Net Long Term Debt Issuance, Long Term Debt Payments,
Long Term Debt Issuance, Investing Cash Flow,
Cash Flow From Continuing Investing Activities, Net Other Investing Changes,
Net Investment Purchase And Sale, Sale Of Investment, Purchase Of Investment,
Net PPE Purchase And Sale, Purchase Of PPE, **Operating Cash Flow**,
Cash Flow From Continuing Operating Activities, Change In Working Capital,
Change In Other Current Liabilities, Change In Other Current Assets,
Change In Payables And Accrued Expense, Change In Payable, Change In Account Payable,
Change In Inventory, Change In Receivables, Changes In Account Receivables,
Other Non Cash Items, Stock Based Compensation, Depreciation Amortization Depletion,
Depreciation And Amortization, Net Income From Continuing Operations

</details>

<details>
<summary>NVDA（49 行）</summary>

Free Cash Flow, Repurchase Of Capital Stock, Repayment Of Debt, Capital Expenditure,
Income Tax Paid Supplemental Data, End Cash Position, Beginning Cash Position,
Changes In Cash, Financing Cash Flow, Cash Flow From Continuing Financing Activities,
Net Other Financing Charges, Proceeds From Stock Option Exercised, Cash Dividends Paid,
Common Stock Dividend Paid, Net Common Stock Issuance, Common Stock Payments,
Net Issuance Payments Of Debt, Net Long Term Debt Issuance, Long Term Debt Payments,
Investing Cash Flow, Cash Flow From Continuing Investing Activities,
Net Investment Purchase And Sale, Sale Of Investment, Purchase Of Investment,
Net Business Purchase And Sale, Purchase Of Business, Net PPE Purchase And Sale,
Purchase Of PPE, **Operating Cash Flow**, Cash Flow From Continuing Operating Activities,
Change In Working Capital, Change In Other Current Liabilities,
Change In Payables And Accrued Expense, Change In Accrued Expense, Change In Payable,
Change In Account Payable, Change In Prepaid Assets, Change In Inventory,
Change In Receivables, Changes In Account Receivables, Other Non Cash Items,
Stock Based Compensation, Deferred Tax, Deferred Income Tax,
Depreciation Amortization Depletion, Depreciation And Amortization,
Operating Gains Losses, Gain Loss On Investment Securities,
Net Income From Continuing Operations

</details>

<details>
<summary>MU（41 行）</summary>

Free Cash Flow, Repurchase Of Capital Stock, Repayment Of Debt, Issuance Of Debt,
Capital Expenditure, End Cash Position, Beginning Cash Position,
Effect Of Exchange Rate Changes, Changes In Cash, Financing Cash Flow,
Cash Flow From Continuing Financing Activities, Net Other Financing Charges,
Proceeds From Stock Option Exercised, Cash Dividends Paid, Common Stock Dividend Paid,
Net Common Stock Issuance, Common Stock Payments, Net Issuance Payments Of Debt,
Net Long Term Debt Issuance, Long Term Debt Payments, Long Term Debt Issuance,
Investing Cash Flow, Cash Flow From Continuing Investing Activities,
Net Other Investing Changes, Net Investment Purchase And Sale, Sale Of Investment,
Purchase Of Investment, Capital Expenditure Reported, **Operating Cash Flow**,
Cash Flow From Continuing Operating Activities, Change In Working Capital,
Change In Other Current Liabilities, Change In Other Current Assets,
Change In Payables And Accrued Expense, Change In Inventory, Change In Receivables,
Other Non Cash Items, Stock Based Compensation, Depreciation Amortization Depletion,
Depreciation And Amortization, Net Income From Continuing Operations

</details>

**结论**：☑ 可用 → criteria 保留 6 条。三只票的 `quarterly_cashflow` 均正常返回，
`Operating Cash Flow` 行在三者中都存在且位置、命名一致，不需要砍到 5 条，
Task 7 不需要删除 `operating_cash_flow` 这条标准，Task 9 分母基数维持 8（不改成 7）。

## 3. 正文长度与 token

除脚本自带的字符数统计外，`probe_tokens()` 额外用 `tiktoken`（`cl100k_base` 编码）
对 MD&A 和风险因素抽出段落做了 token 估算（对应下方脚本输出的 `=== 3. ===` 段）——
**注意**：项目实际用的是 `claude-opus-5`（见 `stockwatch/config.yaml` 的 `llm.model`），
Claude 用的不是 `cl100k_base` 分词器，这里的 token 数只是数量级参考，不是精确值。

| 票 | 全文 tokens | MD&A 长度 / tokens | 风险因素长度 / tokens | 两节合计长度 / tokens |
|---|---|---|---|---|
| AAPL | 21,811 | 27,024 字符 / 5,237 tok | 19,107 字符 / 3,331 tok | 46,131 字符 / 8,568 tok |
| NVDA | 36,236 | 46,369 字符 / 8,698 tok | 34,697 字符 / 6,286 tok | 81,066 字符 / 14,984 tok |
| MU   | 46,186 | 40,795 字符 / 7,705 tok | 108,907 字符 / 19,075 tok | 149,702 字符 / 26,780 tok |

三票平均：MD&A 约 38,063 字符（≈ 7,213 tok），风险因素约 54,237 字符（≈ 9,564 tok），
两节合计约 92,300 字符（≈ 16,777 tok）。MU 是极端值（风险因素章节比 AAPL 长 5.7 倍），
不是异常数据，是该公司文风使然。

**结论**：☑ 需先截断到前 N 字符（或做 token 上限截断）。理由：
两节合计平均约 1.68 万 token，MU 单只票就到 2.68 万 token；stage1 如果要把 MD&A + 风险因素
原文整段喂给 LLM，三只票叠加、加上 prompt 模板和其他上下文后很容易顶到主流模型
单次调用的实际可用上下文上限（尤其是要在一次调用里处理股票池里多只票时）。
不需要做「压缩」（摘要式压缩会引入 LLM 的二次转写风险，与硬性约束 2「所有数值计算用
确定性 Python，LLM 只做摘要和翻译」不冲突，但会让抽取环节多一次不必要的 LLM 调用），
更稳妥的做法是**按字符数硬截断**（例如风险因素只取前 N 字符，因为风险因素章节通常
前几段是最材料性的新增/变更内容，MU 这类公司尾部会有大量常规样板风险条目）。
具体截断阈值留给 Task 6/7 实现时根据实际 prompt 预算决定，本探测只确认「需要截断」
这一结论本身。
