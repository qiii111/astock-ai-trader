#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P3 独立运行验证测试套件（不依赖 Flask / 沙箱 / 真实数据库）

验证 4 项能力：
  T1. daily_job.py 可独立运行（仅标准库）
  T2. 数据库事务原子性（资金与持仓同事务，不出现"扣钱没加仓"）
  T3. 幂等写入（run_key 执行级 + trade_key 成交级，重复执行不重复成交）
  T4. 异常重试与断点恢复（行情源失败->缓存回退/失败标记；失败后可重试）

所有测试使用**临时数据库**，绝不触碰生产 data/astock.db。
"""
import os
import sys
import json
import sqlite3
import tempfile
import datetime
import importlib
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# 源码位于 ../astock/（本文件在 tools/ 下）；同时兼容把本文件放在源码同级的情况。
SRC = os.path.join(os.path.dirname(HERE), "astock")
if not os.path.isfile(os.path.join(SRC, "daily_job.py")):
    SRC = HERE
sys.path.insert(0, SRC)

RESULTS = []


def rec(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


# ---------------------------------------------------------------- T1
def t1_standalone():
    """daily_job.py 仅依赖标准库，可独立运行（不 import flask）"""
    src = open(os.path.join(SRC, "daily_job.py"), encoding="utf-8").read()
    has_flask = "flask" in src.lower() or "Flask" in src
    # 检查所有顶层 import 是否都在标准库白名单
    imports = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("import ") and not line.startswith(" "):
            imports.append(s.split()[1].split(".")[0])
    stdlib = {"argparse", "datetime", "hashlib", "json", "os", "sqlite3",
              "sys", "time", "urllib", "tempfile"}
    non_std = [i for i in imports if i not in stdlib]
    rec("T1 daily_job 无 Flask 依赖", not has_flask, f"含flask={has_flask}")
    rec("T1 daily_job 仅标准库导入", not non_std, f"非标准库={non_std or '无'}")
    # 实跑 --selftest
    r = subprocess.run([sys.executable, "daily_job.py", "--selftest"],
                       cwd=SRC, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "ASTOCK_NO_PUSH": "1"})
    ok = "RESULT_SELFTEST" in r.stdout
    rec("T1 daily_job --selftest 独立跑通", ok, "输出含 RESULT_SELFTEST" if ok else r.stderr[-200:])


# ---------------------------------------------------------------- T2/T3/T4
def _fresh_job(tmp):
    """在临时目录加载 daily_job，隔离生产库"""
    os.environ["ASTOCK_DIR"] = tmp
    import daily_job as J
    importlib.reload(J)
    J.DB_PATH = os.path.join(tmp, "t.db")
    J.DATA_DIR = tmp
    return J


def _mk_offline(J, codes, n=200):
    """复用 daily_job 官方合成器，保证触发金叉信号"""
    out = {}
    for i, (code, name, mk) in enumerate(codes):
        out[code] = (J._make_series(n, start=10.0 + i), "offline", "2025-07-18", False)
    return out


def t2_t3_t4():
    tmp = tempfile.mkdtemp(prefix="p3test_")
    J = _fresh_job(tmp)
    codes = J.WATCHLIST
    offline = _mk_offline(J, codes)

    # ---- T3 幂等：第一次跑 ----
    r1 = J.run(strategy="ma", accounts=["real22000", "paper100k"], dry=False,
               offline_rows=offline, push_enabled=False)
    c = J.db()
    n1 = c.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"]
    cash1 = c.execute("SELECT cash FROM acct WHERE account='real22000'").fetchone()["cash"]
    c.close()
    rec("T3 首次执行 status=done", r1["status"] == "done", f"status={r1['status']}")
    rec("T3 首次产生成交", n1 > 0, f"trades={n1}")

    # ---- T3 幂等：重复跑（同 run_id -> 应被成交级 trade_key 挡住）----
    r2 = J.run(strategy="ma", accounts=["real22000", "paper100k"], dry=False,
               offline_rows=offline, run_id=r1["run_key"].split(":")[2], push_enabled=False)
    c = J.db()
    n2 = c.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"]
    cash2 = c.execute("SELECT cash FROM acct WHERE account='real22000'").fetchone()["cash"]
    c.close()
    rec("T3 重复执行成交数不变（幂等）", n2 == n1, f"第二次 trades={n2} (首次={n1})")
    rec("T3 重复执行资金不变", abs(cash2 - cash1) < 0.001, f"cash {cash1} -> {cash2}")

    # ---- T2 事务原子性：资金不足时必须整笔回滚（不出现扣钱没加仓）----
    c = J.db()
    # 人为把 real22000 资金清零，然后用一个**全新代码**尝试买入（避开幂等跳过）
    c.execute("UPDATE acct SET cash=0 WHERE account='real22000'")
    c.commit()
    pos_before = c.execute("SELECT COUNT(*) n FROM positions WHERE account='real22000'").fetchone()["n"]
    cash_before = c.execute("SELECT cash FROM acct WHERE account='real22000'").fetchone()["cash"]
    c.close()
    st, detail = J.execute(J.db(), "real22000", "999999", "测试标的", "BUY",
                           price=1000.0, shares=0, date="2025-07-18",
                           reason="test-insufficient", dry=False)
    c = J.db()
    pos_after = c.execute("SELECT COUNT(*) n FROM positions WHERE account='real22000'").fetchone()["n"]
    cash_after = c.execute("SELECT cash FROM acct WHERE account='real22000'").fetchone()["cash"]
    c.close()
    rec("T2 资金不足被拒绝", st in ("skip", "reject", "insufficient"), f"status={st} {detail}")
    rec("T2 资金不足时无持仓变化", pos_after == pos_before and abs(cash_after - cash_before) < 0.001,
        f"pos {pos_before}->{pos_after}, cash {cash_before}->{cash_after}")

    # ---- T4 失败标记：全部无数据 -> status=failed 且记录原因 ----
    empty = {code: ([], "offline", None, True) for code, _, _ in codes}
    r_fail = J.run(strategy="ma", accounts=["real22000"], dry=True,
                   offline_rows=empty, run_id="failcase", push_enabled=False)
    rec("T4 全无数据 -> failed", r_fail["status"] == "failed", f"status={r_fail['status']}")
    c = J.db()
    row = c.execute("SELECT status,error FROM run_registry WHERE run_key LIKE '%%failcase%%'").fetchone()
    c.close()
    rec("T4 失败已记录原因", row and row["error"], f"error={row['error'] if row else None}")

    # ---- T4 断点恢复：失败后重试应能成功（不被在途锁死）----
    r_retry = J.run(strategy="ma", accounts=["real22000"], dry=True,
                    offline_rows=offline, run_id="retry-after-fail", push_enabled=False)
    rec("T4 失败后重试成功（断点恢复）", r_retry["status"] == "done", f"status={r_retry['status']}")

    # 清理
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 60)
    print("P3 独立运行验证测试套件（隔离临时库，不触碰生产数据）")
    print("=" * 60)
    t1_standalone()
    print("-" * 60)
    t2_t3_t4()
    print("=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"结果：{passed}/{total} 通过")
    print("RESULT_JSON " + json.dumps(
        [{"name": n, "pass": ok, "detail": d} for n, ok, d in RESULTS],
        ensure_ascii=False))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
