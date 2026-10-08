#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全局配置：初始资金、关注标的、A 股交易规则、策略参数。
所有金额单位：人民币元（CNY）。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "astock.db")

# 初始虚拟资金（严格模拟，绝不接入真实券商）
INITIAL_CAPITAL = 100000.0

# 关注标的：(代码, 名称, 市场)  市场 1=上交所 0=深交所
WATCHLIST = [
    ("600519", "贵州茅台", 1),
    ("600036", "招商银行", 1),
    ("000001", "平安银行", 0),
    ("300750", "宁德时代", 0),
    ("000858", "五粮液", 0),
    ("601318", "中国平安", 1),
]

# ---------- A 股交易规则（模拟撮合必须遵循） ----------
COMMISSION_RATE = 0.00025   # 佣金比例（万 2.5）
COMMISSION_MIN = 5.0        # 单笔最低佣金（元）
STAMP_DUTY_RATE = 0.0005    # 卖出印花税（万 5）
LOT_SIZE = 100              # 1 手 = 100 股（整手交易）
TRANSFER_FEE_RATE = 0.00001  # 过户费（约万 0.1，仅沪市，这里统一微收）

# 涨跌停幅度：主板 10%，科创板(688)/创业板(300,301) 20%
LIMIT_MAIN = 0.10
LIMIT_STAR = 0.20
STAR_PREFIXES = ("688",)
CHINEXT_PREFIXES = ("300", "301")

# 单票最大仓位占比（风控）
MAX_POSITION_RATIO = 0.40

# ---------- 策略参数 ----------
MA_FAST = 20
MA_SLOW = 60
HISTORY_BEG = "20240101"     # 回测/初始化拉取起点

# ---------- 决策模式接口（AI 模式预留，暂不启用） ----------
# "rule" = 规则模式（当前启用）；"ai" = AI 决策模式（接口预留）
DECISION_MODE = "rule"

# ---------- 写入接口鉴权（防止他人篡改模拟账户） ----------
# 定时任务上报回执时必须携带此令牌；仅写入接口校验，读取接口公开。
# 注意：这是轻量防护，用于阻止"随手写入"，不能替代真正的身份认证。
import os as _os
# 写入令牌：**必须**通过环境变量注入，禁止在仓库中留存任何令牌。
# 未配置时 /api/live_push 会直接返回 503 拒绝一切写入（见 app.py，问题 #5）。
INGEST_TOKEN = _os.environ.get("ASTOCK_INGEST_TOKEN", "")

# 读取令牌：配置后，控制台所有读取接口都要求携带该令牌（问题 #6）。
# 未配置则读取公开——**仅适用于本地开发**；接入真实持仓前必须配置。
READ_TOKEN = _os.environ.get("ASTOCK_READ_TOKEN", "")

# 数据新鲜度阈值（天）：超过则在控制台标记"数据过期"
STALE_DAYS = 5
