#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略层：以确定性规则负责"执行"，AI 负责"分析/风控"（绝不代发单）。
已实现：
  · ma_cross_signals  MA20/MA60 金叉死叉（基准策略）
  · macd_signals      MACD 金叉死叉（第二策略，用于对照回测）
  · ma250_trend       年线多空（仓位偏向过滤）
统一决策接口 decide() 预留「规则模式 / AI 决策模式」；当前仅启用规则模式，AI 模式返回 HOLD。
"""
import pandas as pd

from config import MA_FAST, MA_SLOW


def ma_cross_signals(df: pd.DataFrame, fast: int = MA_FAST, slow: int = MA_SLOW) -> pd.Series:
    """与 df 对齐的 signal：1=金叉(买) -1=死叉(卖) 0=无。"""
    ma_f = df["close"].rolling(fast).mean()
    ma_s = df["close"].rolling(slow).mean()
    diff = ma_f - ma_s
    prev = diff.shift(1)
    sig = pd.Series(0, index=df.index)
    sig[(prev <= 0) & (diff > 0)] = 1   # 金叉
    sig[(prev >= 0) & (diff < 0)] = -1  # 死叉
    return sig


def macd_signals(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD 金叉(买)/死叉(卖)。"""
    from indicators import macd
    dif = macd(df["close"], fast, slow, signal)[0]
    dea = macd(df["close"], fast, slow, signal)[1]
    pdif, pdea = dif.shift(1), dea.shift(1)
    sig = pd.Series(0, index=df.index)
    sig[(pdif <= pdea) & (dif > dea)] = 1
    sig[(pdif >= pdea) & (dif < dea)] = -1
    return sig


def ma250_trend(df: pd.DataFrame, n: int = 250) -> pd.Series:
    """年线多空：收盘价在 250 日均线上方为 1（多头），否则 0（空头）。"""
    ma = df["close"].rolling(n).mean()
    return (df["close"] > ma).astype(int)


STRATEGIES = {
    "ma_cross": ma_cross_signals,
    "macd": macd_signals,
}


def decide(code: str, df: pd.DataFrame, mode: str = "rule", strategy: str = "ma_cross", fast: int = MA_FAST, slow: int = MA_SLOW) -> dict:
    """统一决策接口。
    mode='rule'：基于指定确定性策略（默认 MA 交叉）。
    mode='ai' ：预留，待后续接入 AI 决策（暂不启用，返回 HOLD）。
    """
    if mode == "ai":
        # 接口预留：本期不启用，避免黑箱下单
        return {"code": code, "signal": 0, "action": "HOLD", "reason": "AI 模式未启用", "mode": mode}

    sig_fn = STRATEGIES.get(strategy, ma_cross_signals)
    sig = sig_fn(df, fast, slow) if strategy == "ma_cross" else sig_fn(df)
    latest = int(sig.iloc[-1])
    if latest == 1:
        action, reason = "BUY", f"{strategy} 金叉（买入信号）"
    elif latest == -1:
        action, reason = "SELL", f"{strategy} 死叉（卖出信号）"
    else:
        action, reason = "HOLD", f"{strategy} 无交叉信号"
    return {"code": code, "signal": latest, "action": action, "reason": reason, "mode": mode, "strategy": strategy}
