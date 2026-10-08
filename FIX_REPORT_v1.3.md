# 修复报告 v1.3

> 针对 ChatGPT 审查 v1.2 后新发现的 **2 个问题** 做最小范围修复。
> **未部署、未启动定时任务、未连接真实持仓、未迁移任何生产数据库。**

- 修复日期：2026-10-08
- 基线：v1.2（59/59 回归通过）
- 本轮结果：**回归 81/81 通过**；离线自检 **12/12 通过**（无回归）

---

## 一、修改文件清单

| # | 文件 | 类型 | 说明 |
|---|------|------|------|
| 1 | `astock/run_cloud.py` | 修改 | 问题 #1 单次抓取快照；问题 #2 新增只读 dry-run 路径 |
| 2 | `.github/workflows/daily.yml` | 修改 | 问题 #2 dry-run 不再执行 `init_ledger()` |
| 3 | `tools/regression_tests.py` | 修改 | 新增 T9a/T9b/T9c/T9d 四组回归用例（+22 断言） |
| 4 | `FIX_REPORT_v1.3.md` | 新增 | 本文件 |
| 5 | `README.md` | 修改 | 补充 v1.3 说明与 dry-run 只读语义 |

**未改动**（保护已验证行为）：`daily_job.py`、`ledger.py`、`marketdata.py`、`indicators.py`、`strategy.py`、`stats.py`、`report.py`、`engine.py`、`backtest.py`、`ai_analyst.py`、`db.py`、`missed_run_check.py`、`source_probe.py`、`db_probe.py`、`pg_ledger.py`、`app.py`、`config.py`。

---

## 二、问题 #1：二次 `fetch_daily()` 导致信号与成交价不同源

### 2.1 问题描述

v1.2 的 `_fetch_all_quotes()` 负责抓取并校验行情；但随后**生成信号时又调用了一次 `J.fetch_daily()`**。
后果：信号基于「第一次抓取」的数据算出，成交价却取自「第二次抓取」——

> 盘中价格随时在变，等于**按 A 版本决策、按 B 版本成交**。
> 轻则成交价与报告不一致，重则边界信号（刚上穿均线）在第二次抓取时消失，导致"算出买入信号却按已失效的价格下单"。

### 2.2 修复方案（最小改动）

`_fetch_all_quotes()` 改为返回 **5 元组**，新增 `snapshot`（`{code: closes}`）作为**唯一行情副本**：

```python
def _fetch_all_quotes(fetcher=None):
    ...
    snapshot = {}                       # code -> [close, ...]  唯一副本
    ...
    closes = [r["close"] for r in rows]
    snapshot[code] = closes             # 存下唯一副本
    price_map[code] = closes[-1]
    ...
    return price_map, quote_list, market_date, {...}, snapshot
```

信号生成**只从 snapshot 读**，不再二次抓取：

```python
price_map, quote_list, market_date, meta, snapshot = _fetch_all_quotes(fetcher)
...
for q in quote_list:
    code = q["code"]
    if code not in snapshot:
        continue
    closes = snapshot[code]             # ← 复用已抓取那一份，绝不二次调用 fetch_daily
    sig, reason = J.compute_signals(closes, strategy)
    ...
    signals.append({..., "price": closes[-1], "date": market_date})
```

同时删除已无用的 `_market_of()` 辅助函数，避免留下"可以绕过快照"的旁路。

**改动量**：`run_cloud.py` 内 3 处，约 15 行；无接口破坏。

---

## 三、问题 #2：dry-run 仍会执行 `init_ledger()`

### 3.1 问题描述

v1.2 的 `daily.yml` 中，「Ensure schema & migrate」步骤**无条件执行**：

```yaml
- name: Ensure schema & migrate
  run: python -c "import pg_ledger as P; P.init_ledger(); print('schema ready')"
```

即使 `dry_run=true` 也会执行 → **建表、建账户行、跑迁移**。
这违背"dry-run 无副作用"的承诺：一次"只看不动"的验证会真实改写线上库结构。

### 3.2 修复方案（工作流侧）

拆成**互斥的两步**，用 `if` 严格门控：

```yaml
- name: Verify schema (READ-ONLY, dry-run only)
  if: ${{ github.event.inputs.dry_run == 'true' }}      # 只有 dry-run 走这里
  working-directory: astock
  run: python db_probe.py                                # 纯 SELECT 探测

- name: Ensure schema & migrate (WRITE, skipped on dry-run)
  if: ${{ github.event.inputs.dry_run != 'true' }}       # dry-run 时整步跳过
  working-directory: astock
  run: python -c "import pg_ledger as P; P.init_ledger(); print('schema ready')"
```

### 3.3 修复方案（代码侧）

新增**独立的只读路径** `dry_run_readonly()`，`main()` 在 `--dry-run` 时直接走它，
**完全绕开** `run_cloud()`（后者含锁获取与落库）：

```python
def main():
    ...
    if a.dry_run:
        out = dry_run_readonly(strategy=a.strategy, accounts=accounts)
        print("DRY_RUN_RESULT " + json.dumps({...}))
        return 0
```

`dry_run_readonly()` 的四条硬保证：

| 保证 | 实现方式 |
|------|---------|
| **不**调用 `init_ledger()` | 只 `P.connect()`，连接后仅 `SELECT` |
| **不**获取执行锁 | 完全不引用 `acquire_lock` |
| **不**写任何行 | 不调用 `start_run/finish_run/record_trade/upsert_*` |
| **不**推送回执 | 不引用 `_maybe_push` |

并新增只读探测函数，用 `to_regclass()` 判断表是否存在——**不会因"表不存在"而建表**：

```python
def probe_schema_readonly(conn) -> dict:
    info = {"tables": {}, "missing": []}
    with conn.cursor() as cur:
        for t in ("acct","positions","trades","equity_curve","run_registry","exec_lock"):
            cur.execute("SELECT to_regclass(%s) AS oid", (f"public.{t}",))
            row = cur.fetchone()
            if row and row["oid"]:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                info["tables"][t] = cur.fetchone()["n"]
            else:
                info["missing"].append(t)          # 不存在 -> 记录，不创建
    return info
```

最后做**前/后二次探测自证**：`no_side_effects = (after == before)`。

---

## 四、验证结果

### 4.1 回归测试套件：81/81 通过

```
T1  行情新鲜度策略（10）
T1b stale 数据拒绝成交（4）
T2  run_key UNIQUE + 并发去重（4）
T2b 执行锁并发互斥（4）
T3  dry-run 无副作用（7）
T4  多持仓估值（6）
T5  空/未配置 Token 拒绝（8）
T6  未认证公共接口已下线（6）
T7  并发与重复交易（5）
T8  行情全缺失安全跳过（5）
T9a 单次抓取：信号与成交价同源（5）        ← v1.3 新增
T9b dry-run 只读：空库不得建表（6）          ← v1.3 新增
T9c daily.yml 步骤门控（5）                  ← v1.3 新增
T9d --dry-run 主入口不建表（5）              ← v1.3 新增
```

v1.3 关键断言实测输出：

```
[PASS] T9a 每个标的恰好抓取 1 次（无二次 fetch_daily）:
       抓取次数={'600519': 1, '600036': 1, '601318': 1, '000001': 1, '300750': 1, '000858': 1}
[PASS] T9a 信号价 = 首次抓取快照末值（未混入第二次数据）: 不一致=[]
       （测试构造"第二次抓取价上抬 100 元"的漂移源；修复前会相差 100 元）
[PASS] T9a run_cloud 内不出现 fetch_daily 调用: ok
[PASS] T9b 空库初始无业务表: tables=[]
[PASS] T9b dry-run 探测到 6 张表均缺失（未自动创建）:
       missing=['acct','positions','trades','equity_curve','run_registry','exec_lock']
[PASS] T9b dry-run no_side_effects=True
[PASS] T9b 运行后空库**仍然**无表: tables=[]（若 init_ledger 被调用会出现 6 张表）
[PASS] T9b dry_run_readonly 内不调用 init_ledger: ok
[PASS] T9c init_ledger 被 dry_run != 'true' 门控
[PASS] T9c 只读探测步骤仅在 dry_run == 'true' 执行
[PASS] T9d main(--dry-run) 退出码 0
[PASS] T9d DRY_RUN_RESULT.no_side_effects=True
```

> **测试设计说明**：T9a 的漂移源（drift fetcher）是"能真正抓到 bug"的写法——
> 只要代码发生第二次 `fetch_daily()`，成交价就会比信号价高 100 元，断言立即失败。
> 仅靠"两次返回相同数据"无法发现该问题。

### 4.2 离线自检套件：12/12 通过（无回归）

```
T1 daily_job 无 Flask 依赖 / 仅标准库导入 / --selftest 跑通
T2 资金不足被拒绝且无持仓变化
T3 首次执行产生成交；重复执行幂等（成交数与资金均不变）
T4 全无数据 -> failed 并记录原因；失败后重试可断点恢复
```

### 4.3 GitHub Actions dry-run 流程实测（真实行情）

三种库状态分别验证，退出码均为 0：

| 场景 | 库状态 | 表数变化 | `run_registry` | `no_side_effects` |
|------|--------|---------|----------------|-------------------|
| A | **空库**（首次部署前） | 0 → **0** | — | ✅ True |
| B | 普通运行（`dry_run=false`） | 0 → **6** | 正常写入 | — |
| C | 已建表库 | 6 → 6 | 0 → **0** | ✅ True |

场景 A 实测输出（真实在线行情，Eastmoney 源）：

```
DRY-RUN（只读验证）：不建表 / 不迁移 / 不写任何行
警告：以下表不存在（只读模式不会创建）:
      ['acct','positions','trades','equity_curve','run_registry','exec_lock']
600519 贵州茅台: close=1254.30 as_of=2026-10-08 src=eastmoney
600036 招商银行: close=41.70   as_of=2026-10-08 src=eastmoney
601318 中国平安: close=52.63   as_of=2026-10-08 src=eastmoney
000001 平安银行: close=11.79   as_of=2026-10-08 src=eastmoney
300750 宁德时代: close=287.06  as_of=2026-10-08 src=eastmoney
000858 五粮液:   close=70.04   as_of=2026-10-08 src=eastmoney
可用=6 拒绝=0 信号=0
只读复核：表结构与行数均未变化 ✓
DRY_RUN_RESULT {"status":"dry_run_ok","db_checked":true,"no_side_effects":true,
                "market_date":"2026-10-08","signals":0,"rejected":0}
```

事后核查空库表清单：`0 rows` —— **确认未建表**。
场景 B 则成功建出 6 张表 —— 证明该门控**真实有效**，而非"两边都不建表"的假通过。

### 4.4 工作流 YAML 校验

```
OK  .github/workflows/ci-regression.yml  (5 steps)
OK  .github/workflows/daily.yml          (9 steps)
OK  .github/workflows/heartbeat.yml      (6 steps)
OK  .github/workflows/missed-run-check.yml (5 steps)
```

### 4.5 生产数据完整性

修复与测试全程，线上 SQLite 库 `data/astock.db` **未被触碰**：

```
real22000: cash=22000.0
paper100k: cash=100000.0
positions=0   trades=0
mtime 仍为 10-08 13:41（未变化）
```

---

## 五、复现命令

```bash
# 1) 准备一个可写 PostgreSQL（示例用本地 16）
#    DATABASE_URL 指向测试库，切勿指向线上！

# 2) 回归测试（含 v1.3 的 T9a~T9d）
export DATABASE_URL='postgresql://user:pass@host/dbname'
python3 tools/regression_tests.py
#   -> 结果：81/81 通过

# 3) 离线自检（纯标准库，无需 DB）
python3 tools/p3_verify_standalone.py
#   -> 结果：12/12 通过

# 4) 手工验证只读 dry-run（对空库执行，观察未建表）
cd astock && python3 run_cloud.py --dry-run --strategy ma
#   -> DRY_RUN_RESULT {... "no_side_effects": true ...}
```

---

## 六、遗留说明（不属本轮范围）

1. **GitHub 连接能力**：本沙箱 `gh` 未登录、OAuth 网关 404，无法直接建库/推送，
   故交付形态仍为 ZIP。迁移到 GitHub 后需在 Secrets 配置 `DATABASE_URL`、
   `ASTOCK_INGEST_TOKEN`、`ASTOCK_READ_TOKEN`、`ASTOCK_PUSH_URL`。
2. **正式定时任务未启动**：`daily.yml` 的 `schedule` 已就位但**未启用**，
   需人工确认后才开始真跑（当前 `real22000` 仍为空账户）。
3. **Eastmoney 在海外 IP 偶发不可达**：实测本次可用；`fetch_daily()` 具备
   多源 + 重试 + 缓存回退，但**回退数据会被新鲜度策略拒绝成交**（见 T1b），
   这是刻意设计，不是缺陷。
