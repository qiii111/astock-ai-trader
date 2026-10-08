#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术指标层：纯 pandas / numpy 实现，替代 TA-Lib（避免 C 扩展安装）。
公式参考 InStock 已校准口径（与同花顺/通达信一致）。
仅实现策略与展示所需指标：MA、MACD、KDJ、RSI、BOLL。
"""
import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9):
    low_min = low.rolling(n).min()
    high_max = high.rolling(n).max()
    rsv = (close - low_min) / (high_max - low_min) * 100
    rsv = rsv.fillna(50)
    k = sma(rsv, 3)
    d = sma(k, 3)
    j = 3 * k - 2 * d
    return k, d, j


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0)
    dn = -diff.clip(upper=0)
    rs = up.rolling(n).mean() / dn.rolling(n).mean()
    return 100 - 100 / (1 + rs)


def boll(close: pd.Series, n: int = 20, m: int = 2):
    mid = close.rolling(n).mean()
    std = close.rolling(n).std(ddof=0)
    return mid, mid + m * std, mid - m * std


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's ATR（真实波幅均值），用于止损距离与波动率风控。"""
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def attach_indicators(df: pd.DataFrame, fast: int = 20, slow: int = 60) -> pd.DataFrame:
    """在日线 DataFrame 上追加指标列，返回新 DataFrame。"""
    close, high, low = df["close"], df["high"], df["low"]
    df = df.copy()
    df["ma20"] = sma(close, fast)
    df["ma60"] = sma(close, slow)
    dif, dea, hist = macd(close)
    df["macd_dif"], df["macd_dea"], df["macd_hist"] = dif, dea, hist
    k, d_, j = kdj(high, low, close)
    df["kdj_k"], df["kdj_d"], df["kdj_j"] = k, d_, j
    df["rsi14"] = rsi(close)
    mid, up, lowb = boll(close)
    df["boll_mid"], df["boll_up"], df["boll_low"] = mid, up, lowb
    df["atr14"] = atr(high, low, close)
    return df


def latest_indicators(df: pd.DataFrame) -> dict:
    """取末行（最新交易日）的指标快照，供控制台展示与 AI 分析消费。"""
    d = attach_indicators(df)
    r = d.iloc[-1]
    def _v(x):
        try:
            return None if pd.isna(x) else float(x)
        except Exception:
            return None
    return {
        "close": _v(r["close"]),
        "ma20": _v(r["ma20"]), "ma60": _v(r["ma60"]),
        "macd_dif": _v(r["macd_dif"]), "macd_dea": _v(r["macd_dea"]), "macd_hist": _v(r["macd_hist"]),
        "kdj_k": _v(r["kdj_k"]), "kdj_d": _v(r["kdj_d"]), "kdj_j": _v(r["kdj_j"]),
        "rsi14": _v(r["rsi14"]),
        "boll_mid": _v(r["boll_mid"]), "boll_up": _v(r["boll_up"]), "boll_low": _v(r["boll_low"]),
        "atr14": _v(r["atr14"]),
    }
