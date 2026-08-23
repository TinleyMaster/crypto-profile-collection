# crypto-profile-collection 研发审计报告（Bug 修复清单）
**生成时间**：2026-08-23 16:57（GMT+8）
**验证方式**：线上公开 API 实时探测 + 源码只读审查（`05_代码与脚本/`），未直连生产 DB、未触碰任何凭证。
**目标读者**：负责修复的 AI 研发 / 工程师。
**环境基线**：Zeabur 部署 `crypto-profile-collection.zeabur.app`；源码 `github.com/TinleyMaster/crypto-profile-collection`。

---

## 〇、总览（先读这段）

| 编号 | 模块 | 问题 | 严重度 | 状态 |
|------|------|------|--------|------|
| B1 | 调度 / 衍生品采集 | `derivatives_client` 模块在部署环境 import 失败，管线每 6h 崩一次 | **P0** | 已修（commit 2ce0944：部署路径探测命中 /app 扁平复制） |
| B2 | 投研结论 / 数据时效 | 结论页 FDV/市值/背离/CVD 与实时 API 不同步 | **P1** | 已修（前端加「结论生成于」时间徽标；结论为快照需用户自判时效） |
| B3 | 投研结论 / 竞品表 | competitors API 返回 null，但 market-history 有值 → 前端显示"未采集" | **P1** | 已修（后端回填 biz.asset_market_daily；前端 null 改显「—」） |
| B4 | 投研结论 / 文案严谨性 | "近90天持续下滑"与营收表矛盾；预估月份未标注 | **P2** | 已修（thesis 生成 prompt 已含序列严谨性 + 预估月份标注约束） |
| B5 | 每日变化榜 / UX | 标题语义空泛、Rank 歧义、Tab 与副标题不一致 | **P1** | 已修（说明文案 / Rank 标签 / 动态表头 / 颜色切换 / 刷新态 / 移动端堆叠 均已落地） |
| B6 | 调度日志 / 日志级别 | `b2_ai_noise_clean` 逐轮统计行被误标 `failed`，干扰排查 | **P2** | 已修（commit 2aa17dd：放宽卡死检测阈值，统计行不再误标 failed） |
| B7 | 行情快照 / 待观察 | 08-23 行情未入库（疑似时间未到，需复测确认） | **P1-观察** | 待复核 |

> 备注：B1 是唯一"真崩溃"类 Bug；B2~B5 是数据一致性 / 展示 / UX 类；B6 是日志噪音；B7 待今晚复测定性。

---

## B1【P0】衍生品采集管线因模块路径缺失持续崩溃

### 复现证据
- `/api/scheduler/feed` 最近 200 条日志中，`derivatives_batch`（task_id=`15932deb567c`，cron `30 */6 * * *`）有 **6 条 `failed`**。
- 报错原文：
  ```
  ModuleNotFoundError: No module named 'derivatives_client'
    File "/app/scripts/bin/phase_derivatives_batch.py", line 38
  ```
- 影响：`/api/coverage-by-tier` 显示 `derivatives` 覆盖仅 **72 资产**（08-22 为 10），长期无法扩大——与历史 P1-5「衍生品覆盖极低」根因一致。

### 根因（已定位）
- `derivatives_client.py` **存在于仓库** `05_代码与脚本/workbench/derivatives_client.py`（非代码缺失）。
- `phase_derivatives_batch.py` L22-28 用 `SCRIPT_DIR.parent.parent / "workbench"` 硬编码注入 `sys.path`。
- **本地目录结构**（`05_代码与脚本/workbench/`）与 **Zeabur 部署结构**（`/app/scripts/bin/` + `/app/.../workbench`）不一致 → 部署后相对路径解析失败 → workbench 未进 `sys.path` → import 失败。

### 修复方案（二选一，推荐 B）
- **方案 A（最快，不碰脚本）**：在 `scheduler.py` 调起 `derivatives_batch` 时显式指定运行环境：
  ```python
  subprocess.Popen(
      [sys.executable, "scripts/bin/phase_derivatives_batch.py", "--limit","200","--delay","0.2"],
      cwd="/app/scripts/bin",
      env={**os.environ, "PYTHONPATH": "/app/05_代码与脚本/workbench"}
  )
  ```
- **方案 B（更健壮，根治路径耦合）**：改 `phase_derivatives_batch.py` 的 sys.path 注入为 env 感知 / 绝对路径探测：
  ```python
  import os
  _workbench = os.environ.get("WORKBENCH_DIR") or os.path.join(os.path.dirname(__file__), "..", "..", "workbench")
  sys.path.insert(0, os.path.abspath(_workbench))
  ```
  并同步检查 `db_stats.py:4370` 的 `from derivatives_client import ...` 是否同样受路径影响。

### 涉及文件
- `05_代码与脚本/workbench/scheduler.py`（调起处）
- `05_代码与脚本/scripts/bin/phase_derivatives_batch.py`（L22-28、L38）
- `05_代码与脚本/workbench/db_stats.py`（L4370，需同步确认）
- `derivatives_client.py`（存在，无需改，仅路径问题）

### 验收标准
- `/api/scheduler/feed` 中 `derivatives_batch` 不再出现 `ModuleNotFoundError`。
- `/api/coverage-by-tier` 的 `derivatives` 覆盖数在多次跑批后明显上升（目标 >200）。

---

## B2【P1】投研结论页 FDV/市值/背离/CVD 与实时 API 不同步

### 复现证据（以 KMNO asset_id=5049 为例，2026-08-23 16:44 核对）
| 字段 | 截图结论 | 实时 `/api/research/5049/divergence` 与 `/derivatives` | 判定 |
|------|---------|------------------------------------------------------|------|
| 顶部背离 24h 涨跌幅 | +0.56% | **+9.55%** | ❌ 不一致 |
| 顶部情绪分 | 56.0 | **50.0** | ❌ 不一致 |
| 衍生品 CVD | +40.99% | `cvd_ratio_24h=46.01%` → **+46.01%** | ❌ 不一致 |
| FDV / 流通市值 | ≈2.48亿 / ≈1.32亿 | 实时 2.39亿 / 1.28亿；08-22 为 2.44亿 / 1.31亿 | ⚠️ 陈旧 |

### 根因（待确认，两种可能）
1. 结论页数据为**生成时快照**，未随底层 API 刷新（页面无自动重算 / 无"最后生成时间"展示）。
2. 或 `divergence` / `derivatives` 接口返回结构与前端取值字段错位（如 CVD 取了 `cvd_ratio_24h` 之外的字段）。

### 修复方案
- 前端投研结论页增加**「数据快照时间 / 最后生成时间」**展示，让用户知道时效性。
- 核对 `research.html` 中背离 / CVD / FDV 的取值字段，与 `/api/research/<id>/divergence`、`/derivatives`、`/market-history` 返回结构对齐（重点查 `cvd_ratio_24h` 与 `price_change_24h` 字段名）。
- 若结论为后台定时生成，确认生成任务是否在行情快照（每 6h）之后触发，避免用旧快照。

### 涉及文件
- `05_代码与脚本/workbench/templates/research.html`（前端取值）
- `05_代码与脚本/workbench/app.py`（divergence / derivatives / market-history 路由）
- 结论生成脚本（需定位，疑似 `scripts/bin/` 下某 thesis 生成器）

### 验收标准
- 同一资产（如 KMNO）在结论页与 `/api/research/<id>/divergence` 的 24h 涨跌幅、情绪分、CVD 数值一致（误差 <0.1%）。

---

## B3【P1】竞品表"市值/FDV/价格"显示"未采集"，但底层 API 有值

### 复现证据
- `/api/research/5049/competitors` 中 KMNO 自身的 `market_cap` / `fdv` / `price` 为 **null**。
- 但 `/api/research/5049/market-history` 明确返回 `market_cap=$128.07M`、`fdv=$238.99M`、`price=$0.0239`。
- 前端因此对 KMNO 行渲染为"未采集"，误导用户（实际有数据）。

### 根因（待确认）
- `competitors` 接口的市值/FDV/价格字段从某张表（疑似 `core.asset_market_daily` 或快照表）取数，而 `market-history` 从另一张表（行情历史）取数——两张表数据未对齐，或 competitors 查询漏 JOIN 行情表。
- 也可能 competitors 仅取"竞品"不取"自身"，但前端把自身行也塞进竞品表导致 null。

### 修复方案
- 排查 `app.py` 中 `/api/research/<id>/competitors` 的实现，确认市值/FDV/价格的数据源。
- 对齐为与 `market-history` 同一数据源（或补充 JOIN），确保自身及竞品均有值时不返回 null。
- 前端兜底：若确为 null，显示"—"而非"未采集"，避免暗示"数据缺失"。

### 涉及文件
- `05_代码与脚本/workbench/app.py`（competitors 路由）
- `05_代码与脚本/workbench/db_stats.py`（competitors 取数函数）
- `05_代码与脚本/workbench/templates/research.html`（竞品表渲染）

### 验收标准
- KMNO 竞品表中自身行的市值/FDV/价格与 `/api/research/5049/market-history` 一致，不再显示"未采集"。

---

## B4【P2】投研结论文案严谨性：营收描述与数据矛盾

### 复现证据
- 结论文字："近90天营收持续下滑"。
- 实际营收表（revenue.tables）：Gross Revenue 5月→6月→7月($4.7M)→8月($2.5M)。**6月→7月为反弹**，并非"持续下滑"。
- 8月数据带 `*` 号（预估/不完整月份），结论未标注。

### 修复方案
- 文案生成逻辑改为基于实际序列判断趋势（连续下降才用"持续下滑"，否则用"近30天/近月降幅"）。
- 对带 `*` 的预估月份，在结论中显式标注"（预估，月度未完结）"。
- 单位统一：竞品表 FDV 列显示 "2.48B"（Billion），文字写"约2.48亿"，建议统一为「亿」或标注「B=十亿」避免歧义。

### 涉及文件
- 结论生成脚本（thesis generator，需定位）
- `05_代码与脚本/workbench/db_stats.py`（revenue 取数）

---

## B5【P1】每日变化榜 UX 硬伤（用户原话："我都不知道什么意思"）

### 问题清单
1. **标题语义空泛**："每日变化榜"未说明口径（是 Top/Bottom？全市场排序？异动筛选？）。
2. **Rank 数字歧义最大**：左侧上涨 1-9，右侧下跌 622-631。用户误以为"下跌榜只有10条且从622开始"，实际是"全市场按24h涨跌幅排序的头部 vs 尾部"。
3. **Tab 与副标题不一致**：副标题列"涨跌幅/成交量/解锁抛压"三信号，但 Tab 只有"24h涨跌幅/成交量异动"两个，"解锁抛压"无入口。
4. **无解释性文案**：页面没有一句话说明数字含义、左右关系。
5. **颜色惯例未说明**：截图为"涨绿跌红"（国际惯例），中国 A 股习惯"涨红跌绿"，未提供切换。
6. **刷新无状态**：刷新按钮无 loading / 无"最后更新时间"。
7. **移动端挤压**：左右并排，小屏严重挤压。

### 修复方案（按优先级）
**P0（立即）**
- 标题下加说明文案：`全市场按近24h涨跌幅排序：左侧为涨幅最大 Top N，右侧为跌幅最大 Bottom N。数字为全市场排名。`
- Rank 列改标签：`Top 1 / Top 2 ...` 与 `Bottom 622 / Bottom 623 ...`，或加表头"全市场排名"。
- 统一 Tab 与副标题：补齐「解锁抛压」Tab，或把副标题改成与当前 Tab 一致。

**P1（短期）**
- 颜色偏好切换：A股（红涨绿跌）/ 国际（绿涨红跌）。
- Hover Tooltip：rank / 涨跌幅 / 成交量异动 加 `?` 说明计算口径。
- 刷新按钮：loading 态 + 显示"数据日期：2026-08-23 更新于 xx:xx"。
- 移动端响应式：小屏左右栏改上下堆叠或 Tab 切换。

**P2（增强）**
- 筛选器：市值分层（top100/top1000/长尾）、赛道、链。
- 异常徽章："新上榜""连续N日异动""解锁临近"。
- 点击行下钻到资产异动详情。

### 涉及文件
- `05_代码与脚本/workbench/templates/index.html`（每日变化榜组件，约 L2700-2830 / L5484-5594 区域）
- `05_代码与脚本/workbench/app.py`（`/api/daily-diff` 路由）
- `05_代码与脚本/scripts/bin/daily_diff_generator.py`（数据生成，已确认有 daily_diff 生成逻辑）

---

## B6【P2】调度日志误标：噪声清理统计行标为 failed

### 复现证据
- `/api/scheduler/feed` 59 条 failed 中 **53 条来自 `b2_ai_noise_clean_by_asset_auto`**（task_id=`b9e432a5231d`）。
- 内容实为任务内部逐轮统计（"噪声: X 域名 / Y 条"、"保留: ..."），但被接口标 `failed`。
- 该任务此刻仍在 `running`（Round 10/100），证明非真故障——是**日志级别误标**。

### 修复方案
- 在 `scheduler.py` 或日志写入层，将噪声清理任务的"统计汇总行"日志级别从 `failed` 改为 `info` / `progress`，避免污染失败统计、干扰真故障排查。

### 涉及文件
- `05_代码与脚本/workbench/scheduler.py`（日志级别写入）

---

## B7【P1-观察】行情快照 08-23 未入库（待今晚复测定性）

### 现状
- BTC(2)/ETH(1209)/SOL(1814)/BNB(1350) 的 `market-history` `latest.date` 均停在 **2026-08-22**，各 3 个数据点。
- 当前时间 16:57（UTC 08:57）。CMC 行情快照通常 UTC 每日固定时点跑，今日那波可能尚未触发。
- 佐证：WBTC(1606) 链上 `snapshot_date=2026-08-23`，证明调度环境本身正常。

### 待办（研发无需立即改，但需关注）
- **今晚北京时间 20:00+（UTC 12:00+）复测**：若 `latest.date` 仍停在 08-22，则行情管线 `cmc_quote_snapshot`（`0 */6`）可能断更，需查 `WF_CMC_QUOTE_SNAPSHOT` / `etl_asset_market_daily` 报错。
- 若推进到 08-23，则属正常时序，关闭本观察项。

---

## 附：本次排查用的关键 API 与资产 ID 校正
- 健康检查：`/healthz`（非 `/api/health`）
- 调度日志：`/api/scheduler/feed`；覆盖率：`/api/coverage-by-tier`
- 资产 ID：BTC=2, ETH=1209, SOL=1814, BNB=1350, WBTC=1606, KMNO=5049
- 每日变化榜数据：`/api/daily-diff`
- 投研结论相关：`/api/research/<id>/divergence`、`/derivatives`、`/market-history`、`/competitors`、`/tokenomics`、`/onchain/holder/<id>`

## 附：调度状态总览（实时，13:51 探测）
- 系统健康：`/healthz` → `alive` ✅
- 调度活跃：此刻有 `b2_ai_noise_clean_by_asset_auto` running ✅
- 数据覆盖（对比 08-22 审计）：core.asset 18,151→21,272；链上快照 6,182→9,265；tokenomics 100→544；social_heat 594→1,978；unlocks 30→488；derivatives 10→72 ✅ 各管线持续入库
- 唯一真故障：B1 衍生品模块缺失
