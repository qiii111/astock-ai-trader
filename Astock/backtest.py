#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测模块（P2）：在统一模拟引擎上对比不同确定性策略。
输出总收益率、最大回撤、胜率、交易次数，便于策略优选。
所有交易均为虚拟撮合，与真实券商无任何连接。
"""
from config import INITIAL_CAPITAL
from engine import simulate
from stats import compute_stats
from strategy import STRATEGIES


def run_strategy(hist: dict, strategy_name: str, initial: float = INITIAL_CAPITAL) -> dict:
    """对某一策略跑历史模拟，返回统计（含策略名）。"""
    sig_fn = STRATEGIES[strategy_name]
    engine = simulate(hist, initial, mode="rule", signal_fn=sig_fn)
    st = compute_stats(engine)
    st["strategy"] = strategy_name
    return st


def compare(hist: dict, initial: float = INITIAL_CAPITAL) -> list:
    """对比全部已注册策略，按期末净值降序返回。"""
    results = [run_strategy(hist, name, initial) for name in STRATEGIES]
    results.sort(key=lambda r: r["final_total"], reverse=True)
    return results


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from fetcher import load_history_from_db
    from indicators import attach_indicators
    from config import WATCHLIST, MA_FAST, MA_SLOW

    hist = load_history_from_db()
    for code in hist:
        if "ma20" not in hist[code].columns:
            hist[code] = attach_indicators(hist[code], MA_FAST, MA_SLOW)
    for r in compare(hist):
        print(f"{r['strategy']:10} 期末={r['final_total']:.2f} 收益={r['total_return']*100:6.2f}% "
              f"回撤={r['max_drawdown']*100:5.2f}% 胜率={r['win_rate']*100:4.1f}% 交易={r['trade_count']}")
