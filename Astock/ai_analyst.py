#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 分析模块（P4）— 脚手架 / 接口预留，本期【不启用】真实 AI 调用。

定位：AI 是"分析师 + 风控官"，不是"下单员"。
本模块负责把数据库中的行情、指标、信号、持仓、风险指标组装成结构化上下文，
供未来的 AI 决策/日报消费。真实 AI 调用仅在 DECISION_MODE='ai' 且显式接入后进行，
且 AI 只产出"建议/解读/风险提示"，不直接发单（发单永远由确定性规则 + 隔离模拟引擎执行）。
"""
from config import INITIAL_CAPITAL, MAX_POSITION_RATIO
from db import get_conn
from fetcher import load_history_from_db
from indicators import attach_indicators, latest_indicators
from strategy import decide, ma250_trend
from stats import compute_stats_from_db


def build_analysis_context(conn) -> dict:
    """汇总当前账户、持仓、各标的指标与信号，形成 AI 可读的上下文。"""
    s = compute_stats_from_db(conn)
    positions = conn.execute(
        "SELECT code,name,shares,avg_cost,buy_date FROM positions ORDER BY code"
    ).fetchall()
    decisions = conn.execute(
        "SELECT d.code, COALESCE(w.name,d.code) AS name, d.action, d.reason "
        "FROM decisions d LEFT JOIN watchlist w ON d.code=w.code ORDER BY d.id DESC LIMIT 10"
    ).fetchall()

    hist = load_history_from_db()
    quotes_ctx = []
    for code, df in hist.items():
        if "ma20" not in df.columns:
            df = attach_indicators(df)
        ind = latest_indicators(df)
        trend = int(ma250_trend(df).iloc[-1])
        d = decide(code, df, mode="rule")
        name = dict((p["code"], p["name"]) for p in positions)  # 仅用于回退
        quotes_ctx.append({
            "code": code, "close": ind["close"], "ma20": ind["ma20"], "ma60": ind["ma60"],
            "rsi14": ind["rsi14"], "macd_hist": ind["macd_hist"], "atr14": ind["atr14"],
            "above_ma250": bool(trend), "signal": d["action"], "reason": d["reason"],
        })

    return {
        "account": {
            "initial": INITIAL_CAPITAL, "final_total": s["final_total"],
            "total_return": s["total_return"], "max_drawdown": s["max_drawdown"],
            "cash": s["cash"], "market_value": s["market_value"],
            "open_positions": s["open_positions"], "trade_count": s["trade_count"],
            "max_position_ratio_limit": MAX_POSITION_RATIO,
        },
        "positions": [dict(p) for p in positions],
        "decisions": [dict(d) for d in decisions],
        "quotes": quotes_ctx,
    }


def build_analysis_prompt(conn) -> str:
    """把上下文渲染为给 AI 的提示词（不在此处发起 AI 调用）。"""
    ctx = build_analysis_context(conn)
    a = ctx["account"]
    lines = [
        "你是 A 股模拟交易系统的分析师与风控官（仅分析，绝不直接下单）。请基于以下数据给出：",
        "1) 当前账户与持仓的中文解读；2) 各标的指标/信号的风险提示；3) 是否出现与规则信号明显背离需关注的情况。",
        "",
        f"[账户] 初始 {a['initial']:.0f} 元，当前总资产 {a['final_total']:.2f} 元，收益率 {a['total_return']*100:.2f}%，"
        f"最大回撤 {a['max_drawdown']*100:.2f}%，现金 {a['cash']:.2f} 元，持仓市值 {a['market_value']:.2f} 元，"
        f"当前持仓 {a['open_positions']} 只，累计成交 {a['trade_count']} 笔，单票上限 {a['max_position_ratio_limit']*100:.0f}%。",
        "",
        "[持仓]",
    ]
    for p in ctx["positions"]:
        lines.append(f"  · {p['code']} {p['name']} {p['shares']} 股 @成本 {p['avg_cost']:.2f}（买入 {p['buy_date']}）")
    lines.append("")
    lines.append("[各标的指标与信号]")
    for q in ctx["quotes"]:
        lines.append(
            f"  · {q['code']} 收 {q['close']} MA20 {q['ma20']} MA60 {q['ma60']} "
            f"RSI {q['rsi14']} MACD柱 {q['macd_hist']} ATR {q['atr14']} "
            f"年线{'上方' if q['above_ma250'] else '下方'} | 信号 {q['signal']}（{q['reason']}）"
        )
    return "\n".join(lines)


def decide_ai(conn) -> str:
    """预留：未来接入 AI 决策时调用。本期未启用，仅返回提示词文本。"""
    return build_analysis_prompt(conn)
