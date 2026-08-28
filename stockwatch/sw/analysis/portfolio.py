"""
持仓组合的结构化表示。

从 positions 快照 + meta（行业）构建，按 ticker 跨账户合并。
只做加载和权重计算，风险指标在 risk.py。
"""
from dataclasses import dataclass, field


@dataclass
class Holding:
    ticker: str
    description: str = ""
    name: str = ""
    quantity: float = 0.0
    last_price: float | None = None
    market_value: float = 0.0
    cost_basis: float | None = None
    avg_cost: float | None = None
    sector: str | None = None
    industry: str | None = None
    accounts: list = field(default_factory=list)
    is_core: bool = False          # 核心仓（如 VOO 定投），不算在卫星仓集中度里
    is_fund: bool = False          # ETF / 基金，穿透分析时要展开

    @property
    def gain(self):
        """未实现盈亏金额。cost_basis 缺失时返回 None，不猜。"""
        if self.cost_basis is None:
            return None
        return self.market_value - self.cost_basis

    @property
    def gain_pct(self):
        if not self.cost_basis:
            return None
        return (self.market_value - self.cost_basis) / self.cost_basis


@dataclass
class Portfolio:
    snapshot_date: str
    holdings: list                  # list[Holding]，仅股票/ETF
    cash: float = 0.0
    cash_rows: list = field(default_factory=list)

    # ---------- 基本量 ----------
    @property
    def equity_value(self):
        return sum(h.market_value for h in self.holdings)

    @property
    def total_value(self):
        return self.equity_value + self.cash

    @property
    def cost_total(self):
        """只累加有成本数据的持仓；缺失的单独报，不当成 0。"""
        vals = [h.cost_basis for h in self.holdings if h.cost_basis is not None]
        return sum(vals) if vals else None

    @property
    def missing_cost(self):
        return [h.ticker for h in self.holdings if h.cost_basis is None]

    def weights(self, exclude_core=False):
        """按市值的权重。分母是被纳入的那部分，不含现金。"""
        hs = [h for h in self.holdings if not (exclude_core and h.is_core)]
        tot = sum(h.market_value for h in hs)
        if tot <= 0:
            return {}
        return {h.ticker: h.market_value / tot for h in hs}

    @property
    def tickers(self):
        return [h.ticker for h in self.holdings]

    def get(self, ticker):
        for h in self.holdings:
            if h.ticker == ticker:
                return h
        return None

    def sector_weights(self, exclude_core=False):
        w = self.weights(exclude_core)
        out = {}
        for h in self.holdings:
            if h.ticker not in w:
                continue
            key = h.sector or ("ETF/基金" if h.is_fund else "未知")
            out[key] = out.get(key, 0.0) + w[h.ticker]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


FUND_HINT_TYPES = ("etf", "mutual fund", "fund")


def build(store, cfg, snapshot_date=None):
    """从库里最近一次（或指定日期）持仓快照构建 Portfolio。"""
    if snapshot_date:
        rows = store.q("SELECT * FROM positions WHERE snapshot_date=?", (snapshot_date,))
    else:
        rows = store.latest_positions()
    if not rows:
        raise ValueError("库里没有持仓快照。先跑 run_ingest.py --positions <Fidelity CSV>")

    snap = rows[0]["snapshot_date"]
    core = {t.upper() for t in (cfg.get("portfolio.core_tickers") or [])}
    excl = {t.upper() for t in (cfg.get("portfolio.exclude_tickers") or [])}

    meta = {r["ticker"]: r for r in store.q("SELECT * FROM meta")}

    merged, cash_rows, cash = {}, [], 0.0
    for r in rows:
        tk = (r["ticker"] or "").upper()
        if tk in excl:
            continue
        if r["asset_type"] == "cash":
            cash += float(r["market_value"] or 0)
            cash_rows.append(r)
            continue
        mv = float(r["market_value"] or 0)
        if tk not in merged:
            m = meta.get(tk) or {}
            merged[tk] = Holding(
                ticker=tk,
                description=r["description"] or "",
                name=(m.get("name") or r["description"] or ""),
                sector=m.get("sector"),
                industry=m.get("industry"),
                is_core=tk in core,
                is_fund=(m.get("sector") is None and m.get("industry") is None
                         and m.get("name") is not None),
            )
        h = merged[tk]
        h.quantity += float(r["quantity"] or 0)
        h.market_value += mv
        if r["cost_basis_total"] is not None:
            h.cost_basis = (h.cost_basis or 0.0) + float(r["cost_basis_total"])
        if r["last_price"] is not None:
            h.last_price = float(r["last_price"])
        acct = r["account"] or "default"
        if acct not in h.accounts:
            h.accounts.append(acct)

    for h in merged.values():
        if h.quantity:
            h.avg_cost = (h.cost_basis / h.quantity) if h.cost_basis is not None else None

    hs = sorted(merged.values(), key=lambda x: -x.market_value)
    return Portfolio(snapshot_date=snap, holdings=hs, cash=cash, cash_rows=cash_rows)
