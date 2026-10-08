#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_job.py —— 定时任务专用「自包含作业」（仅标准库，可在隔离环境独立运行）。

闭环职责（问题 #3）：
  1. 行情更新：多源抓取 + 缓存回退 + 时间戳校验（marketdata.py 内联版本）
  2. 策略计算：MA20/MA60 与 MACD（可配置）
  3. 模拟成交：按 A 股规则撮合（T+1、整手、涨跌停、佣金+印花税+过户费）
  4. 持仓更新 + 资金更新：双账户（real22000 / paper100k）
  5. 结果同步：把账户状态 + 执行回执推送到已发布控制台
  6. 幂等：trade_key 与 run_key 双重去重，重复执行不重复成交（问题 #4）

用法：
  python3.11 daily_job.py                 # 默认规则=MA，账户=双账户
  python3.11 daily_job.py --strategy macd
  python3.11 daily_job.py --account real22000
  python3.11 daily_job.py --dry-run       # 只计算不写账本
  python3.11 daily_job.py --selftest      # 离线自检（不联网），用于验证闭环逻辑
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
DB_PATH = os.path.join(DATA_DIR, "astock.db")

# 控制台回执推送（可选）。凭据一律来自环境变量，禁止写入代码/仓库。
#   ASTOCK_PUSH_URL    例如 https://<your-console-host>/api/live_push
#   ASTOCK_INGEST_TOKEN 与控制台一致的写入令牌
PUSH_URL = os.environ.get("ASTOCK_PUSH_URL", "")
INGEST_TOKEN = os.environ.get("ASTOCK_INGEST_TOKEN", "")

# ---------- 关注标的 ----------
WATCHLIST = [
    ("600519", "贵州茅台", 1),
    ("600036", "招商银行", 1),
    ("601318", "中国平安", 1),
    ("000001", "平安银行", 0),
    ("300750", "宁德时代", 0),
    ("000858", "五粮液", 0),
]

ACCOUNTS = {
    "real22000": {"initial": 22000.0, "label": "实盘对标（2.2万）"},
    "paper100k": {"initial": 100000.0, "label": "对照账户（10万）"},
}

# ---------- A 股规则 ----------
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_RATE = 0.0005
TRANSFER_RATE = 0.00001
LOT = 100
LIMIT_MAIN = 0.10
LIMIT_STAR = 0.20
MAX_POS_RATIO = 0.40
TIMEOUT = 12
RETRIES = 4
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}

EASTMONEY = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


# ============ 工具 ============
def _today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# ============ 多源行情 ============
def _src_eastmoney(code, market, limit):
    params = {"secid": f"{market}.{code}", "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f59,f60",
              "klt": "101", "fqt": "1", "beg": "20230101", "end": "20500101", "lmt": str(limit)}
    data = json.loads(_get(EASTMONEY + "?" + urllib.parse.urlencode(params))).get("data") or {}
    rows = []
    for k in (data.get("klines") or []):
        p = k.split(",")
        if len(p) < 9:
            continue
        rows.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                     "high": float(p[3]), "low": float(p[4]),
                     "pct_change": float(p[7]), "pre_close": float(p[8])})
    if not rows:
        raise ValueError("eastmoney empty")
    return rows


def _src_tencent(code, market, limit):
    sym = ("sh" if market == 1 else "sz") + code
    url = TENCENT + "?" + urllib.parse.urlencode({"param": f"{sym},day,,,{limit},qfq"})
    node = (json.loads(_get(url)).get("data") or {}).get(sym) or {}
    klines = node.get("qfqday") or node.get("day") or []
    rows, prev = [], None
    for k in klines:
        if len(k) < 5:
            continue
        o, c = float(k[1]), float(k[2])
        pct = ((c / prev - 1) * 100) if prev else 0.0
        rows.append({"date": k[0], "open": o, "close": c, "high": float(k[3]),
                     "low": float(k[4]), "pct_change": round(pct, 3), "pre_close": prev or o})
        prev = c
    if not rows:
        raise ValueError("tencent empty")
    return rows


SOURCES = [("eastmoney", _src_eastmoney), ("tencent", _src_tencent)]


def _cache_path(code):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{code}_daily.json")


def fetch_daily(code, market, limit=260):
    """多源 + 重试 + 缓存回退 + 时间戳验证。

    增强（问题 #2）：不再"第一个源失败才用第二个"，而是**两源都尝试**，
    取「数据日期更新、行数更多」者；同时做时间戳交叉校验。
    返回 (rows, source, as_of, stale)。
    """
    errors = []
    candidates = []
    for name, fn in SOURCES:
        for attempt in range(RETRIES):
            try:
                rows = fn(code, market, limit)
                rows.sort(key=lambda r: r["date"])
                as_of = rows[-1]["date"]
                if as_of > _today():
                    raise ValueError(f"future quote {as_of}")
                candidates.append((name, rows, as_of))
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}#{attempt}:{type(e).__name__}")
                time.sleep(0.3 * (attempt + 1))

    if candidates:
        # 取数据日期最新、行数最多者
        candidates.sort(key=lambda c: (c[2], len(c[1])), reverse=True)
        name, rows, as_of = candidates[0]
        # 交叉校验：若另一源也有数据，验证收盘价一致性（防单源异常值）
        if len(candidates) > 1:
            other = candidates[1]
            if other[2] == as_of and len(other[1]) > 0:
                a_c, b_c = rows[-1]["close"], other[1][-1]["close"]
                if b_c and abs(a_c / b_c - 1) > 0.05:
                    errors.append(f"src_mismatch:{name}={a_c} vs {other[0]}={b_c}")
        with open(_cache_path(code), "w", encoding="utf-8") as f:
            json.dump({"source": name, "fetched_at": _today(), "rows": rows,
                       "cross_checked": len(candidates) > 1}, f, ensure_ascii=False)
        return rows, name, as_of, False

    # 全部在线源失败 -> 缓存回退
    try:
        with open(_cache_path(code), encoding="utf-8") as f:
            obj = json.load(f)
        rows = obj.get("rows") or []
        if rows:
            return rows, "cache", rows[-1]["date"], True
    except (OSError, ValueError):
        pass
    return [], None, None, True


# ============ 指标 ============
def _sma(vals, n):
    out = [None] * len(vals)
    for i in range(len(vals)):
        if i + 1 >= n:
            out[i] = sum(vals[i + 1 - n:i + 1]) / n
    return out


def _ema(vals, n):
    k = 2.0 / (n + 1)
    out, e = [], vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def compute_signals(closes, strategy="ma"):
    """返回最新一根的 signal：1=买 -1=卖 0=持有。"""
    if len(closes) < 65:
        return 0, "样本不足"
    if strategy == "ma":
        ma20, ma60 = _sma(closes, 20), _sma(closes, 60)
        d = ma20[-1] - ma60[-1]
        p = ma20[-2] - ma60[-2]
        if p <= 0 < d:
            return 1, "MA20 上穿 MA60（金叉）"
        if p >= 0 > d:
            return -1, "MA20 下穿 MA60（死叉）"
        return 0, "MA 无交叉"
    if strategy == "macd":
        dif = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
        dea = _ema(dif, 9)
        d, p = dif[-1] - dea[-1], dif[-2] - dea[-2]
        if p <= 0 < d:
            return 1, "MACD DIF 上穿 DEA（金叉）"
        if p >= 0 > d:
            return -1, "MACD DIF 下穿 DEA（死叉）"
        return 0, "MACD 无交叉"
    return 0, "未知策略"


# ============ 账本 ============
def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS acct(account TEXT PRIMARY KEY, initial REAL, cash REAL, label TEXT);
        CREATE TABLE IF NOT EXISTS positions(
            account TEXT, code TEXT, name TEXT, shares INTEGER, avg_cost REAL, buy_date TEXT,
            PRIMARY KEY(account, code));
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, trade_key TEXT UNIQUE,
            date TEXT, code TEXT, name TEXT, action TEXT, price REAL, shares INTEGER,
            amount REAL, commission REAL, stamp_tax REAL, transfer_fee REAL, cost_total REAL,
            realized_pnl REAL, reason TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS equity_curve(
            account TEXT, date TEXT, cash REAL, market_value REAL, total REAL,
            pct_change REAL, market_date TEXT, PRIMARY KEY(account, date));
        CREATE TABLE IF NOT EXISTS run_registry(
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT, task_name TEXT, status TEXT,
            account TEXT, started_at TEXT, finished_at TEXT, market_date TEXT,
            source TEXT, note TEXT, error TEXT);
    """)
    # 向前兼容：旧库补 error 列（问题 #6）
    try:
        conn.execute("ALTER TABLE run_registry ADD COLUMN error TEXT")
    except sqlite3.OperationalError:
        pass
    for a, m in ACCOUNTS.items():
        conn.execute("INSERT OR IGNORE INTO acct(account,initial,cash,label) VALUES(?,?,?,?)",
                     (a, m["initial"], m["initial"], m["label"]))
    conn.commit()


def fees(price, shares, action):
    amount = price * shares
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    transfer = amount * TRANSFER_RATE
    stamp = amount * STAMP_RATE if action == "SELL" else 0.0
    return amount, commission, stamp, transfer, commission + stamp + transfer


def limit_pct(code):
    if code.startswith("688"):
        return LIMIT_STAR
    if code.startswith(("300", "301")):
        return LIMIT_STAR
    return LIMIT_MAIN


def execute(conn, account, code, name, action, price, shares, date, reason, dry=False):
    """A 股规则撮合 + 幂等写入。返回 (status, detail)。"""
    trade_key = f"{account}:{date}:{code}:{action}:{shares}"
    if conn.execute("SELECT 1 FROM trades WHERE trade_key=?", (trade_key,)).fetchone():
        return "duplicate", "已存在，跳过（幂等保护）"

    position = conn.execute("SELECT * FROM positions WHERE account=? AND code=?",
                            (account, code)).fetchone()
    cash = float(conn.execute("SELECT cash FROM acct WHERE account=?", (account,)).fetchone()["cash"])
    total_assets = cash + sum(
        float(p["shares"]) * price for p in conn.execute("SELECT * FROM positions WHERE account=?", (account,))
    )

    if action == "BUY":
        if position:
            return "skip", "已持有，不加仓（单次信号单次买入）"
        budget = min(cash, total_assets * MAX_POS_RATIO)
        shares = int(budget / price // LOT) * LOT
        if shares < LOT:
            return "skip", f"资金不足（可用 {budget:.0f} 元）"
        amount, commission, stamp, transfer, cost = fees(price, shares, "BUY")
        if amount + cost > cash:
            return "skip", "现金不足覆盖成本"
        if dry:
            return "dry", f"BUY {shares}股 花费 {amount + cost:.2f}"
        conn.execute("UPDATE acct SET cash=cash-? WHERE account=?", (amount + cost, account))
        conn.execute("INSERT OR REPLACE INTO positions(account,code,name,shares,avg_cost,buy_date) "
                     "VALUES(?,?,?,?,?,?)", (account, code, name, shares, (amount + cost) / shares, date))
        pnl = None
    else:  # SELL
        if not position or position["shares"] <= 0:
            return "skip", "无持仓，无法卖出"
        held = int(position["shares"])
        # T+1：买入当日不可卖
        if position["buy_date"] == date:
            return "skip", "T+1 限制（当日买入不可卖）"
        shares = held
        amount, commission, stamp, transfer, cost = fees(price, shares, "SELL")
        proceeds = amount - cost
        pnl = proceeds - position["avg_cost"] * shares
        if dry:
            return "dry", f"SELL {shares}股 净得 {proceeds:.2f}"
        conn.execute("UPDATE acct SET cash=cash+? WHERE account=?", (proceeds, account))
        conn.execute("DELETE FROM positions WHERE account=? AND code=?", (account, code))

    conn.execute("""INSERT INTO trades(account,trade_key,date,code,name,action,price,shares,
                    amount,commission,stamp_tax,transfer_fee,cost_total,realized_pnl,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (account, trade_key, date, code, name, action, price, shares,
                  amount, commission, stamp, transfer, cost, pnl, reason,
                  datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return "done", f"{action} {shares}股 @ {price:.2f} 成本 {cost:.2f}"


# ============ 主流程 ============
def run(strategy="ma", accounts=None, dry=False, offline_rows=None, run_id=None, push_enabled=True):
    accounts = accounts or list(ACCOUNTS.keys())
    conn = db()
    ensure_schema(conn)

    # run_key 带执行标识：便于"每次执行都有回执"（问题 #1），
    # 而防重复成交由 trades.trade_key 保证（问题 #4），二者职责分离。
    slot = run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_key = f"{_today()}:{strategy}:{slot}:" + ",".join(accounts)
    # 可选：同日同策略的在途保护（防止严格并发重入），但不再阻止"失败后重试"
    inflight = conn.execute(
        "SELECT run_key FROM run_registry WHERE status='running' AND run_key LIKE ?",
        (f"{_today()}:{strategy}:%" + ",".join(accounts),)).fetchone()
    if inflight:
        _log(f"检测到同日同策略在途执行 {inflight['run_key']}，跳过（并发保护）")
        conn.close()
        return {"status": "duplicate", "run_key": run_key}

    conn.execute("INSERT INTO run_registry(run_key,task_name,status,account,started_at) VALUES(?,?,?,?,?)",
                 (run_key, f"daily_job:{strategy}", "running", ",".join(accounts),
                  datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()

    quotes, signals, source_used, stale_any, market_date = [], [], set(), False, None
    for code, name, market in WATCHLIST:
        if offline_rows is not None:
            rows, src, as_of, stale = offline_rows.get(code, ([], "offline", None, True))
        else:
            rows, src, as_of, stale = fetch_daily(code, market)
        source_used.add(src)
        stale_any = stale_any or stale
        if not rows:
            _log(f"{code} {name}: 无数据（源={src}）")
            quotes.append({"code": code, "name": name, "close": None, "action": "NODATA"})
            continue
        closes = [r["close"] for r in rows]
        sig, reason = compute_signals(closes, strategy)
        market_date = max(market_date or as_of, as_of)
        quotes.append({"code": code, "name": name, "close": closes[-1],
                       "ma20": round(_sma(closes, 20)[-1], 2) if len(closes) >= 20 else None,
                       "ma60": round(_sma(closes, 60)[-1], 2) if len(closes) >= 60 else None,
                       "action": "BUY" if sig == 1 else "SELL" if sig == -1 else "HOLD"})
        if sig != 0:
            signals.append({"code": code, "name": name, "action": "BUY" if sig == 1 else "SELL",
                            "reason": reason, "price": closes[-1], "date": as_of})
        _log(f"{code} {name}: close={closes[-1]:.2f} signal={sig} src={src}{' [STALE]' if stale else ''}")

    # 逐账户执行
    exec_log = {}
    for acct in accounts:
        acts = []
        for s in signals:
            st, detail = execute(conn, acct, s["code"], s["name"], s["action"],
                                  s["price"], 0, market_date, s["reason"], dry=dry)
            acts.append({"code": s["code"], "action": s["action"], "status": st, "detail": detail})
            _log(f"  [{acct}] {s['code']} {s['action']}: {st} - {detail}")
        exec_log[acct] = acts

    # 净值快照
    price_map = {q["code"]: q["close"] for q in quotes if q.get("close")}
    summary = {}
    for acct in accounts:
        cash = float(conn.execute("SELECT cash FROM acct WHERE account=?", (acct,)).fetchone()["cash"])
        mv = sum(float(p["shares"]) * price_map.get(p["code"], p["avg_cost"])
                 for p in conn.execute("SELECT * FROM positions WHERE account=?", (acct,)))
        total = cash + mv
        initial = ACCOUNTS[acct]["initial"]
        conn.execute("""INSERT OR REPLACE INTO equity_curve(account,date,cash,market_value,total,pct_change,market_date)
                        VALUES(?,?,?,?,?,?,?)""",
                     (acct, market_date, cash, mv, total, (total / initial - 1) * 100, market_date))
        conn.commit()
        n_pos = conn.execute("SELECT COUNT(*) c FROM positions WHERE account=?", (acct,)).fetchone()["c"]
        n_trade = conn.execute("SELECT COUNT(*) c FROM trades WHERE account=?", (acct,)).fetchone()["c"]
        summary[acct] = {"cash": round(cash, 2), "market_value": round(mv, 2),
                         "total": round(total, 2), "return_pct": round((total / initial - 1) * 100, 2),
                         "positions": n_pos, "trades": n_trade}

    src_label = ",".join(sorted(str(s) for s in source_used if s))
    ok_quotes = len([q for q in quotes if q.get("close")])
    status = "done" if ok_quotes >= 1 else "failed"
    note = f"quotes={len(quotes)} signals={len(signals)} stale={stale_any} dry={dry}"
    # 问题 #6：失败必须记录明确原因
    err = None
    if status == "failed":
        missing = [q["code"] for q in quotes if not q.get("close")]
        err = f"全部/部分标的无行情数据（源均失败且无缓存）: {','.join(missing) or '全部'} | sources_tried={src_label or 'none'}"
    conn.execute("""UPDATE run_registry SET status=?, finished_at=?, market_date=?, source=?, note=?, error=?
                    WHERE run_key=? AND status='running'""",
                 (status, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  market_date, src_label, note, err, run_key))
    conn.commit()

    receipt = {"run_key": run_key, "strategy": strategy, "status": status,
               "market_date": market_date, "source": src_label, "stale": stale_any,
               "quotes": quotes, "signals": signals, "exec": exec_log,
               "summary": summary, "dry": dry, "errors": ([err] if err else []),
               "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    conn.close()

    if not dry and push_enabled:
        push(receipt)
    return receipt


def push(receipt):
    body = json.dumps({"date": receipt.get("market_date"), "quotes": receipt.get("quotes"),
                       "signals": receipt.get("signals"), "receipt": receipt}, ensure_ascii=False).encode()
    try:
        req = urllib.request.Request(PUSH_URL, data=body,
                                     headers={"Content-Type": "application/json",
                                              "X-Auth-Token": INGEST_TOKEN, **UA})
        resp = urllib.request.urlopen(req, timeout=20).read().decode()
        print("PUSH_OK:" + resp[:200], flush=True)
    except Exception as e:  # noqa: BLE001
        print("PUSH_FAIL:" + repr(e), flush=True)


# ============ 离线自检（不联网，验证闭环逻辑） ============
def _make_series(n=200, start=10.0, step=0.05):
    """构造"长期横盘缓跌 -> 末尾 2 根急速拉升"的行情，使最后一根恰好 MA20 上穿 MA60。"""
    rows, px = [], []
    prices = [start * (0.999 ** i) for i in range(n - 2)]  # 缓跌，MA20<MA60
    last = prices[-1]
    prices += [last * 1.25, last * 1.6]                     # 末尾两根急涨，触发金叉
    for i, px in enumerate(prices):
        d = (datetime.date(2025, 1, 1) + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        rows.append({"date": d, "open": px, "close": px, "high": px * 1.01,
                     "low": px * 0.99, "pct_change": 0.0, "pre_close": px})
    return rows


def selftest():
    """离线闭环自检：用合成行情验证 成交/幂等/持仓/净值 全链路。
    完全隔离：临时数据库 + 禁止推送，绝不影响生产账本。"""
    import tempfile
    global DB_PATH, DATA_DIR
    tmp = tempfile.mkdtemp(prefix="selftest_")
    old_db, old_data = DB_PATH, DATA_DIR
    DB_PATH, DATA_DIR = os.path.join(tmp, "t.db"), tmp
    offline = {}
    for i, (code, name, mk) in enumerate(WATCHLIST):
        offline[code] = (_make_series(200, start=10.0 + i), "offline", "2025-07-18", False)
    print("=== SELFTEST: 第一次跑 ===")
    r1 = run(strategy="ma", accounts=["real22000", "paper100k"], dry=False,
             offline_rows=offline, push_enabled=False)
    print("=== SELFTEST: 重复跑（应再次执行，但不产生重复成交）===")
    r2 = run(strategy="ma", accounts=["real22000", "paper100k"], dry=False,
             offline_rows=offline, run_id="dup-check", push_enabled=False)
    c = db()
    nt = c.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"]
    per = c.execute("SELECT account, COUNT(*) n FROM trades GROUP BY account").fetchall()
    c.close()
    DB_PATH, DATA_DIR = old_db, old_data   # 还原，确保不影响生产
    print("RESULT_SELFTEST", json.dumps({
        "first_status": r1["status"], "second_status": r2["status"],
        "total_trades": nt, "per_account": {p["account"]: p["n"] for p in per},
        "summary": r1["summary"],
    }, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="ma", choices=["ma", "macd"])
    ap.add_argument("--account", default=None, help="real22000 / paper100k，缺省=双账户")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    accounts = [a.account] if a.account else list(ACCOUNTS.keys())
    receipt = run(strategy=a.strategy, accounts=accounts, dry=a.dry_run)
    print("RESULT " + json.dumps({k: receipt[k] for k in
          ("run_key", "status", "market_date", "source", "stale")}, ensure_ascii=False))
    print("SUMMARY " + json.dumps(receipt["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()