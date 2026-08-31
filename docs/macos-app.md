# StockWatch macOS 状态栏应用

状态：已实现并在本机安装；外部网络任务、长时间 LLM 调度和手机投递按下文分开验收
截至：2026-08-31

## 产品边界

`StockWatch.app` 是现有 Python 研究管线的本地运行宿主：它负责状态栏生命周期、
定时任务、运行记录、本地 Web Dashboard 和配置管理。它不改变底层股票池、
Reddit 预抓取或 lean 研究逻辑。

自动化边界：

- 只调度 `build_pool.py`、`prefetch_reddit.py` 和 `run_lean.py`；
- `run_analysis.py` 含 Trader / Portfolio Manager 买卖指令，永不进入自动调度器；
- “发布分析结果”当前只生成本地汇总报告并在 Dashboard 中展示；
- 手机通知、Top10、outbox 和实际送达仍未实现，界面不得把本地发布标成已送达。

## 进程与存储

```text
StockWatch.app (AppKit, LSUIElement)
└─ stockwatch_service.py (Python, 127.0.0.1 only)
   ├─ Scheduler          每个任务独立开关/日程，防重入
   ├─ TaskRunner         参数化 subprocess，可取消，日志分离
   ├─ RuntimeStore       SQLite 运行史与中断恢复
   └─ Local Dashboard   纯本地 HTML/CSS/JS，无 CDN
```

应用不向 `.app` 包内写数据。私有配置、SQLite、报告和日志位于：

```text
~/Library/Application Support/StockWatch/
├─ config.json
├─ runtime.sqlite3
├─ data/
│  ├─ pool/pool.sqlite3
│  ├─ tradingagents/reddit-prefetch.sqlite3
│  └─ tradingagents/lean/
├─ reports/
└─ logs/
```

## 调度契约

四个任务没有隐式链式触发，可独立停用、手动执行或调整时间：

1. `pool_update`：更新股票池 SQLite 快照。
2. `reddit_update`：读取当前股票池快照，生成独立 Reddit 原始缓存。
3. `analysis_update`：对启动时的股票池快照串行执行 lean 研究；默认关闭，避免未经确认的长时间 LLM 消耗。
4. `publish_report`：将已存在的研究文章汇总成本地 Markdown 报告，不访问通知服务。

同名任务同时只能有一个运行实例。定时触发使用持久化 `scheduled_for`
键去重；应用异常退出后，遗留的 `running` 记录会在下次启动时标为 `interrupted`。

## 本地 HTTP 安全

- 仅监听 `127.0.0.1`，不监听 LAN 或公网地址；
- 不发送 CORS 许可头；
- 所有修改状态的请求必须携带页面从同源 bootstrap 获得的随机 token；
- 设置 CSP，不加载第三方脚本、字体或资源；
- 文章和 Reddit 文本以纯文本展示，不把模型/外部内容当 HTML 注入。

## 打包与验收

`scripts/build_macos_app.sh` 用 Command Line Tools 编译原生状态栏宿主，并把当前代码与
Python 虚拟环境放入标准 `.app` bundle。本机仍需已登录的 `claude` CLI 才能执行
lean 研究；股票池、Reddit、UI 和本地发布不依赖 LLM。

静态/离线验收：

- 所有单元测试通过；
- Swift 宿主可编译，`.app` 具有正确 `Info.plist` 和可执行文件；
- 本地服务 `/healthz` 和 Dashboard 可访问；
- 只读页面能读取测试 SQLite/文章，修改 API 无 token 时拒绝。

本次已验收：

- 86 项根目录离线测试通过；
- Swift 6.3.3 宿主编译通过，App bundle 临时签名通过 `codesign --verify --deep --strict`；
- `/Applications/StockWatch.app` 已启动，`/healthz` 正常，服务只监听 `127.0.0.1:8765`；
- 安装后 Dashboard 实际读到候选股票池、分析文章和 Reddit 快照；
- Chrome 实际打开 Dashboard，主导航、运行中心与股票池切换正常；
- 本地报告手动任务运行成功，运行账本明确记录 `not sent to phone`。

仍需分开验收（不能由单元测试替代）：

- 状态栏“停止服务”、异常退出后有限自动重启、登录启动需在日常使用中继续观察；
- 真实 pool/Reddit 定时网络运行尚未由新宿主触发；
- Lean 分析师调度默认关闭，本次没有花费 LLM 调用验证它；
- 当前只有 ad-hoc 本地签名，没有 Developer ID/notarization，不是可对外分发的公证版；
- 手机通知未实现，也未测试。
