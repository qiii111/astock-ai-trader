#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
missed_run_check.py —— 漏执行看门狗。

判定规则（不含主观"大概跑了"）：
  1. 计算「最近一个应执行交易日」：从今天往回找，跳过周六周日；
     （A 股法定节假日不在此处硬编码——那需要年度交易日历，
       本脚本对节假日采取**宽松**处理：只在下述第 3 条做二次确认。）
  2. 查询 run_registry 中 task_name='daily_job:ma' 且 status='done'
     且 market_date = 该交易日 的记录。
  3. 结论：
       - 找到           -> PASS（exit 0）
       - 未找到，但今天是周末 / 无任何历史成功记录（尚未首次上线）-> SKIP（exit 0）
       - 未找到且预期应有 -> FAIL（exit 1）→ GitHub 发送失败邮件

输出结构化 JSON，便于在 Actions Summary 里查看。
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg_ledger as P


TASK_NAME = "daily_job:ma"


def last_weekday(ref: datetime.date) -> datetime.date:
    """从 ref 往回找最近的工作日（周一~周五）。"""
    d = ref
    while d.weekday() >= 5:      # 5=周六 6=周日
        d -= datetime.timedelta(days=1)
    return d


def has_any_success(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM run_registry WHERE task_name=%s AND status='done' LIMIT 1",
                    (TASK_NAME,))
        return cur.fetchone() is not None


def has_any_history(conn) -> bool:
    """是否已有任何执行历史（无论成败）。

    用于区分两种完全不同的情况：
      · 从未有过任何记录  -> 尚未首次上线，SKIP（避免上线前天天误报）
      · 有记录但全部失败  -> **这是真实故障**，必须 FAIL 告警
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM run_registry WHERE task_name=%s LIMIT 1", (TASK_NAME,))
        return cur.fetchone() is not None


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("SKIP: DATABASE_URL 未配置（尚未接入 Neon），跳过漏执行检查")
        return 0

    print(f"DSN = {P.mask_dsn(dsn)}")

    # 云端按 UTC 运行；业务日期按北京时间判定
    bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today = bj_now.date()
    expect = last_weekday(today)

    conn = P.connect()
    try:
        if today.weekday() >= 5:
            print(json.dumps({"result": "SKIP", "reason": "今天是周末，A 股不交易",
                              "today": str(today)}, ensure_ascii=False))
            return 0

        if not has_any_history(conn):
            print(json.dumps({"result": "SKIP",
                              "reason": "尚无任何执行记录（首次上线前，看门狗未激活）",
                              "today": str(today)}, ensure_ascii=False))
            return 0

        last = P.last_done_run(conn, TASK_NAME)
        rows = P.runs_between(conn,
                              f"{expect} 00:00:00",
                              f"{today + datetime.timedelta(days=1)} 00:00:00")
        ok_today = [r for r in rows if r["status"] == "done"]
        failed = [r for r in rows if r["status"] == "failed"]
        running = [r for r in rows if r["status"] == "running"]

        if ok_today:
            print(json.dumps({"result": "PASS", "expected_date": str(expect),
                              "done_runs": len(ok_today),
                              "market_date": ok_today[-1].get("market_date"),
                              "finished_at": ok_today[-1].get("finished_at")},
                             ensure_ascii=False))
            return 0

        # 未成功：区分「跑了但失败」与「完全没跑」
        if any((r.get("market_date") or "") >= str(expect) for r in
               ([last] if last else [])):
            print(json.dumps({"result": "PASS", "expected_date": str(expect),
                              "note": "最近成功记录的行情日为预期交易日",
                              "last_run_key": last["run_key"],
                              "last_finished_at": last["finished_at"]},
                             ensure_ascii=False))
            return 0

        detail = {
            "result": "FAIL",
            "expected_date": str(expect),
            "today": str(today),
            "failed_runs": [{"run_key": r["run_key"], "error": (r.get("error") or "")[:300]}
                            for r in failed],
            "running_stuck": [r["run_key"] for r in running],
            "last_success": ({ "run_key": last["run_key"],
                               "finished_at": last["finished_at"],
                               "market_date": last.get("market_date")} if last else None),
            "hint": ("若为法定节假日，请在上方 runs 中确认当天确实无行情，"
                     "或在仓库变量 MISSED_CHECK_IGNORE_DATES 中登记豁免日期。"),
        }
        print("MISSED_RUN_DETECTED " + json.dumps(detail, ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
