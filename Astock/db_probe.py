#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_probe.py —— Neon 可达性 & 权限探测（不写业务数据）。

检查项：
  1. 能否用 DATABASE_URL 建立连接（含 TCP/TLS/认证）
  2. 服务端版本与当前时间（校验时钟偏移）
  3. 是否已初始化表结构（首次运行为空属正常）
  4. **只读探测**：SELECT 计数，确认当前角色具备读权限
  5. 输出脱敏 DSN，绝不打印密码

退出码：0=连接正常；1=连接失败。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_ledger as P

TABLES = ["acct", "positions", "trades", "equity_curve", "run_registry"]


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("SKIP: DATABASE_URL 未配置")
        return 0

    info = {"dsn": P.mask_dsn(dsn)}
    conn = None
    try:
        conn = P.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT version() AS v, now() AS ts, current_database() AS db")
            r = cur.fetchone()
        info.update({"connected": True, "server_version": (r["v"] or "")[:60],
                     "server_time": str(r["ts"]), "database": r["db"]})

        existing, missing = {}, []
        with conn.cursor() as cur:
            for t in TABLES:
                cur.execute("SELECT to_regclass(%s) AS oid", (f"public.{t}",))
                row = cur.fetchone()
                if row and row["oid"]:
                    cur.execute(f"SELECT COUNT(*) AS n FROM {t}")   # 只读计数
                    existing[t] = cur.fetchone()["n"]
                else:
                    missing.append(t)
        info["tables"] = existing
        info["tables_missing"] = missing
        info["verdict"] = "OK"
        print("DB_PROBE " + json.dumps(info, ensure_ascii=False))
        for t in TABLES:
            if t in existing:
                print(f"  [OK ] {t:<14} rows={existing[t]}")
            else:
                print(f"  [-- ] {t:<14} 未创建（尚未初始化，属正常）")
        return 0
    except SystemExit as e:
        print("DB_PROBE " + json.dumps({"connected": False, "error": str(e)[:200]},
                                       ensure_ascii=False))
        return 1
    except Exception as e:  # noqa: BLE001
        print("DB_PROBE " + json.dumps({"connected": False,
                                        "error": f"{type(e).__name__}: {str(e)[:200]}"},
                                       ensure_ascii=False))
        return 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
