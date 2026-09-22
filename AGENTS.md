# 工作记忆（AGENTS.md）

## 常驻工作流约定（用户指令，必须遵守）

- **【铁律】改完代码 → 清理临时文件 → 自动推送**：每次完成代码修改后**无需用户再提醒**，必须自动执行：① 清理本次会话产生的临时文件；② `git add` → `git commit`（中文、`fix:` 前缀）→ `git push origin main`。
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
  - d6 方向闸门 + listing 收紧：**只有显式 bullish 保留 A/B**（利空→invalid；中性/方向缺失/未知值→tier 封顶 C）（详见下节）→ 本次提交

### 本轮新增（2026-09-18 只读校验 + 修复）

- **分层上线后校验（只读口径）**：open 1189（全 weak）/ watch 758（confirmed 293 + pending 465）/ invalid 163（全 divergent）；四项不变量（open 非 weak、invalid 非 divergent、watch 越界、终态异常）全为 0；慢 Alert 候选 11 取 top2；早报观察区候选 904；快提醒只推「本轮转为 open」集合；`notification_log` 快提醒历史 7 次（09-11~09-16）。
- **outcome 表脏数据（已修）**：`backtest_catalyst_impact.py` 无 now 闸门，`kline_close_at` 在窗口未到期时取到 K 线末尾，把「base 到最新」冒充 72h 收益——实测 60 行伪造（ret_4h = ret_24h = ret_72h，或 0.0000），且 base_time 用 published_at 与 collect 的 created_at 基线混用。已加「仅回放 14d 窗口走完的历史 catalyst」过滤 + 清理 189 行（按窗口逐个判到期，只清未到期列）→ 本次提交。全表「未到期却已结算」已归零。
- **信号滞后非系统性问题**：近 3 天新建信号滞后中位 **2.2h**（均值 75.5h 被历史回填尾巴拉高）；全量分档 2507 条在 0-6h 内。此前「平均滞后 153h」属口径误判。

### 采集停摆外部看护（2026-09-21）

- **看门狗退出码误报（已修 `deaf31f`）**：`check_scan_freshness.py` 原在「发现停摆」时 `return 1`（去重跳过/dry-run/已发告警三条路径），而 `task_manager.py` 把非 0 退出码判为任务失败、`scheduler.py` 随即发管理员失败邮件 → **每小时一封「任务失败」误报**（看门狗自己的停摆邮件另有 6h 去重）。现退出码只在脚本自身异常时非 0，停摆只走邮件+日志。
- **63h 采集停摆 + 缺口回填（2026-09-21 已处理）**：`scan_daemon` 于 09-18 09:45 UTC 死亡/部署被移除，K线+OI 断到 09-21 01:00 UTC（63h），池扫描因预热期无法出信号（主池需 ≥21 根 15m/1h；蓄势池 ACC 需 ≥12 个小时 OI 桶，即数据恢复后还要再等 5~21h）。已回填：K线 `phase_scan_klines.py --backfill-days 3 --force`（264,384 行 / 216 币 × 5m,15m,1h）+ OI `phase_backfill_oi_history.py --force`（95,691 行 / 191 币）；回填后主池 02:50、蓄势池 02:52 UTC 即恢复出信号。
- **`--force` 新增原因**：`phase_scan_klines.py` 的原续跑判断只看「最新 K 线是否新鲜」，缺口在中间时会被判为已覆盖而全部跳过（OI 脚本的 `--force` 同理），故缺口回填必须带 `--force`。

### 停摆「静默失败」根因修复（审计_盘面扫描告警_新两封_2026-09-21，已修 `225c757`）

- **根因（P0-A，物证级）**：容器日志设施断开 → 进程 stdout 变「已关闭文件」→ `_run_task_loop` 每轮第一行 `print(第 N 轮开始)` 在 `try` 内、`func()` 之前抛 `ValueError: I/O operation on closed file.` → 被 `except` 吞成「单轮失败」→ **`func()` 从未执行、数据零写入，而心跳（只走 DB）照常推进、进程完全健康**。04:37~05:25 UTC 全线停产 48min。留下的唯一物证是 `scan_squeeze`（offset 420s，重启后首轮最晚触发）的 `last_error` 残影。
- **决定性判据**：`last_ok_at` 冻结在 04:36~04:39 而 `last_run_at` 推进到 05:25 → 「任务在跑但连续 45 分钟一次都没成功」。此前**两条告警路径（daemon 内置 `_stall_parts` + 外部看门狗 `_collect_items`）都只读 `last_run_at`**，对这类故障集体无感（P0-B）。
- **修复 `225c757`（3 文件）**：① 新增 `_ResilientStream`/`_harden_streams`（`main()` 首行调用），写失败即丢弃，日志故障与业务彻底解耦；每轮首行 print 移出 `try`；② 两条路径判据改用 `last_ok_at` + `last_error`；③ `STALL_HEARTBEAT_TASKS` / `HEARTBEAT_MAX_AGE_MIN` 补齐全 9 任务（原缺 `scan_squeeze`/`scan_liquidation`/`watchlist_monitor`/`prune_scan_data`）；④ 连续 3 轮 `ok=False` → `os._exit(1)` 交 supervisord（线程内 `sys.exit` 只杀线程，必须 `os._exit`）；⑤ 僵尸实例驱逐：锁被占 + 全局心跳停滞 >10min → `pg_terminate_backend` 后重试取锁；⑥ 告警邮件补「疑似静默失败」分节与两类成因区分，看门狗渲染层新增「说明」列；⑦ `supervisord.conf` 的 `scan_daemon` 补 `stopasgroup`/`killasgroup`。
- **自测**：注入「已关闭 stdout」后 print/flush 不抛、`_harden_streams` 幂等、`closed` 恒 False；失败循环 **3 轮内 exit(1)**；`--dry-run` 覆盖全 9 任务；交叉判据/渲染单测 5/5。
- **待部署**：`225c757` 需重启容器才生效（`052a07f` 亦仍未部署）。部署后验证：`scan_squeeze` 等新纳入的 4 个任务出现在看门狗输出、`last_error` 非空时告警不再显示「正常」。
- **复验收口（`复验_盘面扫描停摆告警修复_225c757_2026-09-21.md`，本次修复）**：复验确认 `225c757` 已部署（看门狗输出 9 项 + `prune_scan_data` 阈值 4320m + 「首轮」标签三处对上）、10 项改动代码层全部成立，但指出 3 项 P1：
  - **P1-1（本次引入的风险，最重要）`os._exit(1)` 判据过宽**：原 `fail_streak` 不区分异常类型，模拟外部 API 抛错 3 轮照样自杀 → **把「外部依赖抖动」升级成「整个进程重启」**，牵连全部 9 个任务、重置错峰 offset、打出 OI 采样桶缺口（复验 P2-1）。而本机出口 IP 已被 Binance 判 418，5min 任务连续 15min 不可用即可触发。**已改为进程级判据**：`_write_heartbeat()` 返回 bool，只有「连心跳都写不进 DB」才累计 `fail_streak` 并自杀；外部依赖失败只记 `last_error`，由数据新鲜度 + `last_ok_at` 告警暴露。实测：外部依赖连失 6 轮且心跳正常 → **进程存活**；心跳持续失败 → **3 轮内 exit(1)**（即使 `func()` 每轮都成功）。
  - **P1-2 `_evict_zombie_lock_holder` 未限定 database**：`pg_locks` 是**集群级视图**，缺 `database` 条件会跨库误杀同 key 持有者。已加 `AND database = (SELECT oid FROM pg_database WHERE datname = current_database())`；prod 只读验证：锁行 `database=19522` = `current_database()` oid，新 SQL 命中 pid，反例 `database=0` 命中 0 行。
  - **P1-3 取锁失败直接退出可能耗尽 `startretries` 进 FATAL**：`os._exit(1)` 后 supervisord 在旧锁连接未释放时拉起新实例，取不到锁即 `return 1` 且耗时 < `startsecs=10` → 计为启动失败，3 次即 FATAL（`autorestart` 对 FATAL 无效）。已改为 `LOCK_ACQUIRE_RETRIES=3` / 间隔 2s 重试；僵尸驱逐分支保留（锁被占 + 心跳停滞 → 驱逐后立即取锁）。
  - **P2-1 已随 P1-1 缓解**（OI 桶缺口与重启时刻逐一对齐，根因是重启频繁）；**P2-2（`scan_oi_cvd` 单轮 134s 接近周期、建议心跳记 `last_elapsed_sec`）本轮不动**——需加列迁移，属观察项。
  - **无 DDL**：三项修复均为代码层，故未编 `fix_061` 迁移。
- **FIX-061 启动期取锁超时（工单 `工单_FIX-061_启动期取锁超时_2026-09-21.md`，已修）**：P1-3 只补了「重试次数」漏了「退出耗时下限」——取锁 3 次全失败的**总耗时实测 4.00s（端到端 6.66s）< `supervisord` 的 `startsecs=10`** ⇒ 记「启动失败」→ 耗尽 `startretries` → **FATAL（`autorestart` 无效）**；滚动部署窗口里旧容器仍持锁、新容器取锁失败即命中，旧容器销毁后**永久停摆**（与 09-18 那次 63h 同机制）。已在 `scan_daemon.py` 单文件落地方案 C：新增 `STARTUP_MIN_SEC = 12.0` + 模块级 `assert STARTUP_MIN_SEC > 10` + 失败分支 `_elapsed` 补 sleep；正常启动路径零影响。验收用离线三段式（**部署后 prod 观测不到**，改动全在故障路径）：源码核验（常量 1 定义 + 2 使用 + 断言）、注入测试（锁恒占 + 心跳新鲜 → 退出 **12.00s > 10s**；锁空闲 → **0.00ms**；把值改回 4.0 → 复现 **4.02s FAIL**，证明测试非空转）、配置一致性（12.0 > `startsecs=10`）。
- **口径更正（附录 A）**：P2-1 的 OI 采样桶缺口**不能**归为「已随 P1-1 缓解」——缺口与容器重启**逐一精确对齐**，而今日重启主力是**推送后的部署**（`scan_heartbeat.last_error` 全表为空 ⇒ 自杀分支从未触发），P1-1 只消除「自伤式重启」这一子类。根治方向：采样线程首轮**补采已过去的桶**，或把 OI 采样拆成独立 program。
- **`biz.scan_sampler_state` 非缺陷（附录 B）**：上轮「游标几乎不更新」是 **`updated_at` 覆盖假象**（同批币每轮 UPSERT 覆盖，全表只留最后一轮痕迹）；近 2h 更新集 221 ↔ `oi_cvd_snapshot(realtime)` 近 2h 采样集 221，**双向差集全 0**。两条卫生项（非缺陷）：`last_oi_usd` 528/528 全 NULL 属**废弃列**；`last_trade_id=0` 有 133 行（含 308 行停在 09-18 09:48，即 65h 停摆恢复后成交额跌破 `--min-vol-usd` 未再进采样集）⇒ 表随采样集变化持续留存历史币（528 vs 实际 221，膨胀 2.4×），建议加 TTL 清理。

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
- **残留缺口（已修 fix_056 + d6 扩展）**：`COALESCE(ci.impact_direction, ac.ai_sentiment)` 两者都为 NULL 时方向缺失，曾**绕过 d6 闸门**按公式直接进 A/B（实测非终态 45 条无方向，8 条进 B、30 条 open），样本正是 `regulation`/`other`/「涨幅播报」这类实测负 alpha 类别。
  - **口径扩展**：`signal.py::build()` 由「neutral 封顶 C」改为「**只有显式 bullish 保留 A/B**，其余（neutral / 方向缺失 / 未知值）一律封顶 C」。
  - **callers 补齐**：`phase_catalyst_backfill.py` 的 3 个 `build()` 调用点（step4/step6b/step7）原先都没传方向，规则扩展后会把它写的信号整片压成 C；已补 `impact_direction`（step4/step7 走 `COALESCE(ci.impact_direction, ac.ai_sentiment)`，step6b 用 `ac.ai_sentiment`）。
  - **「AI 方向后到」无需额外步骤**：`run_slow_g3g5` 候选集是「G3-G5 缺失」，快通道产出的信号必然被它重算一次并带入当时方向，已覆盖该路径。
  - **迁移 `fix_056`**（已对 prod 执行）：方向非 bullish 且非终态且 tier A/B → tier='C'，**8 行**（全部 open + tier B + 两个方向源皆 NULL）；备份表 `biz.catalyst_signal_nodir_backup_20260921`。执行后四项不变量（非 bullish 进 A/B、neutral 进 A/B、bearish 非 invalid、二阶进 A/B）在非终态集合上全为 0。
  - **单测**：direction = bullish→A/open、neutral/None/''/'weird_value'→C/open、BEARISH→A/invalid，6/6 通过。

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

### 盘面扫描告警复验 + 容器路径缺陷清扫（2026-09-21）

依据 `复验_盘面扫描告警修复_80774e9_2026-09-21.md`（6 项新发现），并顺带定位了快通道长期 FATAL。

- **复验 6 项全部闭环**
  - **P0-N1 冷启动误报（会反向压制真实停摆 6h）→ `c4fd8cc`**：`main()` 预写 `__daemon__` 进程启动标记心跳；心跳缺失或早于进程启动时，按启动时刻起算宽限。**外部看门狗一并覆盖**（复验建议的内存变量方案对独立进程无效）。
  - **P1-N2 `1000*` 前缀币四条链全断 → `4c551e1`**：`_base_symbol` 单点剥离升级为 `_symbol_candidates` 候选序列（原样 → 去 USDT → 去 `1000`/`1000000` 前缀；倍数前缀**只从裸形态剥离**）；新增 `_lookup_funding` 按候选取值（未命中写 NULL 不写 0）；`_load_funding_map` 为每行注册全部候选别名。实测 `1000FLOKIUSDT`/`1000PEPEUSDT` 费率由 MISS 转 HIT。
  - **P1-N3 `canonical_symbol` 不唯一 → `4c551e1`**：`core.asset` 有 1,866/18,868（9.9%）符号重复、单符号最多 18 行，`fetchone()` 会取到**别的币**。`_get_asset_id` 加 `ORDER BY market_cap_rank NULLS LAST, asset_id LIMIT 1`。⚠️ 候选序列是「原样优先」：`1000SHIBUSDT` 命中 core.asset 孤条目 12559 而非裸币 SHIB(1886)，该条无关联数据 → 退回「无共振」，不会串币。
  - **P1-N4 存量 `source` 误标 → `fix_059`（已对 prod 执行）**：判据 =「同 (exchange, symbol, 小时) 桶内**仅 1 行**且落在整点」即 1h 回填签名；**111,593 行** `realtime` → `backfill`（回标前全表 172,531 行皆为 realtime、`backfill` 0 行）；窗口 `ts < 2026-09-21 01:00 UTC`；备份表 `biz.oi_cvd_source_backup_20260921`、分类表 `biz.oi_cvd_hourly_backfill_20260921`（确认后可 DROP）。
    - **口径教训**：复验建议的「整分钟点」判据在本库**恒成立**（所有 `ts` 秒位都是 0），会把 100% 行判成回填，不可用。
  - **P2-N5 锁连接 `idle in transaction` → `4c551e1`**：`_acquire_singleton_lock` 取锁后补 `conn.commit()`（否则常驻连接长期 `idle in transaction` 阻挡 autovacuum）。
  - **P2-N6 蓄势池 OI 口径 → 注释收口**：**刻意不过滤 `source`**——ACC 需连续 12 个小时桶，1h 回填行正好补停摆/部署窗口的小时桶；过滤掉会让停摆后 ACC 长时间凑不满桶。
- **P2-3（`oi_dir` 最小阈值）不改**：`backtest_scan_scenarios.py:156-163` 同为二值无阈值 → 线上与回测一致，复验方已确认。
- **`stall_alert` 旧键行消失**：是我执行的 `fix_058`（刻意跳过 `fix_050` 第 1 步的合并——合并会把静默窗口重置到 07:11，吞掉真实停摆）。
- **`catalyst_fast_daemon` 长期 FATAL 的根因（本次最大发现）**
  - 容器内 **`workbench/*` 被 Dockerfile 扁平拷到 `/app/`**：`workbench/catalyst` → `/app/catalyst`、`workbench/*.py` → `/app/*.py`、`workbench/kol` → `/app/kol`、`workbench/market_rules.yaml` → `/app/market_rules.yaml`。
  - 脚本里硬编码 `SCRIPT_DIR.parent.parent / "workbench"` 在容器内 = `/app/workbench`（**不存在**）→ `ModuleNotFoundError: No module named 'catalyst'` → 启动 10s 内退出 → supervisord 连试 3 次进 **FATAL 后不再自动重试**（`supervisorctl restart` 才可能拉起）。
  - 修复共 **8 个文件**：`4a134b4`（快通道 daemon）+ `ecebcb9`（`calibrate_catalyst_weights` / `evaluate_catalyst_calibration` / `merge_duplicate_catalysts` / `init_news_media_catalyst_kols`）+ `052a07f`（`ingest_binance_news` / `phase_chain_insider_clusters` / `verify_prelaunch_factor`）。
  - 连带隐患：`calibrate_catalyst_weights.py` 读不到 yaml 时**先验权重被 except 静默吞成空 dict**，而该校准结果经 `_load_calibration()` 注入线上实时打分；`phase_chain_insider_clusters._load_yaml()` 同样静默返回 `{}`，`insider_cluster` 规则从未生效。`phase_meme_lifecycle.py:88` / `phase_meme_risk_labels.py:108` 的重复插入属无害冗余，未动。
  - **规范（后续新脚本必须遵守）**：定位 `workbench` 下资源一律用**候选探测**——`<root>/workbench/...` → `<root>/...` → `/app/...`，或按包标记文件 `catalyst/__init__.py` 判定。**不要**用 `Path(__file__).parent.parent.parent / "workbench"` 这类单点硬编码；也**不要**把字面量 `/app` 当唯一候选（本地无法自测）。既有正确写法参考 `catalyst_thesis_regen.py:37-38`、`build_market_snapshot.py:30-37`、`phase_catalyst_pipeline._setup_paths()`。
- **待部署**：`c4fd8cc` / `4c551e1` / `4a134b4` / `ecebcb9` / `052a07f` 五笔代码提交均未进容器（`46d6771` 是纯 SQL 迁移，已执行完）。
- **推论（待验证）**：快通道可能**已死很久**而非始于本次部署——若成立，AGENTS 里记的「信号滞后中位 2.2h」正是慢通道小时级节奏的表现。部署后可用该指标是否明显下降来验证快通道真的活了。

### 轧空胜负判定邮件修复（审计_轧空胜负判定邮件_NEARUSDT_2026-09-21，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_轧空胜负判定邮件_NEARUSDT_2026-09-21.md`。邮件本身数值与 DB 逐项一致、判定链自洽、已部署，问题出在**两个指标算错 + 判定器静默降级**。

- **P1-1 假 0（核心口径错误）**：`short_liq_ratio` 原用「相邻两桶 `*_liq_usd_1h` 相减 + `max(…,0)`」。但 CoinGlass `*_liq_usd_1h` 是**滚动 1h 窗口快照**，相减得到的是「新滚入 − 滚出」（平稳时≈0、回落时常为负）→ 被截断成 `0.0`，邮件显示 `空单爆仓 +0.000%` 而实际窗口内有 25.6 万 U 空头爆仓。
  - **决策：改用滚动窗口绝对值**（`_latest_liq_snapshot`，取最近一条 ≤15min 快照的 `long/short_liq_usd_1h / vol24`），语义＝「最近 1h 爆仓额」，不跨桶差分、不 `max` 截断。**未采纳审计建议的「上升沿累加」**——累加 `max(v_t−v_{t-1},0)` 测的是窗口**加速度**（平稳爆仓下一阶差≈0），同样错；这是该数据源的数学约束，非实现问题。
  - 阶段 1 入队确认、阶段 3 判定、`task_scan_liquidation` 文档、`coinglass_client` 文档、`fix_056` 注释四处同步更正。
- **P1-2 覆盖率闸门**：判定前算 `expect_buckets = floor(now/300) − ceil(peak/300) + 1`，`coverage = len(win_oi)/expect`；`< MIN_WINDOW_COVERAGE(0.6)` → **不判定**（track 留 tracking，不落 `scan_signal`、不发邮件），stats 加 `insufficient_coverage`。本例缺口 9/13 桶（0.15）会被拦下。邮件在覆盖 <100% 时披露 `数据覆盖 N/M 桶`。
- **P1-3 `None → 0.0` 兜底（会让「缺数据」满足「多头胜」前置条件）**：`evaluate_battle` 重写——`big_long_liq` 仅在 `long_liq_ratio is not None` 时为真；`profit_take`/`long_win` 需 `d_oi`、`cvd`、`long_liq` **三维均已确知**；`short_win` 需 `cvd`+`long_liq` 已知。缺失维度写入 `metrics['data_missing']` 并在 reason 标注，降级 `churn`（无方向）。**未新增 `data_insufficient` 结论枚举**（避免扩 `SQZ_*` 取值域/前端），以 churn + 标注 + 覆盖率闸门达成「拒绝给方向」。
- **P2-1** 邮件时间戳补 `UTC`（原 naive `datetime.now()`，容器 TZ=UTC 但未标注）。
- **P2-2 HTTP 移出 DB 块**：进主事务前先用一次短读取出 tracking 符号，预取 `_live_price` + `_fetch_long_short_ratio` 到 `px_map`/`lsr_map`，DB 块内不再发 HTTP。
- **P2-3 口径不一致只做披露**（分子 CoinGlass 全交易所爆仓 / 分母 Binance 24h 成交额）：邮件脚注明示，字段加 `liq_scope=coinglass_rolling_1h`；未改数据源。
- **P3 展示修正**：`or 0` 全删（`_fmt_ratio` 让 `None→'—'`、`0.0→'+0.000%'` 可区分）；标签改 `最近1h多空爆仓`；加「入队→峰值→判定」时间轴；大户多空比改 `base → now（Δ）`；主动买卖比带数据时点；配色按中文惯例（多头胜=红 `#ef4444`、空头胜=绿 `#22c55e`）。
- **P2-4 阈值偏低（`SQZ_SHORT_LIQ_RATIO_MIN` / `LONG_LIQ_RATIO_THR` 同为 0.00008）暂不动**：两常量本已解耦，值相同但需 1-2 天样本再标定，勿用单样本调参。
- **无迁移**：`squeeze_track.metrics` / `scan_signal.detail` 均为 JSONB，新增字段（`oi_cover`/`top_ratio_base`/`taker_ts`/`liq_scope`/`data_missing`）直接容纳；tracking 轮次的 metrics 改写为 `COALESCE(%s::jsonb, metrics)` 以免覆盖 entry metrics。
- **自测**：判定缺失口径 6 例（`None` 各维度 → 不产方向、`data_missing` 标注）+ 正常四分支 + 渲染（`None→'—'`、`0→'+0.000%'`、UTC、时间轴、配色）全绿；未对 prod 执行写操作。

### 轧空池标定与观测补漏（工单 SQZ-2026-09-21，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\工单_SQZ-2026-09-21_轧空池标定与观测补漏.md`（基线 `66ced4c`，落地于 `f9dbced` 之上）。逐条处置与「待拍板」决策：

- **SQZ-02（P1，已改）`LONG_LIQ_RATIO_THR` 0.00008 → 0.00040709**：
  - 只读复算：旧值 8e-5 落 **约 P75**、无条件越阈率 ≈20%；**「冲高回撤」条件子集越阈率 43%~52%（跨口径上界）** → 系统性屏蔽 `profit_take`/`long_win`。（⚠️ 原文此处写「7 天 / 8018 行 / 211 币」——**前提不成立**，见下条。）
  - 采 **无条件 P95 量级 = 4.07e-4**（条件子集越阈率降至 ≈15%~18%）；`SQZ_SHORT_LIQ_RATIO_MIN` 保持 0.00008；两者确认独立常量、不联动。
    - ⚠️ **口径更正（复验 §4-P3，2026-09-22）**：上文旧记「`SQZ_SHORT_LIQ_RATIO_MIN` ≈ 全样本 P90、越阈率 11.4%、位置合理」——**11.4% 是 5.92h 样本的瞬时分位**。其真实落点与越阈率**不在本文件留数字**（随 `liquidation_snapshot` 滚动窗口漂移，同一周内已见 P80 段 → P85 段位移 — 复验 D4），一律以 `workbench/calib_squeeze_liq_thr.py --days 1` **当次实跑**输出的「含 0 / >0 双率 + 各自 n」为准。⇒「位置合理」的表述**不再成立**，待定稿（阈值本身未动）。
  - **连带行为变化（已写进单测）**：NEAR 线上样本（`d_oi=-2.786, cvd=0.1538, long_liq=1.66e-4`）由 `churn` 翻为 **`profit_take`**——旧阈值把它当「大额多单踩踏」而屏蔽止盈分支；同理「仅 CVD 缺失」样本也归 `profit_take`。这是重标定的预期后果，不是回归。
  - ⚠️ 新值仍是**临时值**，待真实判定窗口样本积累后定稿；条件子集用「60min 内 ≥2% 拉升且已回撤 ≥2%」近似。
- **SQZ-01（P2，已改）**：`check_scan_freshness.py` 新增只读 `_collect_squeeze_health()`——队列占用 / 近 24h 入队 / 覆盖率拒判；越线（`tracking≥9` 或 `enq_24h≥20` 或 `reject≥3`）才渲染。实测 24h 入队 3、队内峰值 1/12 ⇒ **「队列易打满」不成立**，入队门槛不动。
- **SQZ-06（P1，已加观测）重启丢桶**：同一函数检测「`__daemon__` 近 2h 内启动过 + 该窗口 OI 桶数 < 期望×0.9」；实测命中（07:01 UTC 重启，2h 窗口 OI 桶 17/24）。**仅告警，不改采样侧**（补采另立工单，遵工单建议）。健康项会**独立发提示邮件**（专用去重键 `squeeze_health`，**刻意不复用** `scan_stall`——否则健康提示会与真实停摆互相抑制 6h）。
- **SQZ-03（P2，已改）尾部连续性**：覆盖率只看「数量」（前段齐/尾部断可骗过闸门）⇒ 增判 `oi_lag_sec > 2×300s`，拒判 reason=`判定窗口尾部 OI 桶缺失…`、metrics 记 `oi_lag_sec`，邮件披露 `OI滞后 Ns`。
- **SQZ-04（P2，已加）** `05_代码与脚本/workbench/test_squeeze_battle.py`：16 常量 + 7 注入用例 + 边界用例 + `data_missing` + 渲染段 `or 0` 护栏 + None/0 渲染 + `oi_lag_sec` 披露，**41/41 通过**（`python workbench/test_squeeze_battle.py`）。
- **SQZ-05（P2，无需改码/无迁移）**：`scan_heartbeat.metrics` 不动。事务敞口澄清——`task_scan_squeeze` 的读阶段已 `commit`、HTTP 已前置、写事务只在落库处开启（中间纯内存 compute 不 execute）⇒ **写事务不含 I/O**（P2-2 的"12s 事务"实为整轮耗时）。
- **未采纳/未做**：❌ 改调度 offset（工单证明 `offset` 与 `oi_cvd` 同相位，ε 越大读到越旧，消费侧对齐才稳）；❌ 采样侧首轮补采（另立）；❌ 新建 `tests/` 目录（循 `workbench/test_*.py` 惯例）；❌ 迁移（`fix_060` 未占用）。
- **待部署**：`scan_daemon.py` / `check_scan_freshness.py` 改动需重启相应进程（容器 `scan_daemon` + scheduler）后生效。

### SQZ 复验遗留缺陷处置（复验_SQZ工单_00f3513_2026-09-21，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_SQZ工单_00f3513_2026-09-21.md`。复验判定「代码质量高、单测真实（41/41 独立重跑）」，但留 6 项：P1-a/P1-b（🔴）、P2-c/P2-d（🟠）、P3-e/P3-f（🟡）。本轮全部落地（**均为代码/文档层，无 DDL、无阈值变更**）。

- **P1-a（🔴 已修）「7 天标定」前提不成立**：`liquidation_snapshot` 标定时全表只覆盖 **5.92 小时**（`min(ts)=09-21 02:00 UTC`），故 SQL 里的 `INTERVAL '7 days'` **形同虚设**，取到的是「表开始写入至今」的全部行 ⇒ 行数随时间增长（8018 / 10080 / 25296），**「8018 行」是瞬时值不是样本量**。已把 `squeeze.py` / 设计文档 / 本文件的「7 天 / 8018 行」表述改为**实测跨度口径**，并要求标定输出自带 `min/max(ts)`。本次复探（09-22 08:5x CST）跨度已涨到 **22.75 小时 / 38998 行 / 527 币** ⇒ 该表在持续增长，「7 天」永远取不到。
- **P1-b（🔴 已修）标定不可复现** → 新增 `05_代码与脚本/workbench/calib_squeeze_liq_thr.py`（只读）：把**窗口/基准/回撤/振幅口径写死在 `variant_*` 函数**里，每次运行打印样本时间边界 + 无条件分位 + 3 个条件子集变体的越阈率。复验按同一文字口径独立实现得 `n=997 / 51.96%`，与原始的 `n=1202 / 43.2%` 对不上（n 差 17%、越阈率差 8.8pp），正是「口径只写在自然语言里」的后果。
- **P2-c（🟠 已修）判据从「点越阈率 <20%」升级为「跨口径上界 <20%」**：脚本跑 3 变体（A 振幅 hi/lo + 回撤 / B 振幅 close + 回撤 / C 仅回撤）取 max 作判据输入。**⚠️ 首次运行即 FAIL**（跨口径上界 **34.74%** > 20%，见下条「样本仍在漂移」）——这不是脚本错，而是原来那个「14.6% <20%」**本就口径脆弱**：换一个同样合理的回撤口径就越线。**阈值本身不动**（属复验 §9 待拍板项，须用户定）。
- **P2-d（🟠 已修）判定窗口「中段空洞」双判据都拦不住**：覆盖率只看**数量**（13 桶缺 2 个中间桶 → `11/13=0.846` 通过）、`tail_gap` 只看**右端** ⇒「前段齐、中段缺、尾部齐」无人管，而重启/`os._exit(1)` 杀的恰恰是**中间某桶**（快照不可回补）。已在 `scan_daemon.py` 新增 `MAX_MID_GAP_BUCKETS = 2`：统计 `[first_bucket, last_bucket-1]`（右端正在采集的桶归 `tail_gap` 管）内最长连续缺桶，≥2 即拒判，reason=`判定窗口内连续缺桶 N 个…`（仍以 `判定窗口` 开头 ⇒ 看门狗的 `reason LIKE '判定窗口%'` 计数自动涵盖），metrics 增记 `(head|mid)_gap_buckets`。
  - ⚠️ **本轮后续（复验 P2-d/P3，2026-09-22）**：常量与实现已**下沉到 `squeeze.py`**（`MAX_MID_GAP_BUCKETS` / 纯函数 `window_gate(present, first, last) -> (ok, head_gap, mid_gap)`），`scan_daemon` 只调用；左端缺桶单列 `head_gap`（`base_oi` 会落到窗口之外，ΔOI 基准失真），文案改「窗口内连续缺桶 N 个（左端起 h 个 / 中段 m 个）」。C1~C10 已入单测（等价性逐例对齐旧实现，纯增量 0.6%）。
- **P3-e（🟡 已修）注释把瞬时分位写成常量**：`squeeze.py` 原写死 `P75=6.09e-5 / P90=2.27e-4 / P95=4.07e-4`，而表在增长、这些数字每天都在变（复验同口径实测 `5.64e-5 / 2.07e-4 / 3.80e-4`）⇒ 已删掉易变数字，只保留**稳定结论**（旧值落约 P75、条件子集越阈率 43%~52%、P90 不采用），分位数值交由标定脚本输出。
- **P3-f（🟡 已修）`reject` 计数的语义与注释不符**：`SQUEEZE_REJECT_WARN` 的注释写「近 7 天…≥3 次」，实际 SQL 统计的是**当前仍处拒判状态的 track 行数**（同一 track 被拒 10 轮只算 1，也没有 7 天窗口）。已改注释与告警文案为「拒判中 N 条 track（同一 track 多轮被拒只计 1）」——**语义对齐而非改实现**（真做「次数」需新增落库字段，属另立项）。
- **⚠️ 本轮重大副产品 —— 已被下一轮复验证伪（保留原文并更正）**：脚本首次运行显示**结论对取样时段极度敏感**——同一公式，5.92h 样本（09-21 02:00~08:00 UTC）得无条件越阈率 ≈20%、入队侧 short ≈11%；22.75h 样本得 **无条件 40.87% / 入队侧 short 35.10% / 条件子集 30.8%~34.7%**。原结论「⇒『无条件分位』标定方式本身不稳健」**不成立**：40.87% 是 **`asset_klines(5m)` 当时缺 14h**（09-21 10:00~23:59 停摆期）导致**分母被少算**的产物——用同一份爆仓数据 + 减去 14h 空档的分母重构，三项数字全部命中（同一量级的 40.21% / 34.34% / 34.89%）；补齐分母后三项全部回落（漂移仅在「20% → 约 24.6%」量级）。**故「回退 P90」「改按条件分布重标定」的动因都不成立**，真正要修的是标定脚本的分母自证能力（已修，见下节）。真问题变成**判据裕度**：完整分母下条件子集跨口径上界与判据线 20% 之间**只差亚 pp 级**、去未来函数口径整体上抬约 +0.9pp（**具体数字不留档，以脚本实跑为准** — 复验 D4）。**未改阈值**。

### 盘面异动告警邮件修复（审计_盘面异动告警邮件_2026-09-21，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_盘面异动告警邮件_2026-09-21.md`（对象 = 主池「🚨 盘面异动告警」邮件，非 squeeze）。渲染层前轮已修干净，本轮打数据层/信号层。

- **P0-1（A，已修）告警静默丢失**：`_load_alert_candidates` 的 20min 窗口在 alert 任务停摆时会把 high 信号永久滤掉（`alerted_at` 恒 NULL，与「超窗作废」不可区分）。在 `scan_daemon._stall_parts()` 增只读检测：`confidence='high' AND alerted_at IS NULL AND signal_ts < NOW()-30min`（main / accumulation BRK）→ >0 即以「未告警即超窗的 high 信号 N 条」并入既有停摆告警邮件（零新通道）。**未做 B（补发）**——需定义「补发有效性」边界，属待拍板。
  - **⚠️ 检测必须有界（否则它自己就是永久误报源）**：首版漏了回溯上限，而库里至今留着 09-16 那次停摆的 **8 条陈年 `alerted_at IS NULL` 行** ⇒ 停摆告警会被这批行永久钉住，6h 去重期一过就再发一封（实测：无界 =8 条 / 有界 6h =0 条）。现加 `LOST_SIGNAL_LOOKBACK_H = 6`（`signal_ts > NOW()-6h`）。
  - **同时排除「冷却跳过」行**：同币 12h 内已告警时 `task_scan_alert` 是**刻意**跳过且不写 `alerted_at`，属设计行为而非丢失 ⇒ SQL 加 `NOT EXISTS(同 symbol 且 alerted_at ∈ [signal_ts-12h, signal_ts+10min])`。
  - 教训（与 P0-N1 冷启动误报同类）：**任何并入告警邮件的"计数 > 0"判据都必须同时给出上界与豁免条件**，否则告警通道会被陈年数据拖成噪声源。
- **P0-2（已修）共振计数不可信**：
  - `_get_resonance` 催化剂段改为「归一标题去重 + 方向构成」：新增 `_norm_title()`（去 `火星财经：/ChainCatcher 消息，/PANews 9月18日消息，` 等源前缀，取最早分隔符且前缀 ≤16 字符，保留字母数字汉字，取前 40 字）后去重；返回 `catalyst_dir{bullish,bearish,neutral}` 与 `catalyst_raw`。
  - 邮件渲染 `催化剂 N（X多/Y空/Z中）`，当「做多但空>多」或「做空但多>空」时追加红色 `⚠️ 共振方向与结论相悖，请复核`。
  - 实测：XMR `raw=4 dedup=4 全 bearish` → 邮件会出现利空警示；APT `raw=7 dedup=4（1多/2空/2中）`。⚠️ 仅解决**同语言多源转载**（中↔英译文、`APT=高级持续性威胁` 同形异义误标仍未解决，需 classify 侧消歧）。
- **P1-1（已修）做空信号永不告警**：`direction=down` 原最高只能 medium，而候选只要 high ⇒ 做空通道数学不可达。改为对称：`down + oi_dir=up`（S3 空头扎实）时 `high if short_fav else medium`；其余 down 仍 `medium/low`。**注意这**会首次让下跌行情出邮件（`regime.short_fav` 为闸门）。
- **P2-1/P2-2/P2-6/P2-7（已修）渲染**：市场环境（`btc_1h/fgi/cap_trend`）由「每卡重复」提为标题下单行；`lv3_1h` → 「1h 级异动」并加图例；补 `<!DOCTYPE html>`/`lang=zh-CN`/`<meta charset>`/`color-scheme:light`，所有文本节点显式 `color:#111`（防深色模式浅底不可读），`h2 margin:0 0 6px`；卡片加 `HIGH` 徽章。
- **邮件 v2 排版（审计 §三，本次落地）**——用户授权「需要排版的地方由你决定」，全部在 `_render_alert_email` 渲染层，无 schema/生产者改动：
  - **卡片排序 + 相对强度条**（§三.6）：新增 `_alert_strength()` / `_strength_bar()`，基量 = `量比 × |OI 增速|`，共振方向与结论一致 ×1.15 / 相悖 ×0.75，CVD 与结论同向 ×1.05；按分降序 + `①②③` 序号 + 五格 `▉░` 条（按本封最高分归一）。**只做本封邮件内的相对强弱**（绝对阈值无基准，且 P2-5 已警示均值会被离群值绑架），图例明确写「非胜率」。实测：XMR 51.1 > AAA 36.2 > ZZZ 9.0 > EPIC 7.2。
  - **卡片层级**：主行（序号/币/`HIGH` 徽章/强度条/池·级别）→ 场景行（`S1 多头进攻 ↑ 做多` +涨幅，按中文惯例多头=红 `#ef4444`、空头=绿 `#22c55e`）→ 指标行 → 机制行 → 共振行；原「一行塞 5 个字段」拆开。
  - **CVD 机制标签**（P1-3 的可做部分，当时金额列缺失故不做幅度，金额已在下方「剩余项」补齐）：`CVD` 与结论相反时判读机制——做多 + CVD down → 「杠杆驱动（OI 增而现货主动卖），无现货承接」（琥珀色）；做空 + CVD up → 「跌势中有现货承接，防反抽」（蓝色）。
  - **`n/a` 与 `0` 区分**（P2-3 渲染侧）：`_get_resonance()` 增 `asset_linked`，未关联 `core.asset` 时催化剂/KOL 段渲染 `n/a`（无从查询 ≠ 无事件），页脚披露口径。
  - **费率加年化**（§三.4）：`当期 ×3×365`（币安 U 本位 8h 结算），图例注明；不兜底 0（无数据时后续改为 `n/a（未覆盖）`，见下）。
  - **踩坑**：图例文案里写了 markdown 的 `**相对**`，星号原样进了 HTML ⇒ 邮件正文出现 `**`。**HTML 邮件文案不要用 markdown 强调符**。
  - **自测**：`_probe_layout.py` 23/23（排序/条格数恒定 5×N/层级/两种机制标签/同向不打标签/`n/a`/年化/缺失兜底/既有修复不回归/空列表不炸）；`test_squeeze_battle.py` 41/41 不回归。
- **剩余项已全部落地（用户「剩下的问题也按你的意思办」→ 提交 `dd8379d`，2026-09-21）**：
  - **P1-2 regime 阈值收紧**：`btc_1h ±1.0` / `fgi 25·75` / `cap_trend ±1.0` → 常量 `REGIME_BTC_1H_THR=0.5` / `REGIME_FGI_FEAR=28` / `REGIME_FGI_GREED=72` / `REGIME_CAP_TREND_THR=0.5`。标定依据（近 7 天 `up + OI↑` 触发子集，n=98，用 `context_tags` 回放）：原阈值下**唯一真正降级的维度是 `cap_trend`**；四组候选组合的 `high` 条数**均为 60/98** ⇒ 收紧无回归风险（该子集 `btc_1h` 全在 ±0.5% 内、`fgi` 最低 63），作用是让**下一次**真实回调/贪婪时能提前否决。同时把**否决原因**（`多头环境受限（BTC1h-0.80%/市值-2.00%）`）写进 `context_tags` 并随环境行渲染。
  - **P1-3 CVD 幅度**：生产者补落 `cvd_usd`（近 2 桶净额）+ `cvd_ratio`（净额 / 同窗口成交额）；`_compute_l2` 的窗口口径与 `cvd_dir` 一致（`oi_rows[-OI_RISE_BARS:]`），无数据 **NULL 不兜底 0**。渲染 `CVD down -320.0K（占比 -12.0%）`（新增 `_fmt_usd`，M/K 缩写）。
  - **P1-4 BRK 死通道根因（物证级）**：不是「从未触发」，是**两处数学不可达**——① 区间极值原先把触发条本身算进去（`k1h[-(OI_HOURS+1):]`），而 `_detect_brk` 比较的 `close = k1h[-1].close_px` **正是取极值的同一集合成员** ⇒ `close > hi` / `close < lo` 恒假；② 库里最后一根 1h 是**未收盘的当前小时**（实测 08:09 UTC 时 `open_time=08:00`），其 `quote_vol` 只累积了该小时的一部分 ⇒ 同一根 K 线在不同时刻判定结果不同、**不可复现**。（⚠️ 原文此处写「125 个 active ACC 币模拟最高 vr=0.93 ⇒ `BRK_VOL_RATIO=3.0` 永不可达」——那是 08:09（整点后 9 分钟）的单次快照，实测该值随采样分钟漂移 min 0.194 / 中位 0.789 / max 8.263，既有 < 1 也有 > 3，**该论证不成立**；正确理由是「不可复现」。见下方「告警邮件遗留工单处置」DOC-1。）修复：只用**已收盘**条（`now - open_time >= 1h`），区间取触发条**之前**的 `OI_HOURS` 根；`_detect_brk` 新增 `max_age_min`（已收盘条年龄天然多 0~60min ⇒ 阈值放宽一个周期，否则每小时前 40 分钟误判「陈旧」）。BRK 同时落 `trigger_price = break_px`。
  - **P1-5 跨池互斥 60min**：新增 `CROSS_POOL_MUTE_MIN=60` + `_cross_pool_recent(conn, symbols, pools)`（两侧告警都落 `biz.scan_signal.alerted_at`，单点可查、无需新表）。main 侧查 squeeze 已告警符号、squeeze 侧查 main，命中方**只留痕不发信**：写 `alert_suppressed_at` / `alert_suppressed_reason`（新增 `_mark_alert_suppressed`），`_stall_parts` 的丢信号检测同步加 `AND s.alert_suppressed_at IS NULL`（否则被刻意抑制的行会被算成「丢失」）。实测窗口依据：近 14 天跨池同币告警恰好 2 例（XMR 39.0min、龙虾 41.2min），均落在 60min 内；边界复核（08:20 UTC 时）XMR squeeze age=56min 仍命中、龙虾 age=122min 出窗、收到 30min 时 XMR 亦出窗。
  - **P2-3**：费率无数据由 `-` 改为 `n/a（未覆盖）`（实测近 30 天 high 告警 53 条中费率有值 40 = 75%，`cvd_dir` 53 = 100% ⇒ 绝大多数 `-` 是「没这个数据」而非「费率为 0」）；页脚统一口径「n/a = 该维度无从查询，≠ 数值为 0」。
  - **P2-4 主池入场价/失效位**：`task_scan_main_pool` 补算并落库 `trigger_price`（触发周期最新收盘）+ `stop_loss_pct`（该周期近 `LOOKBACK_BARS_MAIN+1`=21 根反向极值：多头取最低价、空头取最高价；触发条自身 `low ≤ close ≤ high` ⇒ pct 恒 ≥ 0）。渲染「失效位 跌破 604.215880（-2.30%，入场 618.440000）」。
  - **P2-5 历史先验**：新增 `_scenario_priors`（`PRIOR_HORIZON_H=12` / `PRIOR_LOOKBACK_DAYS=30` / `PRIOR_MIN_N=10`）——样本 = 近 30 天 `alerted_at IS NOT NULL`、非 invalid、且 `signal_ts+12h` **已过**的主池信号；基线/终点用 `signal_ts`、`signal_ts+12h` 当时最后一根 1h 收盘价（索引 `idx_asset_klines_symbol_interval_ot` 支撑相关子查询）；**方向对齐**收益（做多 +pct / 做空 -pct），只输出**中位 / 胜率 / 样本量**（均值会被 AKEUSDT +147.99% 这类离群值绑架）。实测 S1 n=31 / 中位 +1.21% / 胜率 64.52%，S2~S4 因 n<10 被丢弃。
- **迁移 `fix_060_alert_cvd_and_cross_pool.sql`**（已在 prod 执行）：`biz.scan_signal` 幂等加 `cvd_usd numeric(20,2)` / `cvd_ratio numeric(12,6)` / `alert_suppressed_at timestamptz` / `alert_suppressed_reason text` + 4 条列注释。
- **自测**：本轮剩余项 `_verify_remaining.py` **28/28 通过**（P1-4 四例含「旧口径恒不可达」的数学证明；P1-3 四例含 NULL 不兜底；P1-2 三例用桩按真实 execute 序列喂结果集；P2-5 prod 只读；P1-5 prod 只读含 30/60min 边界；渲染 9 例含 `**` 护栏）；`workbench/test_squeeze_battle.py` 41/41 不回归；`py_compile` 通过。⚠️ 自测踩坑两条：① `_build_regime` 的桩必须按 **btc→fgi→市值** 的真实 `execute` 顺序喂结果集，否则「无报错但全部为默认 True」；② 断言字面量要与渲染精度一致（`_fmt_num(barrier, 6)` → `604.215880`，不是 `604.216`）。
- **旧的未改项（已在上面闭环）**：P0-1 的 B 方案（补发）仍未做（需定义「补发有效性」边界）；BRK 分支**保留**，靠任务 stats 的 `BRK` 计数观测是否真出信号。

### 告警邮件复验回修（复验_告警邮件修复_279565e_4950cb6_9c48c63_2026-09-21，本次提交）

来源：`复验_告警邮件修复_279565e_4950cb6_9c48c63_2026-09-21.md`（对象 = 上节「盘面异动告警邮件修复」）。复验判「已部署 + 10 项改动成立」，但指出 **2 项本轮新引入的 P1**（都比原问题更严重）与 4 项 P2。全部落在 `scan_daemon.py`，**零 DDL、无生产者改动**。

- **P1-N1（已修）`_norm_title` 40 字符截断 ⇒ 系统性误合并**：英文新闻以固定模板开头（`According to the announcement from Binance, the …`），前 40 个字母数字字符全是模板、真正内容在其后 ⇒ 把**不同公告并成一条**，且方向构成失真会让「利空为主」的红色警示**反向漏报**。去重键 `[:40]` → `[:80]`。本机实测（`catalyst_impact ⋈ asset_catalyst`，近 30 天 4,394 行）：旧口径**误合并 208 组 / 吞掉 249 条独立新闻**、**77/642 = 12.0%** 资产条数被低估 → 新口径**误合并 1 组**（唯一 key 2856，完整归一 2857，几乎无损）。
- **P1-N1 附（已修）源前缀剥离由纯长度启发式改内容判据**：原 `0 < i <= 16` 只看长度，实测 **108 个片段属正文却被剥掉**（`A whale address，` / `Strategy，`）。新增 `_is_source_prefix()`：仅「源名白名单（火星财经/ChainCatcher/BlockBeats/PANews/动察 Beating/Binance…）+ 消息类标记（消息/快讯/讯/报道/日报/公告）」「元数据前缀（作者/撰文/原文标题/来源/编译）」「英文日期（`On Sep 18`）」才剥。
- **P1-N2（已修，方案 A）卡片「催化剂 N」与括注方向合计口径不同源**：N 取明细列表（`if len < 4`，≤4），括注取去重全量（≤20）⇒ 会渲染「催化剂 **4**（9多/1空/9中）」= 19 条（全库 282 币中 35 个、12.4%）。新增 `_catalyst_total(cd)`，卡片/标题统一走它（原「明细行标注前 4 条」无从落地——明细**根本不渲染**）。
- **P2-N6（已修）标题补方向构成**：`_alert_title` 原只报总数（且 `len(catalyst)` 同样是截断值），实测「含共振 8 条」里 6 条是利空/中性，读起来却像「多重共振支持」⇒ 现为 `含共振 N 条，催化剂 X多/Y空/Z中`，条数改用 `_catalyst_total`。
- **P2-N3（已修）CVD 机制标签按 `oi_dir` 分两支**：「价涨 + 现货主动卖」有两种**相反**机制，原文案一律断言「OI 增」⇒ 空头回补（OI 降）场景说反（30 天内 `up + oi=down` 已 13 条，现皆 medium，形态一变进 high 即说反）。现 `oi_dir=up` → 杠杆驱动；`oi_dir=down` → 空头回补/多头离场推涨（持续性存疑）；OI 方向未知 → 不作机制断言。
- **P2-N5（已修，用户选「log 归一 + 条旁显示分数」）强度条**：线性归一把第 2/3 名压成同格（EPIC 7.26 与 APT 8.81 同为 1 格）⇒ 改对数域归一（实测 5 / 2 / 3 格，线性为 5/1/1），条后附原始分数，图例同步说明。
- **P2-N4（已修）`--run-once` 绕过单实例锁且不写心跳**：原分支在取锁**之前**直接 `func()` ⇒ ① 可与常驻实例并行跑同一任务，对 `scan_alert`（发信 + 回写 `alerted_at`）会**重复发信**；② 不写心跳 ⇒「跑过没有」与 `biz.scan_heartbeat` 脱节（复验即因此拿到过「未部署」的假信号）。现移到取锁之后、成功后补写任务心跳；docstring/help 注明「需常驻实例已停」。
- **离线验收（AST 提取真码 + 只读 prod 回放）**：`_verify_alert_fix.py` 全绿 —— 口径统计（见上）；渲染回放：`催化剂19（9多/1空/9中）` 且不再出现 `催化剂4（…）`；标题 `含共振 20 条，催化剂 9多/1空/9中`；CVD 四例（OI 升/降/未知/做空）文案各自正确且互不串；强度条 (68.4, 7.26, 8.81) → (5, 2, 3) 格；`top=0` / 空共振不炸；`run-once` 静态位次在取锁之后。`py_compile` 通过。
- **⚠️ 重踩同一坑（已修）**：P2-N5 图例文案又写了 markdown 强调符 `按最高分**对数**归一` ⇒ 会原样渲染出 `**`（与上轮 `**相对**` 同类）。已去除；并加 AST 护栏脚本（提取 4 个渲染函数的字符串字面量、排除 docstring，断言无 `**`）。**教训固化：邮件 HTML 只准用 `<b>`/`<span style>`，不得用 markdown 强调符，改渲染文案后跑一次护栏。**
- **`expire_signals` 常驻首轮：复验时未验，现判「非缺陷」**：08:20~08:26 UTC 探测 `biz.scan_heartbeat` 仍 10 行（9 任务 + `__daemon__`），因当日**第 15 次重启**（08:22:47）把 offset 900s 重置 ⇒ 首轮应在 08:37:47，尚未到点（`expire_signals` 已在 `TASK_DEFS`，产物 `expired_at` 132 行、Δ 精确 24h 已证语义正确）。

### 待办（需设计变更，勿盲目改）

- `run_signal` 候选集显式排除 `cr.resonance_state = 'pending'`，故 `signal_actionability` 的 `pending→watch` 映射实际只对二阶通路生效（直连通路 pending 行不会被重算）。
- `biz.catalyst_outcome` 存在两套 `base_time` 口径：collect 用 `signal.created_at`（前向追踪）、backtest 用 `published_at`（历史回放）。做校准/评估取样时不要混用，且建议加 `base_time + INTERVAL '72 hours' <= updated_at` 剔除未到期行。
- **resonance 权重与「已定价」语义重复**：`resonance` 占 composite 权重 0.30（最大项），但实测高分档（80+）为负 alpha（-4.10%、命中 25%）——它衡量的是「价格已同向反应」，d3 已把这件事放到动作轴（`status`）处理。是否降权 / 改成分档非线性（如已定价段不给正分），以及 `technical` 是否提权（最强单调因子但仅 0.15），需等前向样本积累到 2-4 周再定，勿用当前 5 天样本调参。
- **方向缺失（NULL）如何处置**：已修（见「d6 部署验收」节）——口径改为「只有显式 bullish 保留 A/B」，NULL/未知值一律封顶 C。
- **`calibrate_catalyst_weights.py` 同型口径问题**：已修 —— 第 183-186 行补 `ret_source='klines+market_daily'` + `base_time + 72h <= NOW()`。该脚本**写** `biz.catalyst_calibration`，会被快通道 `_load_calibration()` 注入实时打分，混口径样本会直接污染线上权重，故必须保持前向口径。副作用：校准样本变小，达不到 n≥30 门槛的维度保持默认权重（更保守，安全）。

### 告警邮件遗留工单处置（工单 `待修复工单_告警邮件遗留缺陷_2026-09-21.md`，2026-09-22）

工单基线 `c80ca29`（早于 `8ca1258`），列 13 项；其中 **6 项已被 `8ca1258` 覆盖**（P1-1 去重键 / P1-2 口径同源 / P2-1 CVD 分机制 / P2-2 run-once 入锁 / P2-3 标题方向 / P2-8 强度条对数归一）⇒ 本轮实际待办 **8 项**：P0-1、P2-4、P2-5、P2-6、P2-7、P2-9、DOC-1、PROC-1。

**交付与复核状态（2026-09-22 更新）**：本轮代码改动已由并发进程（commit 作者 `fai婷酱`）连同其 `breakout_px` 工作一并提交 —— `44b407f`（延续确认 `active→confirmed` + `breakout_px` 拆列）、`62b7642`（守护进程永久挂死根因）、`ac1660d`（失效位夹带重标定），当前 `scan_daemon.py` / `check_scan_freshness.py` 工作区**干净**。已逐项只读复核：11 个任务的 `STALL_HEARTBEAT_TASKS`（daemon）与 `HEARTBEAT_MAX_AGE_MIN`（看门狗）**双双含 `confirm_signals`**（新任务未留盲区）；`fix_061` **已落 prod**（`breakout_px` 列存在）；`confirm_signals` 心跳在跑（`round_count=1`）。

- **P0-1（P0，已改未提交）`expire_signals` 常驻首轮几乎永不触发**：`_run_task_loop` 语义是「先 `sleep(offset)` 再循环」，offset 即**首轮最早时刻**；而容器重启周期实测 **493/593/719/911 秒（约 15 分钟）**，`expire_signals` 的 offset 恰为 **900s** ⇒ 与进程存活时长同量级，每次重启都把首轮推到期前。**连带**：两条停摆监控（daemon `_stall_parts` + 看门狗 `check_scan_freshness`）对「本实例尚未跑完首轮」一律给 **3×周期** 宽限（1800s→90min / 86400s→4320min），远大于 15 分钟重启周期 ⇒ 每次重启刷新宽限，**「线程从未启动」被无限期掩盖**（两条监控同时静默）。
  - 修法：offset **900→90**（保证每次重启都能跑完首轮）；首轮宽限改为 `min(3×周期, FIRST_ROUND_GRACE_MAX_MIN)`，**daemon 与看门狗同名常量必须同值**（`=30.0`）；daemon 侧加模块级断言 `FIRST_ROUND_GRACE_MAX_MIN > max(offset)/60 + 5`（改 offset 时强制复核）。
  - 物证（只读 prod，09-22 08:11 CST）：`biz.scan_heartbeat` 中 `expire_signals` 曾**长期 0 行**（表 10 行 = 9 任务 + `__daemon__`）；09-22 00:45 UTC 复探该行已存在且 `last_ok_at = 00:30:48 UTC`（**晚于**新实例 `__daemon__` 启动 00:19:56 UTC ⇒ 本实例确已成功跑过）。⚠️ 但**不能据此断定 offset=90 已生效** —— 见下节 ③：00:21:26（offset=90 的应跑时刻）当时未跑，旁证容器可能仍是未含 P0-1 的旧构建；`round_count` 的语义（是否跨重启累计）未确认，勿用作轮次证据。
  - **⚠️ 自我更正（09-22 复探后推翻上一版的判断）**：曾据「`status='active'` 积到 894 行 / `expired` 仅 132 行」推断「生命周期仍靠 `--run-once` 驱动」——**该推断错误**。按 `pool`/`scenario` 分组复探：894 行**全部未超期**（821 条 `accumulation/ACC` 走 **7 天** TTL，最老 09-16 09:30 才 5.7 天；`main` 与 `accumulation/BRK` 全在 24h 内），**按 `task_expire_signals` 的 WHERE 逐字模拟应更新 0 行**，与实况一致 ⇒ 无积压、无需修复。**教训：判断「巡检任务是否失效」时，必须先把各池 TTL 代入分组核算，单看 `status` 计数的总量会被「长 TTL 池占多数」误导。**
- **P2-4（P2，已随 `ac1660d` 落地并复标定）主池失效位幅度不可用**：原「近 `LOOKBACK_BARS_MAIN+1`=21 根反向极值」实测**风险回报倒挂**（XMR -11.97% / EPIC -8.02% / 1000BONK -8.13% / APT -6.90%，其中一例自身涨幅仅 +4.69% 却配 -11.97% 止损），窄幅盘整时又会紧到 0.3% 被噪音打掉。改为 **`2×ATR(14)` / 参考价，夹在带内**；K 线不足 `PERIOD+1` 根返回 **None 不兜底**（与费率/共振的 `n/a` 同口径）。带值经 `ac1660d` 离线回放（131 条已满 24h 的 1h 主池信号）由 `[3%,12%]` **重标定为 `[8%,20%]`**（`STOP_PCT_MIN=8.0` / `STOP_PCT_MAX=20.0`，依据见常量注释：固定百分比在 8% 见顶 +1.69%/止损 32.8%，带内扫描 `[8,20]` +1.60%/31.3%）。物证：重标定前 prod 主池 `stop_loss_pct` 分布 min 5.64 / 中位 9.89 / **max 21.18**，**>10% 有 5 行**（旧口径产物）。
  - **⚠️ 本轮修复的连带缺陷（文案随常量漂移）**：`ac1660d` 改了 `STOP_PCT_MIN/MAX` 却没同步**三处硬编码 `[3%,12%]`** —— 其中**两处是用户可见的邮件正文**（失效位行、图例），即重标定后收件人看到的仍是旧带，属「改了值但没改口径披露」。修法：新增 `STOP_BAND_TXT = f"{STOP_PCT_MIN:.0f}%~{STOP_PCT_MAX:.0f}%"` 并让两处邮件文案 `2×ATR({STOP_ATR_PERIOD}) 夹 [{STOP_BAND_TXT}]` 由常量派生、函数内注释改为引用常量名 ⇒ 以后改带只需改两行常量，杜绝同类漂移。（离线校验 11/11：正文含 `[8%~20%]`、无 `[3%,12%]`、渲染层无 markdown 强调符、BRK 强度 = `4.05×3.0=12.15`、窄幅落 8% 下限、K 线不足返回 None、首轮宽限 > 最大 offset+5min、`expire_signals` offset=90。）
- **P2-9（P2，已改未提交）BRK 无失效位**：`task_scan_accumulation` 原先只落 `trigger_price`（= `break_px`），**不落 `stop_loss_pct`** ⇒ 卡片有入场价、无失效位。改为主池/BRK **共用 `_atr_stop_pct()`** 同口径。物证：prod `scenario='BRK'` **33 行、`stop_loss_pct` 有值 0 行**。
- **P2-7（P2，已改未提交）BRK 同根已收盘条重复判决**：BRK 周期 1800s，而「已收盘条从收盘到不再是 `closed[-1]`」的窗口是 3600s ⇒ **同一根条被连续两轮判定**（`biz.scan_signal` 对 BRK 无唯一约束 ⇒ 插 2 行；告警层 12h 冷却兜底不会重复发信，但库与 stats 重复）。修法：在 `context_tags` 写 **`bar=<open_time>`** 标签，按 (symbol, 标签) 做**精确**去重（预取近 3h 的 BRK 标签，`stats` 增 `brk_dup_skipped`）。**SQL 用 `left(tag,4) = 'bar='` 而非 `LIKE 'bar=%%'`** —— psycopg3 在 `params=None` 时**不处理 `%`**，`%%` 会字面残留。物证：prod 有 **12 个币的 BRK 存在多行**（实物复现 LAUSDT `trigger_price=0.07481`、`vol_x=4.0`，id=1159 @08:37:59 与 id=1161 @08:51:52）；现存 BRK 行 `context_tags` 均为 `['brk_up','vol_x=…']`、**无 `bar=` 标签**（旧码产物）。
- **P2-5（P2，已改未提交）BRK 强度分恒 0**：BRK 的 `oi_chg_pct` 为 `None`（突破判定只用价+量）⇒ `_alert_strength` 基量 `|vol_ratio × 0| = 0` ⇒ **强度条整体不渲染、混排永远垫底**，哪怕 `vol_ratio = 4.05x`。修法：新增 `BRK_STRENGTH_OI_EQUIV = 3.0` 作**临时等当量**（BRK 门槛即 `BRK_VOL_RATIO=3.0`），仅当 `scenario='BRK'` 且 `oi_chg_pct IS NULL` 时启用；**待 BRK 样本积累后按实际分布标定，勿据单样本调参**。
- **P2-6（P2，已改未提交）历史先验选择偏置**：`_scenario_priors` 样本限 `alerted_at IS NOT NULL` ⇒ 只覆盖**告警期**，而告警本身依赖 regime 顺风（实测覆盖 S1 31/67、S2 2/15、S3~S8 0/50）⇒ 正期望是该口径的**必然**结果，不是信号质量证据。本轮采「**显式披露**」方案（渲染与图例均标注「仅含已告警样本，非无偏基准」），未做 regime 分层（需更长样本）。
- **DOC-1（文档）BRK 论证更正**：原文「125 个 active ACC 币模拟最高 vr=0.93 ⇒ `BRK_VOL_RATIO=3.0` 永不可达」是 **08:09（整点后 9 分钟）的单次快照**，该值随采样分钟漂移（min 0.194 / 中位 0.789 / **max 8.263**，既有 < 1 也有 > 3）⇒ **论证不成立**，正确理由是「同一根未收盘条在不同时刻判定结果不同、**不可复现**」。已同步更正 `scan_daemon.py` 内注释与 `AGENTS.md` 的 P1-4 条目（见上）。
- **PROC-1（流程）验证脚本未入库**：工单点名的 `_verify_remaining.py`（`dd8379d` 的 28/28 脚本）**已不在磁盘**（全仓搜索无果），无法原样入库；本轮改为「只读 prod 探针 + 设计决策留档」替代，**如实记录该脚本已丢失**这一事实。

**交接（给并发进程 / 下一次会话）**：
1. ~~prod 尚无 `breakout_px` 列~~ —— **已过时**：`fix_061` 已执行（09-22 复探确认列存在），代码与迁移已一致，无部署阻塞。
2. **占位符一致性坑**：`task_scan_accumulation` 的 **ACC 与 BRK 共用同一条 INSERT**，本轮给 INSERT 加 `stop_loss_pct`/`breakout_px` 后两侧元组**必须严格同数**（现均为 18 值、末位显式 `status`），否则只要有一轮出现 ACC 候选，`executemany` 就抛「占位符数量不匹配」使**整批失败**。改该 INSERT 时必须同时核对两个 tuple。
3. 首轮宽限：`scan_daemon.FIRST_ROUND_GRACE_MAX_MIN` 与 `check_scan_freshness.FIRST_ROUND_GRACE_MAX_MIN` **必须同值**（`=30.0`），且须 > `TASK_DEFS` 最大 offset + 5min（有断言兜底）。**新增任务时必须同步三处**：`TASK_DEFS`、`STALL_HEARTBEAT_TASKS`、看门狗 `HEARTBEAT_MAX_AGE_MIN`（`confirm_signals` 已三处齐备）。
4. **改「值」必须同改「口径披露」**：任何被邮件正文/图例引用的阈值（带值、费率倍数、窗口小时数）都应**由常量派生文案**而非硬编码 —— `ac1660d` 的 `[3%,12%]`→`[8%,20%]` 漏改三处即是反例（现已用 `STOP_BAND_TXT` 收口）。

### fix_061 收口 + 失效位夹带标定 + 执行层闭环诊断（2026-09-22）

**① fix_061（信号延续确认）已全部落地并推送 `44b407f`**（上一节记的「未提交」已解除）。
按用户拍板「整体提交这两个文件」把 `scan_daemon.py` / `check_scan_freshness.py` 连并发 WIP 一并提交，提交信息显式注明「含并发 WIP（P0-1/P2-4/P2-9/P2-7/P2-5/P2-6/DOC-1）」；
同时提交 `fix_061_scan_signal_breakout.sql`（**已对 prod 执行**）/ `phase_execute_scan_signal.py`（diff 100% 属本工单）/ 设计文档 v0.6。
未纳入并发产物：`phase_check_cvd_ready.py` / `phase_watchlist_monitor.py` / `binance_http.py` / `workbench/scheduler.py` / `imap_client.py` / `workbench/output/*`。
上一节「交接」3 条已闭合：`breakout_px` 列已建（`numeric`）、ACC/BRK 元组同数（18）、两文件首轮宽限同值 30.0。

**② 失效位夹带标定：`[3%, 12%]` → `[8%, 20%]`**（`STOP_ATR_MULT` 保持 2.0；无 DDL）。离线回放 **131 条**已满 24h 的主池信号
（入场 = 触发根收盘价，离场 = `signal_ts+24h`，方向对齐，窗口内先触失效位记 `-stop`）：
- **纯 24h 持有基线 均 +1.44% / 中 +0.32% / 胜 53.4%** —— 任何 ≤5% 的失效位都把期望做差（固定 3% 止损率 **68.7%**）。
- **固定百分比在 8% 见顶**（唯一的内部极值，非单调）：3%→+1.28/68.7、5%→+1.29/52.7、6%→+1.58/42.7、**8%→+1.69/32.8**、10%→+1.29/26.7、12%→+1.04、20%→+0.84（「均/止损率」）。
- 带内扫描同向：`m=2.0` 带[3,12] +0.53%/57.3% → 带[6,15] +1.05%/38.9% → **带[8,20] +1.60%/31.3%**。
- 结论同源 d3/延续确认：**本系统 24h 中位边际（+0.32%）小于日内噪音** ⇒ 窄止损把正期望翻负；失效位**只能作风险披露渲染，绝不能接成自动平仓线**。
- 历史行 `trigger_price` 全 NULL（生产者 P2-4 才补算）⇒ 回放按「该周期 `open_time <= signal_ts` 最后一根收盘价」重建入场价。
- ⚠️ 样本 131 条 / 约 6 天 / 单边上涨为主（90 多 : 41 空），统计力有限，勿据此微调。

**③ prod 采集停摆 ~14h 与恢复（本次实测）**：`scan_daemon` 于 **09-21 10:00 UTC 前后**死亡，**09-22 00:19:56 UTC 新实例拉起**（`__daemon__` 心跳）。
- 停摆期缺口：`1h` 缺 09-21 11:00~14:00、`15m` 缺 11:00~21:00、`5m` 缺 11:00~22:00、**OI 实时桶缺 09-21 10:00~23:59**（约 14h）；`scan_signal` / `alerted_at` 冻在 09-21 09:55。
- 恢复后：`oi_cvd_snapshot(realtime)` 00:20、`signal_ts` 仍待 OI 桶积累；各任务心跳（除 `expire_signals`/`prune_scan_data`/`scan_squeeze` 未到点）在 5 分钟内全绿、`last_error` 空。
- ~~**`expire_signals` 首轮仍像 `offset=900`** ⇒ 旁证容器跑的是未含 P0-1 的旧构建~~ **此判断已被推翻（见 ⑤）**：容器实际已跑 `44b407f`。
- 缺口回填命令（**必须非沙箱**：本机执行时沙箱拦外网，报 `WinError 10051`）：
  `python bin/phase_scan_klines.py --backfill-days 1 --force --intervals 5m,15m,1h` 与 `python bin/phase_backfill_oi_history.py --force`。

**④ 执行层闭环诊断：`exec_state` 全 NULL 不是 bug，是「没调度 + 总开关关」**。
- `supervisord.conf` **无** `phase_execute_scan_signal` program，`scheduler.py` 无对应 job ⇒ 唯一入口是 workbench 手工任务「盘面扫描·P4 执行层（dry-run）」。
- `exec_state`/`executed_at` 仅在 `log_audit` 的 `rec["_mark_exec"]` 为真时回填，而该标志只在 `--live` 且 `.env` `SIGNAL_TRADE_ENABLED=1` 时设置 ⇒ **dry-run 刻意不消费信号**。当前 `.env` = `SIGNAL_TRADE_ENABLED=0`。
- 库内痕迹：`biz.scan_auto_trade_log` **12 行**、末次 **09-17 07:36 UTC**（说明手工跑过 dry-run）；`exec_state`/`executed_at` **0/1035**。
- 候选池非空：严格按 `load_candidates` 条件（main + `p_dir=up` + S1/S2 + 已告警 + `exec_state IS NULL` + `active/confirmed` + 未过期）= **27 条**（不限窗口）。
- 建议顺序（真金白银，勿跳步）：① 开 `SIGNAL_TRADE_ENABLED=1`；② workbench 手工 dry-run 核对意图；③ 手工 `--live` 单笔小额验证下单/审计/回填；④ 再谈加调度。**扫描链路刚从 14h 停摆恢复，暂不加调度、暂不开总开关。**

**⑤ 部署已就位一半（本次更正 ③ 的结论）**：`44b407f` **已经部署**，只有 `ac1660d`（夹带 `[8%,20%]`）还需再构建一次。判据是「心跳时刻 = 实例启动 + 该任务 offset」精确对上：

| 任务 | 心跳时刻 | 推导 |
|---|---|---|
| `__daemon__` | 00:29:18 | 本实例启动 |
| `expire_signals` | 00:30:48 | 启动 **+90s** ⇒ 新 offset（旧值 900 不可能） |
| `confirm_signals` | 00:37:18 | 启动 **+480s** ⇒ 新任务存在（旧构建无此任务） |
| `prune_scan_data` | 仍 09-21 09:33 | 启动 +600s 未到点（预期一致） |

- **`confirmed` 短期不会出现**（预期内，非故障）：主池 40 条 active **全是部署前产物**、`breakout_px` 100% NULL ⇒ 延续确认**无判据可用**；覆盖率只能等**部署后新出**的主池信号。
- `trigger_price` / `stop_loss_pct` 只有 12 行且为 5.64~21.18（旧「21 根极值」产物、**未被 [8%,20%] 夹带过**）⇒ 验收时勿把历史行当成本轮产物。
- **验收命令的正确用法**：`select ... where signal_ts > now() - interval '6 hours'` 在「无新信号期」必然为空，**空 ≠ 故障**；须配合「离线复现该轮筛选」才能判别。

**⑥ 【P1 已修｜待部署】主池 `_l1_screen` 对「后半段才完成的异动」系统性漏检**（本轮发现→本轮修复，`scan_daemon.py`）
- **现象**：prod 恢复后主池 0 新建信号，而 `scan_main_pool` 心跳/`round_count`/`last_error` 全正常。用**同一份常量 + 同一份 prod 数据离线复现**该轮筛选：00:48:20 UTC 时 **269 币中 1 个（`GENIUSUSDT` 15m **+3.52%** / `vr=14.15`）L1(level=2)+L2 全过**，而 00:42:20 那轮干净跑完却什么都没写；`_in_cooldown_main` 已排除（其唯一一条 `created_at`=09-21 02:50，远在 6h 冷却外）。
- **机制**：`scan_klines` 每 5min 把**未收盘**的 15m/1h 条 UPSERT 覆盖（同一行 `close_px`/`quote_vol` 随时间内变），而 `_l1_screen` 只取 `closes[-1]`。一根 15m 条的「终值可用且仍是最新根」的窗口 = `[首个 ≥T+15 的采集, T+20)` **仅约 2~5 分钟**，而扫描每 15min 才一轮 ⇒ **能否看到收盘终值取决于两个相位**；容器每 ~15min 重启又不断打乱相位 ⇒ 等效随机。与已记档的 BRK 坑（AGENTS「P1-4：库里最后一根 1h 是未收盘条 ⇒ 同一根在不同时刻判定不同、不可复现」）**同源**，当时只修了蓄势池 BRK，**主池 L1 未动**。
- **量化（只读，6 天 / 280 币 / 用 5m 重建触发条在 +5/+10/+15 分钟的快照）**：真异动条（终值 `|chg|≥2%` 且 `vr≥2.0`）= **1719**（up 1055 / down 664）；**首次满足阈值的快照位置** = `+5min 204(11.9%)` / `+10min 517(30.1%)` / **`+15min(收盘) 920(53.5%)`** / 条内始终不满足 78(4.5%)。⇒ **53.5% 的异动只有收盘才越阈**，能否被评估取决于相位 ⇒ **量级三到四成的漏检**（`GENIUSUSDT` 即实例）。
- **修法（本轮已实施；改动会提升信号量）**：L1 触发根由「只看 `closes[-1]`」改为 **「优先取最近一根**已收盘**条（值稳定 ⇒ 判定可复现），不合格再回退未收盘条（保持及时性）」**。新增 `_last_closed_idx()`（判据 `open_time + 周期时长 <= now`）与 `_l1_eval()`（对**指定根**做价量粗筛，与 `backtest_scan_scenarios.py::scan_symbol` 同口径），`_l1_screen` 按「已收盘根 → 未收盘根」顺序取首个达标者，并在返回 dict 里带 `bar_idx`。**去重无需新增机制**——现有 `_in_cooldown_main`（6h）天然拦住「未收盘先入池、收盘又命中」的重复。两项副作用已一并落地：① 入场/失效位锚定**被判定的那一根**（`bars[l1["bar_idx"]]`），ATR 窗口同步截断为 `bars[:bar_idx+1]`（原实现两处都写死 `bars[-1]`，只因判定根恒为末根才侥幸自洽）；② `backtest_scan_scenarios.py` 补注口径对齐说明——该回测本就只在已收盘历史条上迭代（`t` 上界 `n-max_h-1`）⇒ **无需改代码**，原先顾虑的线上/回测分歧实际不存在。
- **自测**：新增常驻单测 [test_scan_l1_closed_bar.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_scan_l1_closed_bar.py)（**16/16 通过**，`python workbench/test_scan_l1_closed_bar.py`）：含「仅收盘才越阈」（用例 1，附旧实现对照做回归护栏）、「仅未收盘越阈 → 回退命中」、「两根都越阈 → 取已收盘根」、新鲜度护栏（15m 陈旧 / 根数不足）、多周期取 level 最高者不回归、量比不足不命中、ATR 截断锚定；`py_compile` 两文件均通过。
- **prod 只读回放（2026-09-22 00:59:47 UTC，528 币，复用真实 `_l1_screen`/`_compute_l2`，不写库）**：新规则 L1(level≥2) 命中 **9** / 旧规则 **5**；**旧规则漏掉、新规则补回 4 条，且 4 条全过 L2、全不在 6h 冷却内**（= 会真正入池）：`GENIUSUSDT` 15m +3.41% vr=21.39（**即 ⑥ 记档的同一实例**）、`GRASSUSDT` 1h -5.83% vr=2.70、`RAYSOLUSDT` 15m +2.07% vr=3.17、`TRUMPUSDT` 15m +2.40% vr=3.04；反向（旧命中/新不命中）**0 条**。⇒ 单次快照漏检率 4/9≈**44%**，与离线量化的「三到四成」同量级。
- **待部署**：本次提交需重启容器才生效（当前线上是 `44b407f`，仍是旧 L1）。参考水位：`44b407f` 部署后至 00:59 主池仅新建 **1** 条信号（`breakout_px` 非空，证明新列已生效），部署后信号量应明显高于此。
- **✅ 已部署验收（2026-09-22 01:28 UTC，`aa46f57`，只读）**：① 心跳 12 项全绿、`last_error` 全空；② 信号量抬升——部署前 `00:00` 小时仅 **1** 条，部署后 `01:00` 小时 **7** 条；③ 近 6h 共 **8** 条主池信号，`trigger_price`/`breakout_px`/`stop_loss_pct` **8/8/8 全覆盖**，`stop_loss_pct` 越界 **0**；④ 判定根识别（用 `prev_close = trig/(1+chg/100)` 反查前一根）**8/8 全部定位**：**判定根已收盘 7 / 回退未收盘 1**（`EVAAUSDT` 15m 00:56:23 正是「已收盘根 00:30 仅 +0.40% 不达标 → 回退未收盘根 00:45 +2.23% 命中」的预期形态）⇒ 修法与设计一致。
- **⚠️ 验收方法论的坑（本轮实测，勿再踩）**：**不能用「`trigger_price` 等值 join `biz.asset_klines.close_px`」判定锚定成功**。原因有二：① 回退分支的判定根是未收盘条 ⇒ `scan_klines` 每 5min 持续 UPSERT 覆盖，值必变；② **已收盘条也有窗口**——`_last_closed_idx` 按 `open_time + 周期时长 <= now` 判「时间上已收盘」，但该行可能**尚未被收盘后那一轮采集刷新**、库里仍是收盘前最后一次快照（实测 `WIFUSDT` 15m 00:45：信号时 0.2449 → 现 0.2444；`PTB`/`LSK`/`4USDT`/`1000XEC` 同形）。对**判定**无影响（该值在窗口内冻结、不回漂，判定仍可复现），只影响「事后用库值精确复原触发价」⇒ 验收请改用 `prev_close` 反推法。
- 附：`biz.asset_klines` 有 `fetched_at` 列，但**整批 bar 同一次写入共享同一 `fetched_at`**（实测 6 根全等于最后一次采集时刻）⇒ 不能用它区分「哪一轮写了哪根」。
- **部署后 1h 复看（02:54 UTC）**：心跳 12 项仍全绿；主池信号量 `00:00` 1 条 → **`01:00` 15 条** → `02:00` 6 条（未满小时）⇒ 较部署前 `1 条/小时` 提升一个量级；部署后 22 条新信号中**判定根已收盘 17（77%）/ 回退未收盘 5（23%）/ 未定位 0**。
- **⚠️ 主池 `confirmed` 有「设计决定的最小延迟 1~2 小时」，别误判为故障**（本轮差点又踩）：`task_confirm_signals` 的判据是 `k.open_time > s.signal_ts` 的 1h 根**且该根已收盘**（`+1h <= NOW()`）⇒ 信号后**新开**那根才可用，故最短 = 距下一个整点 1h 开 + 1h 收，最长约 **2h**（`x:59` 出信号 → `x+2:00` 才可确认）。实测 02:54 时点：22 条带 `breakout_px` 的 active 中**仅 1 条时间上可评估**（`EVAAUSDT` 00:56，越位 0）⇒ `confirmed` **0 条属预期**，第一批观察窗口在 **≥03:00 UTC**（`01:00` 小时那批的下一根 1h = 02:00，收盘 03:00）。见 `BREAKOUT_WINDOW_H = 6` 常量注释与 `task_confirm_signals` docstring。
- 存量遗留：主池 `active` 62 条 = **40 条存量（`breakout_px` NULL）** + 22 条新产。存量的 `breakout_px` NULL ⇒ SQL 显式跳过、**永远不会 `confirmed`**，只会随 TTL 到期转 `expired`。是否给存量回填 `breakout_px`（需先定「用哪根 K 线反推」的口径）**待用户拍板**，未动。

**⑦ ③ 的停摆缺口回填已收口（2026-09-22 01:0x UTC 只读核对，命令见 ③）**
- 回填执行结果：K 线 `215424` 根（`5m/15m/1h` × 528 币 × 1 天，失败 `0`）、OI 历史 `95691` 行 / `191` 币（`--force` 全量重填，失败 `0`）。期间 Binance 权重偏高使间隔自动拉长到 ~5s，任务正常跑完（exit 0）。
- **K 线缺口已补齐**：近 3 天 `5m 276434 行` / `15m 92004 行` / `1h 22918 行`，三者**稀疏小时（`count(DISTINCT symbol) < 100`）均为 0** —— ③ 记载的 `1h 09-21 11:00~14:00`、`15m 11:00~21:00`、`5m 11:00~22:00` 三处缺口全部消失。注意币数水位已从 ③/旧记录的 ~247~280 升至 **528**（K 线宇宙扩容，勿再拿旧数字当基线）。
- **OI 停摆窗口已补齐**：`09-21 10:00 ~ 09-22 00:00` 每小时均有 `backfill` 行 **191 币**（`realtime` 该窗口为 0 是预期——daemon 当时已死）；`09-22 00:00` 起 `realtime` 恢复 **248 币**。`09-21 09:00` 为切换缝：`realtime 226` + `backfill 36`（并存）。
- 遗留：`09-21 03:00/06:00/07:00/08:00/09:00` 的 `backfill` 组仅 **34~36 币**（回填源本身对该时段只覆盖子集，**与本次停摆无关**，且不在停摆窗口内）⇒ 这些小时的 OI 横截面偏薄，若后续做 OI 相关横截面统计需避开。
- 心跳：12 项任务全绿、`last_error` 全空；`scan_main_pool` round 37 / `scan_klines` round 87 / `scan_oi_cvd` round 72。

### 轧空池复验遗留 6 项处置（复验_轧空池复验遗留6项_2793be9_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池复验遗留6项_2793be9_2026-09-22.md`。**本轮全是「让结论可比」的工具/口径修复，无 DDL、无阈值变更**（复验明确「不建议在分母未修前动阈值」）。

- **🔴 P1-1a 标定脚本分母不可自证**（同数据同公式**相差 1.66×**——分母缺 14h 时越阈率被系统性放大；**绝对数字不留档，以脚本实跑为准**）：`calib_squeeze_liq_thr.py` 新增 `denominator_coverage()`——按**本次入样币集合**打印每币 5m 根数 / 期望（`bars / (days×288)`）、覆盖小时中位、低于门槛的币数、最差 5 币、`bars_ratio_p10`、低于门槛币占比；**整体覆盖 < `MIN_DENOM_COVERAGE=0.9`、或低于门槛的币 > `MAX_DENOM_BELOW_PCT=5%` 时拒绝出结论并 `exit 3`**（复验 D2：只看均值会被少数劣质币蒙混过关，均值口径会在最需要它的时候失灵）；`--json` 亦置 `denominator_ok=false` **且 `judge.pass` / `judge.reliable` 同步为 false**（复验 D1：原先只置了 flag 未置结论 ⇒ JSON 结论与退出码相反，下游读 JSON 必然误判）。
- **🔴 P1-1b 默认 `--days 7` 与阈值语义（/24h 成交额）冲突**：默认改 **1**（实测同数据 8.32% ↔ 24.76%，3×）；`--vol24-min` 保留为 `--vol-win-min` 的别名；SQL 别名 `vol24` → **`vol_win`**（避免被误当严格 24h）。
- **🔴 P1-1c 未来函数**：两处 `k.open_time <= l.ts` / `k2.open_time <= l.ts` → **`< l.ts`**（`<=` 会取到覆盖 `[l.ts, l.ts+5m)` 的桶 = 判定时刻**之后** 5 分钟的量价；该修正使比率**整体上抬约 +0.9pp** —— 数字不留档，**修后必须重跑脚本，不得复用修改前的任何数值**）。
- **🔴 P1-1d 只报单一口径的越阈率**：入队侧 short 同时报「**>0**（剔除无空头爆仓样本）/ **含 0**」两个率与各自 n（复验 D3 已把 long 侧也补成双率：`long_liq=0` 是「该 1h 无多头爆仓」的**合法观测**、永不可能越阈 ⇒ 只报 >0 子集等于**系统性抬高**触发频率，两口径实测差 1.34×）；并把打印里「设计目标 ≈10%，即落在 P90」改为「**完整分母实测位置以本次实跑为准，勿引用历史分位**」。
- **🟠 P2-1 `judge` FAIL 静默 `return 0`**：改 **`return 2`**（PASS 仍 0，分母不合格 3）⇒ cron/CI 可感知；提示语由「需回退到 P90 或放宽判据」（指错方向）改为「**先核对分母覆盖与未来函数，再谈调阈值**」；`judge` 增 `reliable` 字段。
- **🟠 P2-2 中段闸门无入库单测**：闸门抽成 **纯函数 `squeeze.window_gate(present, first, last, max_gap) -> (ok, head_gap, mid_gap)`**，常量也下沉为 `squeeze.MAX_MID_GAP_BUCKETS`（`scan_daemon` 只调用，删除本地重复实现）；`workbench/test_squeeze_battle.py` 补 **C1~C10 + 边界 + max_gap 注入 + 与旧实现逐例等价性**（**83/83 通过**，`python workbench/test_squeeze_battle.py`）。
- **🟠 P2-3 桶完整度观测被「近 2h 重启过」耦合**：`check_scan_freshness._collect_squeeze_health` 改为**常态评估**近 `OI_BUCKET_WINDOW_H`(=2h) 的 OI 桶数（原常量 `RESTART_GAP_WINDOW_H` 更名），**重启与否只影响文案**（`daemon 近 2h 内重启过（…）` vs `未重启`）⇒ 线程慢死 / 任务卡住 / 采样跳过等**非重启型丢桶**不再无观测（实测正常期 OI 栅格本就缺 24~26%）。
- **🟡 P3「中段」措辞与左端缺桶**：metrics 拆 **`head_gap_buckets`**（自 `first_bucket` 起的连续缺桶——`base_oi = _last_at_or_before(oi_sym, peak_ts)` 会落到窗口**之外**，ΔOI 基准失真，语义更重）+ `mid_gap_buckets`；reason 改为「**判定窗口内连续缺桶 N 个（左端起 h 个 / 中段 m 个，x/y 桶）**」，仍以 `判定窗口` 开头 ⇒ 看门狗 `reason LIKE '判定窗口%'` 计数自动涵盖。判据 `max(head, mid) >= 2`，与旧实现**逐例等价**（C1~C10 全对齐，纯增量仍是 0.6%）。
- **🟡 P3 文档残留瞬时分位**：设计文档 §9/§10.5/§12.1-9 与本文件旧条目里的「`SQZ_SHORT_LIQ_RATIO_MIN` ≈ **P90**、越阈率 **11.4%**、位置合理」**全部更正**为「**位置与越阈率不在文档留数字**（同类实测值随爆仓表滚动窗口漂移 — 复验 D4），一律以 `calib_squeeze_liq_thr.py --days 1` **当次实跑**输出的含 0 / >0 双率（含各自 n）为准，位置待定稿」；同时把「40.87% ⇒ 无条件分位标定不稳健」这一**已被证伪**的结论就地标注为「分母缺 14h 的产物」。
- **待拍板（未做，遵复验 §8）**：#2 阈值取值（候选 = 完整分母 P95 量级，**具体数值以当次实跑为准**）——**本轮不动**，待补齐 OI 采样后重跑 #1 再看上界；#6 OI 采样侧补齐（本轮最大杠杆，另立项）；#7 尾部阈值 `2×300s` 是否放宽到 2.5×（实测 `oi_lag_sec` 已到 588/600，先观察一轮）。
- **⚠️ 判据脆弱性的真正来源（留档）**：完整分母下条件子集的跨口径上界与判据线 20% 之间**只有亚 pp 级裕度**——一次口径微调（去未来函数口径即上抬 ≈+0.9pp）就可能翻盘；**具体数字不留档，以 `calib_squeeze_liq_thr.py` 实跑为准**（复验 D4）。任何「判据 PASS」的表述都必须带**分母覆盖 + 口径**两个前提，否则不成立。

### 轧空池 6 项落地复验处置（复验_轧空池6项落地_f816a34_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池6项落地_f816a34_2026-09-22.md`。复验认定上一轮「8 项缺陷 + 待拍板可落地部分全部落地、实现质量高、单测 83/83 独立复现」，另开 **D1~D7**（4 项新缺陷 + 3 项 P3）+ 顺带发现 **S1~S4**。**本轮仍无 DDL、无阈值变更**（复验 §7#6「仍不建议动阈值」，但更正裕度为**亚 pp 级**、去未来函数口径**当前 PASS**——见 D4）。

- **🔴 D1（P1，已修）`--json` 的 `judge.pass` 与 `denominator_ok` 脱钩**：`--days 7 --json` 实测 `denominator_ok=false` / `reliable=false` / 进程 rc=3，而 `judge.pass` 仍为 `true` ⇒ **JSON 结论与退出码相反**（rc 给 shell/cron 看、`pass` 给 JSON 消费者看，两者矛盾则下游必然误判）。成因：分母不合格时比率被整体压小 ⇒ 轻松 <20% ⇒ 判真，**这个 pass 是纯噪音**。修法一行：`pass = bool(denom_ok and new_rates and max(new_rates) < JUDGE_UPPER_BOUND_PCT)`。
- **🟠 D2（P2，已修）分母闸门只卡整体均值**：`coverage = sum(bars)/(n×expect)` 是**算术平均**，少数劣质币占比够小就能过线（复验算术例：249 币里 27 币仅 10% 覆盖 ⇒ 均值 0.902 过 0.9 线，而这 27 币 `vol_win` 被少算 10×、比率被放大 10× 且照样入样**污染分布**）⇒ 闸门会在最需要它的时候失灵。新增 `MAX_DENOM_BELOW_PCT = 5.0`：`symbols_below/symbols` 超限即拒绝出结论（与整体覆盖并列为两条拒绝理由）；`denominator_coverage()` 另输出 `bars_ratio_p10` 供观察。
- **🟠 D3（P2，已修）long 侧只报过滤后单率**：`SQL_SAMPLE` 带 `AND l.long_liq_usd_1h > 0` ⇒ 无条件分位、旧/新值越阈率、条件子集三变体**全部建立在过滤后子集上**；而 `long_liq=0` 是「该 1h 无多头爆仓」的**合法观测**、永不可能越阈 ⇒ 排除它等于**系统性抬高**触发频率（复验实测两口径差 1.34×）。新增 `SQL_LONG_INCL0` 输出无条件含 0 分布（只需一处 JOIN，省掉两个 LATERAL），打印节拆「主口径 / 含 0 口径」两块。
- **🟠 D4（P2，已修）文档仍以「20.50% / 裕度 0.05pp / 判据翻盘」为事实**：该组数字是上一轮复验报的**易变实测值**（本轮两套独立实现——仓库真码 19.21% / 复验独立脚本 19.11%，差值 ≤0.10pp——**均未复现 20.50%**、全部 PASS）⇒ 「20.50% 翻盘」**撤回**（复验报告 §0 更正横幅已自行认领）。已把设计文档 §10.5 / §12.1-9 / 观察项② 与本文件相关旧条目**统一改为「只写口径与判据、数字以脚本实跑为准」**，仅保留稳定结论：**裕度亚 pp 级**、去未来函数口径**整体上抬 ≈+0.9pp**。
- **🟡 D5（P3，已修）`mid_gap_buckets` 语义变更使历史/新行不可比**：v1 的 `mid_gap_buckets` **含**左端起、v2 **不含**（判据行为等价：`max(head, mid) ≡ v1 的 mid`，单测逐例等价已证）⇒ metrics 新增版本位 **`gap_metric_ver`（当前 2，常量 `squeeze.GAP_METRIC_VER`）**，跨版本回看该字段**必须先看版本位**，否则误读历史行。
- **🟡 D6（P3，已修）拒判文案无单测保护**：该文案承载跨文件耦合（`check_scan_freshness` 用 `reason LIKE '判定窗口%'` 统计「拒判中 N 条 track」），此前**只靠注释承诺**。已把拼装抽成 `squeeze.gap_reason(head_gap, mid_gap, have, expect)` 纯函数（docstring 明写**必须以「判定窗口」开头**，改前缀会让观测静默失联），单测补三条前缀/数值断言。
- **🟡 D7（P3，已修）`window_gate(max_gap=0)` = 全量拒判**：判据是 `max(...) < max_gap`，传 0 恒假 ⇒ **任何窗口都拒判**、静默停摆整条轧空判定。已在 `window_gate` 内显式夹取 `max_gap = max(1, int(max_gap))` 并在 docstring 标注有效域 ≥1；单测补 0 / 负值两例，并断言 `max_gap=0` 下**完整窗口仍通过**（证明未退化为全量拒判）。
- **🟡 S1（已修）`TASK_DEFS` offset 注释滞后于代码**：注释仍写「最大 offset（当前 900s = 15min）」，实际 `expire_signals` 已改 **90**、`confirm_signals` 新增 **480** ⇒ **真实最大 = 600s（`prune_scan_data`）**。两处注释已更正（`FIRST_ROUND_GRACE_MAX_MIN=30` 的 assert 仍成立）。
- **ℹ️ S2 / S3（无需改码）**：`TASK_DEFS` 现为 **11 项**（新增 `confirm_signals 1800/480`）；「桶完整度常态评估」在当前环境**近乎等效旧行为**（daemon 重启周期 ≈14min ≪ `OI_BUCKET_WINDOW_H`=2h ⇒ `restarted` 几乎恒真）——其价值在**不再重启**的场景（正是它要防的死法），属**未来保险**而非当下行为变更。
- **🔴 S4（既有开放项，本轮未做）OI 采样侧补齐仍是最强杠杆**：新看门狗同一时刻实测 `have=14 / expect=24`（14 < 21.6 命中）⇒ **正常期 OI 栅格仍缺约 42%**（上一轮记 26%，同向且更严重）。另立项。
- **待部署 / 待复验（复验 §7#8）**：`head_gap_buckets` / `mid_gap_buckets` / 新拒判文案在生产侧**全库 0 行**（`squeeze_track` 仅 12 行、最后 `judged_at` 在 16h 前、唯一活跃行走的是通过闸门那一支）⇒ `f816a34` 的**部署状态 = 不可判定（判据未打开）**，只能确定「push 后进程重启过」。待首个 `judged` 产出或首个缺桶拒判落库后按复验 §5⑥ 重跑探针。
- **本轮验收实跑（⚠️ 当次记录，**勿引用**——爆仓表滚动 24h 全跨度 + 5m 分母逐时补齐，每次样本都换一批；同一天两次 `--days 1` 实跑上界即 20.28% → 20.45%）**：`--days 1` 覆盖 ≈1.0、低于门槛 0/248、P10 ≈1.0，条件子集跨口径上界 **>20% ⇒ rc=2 FAIL**（复验报告同一时刻为 19.21% PASS——**这正是「文档不留数字」的直接佐证**，也顺带实测了 P2-1 那条**从未被走到的 FAIL 负路径**）；`--days 7` 覆盖 ≈0.55、低于门槛 298/298 = 100% ⇒ **rc=3** 且**打出两条拒绝理由、不打印分布**；`--days 7 --json` ⇒ `denominator_ok=False` + `judge.pass=False` + `reliable=False`（**D1 修复验证通过**）。单测 **91/91**（原 83 + D5/D6/D7 新增 8 条）。