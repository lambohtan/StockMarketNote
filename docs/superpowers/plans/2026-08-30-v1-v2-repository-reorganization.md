# StockWatch V1 Archive and V2 Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the repository into a frozen V1 archive and a documentation-only V2 scaffold without losing private runtime data or implying that V2 is implemented.

**Architecture:** Keep repository-wide navigation and constraints at the root, move the existing implementation and historical evidence into `v1/`, and place private runtime artifacts under ignored `v1/local-data/`. Create `v2/` as a set of explicit draft documents plus source and test directory descriptions, with no Python implementation.

**Tech Stack:** Git, Markdown, Python 3 standard library for read-only validation, SQLite read-only URI checks, macOS `lsof`, `shasum`, Codex custom agents.

**Spec:** `docs/superpowers/specs/2026-08-30-v1-v2-repository-reorganization-design.md`

## Global Constraints

- V1 is a frozen archive; migration does not need to preserve runnable commands, imports, configuration discovery, or launchd paths.
- `v1/local-data/` must remain untracked and must contain the real config, SQLite files, Fidelity CSV files, generated reports, and local SDD evidence.
- Do not expose or repeat real SEC email values, ntfy topics, API keys, Fidelity holdings, or report contents.
- Do not load, unload, generate, or modify a real LaunchAgent.
- Do not run network collection, LLM calls, or a real notification.
- Do not copy TradingAgents or TradingAgents-CN source code into the repository.
- V2 contains no Python files, executable entry points, dependency files, database schema, or Agent prompts.
- Every V2 document must say `状态：DRAFT`, `实现状态：未实现`, and `截至：2026-08-30`.
- Stop before moving SQLite when `lsof` cannot establish that the database, WAL, and SHM files have no open writer.
- Never move or overwrite a destination that already contains different content.
- Use `apply_patch` for text creation and text edits; use path moves only for pure relocations and private binary/runtime artifacts.
- Preserve unrelated user changes; if the worktree is not clean at the start, stop and ask for direction.

## File Map

### Root files retained or created

- Create: `README.md` — version navigation and honest status.
- Modify: `CLAUDE.md` — cross-version product, privacy, and documentation constraints only.
- Modify: `.gitignore` — private/runtime boundaries for V1 and future V2.
- Retain: `docs/superpowers/specs/2026-08-30-v1-v2-repository-reorganization-design.md`.
- Create: `docs/superpowers/plans/2026-08-30-v1-v2-repository-reorganization.md` — this plan.

### V1 tracked archive

- Create: `v1/README.md` — single V1 capability overview.
- Create: `v1/docs/AUDIT-INDEX.md` — old-to-new document map and status.
- Move to `v1/docs/history/`: root `00-调研报告.md`, `01-产品定位与偏好档案.md`, `02-系统设计.md`, `03-需求可行性与架构.md`, `HANDOFF.md`, old `CLAUDE.md`, and old `stockwatch/README.md`.
- Move to `v1/docs/engineering/`: all P3/P4 specs, implementation plans, and probe findings that predate the repository-reorganization spec.
- Move to `v1/stockwatch/`: current `stockwatch/` code, tests, requirements, and packaging content after runtime data and real config are removed.
- Create: `v1/stockwatch/config.example.yaml` — safe structural example.
- Move to `v1/tools/`: current root `tools/`.

### V1 private local data

- Move real `stockwatch/config.yaml` to `v1/local-data/config/config.yaml`.
- Move `stockwatch/data/` to `v1/local-data/database/`.
- Move `stockwatch/uploads/` to `v1/local-data/uploads/`.
- Move `stockwatch/reports/` to `v1/local-data/reports/`.
- Move `.superpowers/sdd/` to `v1/local-data/audit/sdd/`.
- Move `.claude/settings.local.json` to `v1/local-data/audit/tooling/settings.local.json` when present.

### V2 draft scaffold

- Create: `v2/README.md`.
- Create: `v2/docs/architecture.md`.
- Create: `v2/docs/data-sources.md`.
- Create: `v2/docs/research-verdict.md`.
- Create: `v2/docs/ranking-top10.md`.
- Create: `v2/docs/report-notify-schedule.md`.
- Create: `v2/docs/migration.md`.
- Create: `v2/docs/acceptance.md`.
- Create: `v2/src/README.md`.
- Create: `v2/tests/README.md`.

---

### Task 1: Run the non-mutating safety gate

**Files:**
- Read: `.git/`, `.gitignore`, `stockwatch/data/`, `stockwatch/uploads/`, `stockwatch/reports/`, `stockwatch/config.yaml`, `.superpowers/sdd/`, `.claude/settings.local.json`
- Modify: none

**Interfaces:**
- Consumes: current repository at commit `6025b8e` and local runtime artifacts.
- Produces: a verified clean starting point, an inventory, database hashes, and confirmation that SQLite has no open writer.

- [ ] **Step 1: Verify the approved starting commit and a clean worktree**

Run:

```bash
git log -1 --oneline
git status --short
```

Expected: the first command starts with `6025b8e`; the second command prints nothing. Stop if either expectation fails.

- [ ] **Step 2: Inventory tracked and ignored material without printing private contents**

Run:

```bash
git ls-files
git status --ignored --short
find stockwatch/data stockwatch/uploads stockwatch/reports .superpowers/sdd .claude -maxdepth 3 -type f -print
```

Expected: every tracked source/document appears in `git ls-files`; the private runtime paths appear as ignored or untracked local material. Do not open the CSV files or generated reports.

- [ ] **Step 3: Confirm the SQLite set has no open process**

Run:

```bash
lsof stockwatch/data/stockwatch.db stockwatch/data/stockwatch.db-wal stockwatch/data/stockwatch.db-shm
```

Expected: no process rows. An unavailable command, permission failure, or any open-process row is a hard stop for Task 2.

- [ ] **Step 4: Capture database identity and run a read-only integrity check**

Run:

```bash
stat -f '%N %z' stockwatch/data/stockwatch.db stockwatch/data/stockwatch.db-wal stockwatch/data/stockwatch.db-shm
shasum -a 256 stockwatch/data/stockwatch.db stockwatch/data/stockwatch.db-wal stockwatch/data/stockwatch.db-shm
python3 -c 'import sqlite3; p="file:stockwatch/data/stockwatch.db?mode=ro"; c=sqlite3.connect(p, uri=True); print(c.execute("PRAGMA quick_check").fetchone()[0]); c.close()'
```

Expected: three size lines, three SHA-256 values, and `ok`. Keep the terminal output available for Task 4 comparison; do not write it into a tracked document.

- [ ] **Step 5: Record the evidence counts used by the V1 summary**

Run:

```bash
python3 -c 'import sqlite3; p="file:stockwatch/data/stockwatch.db?mode=ro"; c=sqlite3.connect(p, uri=True); names=[r[0] for r in c.execute("select name from sqlite_master where type=\"table\" order by name")]; print("tables=", ",".join(names)); [print(n, c.execute("select count(*) from \""+n+"\"").fetchone()[0]) for n in names]; c.close()'
find stockwatch/tests -maxdepth 1 -name 'test_*.py' -type f | sort | wc -l
```

Expected: table names and counts are printed from the current read-only database, followed by the current number of direct-run test files. These values may be cited only as a 2026-08-30 archive snapshot.

---

### Task 2: Build the frozen V1 archive and privacy boundary

**Files:**
- Create: `README.md`
- Modify: `.gitignore`
- Replace: `CLAUDE.md`
- Create: `v1/README.md`
- Create: `v1/docs/AUDIT-INDEX.md`
- Create: `v1/stockwatch/config.example.yaml`
- Move: files listed in the V1 tracked and private file maps above

**Interfaces:**
- Consumes: Task 1 inventory, counts, hashes, and no-writer result.
- Produces: a self-contained frozen V1 archive, a private ignored local-data boundary, root navigation, and stable document indexes used by V2 migration notes.

- [ ] **Step 1: Strengthen `.gitignore` before moving private data**

Use `apply_patch` so `.gitignore` contains these effective rules while retaining normal Python and macOS ignores:

```gitignore
__pycache__/
*.pyc
.DS_Store
.env

# Private and generated runtime data
v1/local-data/
v2/local-data/
v2/reports/
*.csv
*.db
*.db-wal
*.db-shm
```

Keep the existing path-specific guards for `stockwatch/data`, `stockwatch/uploads`, `stockwatch/reports`, and `.superpowers/` through the move. They may remain as harmless defense-in-depth after the old paths disappear.

- [ ] **Step 2: Create private destinations and move runtime artifacts as atomic groups**

Run only after Task 1 passed:

```bash
mkdir -p v1/local-data/config v1/local-data/audit/tooling
mv stockwatch/config.yaml v1/local-data/config/config.yaml
mv stockwatch/data v1/local-data/database
mv stockwatch/uploads v1/local-data/uploads
mv stockwatch/reports v1/local-data/reports
mv .superpowers/sdd v1/local-data/audit/sdd
mv .claude/settings.local.json v1/local-data/audit/tooling/settings.local.json
```

If `.claude/settings.local.json` is absent, skip only that final move. Do not remove empty parent directories. The SQLite directory move must complete as one filesystem rename; stop if any destination already exists.

- [ ] **Step 3: Verify privacy immediately, before tracked moves**

Run:

```bash
git check-ignore -v v1/local-data/config/config.yaml v1/local-data/database/stockwatch.db v1/local-data/uploads/*.csv v1/local-data/reports/*.md v1/local-data/audit/sdd/progress.md
git ls-files 'v1/local-data/**'
```

Expected: every existing private sample is matched by `v1/local-data/`; `git ls-files` prints nothing. The old tracked `stockwatch/config.yaml` and `stockwatch/uploads/.gitkeep` may appear as deletions in `git status`, which is expected.

- [ ] **Step 4: Move the tracked implementation and historical documents**

Run:

```bash
mkdir -p v1/docs/history v1/docs/engineering
git add -u stockwatch/config.yaml stockwatch/uploads/.gitkeep
git mv stockwatch v1/stockwatch
git mv tools v1/tools
git mv '00-调研报告.md' v1/docs/history/
git mv '01-产品定位与偏好档案.md' v1/docs/history/
git mv '02-系统设计.md' v1/docs/history/
git mv '03-需求可行性与架构.md' v1/docs/history/
git mv HANDOFF.md v1/docs/history/HANDOFF.md
git mv CLAUDE.md v1/docs/history/CLAUDE-v1.md
git mv v1/stockwatch/README.md v1/docs/history/stockwatch-README-v1.md
git mv docs/superpowers/specs/2026-08-27-daily-push-and-menubar-design.md v1/docs/engineering/
git mv docs/superpowers/specs/2026-08-28-p4-股票池深读-design.md v1/docs/engineering/
git mv docs/superpowers/plans/2026-08-27-p3-daily-push.md v1/docs/engineering/
git mv docs/superpowers/plans/2026-08-29-p4-probe-findings.md v1/docs/engineering/
git mv docs/superpowers/plans/2026-08-29-p4-股票池深读.md v1/docs/engineering/
```

Expected: the reorganization spec and this implementation plan remain under root `docs/superpowers/`; all older engineering documents are under V1.

- [ ] **Step 5: Create a safe V1 configuration example**

Create `v1/stockwatch/config.example.yaml` with `apply_patch`. It must retain every top-level section from the archived real config:

```yaml
identity:
  sec_email: "you@example.com"
data:
  db_path: "data/stockwatch.db"
  uploads_dir: "uploads"
benchmarks:
  market: "SPY"
  growth: "QQQ"
  rates: "TLT"
  gold: "GLD"
  sectors: {}
reddit:
  filters: ["all-stocks", "wallstreetbets", "stocks"]
  top_n: 100
history:
  price_period: "2y"
notify:
  ntfy_topic: "replace-with-a-private-topic"
  ntfy_server: "https://ntfy.sh"
llm:
  provider: "claude_cli"
  claude_bin: "/absolute/path/to/claude"
  model: "claude-opus-5"
  max_tokens: 2000
pool:
  new_slots: 8
portfolio:
  core_tickers: ["VOO"]
  exclude_tickers: []
  look_through_etfs: ["QQQ", "VOO", "SPY"]
stress_scenarios: []
schedule:
  compute_daily: "06:00"
  compute_retry: "07:00"
  compute_weekly: "06:30"
  pool_daily: "06:45"
  weekly_day: "tuesday"
  push_time: "08:00"
notify_policy:
  l1_immediate: false
```

Add comments stating that this file is archival, the SEC address must be real when used, public ntfy topics are unsafe, and V1 is not expected to run at its new path.

- [ ] **Step 6: Create root navigation and cross-version constraints**

Create `README.md` with:

1. Title `StockWatch`.
2. A two-row table: V1 is `冻结归档`; V2 is `DRAFT / 未实现`.
3. Links to `v1/README.md` and the repository-reorganization spec.
4. One explicit sentence that the V2 documentation directory will be added by the next repository-reorganization task and that no Agent, Top 10, or 08:00 delivery exists yet.
5. A privacy note that private runtime artifacts live under ignored `v1/local-data/`.

Create a new root `CLAUDE.md` with only these cross-version rules:

1. No order placement or direct buy/sell instructions.
2. Deterministic calculations and ranking belong in Python; LLMs interpret evidence and produce bounded prose.
3. Missing or failed data never becomes empty success or an automatic negative score.
4. Private config, API keys, holdings, and notification topics stay out of tracked documentation.
5. V1 is frozen; new work belongs under V2 after an approved design and plan.
6. Every claim must distinguish verified, historical, draft, and untested status.

- [ ] **Step 7: Write the V1 capability overview**

Create `v1/README.md` with these exact sections:

1. `状态与证据口径` — define code present, historical test claim, archived runtime evidence, unverified, and known defect.
2. `产品范围` — analysis only, no order placement or directives.
3. `实际架构` — Fidelity CSV and sources → SQLite → portfolio/daily/pool flows → reports/outbox/notify.
4. `P1 数据底座` — cite archived `run_ingest.py`, sources, Store, and Task 1 database snapshot.
5. `P2 组合体检` — cite archived analysis modules and the portfolio sample; state TWR and live-data limits.
6. `P3 日报与投递基础` — cite archived daily, alerts, policy, outbox, notify, schedule, and P3 audit; state that live launchd and current phone delivery are not verified.
7. `P4 股票池与逐票深读` — cite archived deepread code and pool sample; state that it lacks cross-stock Top 10 ranking.
8. `测试与样例证据` — cite the exact Task 1 test-file count and historical P3 claim without presenting either as current live acceptance.
9. `已知限制` — include empty production CIK→ticker mapping, missing candidate price history, stale database schema, incomplete dependency declaration, absent `run_weekly.py`, and unverified deployment.
10. `本地数据` — explain the ignored `local-data/` layout without listing private values or holdings.
11. `历史资料` — link to `docs/AUDIT-INDEX.md`.

Every status claim must link to an archived file path. Do not quote private reports or configuration.

- [ ] **Step 8: Write the V1 audit index**

Create `v1/docs/AUDIT-INDEX.md` with one row per moved document containing:

- new path;
- original path;
- topic;
- status: current constraint, historical design, implemented-with-limitations, superseded, or local audit evidence;
- the current replacement or controlling document.

Include separate rows for the five older engineering documents and grouped entries for the ignored P3 SDD ledger/reviews. Mark the 100-point scorecard, the old `core/jobs/web` layout, old Reddit exclusion rule, and unchecked implementation boxes as historical rather than current.

Update only broken relative Markdown link targets caused by the moves, including links from engineering documents to the moved history documents. Do not rewrite the historical prose or status claims.

- [ ] **Step 9: Verify the V1 archive before committing**

Run:

```bash
test -f README.md
test -f CLAUDE.md
test -f v1/README.md
test -f v1/docs/AUDIT-INDEX.md
test -f v1/stockwatch/run_pool.py
test -f v1/stockwatch/config.example.yaml
test -f v1/local-data/config/config.yaml
test ! -e stockwatch
git ls-files 'v1/local-data/**'
git check-ignore -v v1/local-data/config/config.yaml v1/local-data/database/stockwatch.db
python3 -c 'import ast,pathlib; fs=list(pathlib.Path("v1/stockwatch").rglob("*.py")); [ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in fs]; print("parsed", len(fs))'
git diff --check
```

Expected: all `test` commands succeed; `git ls-files` prints nothing; both private samples are ignored; every archived Python file parses; `git diff --check` prints nothing.

- [ ] **Step 10: Commit the V1 archive**

Run:

```bash
git add -u
git add .gitignore README.md CLAUDE.md v1 docs/superpowers
git status --short
git diff --cached --check
git commit -m "chore: archive stockwatch v1"
```

Before commit, inspect `git status` and `git diff --cached --summary`; stop if anything under `v1/local-data/` is staged.

---

### Task 3: Create the documentation-only V2 scaffold

**Files:**
- Create: `v2/README.md`
- Create: `v2/docs/architecture.md`
- Create: `v2/docs/data-sources.md`
- Create: `v2/docs/research-verdict.md`
- Create: `v2/docs/ranking-top10.md`
- Create: `v2/docs/report-notify-schedule.md`
- Create: `v2/docs/migration.md`
- Create: `v2/docs/acceptance.md`
- Create: `v2/src/README.md`
- Create: `v2/tests/README.md`

**Interfaces:**
- Consumes: repository-reorganization spec and V1 archive paths from Task 2.
- Produces: the complete V2 documentation contract that a later V2 feature-design cycle can refine before any implementation.

- [ ] **Step 1: Establish the shared V2 status block**

Every V2 Markdown file starts with its title followed by:

```text
状态：DRAFT
实现状态：未实现
截至：2026-08-30
```

Every file also states that its proposed paths and interfaces are future boundaries, not current code.

- [ ] **Step 2: Write `v2/README.md`**

Required sections:

1. Goal: multi-source pool → Python prefilter → research Agents → Research Verdict → deterministic ranking → Top 10 → 08:00 local delivery.
2. Current status: documentation only; no V2 Python, database, scheduler, or provider configuration exists.
3. Product boundaries: no orders, no direct buy/sell instructions, no prediction claim.
4. V1 relationship: link to `../v1/README.md` and `docs/migration.md`.
5. Document index: link all seven V2 topic documents and both directory READMEs.
6. Third-party boundary: link upstream TradingAgents and TradingAgents-CN repositories; state that only research concepts or clearly licensed open-core functions may be adapted.

Update root `README.md` in this step so its version table links to `v2/README.md` and labels V2 `DRAFT / 未实现`.

- [ ] **Step 3: Write `v2/docs/architecture.md`**

Describe these isolated future components:

```text
Source adapters
→ ticker normalizer and candidate union
→ deterministic 20–30 prefilter
→ per-ticker Agent adapter
→ Research Verdict validator
→ deterministic cross-ticker ranker
→ Top 10 renderer
→ SQLite/outbox
→ notifier at 08:00 local time
```

For each component, document its responsibility, input, output, and failure boundary. State that different tickers require isolated Agent instances/processes because upstream graph state is mutable. Exclude the upstream Trader and Portfolio Manager nodes and the CN Web/application stack.

- [ ] **Step 4: Write `v2/docs/data-sources.md`**

Required content:

- Candidate-source contract: normalized ticker, market, exchange, source, observed time, reason, freshness, and source health.
- Existing V1 inputs: Fidelity CSV, yfinance, ApeWisdom, SEC EDGAR, filing text, and news text.
- Known V1 defects: blank EDGAR ticker mapping, incomplete candidate price history, partial-source success masking.
- Fail-closed rule: source/network/parser failure is not an empty successful pool.
- Minimal credentials: one LLM provider credential or a local model endpoint; SEC requires a real contact email; yfinance/ApeWisdom need no key in V1.
- Optional credentials by selected provider: `FRED_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `TUSHARE_TOKEN`, and `FINNHUB_API_KEY`.
- Secret rule: keys and real notification topics never belong in tracked YAML or Markdown.

- [ ] **Step 5: Write `v2/docs/research-verdict.md`**

Define the future structured result fields:

```text
schema_version
ticker
market
as_of
status: ok | partial | failed
data_quality
evidence[]: source, locator, observed_at, fact, excerpt
market_analysis
fundamental_analysis
news_analysis
sentiment_analysis
bull_case
bear_case
research_summary
risk_review
uncertainties[]
model_provenance
```

Ban fields or prose that act as `action`, `buy`, `sell`, `position_size`, `target_price`, or a trading recommendation. State that numerical facts come from Python/provider evidence, not model invention, and that missing facts remain unknown.

- [ ] **Step 6: Write `v2/docs/ranking-top10.md`**

Required rules:

- Python performs the 20–30 prefilter and final cross-stock ranking.
- LLM free text never enters the rank key directly.
- Ranking configuration must be versioned and separately approved before implementation; this repository-reorganization task approves no weights.
- Unknown is distinct from zero and from negative.
- Each ranked item must retain data-quality and evidence-completeness fields.
- Tie-breaks must be stable and deterministic, ending with normalized ticker ordering.
- Output exactly 10 only when at least 10 eligible researched stocks exist; otherwise label the result `Top N/10` and never fill with failed/unresearched names.

- [ ] **Step 7: Write `v2/docs/report-notify-schedule.md`**

Specify one aggregate `top10` report and one idempotent outbox event per analysis date. The report must show rank inputs, evidence quality, bull/bear cases, risks, and unknowns without directives. Fix 08:00 to the local-wall-clock delivery target, but require a measured 20–30 ticker runtime benchmark before choosing compute start time. Reuse the V1 outbox/notify concepts only after a later migration design and live acceptance.

- [ ] **Step 8: Write `v2/docs/migration.md`**

Create a table with columns `V1 capability`, `decision`, `reason`, `future validation`. Cover:

- reuse: source-result concept, filing/news material fetch, policy guard, outbox/notify concepts;
- adapt: candidate pool, facts, LLM boundary, rendering, Store schema, schedule;
- replace: fixed two-call deepread orchestration and single-ticker score presentation;
- exclude: Trader/Portfolio Manager decisions, TradingAgents-CN Web stack, automatic order execution;
- validate first: EDGAR mapping, candidate price coverage, provider licenses/APIs, latency, rate limits, and live notification delivery.

- [ ] **Step 9: Write `v2/docs/acceptance.md`**

Create phased gates for:

1. contract freeze;
2. data and deterministic prefilter;
3. Agent adapter and validated Research Verdict;
4. deterministic ranking;
5. report and idempotent outbox;
6. schedule and notification;
7. shadow run and cutover.

Each gate must separate static tests from network/provider, real-macOS launchd, and phone-delivery acceptance. State that V2 remains unimplemented until future plans satisfy these gates.

- [ ] **Step 10: Write future directory descriptions**

`v2/src/README.md` lists future responsibility folders only: `candidate_pool`, `agent_adapter`, `verdict`, `ranking`, `reporting`, `delivery`. It explicitly says none exist yet.

`v2/tests/README.md` lists future fixture and test categories: source normalization, fail-closed behavior, Agent contract, directive guard, deterministic ranking, stable ties, report safety, outbox idempotency, schedule rendering, and separately authorized live checks.

- [ ] **Step 11: Verify the V2 scaffold**

Run:

```bash
find v2 -type f -print | sort
find v2 -type f -name '*.py' -print
python3 -c 'import pathlib,sys; fs=list(pathlib.Path("v2").rglob("*.md")); bad=[str(p) for p in fs if "状态：DRAFT" not in p.read_text(encoding="utf-8") or "实现状态：未实现" not in p.read_text(encoding="utf-8")]; print(*bad,sep="\n"); sys.exit(bool(bad))'
rg -n '已经实现|已完成|现已支持' v2 --glob '*.md'
git diff --check
```

Expected: ten Markdown files are listed; no Python files; the status checker prints nothing. Any positive implementation phrase must be in a sentence explicitly denying implementation; revise ambiguous wording before commit.

- [ ] **Step 12: Commit the V2 draft scaffold**

Run:

```bash
git add v2 README.md
git diff --cached --check
git commit -m "docs: draft stockwatch v2 architecture"
```

Expected: only V2 documents and any required root navigation link adjustment are committed.

---

### Task 4: Run repository acceptance and independent review

**Files:**
- Read: all tracked Markdown and Python files, `.gitignore`, `v1/local-data/`, `~/.codex/agents/luna-worker.toml`
- Modify: only files that fail a stated acceptance check

**Interfaces:**
- Consumes: completed V1 archive and V2 scaffold.
- Produces: verified repository structure, privacy evidence, integrity evidence, a clean worktree, and a reviewer report.

- [ ] **Step 1: Verify all old tracked files are accounted for**

Run:

```bash
git diff --summary 6025b8e..HEAD
git diff --stat 6025b8e..HEAD
git status --short
```

Expected: the summary shows renames/creates/deletes consistent with the spec; the worktree is clean after Tasks 2 and 3.

- [ ] **Step 2: Re-run privacy checks**

Run:

```bash
git ls-files 'v1/local-data/**'
git check-ignore -v v1/local-data/config/config.yaml v1/local-data/database/stockwatch.db v1/local-data/uploads/*.csv v1/local-data/reports/*.md
find v1/local-data -type f -maxdepth 5 -print
```

Expected: no local-data file is tracked; every sampled path is ignored; the local inventory still contains config, database set, CSV files, reports, and audit evidence. Do not display contents.

- [ ] **Step 3: Compare SQLite identity and integrity after the move**

Run:

```bash
stat -f '%N %z' v1/local-data/database/stockwatch.db v1/local-data/database/stockwatch.db-wal v1/local-data/database/stockwatch.db-shm
shasum -a 256 v1/local-data/database/stockwatch.db v1/local-data/database/stockwatch.db-wal v1/local-data/database/stockwatch.db-shm
python3 -c 'import sqlite3; p="file:v1/local-data/database/stockwatch.db?mode=ro"; c=sqlite3.connect(p, uri=True); print(c.execute("PRAGMA quick_check").fetchone()[0]); c.close()'
```

Expected: sizes and hashes exactly match Task 1 by file role; quick check prints `ok`. Stop and report rather than repairing or replacing data if they differ.

- [ ] **Step 4: Validate tracked syntax and document shape without executing V1**

Run:

```bash
python3 -c 'import ast,pathlib; fs=list(pathlib.Path("v1/stockwatch").rglob("*.py")); [ast.parse(p.read_text(encoding="utf-8"), filename=str(p)) for p in fs]; print("parsed", len(fs))'
find v2 -type f -name '*.py' -print
rg -n '状态：DRAFT|实现状态：未实现' v2 --glob '*.md'
rg -n 'TO''DO|TB''D|待补|待填' README.md CLAUDE.md v1 v2 docs/superpowers/specs/2026-08-30-v1-v2-repository-reorganization-design.md
git diff --check 6025b8e..HEAD
```

Expected: all V1 Python parses; no V2 Python path; every V2 document has both status lines; the unresolved-marker search prints nothing; diff check prints nothing.

- [ ] **Step 5: Validate local Markdown links**

Run this read-only checker:

```bash
python3 -c 'import pathlib,re,sys; roots=[pathlib.Path("README.md"),pathlib.Path("CLAUDE.md"),*pathlib.Path("v1").rglob("*.md"),*pathlib.Path("v2").rglob("*.md"),*pathlib.Path("docs/superpowers").rglob("*.md")]; bad=[]; rx=re.compile(r"\[[^]]+\]\((?!https?://|#)([^)#]+)(?:#[^)]+)?\)"); [(bad.append((str(p),t)) if not (p.parent/t).resolve().exists() else None) for p in roots for t in rx.findall(p.read_text(encoding="utf-8"))]; print(*[f"{p}: {t}" for p,t in bad],sep="\n"); sys.exit(bool(bad))'
```

Expected: no output and exit code 0.

- [ ] **Step 6: Check Codex and `luna_worker` compatibility**

Run:

```bash
codex --version
sed -n '1,220p' ~/.codex/agents/luna-worker.toml
```

Expected: the installed Codex recognizes role files in `~/.codex/agents/`; the file declares the custom Agent model and reasoning level without unsupported keys. Do not edit global Codex configuration during this task.

- [ ] **Step 7: Dispatch a read-only `luna_worker` acceptance review**

The main Agent dispatches one `luna_worker` with a clean context and these exact boundaries:

- read only;
- inspect root navigation, V1 summary/index, V2 status blocks, ignored-data evidence, and the diff from `6025b8e`;
- do not open private config, CSV, database contents, or generated reports;
- report missing files, broken links, ambiguous implementation claims, or tracked private data;
- acceptance requires zero critical findings.

If findings exist, return them to the responsible task, apply only the necessary fix, rerun the affected checks, and ask the same worker to verify the fix.

- [ ] **Step 8: Commit acceptance fixes only when needed**

When Task 4 changes tracked files, run:

```bash
git add README.md CLAUDE.md .gitignore v1 v2 docs/superpowers
git diff --cached --check
git commit -m "docs: finish v1 v2 repository organization"
```

When no tracked fix is needed, do not create an empty commit.

- [ ] **Step 9: Prepare the final handoff evidence**

Run:

```bash
git status --short
git log --oneline -4
git diff --stat 6025b8e..HEAD
git diff --summary 6025b8e..HEAD
```

Expected: clean worktree, commits for V1 and V2, and a rename/create summary that contains no local-data files. The final report must separate verified structure/privacy/integrity from untested V1 runtime and unimplemented V2 behavior.
