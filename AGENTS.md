# 工作记忆（AGENTS.md）

## 常驻工作流约定（用户指令，必须遵守）

- **继续处理 bug**：审计报告会以 `审计_*.md` 形式提供（位于 `E:\瞎搞乱搞\workbuddy\crypto-profile-collection\`），按其中发现的 P0/P1/P2 缺陷逐项修复。
- **改完代码后**：
  1. 清理本次会话产生的临时文件（`Temp/opencode` 下的探测脚本、工作区内的调试/临时产物）；
  2. **自动推送**：`git add` → `git commit` → `git push origin main`，提交信息用中文、`fix:` 前缀。
- **原则**：只提交自己改的文件；并发进程（workbuddy）对 `grade.py` / `phase_catalyst_pipeline.py` 等文件的改动**不要纳入**自己的提交；破坏性数据操作（DELETE/清表）需用户授权后再执行。

## 项目关键信息

- 工作目录：`E:\瞎搞乱搞\web3\加密货币研究报告`（git 仓库，origin/main）
- 代码位置：`05_代码与脚本/scripts`（ingest/backfill 等 bin 脚本）、`05_代码与脚本/workbench`（web 应用 + 大盘分析 macro_market.py + AI 分析 ai_signal_analyzer.py）
- 审计报告归档：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_*.md`
- prod DB：PostgreSQL（`.env` 的 `DATABASE_URL`），`biz.*` 业务表、`core.asset` 资产表、`src_cmc.*` 快照表

## 已知待办/历史修复速查（2026-09-18）

- 早报 4 项缺陷（P1 巨鲸跨链混取 / P1 MVRV 缺失 / P2 Date 头 / P2 成交量口径）→ 已修 `85a7d52`
- etf_flow_daily 零值污染（F1-F5：占位 0 拦截 / 增量回补 / 调度双跑 / --prune-zeros / 日志告警）→ 已修 `54847e8`
- 高亮信号板块（P0-1 KOL 归因 event_token 优先 + trigger_logic 带事件标的 / P0-1 AI onchain 方向硬约束 / P1-1 analysis_ts / P1-2 事件驱动直通标注）→ 已修
- 催化剂决策链路 d1~d6（按审计清单逐项落地）：
  - d1 G1 新增「发布前启动程度」惩罚项（追高扣分）→ `c946ed1`
  - d2 权重校准解耦「强度」与「方向可靠性」→ `621fb3d`
  - d3 信号分层：`confirmed`(价格已定价)→`watch` 观察池不推送 / `weak`→`open` 可动作 / `divergent`→`invalid`。实测依据（仅前向样本 `ret_source='klines+market_daily'`）：confirmed 72h 超额 **-2.17%（n=49，命中 40.9%）** vs weak **+0.64%（n=481，命中 59.3%）**，即「等价格确认再开单」= 追高。迁移 `fix_053` + 存量 2110 行按新口径收敛 → `3ce5253`
  - d4 `event_type` 方向映射实测校正（tech_upgrade 实为利好出尽→bearish；funding/burn 样本不足降 neutral）→ `a4ef955`
  - d5 二阶共振刷新：二阶信号不再「创建时算一次就冻结」，慢通道每轮按当前 peer-median 重算共振并回写既有信号行（详见下节）→ `f791421`
  - d6 方向闸门 + listing 收紧：利空/中性不再产出做多档位（详见下节）→ 本次提交

### 本轮新增（2026-09-18 只读校验 + 修复）

- **分层上线后校验（只读口径）**：open 1189（全 weak）/ watch 758（confirmed 293 + pending 465）/ invalid 163（全 divergent）；四项不变量（open 非 weak、invalid 非 divergent、watch 越界、终态异常）全为 0；慢 Alert 候选 11 取 top2；早报观察区候选 904；快提醒只推「本轮转为 open」集合；`notification_log` 快提醒历史 7 次（09-11~09-16）。
- **outcome 表脏数据（已修）**：`backtest_catalyst_impact.py` 无 now 闸门，`kline_close_at` 在窗口未到期时取到 K 线末尾，把「base 到最新」冒充 72h 收益——实测 60 行伪造（ret_4h = ret_24h = ret_72h，或 0.0000），且 base_time 用 published_at 与 collect 的 created_at 基线混用。已加「仅回放 14d 窗口走完的历史 catalyst」过滤 + 清理 189 行（按窗口逐个判到期，只清未到期列）→ 本次提交。全表「未到期却已结算」已归零。
- **信号滞后非系统性问题**：近 3 天新建信号滞后中位 **2.2h**（均值 75.5h 被历史回填尾巴拉高）；全量分档 2507 条在 0-6h 内。此前「平均滞后 153h」属口径误判。

### 二阶共振刷新（d5，已修 2026-09-18）

- **问题**：`biz.catalyst_signal` 有 3669 行无对应 `biz.catalyst_resonance`（100% 是二阶信号，非二阶孤儿 0 行）。根因：SO 路径候选集 one-shot（`NOT EXISTS second_order`）+ 只处理 lookback 24h 内新分级 catalyst + 写入 `ON CONFLICT DO NOTHING`，故二阶信号的 `resonance_score/state` 只在创建时算一次（resonance 权重 0.30 是最大项，长期失真）。实测：497 catalyst / 5823 映射 / 3670 信号中，**2352 条 score 与当前 peer-median 期望不符、521 条 state 失真**；19 个 catalyst 无任何 resonance 行（81 信号，其中 18 行非终态）。
- **修复**：`phase_catalyst_pipeline.py` 新增 `_peer_median_by_catalyst()`（口径：`sorted(scores)[len//2]`）与 `refresh_second_order_resonance()`，用 `CatalystSignalBuilder.build()` 按当前 peer-median 重算 `resonance_score/state → composite_score/tier/confidence/invalidation/status`，经 temp 表 `_write_so_resonance_refresh()` 批量回写既有非终态行；值无变化不写；无 peer 数据的 catalyst 跳过。
- **慢通道执行顺序**：二阶展开 → G3-G5 补全 → **二阶共振刷新（步骤 2.5）** → 过期巡检。
- **不覆盖字段**（保护白名单）：entry/stop/tp、`ai_reason`、`investment_cycle`、`persistence`/`technical_state`、regime、`expires_at`；`expired`/`done` 终态冻结不被改写。
- **g3g5 status 对齐 d3**：`run_slow_g3g5` 原硬编码 `ELSE 'open'` 改为 `status = t.status`（tier=None → invalid），否则已定价(confirmed)/未反应(pending) 的信号会被误放进可动作集合。
- **回滚单测（事务内）**：`{scanned:1491, refreshed:105, skipped_no_peer:18, status_promoted:26, status_demoted:39}`；105 行变化中受保护字段零改动，刷新后四项不变量越界均为 0。
- **无需迁移/批量 UPDATE**：存量行在下一轮慢通道幂等收敛（未对 prod 执行写操作，由部署后的守护进程执行）。

### 方向闸门 + 分级纠偏（d6，已修 2026-09-21）

对应审计 P0-2 / P1-1 / P2-2。本系统档位是「做多」口径，但 `catalyst_impact.impact_direction` / `ai_sentiment` 此前只参与 G2 共振打分，不参与档位与动作判定。

- **P0-2 利空新闻被判成 A 级做多**：ZEC catalyst（正文实为「Zcash 涨6% / XRP 跌7% / Clarity Act 参议院受阻」的 Bankless 行情综述）被 rule 判为 `listing`（事件权重 95），AI 判 `market_update`。根因：listing 关键词（上线/发行/上市/list/launch）在**长正文**做子串匹配，且 `require_token_hint` 因 `related_pairs` 非空被绕过。
  - 修复 `classify.py` 新增 per-rule `title_only`（`_compiled_rules` 元组第 4 位）；`catalyst_rules.yaml` 的 `listing`/`delisting` 改 `title_only: true` 并收紧为公告型短语（删除 新增/发行/上市/list/launch）。11 条旧误判样本全部离开 listing，真实上线公告（标题含 PATH/USDT）仍命中。
- **d6 方向闸门**（唯一落点 `signal.py::CatalystSignalBuilder.build()`，所有调用方共用）：
  - `bearish` → `status='invalid'`（利空不产做多机会；tier/composite 保留供回测）
  - `neutral` → `tier` 封顶 C（中性方向不占 A/B 推送位；信号与档位保留）
  - 与 RR 闸门同属「只降级不改分」的显式例外。
  - `build()` 新增参数 `impact_direction`；4 个调用点（`run_signal` / `run_slow_second_order` / `refresh_second_order_resonance` / `run_slow_g3g5`）均已传入，方向取 `COALESCE(ci.impact_direction, ac.ai_sentiment)`。
- **P2-2 二阶信号 tier 上限 C**：此前仅创建路径（`run_slow_second_order`、`phase_catalyst_backfill`）有 cap，重算路径漏加。已补 `refresh_second_order_resonance`、`run_slow_g3g5`（新增 `is_second_order` 标记 + cap）。
- **存量收敛迁移 `fix_055`**（本次已对 prod 执行，三段幂等 UPDATE，备份表 `biz.catalyst_signal_direction_backup_20260921`）：
  - bearish → invalid **709 行**；neutral → tier C **658 行**；二阶 → tier C **1,572 行**。
  - 终态 `expired`/`done` 冻结不改；已 `invalid` 不再改（避免 `updated_at` 抖动，该列被「信号滞后」口径引用）。
  - 收敛后校验：三项不变量越界均为 0（bearish 非 invalid=0、neutral A/B=0、二阶 A/B=0）。
  - 注：直连通路的 bearish/neutral 行**必须**靠本迁移收敛——`run_slow_g3g5` 候选集是「G3-G5 缺失」，补全后的行不会被重算。

### d6 部署验收（2026-09-21）

- **三项不变量在「非终态」集合上全部为 0**：`neutral` 且 tier A/B = 0、`bearish` 且 status ≠ invalid = 0、二阶 且 tier A/B = 0。部署后新建信号里 neutral 全为 C、bearish 全为 invalid、tier A = 0。
- **验收口径坑（必记）**：不要用「全表 bearish ≠ invalid」判违规——`expired`/`done` 终态行按设计冻结。全表二阶 A/B 有 **986 条，但 986/986 全是 `expired`**，非终态 0 条；非二阶 A/B 遗留 744 条（486 终态 + 258 非终态，后者 95 bullish open / 39 bullish watch / 5 bullish A open 等均属合法，另 107 条为 `invalid`，来自 d3 的 divergent 口径）。验收必须显式排除终态。
- **残留缺口（待修）**：`COALESCE(ci.impact_direction, ac.ai_sentiment)` 两者都为 NULL 时方向缺失，**绕过 d6 闸门**按公式直接进 A/B。实测非终态 **45 条无方向**（8 条进 B、30 条 open），样本正是 `regulation`/`other`/「涨幅播报」这类实测负 alpha 类别。且这批行不会被 AI 后到的方向修正——`run_slow_g3g5` 候选集是「G3-G5 缺失」，d5 的二阶共振刷新只覆盖二阶通路。

### 参数回测与方向复核（2026-09-21）

- **样本口径**：前向样本 = `ret_source='klines+market_daily'` 且 `base_time + INTERVAL '72 hours' <= NOW()` → **741 条**（2026-09-11 ~ 09-15，5 个交易日）；`backtest` 行（base_time=`published_at`，219 条）**不与前向混用**。741 条中 **627 条来自同一 KOL 源**、集中在 5 天内 → 时间簇相关，统计力有限，结论按此打折。
- **d3 / d6 两个闸门被前向数据证实**：
  - `bullish` n=307 平均超额 **+1.42%**（对齐命中 53.4%）；`neutral` n=368 平均 **-0.02%**、中位 **-0.78%** → 占样本 50% 且中位为负，d6 降 C 正确；`bearish` n=48 平均 **-0.66%**（做空判对 70.8%）→ d6 置 invalid 正确。
  - `confirmed` **-2.17%**（对齐命中 40.9%）vs `weak` **+0.71%**（59.3%）→ d3 分层依据复现。
- **参数结构问题（观察项，暂不动）**：
  - **高分 ≠ 高收益**：composite 80+ 组 72h 平均 **-7.25%**（n=2，样本过少）；根源看因子分层：`resonance_score` 80+ 组 **-4.10%、命中 25%**，而 35-59 组 +1.57%/58.5%。resonance 权重 **0.30（最大项）**衡量的是「已被定价」，d3 已从**动作轴**（status）处理它，强度轴仍占 0.30 属重复计权。
  - **`technical_state` 是最强单调因子但权重仅 0.15**：up **+3.385/79.4%** > range +0.568/52.0% > down +0.151/34.5%（区分度 0.15→3.39）。
  - `base_strength` 单调但极平坦（1.39/1.30/1.50/1.53），区分度小。
  - **IC**：强度 **0.133**、方向对齐 **0.206** → 整链路（方向+强度）有效；已有校准权重把强度 IC 提到 0.145，但方向对齐 IC 持平（-0.001）。
- **tech_upgrade 方向复核（P1 疑点，已澄清）**：d4 把 `tech_upgrade` 改为 bearish，前向 40 条中 **36 条已按 bearish 结算**，中位 **-0.78%**、涨占比 **0.325** →「利好出尽」成立，**d4 无需翻转**。注意别把 `sign×excess` 的**方向对齐超额为正**误读成「看涨」——它是 `-excess`，为正恰恰说明看跌判对了。
- **`verify_event_direction.py` 口径修正**：原只筛 `excess_72h IS NOT NULL`，会混入 `backtest`（`published_at` 基线）行；已加 `ret_source` + 到期闸门（前向口径）。修正前后仅 **`other`** 一类结论变化（混口径 neutral → 前向 bearish，中位 -2.12%、涨占比 0.33、n=24），其余八类一致。
- **`other` 不补 bearish**：它是关键词未命中的兜底桶，随分类规则改进内容会漂移，且 n=24 样本小，硬绑 bearish 会掩盖真实利多事件。留作观察。

### 待办（需设计变更，勿盲目改）

- `run_signal` 候选集显式排除 `cr.resonance_state = 'pending'`，故 `signal_actionability` 的 `pending→watch` 映射实际只对二阶通路生效（直连通路 pending 行不会被重算）。
- `biz.catalyst_outcome` 存在两套 `base_time` 口径：collect 用 `signal.created_at`（前向追踪）、backtest 用 `published_at`（历史回放）。做校准/评估取样时不要混用，且建议加 `base_time + INTERVAL '72 hours' <= updated_at` 剔除未到期行。
- **resonance 权重与「已定价」语义重复**：`resonance` 占 composite 权重 0.30（最大项），但实测高分档（80+）为负 alpha（-4.10%、命中 25%）——它衡量的是「价格已同向反应」，d3 已把这件事放到动作轴（`status`）处理。是否降权 / 改成分档非线性（如已定价段不给正分），以及 `technical` 是否提权（最强单调因子但仅 0.15），需等前向样本积累到 2-4 周再定，勿用当前 5 天样本调参。
- **方向缺失（NULL）如何处置**：快通道产信号时 AI 尚未给方向，`COALESCE(ci.impact_direction, ac.ai_sentiment)` 为 NULL → 绕过 d6 进 A/B，且后续不被重算（实测非终态 45 条、8 条进 B）。二选一，需设计确认：① 无方向时不给档位（但快通道天生缺方向，会让 A 级邮件近乎绝迹，不可取）；② 慢通道按「AI 方向后到」重算该批信号（仿 d5 的刷新机制，限定近 N 小时新建 + 创建时无方向 + 非终态）。
- **`calibrate_catalyst_weights.py` 同型口径问题**：第 183-184 行只筛 `data_tier='L1'` + `excess_72h IS NOT NULL`，未筛 `ret_source` → 混入 backtest（`published_at` 基线）行。危害大于 `verify_event_direction.py`（已修）：它**写** `biz.catalyst_calibration`，而快通道用 `_load_calibration()` 把校准权重**注入实时打分**。