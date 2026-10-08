#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手机控制台（Flask 单端口 HTTP 服务）。

读取共享账本（ledger / daily_job 落库）展示：
  - 双账户总览：实盘对标 22,000 元 + 对照账户 100,000 元
  - 持仓、成交明细、费用拆分（佣金/印花税/过户费）
  - 净值曲线、今日信号、行情快照
  - 定时任务执行回执（run_registry）：可核验"是否真跑了、跑了什么、行情日期、数据源"
监听 $PORT（发布为应用时由平台注入），绑定 0.0.0.0。
"""
import json
import os

from flask import Flask, render_template_string, jsonify, request

from config import DECISION_MODE, WATCHLIST, INGEST_TOKEN
from config import READ_TOKEN
from db import get_conn, init_db
from ledger import ACCOUNTS, init_ledger

app = Flask(__name__)

CACHE = {"t": 0, "data": None}


def _token_eq(a: str, b: str) -> bool:
    """常量时间比较，避免通过响应时间差推测 Token（时序攻击）。

    ⚠️ 前提：调用方**必须**已确认 b 非空（问题 #5）。
       本函数不做"空对空"放行。
    """
    import hmac
    if not a or not b:
        return False
    return hmac.compare_digest(str(a), str(b))


def _fmt(v, nd=2):
    return "-" if v is None else f"{v:,.{nd}f}"


# 测试/探针标记黑名单（问题 #7/#8：防止测试数据污染控制台展示）
_BAD_MARKERS = ("HACK", "UNAUTH", "PROBE", "TEST-", "SELFTEST", "VALID-")


def _is_valid_live_payload(payload_str):
    """结构校验：必须是真实行情快照（有非空 quotes 且至少一条有收盘价）。

    仅靠关键词黑名单不够——自造的测试回执（如空 quotes）会绕过黑名单。
    因此这里做正向校验：没有真实行情的一律不展示。
    """
    try:
        p = json.loads(payload_str)
    except Exception:
        return False
    if not isinstance(p, dict):
        return False
    quotes = p.get("quotes")
    if not isinstance(quotes, list) or not quotes:
        return False
    # 至少一条有有效收盘价
    return any(isinstance(q, dict) and q.get("close") for q in quotes)


def _latest_valid_live(conn):
    """取最近一条"结构有效且非测试"的行情推送，供控制台展示。"""
    rows = conn.execute(
        "SELECT ts,date,payload FROM live_push "
        "WHERE date IS NOT NULL AND date != '' AND date LIKE '____-__-__' "
        "ORDER BY id DESC LIMIT 50"
    ).fetchall()
    for r in rows:
        if any(m in r["payload"].upper() for m in _BAD_MARKERS):
            continue
        if _is_valid_live_payload(r["payload"]):
            return r
    return None


def _svg_equity(points, initial):
    """points: [(date,total)]；红线=账户净值，灰虚线=初始本金。"""
    if len(points) < 2:
        return ""
    w, h, pad = 320, 140, 10
    totals = [p[1] for p in points]
    lo, hi = min(totals + [initial]), max(totals + [initial])
    if hi == lo:
        hi += 1
    n = len(totals)
    x = lambda i: pad + i * (w - 2 * pad) / (n - 1)
    y = lambda v: h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(totals))
    base = y(initial)
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="none">'
            f'<line x1="{pad}" y1="{base:.1f}" x2="{w-pad}" y2="{base:.1f}" stroke="#bbb" stroke-dasharray="3,3"/>'
            f'<polyline fill="none" stroke="#e63946" stroke-width="2" points="{pts}"/></svg>')


HTML = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股 AI 模拟交易控制台</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1d3557;color:#fff;padding:14px 16px;font-size:17px;font-weight:600}
.wrap{padding:12px;max-width:640px;margin:0 auto}
.card{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.kpi{background:#f8f9fb;border-radius:8px;padding:10px}
.kpi .l{font-size:12px;color:#888}.kpi .v{font-size:19px;font-weight:700;margin-top:2px}
.pos{color:#e63946}.neg{color:#2a9d8f}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 6px;text-align:right;border-bottom:1px solid #eee}
th:first-child,td:first-child{text-align:left}
.sig-buy{color:#e63946;font-weight:700}.sig-sell{color:#2a9d8f;font-weight:700}.sig-hold{color:#888}
.tag{display:inline-block;font-size:11px;background:#eef;color:#335;padding:1px 6px;border-radius:6px}
.ok{color:#2a9d8f}.bad{color:#e63946}.warn{color:#e08a00}
h3{font-size:14px;margin:0 0 8px;color:#1d3557}
.refresh{font-size:12px;color:#888;margin:6px 0 0}
.banner{font-size:12px;background:#fff3cd;color:#7a5b00;padding:8px 10px;border-radius:8px;margin-bottom:12px}
</style></head><body>
<header>A股 AI 模拟交易控制台 <span class="tag">{{ mode }} 模式 · 纯模拟</span></header>
<div class="wrap">
  <div class="banner">本系统为纯模拟盘，与真实券商<b>零连接、零下单</b>。用于策略验证，辅助真实投资决策。</div>

  {% for a in accounts %}
  <div class="card">
    <h3>{{ a.label }} <span class="tag">{{ a.account }}</span></h3>
    <div class="grid">
      <div class="kpi"><div class="l">总资产</div><div class="v">{{ '%.2f'|format(a.total) }}</div></div>
      <div class="kpi"><div class="l">收益率</div><div class="v {{'pos' if a.return_pct>=0 else 'neg'}}">{{ '%+.2f'|format(a.return_pct) }}%</div></div>
      <div class="kpi"><div class="l">现金</div><div class="v">{{ '%.2f'|format(a.cash) }}</div></div>
      <div class="kpi"><div class="l">持仓市值</div><div class="v">{{ '%.2f'|format(a.market_value) }}</div></div>
      <div class="kpi"><div class="l">最大回撤</div><div class="v neg">{{ '%.2f'|format(a.max_drawdown) }}%</div></div>
      <div class="kpi"><div class="l">持仓/成交</div><div class="v">{{ a.positions }} / {{ a.trades }}</div></div>
    </div>
    {% if a.svg %}<div style="margin-top:10px">{{ a.svg|safe }}</div>
    <p class="refresh">净值曲线 · 本金 {{ '%.0f'|format(a.initial) }} 元 · 截至 {{ a.last_date }}</p>{% endif %}
    {% if a.holds %}
    <table style="margin-top:8px"><tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th><th>现价</th><th>浮动盈亏</th></tr>
    {% for p in a.holds %}<tr><td>{{p.code}}</td><td>{{p.name}}</td><td>{{p.shares}}</td>
      <td>{{ '%.2f'|format(p.avg_cost) }}</td><td>{{ '%.2f'|format(p.price) }}</td>
      <td class="{{'pos' if p.pnl>=0 else 'neg'}}">{{ '%+.2f'|format(p.pnl) }} ({{ '%+.1f'|format(p.pnl_pct) }}%)</td></tr>{% endfor %}
    </table>{% else %}<p class="refresh">当前无持仓</p>{% endif %}
  </div>
  {% endfor %}

  {% if run %}
  <div class="card"><h3>定时任务执行回执（可核验）</h3>
    <table>
      <tr><th>run_key</th><td style="text-align:left">{{ run.run_key }}</td></tr>
      <tr><th>状态</th><td style="text-align:left" class="{{ 'ok' if run.status=='done' else 'bad' }}">{{ run.status }}</td></tr>
      <tr><th>行情日期</th><td style="text-align:left">{{ run.market_date }}</td></tr>
      <tr><th>数据源</th><td style="text-align:left">{{ run.source }}</td></tr>
      <tr><th>开始/结束</th><td style="text-align:left">{{ run.started_at }} → {{ run.finished_at }}</td></tr>
      <tr><th>说明</th><td style="text-align:left">{{ run.note }}</td></tr>
    </table>
    <p class="refresh">该回执由定时任务运行后写入共享账本，证明任务确实执行（而非仅看 lastRunAt）。</p></div>
  {% endif %}

  <div class="card"><h3>今日信号（{{ mode }} 模式）</h3>
    <table><tr><th>代码</th><th>名称</th><th>信号</th><th>说明</th></tr>
    {% for d in decisions %}<tr><td>{{d.code}}</td><td>{{d.name}}</td>
      <td class="{{ 'sig-buy' if d.action=='BUY' else ('sig-sell' if d.action=='SELL' else 'sig-hold') }}">{{d.action}}</td>
      <td style="text-align:left">{{d.reason}}</td></tr>{% endfor %}
    {% if not decisions %}<tr><td colspan="4" class="refresh">暂无（等待定时任务写入）</td></tr>{% endif %}
    </table></div>

  {% if live %}
  <div class="card"><h3>最新行情（定时任务推送 · 交易日 {{ live.date }})</h3>
    <table><tr><th>代码</th><th>名称</th><th>收</th><th>MA20</th><th>MA60</th><th>信号</th></tr>
    {% for q in live.quotes %}<tr><td>{{q.code}}</td><td>{{q.name}}</td>
      <td>{% if q.close %}{{ '%.2f'|format(q.close) }}{% else %}<span class="sig-hold">无数据</span>{% endif %}</td>
      <td>{% if q.ma20 %}{{ '%.2f'|format(q.ma20) }}{% else %}-{% endif %}</td>
      <td>{% if q.ma60 %}{{ '%.2f'|format(q.ma60) }}{% else %}-{% endif %}</td>
      <td class="{{ 'sig-buy' if q.action=='BUY' else ('sig-sell' if q.action=='SELL' else 'sig-hold') }}">{{q.action}}</td></tr>{% endfor %}
    </table>
    <p class="refresh">推送时间 {{ live.ts }} · 数据源故障时自动回退缓存（标记 stale）</p></div>
  {% endif %}

  <div class="card"><h3>最近成交</h3>
    <table><tr><th>日期</th><th>账户</th><th>动作</th><th>代码</th><th>价</th><th>股</th><th>费用</th></tr>
    {% for t in tx %}<tr><td>{{t.date or '-'}}</td><td>{{t.account}}</td>
      <td class="{{ 'sig-buy' if t.action=='BUY' else 'sig-sell' }}">{{t.action}}</td>
      <td>{{t.code}}</td><td>{{ '%.2f'|format(t.price or 0) }}</td><td>{{t.shares}}</td>
      <td>{{ '%.2f'|format(t.cost_total or 0) }}</td></tr>{% endfor %}
    {% if not tx %}<tr><td colspan="7" class="refresh">暂无成交（等待定时任务写入）</td></tr>{% endif %}
    </table></div>

  <p class="refresh">数据更新：{{ updated }} · 纯模拟，与真实券商无任何连接。</p>
</div></body></html>
"""


def _account_block(conn, acct):
    meta = ACCOUNTS[acct]
    row = conn.execute("SELECT initial,cash,label FROM acct WHERE account=?", (acct,)).fetchone()
    if not row:
        return {"account": acct, "label": meta["label"], "initial": meta["initial"],
                "cash": meta["initial"], "market_value": 0, "total": meta["initial"],
                "return_pct": 0.0, "max_drawdown": 0.0, "positions": 0, "trades": 0,
                "holds": [], "svg": "", "last_date": "-"}
    # 最新价
    prices = {}
    for q in conn.execute(
        "SELECT code, close FROM daily_quotes WHERE (code,date) IN "
        "(SELECT code, MAX(date) FROM daily_quotes GROUP BY code)"
    ):
        prices[q["code"]] = q["close"]
    live = _latest_valid_live(conn)
    if live:
        try:
            for q in json.loads(live["payload"]).get("quotes", []):
                if q.get("close"):
                    prices[q["code"]] = q["close"]
        except Exception:
            pass

    holds = []
    mv = 0.0
    for p in conn.execute("SELECT * FROM positions WHERE account=? ORDER BY code", (acct,)):
        price = prices.get(p["code"], p["avg_cost"])
        val = price * p["shares"]
        mv += val
        pnl = (price - p["avg_cost"]) * p["shares"]
        holds.append({"code": p["code"], "name": p["name"], "shares": p["shares"],
                      "avg_cost": p["avg_cost"], "price": price, "pnl": pnl,
                      "pnl_pct": (price / p["avg_cost"] - 1) * 100 if p["avg_cost"] else 0})
    cash = row["cash"]
    total = cash + mv
    # 回撤
    eq = conn.execute("SELECT date,total FROM equity_curve WHERE account=? ORDER BY date", (acct,)).fetchall()
    pts = [(r["date"], r["total"]) for r in eq]
    peak, mdd = -1e18, 0.0
    for _, v in pts:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    n_tr = conn.execute("SELECT COUNT(*) c FROM trades WHERE account=?", (acct,)).fetchone()["c"]
    return {"account": acct, "label": row["label"] or meta["label"], "initial": row["initial"],
            "cash": cash, "market_value": mv, "total": total,
            "return_pct": (total / row["initial"] - 1) * 100 if row["initial"] else 0,
            "max_drawdown": mdd, "positions": len(holds), "trades": n_tr,
            "holds": holds, "svg": _svg_equity(pts, row["initial"]),
            "last_date": pts[-1][0] if pts else "-"}


def _snapshot():
    conn = get_conn()
    accounts = [_account_block(conn, a) for a in ACCOUNTS]
    tx = conn.execute(
        "SELECT date,account,action,code,price,shares,cost_total FROM trades ORDER BY id DESC LIMIT 15"
    ).fetchall()
    run = conn.execute(
        "SELECT run_key,status,market_date,source,started_at,finished_at,note "
        "FROM run_registry ORDER BY id DESC LIMIT 1"
    ).fetchone()
    decisions = conn.execute(
        "SELECT d.code, COALESCE(w.name, d.code) AS name, d.action, d.reason "
        "FROM decisions d LEFT JOIN watchlist w ON d.code = w.code ORDER BY d.id DESC LIMIT 10"
    ).fetchall()
    live = _latest_valid_live(conn)
    conn.close()
    live_obj = None
    if live:
        try:
            p = json.loads(live["payload"])
            date_val = live["date"]
            if not date_val:
                date_val = p.get("date") or "-"
            live_obj = {"ts": live["ts"] or "-", "date": date_val, "quotes": p.get("quotes", [])}
        except Exception:
            live_obj = None
    updated = "-"
    if live:
        updated = live["ts"]
    elif run:
        updated = run["finished_at"] or "-"
    return {"accounts": accounts, "tx": tx, "run": run, "decisions": decisions,
            "live": live_obj, "updated": updated}


def _read_allowed() -> bool:
    """读取鉴权（问题 #6）。

    若服务端配置了 ASTOCK_READ_TOKEN，则所有读取接口都要求携带该令牌
    （Header `X-Auth-Token` 或 URL 参数 `?token=`），否则 401。
    未配置则维持公开——便于本地开发；**接入真实持仓前必须配置**。

    这样做的原因：控制台会展示账户资金与持仓。若不设读取鉴权，
    任何知道 URL 的人都能看到持仓明细。
    """
    if not READ_TOKEN:
        return True
    supplied = (request.headers.get("X-Auth-Token")
                or request.args.get("token")
                or "")
    return _token_eq(supplied, READ_TOKEN)


def _require_read():
    """返回 None 表示通过，否则返回 401 响应。"""
    if _read_allowed():
        return None
    return jsonify({"ok": False, "error": "unauthorized (read)"}), 401


@app.route("/")
def index():
    denied = _require_read()
    if denied:
        return denied
    snap = _snapshot()
    return render_template_string(HTML, mode=DECISION_MODE, **snap)


@app.route("/api/state.json")
def state():
    denied = _require_read()
    if denied:
        return denied
    snap = _snapshot()
    return jsonify({
        "accounts": snap["accounts"],
        "recent_trades": [dict(t) for t in snap["tx"]],
        "run": dict(snap["run"]) if snap["run"] else None,
        "decisions": [dict(d) for d in snap["decisions"]],
        "live": snap["live"], "updated": snap["updated"],
        "note": "纯模拟盘，无真实券商连接",
    })


@app.route("/api/runs.json")
def runs_json():
    """可核验的执行记录列表（问题 #1：不看 lastRunAt，看实际落库回执）。"""
    denied = _require_read()
    if denied:
        return denied
    conn = get_conn()
    rows = conn.execute(
        "SELECT id,run_key,task_name,status,account,started_at,finished_at,market_date,source,note "
        "FROM run_registry ORDER BY id DESC LIMIT 40"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/live_push", methods=["POST"])
def live_push():
    """定时任务（独立环境）跑完后把回执 POST 到这里，落共享账本。

    安全（问题 #5）：**服务端未配置 Token 时拒绝一切写入**。
      之前 `token != INGEST_TOKEN` 在 INGEST_TOKEN 为空串时，
      攻击者发送空 Token 即可通过认证（""==""）→ 任意篡改账户。
      现在改为：未配置 → 503 直接拒绝，不做任何比较。

    校验：拒绝明显无效/测试 payload；done 回执必须带真实行情。
    """
    import time as _t

    # ---- 问题 #5：服务端未配置 Token -> 一律拒绝（绝不允许空 Token 通过） ----
    if not INGEST_TOKEN:
        return jsonify({"ok": False,
                        "error": "server token not configured; writes disabled"}), 503

    token = (request.headers.get("X-Auth-Token")
             or (request.get_json(silent=True) or {}).get("token")
             or "")
    if not token or not _token_eq(token, INGEST_TOKEN):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    r0 = data.get("receipt") or {}
    rk = str(r0.get("run_key") or data.get("run_key") or "")
    if not rk and not data.get("date"):
        return jsonify({"ok": False, "error": "empty payload"}), 400
    if any(k in rk.upper() for k in _BAD_MARKERS):
        return jsonify({"ok": False, "error": "rejected"}), 400
    # done 回执必须携带真实行情（non-empty quotes）
    if (r0.get("status") == "done"
            and not _is_valid_live_payload(json.dumps(data, ensure_ascii=False))):
        return jsonify({"ok": False, "error": "no valid quotes in done receipt"}), 400

    conn = get_conn()
    conn.execute(
        "INSERT INTO live_push(ts, date, payload) VALUES(?,?,?)",
        (_t.strftime("%Y-%m-%d %H:%M:%S"), str(data.get("date") or ""),
         json.dumps(data, ensure_ascii=False)),
    )
    r = data.get("receipt") or {}
    if r.get("run_key"):
        conn.execute(
            """INSERT OR IGNORE INTO run_registry(run_key,task_name,status,account,started_at,finished_at,market_date,source,note,error)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (r.get("run_key"), "daily_job(push)", r.get("status"),
             ",".join(r.get("summary", {}).keys()) if r.get("summary") else None,
             r.get("finished_at"), r.get("finished_at"), r.get("market_date"),
             r.get("source"), f"via push; stale={r.get('stale')}",
             (json.dumps(r.get("errors"), ensure_ascii=False) if r.get("errors") else None)))
        # 仅 done 才同步账户快照，避免失败回执污染账户
        if r.get("status") == "done":
            for acct, s in (r.get("summary") or {}).items():
                conn.execute("UPDATE acct SET cash=? WHERE account=?", (s.get("cash"), acct))
                conn.execute(
                    """INSERT OR REPLACE INTO equity_curve(account,date,cash,market_value,total,pct_change,market_date)
                       VALUES(?,?,?,?,?,?,?)""",
                    (acct, r.get("market_date"), s.get("cash"), s.get("market_value"),
                     s.get("total"), s.get("return_pct"), r.get("market_date")))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "received_run": (data.get("receipt") or {}).get("run_key")})


@app.route("/api/live.json")
def live_json():
    denied = _require_read()
    if denied:
        return denied
    conn = get_conn()
    row = _latest_valid_live(conn)
    conn.close()
    if not row:
        return jsonify(None)
    return jsonify({"ts": row["ts"], "date": row["date"], "payload": json.loads(row["payload"])})


@app.route("/healthz")
def healthz():
    """健康检查。只暴露布尔状态，不泄露配置细节。"""
    return jsonify({"ok": True, "writes_enabled": bool(INGEST_TOKEN)})


@app.route("/report")
def report_view():
    """把最新日报以 HTML 呈现（手机可读）。"""
    denied = _require_read()
    if denied:
        return denied
    import glob
    reps = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "daily_report_*.md")))
    if not reps:
        return "<meta name=viewport content='width=device-width'><p>暂无日报。</p>"
    with open(reps[-1], encoding="utf-8") as f:
        md = f.read()
    import markdown as _md
    body = _md.markdown(md, extensions=["tables"])
    return (f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<style>body{{font-family:-apple-system,sans-serif;padding:14px;max-width:720px;margin:auto;line-height:1.6}}"
            f"table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #ddd;padding:6px}}"
            f"th{{background:#1d3557;color:#fff}}</style>{body}")


if __name__ == "__main__":
    init_db()       # 旧表（兼容已有历史数据）
    init_ledger()   # 共享账本表（acct/positions/trades/equity_curve/run_registry）
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
