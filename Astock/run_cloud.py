#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_cloud.py —— GitHub Actions 专用入口：把 daily_job 的决策写入 Neon PostgreSQL。

设计原则（对应审查发现的问题）：
  #1 数据完整性：stale / 缓存回退 / 行情日期不符预期的数据 **一律拒绝成交**，
     而不是"记录下来继续跑"。行情缺失时安全跳过并如实上报。
  #2 并发安全：先 acquire_lock() 抢执行锁（DB 主键原子性），
     再靠 run_key UNIQUE 约束兜底；绝不依赖 SELECT ... LIKE 判重。
  #3 dry-run 无副作用：不抢锁、不写任何表（含 run_registry / equity_curve）。
  #4 估值正确：每只持仓用自己的最新有效价格，缺价则保守处理并报警。

为什么需要这一层，而不直接改 daily_job.py？
  daily_job.py 是**已验证 12/12 通过**的本地 SQLite 版本（自包含、仅标准库）。
  直接改它会破坏已被验证的行为。因此这里采用**适配器模式**：
      run_cloud.py 复用 daily_job 的行情抓取 / 信号计算 / 撮合规则，
      只把「落库」这一层替换为 pg_ledger（Neon）。

安全：
  · 连接串只从 DATABASE_URL 读取，日志输出一律脱敏（mask_dsn）。
  · 退出码：0=成功/跳过；1=失败（让 Actions 标红并邮件告警）。
"""
import argparse
import datetime
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daily_job as J          # 复用行情/信号/费用/限价规则
import pg_ledger as P          # Neon 落库层

# 行情新鲜度阈值（自然日）：超过则视为过期，拒绝据此成交。
# 注：A 股有周末与法定节假日，因此阈值需 > 3；取 7 天可在覆盖长假期
#     的同时，仍能拦住"缓存停在两个月前"这类真实故障。
MAX_QUOTE_AGE_DAYS = int(os.environ.get("ASTOCK_MAX_QUOTE_AGE_DAYS", "7"))


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _log(msg: str) -> None:
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _owner() -> str:
    """执行者标识，用于锁归属与排障。"""
    return f"{socket.gethostname()}:{os.getpid()}:{os.environ.get('GITHUB_RUN_ID', 'local')}"


def _run_key(strategy: str, accounts, run_id=None) -> str:
    slot = run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{_today()}:{strategy}:{slot}:" + ",".join(accounts)


def _parse_date(s):
    if not s:
        return None
    txt = str(s)[:10].replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def check_quote_freshness(as_of, stale: bool, today: datetime.date):
    """判断单个标的的行情是否可用于成交（问题 #1）。

    返回 (usable: bool, reason: str)。拒绝成交的情形：
      · stale=True            —— 走了缓存回退，说明在线源全失败
      · 无 as_of              —— 无数据
      · as_of 晚于今天        —— 未来数据（时间戳异常）
      · as_of 早于今天超过阈值 —— 过期数据（含缓存回退与源异常）
    """
    if stale:
        return False, "stale（缓存回退，在线源不可用）"
    if not as_of:
        return False, "无行情日期"
    d = _parse_date(as_of)
    if d is None:
        return False, f"无法解析行情日期 {as_of!r}"
    if d > today:
        return False, f"行情日期 {d} 晚于今天（时间戳异常）"
    age = (today - d).days
    if age > MAX_QUOTE_AGE_DAYS:
        return False, f"行情过期 {age} 天（阈值 {MAX_QUOTE_AGE_DAYS}）"
    return True, "ok"


def execute_pg(conn, account, code, name, action, price, shares, date, reason,
               dry=False, price_map=None):
    """A 股规则撮合 + 幂等写入（PostgreSQL 事务版）。

    ⚠️ 关键修正（问题 #4）：
       total_assets 不再用「当前交易标的价格」给**所有**持仓估值。
       改为按 price_map（各标的自己最新有效价）逐只估值；
       无法估值的持仓 -> 总资产不可信 -> 保守拒绝下单，
       避免高估总资产、放宽 40% 仓位上限而超额买入。

    ⚠️ 资金/持仓/成交三者在同一事务内提交（数据一致性）。
    """
    trade_key = f"{account}:{date}:{code}:{action}:{shares}"

    if P.trade_exists(conn, trade_key):
        return "duplicate", "已存在，跳过（幂等保护）"

    pos_list = P.get_positions(conn, account)
    position = next((p for p in pos_list if p["code"] == code), None)
    cash = P.get_cash(conn, account)

    # ---- 正确估值：逐持仓用各自最新有效价（问题 #4） ----
    pm = dict(price_map or {})
    if pm.get(code) in (None, 0):
        pm[code] = price          # 本次交易标的的价格是已知的
    mv, missing, _used = P.value_positions(conn, account, pm)
    total_assets = cash + mv

    if action == "BUY":
        if position:
            return "skip", "已持有，不加仓（单次信号单次买入）"
        if missing:
            return "skip", (f"拒绝下单：持仓 {'/'.join(missing)} 缺最新价，"
                            f"总资产无法可靠估算")
        budget = min(cash, total_assets * J.MAX_POS_RATIO)
        shares = int(budget / price // J.LOT) * J.LOT
        if shares < J.LOT:
            return "skip", f"资金不足（可用 {budget:.0f} 元）"
        amount, commission, stamp, transfer, cost = J.fees(price, shares, "BUY")
        if amount + cost > cash:
            return "skip", "现金不足覆盖成本"
        if dry:
            return "dry", f"BUY {shares}股 花费 {amount + cost:.2f}"
        try:
            P.set_cash(conn, account, cash - (amount + cost))
            P.upsert_position(conn, account, code, name, shares, (amount + cost) / shares, date)
            inserted = P.record_trade(conn, {
                "account": account, "trade_key": trade_key, "date": date, "code": code,
                "name": name, "action": action, "price": price, "shares": shares,
                "amount": amount, "commission": commission, "stamp_tax": stamp,
                "transfer_fee": transfer, "cost_total": cost, "realized_pnl": None,
                "reason": reason,
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            if not inserted:
                conn.rollback()
                return "duplicate", "并发插入冲突，已回滚（幂等保护）"
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return "done", f"BUY {shares}股 @ {price:.2f} 成本 {cost:.2f}"

    # ---- SELL ----
    if not position or position["shares"] <= 0:
        return "skip", "无持仓，无法卖出"
    held = int(position["shares"])
    if position["buy_date"] == date:
        return "skip", "T+1 限制（当日买入不可卖）"
    shares = held
    amount, commission, stamp, transfer, cost = J.fees(price, shares, "SELL")
    proceeds = amount - cost
    pnl = proceeds - float(position["avg_cost"]) * shares
    if dry:
        return "dry", f"SELL {shares}股 净得 {proceeds:.2f}"
    try:
        P.set_cash(conn, account, cash + proceeds)
        P.upsert_position(conn, account, code, name, 0, 0.0, date)
        inserted = P.record_trade(conn, {
            "account": account, "trade_key": trade_key, "date": date, "code": code,
            "name": name, "action": action, "price": price, "shares": shares,
            "amount": amount, "commission": commission, "stamp_tax": stamp,
            "transfer_fee": transfer, "cost_total": cost, "realized_pnl": pnl,
            "reason": reason,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        if not inserted:
            conn.rollback()
            return "duplicate", "并发插入冲突，已回滚（幂等保护）"
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return "done", f"SELL {shares}股 @ {price:.2f} 成本 {cost:.2f}"


def _fetch_all_quotes(fetcher=None):
    """抓取全部标的行情并做新鲜度判定（问题 #1）。

    ⚠️ 单次抓取原则（v1.3 问题 #1）：
       本函数是**唯一**的行情获取入口。返回的 snapshot 会被信号计算与成交
       共同复用，绝不二次调用 fetch_daily —— 否则信号与成交价可能来自
       不同数据版本（盘中价格会变），造成"按 A 版本决策、按 B 版本成交"。

    fetcher: 可注入的行情函数（供测试）。签名与 J.fetch_daily 相同。

    返回 (price_map, quote_list, market_date, meta, snapshot)：
      price_map  仅含**新鲜可用**标的 {code: close}
      quote_list 全部标的状态（含被拒绝原因），用于回执与告警
      snapshot   {code: closes 列表}，**唯一**的行情副本，供信号计算复用
    """
    f = fetcher or J.fetch_daily
    today = datetime.date.today()
    price_map, quote_list, sources, stale_any = {}, [], set(), False
    rejected = []
    market_date = None
    snapshot = {}          # code -> [close, ...]  唯一副本

    for code, name, market in J.WATCHLIST:
        rows, src, as_of, stale = f(code, market)
        sources.add(src)
        stale_any = stale_any or stale
        usable, why = check_quote_freshness(as_of, stale, today)

        if not rows:
            usable, why = False, f"无数据（源={src}）"

        if not usable:
            _log(f"{code} {name}: 拒绝成交 —— {why}")
            quote_list.append({"code": code, "name": name, "close": None,
                               "action": "REJECTED", "reason": why,
                               "as_of": as_of, "source": src})
            rejected.append({"code": code, "name": name, "reason": why,
                             "as_of": as_of, "source": src})
            continue

        closes = [r["close"] for r in rows]
        # 存下唯一副本：后续信号计算**只能**从这里读，不再重新抓取
        snapshot[code] = closes
        price_map[code] = closes[-1]
        market_date = max(market_date or as_of, as_of)
        quote_list.append({
            "code": code, "name": name, "close": closes[-1],
            "ma20": round(J._sma(closes, 20)[-1], 2) if len(closes) >= 20 else None,
            "ma60": round(J._sma(closes, 60)[-1], 2) if len(closes) >= 60 else None,
            "action": "HOLD", "as_of": as_of, "source": src,
        })
        _log(f"{code} {name}: close={closes[-1]:.2f} as_of={as_of} src={src}")

    return price_map, quote_list, market_date, {
        "usable": sorted(price_map.keys()),
        "rejected": rejected,
        "stale_any": stale_any,
        "source_label": ",".join(sorted(str(s) for s in sources if s)),
    }, snapshot


def run_cloud(strategy="ma", accounts=None, dry=False, run_id=None,
              fetcher=None, owned_conn=None):
    accounts = accounts or list(P.ACCOUNTS.keys())
    conn = owned_conn or P.connect()
    owns_conn = owned_conn is None

    run_key = _run_key(strategy, accounts, run_id)
    owner = _owner()
    lock_key = f"{_today()}:{strategy}:" + ",".join(accounts)

    if dry:
        _log("DRY-RUN：不获取执行锁、不写入任何表（问题 #3）")

    locked = False
    work_key = run_key if not dry else f"dry:{run_key}"
    try:
        if not dry:
            # 问题 #2：先抢 DB 级执行锁（原子，非 SELECT 判重）
            locked = P.acquire_lock(conn, lock_key, owner)
            if not locked:
                _log(f"未获取执行锁 {lock_key}（他人持有），跳过")
                return {"run_key": run_key, "status": "locked"}

        # 问题 #1：**单次**抓行情 + 新鲜度判定，之后信号与成交共用同一快照
        price_map, quote_list, market_date, meta, snapshot = _fetch_all_quotes(fetcher)

        if not price_map:
            err = ("全部标的行情不可用（缺失/stale/过期），已拒绝所有成交 | "
                   + "; ".join(f"{r['code']}={r['reason']}" for r in meta["rejected"]))
            _log("FAIL: " + err)
            if not dry:
                P.start_run(conn, work_key, f"daily_job:{strategy}", ",".join(accounts))
                P.finish_run(conn, work_key, "failed", None, meta["source_label"],
                             f"quotes_rejected={len(meta['rejected'])}", err)
            return {"run_key": run_key, "status": "failed", "errors": [err],
                    "quotes": quote_list, "rejected": meta["rejected"],
                    "summary": {}, "dry": dry}

        # 占用 run_key：UNIQUE 约束兜底（dry-run 不落库）
        if not dry:
            if not P.start_run(conn, work_key, f"daily_job:{strategy}", ",".join(accounts)):
                _log(f"run_key={work_key} 已存在（UNIQUE 约束），跳过")
                return {"run_key": run_key, "status": "duplicate"}

        # 信号：**只从 snapshot 读**，绝不重新抓取（v1.3 问题 #1）
        # 这样信号的输入与成交价必然同源同版本。
        signals = []
        for q in quote_list:
            code = q["code"]
            if code not in snapshot:
                continue
            closes = snapshot[code]          # ← 复用已抓取的那一份，不二次调用 fetch_daily
            sig, reason = J.compute_signals(closes, strategy)
            q["action"] = "BUY" if sig == 1 else "SELL" if sig == -1 else "HOLD"
            if sig != 0:
                # 成交价取自同一快照的最后一根收盘价
                signals.append({"code": code, "name": q["name"],
                                "action": q["action"], "reason": reason,
                                "price": closes[-1], "date": market_date})
        _log(f"可用={len(price_map)} 拒绝={len(meta['rejected'])} 信号={len(signals)}")

        # 逐账户执行（问题 #4：传 price_map 做正确估值）
        exec_log = {}
        for acct in accounts:
            acts = []
            for s in signals:
                st, detail = execute_pg(conn, acct, s["code"], s["name"], s["action"],
                                        s["price"], 0, market_date, s["reason"],
                                        dry=dry, price_map=price_map)
                acts.append({"code": s["code"], "action": s["action"],
                             "status": st, "detail": detail})
                _log(f"  [{acct}] {s['code']} {s['action']}: {st} - {detail}")
            exec_log[acct] = acts

        # 净值快照（问题 #4：逐持仓各自最新有效价）
        summary = {}
        for acct in accounts:
            cash = P.get_cash(conn, acct)
            mv, missing, _used = P.value_positions(conn, acct, price_map)
            total = cash + mv
            initial = P.ACCOUNTS[acct]["initial"]
            if market_date and not dry:
                P.upsert_equity(conn, acct, market_date, cash, mv, market_date,
                                (total / initial - 1) * 100)
            summary[acct] = {"cash": round(cash, 2), "market_value": round(mv, 2),
                             "total": round(total, 2),
                             "return_pct": round((total / initial - 1) * 100, 2),
                             "positions": len(P.get_positions(conn, acct)),
                             "unvalued_positions": missing}
        if not dry:
            conn.commit()

        note = (f"quotes_ok={len(price_map)} rejected={len(meta['rejected'])} "
                f"signals={len(signals)} stale_any={meta['stale_any']} dry={dry}")
        err = None
        if meta["rejected"]:
            err = ("部分标的行情不可用已拒绝成交: "
                   + "; ".join(f"{r['code']}={r['reason']}" for r in meta["rejected"]))
        if not dry:
            P.finish_run(conn, work_key, "done", market_date, meta["source_label"], note, err)

        receipt = {"run_key": run_key, "strategy": strategy, "status": "done",
                   "market_date": market_date, "source": meta["source_label"],
                   "stale": meta["stale_any"], "quotes": quote_list, "signals": signals,
                   "exec": exec_log, "summary": summary, "dry": dry,
                   "rejected": meta["rejected"], "errors": ([err] if err else []),
                   "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    finally:
        if locked:
            try:
                P.release_lock(conn, lock_key, owner)
            except Exception as e:  # noqa: BLE001
                _log(f"释放锁失败（将由 TTL 自动过期）: {e!r}")
        if owns_conn:
            conn.close()

    if not dry:
        _maybe_push(receipt)
    return receipt


def _maybe_push(receipt):
    url = os.environ.get("ASTOCK_PUSH_URL", "").strip()
    token = os.environ.get("ASTOCK_INGEST_TOKEN", "").strip()
    if not url or not token:
        _log("未配置 ASTOCK_PUSH_URL / ASTOCK_INGEST_TOKEN，跳过回执推送")
        return
    import urllib.request
    body = json.dumps({"date": receipt.get("market_date"), "quotes": receipt.get("quotes"),
                       "signals": receipt.get("signals"), "receipt": receipt},
                      ensure_ascii=False).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "X-Auth-Token": token, **J.UA})
        resp = urllib.request.urlopen(req, timeout=20).read().decode()
        _log("PUSH_OK:" + resp[:120])
    except Exception as e:  # noqa: BLE001
        _log("PUSH_FAIL:" + repr(e)[:160])


def probe_schema_readonly(conn) -> dict:
    """只读探测：不建表、不迁移、不写任何行（v1.3 问题 #2）。

    返回各表是否已存在及行数；仅供 dry-run 报告使用。
    **不调用 init_ledger()** —— 那是写操作。
    """
    info = {"tables": {}, "missing": []}
    with conn.cursor() as cur:
        for t in ("acct", "positions", "trades", "equity_curve", "run_registry", "exec_lock"):
            cur.execute("SELECT to_regclass(%s) AS oid", (f"public.{t}",))
            row = cur.fetchone()
            if row and row["oid"]:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                info["tables"][t] = cur.fetchone()["n"]
            else:
                info["missing"].append(t)
    return info


def dry_run_readonly(strategy="ma", accounts=None, fetcher=None):
    """**真正的只读 dry-run**（v1.3 问题 #2）。

    保证：
      · **不**调用 init_ledger() —— 不建表、不迁移、不插账户行
      · **不**获取执行锁
      · **不**写 run_registry / trades / positions / equity_curve / acct
      · **不**推送回执
      · 数据库只做 SELECT（含 to_regclass 探测）
      · 只抓行情并计算信号，把结果打印出来供人工核对

    若 DATABASE_URL 未配置，则完全跳过数据库，仅验证行情与信号链路。
    """
    accounts = accounts or list(P.ACCOUNTS.keys())
    _log("=" * 56)
    _log("DRY-RUN（只读验证）：不建表 / 不迁移 / 不写任何行")
    _log("=" * 56)

    out = {"mode": "dry_run_readonly", "strategy": strategy,
           "accounts": accounts, "db_checked": False}

    # ---- 1) 数据库只读探测（若配置了 DSN） ----
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        _log(f"DSN = {P.mask_dsn(dsn)}")
        try:
            conn = P.connect()                     # 仅连接，不 init_ledger
            before = probe_schema_readonly(conn)
            out["db_checked"] = True
            out["schema"] = before
            if before["missing"]:
                _log(f"警告：以下表不存在（只读模式不会创建）: {before['missing']}")
            else:
                _log("表结构完整: " + ", ".join(f"{k}={v}" for k, v in before["tables"].items()))
            conn.close()
        except SystemExit as e:
            out["db_error"] = str(e)[:200]
            _log(f"数据库不可达（不视为失败）: {e}")
        except Exception as e:  # noqa: BLE001
            out["db_error"] = f"{type(e).__name__}: {e}"[:200]
            _log(f"数据库探测异常（不视为失败）: {out['db_error']}")
    else:
        _log("DATABASE_URL 未配置 -> 跳过数据库探测，仅验证行情与信号")

    # ---- 2) 抓行情 + 新鲜度判定 + 算信号（纯计算，无写） ----
    price_map, quote_list, market_date, meta, snapshot = _fetch_all_quotes(fetcher)
    out["quotes_ok"] = sorted(price_map.keys())
    out["quotes_rejected"] = meta["rejected"]
    out["market_date"] = market_date
    out["source"] = meta["source_label"]
    out["stale_any"] = meta["stale_any"]

    signals = []
    for q in quote_list:
        code = q["code"]
        if code not in snapshot:
            continue
        sig, reason = J.compute_signals(snapshot[code], strategy)
        q["action"] = "BUY" if sig == 1 else "SELL" if sig == -1 else "HOLD"
        if sig != 0:
            signals.append({"code": code, "name": q["name"], "action": q["action"],
                            "reason": reason, "price": snapshot[code][-1],
                            "date": market_date})
    out["signals"] = signals
    out["quotes"] = quote_list

    _log(f"可用={len(price_map)} 拒绝={len(meta['rejected'])} 信号={len(signals)}")
    if signals:
        for s in signals:
            _log(f"  将要执行（未执行）: [{s['action']}] {s['code']} {s['name']} @ {s['price']:.2f}")
    else:
        _log("本次未产生信号（这很正常）")

    # ---- 3) 二次只读复核：确认真的什么都没写 ----
    if out.get("db_checked") and dsn:
        try:
            conn = P.connect()
            after = probe_schema_readonly(conn)
            conn.close()
            out["schema_after"] = after
            unchanged = (after["tables"] == before["tables"]
                         and after["missing"] == before["missing"])
            out["no_side_effects"] = unchanged
            if unchanged:
                _log("只读复核：表结构与行数均未变化 ✓")
            else:
                _log(f"只读复核失败！before={before} after={after}")
        except Exception as e:  # noqa: BLE001
            out["no_side_effects"] = None
            _log(f"只读复核跳过（连接异常）: {e!r}")

    out["status"] = "dry_run_ok"
    return out


def main():
    ap = argparse.ArgumentParser(description="GitHub Actions → Neon 每日作业")
    ap.add_argument("--strategy", default="ma", choices=["ma", "macd"])
    ap.add_argument("--account", default=None, help="real22000 / paper100k，缺省=双账户")
    ap.add_argument("--dry-run", action="store_true",
                    help="只读验证：不建表/不迁移/不写任何行（v1.3 问题 #2）")
    a = ap.parse_args()
    accounts = [a.account] if a.account else list(P.ACCOUNTS.keys())

    # ---- dry-run：走独立只读路径，绝不触碰 init_ledger ----
    if a.dry_run:
        out = dry_run_readonly(strategy=a.strategy, accounts=accounts)
        print("DRY_RUN_RESULT " + json.dumps({
            "status": out["status"],
            "db_checked": out["db_checked"],
            "no_side_effects": out.get("no_side_effects"),
            "market_date": out.get("market_date"),
            "quotes_ok": out.get("quotes_ok"),
            "signals": len(out.get("signals") or []),
            "rejected": len(out.get("quotes_rejected") or []),
        }, ensure_ascii=False))
        return 0

    _log(f"DSN = {P.mask_dsn(os.environ.get('DATABASE_URL', ''))}")
    receipt = run_cloud(strategy=a.strategy, accounts=accounts, dry=False)

    st = receipt.get("status")
    if st in ("duplicate", "locked"):
        print("RESULT " + json.dumps({"status": st, "run_key": receipt.get("run_key")},
                                     ensure_ascii=False))
        return 0
    print("RESULT " + json.dumps({k: receipt.get(k) for k in
          ("run_key", "status", "market_date", "source", "stale")}, ensure_ascii=False))
    print("SUMMARY " + json.dumps(receipt.get("summary", {}), ensure_ascii=False))
    if receipt.get("rejected"):
        print("REJECTED " + json.dumps(receipt["rejected"], ensure_ascii=False))
    return 0 if st == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
