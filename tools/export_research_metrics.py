#!/usr/bin/env python3
"""Public ETF/stock research metrics. No private portfolio, credentials, or orders."""
import datetime as dt
import json
import math
import re
import urllib.request
from pathlib import Path

# Public research universe, not derived from private accounts.
UNIVERSE = {
    "510300": ("沪深300ETF", "宽基ETF"),
    "159338": ("中证A500ETF", "宽基ETF"),
    "588000": ("科创50ETF", "成长ETF"),
    "512890": ("红利低波ETF", "红利ETF"),
    "515300": ("300红利低波ETF", "红利ETF"),
    "600900": ("长江电力", "红利股"),
    "601398": ("工商银行", "红利股"),
    "003816": ("中国广核", "红利股"),
    "000582": ("北部湾港", "周期股"),
    "513180": ("恒指科技ETF", "成长ETF"),
    "159549": ("红利100ETF", "红利ETF"),
    "159692": ("证券30ETF", "行业ETF"),
    "520920": ("恒生科TH", "成长ETF"),
}
def market(code):
    return "sh" if code.startswith(("5", "6", "9")) else "sz"

def stock_fundamentals(code):
    """Public quote-level valuation only; NOT audited financial statement data."""
    symbol = market(code) + code
    url = "https://qt.gtimg.cn/q=" + symbol
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as res:
        payload = res.read().decode("gbk", errors="replace")
    match = re.search(r'="([^"]+)"', payload)
    if not match:
        raise ValueError("quote missing")
    fields = match.group(1).split("~")
    if len(fields) < 47 or fields[2] != code:
        raise ValueError("unexpected fields")
    # Tencent quote fields: 39 = P/E TTM, 46 = P/B.
    def positive(index):
        value = float(fields[index])
        return round(value, 3) if math.isfinite(value) and value > 0 else None
    pe, pb = positive(39), positive(46)
    if pe is None and pb is None:
        raise ValueError("valuation missing")
    return {"pe_ttm": pe, "pb": pb, "source": "Tencent public quote fields",
            "financial_report_verified": False}

def history(code):
    # Tencent daily-adjusted? qfq history is adjusted; used for percentage changes only.
    symbol = market(code) + code
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + symbol + ",day,,,65,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as res:
        payload = json.load(res)
    data = (payload.get("data") or {}).get(symbol) or {}
    candles = data.get("qfqday") or data.get("day") or []
    series = []
    for row in candles:
        try:
            date = dt.date.fromisoformat(row[0])
            close = float(row[2])
            if close > 0 and math.isfinite(close):
                series.append((date, close))
        except (ValueError, TypeError, IndexError):
            continue
    series.sort()
    if len(series) < 21:
        raise ValueError("insufficient history")
    closes = [v for _, v in series]
    daily = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
    recent = daily[-20:]
    avg = sum(recent) / len(recent)
    vol = (sum((v-avg)**2 for v in recent) / len(recent)) ** 0.5 * (252**0.5) * 100
    return {
        "as_of": series[-1][0].isoformat(),
        "return_20d_pct": round((closes[-1] / closes[-21] - 1) * 100, 2),
        "volatility_20d_annual_pct": round(vol, 2),
        "drawdown_60d_pct": round((closes[-1] / max(closes[-60:]) - 1) * 100, 2),
        "observations": len(series),
    }

results = {}
errors = {}
today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
for code, (name, category) in UNIVERSE.items():
    record = {"name": name, "category": category, "fundamentals": None}
    try:
        metrics = history(code)
        age = (today - dt.date.fromisoformat(metrics["as_of"])).days
        if age < 0 or age > 10:
            raise ValueError("stale history")
        record.update(metrics)
        # Price momentum / volatility screening only, NOT a fundamental or buy score.
        trend = 50 + max(-30, min(30, metrics["return_20d_pct"] * 2))
        risk = max(0, 20 - max(0, metrics["volatility_20d_annual_pct"] - 15) * 0.5)
        record["technical_score_100"] = round(trend + risk, 1)
        record["technical_label"] = "走势观察分（非买入评级）"
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        errors[code] = type(exc).__name__
    if category in ("红利股", "周期股"):\n        try:\n            record["fundamentals"] = stock_fundamentals(code)\n        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:\n            errors[code + "_valuation"] = type(exc).__name__\n    results[code] = record

out = Path("docs/research_metrics.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "source": "Tencent public historical daily K-line",
    "methodology": "Technical screen only; stock P/E and P/B are unverified quote fields, not financial-report analysis",
    "securities": results, "errors": errors,
}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("Research metrics available:", len(results)-len(errors), "/", len(results), "errors:", errors)
