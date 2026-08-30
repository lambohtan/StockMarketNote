"""
Fidelity Positions CSV 解析。

Fidelity 的导出文件不是干净的 CSV：开头可能有空行，结尾有免责声明段落，
中间混着现金/货币基金/Pending Activity 行，数字带 $ 、逗号和百分号。
这里做容错解析，并把每一行分类，让上层决定哪些算持仓。
"""
import csv, io, re
from datetime import date

CASH_HINTS = ("SPAXX", "FDRXX", "FZFXX", "FCASH", "CORE**", "MONEY MARKET")

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

# Fidelity 各版本导出的列名不完全一致，这里做映射
ALIASES = {
    "symbol": "ticker", "description": "description", "quantity": "quantity",
    "last_price": "last_price", "current_value": "market_value",
    "cost_basis_total": "cost_basis_total", "cost_basis": "cost_basis_total",
    "average_cost_basis": "avg_cost", "average_cost": "avg_cost",
    "total_gain_loss_dollar": "total_gain", "type": "asset_type",
    "account_number": "account", "account_name": "account_name",
    "account": "account",
}

def parse_positions(text, snapshot_date=None):
    """返回 (rows, report)。rows 是可入库的持仓；report 说明跳过了什么。"""
    snapshot_date = snapshot_date or date.today().isoformat()
    lines = text.splitlines()

    # 找表头：同时含 symbol 和 quantity 的那一行
    hdr_i = None
    for i, ln in enumerate(lines[:40]):
        low = ln.lower()
        if "symbol" in low and ("quantity" in low or "current value" in low):
            hdr_i = i
            break
    if hdr_i is None:
        raise ValueError("找不到表头行（需要同时包含 Symbol 和 Quantity/Current Value）")

    body = "\n".join(lines[hdr_i:])
    reader = csv.DictReader(io.StringIO(body))
    raw_cols = reader.fieldnames or []
    colmap = {}
    for c in raw_cols:
        n = _norm(c)
        if n in ALIASES:
            colmap[c] = ALIASES[n]

    rows, skipped = [], []
    for rec in reader:
        get = lambda k: rec.get(k)
        d = {}
        for src, dst in colmap.items():
            d[dst] = rec.get(src)

        tk = (d.get("ticker") or "").strip().upper()
        desc = (d.get("description") or "").strip()

        # 结尾免责声明 / 空行
        if not tk or len(tk) > 12 or " " in tk:
            if desc or tk:
                skipped.append(("非持仓行", (tk or desc)[:60]))
            continue
        if "PENDING" in tk or "PENDING" in desc.upper():
            skipped.append(("待结算", tk)); continue

        qty = _num(d.get("quantity"))
        mv  = _num(d.get("market_value"))
        if qty is None and mv is None:
            skipped.append(("无数量与市值", tk)); continue

        kind = "equity"
        if any(h in tk.upper() or h in desc.upper() for h in CASH_HINTS):
            kind = "cash"

        rows.append({
            "snapshot_date": snapshot_date,
            "ticker": tk,
            "description": desc,
            "quantity": qty,
            "last_price": _num(d.get("last_price")),
            "market_value": mv,
            "cost_basis_total": _num(d.get("cost_basis_total")),
            "avg_cost": _num(d.get("avg_cost")),
            "total_gain": _num(d.get("total_gain")),
            "asset_type": kind,
            "account": (d.get("account") or "default").strip() or "default",
        })

    report = {
        "detected_columns": raw_cols,
        "mapped": colmap,
        "parsed": len(rows),
        "equity": sum(1 for r in rows if r["asset_type"] == "equity"),
        "cash": sum(1 for r in rows if r["asset_type"] == "cash"),
        "skipped": skipped[:20],
        "skipped_total": len(skipped),
    }
    return rows, report


def parse_file(path, snapshot_date=None):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return parse_positions(f.read(), snapshot_date)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码文件")
