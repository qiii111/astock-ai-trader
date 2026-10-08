#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行情数据层（仅标准库，可在定时任务的隔离环境中独立运行）。

解决东方财富接口不稳定：
  1. 多数据源冗余：东财 push2his -> 腾讯 ifzq.gtimg.cn（自动降级）
  2. 指数退避重试 + 每次请求独立超时
  3. 磁盘缓存（cache/ 目录）：任一源成功即落盘；全部源失败则回退最近缓存
  4. 数据时间戳验证：返回 as_of（该根 K 线的日期）与 fetched_at（抓取时刻），
     并校验 as_of 不得晚于当前交易日；缓存回退时标记 stale=True
"""
import json
import os
import time
import datetime

try:
    import urllib.request as _url
    import urllib.parse as _parse
except ImportError:  # pragma: no cover
    raise

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")

TIMEOUT = 12
RETRIES = 4

EASTMONEY = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _secid(code: str, market: int) -> str:
    return f"{market}.{code}"


def _get(url: str, timeout: int = TIMEOUT) -> str:
    req = _url.Request(url, headers=UA)
    with _url.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _fetch_eastmoney(code: str, market: int, limit: int = 260):
    params = {
        "secid": _secid(code, market),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f59,f60",
        "klt": "101", "fqt": "1", "beg": "20230101", "end": "20500101", "lmt": str(limit),
    }
    url = EASTMONEY + "?" + _parse.urlencode(params)
    text = _get(url)
    data = json.loads(text).get("data") or {}
    klines = data.get("klines") or []
    rows = []
    for k in klines:
        p = k.split(",")
        if len(p) < 9:
            continue
        rows.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "volume": float(p[5]), "amount": float(p[6]),
            "pct_change": float(p[7]), "pre_close": float(p[8]),
        })
    if not rows:
        raise ValueError("eastmoney empty")
    return rows


def _fetch_tencent(code: str, market: int, limit: int = 260):
    # 腾讯：沪市前缀 sh，深市前缀 sz
    sym = ("sh" if market == 1 else "sz") + code
    params = {"param": f"{sym},day,,,{limit},qfq"}
    url = TENCENT + "?" + _parse.urlencode(params)
    text = _get(url)
    node = (json.loads(text).get("data") or {}).get(sym) or {}
    klines = node.get("qfqday") or node.get("day") or []
    rows = []
    prev = None
    for k in klines:
        # [date, open, close, high, low, volume, ...]
        if len(k) < 5:
            continue
        o, c, h, l = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        vol = float(k[5]) if len(k) > 5 else 0.0
        pct = ((c / prev - 1) * 100) if prev else 0.0
        rows.append({
            "date": k[0], "open": o, "close": c, "high": h, "low": l,
            "volume": vol, "amount": 0.0,
            "pct_change": round(pct, 3), "pre_close": prev or o,
        })
        prev = c
    if not rows:
        raise ValueError("tencent empty")
    return rows


SOURCES = [("eastmoney", _fetch_eastmoney), ("tencent", _fetch_tencent)]


def _cache_path(code: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{code}_daily.json")


def _write_cache(code: str, rows: list, source: str):
    try:
        with open(_cache_path(code), "w", encoding="utf-8") as f:
            json.dump({"source": source, "fetched_at": _today(), "rows": rows}, f, ensure_ascii=False)
    except OSError:
        pass


def _read_cache(code: str):
    try:
        with open(_cache_path(code), "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj.get("rows") or [], obj.get("fetched_at")
    except (OSError, ValueError):
        return [], None


def fetch_daily(code: str, market: int, limit: int = 260, use_cache: bool = True):
    """带多源冗余/重试/缓存/时间戳验证的日线抓取。

    返回 dict: {code, rows, source, as_of, fetched_at, stale, errors}
    - source='cache' 表示所有在线源失败、回退到磁盘缓存
    - stale=True  表示数据可能过期
    """
    errors = []
    for name, fn in SOURCES:
        for attempt in range(RETRIES):
            try:
                rows = fn(code, market, limit)
                rows.sort(key=lambda r: r["date"])
                as_of = rows[-1]["date"]
                # 数据时间戳验证：不得晚于今天
                if as_of > _today():
                    raise ValueError(f"future quote {as_of}")
                _write_cache(code, rows, name)
                return {"code": code, "rows": rows, "source": name,
                        "as_of": as_of, "fetched_at": _today(), "stale": False,
                        "errors": errors}
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}#{attempt}:{type(e).__name__}")
                time.sleep(0.4 * (attempt + 1))
        time.sleep(0.5)
    # 全部在线源失败 -> 缓存回退
    if use_cache:
        rows, fetched_at = _read_cache(code)
        if rows:
            as_of = rows[-1]["date"]
            return {"code": code, "rows": rows, "source": "cache",
                    "as_of": as_of, "fetched_at": fetched_at, "stale": True,
                    "errors": errors}
    return {"code": code, "rows": [], "source": None, "as_of": None,
            "fetched_at": None, "stale": True, "errors": errors}
