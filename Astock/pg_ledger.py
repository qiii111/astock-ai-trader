#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Neon PostgreSQL 账本层（云端执行版）。

与本地 SQLite 版 ledger.py 保持**完全相同的幂等语义**：
  · 执行级去重：run_registry.run_key
  · 成交级去重：trades.trade_key  UNIQUE + ON CONFLICT DO NOTHING
  · 事务包裹：资金变更 + 持仓变更 + 成交写入 在同一事务内，要么全成要么全滚

连接串只从环境变量读取：
    DATABASE_URL = postgresql://<user>:<pwd>@<host>/<db>?sslmode=require
禁止写入代码 / 日志 / 仓库。日志中一律脱敏。

依赖：psycopg[binary]（见 requirements.txt）
"""
import os
import datetime
import socket
from urllib.parse import urlsplit

try:
    import psycopg  # psycopg 3
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    raise SystemExit(
        "缺少依赖 psycopg。请先执行：pip install 'psycopg[binary]>=3.1'"
    )


ACCOUNTS = {
    "real22000": {"initial": 22000.0, "label": "实盘对标（2.2万）"},
    "paper100k": {"initial": 100000.0, "label": "对照账户（10万）"},
}

# 连接重试参数（云端网络抖动是常态）
CONNECT_RETRIES = 4
CONNECT_BACKOFF = 1.5
STATEMENT_TIMEOUT_MS = 20000


def mask_dsn(dsn: str) -> str:
    """把连接串里的密码替换为 ***，用于安全日志输出。"""
    if not dsn:
        return "<empty>"
    out = dsn
    try:
        # postgresql://user:pwd@host/db  ->  postgresql://user:***@host/db
        if "://" in out and "@" in out:
            head, tail = out.split("://", 1)
            cred, host = tail.split("@", 1)
            if ":" in cred:
                user = cred.split(":", 1)[0]
                out = f"{head}://{user}:***@{host}"
    except Exception:  # noqa: BLE001
        return "<masked>"
    return out


def dsn() -> str:
    v = os.environ.get("DATABASE_URL", "").strip()
    if not v:
        raise SystemExit("未设置环境变量 DATABASE_URL（请在 GitHub Actions Secrets 中配置）")
    return v


def connect():
    """Connect to Neon over IPv4 when available; retain DNS hostname for TLS."""
    import time as _t

    url = dsn()
    hostname = urlsplit(url).hostname
    if not hostname:
        raise SystemExit("DATABASE_URL 缺少数据库主机名")

    last = None
    for attempt in range(CONNECT_RETRIES):
        try:
            # GitHub-hosted runners may lack IPv6 routes, while Neon DNS returns AAAA.
            ipv4 = socket.getaddrinfo(
                hostname, 5432, family=socket.AF_INET, type=socket.SOCK_STREAM
            )[0][4][0]
            return psycopg.connect(
                url, hostaddr=ipv4, row_factory=dict_row, autocommit=False,
                connect_timeout=10,
                options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            )
        except (OSError, psycopg.Error, IndexError) as e:
            last = e
            if attempt + 1 < CONNECT_RETRIES:
                _t.sleep(CONNECT_BACKOFF * (attempt + 1))
    # Never log connection exception text: drivers may include sensitive DSN data.
    raise SystemExit(
        f"数据库 IPv4 连接失败（已重试 {CONNECT_RETRIES} 次）：{type(last).__name__}"
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS acct(
    account TEXT PRIMARY KEY,
    initial DOUBLE PRECISION,
    cash    DOUBLE PRECISION,
    label   TEXT
);

CREATE TABLE IF NOT EXISTS positions(
    account   TEXT,
    code      TEXT,
    name      TEXT,
    shares    INTEGER,
    avg_cost  DOUBLE PRECISION,
    buy_date  TEXT,
    PRIMARY KEY(account, code)
);

CREATE TABLE IF NOT EXISTS trades(
    id           BIGSERIAL PRIMARY KEY,
    account      TEXT,
    trade_key    TEXT UNIQUE,
    date         TEXT,
    code         TEXT,
    name         TEXT,
    action       TEXT,
    price        DOUBLE PRECISION,
    shares       INTEGER,
    amount       DOUBLE PRECISION,
    commission   DOUBLE PRECISION,
    stamp_tax    DOUBLE PRECISION,
    transfer_fee DOUBLE PRECISION,
    cost_total   DOUBLE PRECISION,
    realized_pnl DOUBLE PRECISION,
    reason       TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS equity_curve(
    account      TEXT,
    date         TEXT,
    cash         DOUBLE PRECISION,
    market_value DOUBLE PRECISION,
    total        DOUBLE PRECISION,
    pct_change   DOUBLE PRECISION,
    market_date  TEXT,
    PRIMARY KEY(account, date)
);

CREATE TABLE IF NOT EXISTS run_registry(
    id          BIGSERIAL PRIMARY KEY,
    run_key     TEXT UNIQUE,
    task_name   TEXT,
    status      TEXT,
    account     TEXT,
    started_at  TEXT,
    finished_at TEXT,
    market_date TEXT,
    source      TEXT,
    note        TEXT,
    error       TEXT
);

-- 并发安全执行锁（问题 #2）：
--   lock_key 为主键，靠 INSERT 冲突实现「同一时刻只有一个执行者」（advisory-style）。
--   不用 SELECT LIKE 判断重复 —— 那是竞态条件，两个并发流水线会同时通过检查。
CREATE TABLE IF NOT EXISTS exec_lock(
    lock_key   TEXT PRIMARY KEY,
    owner      TEXT,
    acquired_at TEXT,
    expires_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_acct   ON trades(account, date);
CREATE INDEX IF NOT EXISTS idx_equity_acct   ON equity_curve(account, date);
CREATE INDEX IF NOT EXISTS idx_runs_key      ON run_registry(run_key);
CREATE INDEX IF NOT EXISTS idx_runs_status   ON run_registry(status, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_task_day ON run_registry(task_name, market_date);
"""


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_ledger() -> None:
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        for acct, meta in ACCOUNTS.items():
            cur.execute(
                "INSERT INTO acct(account, initial, cash, label) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(account) DO NOTHING",
                (acct, meta["initial"], meta["initial"], meta["label"]),
            )
    conn.commit()
    _migrate_run_key_unique(conn)
    conn.close()


def _migrate_run_key_unique(conn) -> None:
    """为**已存在**的旧库补上 run_key UNIQUE 约束（问题 #2）。

    CREATE TABLE IF NOT EXISTS 不会给已存在的表加约束，因此这里显式迁移：
      1. 若已有重复 run_key，先把重复行标记出来但**不删除**（保留审计），
         仅保留 id 最小的一条作为「有效」记录——通过迁移到新表实现。
      2. 加 UNIQUE 约束。

    幂等：已存在约束则直接跳过。
    """
    with conn.cursor() as cur:
        # 是否已有 run_key 上的唯一约束/唯一索引？
        cur.execute(
            """SELECT 1 FROM pg_constraint c
               JOIN pg_class t ON t.oid = c.conrelid
               WHERE t.relname='run_registry' AND c.contype='u' LIMIT 1""")
        if cur.fetchone():
            return
        cur.execute(
            """SELECT 1 FROM pg_indexes
               WHERE tablename='run_registry' AND indexdef LIKE '%UNIQUE%'
                 AND indexdef LIKE '%run_key%' LIMIT 1""")
        if cur.fetchone():
            return

        # 归档重复行（保留证据），然后只留 id 最小者
        cur.execute(
            """CREATE TABLE IF NOT EXISTS run_registry_dupes AS
               SELECT * FROM run_registry WHERE 1=0""")
        cur.execute(
            """INSERT INTO run_registry_dupes
               SELECT r.* FROM run_registry r
               WHERE r.run_key IS NOT NULL
                 AND r.id > (SELECT MIN(r2.id) FROM run_registry r2
                             WHERE r2.run_key = r.run_key)""")
        cur.execute(
            """DELETE FROM run_registry r
               WHERE r.run_key IS NOT NULL
                 AND r.id > (SELECT MIN(r2.id) FROM run_registry r2
                             WHERE r2.run_key = r.run_key)""")
        try:
            cur.execute("ALTER TABLE run_registry ALTER COLUMN run_key SET NOT NULL")
        except Exception:  # noqa: BLE001
            conn.rollback()
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_run_registry_run_key "
                    "ON run_registry(run_key)")
    conn.commit()


# ---------------- 并发安全执行锁（问题 #2） ----------------
class LockNotAcquired(Exception):
    """另一执行者已持有同日同任务的锁。"""


def acquire_lock(conn, lock_key: str, owner: str, ttl_seconds: int = 3600) -> bool:
    """尝试获取执行锁。返回 True=拿到，False=他人持有。

    实现：exec_lock.lock_key 是主键，靠 INSERT ... ON CONFLICT DO NOTHING
    的原子性保证**同一时刻只有一个执行者**。

    过期锁回收：若已存在的锁 expires_at < now()，则视为陈旧锁并抢占
    （应对"上一个作业被超时杀掉但没来得及释放"的情况）。
    """
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    exp = (datetime.datetime.now() + datetime.timedelta(seconds=ttl_seconds)
           ).strftime("%Y-%m-%d %H:%M:%S")
    with conn.cursor() as cur:
        # 1) 先尝试直接插入（无锁时成功）
        cur.execute(
            "INSERT INTO exec_lock(lock_key, owner, acquired_at, expires_at) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT(lock_key) DO NOTHING RETURNING lock_key",
            (lock_key, owner, now_s, exp))
        if cur.fetchone() is not None:
            conn.commit()
            return True
        # 2) 已有锁：若已过期则原子抢占（条件更新，避免两个进程同时抢）
        cur.execute(
            "UPDATE exec_lock SET owner=%s, acquired_at=%s, expires_at=%s "
            "WHERE lock_key=%s AND expires_at < %s RETURNING lock_key",
            (owner, now_s, exp, lock_key, now_s))
        got = cur.fetchone() is not None
    conn.commit()
    return got


def release_lock(conn, lock_key: str, owner: str) -> None:
    """释放锁（仅释放自己持有的，避免误删他人的锁）。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM exec_lock WHERE lock_key=%s AND owner=%s",
                    (lock_key, owner))
    conn.commit()


# ---------------- 执行级去重（run_key） ----------------
def run_already_done(conn, run_key: str) -> bool:
    """执行级去重：给定 run_key 已存在 done/running 记录则返回 True。

    注意：run_key 内含「执行时间戳」，因此**同一个 run_key 精确匹配**
    只能拦截完全相同的重放；跨执行的重叠触发需用同日前缀匹配
    （见 same_day_run_exists）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM run_registry WHERE run_key=%s "
            "AND status IN ('done','running') ORDER BY id DESC LIMIT 1",
            (run_key,),
        )
        return cur.fetchone() is not None


def same_day_run_exists(conn, day_prefix: str) -> bool:
    """同日同策略是否已有 done/running 记录。

    ⚠️ 这只是一个**快速预检**，存在竞态（两个进程可能同时读到 False）。
       真正的并发安全由 acquire_lock() + run_key UNIQUE 约束保证。
       本函数仅用于减少无谓的重复工作，不能作为唯一防线。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM run_registry WHERE run_key LIKE %s "
            "AND status IN ('done','running') LIMIT 1",
            (day_prefix + "%",),
        )
        return cur.fetchone() is not None


def start_run(conn, run_key: str, task_name: str, account: str) -> bool:
    """插入 running 行。**依赖 run_key UNIQUE 约束**做最终去重（问题 #2）。

    返回 True=成功占用该 run_key；False=已存在（并发或重放），调用方应跳过。
    不使用 SELECT 预检 —— 那是竞态条件。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO run_registry(run_key,task_name,status,account,started_at) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(run_key) DO NOTHING RETURNING id",
            (run_key, task_name, "running", account, now()),
        )
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


def finish_run(conn, run_key: str, status: str, market_date, source, note, error=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE run_registry SET status=%s, finished_at=%s, market_date=%s, "
            "source=%s, note=%s, error=%s WHERE run_key=%s AND status='running'",
            (status, now(), market_date, source, note, error, run_key),
        )
    conn.commit()


# ---------------- 成交级去重（trade_key） ----------------
def record_trade(conn, row: dict) -> bool:
    """幂等写入成交：trade_key 冲突则 DO NOTHING，返回 False 表示重复。

    **不在此处 commit** —— 由调用方把「资金/持仓/成交」放在同一事务里提交。
    """
    cols = list(row.keys())
    placeholders = ",".join("%s" for _ in cols)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO trades({','.join(cols)}) VALUES({placeholders}) "
            "ON CONFLICT(trade_key) DO NOTHING RETURNING id",
            tuple(row[c] for c in cols),
        )
        return cur.fetchone() is not None


def trade_exists(conn, trade_key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM trades WHERE trade_key=%s LIMIT 1", (trade_key,))
        return cur.fetchone() is not None


# ---------------- 账户 / 持仓 / 净值 ----------------
def get_cash(conn, account: str) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT cash FROM acct WHERE account=%s", (account,))
        r = cur.fetchone()
        return float(r["cash"]) if r else 0.0


def set_cash(conn, account: str, cash: float) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE acct SET cash=%s WHERE account=%s", (cash, account))


def upsert_position(conn, account, code, name, shares, avg_cost, buy_date) -> None:
    with conn.cursor() as cur:
        if shares <= 0:
            cur.execute("DELETE FROM positions WHERE account=%s AND code=%s", (account, code))
        else:
            cur.execute(
                "INSERT INTO positions(account,code,name,shares,avg_cost,buy_date) "
                "VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(account,code) DO UPDATE SET "
                "name=EXCLUDED.name, shares=EXCLUDED.shares, "
                "avg_cost=EXCLUDED.avg_cost, buy_date=EXCLUDED.buy_date",
                (account, code, name, shares, avg_cost, buy_date),
            )


def get_positions(conn, account: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM positions WHERE account=%s ORDER BY code", (account,))
        return cur.fetchall()


def upsert_equity(conn, account, date, cash, market_value, market_date, pct_change=0.0) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO equity_curve(account,date,cash,market_value,total,pct_change,market_date) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(account,date) DO UPDATE SET "
            "cash=EXCLUDED.cash, market_value=EXCLUDED.market_value, "
            "total=EXCLUDED.total, pct_change=EXCLUDED.pct_change, "
            "market_date=EXCLUDED.market_date",
            (account, date, cash, market_value, cash + market_value, pct_change, market_date),
        )


# ---------------- 估值：各自最新有效价格（问题 #4） ----------------
class PriceUnavailable(Exception):
    """持仓标的价格缺失，无法安全估值。"""


def value_positions(conn, account: str, price_map: dict, required_codes=None):
    """按**各持仓自己的最新有效价格**估值，绝不拿当前交易标的价格套用全部持仓。

    参数：
      price_map      {code: price} 本次成功取到的行情（可能不含某些持仓）
      required_codes 可选；若给定，则这些代码必须在 price_map 中有价，
                     否则抛 PriceUnavailable（拒绝在信息不全时做资金决策）。

    返回 (market_value, missing_list, used)：
      market_value  已能安全估值的部分
      missing_list  缺价的持仓代码
      used          {code: price} 实际用于估值的价格

    规则：
      · 有最新价         -> 用最新价
      · 无最新价但有成本 -> **不计入市值并报缺失**（不用 avg_cost 冒充市值，
                            那会系统性地高估/低估总资产，进而影响 40% 仓位上限）
    """
    pos = get_positions(conn, account)
    mv = 0.0
    missing, used = [], {}
    for p in pos:
        code = p["code"]
        shares = float(p["shares"])
        px = price_map.get(code)
        if px is None or px <= 0:
            missing.append(code)
            continue
        used[code] = px
        mv += shares * px

    if required_codes:
        lack = [c for c in required_codes if c not in used]
        if lack:
            raise PriceUnavailable(
                f"账户 {account} 缺少必需价格: {','.join(sorted(set(lack)))}")
    return mv, missing, used


# ---------------- 漏执行检测（问题 #6） ----------------
def last_done_run(conn, task_name: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_key, finished_at, market_date, status FROM run_registry "
            "WHERE task_name=%s AND status='done' ORDER BY id DESC LIMIT 1",
            (task_name,),
        )
        return cur.fetchone()


def runs_between(conn, start_ts: str, end_ts: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_key, task_name, status, started_at, finished_at, market_date, error "
            "FROM run_registry WHERE started_at >= %s AND started_at < %s ORDER BY id",
            (start_ts, end_ts),
        )
        return cur.fetchall()
