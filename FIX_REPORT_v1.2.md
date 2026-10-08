# 修复报告 —— 部署阻断问题（v1.2）

> 本轮仅修复代码与补充测试。**未连接真实持仓、未迁移数据库、未启动定时任务、未部署线上系统。**

修复日期：2026-10-08
基线版本：v1.1（26 个文件）
修复版本：v1.2
测试结论：**回归套件 59/59 通过 · 离线套件 12/12 通过**

---

## 一、逐项修复对照

### 问题 #1 · stale / 过期行情不得用于成交

**原缺陷**：`run_cloud.py` 只把 `stale` 写进回执的 `note` 字段，然后**继续执行成交**。
行情过期三个月也照买不误。

**修复**：新增 `check_quote_freshness()`，在成交前对每个标的做四项判定，
任一不满足即 **REJECTED**（不成交、计入回执、写失败原因）：

| 判定 | 拒绝条件 |
|---|---|
| 缓存回退 | `stale=True`（在线源全失败） |
| 无数据 | `rows` 为空 |
| 时间戳异常 | `as_of` 晚于今天（未来数据） |
| **过期** | `as_of` 早于今天超过 `ASTOCK_MAX_QUOTE_AGE_DAYS`（默认 7 天） |

阈值取 7 天的原因：需覆盖 A 股周末与法定长假，同时能拦住"缓存停在数月前"的真实故障。

**全部标的都不可用**时：`status=failed`、零成交、`run_registry` 记录明确 error、退出码 1。

修改文件：`astock/run_cloud.py`
覆盖测试：`T1`（10 个判定用例）、`T1b`（stale 全量拒绝）、`T8`（全缺失）

---

### 问题 #2 · run_key 缺数据库级唯一性 + 无并发锁

**原缺陷**：两处竞态
1. `run_registry.run_key` 无 UNIQUE 约束 —— 重复执行会产生多条记录
2. 判重靠 `SELECT ... WHERE run_key LIKE '...'` —— 两个并发进程会**同时读到空结果**然后都执行

**修复**（三层防线，逐层收窄）：

| 层 | 机制 | 作用 |
|---|---|---|
| 1 | `exec_lock` 表（`lock_key` 主键） | `INSERT ... ON CONFLICT DO NOTHING` 原子抢锁，同一时刻只有一个执行者 |
| 2 | `run_registry.run_key UNIQUE` | 即使抢锁逻辑被绕过，数据库仍拒绝重复插入 |
| 3 | `trades.trade_key UNIQUE` | 最后兜底，保证单笔成交绝不重复 |

`acquire_lock()` 支持**过期锁回收**（`expires_at < now()` 时原子抢占），
应对"上一作业被超时杀掉没释放锁"的情况。锁默认 TTL 3600 秒，`finally` 中必定释放。

**旧库迁移**：`init_ledger()` 会自动检测并补约束。若旧库已有重复 `run_key`，
**不删除**，而是归档到 `run_registry_dupes`（保留审计痕迹），仅保留 id 最小的一条。

修改文件：`astock/pg_ledger.py`、`astock/run_cloud.py`
新增表：`exec_lock`
覆盖测试：`T2`（UNIQUE 约束 + 6 线程并发）、`T2b`（锁获取/释放/8 线程并发）、`T7`（5 并发 run_cloud）

迁移验证（真实 PostgreSQL 16）：

```
== 迁移前 ==   run_key 唯一索引: （无）   行数: 3
== 迁移后 ==   run_key 唯一索引: uq_run_registry_run_key
              run_registry 行数: 2  (重复的 1 条已归档)
              归档到 run_registry_dupes: 1
              exec_lock 表已建: True
```

---

### 问题 #3 · `--dry-run` 有副作用

**原缺陷**：dry-run 仍会
1. 调 `acquire_lock()` 抢锁（阻塞真实任务）
2. 写 `run_registry`（status=done 的行会影响漏执行看门狗判定）
3. 写 `equity_curve` 净值快照

**修复**：`dry=True` 时
- 跳过抢锁（日志明示）
- 跳过 `start_run` / `finish_run`
- 跳过 `upsert_equity`
- 跳过回执推送
- 返回 `receipt["dry"] = True`

修改文件：`astock/run_cloud.py`
覆盖测试：`T3`（逐表比对前后行数 + 资金比对 + 断言无 `dry:` 前缀记录）

---

### 问题 #4 · 用当前标的价格估值全部持仓

**原缺陷**：
```python
total_assets = cash + sum(float(p["shares"]) * price for p in pos_list)
#                                        ^^^^^ 当前交易标的的价格，套用到所有持仓
```
后果：若账户持有茅台（¥1250）但本次信号标的是平安银行（¥11.78），
总资产被严重低估 → 40% 仓位上限被算错 → 可能超额买入或漏买。

**修复**：新增 `pg_ledger.value_positions()`，**逐持仓取各自最新有效价**：

```python
mv, missing, used = P.value_positions(conn, account, price_map)  # price_map 来自本次抓取的全部新鲜行情
```

**缺价处理**（关键）：缺价持仓**不计入市值，也不允许用 `avg_cost` 冒充**。
买入前若存在缺价持仓，直接拒绝下单并说明原因：

```
拒绝下单：持仓 B002/C003 缺最新价，总资产无法可靠估算
```

净值快照同样改用 `value_positions()`，并在 `summary` 中输出 `unvalued_positions` 列表。

修改文件：`astock/pg_ledger.py`（新增 `value_positions` / `PriceUnavailable`）、`astock/run_cloud.py`
覆盖测试：`T4`（4 个场景：各自取价 / 缺价不计市值 / 抛异常 / 拒绝下单）

---

### 问题 #5 · 空 Token 可通过认证

**原缺陷**：
```python
token = request.headers.get("X-Auth-Token") or (request.get_json(...) or {}).get("token")
if token != INGEST_TOKEN:      # INGEST_TOKEN 未配置 == "" 时，token 传 "" 即通过
    return 401
```
`"" == ""` 为真 → **服务端未配置 Token 时，任何人发空 Token 都能写入账户**。

**修复**（双重加固）：
1. **未配置即拒绝**：`if not INGEST_TOKEN: return 503`，不做任何比较
2. **空值显式拦截**：`if not token or not _token_eq(token, INGEST_TOKEN): return 401`
3. 改用 `hmac.compare_digest()` 常量时间比较，防时序攻击
4. `/healthz` 只暴露 `writes_enabled` 布尔值

修改文件：`astock/app.py`、`astock/config.py`
覆盖测试：`T5`（3 项静态 + 5 项动态，含 Flask test_client 实际请求）

---

### 问题 #6 · 未认证的公共代码分发接口

**原缺陷**：两个 `GET` 端点无需任何认证即可拉取源码：
- `/api/job_script` → 返回 `daily_job.py` 全文
- `/api/runner` → 返回完整自包含执行包（含内联的 `daily_job.py`）

任何人访问 URL 即可获得交易逻辑源码，属信息泄露。且这套"HTTP 唤醒沙箱"机制
已被 GitHub Actions 直连架构取代，属于**遗留死代码**。

**修复**：
1. **删除** `/api/job_script` 与 `/api/runner` 两个路由
2. 移除 `app.py` 中的源码内联逻辑
3. 顺带排查其余端点，为读取接口补上鉴权：

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `/` `/api/state.json` `/api/runs.json` `/api/live.json` `/report` | `_require_read()` | 配置 `ASTOCK_READ_TOKEN` 后必须带令牌，否则 401 |
| `/api/live_push` | Token + 未配置即 503 | 写入 |
| `/healthz` | 无（只返回布尔值） | 探活 |

路由从 9 个收敛到 7 个。

修改文件：`astock/app.py`、`astock/config.py`、`.env.example`
覆盖测试：`T6`（5 项断言，含路由枚举与 healthz 泄露检查）

---

### 问题 #7 · 增加自动化回归测试

新增 `tools/regression_tests.py`，**59 项断言**，覆盖：

| 组 | 内容 | 用例数 |
|---|---|---|
| T1 | 过期/stale/未来日期/无数据 行情判定 | 10 |
| T1b | stale 全量数据 → 零成交 + 记录失败 | 4 |
| T2 | run_key UNIQUE + 6 线程并发 | 4 |
| T2b | 执行锁获取/释放/8 线程并发 | 4 |
| T3 | dry-run 无副作用（4 张表 + 资金） | 7 |
| T4 | 多持仓估值 4 场景 | 6 |
| T5 | 空 Token 拒绝（静态 + Flask 动态） | 8 |
| T6 | 已下线接口 + 鉴权覆盖 | 6 |
| T7 | 并发执行 + 重复交易不重复扣款 | 5 |
| T8 | 行情全缺失安全跳过并上报 | 5 |

新增 `.github/workflows/ci-regression.yml`：push/PR 时自动跑，
使用 GitHub 托管的**临时 PostgreSQL service**，绝不接触 Neon 生产库。

修改文件：新增 `tools/regression_tests.py`、`.github/workflows/ci-regression.yml`
另：`.github/workflows/daily.yml` 增加「幂等建表 + 迁移」步骤

---

## 二、测试结果

### 回归套件（59/59）

```
T1  行情新鲜度策略 ....................... 10/10
T1b stale 不产生成交 .................... 4/4
T2  run_key 数据库唯一性 ................. 4/4
T2b 并发执行锁 .......................... 4/4
T3  dry-run 无副作用 .................... 7/7
T4  多持仓估值 .......................... 6/6
T5  空 Token 拒绝 ....................... 8/8
T6  已下线接口 + 鉴权 ................... 6/6
T7  并发 + 重复交易 ..................... 5/5
T8  行情全缺失 .......................... 5/5
────────────────────────────────────────
合计 59/59 通过
```

### 关键测试原始输出

```
[T2] 6 线程并发抢同一 run_key -> 仅 1 成功
     success=1, results=[True, False, False, False, False, False]

[T2b] 8 线程并发抢锁 -> 仅 1 成功   success=1

[T3] dry-run 未写 run_registry: 0 -> 0
     dry-run 未写 trades: 0 -> 0
     dry-run 未写 positions: 0 -> 0
     dry-run 未写 equity_curve: 0 -> 0
     dry-run 资金不变: {'real22000': 22000.0, 'paper100k': 100000.0} -> 同上

[T4] 三持仓各自取价 -> 市值正确: mv=62000.0
     缺价持仓不计入市值: mv2=42000.0（若用 avg_cost 会得 62000）
     持仓缺价 -> 拒绝买入: status=skip

[T5] 未配置 Token 时空 Token 写入 -> 503
     配置后空 Token -> 401
     配置后错误 Token -> 401

[T7] 5 并发 run_cloud -> 至多 1 个 done
     statuses=['locked','locked','locked','locked','done']

[T8] 无可用行情 -> status=failed，零成交，rejected=6
```

### 原有套件无回归

```
tools/p3_verify_standalone.py ........... 12/12 通过
```

### 旧库迁移验证（真实 PostgreSQL 16）

```
迁移前: run_key 唯一索引 = (无)，run_registry 行数 = 3（含 1 条重复）
迁移后: run_key 唯一索引 = uq_run_registry_run_key
        run_registry 行数 = 2（重复行归档，未删除）
        run_registry_dupes = 1
        exec_lock 表已创建
        重复 run_key 再插入 -> 被拒 ✓
        新 run_key 插入     -> 成功 ✓
```

---

## 三、修改文件清单

| 文件 | 变更 | 说明 |
|---|---|---|
| `astock/run_cloud.py` | **重写** | 新鲜度判定、执行锁、dry-run 净化、正确估值 |
| `astock/pg_ledger.py` | **大改** | `run_key UNIQUE` + 迁移、`exec_lock`、`acquire_lock`/`release_lock`、`value_positions`、`PriceUnavailable`、`start_run` 返回布尔 |
| `astock/app.py` | **大改** | 删除 `/api/job_script` `/api/runner`；未配置 Token → 503；`hmac.compare_digest`；读取鉴权 |
| `astock/config.py` | 小改 | 新增 `READ_TOKEN` |
| `.env.example` | 小改 | 新增 `ASTOCK_READ_TOKEN`、`ASTOCK_MAX_QUOTE_AGE_DAYS` |
| `.github/workflows/daily.yml` | 小改 | 新增幂等建表 + 迁移步骤 |
| `.github/workflows/ci-regression.yml` | **新增** | push/PR 自动回归测试 |
| `tools/regression_tests.py` | **新增** | 59 项回归断言 |
| `requirements.txt` | 小改 | 注明 Flask 对 T5 用例的必要性 |

**未变更**：`daily_job.py`、`ledger.py`、`marketdata.py`、`indicators.py`、
`strategy.py`、`stats.py`、`report.py`、`engine.py`、`backtest.py`、`ai_analyst.py`、
`db.py`、`missed_run_check.py`、`source_probe.py`、`db_probe.py`、
`tools/p3_verify_standalone.py`

---

## 四、仍需你注意的限制

1. **`ASTOCK_MAX_QUOTE_AGE_DAYS` 默认 7 天**。A 股春节等长假可能超过 7 天，
   届时会被判定为"过期"而拒绝成交（**保守方向，安全**）。
   如遇长假后首日误报，可临时调大该环境变量。

2. **`ASTOCK_READ_TOKEN` 默认未配置 = 读取公开**。
   接入真实持仓前**必须**在部署环境中配置，否则持仓明细可被任何人查看。

3. **`exec_lock` 的 TTL 为 1 小时**。若某次执行真的超过 1 小时
   （正常情况不会，`timeout-minutes: 20` 会更早杀掉），锁会被下一个执行者抢占。

4. **旧库迁移会修改 `run_registry`**。首次对已有数据的库运行时会：
   归档重复 `run_key` 到 `run_registry_dupes`、给 `run_key` 加 `NOT NULL`。
   建议首次执行前先用 Neon 的 6 小时即时恢复窗口做一次快照。

5. **本轮未做的**：未连接真实持仓、未迁移任何真实数据库、
   未启动定时任务、未部署。所有测试均在临时 PostgreSQL 实例上完成，
   该实例已停止并删除。生产库 `data/astock.db` 全程未被触碰。
