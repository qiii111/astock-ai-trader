#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
source_probe.py —— 行情源可达性探测（无凭据、不写库）。

背景：东方财富 push2his 接口对**境外 IP**（GitHub Actions 的美国 Runner）会阻断，
      腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn 目前可稳定访问。
本脚本对每个候选源逐一探测，输出：是否可达 / 延迟 / 最新行情日期。

它的存在意义（问题 #6）：把「数据源是否可用」变成**每天可自动核验的事实**，
而不是等到回测结果异常时才回头怀疑数据。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import marketdata as M

UA = M.UA
PROBE_CODE, PROBE_MARKET = "600519", 1     # 贵州茅台（沪市主板）
TIMEOUT = 15


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body, (time.time() - t0) * 1000.0


def probe(name, url, parser):
    rec = {"source": name, "url_host": urllib.parse.urlparse(url).netloc}
    try:
        body, ms = _get(url)
        rows = parser(body)
        rec.update({"reachable": True, "latency_ms": round(ms, 1),
                    "rows": len(rows), "as_of": rows[-1]["date"] if rows else None})
    except Exception as e:  # noqa: BLE001
        rec.update({"reachable": False, "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return rec


def _parse_tencent(body):
    data = json.loads(body.decode("utf-8", "ignore"))
    sym = "sh" + PROBE_CODE
    node = (data.get("data") or {}).get(sym) or {}
    kl = node.get("qfqday") or node.get("day") or []
    return [{"date": k[0], "close": float(k[2])} for k in kl if len(k) >= 3]


def _parse_eastmoney(body):
    data = json.loads(body.decode("utf-8", "ignore")).get("data") or {}
    out = []
    for k in (data.get("klines") or []):
        p = k.split(",")
        if len(p) >= 3:
            out.append({"date": p[0], "close": float(p[2])})
    return out


def main():
    em_url = M.EASTMONEY + "?" + urllib.parse.urlencode({
        "secid": f"{PROBE_MARKET}.{PROBE_CODE}", "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f59,f60",
        "klt": "101", "fqt": "1", "beg": "20230101", "end": "20500101", "lmt": "20"})
    tx_url = M.TENCENT + "?" + urllib.parse.urlencode(
        {"param": f"sh{PROBE_CODE},day,,,20,qfq"})

    results = [
        probe("eastmoney(push2his)", em_url, _parse_eastmoney),
        probe("tencent(web.ifzq.gtimg.cn)", tx_url, _parse_tencent),
    ]
    usable = [r["source"] for r in results if r.get("reachable") and r.get("rows")]
    out = {"probe_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
           "results": results, "usable_sources": usable,
           "verdict": "OK" if usable else "NO_USABLE_SOURCE"}

    print("SOURCE_PROBE " + json.dumps(out, ensure_ascii=False))
    for r in results:
        flag = "OK " if r.get("reachable") else "FAIL"
        print(f"  [{flag}] {r['source']:<32} "
              f"{'latency=' + str(r.get('latency_ms')) + 'ms' if r.get('reachable') else r.get('error')}"
              f"{' as_of=' + str(r.get('as_of')) if r.get('as_of') else ''}")

    # 只要还有可用源就不失败；全部不可用才让流水线标红
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(main())
