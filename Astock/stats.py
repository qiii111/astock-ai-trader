#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收益统计：总收益率、最大回撤、胜率、交易次数、已实现盈亏。
读取模拟引擎结果（也可后续从 DB 读取）。
"""
from config import INITIAL_CAPITAL


def compute_stats_from_db(conn) -> dict:
    """从 SQLite 读取净值与成交，计算收益统计（供控制台展示）。"""
    eq = conn.execute(
        "SELECT date,total FROM equity_daily ORDER BY date"
    ).fetchall()
    totals = [r["total"] for r in eq]
    if not totals:
        return {"initial": INITIAL_CAPITAL, "final_total": INITIAL_CAPITAL,
                "total_return": 0.0, "max_drawdown": 0.0,
                "win_rate": 0.0, "trade_count": 0, "buy_count": 0,
                "sell_count": 0, "total_realized_pnl": 0.0, "fee_total": 0.0,
                "tax_total": 0.0, "open_positions": 0}
    final = totals[-1]
    total_return = (final - INITIAL_CAPITAL) / INITIAL_CAPITAL
    peak = totals[0]
    mdd = 0.0
    for t in totals:
        if t > peak:
            peak = t
        dd = (peak - t) / peak if peak else 0
        if dd > mdd:
            mdd = dd
    sells = conn.execute(
        "SELECT realized_pnl FROM transactions WHERE action='SELL'"
    ).fetchall()
    wins = [s["realized_pnl"] for s in sells if (s["realized_pnl"] or 0) > 0]
    losses = [s["realized_pnl"] for s in sells if (s["realized_pnl"] or 0) <= 0]
    win_rate = (len(wins) / len(sells)) if sells else 0.0
    total_realized = sum((s["realized_pnl"] or 0) for s in sells)
    buys = conn.execute("SELECT count(*) c FROM transactions WHERE action='BUY'").fetchone()["c"]
    sells_n = len(sells)
    fees = conn.execute("SELECT COALESCE(SUM(fee),0) f FROM transactions").fetchone()["f"]
    taxes = conn.execute("SELECT COALESCE(SUM(tax),0) t FROM transactions").fetchone()["t"]
    open_pos = conn.execute("SELECT count(*) c FROM positions").fetchone()["c"]
    cash = conn.execute("SELECT value FROM account WHERE key='cash'").fetchone()
    cash = cash["value"] if cash else INITIAL_CAPITAL
    return {
        "initial": INITIAL_CAPITAL,
        "final_total": final,
        "cash": cash,
        "market_value": final - cash,
        "total_return": total_return,
        "max_drawdown": mdd,
        "win_rate": win_rate,
        "trade_count": buys + sells_n,
        "buy_count": buys,
        "sell_count": sells_n,
        "total_realized_pnl": total_realized,
        "fee_total": fees,
        "tax_total": taxes,
        "cost_total": fees + taxes,
        "open_positions": open_pos,
        "equity_points": len(totals),
    }


def compute_stats(engine) -> dict:
    equity = engine.equity
    if not equity:
        return {"error": "无净值数据"}
    totals = [e[3] for e in equity]
    final = totals[-1]
    total_return = (final - INITIAL_CAPITAL) / INITIAL_CAPITAL

    peak = totals[0]
    mdd = 0.0
    for t in totals:
        if t > peak:
            peak = t
        dd = (peak - t) / peak if peak else 0
        if dd > mdd:
            mdd = dd

    sells = [t for t in engine.transactions if t["action"] == "SELL"]
    wins = [s for s in sells if (s["realized_pnl"] or 0) > 0]
    losses = [s for s in sells if (s["realized_pnl"] or 0) <= 0]
    win_rate = (len(wins) / len(sells)) if sells else 0.0
    total_realized = sum((s["realized_pnl"] or 0) for s in sells)

    buys = [t for t in engine.transactions if t["action"] == "BUY"]
    return {
        "initial": INITIAL_CAPITAL,
        "final_total": final,
        "cash": engine.cash,
        "market_value": final - engine.cash,
        "total_return": total_return,
        "annualized_note": "历史区间收益，非年化",
        "max_drawdown": mdd,
        "trade_count": len(buys) + len(sells),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "win_rate": win_rate,
        "total_realized_pnl": total_realized,
        "fee_total": engine.fee_total,
        "tax_total": engine.tax_total,
        "open_positions": len(engine.positions),
    }
