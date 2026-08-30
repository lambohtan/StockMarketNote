"""
Fidelity Activity / History CSV 解析（交易流水）。

用途：算时间加权收益率（TWR）。Positions CSV 只能给「相对成本的涨跌」，
那个数字受加仓时点影响 —— 一直在跌的时候不断买入，成本被拉低，
看起来亏得少，但其实是在亏钱的过程中投入了更多钱。TWR 剔除这个影响。

⚠️ 列名映射未经真实文件验证。第一次跑请用 --dry-run 核对 detected_columns。
"""
import csv, io, re
from datetime import datetime

def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
    s = s.replace("(", "-").replace(")", "")
    if s in ("", "-", "--", "n/a", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None

def _norm(h):
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")

ALIASES = {
    "run_date": "date", "date": "date", "trade_date": "date",
    "action": "action", "transaction_type": "action", "description": "action_desc",
    "symbol": "ticker", "security_description": "security",
    "quantity": "quantity", "shares": "quantity",
    "price": "price", "price_1": "price",
    "amount": "amount", "amount_1": "amount",
    "commission": "commission", "fees": "fees",
    "settlement_date": "settle_date",
    "account_number": "account", "account": "account", "account_name": "account",
    "type": "sec_type",
}

# 货币基金 / 现金等价物。它们的申购赎回是现金 sweep，不是投资决策，
# 混进买卖会污染 TWR 的现金流。SPAXX 的 REINVESTMENT 尤其容易被误判成买入。
CASH_TICKERS = ("SPAXX", "FDRXX", "FZFXX", "FCASH", "FDIC", "SPRXX", "FZDXX")
CASH_DESC_HINTS = ("MONEY MARKET", "CASH RESERVES", "FDIC")

# 交易动作分类。Fidelity 的 Action 是一段自由文本，靠关键词判断
BUY_KW  = ("YOU BOUGHT", "BOUGHT", "PURCHASE", "REINVESTMENT")
SELL_KW = ("YOU SOLD", "SOLD", "REDEMPTION", "REDEEMED")
DIV_KW  = ("DIVIDEND", "INTEREST", "CAPITAL GAIN", "LONG-TERM CAP", "SHORT-TERM CAP")
FLOW_KW = ("ELECTRONIC FUNDS", "TRANSFER", "CONTRIBUTION", "DEPOSIT",
           "WITHDRAWAL", "JOURNAL", "WIRE", "CHECK", "DIRECT DEPOSIT")

def is_cash_equivalent(ticker, action=""):
    t = (ticker or "").upper().strip("*")
    a = (action or "").upper()
    return (any(t.startswith(c) for c in CASH_TICKERS)
            or any(h in a for h in CASH_DESC_HINTS))


def classify(action, ticker=""):
    a = (action or "").upper()
    # 先判现金等价物：SPAXX 的 REINVESTMENT 会命中 BUY_KW，但它是 sweep 不是买入
    if is_cash_equivalent(ticker, a):
        return "cash_sweep"
    if any(k in a for k in BUY_KW):
        return "buy"
    if any(k in a for k in SELL_KW):
        return "sell"
    if any(k in a for k in DIV_KW):
        return "income"
    if any(k in a for k in FLOW_KW):
        return "cashflow"
    return "other"

def _parse_date(v):
    s = (str(v) or "").strip()
    for f in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b-%d-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None

def parse_activity(text):
    """返回 (txns, report)。txns 按日期升序，每条含 date/kind/ticker/quantity/amount。"""
    lines = text.splitlines()
    hdr_i = None
    for i, ln in enumerate(lines[:60]):
        low = ln.lower()
        if ("run date" in low or "date" in low) and ("action" in low or "symbol" in low):
            hdr_i = i
            break
    if hdr_i is None:
        raise ValueError("找不到表头行（需要含 Date 和 Action/Symbol）")

    reader = csv.DictReader(io.StringIO("\n".join(lines[hdr_i:])))
    raw_cols = reader.fieldnames or []
    colmap = {c: ALIASES[_norm(c)] for c in raw_cols if _norm(c) in ALIASES}

    txns, skipped, kinds = [], [], {}
    for rec in reader:
        d = {dst: rec.get(src) for src, dst in colmap.items()}
        dt = _parse_date(d.get("date"))
        if not dt:
            if any((v or "").strip() for v in rec.values()):
                skipped.append(("日期无法解析", str(list(rec.values())[:2])[:60]))
            continue
        action = (d.get("action") or d.get("action_desc") or "").strip()
        tk_raw = (d.get("ticker") or "").strip().upper()
        kind = classify(action, tk_raw)
        kinds[kind] = kinds.get(kind, 0) + 1
        tk = tk_raw
        qty = _num(d.get("quantity"))
        amt = _num(d.get("amount"))
        # Fidelity 的 Quantity 在卖出时已经是负数；买入为正
        txns.append({
            "date": dt, "kind": kind, "action": action, "ticker": tk,
            "quantity": qty, "price": _num(d.get("price")), "amount": amt,
            "account": (d.get("account") or "default").strip() or "default",
        })

    txns.sort(key=lambda x: x["date"])
    report = {
        "detected_columns": raw_cols, "mapped": colmap,
        "parsed": len(txns), "kinds": kinds,
        "date_range": (txns[0]["date"], txns[-1]["date"]) if txns else None,
        "unclassified": sorted({t["action"] for t in txns if t["kind"] == "other"})[:15],
        "skipped": skipped[:10], "skipped_total": len(skipped),
    }
    return txns, report


def parse_file(path):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return parse_activity(f.read())
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码文件")
