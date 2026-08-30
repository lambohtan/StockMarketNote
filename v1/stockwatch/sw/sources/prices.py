"""yfinance 适配器。非官方接口，随时可能变 —— 所有取数都做防御。"""
import warnings, math, logging
warnings.filterwarnings("ignore")
# yfinance 会把「这只票在该区间没数据」打到 stderr，刷屏且盖住真正的输出。
# 覆盖率我们自己在报告里逐只标注，这里静音。
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
import yfinance as yf
import pandas as pd
from .base import SourceResult, timed


def _clean(tickers):
    return sorted(set(t for t in tickers if t and (str(t).replace("-", "").isalpha() or "." in str(t))))


def _to_rows(df, tickers):
    """把 yf.download 的宽表拍平成 (date, ticker, close, volume) 行。单票/多票两种形状都兼容。"""
    close = df["Close"] if "Close" in df.columns.get_level_values(0) else df
    vol = df["Volume"] if "Volume" in df.columns.get_level_values(0) else None
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    if vol is not None and isinstance(vol, pd.Series):
        vol = vol.to_frame(tickers[0])

    rows = []
    for d, r in close.iterrows():
        ds = d.date().isoformat()
        for tk in close.columns:
            c = r[tk]
            if c is None or (isinstance(c, float) and math.isnan(c)):
                continue
            v = None
            if vol is not None and tk in vol.columns:
                vv = vol.loc[d, tk]
                v = None if (vv is None or (isinstance(vv, float) and math.isnan(vv))) else float(vv)
            rows.append((ds, tk, float(c), v))
    return rows, f"{len(close.columns)} 只 × {len(close)} 天"


@timed("yfinance.prices")
def fetch_prices(tickers, period="2y"):
    tickers = _clean(tickers)
    if not tickers:
        return SourceResult(source="yfinance.prices", rows=0, data=[])
    df = yf.download(tickers, period=period, interval="1d",
                     progress=False, auto_adjust=True, group_by="column")
    if df is None or len(df) == 0:
        raise RuntimeError("yfinance 返回空数据")
    rows, detail = _to_rows(df, tickers)
    return SourceResult(source="yfinance.prices", rows=len(rows), data=rows, detail=detail)


@timed("yfinance.prices_range")
def fetch_prices_range(tickers, start, end):
    """指定日期区间取数。压力测试要回到 2018，用 period 参数够不着。"""
    tickers = _clean(tickers)
    if not tickers:
        return SourceResult(source="yfinance.prices_range", rows=0, data=[])
    df = yf.download(tickers, start=start, end=end, interval="1d",
                     progress=False, auto_adjust=True, group_by="column")
    if df is None or len(df) == 0:
        return SourceResult(source="yfinance.prices_range", rows=0, data=[],
                            ok=False, detail=f"{start}~{end} 无数据")
    rows, detail = _to_rows(df, tickers)
    return SourceResult(source="yfinance.prices_range", rows=len(rows), data=rows,
                        detail=f"{start}~{end} {detail}")


@timed("yfinance.meta")
def fetch_meta(tickers):
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    rows, bad = [], []
    for tk in tickers:
        try:
            info = yf.Ticker(tk).info or {}
            rows.append((tk, info.get("sector"), info.get("industry"),
                         info.get("shortName") or info.get("longName"),
                         info.get("marketCap"), now))
        except Exception as e:
            bad.append(f"{tk}:{type(e).__name__}")
    return SourceResult(source="yfinance.meta", rows=len(rows), data=rows,
                        ok=len(rows) > 0,
                        detail=("失败: " + ",".join(bad[:5])) if bad else "")


@timed("yfinance.earnings")
def fetch_earnings(ticker):
    """财报日期适配器，区分「窗口无日期」和「所有查询失败」。"""
    from datetime import datetime
    tk = yf.Ticker(ticker)
    errors = []
    try:
        cal = tk.calendar
        if isinstance(cal, dict):
            value = cal.get("Earnings Date")
            if value:
                value = value[0] if isinstance(value, (list, tuple)) else value
                return SourceResult(source="yfinance.earnings", rows=1,
                                    data=[str(value)])
        elif isinstance(cal, pd.DataFrame) and not cal.empty:
            return SourceResult(source="yfinance.earnings", rows=1,
                                data=[str(cal.iloc[0, 0])])
    except Exception as exc:
        errors.append(type(exc).__name__)
    try:
        ed = tk.get_earnings_dates(limit=4)
        if ed is not None and len(ed):
            return SourceResult(source="yfinance.earnings", rows=1,
                                data=[str(ed.index[0])])
    except Exception as exc:
        errors.append(type(exc).__name__)
    try:
        timestamp = (tk.info or {}).get("earningsTimestamp")
        if timestamp:
            return SourceResult(source="yfinance.earnings", rows=1,
                                data=[datetime.fromtimestamp(timestamp).isoformat()])
    except Exception as exc:
        errors.append(type(exc).__name__)
    return SourceResult(source="yfinance.earnings", rows=0, data=[],
                        ok=not errors,
                        detail=("查询失败：" + ",".join(errors[:3])) if errors
                                else "窗口内无财报日期")


def next_earnings(ticker):
    """兼容旧接口，只返回日期字符串；状态由 fetch_earnings 提供。"""
    result = fetch_earnings(ticker)
    return (result.data or [None])[0]
