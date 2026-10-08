#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成每日策略报告（自包含，可离线运行 / 可被定时任务调用）。

输出 reports/daily_report_YYYY-MM-DD.md，包含：
  1. 双账户状态（2.2万 实盘对标 / 10万 对照）
  2. 今日信号与模拟成交
  3. 策略对比（MA20/60 vs MACD）：全样本 + 样本外
  4. 数据源健康与数据时间戳
  5. 风险提示（过拟合、成本、样本外衰减）
"""
import json
import os
import sys
import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
REPORTS = os.path.join(BASE, "reports")


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    os.makedirs(REPORTS, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    lines = [f"# A股 AI 模拟交易日报 · {today}", ""]
    lines.append("> 纯模拟盘，与真实券商零连接。用于策略验证，辅助真实投资决策。\n")

    # 双账户
    lines.append("## 一、账户状态\n")
    lines.append("| 账户 | 初始 | 总资产 | 收益率 | 持仓 | 成交 | 备注 |")
    lines.append("|---|---|---|---|---|---|---|")
    try:
        import sqlite3
        from ledger import ACCOUNTS
        conn = sqlite3.connect(os.path.join(BASE, "data", "astock.db"))
        conn.row_factory = sqlite3.Row
        for acct, meta in ACCOUNTS.items():
            row = conn.execute("SELECT cash FROM acct WHERE account=?", (acct,)).fetchone()
            cash = row["cash"] if row else meta["initial"]
            mv = 0.0
            holds = conn.execute("SELECT * FROM positions WHERE account=?", (acct,)).fetchall()
            n_tr = conn.execute("SELECT COUNT(*) c FROM trades WHERE account=?", (acct,)).fetchone()["c"]
            total = cash + mv
            ret = (total / meta["initial"] - 1) * 100
            lines.append(f"| {meta['label']} | {meta['initial']:.0f} | {total:.2f} | {ret:+.2f}% | "
                         f"{len(holds)} | {n_tr} | 模拟 |")
        conn.close()
    except Exception as e:  # noqa: BLE001
        lines.append(f"（账户数据读取失败：{e}）")
    lines.append("")

    # 最近执行回执
    ev = load(os.path.join(BASE, "data", "run_receipt.json"))
    if ev:
        lines.append("## 二、最近一次定时执行回执\n")
        lines.append(f"- run_key: `{ev.get('run_key')}`")
        lines.append(f"- 状态: **{ev.get('status')}**")
        lines.append(f"- 行情日期: {ev.get('market_date')} · 数据源: {ev.get('source')} · stale={ev.get('stale')}")
        lines.append(f"- 完成时间: {ev.get('finished_at')}")
        lines.append("")

    # 数据源健康
    sh = load(os.path.join(BASE, "data", "source_health.json"))
    if sh:
        lines.append("## 三、数据源健康\n")
        lines.append("| 数据源 | 成功次数 | 平均延迟(s) |")
        lines.append("|---|---|---|")
        for k, v in (sh.get("sources") or {}).items():
            lines.append(f"| {k} | {v.get('ok')} | {v.get('avg_latency_s')} |")
        lines.append("")

    # 策略对比（若已生成）
    bt = load(os.path.join(BASE, "data", "backtest_compare.json"))
    if bt:
        lines.append("## 四、策略对比与样本外验证\n")
        lines.append(f"数据区间：{bt['data_range'][0]} ~ {bt['data_range'][1]}（{bt['days']} 交易日）\n")
        for period, res in bt["results"].items():
            b = res.get("_benchmark") or {}
            lines.append(f"**{period}**（基准买入持有 {b.get('return_pct')}%）\n")
            lines.append("| 策略 | 收益 | 年化 | 最大回撤 | 夏普 | 交易 | 费用 | 超额 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for s in ("ma20_60", "macd"):
                r = res.get(s)
                if not r:
                    continue
                lines.append(f"| {s} | {r['return_pct']}% | {r['annual_pct']}% | {r['max_drawdown_pct']}% | "
                             f"{r['sharpe']} | {r['trades']} | {r['fees']} | {r.get('excess_vs_benchmark_pct')}% |")
            lines.append("")
        lines.append("> ⚠️ **过拟合警示**：若某策略在样本内收益远高于样本外，说明其优势可能来自过拟合，"
                     "不应据此上真实资金。请以「样本外 + 扣除成本后」的表现作为选择依据。\n")

    out = os.path.join(REPORTS, f"daily_report_{today}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("REPORT_SAVED " + out)
    print("\n".join(lines[:40]))


if __name__ == "__main__":
    main()
