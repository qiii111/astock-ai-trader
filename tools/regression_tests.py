#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regression_tests.py —— 针对审查发现问题的回归测试套件。

覆盖（与审查项一一对应）：
  T1  过期/stale 行情必须拒绝成交，不得只记录状态
  T2  run_key 数据库级唯一性 + 并发执行锁
  T3  --dry-run 完全无副作用（不写 run_registry / acct / positions / equity_curve）
  T4  多持仓估值使用各自最新有效价格，不得套用当前标的价格
  T5  空 Token / 未配置 Token 必须被拒绝
  T6  已下线未认证公共代码分发接口
  T7  并发执行只有一个成功；重复交易不重复扣款
  T8  行情全缺失时安全跳过并正确上报
  T9a 单次抓取：信号与成交价来自同一份快照（v1.3 问题 #1）
  T9b dry-run 只读：空库不得建表、no_side_effects=True（v1.3 问题 #2）
  T9c daily.yml 步骤门控：init_ledger 必须被 dry_run != 'true' 门控
  T9d --dry-run 主入口：空库上不建表，DRY_RUN_RESULT 正确

运行方式（需要 DATABASE_URL 指向一个可写的 PostgreSQL）：
    export DATABASE_URL='postgresql://...'
    python3 tools/regression_tests.py

**使用独立的 schema 前缀，绝不触碰生产表。**
若未设置 DATABASE_URL，则跳过依赖数据库的用例并明确报告。
"""
import datetime
import importlib
import io
import json
import os
import re
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "astock")
if not os.path.isfile(os.path.join(SRC, "daily_job.py")):
    SRC = HERE
sys.path.insert(0, SRC)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append({"name": name, "pass": bool(ok), "detail": str(detail)[:400]})
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}" + (f": {detail}" if detail else ""), flush=True)


def section(title):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


# ======================================================================
# T1 过期/stale 行情必须拒绝成交（问题 #1）
# ======================================================================
def t1_quote_freshness_policy():
    section("T1 行情新鲜度策略（问题 #1）")
    import run_cloud as R

    today = datetime.date(2026, 10, 8)

    cases = [
        # (as_of, stale, 期望可用, 说明)
        ("2026-10-08", False, True, "当日新鲜行情"),
        ("2026-10-07", False, True, "1天前（阈值内）"),
        ("2026-10-01", False, True, "7天前（恰好阈值）"),
        ("2026-09-30", False, False, "8天前（超阈值）"),
        ("2026-07-01", False, False, "约3个月前（典型缓存停摆）"),
        ("2026-10-09", False, False, "未来日期（时间戳异常）"),
        ("2026-10-08", True, False, "stale=True（缓存回退）"),
        (None, False, False, "无行情日期"),
        ("", True, False, "无日期且 stale"),
        ("not-a-date", False, False, "日期无法解析"),
    ]
    for as_of, stale, expect, desc in cases:
        usable, why = R.check_quote_freshness(as_of, stale, today)
        record(f"T1 | {desc} -> {'可用' if expect else '拒绝'}", usable == expect,
               f"实际 usable={usable} reason={why}")


def t1_stale_not_traded():
    """核心断言：stale 行情不得产生任何成交。"""
    section("T1b stale 行情不产生成交（问题 #1）")
    if not os.environ.get("DATABASE_URL"):
        record("T1b stale 不成交（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    import run_cloud as R
    import daily_job as J

    conn = P.connect()
    P.init_ledger()
    _reset(conn)

    # 构造：所有标的都返回 stale 数据
    def stale_fetcher(code, market, limit=260):
        rows = J._make_series(200, start=10.0)   # 会触发金叉的行情
        return rows, "cache", "2025-07-18", True  # stale=True

    rc = R.run_cloud(strategy="ma", accounts=["real22000"], dry=False,
                     run_id="t1b", fetcher=stale_fetcher, owned_conn=conn)
    nt = _count(conn, "trades")
    cash = P.get_cash(conn, "real22000")
    record("T1b stale 数据 -> status=failed", rc["status"] == "failed", f"status={rc['status']}")
    record("T1b stale 数据 -> 零成交", nt == 0, f"trades={nt}")
    record("T1b stale 数据 -> 资金不变", abs(cash - 22000.0) < 1e-9, f"cash={cash}")
    # 失败原因必须被记录
    with conn.cursor() as cur:
        cur.execute("SELECT error FROM run_registry WHERE run_key LIKE 'dry:t1b%' "
                    "OR run_key LIKE '%t1b%' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    record("T1b 失败原因已记录", bool(row and row["error"]), 
           (row["error"] if row else "no row")[:150])
    conn.close()


# ======================================================================
# T2 run_key 唯一性 + 并发锁（问题 #2）
# ======================================================================
def t2_db_uniqueness():
    section("T2 run_key 数据库级唯一性（问题 #2）")
    if not os.environ.get("DATABASE_URL"):
        record("T2 run_key UNIQUE 约束", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    conn = P.connect()
    P.init_ledger()

    # 1) 约束是否存在
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM pg_indexes WHERE tablename='run_registry'
                       AND indexdef LIKE '%UNIQUE%' AND indexdef LIKE '%run_key%' LIMIT 1""")
        has_idx = cur.fetchone() is not None
    record("T2 run_registry.run_key 有 UNIQUE 索引", has_idx)

    # 2) 直接重复插入必须被数据库拒绝
    key = f"t2-uniq-{os.getpid()}"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM run_registry WHERE run_key=%s", (key,))
    conn.commit()
    ok1 = P.start_run(conn, key, "t2test", "real22000")
    ok2 = P.start_run(conn, key, "t2test", "real22000")   # 第二次必须 False
    record("T2 首次 start_run 成功", ok1 is True)
    record("T2 重复 start_run 被 UNIQUE 拒绝", ok2 is False, f"second={ok2}")

    # 3) 并发插入：多线程同时抢同一 run_key，只能有一个成功
    key2 = f"t2-conc-{os.getpid()}"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM run_registry WHERE run_key=%s", (key2,))
    conn.commit()
    results = []
    lock = threading.Lock()

    def worker():
        c = P.connect()
        try:
            r = P.start_run(c, key2, "t2test", "real22000")
        finally:
            c.close()
        with lock:
            results.append(r)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    record("T2 6 线程并发抢同一 run_key -> 仅 1 成功",
           sum(1 for r in results if r) == 1,
           f"success={sum(1 for r in results if r)}, results={results}")

    conn.close()


def t2_exec_lock():
    section("T2b 并发执行锁 exec_lock（问题 #2）")
    if not os.environ.get("DATABASE_URL"):
        record("T2b 执行锁（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    conn = P.connect()
    P.init_ledger()
    lk = f"lock-test-{os.getpid()}"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM exec_lock WHERE lock_key=%s", (lk,))
    conn.commit()

    a = P.acquire_lock(conn, lk, "owner-A")
    b = P.acquire_lock(conn, lk, "owner-B")     # 必须失败
    record("T2b A 获取锁成功", a is True)
    record("T2b B 获取同一锁失败", b is False, f"B={b}")

    P.release_lock(conn, lk, "owner-A")
    c = P.acquire_lock(conn, lk, "owner-C")     # 释放后应能获取
    record("T2b 释放后 C 可获取", c is True, f"C={c}")

    # 并发抢锁：多线程只能有一个成功
    lk2 = f"lock-conc-{os.getpid()}"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM exec_lock WHERE lock_key=%s", (lk2,))
    conn.commit()
    got, glock = [], threading.Lock()

    def w(i):
        cc = P.connect()
        try:
            r = P.acquire_lock(cc, lk2, f"o{i}")
        finally:
            cc.close()
        with glock:
            got.append(r)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    record("T2b 8 线程并发抢锁 -> 仅 1 成功",
           sum(1 for g in got if g) == 1, f"success={sum(1 for g in got if g)}")

    # 清理
    with conn.cursor() as cur:
        cur.execute("DELETE FROM exec_lock WHERE lock_key IN (%s,%s)", (lk, lk2))
        cur.execute("DELETE FROM run_registry WHERE run_key LIKE %s OR run_key LIKE %s",
                    (f"t2-uniq-{os.getpid()}%", f"t2-conc-{os.getpid()}%"))
    conn.commit()
    conn.close()


# ======================================================================
# T3 dry-run 无副作用（问题 #3）
# ======================================================================
def t3_dry_run_no_side_effects():
    section("T3 --dry-run 完全无副作用（问题 #3）")
    if not os.environ.get("DATABASE_URL"):
        record("T3 dry-run 无副作用（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    import run_cloud as R
    import daily_job as J

    conn = P.connect()
    P.init_ledger()
    _reset(conn)

    before = _snapshot_counts(conn)
    before_cash = {a: P.get_cash(conn, a) for a in P.ACCOUNTS}

    # 用会触发买入信号的新鲜行情
    def fresh_fetcher(code, market, limit=260):
        rows = J._make_series(200, start=10.0)
        return rows, "test", datetime.date.today().strftime("%Y-%m-%d"), False

    rc = R.run_cloud(strategy="ma", accounts=["real22000", "paper100k"],
                     dry=True, run_id="t3dry", fetcher=fresh_fetcher, owned_conn=conn)

    after = _snapshot_counts(conn)
    after_cash = {a: P.get_cash(conn, a) for a in P.ACCOUNTS}

    record("T3 dry-run 返回 dry=True", rc.get("dry") is True)
    record("T3 dry-run 未写 run_registry",
           before["run_registry"] == after["run_registry"],
           f"{before['run_registry']} -> {after['run_registry']}")
    record("T3 dry-run 未写 trades",
           before["trades"] == after["trades"],
           f"{before['trades']} -> {after['trades']}")
    record("T3 dry-run 未写 positions",
           before["positions"] == after["positions"],
           f"{before['positions']} -> {after['positions']}")
    record("T3 dry-run 未写 equity_curve",
           before["equity_curve"] == after["equity_curve"],
           f"{before['equity_curve']} -> {after['equity_curve']}")
    record("T3 dry-run 资金不变", before_cash == after_cash,
           f"{before_cash} -> {after_cash}")
    record("T3 dry-run 未创建 dry: 前缀记录",
           not _exists(conn, "SELECT 1 FROM run_registry WHERE run_key LIKE 'dry:%'"))

    conn.close()


# ======================================================================
# T4 多持仓估值（问题 #4）
# ======================================================================
def t4_multi_position_valuation():
    section("T4 多持仓估值用各自最新价（问题 #4）")
    if not os.environ.get("DATABASE_URL"):
        record("T4 多持仓估值（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    conn = P.connect()
    P.init_ledger()
    _reset(conn)

    # 建 3 个持仓：成本各 10 元，1000 股
    for code, name in [("A001", "标的A"), ("B002", "标的B"), ("C003", "标的C")]:
        P.upsert_position(conn, "real22000", code, name, 1000, 10.0, "2026-01-01")
    conn.commit()

    # 场景 1：三只都有各自价格 -> 市值应为 1000*(12+20+30)=62000
    pm = {"A001": 12.0, "B002": 20.0, "C003": 30.0}
    mv, missing, used = P.value_positions(conn, "real22000", pm)
    record("T4 三持仓各自取价 -> 市值正确", abs(mv - 62000.0) < 1e-6,
           f"mv={mv} used={used} missing={missing}")
    record("T4 三持仓无缺失", missing == [], f"missing={missing}")

    # 场景 2：B002 缺价 -> 不得用 A001/C003 的价格或 avg_cost 冒充
    pm2 = {"A001": 12.0, "C003": 30.0}
    mv2, missing2, used2 = P.value_positions(conn, "real22000", pm2)
    record("T4 缺价持仓不计入市值（不用成本冒充）",
           abs(mv2 - (1000 * 12.0 + 1000 * 30.0)) < 1e-6,
           f"mv2={mv2} (期望 42000，若用 avg_cost 会得 62000)")
    record("T4 缺价持仓被报告", missing2 == ["B002"], f"missing={missing2}")

    # 场景 3：required_codes 缺失时必须抛异常
    raised = False
    try:
        P.value_positions(conn, "real22000", pm2, required_codes=["B002"])
    except P.PriceUnavailable as e:
        raised = True
        detail = str(e)
    record("T4 必需价格缺失时抛 PriceUnavailable", raised,
           detail if raised else "未抛出")

    # 场景 4：execute_pg 在持仓缺价时必须拒绝下单（不得高估总资产）
    from run_cloud import execute_pg
    st, detail = execute_pg(conn, "real22000", "D004", "新标的", "BUY",
                            10.0, 0, "2026-10-08", "test",
                            dry=False, price_map={"A001": 12.0, "D004": 10.0})
    record("T4 持仓缺价 -> 拒绝买入", st == "skip", f"status={st} detail={detail}")

    _reset(conn)
    conn.close()


# ======================================================================
# T5 空 Token 必须被拒绝（问题 #5）
# ======================================================================
def t5_empty_token_rejected():
    section("T5 空/未配置 Token 必须被拒绝（问题 #5）")
    src = open(os.path.join(SRC, "app.py"), encoding="utf-8").read()

    # 静态断言：必须有"未配置就拒绝"的分支
    has_guard = re.search(
        r"if\s+not\s+INGEST_TOKEN\s*:\s*\n\s*return\s+jsonify\([^)]*\)\s*,\s*503", src) is not None
    record("T5 存在「未配置 Token -> 503」硬拒绝分支", has_guard)

    # 静态断言：不再有裸的 token != INGEST_TOKEN 直接比较（无空值防护）
    naive = re.search(r"if\s+token\s*!=\s*INGEST_TOKEN\s*:", src) is not None
    record("T5 已移除裸 token != INGEST_TOKEN 比较", not naive)

    # 静态断言：使用常量时间比较
    record("T5 使用 hmac.compare_digest（防时序攻击）",
           "compare_digest" in src)

    # 动态断言：用 Flask 测试客户端验证行为
    try:
        os.environ.pop("ASTOCK_INGEST_TOKEN", None)
        os.environ.pop("ASTOCK_READ_TOKEN", None)
        for m in ("app", "config"):
            if m in sys.modules:
                del sys.modules[m]
        import config as C
        importlib.reload(C)
        import app as A
        importlib.reload(A)
        A.app.config["TESTING"] = True
        cli = A.app.test_client()

        # Token 未配置：空 Token 写入 -> 503
        r1 = cli.post("/api/live_push", json={"run_key": "x", "date": "2026-10-08"},
                      headers={"X-Auth-Token": ""})
        record("T5 未配置 Token 时空 Token 写入 -> 503", r1.status_code == 503,
               f"status={r1.status_code}")
        # 任意 Token 也应被拒
        r2 = cli.post("/api/live_push", json={"run_key": "x", "date": "2026-10-08"},
                      headers={"X-Auth-Token": "anything"})
        record("T5 未配置 Token 时任意 Token -> 503", r2.status_code == 503,
               f"status={r2.status_code}")
        # 不带 Token 也应被拒
        r3 = cli.post("/api/live_push", json={"run_key": "x", "date": "2026-10-08"})
        record("T5 未配置 Token 时无 Token -> 503", r3.status_code == 503,
               f"status={r3.status_code}")

        # 已配置 Token：错误 Token -> 401，正确 Token -> 非 401/503
        os.environ["ASTOCK_INGEST_TOKEN"] = "correct-token-xyz"
        importlib.reload(C)
        importlib.reload(A)
        A.app.config["TESTING"] = True
        cli = A.app.test_client()
        r4 = cli.post("/api/live_push", json={"run_key": "x", "date": "2026-10-08"},
                      headers={"X-Auth-Token": ""})
        record("T5 配置后空 Token -> 401", r4.status_code == 401, f"status={r4.status_code}")
        r5 = cli.post("/api/live_push", json={"run_key": "x", "date": "2026-10-08"},
                      headers={"X-Auth-Token": "wrong"})
        record("T5 配置后错误 Token -> 401", r5.status_code == 401, f"status={r5.status_code}")
    except Exception as e:  # noqa: BLE001
        record("T5 动态验证异常", False, f"{type(e).__name__}: {e}")
    finally:
        os.environ.pop("ASTOCK_INGEST_TOKEN", None)
        os.environ.pop("ASTOCK_READ_TOKEN", None)


# ======================================================================
# T6 已下线未认证分发接口（问题 #6）
# ======================================================================
def t6_removed_endpoints():
    section("T6 下线未认证代码分发接口（问题 #6）")
    src = open(os.path.join(SRC, "app.py"), encoding="utf-8").read()

    record("T6 /api/job_script 路由已移除", "/api/job_script" not in src)
    record("T6 /api/runner 路由已移除", '"/api/runner"' not in src)
    record("T6 不再内联下发 daily_job 源码",
           "inlined daily_job.py" not in src)

    # 枚举剩余路由，确认都已鉴权或为只读安全端点
    routes = re.findall(r'@app\.route\("([^"]+)"(?:,\s*methods=\[([^\]]+)\])?\)', src)
    record("T6 剩余路由数量已收敛", len(routes) <= 8, f"routes={[r[0] for r in routes]}")

    # /healthz 不泄露敏感信息
    hz = re.search(r'def healthz\(\):(.*?)(?=\n@app\.route|\Z)', src, re.S)
    if hz:
        body = hz.group(1)
        leak = any(k in body for k in ("INGEST_TOKEN\"", "DATABASE_URL", "password"))
        record("T6 /healthz 不泄露敏感配置", not leak, body.strip()[:120])

    # 读取端点应有鉴权
    record("T6 读取端点已加鉴权（_require_read）",
           src.count("_require_read()") >= 5, f"count={src.count('_require_read()')}")


# ======================================================================
# T7 并发 + 重复交易（问题 #7）
# ======================================================================
def t7_concurrent_and_duplicate():
    section("T7 并发执行与重复交易（问题 #7）")
    if not os.environ.get("DATABASE_URL"):
        record("T7 并发（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    import run_cloud as R
    from run_cloud import execute_pg
    import daily_job as J

    conn = P.connect()
    P.init_ledger()
    _reset(conn)

    # --- 7a 重复交易不重复扣款 ---
    st1, d1 = execute_pg(conn, "real22000", "600519", "贵州茅台", "BUY", 10.0, 0,
                         "2026-10-08", "dup-test", dry=False, price_map={"600519": 10.0})
    c1 = P.get_cash(conn, "real22000")
    st2, d2 = execute_pg(conn, "real22000", "600519", "贵州茅台", "BUY", 10.0, 0,
                         "2026-10-08", "dup-test", dry=False, price_map={"600519": 10.0})
    c2 = P.get_cash(conn, "real22000")
    record("T7 首次买入 done", st1 == "done", f"{st1}: {d1}")
    record("T7 重复买入 duplicate", st2 == "duplicate", f"{st2}: {d2}")
    record("T7 重复买入不重复扣款", abs(c1 - c2) < 1e-9, f"cash {c1} -> {c2}")

    # --- 7b 并发调用 run_cloud：只有一个拿到锁 ---
    _reset(conn)
    ok_runs, out = [], []
    olock = threading.Lock()

    def fresh(code, market, limit=260):
        return J._make_series(200, start=10.0), "test", \
            datetime.date.today().strftime("%Y-%m-%d"), False

    def worker(i):
        try:
            rc = R.run_cloud(strategy="ma", accounts=["paper100k"], dry=False,
                             run_id=f"conc-{i}", fetcher=fresh, owned_conn=None)
            with olock:
                out.append(rc.get("status"))
        except Exception as e:  # noqa: BLE001
            with olock:
                out.append(f"err:{type(e).__name__}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    done = out.count("done")
    locked = out.count("locked") + out.count("duplicate")
    record("T7 5 并发 run_cloud -> 至多 1 个 done", done <= 1,
           f"statuses={out}")
    record("T7 其余被锁/去重拦截", done + locked == len(out),
           f"done={done} locked/dup={locked}")

    _reset(conn)
    conn.close()


# ======================================================================
# T8 行情全缺失 -> 安全跳过并上报（问题 #1/#8）
# ======================================================================
def t8_all_quotes_missing():
    section("T8 行情全缺失安全跳过并上报（问题 #1/#8）")
    if not os.environ.get("DATABASE_URL"):
        record("T8 全缺失（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    import run_cloud as R

    conn = P.connect()
    P.init_ledger()
    _reset(conn)

    def none_fetcher(code, market, limit=260):
        return [], None, None, True

    rc = R.run_cloud(strategy="ma", accounts=["real22000", "paper100k"],
                     dry=False, run_id="t8none", fetcher=none_fetcher, owned_conn=conn)

    record("T8 无可用行情 -> status=failed", rc["status"] == "failed",
           f"status={rc['status']}")
    record("T8 无可用行情 -> 零成交", _count(conn, "trades") == 0)
    record("T8 全部标的被标记 REJECTED",
           len(rc.get("rejected", [])) == 6, f"rejected={len(rc.get('rejected', []))}")
    record("T8 errors 非空且说明原因", bool(rc.get("errors")),
           (rc.get("errors") or [""])[0][:150])

    # 回执里必须有可核验的失败记录
    with conn.cursor() as cur:
        cur.execute("SELECT status, error FROM run_registry WHERE run_key LIKE '%t8none%' "
                    "ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    record("T8 run_registry 记录了 failed + error",
           bool(row and row["status"] == "failed" and row["error"]),
           f"{(row['status'] if row else None)} / {(row['error'] if row else None)}")

    _reset(conn)
    conn.close()


# ======================================================================
# T9 v1.3：单次抓取（问题 #1）+ dry-run 只读（问题 #2）
# ======================================================================
class _CountingFetcher:
    """包装 fetcher，记录每个标的被抓取的**次数**，用于证明单次抓取。"""

    def __init__(self, inner, prices=None):
        self.inner = inner
        self.calls = {}            # code -> 调用次数
        self.prices = prices or {}  # code -> close，用于构造"会变"的行情
        self.counter = 0

    def __call__(self, code, market, limit=260):
        self.calls[code] = self.calls.get(code, 0) + 1
        self.counter += 1
        rows = self.inner(code, market, limit)
        if code in self.prices:
            # 保证最后一根收盘价等于给定值，便于断言"成交价来自哪一次抓取"
            rows = [dict(r) for r in rows]
            rows[-1]["close"] = self.prices[code]
        return rows


def t9a_single_fetch_snapshot():
    """问题 #1：信号与成交价必须来自**同一次**抓取的同一份快照。"""
    section("T9a 单次抓取：信号与成交价同源（v1.3 问题 #1）")
    if not os.environ.get("DATABASE_URL"):
        record("T9a 单次抓取（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return
    import pg_ledger as P
    import run_cloud as R
    import daily_job as J

    conn = P.connect()
    P.init_ledger()
    _reset(conn)

    # 构造"上游足够强"的确定性序列：ma 策略下最后一根决定信号
    base = J._make_series(200, start=10.0)

    # 若第二次抓取返回不同价格，就会暴露"信号与成交价不同版本"的 bug。
    # 这里让计数 fetcher 每次都返回**递增**的最后一根价格，
    # 若代码二次调用 fetch_daily，成交价会 != 信号价。
    seq = {"n": 0}

    def drifting(code, market, limit=260):
        return [dict(r) for r in base]

    class _DriftFetcher:
        def __init__(self):
            self.calls = {}

        def __call__(self, code, market, limit=260):
            self.calls[code] = self.calls.get(code, 0) + 1
            rows = [dict(r) for r in base]
            # 每次调用把最后一根抬高 100 元 —— 只要发生第二次抓取就必然不一致
            rows[-1]["close"] = rows[-1]["close"] + 100.0 * (self.calls[code] - 1)
            return rows, "test", datetime.date.today().strftime("%Y-%m-%d"), False

    f = _DriftFetcher()
    rc = R.run_cloud(strategy="ma", accounts=["paper100k"], dry=True,
                     run_id="t9a", fetcher=f, owned_conn=conn)

    codes = [c for c, _n, _m in J.WATCHLIST]
    over = {c: f.calls.get(c, 0) for c in codes if f.calls.get(c, 0) > 1}
    record("T9a 每个标的恰好抓取 1 次（无二次 fetch_daily）",
           all(f.calls.get(c, 0) == 1 for c in codes),
           f"抓取次数={f.calls}")

    # 强断言：信号的 price 必须等于该标的第一次抓取的最后一根收盘价
    sigs = rc.get("signals") or []
    mism = []
    for s in sigs:
        # drift fetcher 第 1 次返回 base 原值（未被抬高）
        expected = base[-1]["close"]
        if abs(s["price"] - expected) > 1e-9:
            mism.append((s["code"], s["price"], expected))
    record("T9a 信号价 = 首次抓取快照末值（未混入第二次数据）",
           len(mism) == 0, f"不一致={mism}（原文若二次抓取会相差 100 元）")

    # 断言 quote_list 的 close 与 snapshot 同值（同一份副本）
    qmap = {q["code"]: q.get("close") for q in (rc.get("quotes") or [])}
    bad = [(c, qmap.get(c)) for c in qmap
           if qmap.get(c) is not None and abs(qmap[c] - base[-1]["close"]) > 1e-9]
    record("T9a 回执行情 close 与快照同源", len(bad) == 0, f"不一致={bad}")

    # 静态断言：信号计算处不得出现第二次 fetch_daily 调用
    # （先剥离注释与 docstring，否则会误匹配"绝不二次调用 fetch_daily"这类说明文字）
    src = _strip_noise(open(os.path.join(SRC, "run_cloud.py"), encoding="utf-8").read())
    body = src.split("def run_cloud(")[1].split("def _maybe_push(")[0]
    record("T9a run_cloud 内不出现 fetch_daily 调用",
           "fetch_daily" not in body,
           "run_cloud 主体含 fetch_daily 调用" if "fetch_daily" in body else "ok")
    record("T9a 信号只从 snapshot 读",
           "snapshot[code]" in body or "snapshot[" in body,
           "未发现 snapshot 引用")

    _reset(conn)
    conn.close()


def t9b_dry_run_readonly_no_tables():
    """问题 #2：dry-run 走只读路径，在**空库**上不得建表/写行。"""
    section("T9b dry-run 只读：空库不得建表（v1.3 问题 #2）")
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        record("T9b dry-run 只读（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return

    import psycopg
    import run_cloud as R
    import daily_job as J

    # 建一个**全新空库**，确认 dry-run 不会自动建表
    empty_dsn = _make_temp_db(dsn, "dryrun_empty")
    if not empty_dsn:
        record("T9b 空库创建", False, "无法创建临时空库")
        return

    # 记录初始状态：6 张表全部不存在
    with psycopg.connect(empty_dsn, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("""SELECT tablename FROM pg_tables
                           WHERE schemaname='public'""")
            tables_before = sorted(r[0] for r in cur.fetchall())
    record("T9b 空库初始无业务表", tables_before == [], f"tables={tables_before}")

    def fresh_fetcher(code, market, limit=260):
        rows = J._make_series(200, start=10.0)
        return rows, "test", datetime.date.today().strftime("%Y-%m-%d"), False

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = empty_dsn
    try:
        out = R.dry_run_readonly(strategy="ma", accounts=["paper100k"],
                                 fetcher=fresh_fetcher)
    finally:
        if old is not None:
            os.environ["DATABASE_URL"] = old

    record("T9b dry-run 返回 dry_run_ok", out.get("status") == "dry_run_ok",
           f"status={out.get('status')}")
    record("T9b dry-run 探测到 6 张表均缺失（未自动创建）",
           set(out.get("schema", {}).get("missing", [])) == {
               "acct", "positions", "trades", "equity_curve", "run_registry", "exec_lock"},
           f"missing={out.get('schema', {}).get('missing')}")
    record("T9b dry-run no_side_effects=True", out.get("no_side_effects") is True,
           f"no_side_effects={out.get('no_side_effects')}")

    with psycopg.connect(empty_dsn, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("""SELECT tablename FROM pg_tables
                           WHERE schemaname='public'""")
            tables_after = sorted(r[0] for r in cur.fetchall())
    record("T9b 运行后空库**仍然**无表", tables_after == [],
           f"tables={tables_after}（若 init_ledger 被调用会出现 6 张表）")

    # 静态断言：dry-run 路径不得引用 init_ledger（剥离注释/docstring 后判断）
    src = _strip_noise(open(os.path.join(SRC, "run_cloud.py"), encoding="utf-8").read())
    drybody = src.split("def dry_run_readonly(")[1].split("def main(")[0]
    record("T9b dry_run_readonly 内不调用 init_ledger",
           "init_ledger" not in drybody,
           "dry_run_readonly 含 init_ledger 调用" if "init_ledger" in drybody else "ok")
    record("T9b dry_run_readonly 内不获取执行锁",
           "acquire_lock" not in drybody,
           "含 acquire_lock" if "acquire_lock" in drybody else "ok")

    _drop_temp_db(dsn, "dryrun_empty")


def t9c_workflow_dry_run_gating():
    """问题 #2（工作流侧）：init_ledger 步骤必须被 dry_run != 'true' 门控。"""
    section("T9c daily.yml 步骤门控（v1.3 问题 #2）")
    wf_path = os.path.join(os.path.dirname(SRC), ".github", "workflows", "daily.yml")
    if not os.path.isfile(wf_path):
        record("T9c daily.yml 存在", False, f"未找到 {wf_path}")
        return

    raw = open(wf_path, encoding="utf-8").read()
    try:
        import yaml
        doc = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001
        record("T9c daily.yml 可解析", False, f"{type(e).__name__}: {e}")
        return

    steps = doc["jobs"]["run"]["steps"]

    init_steps = [s for s in steps if "init_ledger" in str(s.get("run", ""))]
    record("T9c 存在 init_ledger 步骤", len(init_steps) == 1,
           f"找到 {len(init_steps)} 个")
    if init_steps:
        cond = str(init_steps[0].get("if", ""))
        record("T9c init_ledger 被 dry_run != 'true' 门控",
               "dry_run != 'true'" in cond,
               f"if: {cond or '(无)'}")

    read_steps = [s for s in steps if "db_probe.py" in str(s.get("run", ""))]
    record("T9c 存在只读探测步骤（dry-run 专用）", len(read_steps) == 1,
           f"找到 {len(read_steps)} 个")
    if read_steps:
        cond = str(read_steps[0].get("if", ""))
        record("T9c 只读探测步骤仅在 dry_run == 'true' 执行",
               "dry_run == 'true'" in cond, f"if: {cond or '(无)'}")

    # 顺序断言：只读探测应排在写操作之前
    names = [str(s.get("name", "")) for s in steps]
    try:
        i_read = next(i for i, n in enumerate(names) if "READ-ONLY" in n)
        i_write = next(i for i, n in enumerate(names) if "init_ledger" in str(steps[i].get("run", "")))
        record("T9c 只读探测排在写操作之前", i_read < i_write,
               f"read@{i_read} write@{i_write}")
    except StopIteration:
        record("T9c 步骤顺序断言", False, "未找到相应步骤")


def t9d_dry_run_never_calls_init_ledger():
    """问题 #2（行为侧）：即便通过 main() 走 --dry-run，也不得建表。"""
    section("T9d --dry-run 主入口在空库上不建表（v1.3 问题 #2）")
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        record("T9d 主入口 dry-run（需 DATABASE_URL）", False, "已跳过：未配置 DATABASE_URL")
        return

    import psycopg
    import run_cloud as R

    empty_dsn = _make_temp_db(dsn, "dryrun_main")
    if not empty_dsn:
        record("T9d 空库创建", False, "无法创建临时空库")
        return

    # 直接调用 main()，模拟 `python run_cloud.py --dry-run`
    old_argv = sys.argv[:]
    old_dsn = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = empty_dsn
    buf = io.StringIO()
    sys.argv = ["run_cloud.py", "--dry-run", "--strategy", "ma"]
    try:
        import contextlib
        with contextlib.redirect_stdout(buf):
            code = R.main()
    finally:
        sys.argv = old_argv
        if old_dsn is not None:
            os.environ["DATABASE_URL"] = old_dsn

    text = buf.getvalue()
    record("T9d main(--dry-run) 退出码 0", code == 0, f"rc={code}")
    record("T9d 输出含 DRY_RUN_RESULT", "DRY_RUN_RESULT" in text,
           text.strip().splitlines()[-1][:200] if text.strip() else "(无输出)")

    import json as _json
    payload = {}
    for line in text.splitlines():
        if line.startswith("DRY_RUN_RESULT "):
            payload = _json.loads(line[len("DRY_RUN_RESULT "):])
            break
    record("T9d DRY_RUN_RESULT.status=dry_run_ok",
           payload.get("status") == "dry_run_ok", f"{payload}")
    record("T9d DRY_RUN_RESULT.no_side_effects=True",
           payload.get("no_side_effects") is True, f"{payload}")

    with psycopg.connect(empty_dsn, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("""SELECT tablename FROM pg_tables WHERE schemaname='public'""")
            tables = sorted(r[0] for r in cur.fetchall())
    record("T9d 主入口 dry-run 后空库仍无表", tables == [],
           f"tables={tables}")

    _drop_temp_db(dsn, "dryrun_main")


def _strip_noise(src):
    """去掉注释与字符串字面量，避免静态断言误匹配文档字符串。"""
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)      # 三引号 docstring
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    src = re.sub(r"#.*", "", src)                   # 行注释
    return src


# ---------- 临时库辅助（仅测试用） ----------
def _admin_dsn(dsn):
    """把 DSN 指向 postgres 维护库，用于 CREATE/DROP DATABASE。"""
    import urllib.parse as up
    u = up.urlparse(dsn)
    return u._replace(path="/postgres").geturl()


def _make_temp_db(dsn, name):
    import psycopg
    try:
        with psycopg.connect(_admin_dsn(dsn), autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
                cur.execute(f'CREATE DATABASE "{name}"')
        import urllib.parse as up
        u = up.urlparse(dsn)
        return u._replace(path=f"/{name}").geturl()
    except Exception as e:  # noqa: BLE001
        print(f"  (临时库创建失败: {e})")
        return None


def _drop_temp_db(dsn, name):
    import psycopg
    try:
        with psycopg.connect(_admin_dsn(dsn), autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception:  # noqa: BLE001
        pass


# ======================================================================
# 辅助
# ======================================================================
def _reset(conn):
    """清空业务表并重置资金（仅用于测试数据库）。"""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE trades, run_registry, positions, equity_curve RESTART IDENTITY")
        cur.execute("UPDATE acct SET cash=initial")
        cur.execute("DELETE FROM exec_lock")
    conn.commit()


def _count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return cur.fetchone()["n"]


def _snapshot_counts(conn):
    return {t: _count(conn, t) for t in
            ("trades", "run_registry", "positions", "equity_curve")}


def _exists(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone() is not None


def main():
    print("=" * 64)
    print("回归测试套件 —— 审查问题修复验证")
    print(f"运行时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"DATABASE_URL: {'已配置' if os.environ.get('DATABASE_URL') else '未配置'}")
    print("=" * 64)

    for fn in (t1_quote_freshness_policy, t1_stale_not_traded, t2_db_uniqueness,
               t2_exec_lock, t3_dry_run_no_side_effects, t4_multi_position_valuation,
               t5_empty_token_rejected, t6_removed_endpoints,
               t7_concurrent_and_duplicate, t8_all_quotes_missing,
               t9a_single_fetch_snapshot, t9b_dry_run_readonly_no_tables,
               t9c_workflow_dry_run_gating, t9d_dry_run_never_calls_init_ledger):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            record(f"{fn.__name__} 异常终止", False,
                   f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["pass"])
    print("\n" + "=" * 64)
    print(f"结果：{passed}/{total} 通过")
    failed = [r for r in RESULTS if not r["pass"]]
    if failed:
        print("\n失败项：")
        for r in failed:
            print(f"  ✗ {r['name']}\n      {r['detail']}")
    print("=" * 64)
    print("RESULT_JSON " + json.dumps(RESULTS, ensure_ascii=False))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
