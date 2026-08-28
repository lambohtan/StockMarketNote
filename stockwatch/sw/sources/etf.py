"""ETF 成分股（yfinance funds_data）。

⚠️ 只能拿到前十大持仓 —— QQQ 前十大约占 46%，VOO 约占 38%，
剩下的权重无法穿透。所有基于此的结论都必须标注这个覆盖率，不能假装看到了全貌。
"""
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
from .base import SourceResult, timed


@timed("yfinance.etf")
def fetch_top_holdings(tickers):
    """返回 {etf: {"holdings": {sym: weight}, "coverage": 前十大合计权重, "sectors": {...}}}"""
    out, errs = {}, []
    for tk in tickers:
        try:
            fd = yf.Ticker(tk).funds_data
            th = fd.top_holdings
            if th is None or len(th) == 0:
                errs.append(f"{tk}:空")
                continue
            col = "Holding Percent" if "Holding Percent" in th.columns else th.columns[-1]
            hold = {str(i).upper(): float(th.loc[i, col]) for i in th.index}
            try:
                sectors = dict(fd.sector_weightings or {})
            except Exception:
                sectors = {}
            out[tk] = {"holdings": hold, "coverage": sum(hold.values()), "sectors": sectors}
        except Exception as e:
            errs.append(f"{tk}:{type(e).__name__}")
    return SourceResult(source="yfinance.etf", rows=len(out), data=out,
                        ok=len(out) > 0, detail=",".join(errs[:5]))
