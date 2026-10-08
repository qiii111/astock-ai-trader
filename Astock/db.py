#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite 账本层（替代原项目的 MySQL）。
仅用标准库 sqlite3，零额外依赖。表结构覆盖：行情、关注列表、持仓、
账户现金、成交、每日净值、日志、决策（AI 模式预留）。
"""
import os
import sqlite3

from config import DB_PATH, DATA_DIR, INITIAL_CAPITAL


def get_conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_quotes(
            code    TEXT,
            date    TEXT,
            open    REAL, close REAL, high REAL, low REAL,
            volume  REAL, amount REAL, pct_change REAL, pre_close REAL,
            ma20    REAL, ma60 REAL,
            PRIMARY KEY(code, date)
        );
        CREATE TABLE IF NOT EXISTS watchlist(
            code TEXT PRIMARY KEY, name TEXT, market INTEGER
        );
        CREATE TABLE IF NOT EXISTS positions(
            code TEXT PRIMARY KEY, name TEXT,
            shares INTEGER, avg_cost REAL, buy_date TEXT
        );
        CREATE TABLE IF NOT EXISTS account(
            key TEXT PRIMARY KEY, value REAL
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, code TEXT, name TEXT, action TEXT,
            price REAL, shares INTEGER, amount REAL, fee REAL,
            tax REAL, realized_pnl REAL, reason TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS equity_daily(
            date TEXT PRIMARY KEY, cash REAL, market_value REAL,
            total REAL, pct_change REAL
        );
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, level TEXT, message TEXT
        );
        CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, code TEXT, mode TEXT, signal TEXT,
            action TEXT, reason TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sched_evidence(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, run_key TEXT, payload TEXT
        );
        CREATE TABLE IF NOT EXISTS live_push(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, date TEXT, payload TEXT
        );
        """
    )
    # 为已存在的旧库补充指标列（向前兼容）
    for col in ("ma20", "ma60"):
        try:
            conn.execute(f"ALTER TABLE daily_quotes ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass
    # 为已存在的旧库补充印花税列（向前兼容）
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN tax REAL")
    except sqlite3.OperationalError:
        pass
    # 为已存在的旧库补充调度证据表（向前兼容）
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS sched_evidence(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_key TEXT, payload TEXT)")
    except sqlite3.OperationalError:
        pass
    # 为已存在的旧库补充实时推送表（向前兼容）
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS live_push(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, date TEXT, payload TEXT)")
    except sqlite3.OperationalError:
        pass
    # 初始化账户现金
    conn.execute(
        "INSERT OR IGNORE INTO account(key, value) VALUES('cash', ?)",
        (INITIAL_CAPITAL,),
    )
    conn.commit()
    conn.close()


def log(level: str, message: str) -> None:
    import datetime
    conn = get_conn()
    conn.execute(
        "INSERT INTO logs(ts, level, message) VALUES(?,?,?)",
        (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, message),
    )
    conn.commit()
    conn.close()
