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
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + symbol + ",day,,,130,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as res:
        payload = json.load(res)
    data = (payload.get("data") or {}).get(symbol) or {}
    candles = data.get("qfqday") or data.get("day") or []
    series = []
    ohlcv = []
    for row in candles:
        try:
            date = dt.date.fromisoformat(row[0])
            close = float(row[2])
            high, low, volume = float(row[3]), float(row[4]), float(row[5])
            if close > 0 and low > 0 and high >= low and volume >= 0 and all(math.isfinite(v) for v in (close, high, low, volume)):
                series.append((date, close))
                ohlcv.append((date, high, low, volume))
        except (ValueError, TypeError, IndexError):
            continue
    series.sort()
    ohlcv.sort()
    if len(series) < 60:
        raise ValueError("insufficient history")
    closes = [v for _, v in series]
    daily = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
    recent = daily[-20:]
    avg = sum(recent) / len(recent)
    vol = (sum((v-avg)**2 for v in recent) / len(recent)) ** 0.5 * (252**0.5) * 100
    def ema(values, n):
        a = 2 / (n + 1)
        result = [values[0]]
        for v in values[1:]:
            result.append(a * v + (1 - a) * result[-1])
        return result
    fast, slow = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    bars = [2 * (a - b) for a, b in zip(dif, dea)]
    k, d, kdjs = 50.0, 50.0, []
    for i, close in enumerate(closes):
        window = ohlcv[max(0, i - 8):i + 1]
        high = max(x[1] for x in window)
        low = min(x[2] for x in window)
        rsv = 50 if high == low else (close - low) / (high - low) * 100
        k = 2 * k / 3 + rsv / 3
        d = 2 * d / 3 + k / 3
        kdjs.append((k, d, 3 * k - 2 * d))
    def cross(a0, b0, a1, b1):
        return "金叉" if a0 <= b0 and a1 > b1 else ("死叉" if a0 >= b0 and a1 < b1 else "无新交叉")
    volumes = [x[3] for x in ohlcv]
    technical = {
        "macd": {"dif": round(dif[-1], 4), "dea": round(dea[-1], 4),
                 "histogram": round(bars[-1], 4),
                 "cross": cross(dif[-2], dea[-2], dif[-1], dea[-1]),
                 "position": "零轴上方" if dif[-1] > 0 else "零轴下方",
                 "momentum": "增强" if bars[-1] > bars[-2] else "减弱"},
        "kdj": {"k": round(kdjs[-1][0], 2), "d": round(kdjs[-1][1], 2),
                "j": round(kdjs[-1][2], 2),
                "cross": cross(kdjs[-2][0], kdjs[-2][1], kdjs[-1][0], kdjs[-1][1]),
                "direction": "向上" if kdjs[-1][0] > kdjs[-2][0] else "向下",
                "zone": "超买区域" if kdjs[-1][1] >= 80 else ("超卖区域" if kdjs[-1][1] <= 20 else "中性区域")},
        "ma": {str(n): round(sum(closes[-n:]) / n, 3) for n in (5, 10, 20, 60)},
        "volume_ratio_5_20": round((sum(volumes[-5:]) / 5) / (sum(volumes[-20:]) / 20), 2) if sum(volumes[-20:]) else None,
        "capital_flow": None
    }
    return {
        "technical": technical,
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
    if category in ("红利股", "周期股"):
        try:
            record["fundamentals"] = stock_fundamentals(code)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            errors[code + "_valuation"] = type(exc).__name__
    results[code] = record

out = Path("docs/research_metrics.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "source": "Tencent public historical daily K-line",
    "methodology": "Technical screen only; stock P/E and P/B are unverified quote fields, not financial-report analysis",
    "securities": results, "errors": errors,
}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("Research metrics available:", len(results)-len(errors), "/", len(results), "errors:", errors)
