#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟撮合引擎：初始虚拟资金，严格遵循 A 股交易规则，与真实券商物理隔离。
规则：T+1、整手 100 股、涨跌停限制、无卖空/无杠杆、佣金+印花税+过户费。
执行模型：信号在 T 日收盘生成，于 T+1 开盘撮合（避免未来函数，天然满足 T+1）。
"""
import datetime

from config import (
    COMMISSION_MIN,
    COMMISSION_RATE,
    INITIAL_CAPITAL,
    LOT_SIZE,
    MAX_POSITION_RATIO,
    STAMP_DUTY_RATE,
    STAR_PREFIXES,
    CHINEXT_PREFIXES,
    TRANSFER_FEE_RATE,
)
from db import get_conn, init_db
from strategy import ma_cross_signals


def _limit_factor(code: str) -> float:
    if code.startswith(STAR_PREFIXES) or code.startswith(CHINEXT_PREFIXES):
        return 0.20
    return 0.10


class SimEngine:
    def __init__(self, initial: float = INITIAL_CAPITAL):
        self.cash = initial
        self.positions = {}      # code -> {shares, avg_cost, buy_date, name}
        self.transactions = []   # 成交记录
        self.equity = []         # (date, cash, market_value, total)
        self.fee_total = 0.0
        self.tax_total = 0.0

    # ---------- 撮合核心 ----------
    def _limit_prices(self, code: str, pre_close: float):
        f = _limit_factor(code)
        up = round(pre_close * (1 + f), 2)
        dn = round(pre_close * (1 - f), 2)
        return up, dn

    def try_buy(self, date, code, name, price, row) -> bool:
        # 涨跌停：开盘即涨停无法买入
        up, _ = self._limit_prices(code, float(row["pre_close"]))
        if price >= up - 0.01:
            return False
        if code in self.positions:
            return False  # 不追加，保持简单
        budget = self.cash * MAX_POSITION_RATIO
        shares = int(budget // (price * LOT_SIZE)) * LOT_SIZE
        if shares <= 0:
            return False
        cost = shares * price
        fee = max(cost * COMMISSION_RATE, COMMISSION_MIN)
        transfer = cost * TRANSFER_FEE_RATE
        total = cost + fee + transfer
        if total > self.cash:
            shares = int((self.cash - fee) // (price * LOT_SIZE)) * LOT_SIZE
            if shares <= 0:
                return False
            cost = shares * price
            fee = max(cost * COMMISSION_RATE, COMMISSION_MIN)
            transfer = cost * TRANSFER_FEE_RATE
            total = cost + fee + transfer
        self.cash -= total
        self.positions[code] = {"shares": shares, "avg_cost": price, "buy_date": date, "name": name}
        self.fee_total += fee + transfer
        self.transactions.append({
            "date": date, "code": code, "name": name, "action": "BUY",
            "price": price, "shares": shares, "amount": cost,
            "fee": fee + transfer, "tax": 0.0, "realized_pnl": None,
            "reason": "MA 金叉", "created_at": _now(),
        })
        return True

    def try_sell(self, date, code, name, price, row) -> bool:
        pos = self.positions.get(code)
        if not pos:
            return False
        if pos["buy_date"] >= date:   # T+1：当日买入不可卖
            return False
        # 涨跌停：开盘即跌停无法卖出
        _, dn = self._limit_prices(code, float(row["pre_close"]))
        if price <= dn + 0.01:
            return False
        shares = pos["shares"]
        proceeds = shares * price
        fee = max(proceeds * COMMISSION_RATE, COMMISSION_MIN)
        tax = proceeds * STAMP_DUTY_RATE
        transfer = proceeds * TRANSFER_FEE_RATE
        realized = proceeds - fee - tax - transfer - shares * pos["avg_cost"]
        self.cash += proceeds - fee - tax - transfer
        self.fee_total += fee + transfer
        self.tax_total += tax
        del self.positions[code]
        self.transactions.append({
            "date": date, "code": code, "name": name, "action": "SELL",
            "price": price, "shares": shares, "amount": proceeds,
            "fee": fee + transfer, "tax": tax, "realized_pnl": realized,
            "reason": "MA 死叉", "created_at": _now(),
        })
        return True

    def mark_equity(self, date, close_lookup):
        mv = 0.0
        for code, pos in self.positions.items():
            c = close_lookup.get(code, {}).get(date)
            if c is not None:
                mv += pos["shares"] * c
        total = self.cash + mv
        self.equity.append((date, self.cash, mv, total))


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def simulate(hist: dict, initial: float = INITIAL_CAPITAL, mode: str = "rule", signal_fn=None):
    """组合级历史模拟：多标的共享一个现金账户，信号 T 日生成、T+1 开盘撮合。
    signal_fn: 接收 df 返回 signal 序列；缺省使用 MA20/MA60 交叉。
    """
    if signal_fn is None:
        signal_fn = ma_cross_signals
    engine = SimEngine(initial)
    prepared = {}
    close_lookup = {}
    for code, df in hist.items():
        df = df.sort_values("date").reset_index(drop=True)
        df["signal"] = signal_fn(df)
        df["exec_signal"] = df["signal"].shift(1).fillna(0).astype(int)  # 次日开盘执行
        prepared[code] = df
        close_lookup[code] = dict(zip(df["date"], df["close"]))

    all_dates = sorted({d for df in prepared.values() for d in df["date"]})
    for date in all_dates:
        # 先卖后买：释放的现金可用于当日买入
        for code, df in prepared.items():
            row = df[df["date"] == date]
            if row.empty:
                continue
            row = row.iloc[0]
            es = int(row["exec_signal"])
            if es == -1:
                engine.try_sell(date, code, row["name"], float(row["open"]), row)
        for code, df in prepared.items():
            row = df[df["date"] == date]
            if row.empty:
                continue
            row = row.iloc[0]
            es = int(row["exec_signal"])
            if es == 1:
                engine.try_buy(date, code, row["name"], float(row["open"]), row)
        engine.mark_equity(date, close_lookup)
    return engine


def reset_simulation():
    """清空模拟产生的数据（成交/净值/持仓/现金），便于重新跑。"""
    conn = get_conn()
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM equity_daily")
    conn.execute("DELETE FROM positions")
    conn.execute("DELETE FROM account")
    conn.execute("INSERT INTO account(key,value) VALUES('cash',?)", (INITIAL_CAPITAL,))
    conn.commit()
    conn.close()


def save_to_db(engine, mode: str = "rule"):
    """把一次模拟结果持久化到 SQLite（成交/净值/持仓/现金/决策）。"""
    init_db()
    conn = get_conn()
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM equity_daily")
    conn.execute("DELETE FROM positions")
    conn.execute("DELETE FROM account")
    conn.execute("INSERT INTO account(key,value) VALUES('cash',?)", (engine.cash,))
    for t in engine.transactions:
        conn.execute(
            """INSERT INTO transactions(date,code,name,action,price,shares,amount,fee,tax,realized_pnl,reason,created_at)
               VALUES(:date,:code,:name,:action,:price,:shares,:amount,:fee,:tax,:realized_pnl,:reason,:created_at)""",
            t,
        )
    for d, cash, mv, total in engine.equity:
        pct = (total - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100 if engine.equity[0][3] else 0
        conn.execute(
            "INSERT OR REPLACE INTO equity_daily(date,cash,market_value,total,pct_change) VALUES(?,?,?,?,?)",
            (d, cash, mv, total, pct),
        )
    for code, pos in engine.positions.items():
        conn.execute(
            "INSERT OR REPLACE INTO positions(code,name,shares,avg_cost,buy_date) VALUES(?,?,?,?,?)",
            (code, pos["name"], pos["shares"], pos["avg_cost"], pos["buy_date"]),
        )
    conn.commit()
    conn.close()
