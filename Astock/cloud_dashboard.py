#!/usr/bin/env python3
"""Read-only mobile dashboard for the Neon paper-trading ledger."""
import hmac
import os
from functools import wraps
from flask import Flask, Response, render_template_string, request
import pg_ledger as ledger

app = Flask(__name__)
PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股模拟交易控制台</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#1b2535;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:800px;margin:auto;padding:18px}header{background:#162a48;color:white;padding:22px 18px}
header h1{font-size:22px;margin:0 0 6px}header small{color:#c8d5e9}.card{background:white;border-radius:16px;padding:18px;margin:15px 0;box-shadow:0 2px 12px #162a4810}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.metric{background:#f5f7fb;border-radius:10px;padding:12px}
.label{color:#65748a;font-size:12px}.value{font-size:21px;font-weight:750;margin-top:5px;overflow-wrap:anywhere}
h2{font-size:17px;margin:0 0 13px}.muted{color:#728094;font-size:13px}.pos{color:#ca3547}.neg{color:#19856b}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:10px 5px;border-bottom:1px solid #e9edf3;white-space:nowrap}
.scroller{overflow-x:auto}svg{max-width:100%;height:125px}a{color:#1d5ea8}
</style></head><body><header><h1>A股模拟交易控制台</h1><small>仅虚拟资金 · 不连接券商 · 数据来自 Neon</small></header><main>
{% for a in accounts %}<section class="card"><h2>{{a.label}} <span class="muted">({{a.account}})</span></h2>
<div class="grid"><div class="metric"><div class="label">总资产</div><div class="value">¥{{'%.2f'|format(a.total)}}</div></div>
<div class="metric"><div class="label">累计收益率</div><div class="value {{'pos' if a.pnl>=0 else 'neg'}}">{{'%+.2f'|format(a.pct)}}%</div></div>
<div class="metric"><div class="label">可用现金</div><div class="value">¥{{'%.2f'|format(a.cash)}}</div></div>
<div class="metric"><div class="label">持仓估值</div><div class="value">¥{{'%.2f'|format(a.mv)}}</div></div></div>
<p class="muted">初始虚拟本金 ¥{{'%.0f'|format(a.initial)}} · 估值截至 {{a.last_date or '尚无每日估值'}}{% if a.holdings and not a.last_date %} · 当前持仓估值尚未更新{% endif %}</p>
{% if a.chart %}<svg viewBox="0 0 320 125" preserveAspectRatio="none" aria-label="净值曲线">
<polyline points="{{a.chart}}" fill="none" stroke="#2563eb" stroke-width="2.5" vector-effect="non-scaling-stroke"/></svg>{% else %}<p class="muted">尚无足够的净值记录，运行几天后将显示曲线。</p>{% endif %}
<h2>当前持仓</h2><div class="scroller"><table><tr><th>代码</th><th>名称</th><th>股数</th><th>成本价</th><th>买入日期</th></tr>
{% for p in a.holdings %}<tr><td>{{p.code}}</td><td>{{p.name}}</td><td>{{p.shares}}</td><td>{{'%.2f'|format(p.avg_cost)}}</td><td>{{p.buy_date}}</td></tr>{% else %}<tr><td colspan="5" class="muted">暂无持仓</td></tr>{% endfor %}</table></div>
<h2 style="margin-top:18px">最近成交</h2><div class="scroller"><table><tr><th>日期</th><th>方向</th><th>股票</th><th>股数</th><th>成交价</th><th>费用</th></tr>
{% for t in a.trades %}<tr><td>{{t.date}}</td><td>{{t.action}}</td><td>{{t.name}}</td><td>{{t.shares}}</td><td>{{'%.2f'|format(t.price)}}</td><td>{{'%.2f'|format(t.cost_total or 0)}}</td></tr>{% else %}<tr><td colspan="6" class="muted">暂无模拟成交</td></tr>{% endfor %}</table></div></section>{% endfor %}
<section class="card"><h2>最近自动任务</h2><div class="scroller"><table><tr><th>开始时间</th><th>状态</th><th>行情日期</th><th>备注</th></tr>
{% for r in runs %}<tr><td>{{r.started_at}}</td><td>{{r.status}}</td><td>{{r.market_date or '-'}}</td><td>{{(r.note or r.error or '-')[:100]}}</td></tr>{% else %}<tr><td colspan="4" class="muted">尚无正式执行记录；Dry Run 不写数据库</td></tr>{% endfor %}</table></div></section>
<p class="muted">刷新页面获取最新账本数据。收盘价模拟成交不代表真实可成交价格。</p></main></body></html>"""


def protected(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = os.environ.get("ASTOCK_READ_TOKEN", "")
        if not expected:
            return Response("Dashboard is locked: ASTOCK_READ_TOKEN is not configured.", status=503)
        auth = request.authorization
        if not auth or not hmac.compare_digest(auth.password or "", expected):
            return Response("Authentication required", status=401,
                            headers={"WWW-Authenticate": 'Basic realm="Paper Dashboard"'})
        return fn(*args, **kwargs)
    return wrapper


def chart_points(rows):
    if len(rows) < 2:
        return ""
    vals = [float(r["total"]) for r in rows]
    low, high = min(vals), max(vals)
    span = high - low or 1.0
    return " ".join(f"{10+i*300/(len(vals)-1):.1f},{115-(v-low)*105/span:.1f}" for i,v in enumerate(vals))


@app.get("/health")
def health():
    return {"ok": True, "service": "paper-dashboard"}


@app.get("/")
@protected
def dashboard():
    conn = ledger.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT account,initial,cash,label FROM acct ORDER BY account")
            account_rows = cur.fetchall()
            accounts = []
            for a in account_rows:
                key = a["account"]
                cur.execute("SELECT * FROM positions WHERE account=%s ORDER BY code", (key,))
                holdings = cur.fetchall()
                cur.execute("SELECT * FROM trades WHERE account=%s ORDER BY id DESC LIMIT 25", (key,))
                trades = cur.fetchall()
                cur.execute("SELECT date,total,market_value FROM equity_curve WHERE account=%s ORDER BY date DESC LIMIT 90", (key,))
                equity = list(reversed(cur.fetchall()))
                # Never present stale or cost-based holdings as today's marked-to-market value.
                last = equity[-1] if equity else None
                cash = float(a["cash"])
                mv = float(last["market_value"]) if last else 0.0
                # The last recorded valuation can predate later trades; indicate that clearly.
                total = cash + mv if last else cash
                initial = float(a["initial"])
                accounts.append(dict(**a, cash=cash, initial=initial, mv=mv,
                                     total=total, pnl=total-initial,
                                     pct=(total/initial-1)*100 if initial else 0,
                                     holdings=holdings, trades=trades,
                                     last_date=last["date"] if last else None,
                                     chart=chart_points(equity)))
            cur.execute("SELECT started_at,status,market_date,note,error FROM run_registry ORDER BY id DESC LIMIT 15")
            runs = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    response = Response(render_template_string(PAGE, accounts=accounts, runs=runs))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
