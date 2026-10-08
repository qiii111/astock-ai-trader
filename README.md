# A股 AI 投资分析 · 模拟交易系统

> **免责声明**：本项目是**纯模拟**研究与分析工具的代码骨架。不接入任何真实券商接口、
不自动下单、不构成投资建议。代码运行产生的任何数据均为模拟数据。

---

## 一、这个仓库做什么

一个「**GitHub Actions 定时 → 抓行情 → 算信号 → 写云端数据库 → 手机查看**」的闭环。
没有常驻服务器，不需要你维护 VPS，费用为 0。

```
┌──────────────────────┐
│ GitHub Actions       │  ← 调度器（北京时间工作日 15:30 触发）
│ ubuntu-latest Runner │  ← 美国 IP，不是国内机器
└──────────┬───────────┘
           │ ① 抓日线行情（腾讯为主源，东方财富为备源）
           ▼
┌──────────────────────┐
│ 行情数据源            │  web.ifzq.gtimg.cn（腾讯，境外可访问）
│                      │  push2his.eastmoney.com（东财，境外可能被阻断）
└──────────┬───────────┘
           │ ② MA20/60 金叉死叉 → 模拟撮合（T+1 / 整手 / 涨跌停 / 佣金印花税）
           ▼
┌──────────────────────┐
│ Neon PostgreSQL      │  ← 云端持久账本（免费额度内）
│  trades / positions  │     幂等写入：run_key + trade_key 双重去重
│  run_registry        │     ← 每次执行都留回执，可核验"是否真跑了"
└──────────┬───────────┘
           │ ③ 只读查询
           ▼
┌──────────────────────┐
│ 手机控制台（可选）     │  Flask 应用，登录后查看双账户净值/持仓/成交
└──────────────────────┘
```

**关键设计取舍**：

| 决策 | 选择 | 理由 |
|---|---|---|
| 调度器 | GitHub Actions cron | 免费、无需常驻进程、失败自动邮件告警 |
| 主行情源 | 腾讯 `web.ifzq.gtimg.cn` | 实测境外（美国 Runner）可稳定访问；东财对境外 IP 常阻断 |
| 数据库 | Neon PostgreSQL Free | 免信用卡、支持标准 SQL 事务与 UNIQUE 约束 |
| 幂等策略 | `trade_key` UNIQUE + `ON CONFLICT DO NOTHING` | 重试/重叠触发绝不重复扣款 |
| 漏执行检测 | 独立看门狗 workflow | 「没跑」和「跑了但失败」必须可区分，不能静默 |

---

## 二、目录结构

```
.
├── .github/workflows/
│   ├── daily.yml              # 每日作业（北京工作日 15:30）
│   ├── missed-run-check.yml   # 漏执行看门狗（北京 18:30 / 21:30）
│   ├── heartbeat.yml          # 心跳 + 数据源/DB 可达性探测（北京 08:00）
│   └── ci-regression.yml      # push/PR 自动回归测试（临时 PostgreSQL）
├── astock/
│   ├── daily_job.py           # ★ 自包含作业（纯标准库，含 --selftest 离线自检）
│   ├── ledger.py              # SQLite 账本层（本地开发用）
│   ├── pg_ledger.py           # ★ Neon PostgreSQL 账本层（云端用）
│   ├── run_cloud.py           # ★ GitHub Actions 入口：daily_job 逻辑 → Neon
│   ├── marketdata.py          # 多源行情抓取（重试 + 缓存 + 时间戳校验）
│   ├── missed_run_check.py    # 漏执行判定逻辑
│   ├── source_probe.py        # 行情源可达性探测（无凭据）
│   ├── db_probe.py            # 数据库连通性与权限探测（只读）
│   ├── config.py              # 参数集中配置（标的、费率、阈值）
│   ├── app.py / db.py         # 可选：手机控制台（Flask + SQLite）
│   ├── indicators.py          # MA / MACD / KDJ / RSI / BOLL / ATR
│   ├── strategy.py            # 策略层（金叉死叉、年线过滤）
│   ├── stats.py / report.py   # 绩效统计与报告生成
│   ├── engine.py / backtest.py / ai_analyst.py
│   └── ...
├── tools/
│   ├── p3_verify_standalone.py  # 离线验证套件 T1–T4（12 项断言，用临时库）
│   └── regression_tests.py      # 回归套件（81 项断言，需 PostgreSQL）
├── FIX_REPORT_v1.2.md           # 部署阻断问题修复报告（7 项）
├── FIX_REPORT_v1.3.md           # 单次抓取 + dry-run 只读修复报告（2 项）
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 三、A 股撮合规则（代码中的硬约束）

| 规则 | 实现位置 | 说明 |
|---|---|---|
| T+1 | `execute()` 中 `position["buy_date"] == date` 判定 | 当日买入不可卖 |
| 整手交易 | `int(budget / price // 100) * 100` | 100 股为 1 手 |
| 涨跌停 | `limit_pct()`：主板 10%，688/300/301 → 20% | 超限拒绝成交 |
| 佣金 | `max(amount × 0.025%, 5元)` | **5 元最低佣金是假设值，需按你的券商实际费率调整** |
| 印花税 | 卖出 `amount × 0.05%` | 仅卖出收取 |
| 过户费 | `amount × 0.001%` | 双向微收 |
| 单票上限 | `MAX_POS_RATIO = 40%` | 风控硬约束 |

> **待确认参数**：上表佣金费率（0.025%）与最低佣金（5 元）是**通用假设**。
> 不同券商差异很大（部分券商 ETF 最低佣金仅 0.1 元或免最低）。
> 请在 `astock/config.py` 中按你的实际费率修改，否则回测与模拟结果会有系统性偏差。

---

## 四、iPhone 配置步骤（一次性，约 15 分钟）

### 步骤 1 · 创建 GitHub 私有仓库

1. iPhone 浏览器打开 [github.com](https://github.com) → 登录 → 右上角 `+` → **New repository**
2. 填写：
   - **Repository name**：`astock-ai-trader`
   - **Description**：`A-share simulated trading (private)`
   - **Visibility**：选 **🔒 Private** ← **必须，绝不能选 Public**
   - ❌ 不要勾选 "Add a README file"（本包已自带）
3. 点 **Create repository**

### 步骤 2 · 上传本包内容

**方式 A（推荐，用 GitHub 网页）**：
解压本 ZIP → 在新建的空仓库页面点 **uploading an existing file** →
把**解压出的所有内容**（含 `.github`、`astock`、`tools` 等**隐藏目录**）拖入 →
Commit changes。

> ⚠️ iPhone 上「文件」App 可能不显示 `.github` 这类以点开头的文件夹。
> 若拖拽失败，改用 **方式 B**。

**方式 B（用 Working Copy App，免费）**：
App Store 安装 [Working Copy](https://workingcopy.app) → Clone 你的仓库 →
把解压后的文件导入 → `.github` 会正常显示 → Commit & Push。

**方式 C（用 GitHub Actions 自动上传，最省事）**：
见本文档末尾「附录 · 用命令行上传」。

### 步骤 3 · 注册 Neon 并拿到连接串

1. iPhone 浏览器打开 [neon.tech](https://neon.tech) → **Sign up**（可用 GitHub 账号登录，免信用卡）
2. **Region 选择**：`Asia Pacific (Singapore)` 或 `Asia Pacific (Tokyo)`
   （离国内最近；实际以连接延迟测试为准，见步骤 5）
3. 创建项目 → 进入 **Connection Details** → 选 **Pooled connection** → 复制连接串
4. 连接串形如：
   ```
   postgresql://neondb_owner:AbCd1234@ep-cool-name-123456-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```

> ⚠️ **这条连接串等同于数据库的钥匙**。
> - ✅ 只粘贴到 GitHub Secrets（步骤 4）
> - ❌ 不要发到聊天、微信、截图、任何公开脚本或 issue

### 步骤 4 · 配置 GitHub Secrets

仓库页 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value | 必需 |
|---|---|---|
| `DATABASE_URL` | 步骤 3 复制的完整连接串 | ✅ |
| `ASTOCK_PUSH_URL` | 你的控制台推送地址（不用控制台就留空/不建） | ⬜ |
| `ASTOCK_INGEST_TOKEN` | 控制台写入令牌（同上） | ⬜ |

> 若暂不配置 `ASTOCK_PUSH_URL`，`run_cloud.py` 会自动跳过推送，不影响主流程。

### 步骤 5 · 手动触发一次，验证端到端

仓库页 → **Actions** 标签 → 左侧选 **heartbeat** → 右侧 **Run workflow** → 绿色按钮。

跑完后展开日志，检查：
- `SOURCE_PROBE` 行应显示 `tencent` 为 `OK`（`eastmoney` 显示 FAIL 属预期）
- `DB_PROBE` 行应显示 `"connected": true`，`tables_missing` 为 5 张表（首次运行正常）

再手动跑一次 **daily-job**（可先把 `dry_run` 设为 `true` 试跑，只算不写库）。

> **首次运行会建表**。`run_cloud.py` → `pg_ledger.init_ledger()` 会自动创建
> `acct / positions / trades / equity_curve / run_registry` 五张表。

### 步骤 6 · 确认调度已生效

- **daily-job**：每个工作日（周一~周五）北京时间 15:30 自动运行
- **missed-run-check**：北京时间 18:30 与 21:30 检查当天是否有成功回执
- **heartbeat**：北京时间 08:00

---

## 五、三级漏执行告警（重要）

GitHub Actions 的 cron **不保证准时**，高峰期可能延迟几分钟到几十分钟。
本项目用三层独立机制确保「任务没跑」会被发现：

| 层级 | 机制 | 能发现什么 |
|---|---|---|
| 1 | Actions 原生失败邮件 | 流程报错 / 超时（`timeout-minutes: 20`） |
| 2 | `missed-run-check.yml` | 当天**根本没有** started 记录，或全部 failed |
| 3 | `heartbeat.yml` | 调度链路整体瘫痪（连心跳都没跑 = 平台问题） |

另外，`run_registry` 表本身就是可核验的执行日志：

```sql
-- 最近 7 天的执行情况（在 Neon SQL Editor 里跑）
SELECT run_key, status, market_date, source,
       started_at, finished_at, LEFT(error, 120) AS err
FROM run_registry
ORDER BY id DESC LIMIT 20;
```

**如何区分三种异常**：

| 现象 | 含义 | 处理 |
|---|---|---|
| 有 `failed` 行 + error 非空 | 代码/数据源问题 | 看 error 字段定位 |
| 有 `running` 行但无 `finished_at` | 执行中途被中断 | Actions 日志会显示超时 |
| 完全没有当天的行 | 调度未触发 | 检查是否节假日 / Actions 用量耗尽 |

> **法定节假日**：A 股休市日不产生新行情，属于正常情况。
> 看门狗目前对节假日采取宽松处理；若误报，可在 `missed_run_check.py` 中维护豁免日期表。

---

## 五之二、安全机制（v1.2 加固）

### 写入鉴权
`/api/live_push` 要求 `X-Auth-Token`。
**服务端未配置 `ASTOCK_INGEST_TOKEN` 时，一律返回 503 拒绝所有写入**——
不会出现"空 Token 通过认证"的情况。Token 比较使用 `hmac.compare_digest`（防时序攻击）。

### 读取鉴权
配置 `ASTOCK_READ_TOKEN` 后，控制台全部读取接口（`/`、`/api/state.json`、
`/api/runs.json`、`/api/live.json`、`/report`）都要求携带令牌。
未配置则读取公开，**仅限本地开发**；接入真实持仓前必须配置。

### 已下线的接口
`/api/job_script` 与 `/api/runner` 曾允许**无认证**拉取完整交易逻辑源码，已删除。
HTTP 唤醒沙箱的机制也已由 GitHub Actions 直连架构取代。

### 数据完整性（拒绝脏数据成交）
行情必须通过四项检查才会用于成交，否则标记 `REJECTED` 并计入回执：

| 检查 | 拒绝条件 |
|---|---|
| 缓存回退 | `stale=True`（在线源全失败） |
| 无数据 | 返回空行 |
| 时间戳异常 | 行情日期晚于今天 |
| 过期 | 早于今天超过 `ASTOCK_MAX_QUOTE_AGE_DAYS`（默认 7 天） |

全部标的都不可用 → 直接 `failed`、零成交、记录原因、退出码 1。

### 并发安全
三层防线，任何一层单独失效都不会导致重复成交：

1. `exec_lock` 表主键 —— 原子抢锁，同时刻只有一个执行者
2. `run_registry.run_key UNIQUE` —— 数据库层拒绝重复
3. `trades.trade_key UNIQUE` —— 单笔成交绝不重复扣款

### 正确的持仓估值
每只持仓使用**自己的最新有效价格**估值，不会把当前交易标的的价格套用到其他持仓。
缺价持仓不计入市值、也不用成本价冒充；此时拒绝下单（避免总资产失真导致仓位超限）。

## 五之三、v1.3 追加加固（单次抓取 + dry-run 只读）

### 单次抓取：信号与成交价必然同源
行情**只抓取一次**，`snapshot` 是唯一副本；信号计算与成交价都从它读，
不再二次调用 `fetch_daily()`。
这消除了"按 A 版本数据决策、按 B 版本价格成交"的隐患——
盘中价格随时变化，二次抓取会让边界信号的成交价失真。

### dry-run 是真正的只读验证
`python run_cloud.py --dry-run` 走**独立只读路径**，硬保证：

| 保证 | 说明 |
|---|---|
| 不建表 / 不迁移 | **不调用** `init_ledger()`；用 `to_regclass()` 探测表是否存在 |
| 不获取执行锁 | 完全不触碰 `exec_lock` |
| 不写任何行 | 无 `run_registry` / `trades` / `positions` / `equity_curve` / `acct` 写入 |
| 不推送回执 | 不触发 `_maybe_push` |
| 自证 | 运行前后各探测一次，输出 `no_side_effects: true` |

`daily.yml` 同步拆分为互斥两步：只读探测（`dry_run == 'true'`）
与建表迁移（`dry_run != 'true'`），**dry-run 时后者整步跳过**。

> 实测：对**空库**执行 dry-run 后仍为 0 张表；执行正式流程则建出 6 张表 —— 门控真实有效。

## 六、本地运行（可选）

```bash
# 1. 离线自检（不联网、不写库，验证撮合/幂等/持仓逻辑）
cd astock && python3 daily_job.py --selftest

# 2. 离线验证套件（12 项断言，使用临时数据库，绝不触碰生产数据）
python3 tools/p3_verify_standalone.py

# 2b. 回归套件（81 项断言，覆盖安全/并发/数据完整性/单次抓取/dry-run 只读；需 DATABASE_URL）
export DATABASE_URL='postgresql://...'      # 指向**测试库**，勿用生产
python3 tools/regression_tests.py

# 3. 试运行（真实抓行情、算信号，但不写账本）
python3 daily_job.py --dry-run

# 4. 写入本地 SQLite（默认双账户 real22000 / paper100k）
python3 daily_job.py --strategy ma
```

**写入 Neon**（需先 `export DATABASE_URL=...`）：

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql://...'
cd astock && python run_cloud.py --dry-run      # 只读验证：不建表/不迁移/不写任何行
cd astock && python run_cloud.py                # 正式写入（首次会建表并迁移）
```

---

## 七、已知限制与风险（务必阅读）

### 1. 东方财富源在境外大概率不可用
实测 GitHub Actions 的美国 Runner 访问 `push2his.eastmoney.com` 会连接被重置
（HTTP 000 / RemoteDisconnected）。**因此腾讯是本项目的事实主源**。
若腾讯也变更策略，`source_probe.py` 会在心跳日志里暴露出来。

### 2. GitHub cron 延迟是常态
官方明确说明 `schedule` 在高负载时可能延迟，极端情况可达数十分钟。
本项目通过「延迟可接受 + 漏执行必告警」的组合来应对，而非追求准时。

### 3. 免费额度限制

| 项目 | 免费额度 | 本项目月耗（估算） |
|---|---|---|
| GitHub Actions（Private 仓库） | 2,000 分钟/月 | 约 **30 分钟/月**（3 条流水线 × 21 工作日 × ~10s，含安装依赖） |
| Neon 存储 | 1 GB/项目（2026-10-02 起） | < 50 MB/年（纯文本账本） |
| Neon 计算 | 100 CU-小时/项目/月 | 极低（scale-to-zero 后仅查询时计费） |

> ⚠️ **Actions 免费额度对 Private 仓库生效**。若误建为 Public 仓库则额度无限，
> 但**代码会公开**——所以务必保持 Private。
> 额度与规则会变化，请以 GitHub / Neon 官方控制台**实际显示**为准。

### 4. 数字精度与"事实/估计"的区分
本项目所有输出遵循：**事实**（有可复现数据）、**估计**（标注 `≈` 与假设）、
**未完成项**（明确说明缺失）三者严格区分，不把估算包装成精确值。
例如：ETF 换仓的佣金费率是**待确认参数**，代码中不硬编码单一答案。

### 5. Neon scale-to-zero
免费版实例在 5 分钟无活动后自动挂起，下次连接会冷启动（约 0.5–2 秒）。
`pg_ledger.connect()` 内置 4 次重试 + 退避，足以覆盖冷启动。

---

## 八、常见问题

**Q：Actions 报 `未设置环境变量 DATABASE_URL`？**
A：`DATABASE_URL` Secret 没配好。注意名字必须完全一致，且要在**同一仓库**的
Secrets 里（不是 Environment secrets 误配）。

**Q：`psycopg` 安装失败？**
A：`requirements.txt` 用的是 `psycopg[binary]`（含预编译二进制），
在 ubuntu-latest 上通常秒装。若失败，检查是否被 `pip` 缓存污染，可加 `--no-cache-dir`。

**Q：为什么 daily-job 里要先跑 `--selftest`？**
A：先证明撮合/幂等/持仓逻辑完好，再动真账本。这是一个廉价的前置防线。

**Q：想改调度时间？**
A：编辑 `.github/workflows/daily.yml` 里的 `cron: "30 7 * * 1-5"`。
**cron 是 UTC 时间**，北京时间 15:30 = UTC 07:30。

**Q：真实持仓怎么保证不泄露？**
A：本仓库只含**代码**。持仓数据全部在 Neon 数据库里，靠 `DATABASE_URL` 访问；
仓库 `.gitignore` 已排除 `data/`、`*.db`、`backups/`、`p2data/`、`.env*` 等。
上传前请再核对一次 `git status`，确认没有意外文件。

---

## 附录 · 上传前自检清单

在上传本包到 GitHub 前，请逐项确认：

- [ ] 仓库可见性 = **Private**
- [ ] 解压后的目录里**没有** `data/`、`backups/`、`p2data/`、`reports/`
- [ ] 没有 `*.db` 文件
- [ ] 没有 `.env`（只有 `.env.example`，且其中**不含任何真实值**）
- [ ] 全局搜索 `postgresql://`，应只在 `.env.example` 和 README 的**示例**中出现
- [ ] 全局搜索 `token`，确认无硬编码令牌
- [ ] `DATABASE_URL` 只存在于 GitHub Secrets 中

---

## 附录 · 用命令行上传（方式 C）

若你有电脑可用，这是最稳的方式：

```bash
unzip astock-ai-trader.zip
cd astock-ai-trader
git init
git add .
git status                      # ← 检查即将提交的文件列表
git commit -m "init: A-share simulated trading system"
git branch -M main
git remote add origin https://github.com/<你的用户名>/astock-ai-trader.git
git push -u origin main
```

> `git status` 那一步**不要跳过**。确认列表里没有数据库、日志、真实持仓文件。

---

## 许可

仅限个人研究使用。使用者需自行承担因数据源变更、规则理解偏差、
或据此做出的任何投资决策所产生的全部后果。
