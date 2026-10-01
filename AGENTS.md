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
- AI 追溯日志数据质量（P0 asset_id/symbol 路径 / P1 坏 JSON 控制符修复 / P1 判定阈值后处理）→ 已修 `35ef6f0`；存量 `sys.ai_trace` 回填用 `backfill_ai_trace_identity.py --all`（从 user_prompt 提取 symbol + core.asset 解析 asset_id，占位符清 NULL）
  - **复验收口（`复验_prod回填与数据质量_f53ac96_2026-09-23.md`，本次修复）**：连 prod 只读复验确认历史回填真实有效（875/1366 带 identity、linkage 零错配、0 占位符），`both_null=491` 属「源无资产上下文」保持 NULL 正确（口径已对齐，非缺陷）；但暴露 **P1 衍生缺陷：`raw_response` 写入路径未清洗**（全表 49 条坏 JSON，9-23 当天仍新增 7 条）——`_sanitize_json_control_chars` 只作用在解析/展示层，`_write_ai_trace` 落的是 LLM 原样输出。**已修**：新增 `_to_storable_json()` 分级清洗（合法原样透传 → 补转义 → 解析后规范 JSON → 不可解析保原文），`_write_ai_trace` 库内列存清洗结果、JSONL 兜底文件仍存 AI 原话（可查询性归库、原话保真归文件）。
  - **坏 JSON 主因补修（同批复验，占比 75%）**：`Expecting ',' delimiter` 根因是 LLM 用 ASCII 直引号做中文强调未转义（如 `属"强而透支"结构`）。新增 `llm_client._sanitize_unescaped_quotes()`（字符串内未转义直引号补 `\"`，对合法 JSON 恒为空操作），已并入 `extract_json_from_llm_response` 与 `_to_storable_json` —— 写入口、解析/展示口径（方案 B `parsed_response`）、存量修复三处同步受益。
  - **存量坏 JSON 已回写（2026-09-23 执行）**：`backfill_ai_trace_identity.py --clean-raw` 只做**无损**修复（补转义后要求整体可解析才回写），实测 1383 行中 48 条坏 JSON → 回写 37 条、跳过 11 条。跳过的是「输出被截断/未闭合」型，截断补齐会丢 170~1050 字原文，**审计记录不该被悄悄截短**，故保留原文、靠 `parsed_response` 兜底展示。验收：`raw_response IS JSON`（PG 17）失败行由 48 → 11；抽样 id=1359/1350/1329/1195 键数 12~13、score 与 `reason_detail` 正文无损。
  - **方案 B 部署态已在线确认**：`GET https://crypto-profile-collection.zeabur.app/api/ai-trace?limit=1&date=2026-09-23` 实测返回 `parsed_response`（含 `_risk_forced_off` override 标记）→ `adaa5cb` 已生效。
  - **⚠️ 遗留待办：写入口修复需显式 redeploy 才在线上生效**（`35ef6f0` 后仍新增坏 JSON 即为此坑）。本地已修 + 存量已清，但 Zeabur 容器不重部署则新增行仍落脏原文（教训见 8-27/8-28：push ≠ 线上生效）。
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
- **P2-1** 邮件时间戳**统一北京时间（东八区）**：原 naive `datetime.now()` 无标注 → 曾补 `UTC` → 2026-09-24 起全部改东八区并标注「（北京时间）」（见末节「邮件时区统一」）。
- **P2-2 HTTP 移出 DB 块**：进主事务前先用一次短读取出 tracking 符号，预取 `_live_price` + `_fetch_long_short_ratio` 到 `px_map`/`lsr_map`，DB 块内不再发 HTTP。
- **P2-3 口径不一致只做披露**（分子 CoinGlass 全交易所爆仓 / 分母 Binance 24h 成交额）：邮件脚注明示，字段加 `liq_scope=coinglass_rolling_1h`；未改数据源。
- **P3 展示修正**：`or 0` 全删（`_fmt_ratio` 让 `None→'—'`、`0.0→'+0.000%'` 可区分）；标签改 `最近1h多空爆仓`；加「入队→峰值→判定」时间轴；大户多空比改 `base → now（Δ）`；主动买卖比带数据时点；配色按中文惯例（多头胜=红 `#ef4444`、空头胜=绿 `#22c55e`）。
- **P2-4 阈值偏低（`SQZ_SHORT_LIQ_RATIO_MIN` / `LONG_LIQ_RATIO_THR` 同为 0.00008）暂不动**：两常量本已解耦，值相同但需 1-2 天样本再标定，勿用单样本调参。
- **无迁移**：`squeeze_track.metrics` / `scan_signal.detail` 均为 JSONB，新增字段（`oi_cover`/`top_ratio_base`/`taker_ts`/`liq_scope`/`data_missing`）直接容纳；tracking 轮次的 metrics 改写为 `COALESCE(%s::jsonb, metrics)` ~~以免覆盖 entry metrics~~ ⚠️ **该 rationale 已被证伪（复验 F5，见下文 `ec1c1c2` 段）**：`COALESCE(%s::jsonb, metrics)` 是**整对象替换**，正是「覆盖 entry metrics」的成因，不是防它的手段；现已改为合并语义 `metrics || COALESCE(%s::jsonb,'{}'::jsonb)`。此处保留原文仅为审计链，**勿据其行事**。
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

### catalyst_run_all 停滞 34.8h 修复（2026-09-22，本次提交）

来源：看护邮件「关键 cron `catalyst_run_all` 已 34.8 小时无成功执行（最近 done 1789921874.387732 / 阈值 18h）」。

- **根因（物证级）**：`sys.task` 里 09-20 16:31 后连续三次失败——`3656e212e7fa`（09-21 04:00）`stuck: 240分钟无新日志`、`dd30d3a70dc0`（09-21 10:53）`timeout: 运行超过 12h`、`c27dc9c10a43`（09-22 00:20）`manually stopped: 00:28:05 后无日志`。`sys.task_log` 末尾显示 **DeepSeek `402 Client Error: Payment Required`（账户欠费）**：AI 预处理/ thesis 阶段对 402 逐条降级仍拿不到结果，长时间无日志被收割。
- **放大器（本轮的代码 bug）**：`catalyst_run_all.py` 的 `subprocess.run(cmd)` **无超时** ⇒ 任一阶段挂起会把整条管道拖到 task_manager 的 12h 硬超时，占满并发槽位；且看护 `scheduler_watchdog` 只在 `timeout:/stuck:` 时拦补跑，**`manually stopped:` 等错误不拦** ⇒ 任务自身失败时每 18h 被补跑一次，形成「补跑→再卡→再补跑」。
- **修复 1（`catalyst_run_all.py`）**：每阶段加 `CATALYST_STAGE_TIMEOUT_SEC`（默认 **5400s=90min**），超时用 `os.killpg` 杀掉**整个进程组**并继续后续阶段；6 阶段最坏 9h < 12h 硬超时。
- **修复 2（`scheduler_watchdog.py`）**：新增 `_recent_submission(key, threshold)`——**只有「近阈值内一次提交都没有」才补跑**（scheduler 真失活）；若近期已有提交（scheduler 存活、任务自身失败/卡住）则**只告警不补跑**，邮件附「最近错误」。彻底消除补跑恶性循环（实测 `recent_submission(18h)=True`、`(1h)=False`，与 02:29 那次提交吻合）。
- **仍需人工/部署**：① **DeepSeek 账户欠费需充值**（代码层已熔断/快速失败，但没额度就产不出结果）；② 容器需重新部署以带上 `llm_client` 的 402 熔断与 `process_catalyst_ai` 的「整批 0 成功即中止」护栏（两者在 repo 已有，但 09-22 00:20 那次仍逐条 402，疑似部署滞后）。
- **自测**：`_run_stage` 超时返回 124 且 1s 内杀进程、正常命令返回 0；看护 `_check_key` 四例（失活→补跑 / timeout→拦 / 近阈值有提交→拦且文案指向任务自身 / 未超阈值→ok）全绿；`_recent_submission` 只读 prod 复核通过。

### catalyst_run_all 充值后仍报错：thesis 重生 LLM 流式挂死（2026-09-22，本次提交）

用户已充值 DeepSeek，但看护仍报 37.1h 无成功执行。复查 `sys.task` / `sys.task_log`：

- **402 已解决**：09-22 02:29 那次（`58f441d35f07`）AI 预处理阶段已跑过（日志无 402），说明充值生效。
- **新卡点 = thesis 重生**：该 run 日志停在 **02:55:05 `[57/88] 重生 asset_id=3757 ...`**，之后 **2h45m 无任何新日志**（将被 task_manager 按 `stuck: 240min` 收割）。即卡在 `catalyst_thesis_regen.py` → `db_stats.generate_research_thesis(3757)` 的 LLM 调用，**不是 402**。
- **根因**：`llm_client._call_chat_completions` / `_call_responses` 的 `resp.iter_lines()` 流式循环**只有 per-read 超时（60s）**，而任一 SSE chunk 到达就把 read 超时重置 ⇒ 服务端「滴流/半开连接」时循环**永不返回**；且构造参数 `self._timeout`（thesis 传 120s）此前**根本没用于流式请求**（HTTP timeout 硬编码 `(10,60)`）。
- **修复（`llm_client.py`）**：新增 `STREAM_TOTAL_TIMEOUT_SEC = 600`（总时限下限，≥10min，远大于正常 20~60s 响应）；两条流式路径都在循环内判 `time.monotonic() > deadline`，超时即 `resp.close()` 并抛 `requests.exceptions.ReadTimeout`；`chat()` 的 except 补上该类（原先只捕 urllib3 的 `ReadTimeoutError`，捕不到 requests 包装后的异常）⇒ 走重试/兜底 provider，而不再无限挂起。
- **仍需部署**：容器要重新部署才能带上 `a9466db`（阶段超时 90min）+ 本次 `llm_client` 总时限；在此之前旧代码仍会挂到 12h/240min 被收割。
- **自测**：模拟「持续有 chunk、永不 [DONE]」的滴流响应 → 总时限内抛 ReadTimeout、`resp.close()` 被调用；chat/responses 两条路径均验证。

### 催化剂 Alert 邮件数据质量修复（审计_催化剂邮件_A级2条_2026-09-22，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\_audit\审计_催化剂邮件_A级2条_2026-09-22.md`（对象 = 「🎯 催化剂 Alert·A级」邮件）。新闻本身真实，问题全在**管道数据质量与评级闸门**。

- **P0（已修）价格缺失仍评 A 级「可交易」**：COPPER 现价/入场/止损/止盈**全 0**，却以 86 分进 A 级（AI 推理自己都写「规则目标价与止损价均为0」）。
  - `signal.py::build()` 新增 `_prices_valid(entry, stop, tp)` 闸门：三者任一 `None`/`0`/负/非数值 ⇒ **tier 封顶 C、动作降 watch**（与 RR/方向闸门同属「只降级不改分」的显式例外）。根因：原 `if entry_price and ...` 对 0 短路 → rr=None → RR 闸门不触发。
  - `technical.py::analyze()` 新增 `last_price <= 0` 守卫：源价缺失落成 0 时直接 `detail.error='non_positive_price'`，不产出任何 MA/ATR/档位（防 G5 展示一排 0）。
- **P1（已修）代币快照「流通/总量」误加 `$`**：`_fmt_big` 是货币格式化器，供应量是**代币枚数**（ETH 显示 `$120.68M`、COPPER `$100000.00T`）。新增 `currency=False` 参数，供应量两格改为纯数量（顺带修掉 `f>=1e12` 分支恒真的冗余写法）。
- **P1（已修）G4 缺失字段给中性 50 恰好过线**：`_score_liquidity(None)`/`_score_tvl(None)` 由 50 → **0**（「无数据不放行」），check 文案的 `$0` 改为「未知」。实测 COPPER 式全 unknown 组合：综合分 50 → **35（fail）**。
- **P2/P3（已修展示层）**：G2「有效期 0 天」→「—」（与信号级 7 天有效期矛盾）；G1 rule/AI 事件类型分歧加 `⚠️ 规则与 AI 分类分歧` 注；`uni-qr` 原文链接加「桌面端可能无法打开」注。
- **未做（另立）**：COPPER `total_supply=1e17` 数据源异常（需查 supply 抓取）；**商品铜新闻 → `$COPPER` meme 同名误匹配**（需 `classify.py`/`asset_filter.py` 侧消歧，属分类治理）。
- **无迁移**：全在 `workbench/catalyst/*.py`。
- **自测**：价格闸门 11 例（正/0/None/负/做空优先 invalid）+ `_fmt_big` 货币/数量 4 例 + G4 缺失判 0/COPPER 式 fail 6 例 + A 卡片渲染 smoke（supply 去$/G1 分歧/G2 破折号/QR 注）全绿；`technical` 非正价守卫实测 `entry/sl/tp=None`。

### 催化剂遗留两项处置：COPPER 极小价/供应量 + 商品同名误连（2026-09-22，本次提交）

- **COPPER `total_supply=1e17` 核查（只读）**：`core.asset` 7949(`$COPPER`,meme,rank 4687) 的 `total_supply=1e17`、`circulating_supply=NULL`；`src_cmc.cmc_asset_quote_snapshot`（cmc_id 36847）同样报 `total_supply=1e17`、`circulating=0`。另一条 `$COPPER`(asset 14346) 亦 ~1e17 ⇒ **是 CMC 上报值、非解析 bug**（meme 供应量 1e15~1e18 常见）。真正的问题是**显示精度**：
  - `notifier._fmt_price`（两处）对 `f<1e-4` 用固定 8 位小数 ⇒ `2.9e-12` 渲染成 `0.00000000`（「现价 0」「MA20 $1.632e-12」的假象）。改为 `f<1e-4` 用**科学计数**（`.4e`）。
  - `technical._round_price()`：原 `round(stop,6)`/`round(atr,6)` 把 1e-12 级价格抹成 **0**（档位失效的根因之一）→ 改为按**有效数字**（8 位）取整。实测极小价 `sl=2.95e-12 / tp=6.275e-12 / atr=1.88e-13` 均非 0。
- **商品同名误连消歧（P3，已修未来路径）**：`linker.py` 新增 `_COMMODITY_AMBIGUOUS_SYMBOLS`（COPPER/GOLD/SILVER/OIL/XAU/XAG…）+ `has_crypto_context()`；`map_pairs_to_asset_ids(..., context_text=)` 对**同名商品词 symbol** 要求正文含加密语境（cashtag `$COPPER`/`COPPERUSDT`/token/meme/链上/代币/上线…），否则跳过（Binance Square 常把 Bloomberg/LME 铜金新闻标成 `COPPERUSDT`）。`pipeline._resolve_asset_ids`/`_merge_catalyst` 与 `backfill_catalyst_links.link_catalyst` 已传 `context_text`；歧义符号**不进 symbol 缓存**（避免跨文污染）。实测宏观铜文案→`[]`、含 `$COPPER`/「代币/上线」→保留。
  - **存量未清理（需授权）**：只读量化到**疑似误连 60 条**——`XAU`(XAU9999 Meme,8198) 41/84、`COPPER`($COPPER,7949) 19/27（正文无加密语境）。清理属 DELETE，按铁律**需用户授权后**再执行（可能还需联动 `catalyst_signal`）。
- **无迁移**：全在 `workbench/catalyst/*.py` + `scripts/bin/backfill_catalyst_links.py`。
- **自测**：同名消歧 6 例（宏观→drop、cashtag/代币→保留）+ `_fmt_price` 4 例 + `_round_price`/极小价档位 4 例全绿。

### 高亮信号确定性度量重构 A+B（工单 OPT-HL-DETERMINACY-001，2026-09-23，本次提交）

来源：`待修复工单_高亮信号确定性度量_AB_2026-09-23.md`（派生自 `审计_高亮信号_确定性挖掘视角_2026-09-23.md`）。**工单修正了审计的一处误判**：whale/kol/chain/narrative **实际走六轴加权**（非固定档），真正根因是「单轴事件量被六轴中性（None→50）稀释 → 塌缩到 55-60、MED 内部零区分度」。

- **A1（`macro_market.py`）事件强度连续主轴**：新增 `_event_strength_score(kind, value, t)`（usd 对数连续 $1M→50/$100M→74/$1B→86；flow_pct/mcap_pct 线性封顶）。五类软信号生成段融合：`conviction = round(0.6*conviction + 0.4*es)` 并把 `event_strength` 写入 opp：
  - `whale_flow`（`usd_total`）、`narrative`（`mcap_change_7d_pct`）、`chain_inflow`（`flow_pct`）。
  - `kol_onchain`：**单源（n_confirm<2）封顶 `es=min(es,45)`**（与工单 C 协同，只进观察池）。
  - `etf_flow`（BTC long/short + 非 BTC 3 处）：阶梯档改连续 `strength=round(0.5*base_str + 0.5*es)`（金额单位百万→×1e6）。
  - `_push_opportunity` 只追加 `conviction_*`、不清字段 ⇒ `event_strength` 自动透传。
- **A2（`templates/index.html`）**：高亮卡片新增 `eventStrengthLine`（`事件强度 N · 共振×M`）+ `.signal-event-strength` 样式，插入在 `strengthLine` 后。
- **B1（fng 阈值放宽）25→30 / 75→70**：⚠️ **关键坑**——`_load_market_rules()` 只覆盖「key 已存在于默认 dict」的 yaml 项，而 `fng_fear_max`/`fng_greed_min` **原不在 `OPPORTUNITY_THRESHOLDS_DEFAULT`**，故 yaml 里写了也**不生效**；已把两项登记进默认 dict（30/70）并同步 `market_rules.yaml`。`emotion_fear_max=50` 是另一维度（emotion_subscore），未动。
- **B2 未做（前置 BLOCKER）**：`mvrv_universe.status=error` + 叙事榜缺失属**上游数据源真实故障**，需授权查 prod/接入代码（`db_stats`/CoinMetrics/叙事榜），不在本 PR。
- **无迁移**：全在 `workbench/macro_market.py` / `market_rules.yaml` / `templates/index.html`。
- **自测**：`_event_strength_score` 12 例（含 clamp/None/负值/区分度）+ 单源 KOL 封顶 + yaml fng 生效 + `select_highlight_signals` 合并保留 `event_strength`/`resonance_count` 全绿。

### B2 上游故障根因修复：叙事榜 ETL 未调度（复验_高亮信号_c38e755，2026-09-23，本次提交）

复验 `c38e755` 判定 A+B 全部生效，唯一未闭环 = **B2（叙事榜缺失）**。只读排查后定位为**调度缺口**（非数据源故障）：

- **根因（物证级）**：`src_cmc.cmc_category_member` MAX(snapshot_date)=**2026-09-22**（新鲜，由每日 `cmc_category_refresh` 02:30 刷新），而 `biz.sector_narrative_asset` MAX(as_of_date)=**2026-08-29**（停更 25 天）——因为写入它的 `etl_sector_narrative_assets.py` **从未注册进 scheduler**。⇒ `macro_market.fetch_category_flow` 的 DB 兜底/成分币全失效 → `narrative_flow_ranking` 空 → 线上 `DEGRADED: ['P1-1 叙事榜缺失']`。
- **修复 1（`scheduler.py`）**：新增 `("etl_sector_narrative", "35 2 * * *", "etl_sector_narrative_assets.py", [], ...)`，紧跟 `cmc_category_refresh` 后跑（纯 DB→DB）。`--dry-run` 实测可产出 **2026-09-20：20 叙事 / 6755 行 / 96.5% 匹配 asset_id**。
- **修复 2（`macro_market.fetch_category_flow`）**：db-only 兜底从「仅 categories API 抛异常时」扩到「**API 成功但 watchlist 名称不匹配**时」也兜底（原实现直接返回空）；并给 db-only 条目补**成分币市值加权的 24h 动量**（否则 `build_narrative_flow_ranking` 因 momentum/mcap7 全 None 把它们全过滤 → 叙事榜仍空）。实测两条失败路径都返回 20 条、`narrative_flow_ranking` 非空。
- **MVRV 部分（未发现代码 bug）**：`_build_mvrv_universe` 数据源为 `biz.cm_asset_onchain_daily`（`cm_incremental` 每日 06:30 跑、`done`），最新日 16 币/14 有 MVRV+市值，查询应返回 14 币（ok）。**复验的间歇 error 更可能是 DB 连接瞬时问题**，非可复现代码缺陷；`--` 未改。覆盖率偏小（CM onchain universe 仅 ~16 币）属数据源限制，另议。
- **无迁移**：`scheduler.py` + `macro_market.py`。
- **部署/落地**：容器 redeploy 后新调度生效；也可手工跑一次 `python bin/etl_sector_narrative_assets.py`（**会写 prod**，属幂等 ETL，按铁律需授权后再执行——本次未跑）。

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
- **PROC-1（流程）验证脚本未入库**：工单点名的 `_verify_remaining.py`（`dd8379d` 的 28/28 脚本）**已不在磁盘**（全仓搜索无果），无法原样入库；本轮改为「只读 prod 探针 + 设计决策留档」替代，**如实记录该脚本已丢失**这一事实。⇒ **`ef47b77` 已补 `workbench/test_scan_alert_remaining.py`（16/16 通过，源码 AST + 只读库不变量，以 `bar=` 标签为修复后产物自证标记），PROC-1 闭环**；原脚本虽丢失，但新探针覆盖其判据且可独立复现。
- **🔄 审计误判纠正（2026-09-22 后续核验）**：`审计_盘面异动告警邮件_2026-09-22.md` 初版将 P2-2/P2-7/P2-9 列为「残留」，经独立核验为**误判**——P2-2 在 `f816a34` 中已由 `8ca1258`（其祖先）修复（初版误引旧工单 `L2562/L2583`，f816a34 实码 L3291 取锁→L3302 run_once→L3307 写心跳）；P2-7/P2-9 已在 `44b407f` 修复并部署（只读库实证最新 BRK `id=1220/1221` 带 `bar=` 标签且 `stop_loss_pct` 落 [8%,20%]，33 条 NULL 为修复前历史）；仅 PROC-1 为真实缺口（已 `ef47b77` 补探针）。**方法教训：声明「残留」前必须 `git fetch` 最新 main 并确认缺陷在最新提交仍未修，且不得照搬旧工单行号（已固化进 `deploy-state-verification` #52）。**

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
- 存量遗留：主池 `active` 62 条 = **40 条存量（`breakout_px` NULL）** + 22 条新产。存量的 `breakout_px` NULL ⇒ SQL 显式跳过、**永远不会 `confirmed`**，只会随 TTL 到期转 `expired`。**已定：不回填，让它自然到期**（用户认可）——理由是存量是旧 L1 判定的产物、判定根口径与现版本不一致，回填等于给旧信号套新口径，反而污染 `confirmed` 的语义。
- **✅ 延续确认链路已端到端打通（03:04:25 UTC，`confirm_signals` round 8）**：一次性升格 **2 条** `confirmed` —— `B2USDT` 15m up（sig 01:44:22, trig 0.4997, brk 0.5034）、`4USDT` 15m down（sig 01:16:56, trig 0.020859, brk 0.020506）。同时「已越位但仍是 active」的矛盾检查 **0 条** ⇒ 巡检覆盖完整。至此 L1（优先已收盘条）→ 入场/失效位锚定 → 延续确认 三段全部在生产验证通过。
- **⚠️ 任务真实节奏由「进程寿命 + offset + interval」三者共同决定，不能按名义 `interval_sec` 推**（本轮实测，勿再误判为「任务超期」）：`_run_task_loop` 的 `offset_sec` 是**线程启动时的一次性错峰 sleep**，此后每 `interval_sec` 一轮 ⇒ **每进程周期实际轮数 ≈ `floor((进程寿命 − offset_sec) / interval_sec) + 1`**。实例：`__daemon__` 02:56:25 起新进程 → `confirm_signals`(offset 480s) 精确在 **03:04:25** 跑一轮；当日 02:32:02 → 03:04:25 间隔 32min 看似超过名义 30min，**实为跨进程边界的正常相位**。
- **进程寿命实测 ≈ 74.6 min（推翻旧记「约 15 分钟」）**，由三个任务独立反算一致：`__daemon__` round 28→46 / 22.37h ⇒ 74.6min/轮（该标记每周期写 1 次）；`scan_klines`(0s, 300s) 实测 5.1min/轮 ⇒ 约 15 轮/周期；`confirm_signals`(480s, 1800s) 实测 27.8min/轮 ⇒ 约 3 轮/周期（`floor((4476−480)/1800)+1 = 3` ✓）；`expire_signals`(90s, 1800s) 实测 26.4min/轮 ✓。
- **推论**：`interval_sec ≫ 进程寿命` 的任务每周期只跑首轮 —— `prune_scan_data`(offset 600s, interval 86400s) 名义日频，实际 **~每 74.6min 一轮（≈19 次/天）**（按时间阈值删除，幂等，无害但空转）。反之 `interval_sec ≪ 进程寿命` 的任务（如 `scan_klines` 300s）稳态就是 `interval_sec` 满速跑。要收紧资源时按此公式换算，别看名义值。

**⑦ ③ 的停摆缺口回填已收口（2026-09-22 01:0x UTC 只读核对，命令见 ③）**
- 回填执行结果：K 线 `215424` 根（`5m/15m/1h` × 528 币 × 1 天，失败 `0`）、OI 历史 `95691` 行 / `191` 币（`--force` 全量重填，失败 `0`）。期间 Binance 权重偏高使间隔自动拉长到 ~5s，任务正常跑完（exit 0）。
- **K 线缺口已补齐**：近 3 天 `5m 276434 行` / `15m 92004 行` / `1h 22918 行`，三者**稀疏小时（`count(DISTINCT symbol) < 100`）均为 0** —— ③ 记载的 `1h 09-21 11:00~14:00`、`15m 11:00~21:00`、`5m 11:00~22:00` 三处缺口全部消失。注意：**`528` 不是「新水位」，是回填脚本的宇宙**（该脚本不过滤量），实时扫描宇宙是 **~250~265**（`scan_klines` 走 `min_vol_usd=5_000_000` 过滤，见 `TASK_DEFS` 3526 行）——同一小时内两个宇宙并存会让「每小时入账币数」在回填窗口跳成 528、其后回落 ~255，**别把它当成覆盖收缩**。做逐币统计（如 L1 回放）时须用与线上一致的口径（带 `min_vol_usd`），否则会混入约一半不在实时扫描范围内的低量合约。
- **OI 停摆窗口已补齐**：`09-21 10:00 ~ 09-22 00:00` 每小时均有 `backfill` 行 **191 币**（`realtime` 该窗口为 0 是预期——daemon 当时已死）；`09-22 00:00` 起 `realtime` 恢复 **248 币**。`09-21 09:00` 为切换缝：`realtime 226` + `backfill 36`（并存）。
- 遗留：`09-21 03:00/06:00/07:00/08:00/09:00` 的 `backfill` 组仅 **34~36 币**（回填源本身对该时段只覆盖子集，**与本次停摆无关**，且不在停摆窗口内）⇒ 这些小时的 OI 横截面偏薄，若后续做 OI 相关横截面统计需避开。
- 心跳：12 项任务全绿、`last_error` 全空；`scan_main_pool` round 37 / `scan_klines` round 87 / `scan_oi_cvd` round 72。

**⑧ 2 天 soak 中期验收（第 1 天，2026-09-23 01:18 UTC，只读）—— 5 项检查全过**

| # | 项 | 结果 |
|---|---|---|
| 1 | 无停摆 | 12 项心跳 `stale ≤ 12.2min`、`last_error` 全空；**逐小时覆盖检测**：`asset_klines` 5m 近 30h 的 31 个小时桶**稀疏(<100币) 0 个**（最薄 245 币），`oi_cvd_snapshot` 的 `realtime` 自 `09-22 00:00` 起稳定 ~250币 / 2300~3000行·h⁻¹ ⇒ **全天无停摆** |
| 2 | 无长缺口 | 主池小时级信号量 **26/31** 个钟点有信号；唯一空白段 `09-21 19:00~23:00` 是**修前停摆窗口**（非新缺） |
| 3 | 冷却去重 | 近 24h 主池同币对 **仅 2 对，间隔均恰为 360.0min**（`BRUSDT`、`DUSKUSDT`）⇒ 是冷却到期后的合法再信号，**无违规重复**，且 6h 边界精确到分钟 |
| 4 | TTL 收敛 | `expired_at − signal_ts` **恒为 24.000h**；主池 4 个 scenario 的 `active` 最大年龄 **23.8~24.1h**（≤24h+一轮巡检滞后，符合预期）；`status='stale'` **0 条**；蓄势池分档正确——`ACC` 900 条全 `active`（最老 159.9h < 7d ✓）、`BRK` 35 条 `expired @24h` ✓ |
| 5 | 转化率 | 近 24h 主池 **confirmed 率 45.2%**（85/188）、蓄势池 **45.0%**（59/131）；**失效位触发率 20.9%**（41/196 在 24h 内触及 8%~20% 的 stop）；每小时的 confirmed 数 1~8、全天连续无缺口 |

- L1 修法持续性：近 24h 主池 188 条信号，判定根**全部定位成功**，其中**已收盘 148（78.7%）/ 回退未收盘 40** —— 与部署当天 77% 一致，**修法稳定**。
- **⚠️ 本轮两个自伤/踩坑（均已修正，勿复现）**：① **失效位触发率初算算出 0/188 是错的** —— `biz.scan_signal` 的 `expired_at` **只对 `status='expired'` 行写入**（`active`/`confirmed` 行为 NULL），所以窗口必须用 `signal_ts + TTL` 而不是 `o.expired_at or signal_ts`，否则 `active`/`confirmed` 的窗口塌成 0 长度、必然 0 命中；② 由此「`active` 且 `expired_at < NOW()`」的检查是**真空检查**（恒为 0），要验 TTL 请用「active 的 `signal_ts` 年龄 vs TTL」。

**⑨ 2 天 soak 终期验收（第 2 天，2026-09-24 01:09 UTC，只读）—— 机制 5 项全过；收益层出现 regime 翻转**

| # | 项 | 结果 |
|---|---|---|
| 1 | 无停摆 | 12 个**真任务** `stale` 全在 `3×周期` 内、`last_error` 全空；`asset_klines` 5m 近 48h **稀疏桶 0**、1h 近 36h **稀疏根 0**、`oi_cvd_snapshot.realtime` 仅 3 处 15min 缺口（5m 采样一轮抖动，非停摆）⇒ **无停摆** |
| 2 | 无长缺口 | 主池 **46/49** 个钟点有信号；两个真空白 `09-23 19:00` / `09-23 23:00`，**同小时其他池有信号、K 线零稀疏** ⇒ 是「无币达标」而非停摆 |
| 3 | 冷却去重 | 验收窗口（近 48h）**0 违例**；全表 8 条违例**全在 `09-16~09-17` 标定期**（gap 2:15~5:45h），非新增 |
| 4 | TTL 收敛 | `main`×8 scenario 恒 **24.000h**、`accumulation.BRK` **24.000h**、`accumulation.ACC` **168.000h**(7d)；真 stale（`active` 超本池 TTL）**0 条** |
| 5 | 转化率 | `breakout_px` 非空且 <24h：**confirmed 147 / 334 = 44.0%** —— 与第 1 天 45.2% 一致，修法后稳定 |

- **⚠️ 上一轮把 `__daemon__` 的 `stale` 当「最长心跳间隔」读是误读**：它只在**启动时写一次**心跳（`_write_heartbeat(DAEMON_START_TASK)`，[L3963](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/scripts/bin/scan_daemon.py#L3963)），其 `last_run_at` = **进程启动时刻** ⇒ 该值 = **进程年龄**（实测 17.7min），**不是周期任务**。心跳检查只需看 `STALL_HEARTBEAT_TASKS` 那 11 项。
- **🔴 `confirmed` 是临时态，转化率无法事后统计**：`task_expire_signals` 的 UPDATE 覆盖 `status IN ('active','confirmed')` ⇒ **24h 后 confirmed 被覆写为 expired**（**有意设计**，见其 docstring：confirmed 是「升格」但仍属观察名单、仍受 TTL 收口）。后果：`>=24h` 组只剩 **4 confirmed / 192 expired** 作证 ⇒ 统计转化率**只能用 <24h 窗口**，不得用「已走完 24h」做分母（第 1 天 45.2% 正是 <24h 窗口口径）。
- **🔴 主池空头从未告警（regime 否决，设计使然）**：全量 down 372 条中 `alerted_at` 非空 **0 条**；up 334 条中 185 条告警。根因：所有 down 行的 `context_tags` 均带「**空头环境受限（FGI79/市值+0.71%）**」⇒ `confidence` 进不了 `high`（`_load_alert_candidates` 硬门槛 `confidence='high'`）。**在 Greed + 市值上行 regime 下系统对下跌完全沉默**（09-23 有 255 条 down 信号、0 条告警）—— 判定无误，但这是「只做多」的隐性后果，需在评估里显式承认。
- **🔴 单小时爆发 = 1 个宏观事件，不是 N 个样本**：`09-23 14:00` 那小时主池 **192 条**（窗口 14:04~14:50、去重币数 192、**189 down / 187 S4**、均跌幅 −3.96%、均量比 5.15），`09-23 15:00` 蓄势池 **103 条** 同理 ⇒ 横截面**完全相关**（大盘同步下跌）。**任何按条数计的胜率/赔率都会把 1 个 bet 当上百样本**，统计前必须按突发事件聚类或做横截面中性化。
- **🔴 收益读数出现 regime 翻转，上一轮的「24h 期望 +0.99%」未跨 regime**：`scan_edge_daily` 09-23（修法后第一个完整交易日、BTC **−2.79%**）= `win_24h 20.0%` / `avg_24h −5.75%` / `pf_24h 0.26` / `mismatch_flag=True severity=high`。结算表 24h 均收益按日：09-17 +0.91%、09-18 **+18.42%**（超额 +13.55%）、09-21 +1.96%、09-22 **−0.54%**、09-23 **−5.22%** ⇒ **日间方差主导，日级 edge 远小于单日 regime 收益**。
- **本轮三个口径踩坑（勿复现）**：① 池名是 `accumulation` 不是 `acc`（写 `'acc'` 会让 CASE 落到 ELSE、把 7d TTL 的 ACC 全判成超期）；② `biz.scan_stall_alert` **无 `created_at`**（只有 `task/last_email_ts/updated_at`，且 `last_email_ts=NULL + updated_at` 表示**已自愈复位**，非从未告警）；③ `biz.asset_klines` 的周期列是 **`interval`** 不是 `timeframe`（`scan_signal` 里才叫 `timeframe`）。
- **结论**：判定门与执行层**无缺陷**，机制稳定 ⇒ **但仍不能开总开关**（`SIGNAL_TRADE_ENABLED=0` 保持）。下一步：继续 dry-run 攒 confirmed 收益样本，且**攒样本必须按 `scan_effective_sample.py` 的 L2 `n_eff ≥ 10` 核验**（≈ 独立交易日数 × 方向数），**不能按条数或日历日数**——否则「攒够 10 个日历日」不等于 10 个独立样本。

### 横截面去相关工具（有效样本数）落地（2026-09-24，`e68035e`）

来源：⑨ 节第 2 天 soak 验收的 🔴 发现（单小时 192 条 = 1 个宏观事件）⇒ 该口径从「结论」变成「工具」。

- **✅ 交付 `05_代码与脚本/scripts/bin/scan_effective_sample.py`**（**只读 CLI、不动任何表**；与 `build_scan_edge_report.py` 同源不同工具——后者负责日报落库，本工具只回答「这些读数有多少独立样本」）。两个去相关层级，簇内**等权平均**成 1 个观测：**L1 事件级** = 同小时 × 同向；**L2 日级** = 同日 × 同向。判据 **`n_eff(L2) ≥ --min-eff`（默认 10）**，退出码 `0` 可用 / `3` 不可用（可直接作验收断言）。用法：`python 05_代码与脚本/scripts/bin/scan_effective_sample.py --days 7 [--pool main] [--json]`。
- **实跑读数（09-24，`--days 7`）**：**326 条 → L1 51 个事件 → L2 6 个日级观测**；24h 原始 `n=188 +1.35%` / L1 `n=51 +2.36%` / L2 `n=6 +2.33%` ⇒ **去相关几乎不改点估计（等权重加权，均值基本不动），改的是 `n_eff`**；而 **`n_eff=6 < 10` ⇒ 当前读数只有点估计、无统计显著性**。
- **🔴 集中度眼下被「隐性只做多」掩盖（勿误读为「已无问题」）**：工具的最大簇**只有 12 条**（`09-21 09:00 up n=12 +5.87%`、`09-23 00:00 up n=12 −5.22%`），**不是** 09-23 那 192 条 —— 根因是 `biz.scan_signal_outcome` **只收已告警样本**（实测 326/326 行 `alerted_at` 非空），而 189 条 down 被 regime 否决、**根本没进结算表**。⇒ **一旦 regime 反转（`short_fav` 转真），那批 down 会同时告警并形成巨型簇，`n_eff` 会立刻塌到个位数**。
- **🌐 `--json` 出口口径**：`odds`/`pf` 在任一侧为空时返 **`None`**（**不记 ∞**，与 `build_scan_edge_report.agg` 一致）——`Infinity` 不是合法 JSON，会把 `jq` / `JSON.parse` 打炸；输出走 `json.dumps(..., allow_nan=False)`，使任何非有限值**显式报错**而非静默写出（修前 `--json` 的 `windows` 还因代码缩进误置在 `if not args.json` 内而恒为空 dict）。
- **离线护栏**：`05_代码与脚本/workbench/test_scan_effective_sample.py`（32 项，**不连库**）—— 覆盖聚类语义 / `stat` 口径（平盘计负）/ **单一大簇不得当 N 个样本** / L1 ≥ L2 粒度单调 / `--json` 结构回归 / 退出码契约 / JSON 合法性（不含 `Infinity`、`NaN`）。

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
- **部署判定（按「本提交独有锚点」重写，复验 F1 更正）**：`21fad2d` = **已部署并生效**（判据：`metrics->'gap_metric_ver' = "2"` 首落库 `id=15 KERNELUSDT` @ 02:46:02 —— 该常量是 `21fad2d` **新增**的 ⇒ 属**本提交独有锚点**；时间线自洽：提交 02:16:40 → `__daemon__` 02:24:02 进程重建 → 02:46:02 首行带新键）。**`ef5f077` 的 daemon 改动 = 不可判定（判据未打开）** —— 它只改**拒判分支**，而 `reason LIKE '判定窗口%'` 全库 0 行、带 `head_gap_buckets` 的行**全为 `judged`**（属 `21fad2d` 产物）⇒ **本提交无任何 DB 可观测差异**（本轮新增的 `gate_ok` 位也只在两条路径里写，须事件触发）。
  ⇒ **规则（写死）：部署判定必须以「本提交独有」的字面量/字段/列/键为锚；不得引用祖先提交的判据；无锚点就写「不可判定」，并说明「需 X 事件发生后方可判」。** 待触发条件：首个 `reason LIKE '判定窗口%'` 的 `tracking` 行出现时，检查其 `metrics` 是否含 `gap_metric_ver=2` **且仍保留** `confirm`/`short_liq_ratio`（后者用于验 F5 是否修）。
- **本轮验收实跑（⚠️ 当次记录，**勿引用具体数字**）**：口径与结论方向固定，数值一律以 `calib_squeeze_liq_thr.py` 当次输出为准（爆仓表滚动 24h 全跨度 + 5m 分母逐时补齐，每次样本都换一批）。① `--days 1`：分母自证通过，条件子集跨口径上界**在判据线上下来回摆动** ⇒ 顺带实测了 P2-1 那条**从未被走到的 FAIL 负路径**（rc=2）；② `--days 7`：分母覆盖不达标 ⇒ **rc=3**，打出拒绝理由、**不打印分布**（名副其实）；③ `--days 7 --json` ⇒ `denominator_ok=false` + `judge.pass=false` + `reliable=false`（**D1 修复验证通过**）。单测 **91/91**（原 83 + D5/D6/D7 新增 8 条）。

### 轧空池 D1-D7 落地复验处置（复验_轧空池D1-D7落地_21fad2d_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池D1-D7落地_21fad2d_2026-09-22.md`。复验确认 D1~D7 + S1 **代码面 8/8 全部落地**、单测 91/91 独立复现、`py_compile` 5/5、阈值未动（23 常量逐一相同），并给出**部署判定 = 已部署并生效**；另开 **E1~E9**。**本轮仍无 DDL、无阈值变更**（复验 §7：**在补齐分子连续性之前不要再据这条判据动阈值**）。

- **🔴 E1（P1，已修）`--json` 退出码仍未与 `judge.pass` 联动**：D1 只修了 `pass` 字段本身，**JSON 分支的 `return` 仍是 `0 if denom_ok else 3`** ⇒ `judge.pass=False`（判据不通过）时进程仍返回 0，**退出码与结论相反**。已统一为：`pass → 0`；样本可用但不通过 → **2**；样本不可用 → **3**（文本分支原有的 `return 0 if j["pass"] else 2` 不变）。
- **🔴 E2（P1，本轮核心）判据「上界 < 20%」在统计上没有判别力**：同一提交在判据线两侧抖动（本次实测：两次 FAIL ↔ 三次 PASS），B 变体 n≈1.6k 时二项 **SE ≈ 1pp、95% CI 覆盖 20%**，Bootstrap 2000 次得 `P(上界 ≥ 20%) ≈ 38.5%`，**只需 +5 行数据（占样本 0.03%）即翻盘**；且同一 24h 窗口内各 4h 段上界能摆 **≈9pp**（单段必然 FAIL ↔ 另一段轻松 PASS）。⇒ 这是**统计问题、不是阈值问题**。已落地：`wilson_ci()`（Wilson 95% CI）+ `segment_upper_bounds()`（按 `SEGMENT_HOURS=4` 分桶、`MIN_SEGMENT_N=30` 以下跳过）+ `judge` 增 `upper_bound_ci95_pct` / `upper_bound_variant` / `upper_bound_n` / `decisive` / ~~`ci_decisive`~~ / ~~`segment_straddle`~~（⚠️ 这两个键名**已被 I1 改为** `ci_decisive_raw` / `segment_straddle_raw`：它们不读 `sample_ok`、仅供诊断，**判据读 `decisive`**），**`pass` 需 `decisive`**；文本区新增【统计判别力】节（CI、各段上界区间、不可判时显式告警）。**结论留档：撤回「20.50% 翻盘」是对的，但撤回之后不能顺势得出「当前 PASS」——PASS 与 FAIL 在此样本量下等权。** 另：旧文本区把「不可判」也印成 `FAIL`（与上句自相矛盾，读者会据此调阈值）⇒ 结论改**三态** `judge.conclusion` = `PASS` / `FAIL` / `INCONCLUSIVE`，文本区对应三态分别提示（不可判时明确写「不构成阈值决策依据」）；退出码语义不变（PASS→0；样本可用但得不出 PASS，含 FAIL 与不可判→2；样本不可用→3），机器可读的区分走 `conclusion` 字段。
- **🔴 E2/P2 E3（已修）「自证」只做了分母、没做分子**：`denominator_coverage()` 只验 `asset_klines`（分母），而爆仓表（分子）的时间连续性**从未被验过**——本轮实测其 24h 窗口内**只有 11 个整点有数据、最长连续空洞 14 小时**，而 `table_bound.span_hours = max(ts)-min(ts)` **照样显示「24h 连续」**（正是 E2 判别力不足的一半成因，也是上一轮「分母缺 14h ⇒ 越阈率放大 1.66×」的**镜像缺口**）。已落地 `molecule_coverage()`（逐小时直方图 + 整点覆盖 + 最长连续零行小时数）+ 常量 `MIN_MOLECULE_HOUR_COVERAGE=0.8` / `MAX_MOLECULE_HOLE_H=3`；**分母与分子都要自证通过，样本才可用**（`sample_ok = denom_ok and molecule_ok`），`--json` 增 `molecule` / `molecule_ok` / `sample_ok`。⇒ 这条落地后，本期数据 `--days 1` 会**直接 rc=3**——**那才是对的**：在只有 11 小时活跃的样本上算「/24h」越阈率没有意义。
- **🟠 E4（P2，已修）观测字段落库路径错位**：`scan_daemon.py` 拒判分支的 `track_updates` 第 9 个元素（metrics）原为 `None`，而 SQL 是 `metrics=COALESCE(%s::jsonb, metrics)` ⇒ **拒判路径永不写** `head_gap_buckets`/`mid_gap_buckets`/`gap_metric_ver`；judged 路径必经闸门 ⇒ 落库值**数学上恒 0/1**（生产实证 `id=15 KERNELUSDT`：`head_gap=0`/`mid_gap=0`/`ver=2`）⇒ **真正要观测的病例（拒判）反而落不了库**。已改为拒判分支也组装 metrics（含 `gap_metric_ver`/`head_gap_buckets`/`mid_gap_buckets`/`oi_cover`/`oi_lag_sec`），`judged_at` 仍保持 `None`。
- **🟡 E5/E6（P3，已修）分母闸门粒度**：`bars_ratio_p10` 原**只打印、不参与闸门**，而 `symbols_below` 只判「`< 0.9`」（覆盖 0.89 与 0.10 同等对待 ⇒ `5% × 248 = 12 币`可在 10% 覆盖下放行、分母被少算 10×）⇒ 已新增 `MIN_DENOM_P10 = 0.5` 并把**每币覆盖率 P10 纳入闸门**（对长尾币比「低于门槛币占比」更敏感）。
- **🟡 E9（P3，已修）`--days 7` 拒绝理由语义重叠**：整体覆盖不达标时「低于门槛币占比」「P10」**在同一数据下必然同真**（共线）⇒ 旧码一次给出三条互相重复的理由。已改为：整体覆盖不达标时**只报它**（P10 与低于门槛币数仍照常打印在【分母自证】节，属观察信息）。
- **🟡 E8（P3，已修）文档记录段自相矛盾**：本文件同一节既写「数字不在文档留档」，又在「本轮验收实跑」段写具体上界（`20.28% → 20.45%`、`19.21%`）⇒ 规则与实践冲突。已把该段改为**只留口径与摆动幅度**（「在判据线上下来回摆动」「rc 语义」「单测数」），具体数字指向脚本输出。
- **🔴 S4 / E7（既有开放项，本轮只做根因取证，修法待立项）OI 采样桶缺口**：近 4h 逐 5m 桶实测 —— 00:20 之后每桶 **247~253 行**（满格 253），缺口是**整桶零行**且**逐个出现**（00:50 / 01:00 / 01:40 / 02:20 / 03:00 / 03:20 ⇒ 近 2h `20/24`，缺 16.7%），即 **丢的是整桶（全部币一起丢），不是个别币**。
  - **根因（代码级，已定位）**：`scan_oi_cvd` 每轮把 `_bucket_5m(now)`（**轮次开始时刻**）当作 ts 一次性写全量币，而 `_run_task_loop` 的节奏是「按轮次**开始**对齐」——`sleep_time = max(1.0, interval_sec - elapsed)`（`interval=300s`）。当某轮耗时 `elapsed > interval` 时下一轮顺延到 `t + elapsed + 1`；若该轮**起点又落在桶的后段**（`r = t mod 300 ≥ ~199`），下一轮起点就跳进 `bucket+2` ⇒ **`bucket+1` 整个桶没人采样**（快照型数据不可回补，永久缺失）。⇒ 缺口 = 「偶发慢轮」×「起点相位」两个条件同时成立的产物，故呈**周期性零散**（实测约每 8 轮 1 次）。
  - **慢轮的来源**：`task_scan_oi_cvd` 每轮对 ~253 币 × 2~3 次 HTTP（OI / 标记价 / trades cursor），`workers=8`；Binance 权重压力下 `binance_http` 的自适应间隔会拉长（项目实测可到 ~5s/次）⇒ 单轮耗时在 90s~400s+ 之间摆动，**周期性越过 300s**。
  - **候选修法（择一，均需独立立项 + 生产验收）**：① **给单轮加总时限**（如 ~200s，`as_completed(timeout=...)` 后写回已收结果）⇒ 把「整桶全丢」降级为「少数落后币丢该桶」，但引入**部分桶**（需先确认判定侧对部分桶的处理口径）；② 把 `interval` 压到桶长的 1/2（如 150s）⇒ 只要单轮不超 150s 则任一桶必有采样，但**在慢轮期仍会失效**，治标；③ 采集器改为**按桶对齐发射**（起点固定在桶边界后固定偏移，`r` 恒小）⇒ 单轮超时 ≤480s 即不再跳桶，属**调度器级**改动（影响 11 个任务，需单独回归）。
  - **本轮不做修法的理由**：这是**数据采集侧的调度改动**（生产 daemon 核心循环），与阈值/判据无关，却直接影响所有 OI 下游；应独立立项、独立验收，不应搭在判据修复里顺手改。
  - **E7 连带留档（判据语义漂移）**：生产实证 `oi_cover = {have: 2, expect: 3}`（`KERNELUSDT`）与 `{have: 3, expect: 5}`（`ZETAUSDT`）⇒ **判定窗口实际只有 2~5 桶**，`MAX_MID_GAP_BUCKETS=2` 在 3 桶窗口上等于「缺 2/3 即拒判」，与它在 12 桶窗口上的「缺 2/12」**完全不是一回事**（常量语义随窗口长度漂移；**丢 1 桶 = 窗口覆盖率掉 33~50%**）。⇒ 与 S4 同源，须一并立项。

### 轧空池 E1~E9 落地复验处置（复验_轧空池E1-E9落地_ef5f077_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池E1-E9落地_ef5f077_2026-09-22.md`。复验确认 **E1~E9 代码面 9/9 落地**（`wilson_ci` 手工参考值 6 组全对、`segment_upper_bounds` 与独立实现同段同值、`molecule_coverage` 与自写 SQL 逐位一致）、阈值未动（24 常量 0 差异）、两套独立实现逐位吻合；另开 **F1~F6**。**本轮无 DDL、无阈值变更**。

- **🔴 F1（P1，方法）部署判据复用**：我写的「✅ 已部署并生效」引用的 `gap_metric_ver=2 @02:46:02 id=15` 是 **`21fad2d` 的判据**（该常量由它新增），而 `ef5f077` 的 daemon 改动**只落在拒判分支**（`reason LIKE '判定窗口%'` 全库 0 行、带新键的行全为 `judged`）⇒ 本提交**无 DB 可观测差异 ⇒ 不可判定**。已按上方「部署判定规则」改正（规则同时写入上方 D1-D7 节末尾）。
- **🔴 F2（P1，语义，已修）`rc=2` 吞掉了「不可判」与「有判别力 FAIL」**：E1 修好了「退出码与结论相反」，却把矛盾推进了一层 —— 注入矩阵里「50% 越阈的有判别力 FAIL」（S2）与「20% 越阈的不可判」（S3）**同为 `rc=2`**，而 E2 的全部论点正是这两者**等权** ⇒ 只看 rc 的下游（cron/CI/看板）仍会读成「判据明确不通过」并据此调阈值。已落地**四码**（`exit_code()` 纯函数，文本与 `--json` 同码）：**0 = PASS ｜ 2 = 有判别力 FAIL ｜ 3 = 样本不可用 ｜ 4 = 不可判**；`judge.conclusion` 由 `exit_code()` **单一真源派生**（四态：`PASS`/`FAIL`/`SAMPLE_UNUSABLE`/`INCONCLUSIVE`，与码位一一对应 —— `ec1c1c2` 时还是「三态、各自派生」，导致 `sample_ok=false` 竟印成 `FAIL`，见下文 G1），文本区在判据行后直接打印退出码含义。**（上一轮已把「不可判」从 FAIL 文案里拆出来，本轮补齐码位。）**
- **🟠 F3（P2，已修）`MIN_DENOM_P10` 闸门逻辑不可达 + 对目标场景无感**：① `pct()` 是**线性插值**分位 —— 注入「300 币里 15 币覆盖率 0.49」时 `k=299×0.10=29.9` 落在好币区间 ⇒ **P10 仍输出 1.000**，恰好看不见要抓的 5% 尾部；② `P10 < 0.5` ⇒ 至少 10% 的币 < 0.5 < 0.9 ⇒ **必先触发** `MAX_DENOM_BELOW_PCT` ⇒ 被数学支配、永不单独触发（E9 想消除的「重复理由」又回来了）。⇒ 改为 `MIN_DENOM_HALF_COVERAGE=0.5` + `MAX_DENOM_BELOW_HALF_PCT=2.0`（「覆盖率低于半格的币占比」），与「低于门槛占比」**不共线**（占比落在 2%~5% 区间时可单独触发，正是目标场景）；P10 降级为**纯观察项**（打印时标注）。同时把 E9 的「只报最重一条」原则应用到该分支。
- **🟠 F4（P2，已修）`MIN_SEGMENT_N` 判错分母**：守卫判的是**段内总行数**，而 `rate()` 的分母是**变体过滤后**子集 ⇒ 段内塞 1 行「仅某变体命中且越阈」即可把该变体 rate 拉到 100%，**1 行（占样本 0.3%）就能把「不跨线」翻成「跨线」** ⇒ `decisive` 被单条观测操纵（反向亦然：可掩盖真实跨线）。已改为**逐变体判 n**（不足者不进该段），并在 `segments[].n_by_variant` 暴露各变体 n 供人核对。
- **🟠 F5（P2，已修）E4 引入「入场指标被覆盖」的数据丢失副作用**：落库 SQL 原是 `metrics = COALESCE(%s::jsonb, metrics)` —— **整对象替换**，而 `tracking` 行的 `metrics` 还承载**入场指标**（`confirm`/`oi_chg_pct`/`short_liq_ratio`/`cvd_ratio`）；拒判路径写整对象会把这些**永久覆盖掉**（该行仍是 tracking ⇔ 未判定，后续再也还原不了入场依据）⇒ **为改善可观测性反而毁掉另一类可观测性**。已改为**合并**语义：`metrics = COALESCE(metrics || COALESCE(%s::jsonb, '{}'::jsonb), metrics)`（`%s` 为 NULL 时原样不动，保住原语义；judged 路径一并受益——此前它同样覆盖，属既有行为）。
- **🟡 F6b（已修）`wilson_ci(k > n)` 抛 `ValueError: math domain error`**：`p(1-p) < 0` 无入参守卫 ⇒ 已夹取 `k ∈ [0, n]`（脚本内恒 `k ≤ n`，但纯函数被复用/注入时不该崩）。
- **🟡 F6c（已修）本轮改动零单测**：`test_squeeze_battle.py` 此前对 `wilson_ci`/`segment_upper_bounds`/`molecule_coverage`/退出码**断言数 0**，只报「单测 91/91」会误导（它只覆盖未改动的 `squeeze.py`）⇒ 新增 16 条断言（`wilson_ci` 5 例含报告参考值、`exit_code` 四态 6 例 + 码位互斥、`segment_upper_bounds` F4 守卫 2 例、`molecule_coverage` 边界 2 例 + 伪游标）⇒ **107/107**。
- **🟡 F6f（已修）`judged` 路径的 gap 位恒 0/1**：judged 必经闸门 ⇒ `head/mid_gap` 数学上只能 0/1，E4 落地后 judged 行仍**不携带有效病例信息** ⇒ 两条路径统一补落 `gate_ok` 布尔位（拒判 `False` / 判定 `True`），可直接数出「通过 vs 拒绝」分布。
- **🟡 F6e（已修）「不留数字」规则缺例外条款**：规则原写「数字不在文档留档」但全文件仍有大量数字（多在历史引用/根因陈述位，带时间与前提标注）⇒ 规则与实践并存的正是 E8 要消除的那类矛盾。规则改为：**判据位不得留数字；历史引用/根因陈述位可留，须带时间与前提标注**（并注明「以本次实跑为准」）。
- **📌 F1 的部署侧待验（本条不可判定）**：首个 `reason LIKE '判定窗口%'` 的 `tracking` 行出现后，检查其 `metrics` 是否含 `gap_metric_ver=2`、`gate_ok=false`，**且仍保留** `confirm`/`short_liq_ratio`（验 F5）。
- **📌 审计链更正**：本节（F 段）**并非由 `ec1c1c2`（`a41690f` 的父提交也不含）携带** —— 它被并发进程误扫进 **`c9478af`（「O1 费率覆盖缺口」）+ `a1f45e0`（「O1 收尾」）**；`a41690f` 的 numstat **只动 `llm_client.py`**。内容正确、已在 `origin/main`，仅「谁携带了这条规则」需以 `c9478af`/`a1f45e0` 为准。

### 轧空池 F1~F6 落地复验处置（复验_轧空池F1-F6落地_ec1c1c2_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池F1-F6落地_ec1c1c2_2026-09-22.md`。复验确认 **F1~F6 代码面全部落地**（`py_compile` 5/5、单测 107/107、四码注入矩阵全走到、`squeeze.py` 逐字节未改、阈值 24 常量 0 差异）、工区↔blob 全 SAME、独立复算在**同一时点钉死**下与真码分类等价（19008 行全部逐位一致，仅数值表示层差异）；另开 **G1~G5**。**本轮无 DDL、无阈值变更**。

- **🔴 G1（P2，已修）`judge.conclusion` 未与 `sample_ok` 联动 —— 同一缺陷类第三次复发**：真码实跑 `--days 7 --json` 打出 `sample_ok=false` + `conclusion="FAIL"` + `reliable=false` + `rc=3` —— **四处口径互相打架**。根因：`conclusion` 由 `pass`+`decisive`/`segment_straddle`（该键名**后被 I1 改为** `segment_straddle_raw`）**另算一遍**、计算路径**完全不读 `sample_ok`** ⇒ 样本不合格时仍能打出「FAIL」这种强判定词。谱系：D1 `pass` 与 rc 相反 → F2 `decisive` 与「有判别力 FAIL」共用 rc=2 → 本轮 `conclusion`。**`--days 1` 恰好因 `segment_straddle=true` 输出 `INCONCLUSIVE`，掩盖了该 bug**（只有 `--days 7` 这种「straddle=false 但 sample 不合格」的组合才暴露）。⇒ 改为 `CONCLUSION_BY_CODE = {0:PASS, 2:FAIL, 3:SAMPLE_UNUSABLE, 4:INCONCLUSIVE}`，由 `exit_code()` **单一真源映射**，`judge` 同时落 `exit_code` 字段；**杜绝再有第四个字段各自为政**。
- **🟡 G3（P3，已修）`exit_code()` 未被所有出口复用 + 状态机未穷尽**：① 文本分支的 `return 3` 是**硬编码字面量**（另两处走 `exit_code()`，改码表时会分叉）⇒ 三处出口统一成同一个 `rc` 变量；② `ub_sub is None`（**无任何变体命中** ⇒ 上界根本算不出来）旧码落到 **rc=4**，而 4 的文档语义是「样本可用、只是判据不具判别力」，两者不同源 ⇒ 给 `exit_code()` 增 `measurable` 参数并**并入 3**（同属「任何比率都不成立」）。
- **🟠 G4（P3，已修；判据后续被 H4 更正，字段名后续被 I1 改为 `ci_decisive_raw`）`ci_decisive` 自身是刀锋判定**：实测 CI = `[20.01, 23.54]`，下界对判据线 20.00% 只差 **0.01pp** 即判「有判别力」—— 布尔化把「差 0.01pp」与「差 10pp」印成同一句话，而前者远在任何统计误差之内 ⇒ 实质掷硬币。⇒ 新增纯函数 `ci_margin_pp()` 并落 `judge.ci_distance_pp`，文本区补印裕度。⚠️ 初版取**绝对值**，被 H4 判为丢方向；现为**带符号**（见 H 段）。
- **🟠 G5（P3，已修；判据后续被 H3 更正）E9 去重只作用于分母组**：`--days 1` 分子组仍打出**2 条**理由（① 整点覆盖 41.7% ② 最长空洞 14h），而单段长空洞**必然**同时压低覆盖率 ⇒ 读者读成「两个问题」而实际只有一个。⇒ 抽出纯函数 `molecule_fail_reasons()`（同 `gap_reason` 的 D6 先例，便于单测）折叠同源理由。⚠️ 初版折叠判据用**门槛相对**（`hole > expect × (1-门槛)`），被 H3 判为印错因果 + 吞掉独立缺陷；现用**观测相对**（见 H 段）。
- **🟡 G2（P3，已修）文档残留与自述偏差**：① 本节上方（工单 `SQZ-2026-09-21`）原写 `COALESCE(%s::jsonb, metrics)`「以免覆盖 entry metrics」——**该 rationale 已被 F5 证伪**（它正是覆盖的成因），且无更正指针；已就地加删除线 + ⚠️ 证伪标注 + 「勿据其行事」。② F2 条原写「`conclusion` 三态与四码一一对应」不成立（3 态 vs 4 码，且 `sample_ok=False` 时可为 `FAIL`）⇒ 已改为「由 `exit_code()` 单一真源派生」。③ 我上一轮自述「AGENTS.md 已随 `a41690f` 推送」**不准确**，审计链更正见上方 F 段末条。
- **✅ 部署态 = 已部署并已生效（判据已打开 —— 复验_轧空池G1-G5处置_e719a87 §5.1 翻盘）**：`ec1c1c2` 提交后同一锚点已出现 —— `metrics ? 'gate_ok'` **1 行**（`biz.squeeze_track` id=19，`judged_at` 06:55:59Z 晚于提交 06:00:01Z），且该 `judged` 行**携带 `confirm`/`timeframe`/`oi_chg_pct`**（verdict 只给 `d_oi_pct`/`cvd_ratio`/`long_liq_ratio`/`top_ratio_chg`/`taker_ratio`/`data_missing`，`confirm` 只在 entry 写入）⇒ 只可能由 **F5 合并语义**保留 ⇒ **F5 已生效**；`gate_ok` 经 `git log -S` 核实为 `ec1c1c2` 独有锚点。**仍未触发**：指纹①（**拒判**路径）—— 首个 `reason LIKE '判定窗口%'` 的 tracking 行含 `gate_ok=false` 且**仍保留** `confirm`/`short_liq_ratio`。

### 轧空池 G1~G5 处置复验 H1~H5（复验_轧空池G1-G5处置_e719a87_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池G1-G5处置_e719a87_2026-09-22.md`。复验确认 **G1~G5 代码面全部落地**（`py_compile` 2/2、单测 119/119、四码注入全走到、`squeeze.py` 逐字节未改、AST 常量 17→18 仅 +1 无值变化）、G1 在 prod 真实样本上**四处口径首次完全一致**；另开 **H1~H5**。**本轮无 DDL、无阈值变更**。

- **🔴 H3（P2，已修）折叠判据用「门槛相对」而非「观测相对」—— prod 已实际印出错误因果**：`hole > expect × (1-门槛)` 只保证「空洞**足以使**覆盖不达标」，**不等于**「空洞**解释了实测**覆盖率」。prod `--days 7`：`hole=139h`、`missing=168-15=153h` ⇒ 空洞最多把覆盖压到 `1-139/168 = 17.3%`，而实测 **8.9%**（另有 14h 散点缺失）⇒ 旧文案「该空洞自身已足以把整点覆盖压到 8.9%」是**事实错误的因果**，且把「散点缺失」这个**独立缺陷**吞掉（正是 G5 想避免的反向错误：实际两个问题被读成一个）。⇒ 判据收紧为**观测相对**「该空洞**即窗口内的全部缺失**」（`hole ≥ expect - present`），折叠只在真同源时发生；否则覆盖那条须**点明**「空洞仅占 Xh ⇒ 另有散点缺失」。**实测**：`--days 1`（14h 恰为全部缺失）仍折叠为 1 条；`--days 7` 改为 2 条且带散点说明。
- **🔴 H1（P3，已修）`reliable` 未与单一真源联动 —— 同一缺陷类第四次复发**：G1 把 `conclusion` 绑到 `exit_code()` 后该函数入参多了 `measurable`，而 `"reliable": sample_ok` **没跟上** ⇒ 出现 `conclusion="SAMPLE_UNUSABLE"` 却 `reliable=true` 的矛盾。谱系：D1 `pass` ↔ rc → F2 `decisive` ↔ rc=2 → G1 `conclusion` ↔ rc → **本轮 `reliable`**。⇒ 直接由 rc 派生（`"reliable": rc != 3`，⟺ `sample_ok and measurable`），**此字段收口**。
- **🟠 H2（P3，已修）文本分支对 `SAMPLE_UNUSABLE` 误印「判据不通过且具备判别力」**：该 `elif not j["pass"]:` 本是为 rc=2 写的，`conclusion` 新增第四态后 rc=3 的另一来源也落进来 ⇒ 双重误述（该 case `decisive=False` 根本谈不上判别力；实际原因也非「判据不通过」）。⇒ 改为按 `conclusion` 精确分派（`INCONCLUSIVE`/`FAIL`/`SAMPLE_UNUSABLE` 三分支）。
- **🟠 H4（P3，已修）`ci_margin_pp` 取绝对值 ⇒ 丢失方向**：`[10,15]` 与 `[25,30]` 对线 20 都返回 `5.0`，**裕度同值但结论相反**（PASS 侧 vs FAIL 侧），而判据是**有向**的（上界须 `<20%`）。⇒ 改**带符号**：正 = CI 整段在判据线下方（PASS 侧）、负 = 上方（FAIL 侧）、`0.0` = 已跨线；文本区并写明所在侧。**实测**：`+5.00pp ⇒ PASS 侧` / `-5.00pp ⇒ FAIL 侧`。
- **🟠 H5（P3，已修）`ub_sub=None` 时文本仍印「上界由 `None` 决定」+「CI 含 20%？YES」**：把「**没有**上界可算」说成「**CI 含**判据线」，借用了 `ci_decisive=False` 的既有措辞。⇒ 单列该状态（「无任何变体命中 ⇒ 上界**不可算**（measurable=False）⇒ 退出码 3」）并**跳过** CI 与裕度行。
- **📌 补测（待拍板 #7）**：12 行 `check` 变更（**净增 9 条**断言）⇒ 119→**128/128**，含 H3 反例（prod 真值 139/153）、H4 两侧 + 跨线、以及 **G1/H1/G3/H2/H5 的源码级守卫**（同类已四次复发，不再只靠人工复核 —— 沿用「测试3」扫源码的既有先例）。⚠️ 原句「新增 12 条断言 ⇒ 128/128」不自洽（119+12=131≠128），由复验 **I2** 指出并更正（12 是新增**行数**，含 3 行对既有断言的改写）。
- **📌 阈值仍一律不动**：prod 样本 rc 恒为 3（分母 0.565 / 分子 8.9% 双不合格）⇒ 依旧无标定依据。**数据面根因仍是最大杠杆**：`liquidation_snapshot` 29h 内缺 153h、`asset_klines` 7 天覆盖仅 0.565。

### 轧空池 H1~H5 处置复验 I1~I6（复验_轧空池H1-H5处置_5597ed3_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池H1-H5处置_5597ed3_2026-09-22.md`。复验确认 **H1~H5 代码面 5/5 落地**（`py_compile` 2/2、单测 128/128、四码注入矩阵全走到、H3 折叠判据**全枚举穷尽性不一致点数 0**、`squeeze.py` 逐字节未改、calib 阈值类常量 0 值变化、顶层常量 18→20（+`RELIABLE_BY_CODE`/`DECISIVE_BY_CODE` 两张映射表））、prod 四模式实跑文本与自述**逐字一致**；另开 **I1~I6**，并给出方法论修正 **M1**。**本轮无 DDL、无阈值变更**。

- **🔴 I1（P2，已修）`judge.decisive` / `ci_decisive` 仍未与 `sample_ok` 联动 —— 同一缺陷类第五次复发，且 prod 已实际印出**：prod `--days 7 --json` 实测 `sample_ok=false` + `exit_code=3` + `conclusion="SAMPLE_UNUSABLE"` + `reliable=false` + **`decisive=true`** 并列 —— 正是 G1 的定义式措辞「样本不合格时仍能打出强判定词」，下游读 JSON 会把「样本不可用上的 CI 读数」当成「已有有判别力的判断」。谱系：D1 `pass` ↔ rc → F2 `decisive` ↔ rc=2 → G1 `conclusion` ↔ rc → H1 `reliable` ↔ rc → **本轮 `decisive`/`ci_decisive`**（技能 `#60`「修好一个『判据字段各自为政』后必须收割所有兄弟字段」的未收割残余）。⇒ 取报告建议 **B（保留原始诊断，不为「收口」丢信息）**：`decisive` 收口到 `DECISIVE_BY_CODE[rc]`；CI/分段的原始性质改名 `ci_decisive_raw` / `segment_straddle_raw` 并加 `raw_note` 注明「未与 `sample_ok` 联动，仅供诊断；判据请读 conclusion / reliable / decisive / exit_code」。
- **🟡 I3（P3，已修）`"reliable": rc != 3` 是「非 3」式写法，与 `CONCLUSION_BY_CODE[rc]` 鲁棒性不对称**：同一份 `judge` 里 `conclusion` 走映射表 ⇒ 码表扩展**KeyError 炸响**（fail-loud，好）；而 `reliable` 写 `!= 3` ⇒ 第 5 码出现时**静默判「可靠」**（fail-silent，差）。⇒ 改 `RELIABLE_BY_CODE = {0:True, 2:True, 3:False, 4:True}`，与 `DECISIVE_BY_CODE = {0:True, 2:True, 3:False, 4:False}` 同构；并加守卫断言**三表键集必须一致**（新增码位时三处**同时**炸响）。
- **🟡 I2（P3，已修）自述/文档的断言计数不成立**：「12 条新断言 ⇒ 128/128」中 119+12=131≠128 —— 12 是**新增行数**，其中 3 行是对既有断言的**改写**（`ci_margin_pp` 两例 + `_f1`/`_f2` 折叠语义）⇒ **净增 9 条**（`check(` 53→62、套件 119→128）。已同步更正上方 H 段该句措辞。
- **🟡 I4（P3，已修）源码级守卫耦合注释措辞**：`check('elif not j["pass"]:' not in _calib_src …)` **仅因** calib 注释里写的是无冒号版本才通过 —— 一旦有人在注释/文档里写出带冒号的同名字面量就会**误报失败**（假阳性）。⇒ 新增 `_code_only()` 并全部改用 `_calib_code` 匹配；注释不是代码，守卫不该耦合注释措辞。（复验 **J2**：初版按 `ln.split("#",1)[0]` 是**文本**剥离，而 calib 模块 docstring 的用法示例经 `tokenize` 实测含 1 处 `#` ⇒ 已改 **`tokenize` 词法剥离**，只丢 COMMENT token、字符串原样保留。）
- **🟡 I5（P3，已修）`hours_present_ratio` 的分母与折叠判据不同源**：ratio 原写 `present / len(hist)`（**网格实际长度**），而折叠判据用的 `missing_hours = expect_hours - hours_present`（**期望整点数**）⇒ 二者当前恒等（**死耦合**），一旦网格定义变更（如含当前小时）就会印出「分母不一致的一对数字」。⇒ 分母统一为 `expect_hours`；`grid_hours` 仍保留 `len(hist)` 供对照。
- **🟡 I6（P3，已修）H5 文案硬编码「退出码 3」**：紧邻判据行用 `j['exit_code']` 动态取值，而 H5 那行 prose 写死 `3` ⇒ 码表变动时 prose **静默漂移**。⇒ 改用 `{rc}` 动态引用。
- **📌 M1（方法论修正，复验 §5.3）部署判定取证：`git log -S <literal>` 必须路径限定**：本轮核查「`decisive` 由谁引入」时若无路径限定，命中 4 个提交**全部是噪音** —— 噪音全部来自本文件（`AGENTS.md`）里的审计叙述（散文把字面量写进了文档）⇒ 取证一律 `git log -S <literal> -- <代码路径>`。
- **📌 补测**：`check(` 62 → **69**（+7：I1/I3 映射表值 + 键集一致 + `sample_ok=False ⇒ decisive/reliable=False` 共 4 条，源码守卫由 5 条扩到 8 条 +3）⇒ 单测 **135/135**。
- **📌 部署判定 = 不涉及**（离线标定工具 + 单测 + 文档，无 daemon/scheduler 入口；同 `e719a87`/`5597ed3`）。复验另**独立复核**了本文件对 `ec1c1c2` 部署态的翻盘断言 → 数字全部吻合（§5）。
- **📌 本轮验证**：`py_compile` 2/2；单测 **135/135**；prod `--days 7 --json` / `--days 1 --json` 均 `sample_ok=false` + **`decisive=false`（I1 实测收口）** + `reliable=false` + `conclusion="SAMPLE_UNUSABLE"` + `exit_code=3`；`--days 7` 文本拒判**2 条**（含「另有散点缺失」）、`--days 1` 文本拒判**1 条**（折叠）；文本出口另用**伪连接注入探针**驱动（prod 因 `sample_ok=False` 提前 return、覆盖不到【统计判别力】的分段行）⇒ 探针 rc=4，`segment_straddle_raw` 引用不炸、分段行正常打印「同向」。
- **📌 阈值仍一律不动**：prod 样本 rc 恒为 3（`--days 1` 分母 0.993 合格 / 分子 10/24=41.7% + 14h 空洞；`--days 7` 分母 0.568 / 分子 16/168=9.5% + 138h 空洞）⇒ 依旧无标定依据。**数据面根因仍是最大杠杆**：`liquidation_snapshot` 29h 内缺 152h、`asset_klines` 7 天覆盖仅 0.568。

### 轧空池 I1~I6 处置复验 J1~J4（复验_轧空池I1-I6处置_64bc948_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_轧空池I1-I6处置_64bc948_2026-09-22.md`。复验确认 **I1~I6 代码面 6/6 全部落地**（`py_compile` 2/2、单测 135/135、注入四码回归 + I1/I3 四处口径全一致、`squeeze.py` 逐字节未改、阈值常量抽检全同值）、**I1 经 prod `--days 7 --json` 实证已 mask**（`ci_decisive_raw=True` ⇒ 旧口径必为 `decisive=True`，新码 `False`）；**改名零破坏**（全库无任何消费者读被改名的 `judge` 键）；**不涉及部署判定**（自证）。另开 **J1~J4（全 P3）**，并给出观察项 **O1~O4**。**本轮无 DDL、无阈值变更**。

- **🟡 J1（P3，已修）自述「calib 顶层常量 18→18 零变化」不成立 —— 实测 18→20**：+`RELIABLE_BY_CODE` / +`DECISIVE_BY_CODE` 两张映射表（0 删除、0 值变化）。实质主张「**阈值未动**」为真，错的只是括号里的**计数**。⇒ 已改为「阈值类常量 0 值变化；顶层常量 18→20（+2 映射表）」。**同类第二次**（与 **I2** 同性质）：自述里的**可核验计数**未与既有计数器口径对账。
- **🟡 J2（P3，已修）自述「已核 calib 无字符串内含 `#`」不成立 —— 实测 1 处**：`tokenize` 实测 1 个 STRING 含 `#` = 模块 docstring 用法示例（`# 默认 1 天窗口`）。**当前无实害**（全部守卫字面量均位于纯代码行 L616–L784），但 `ln.split("#",1)[0]` 是**行内文本**剥离而非**词法**剥离 ⇒ 潜在误伤面真实。⇒ 已把 `_code_only()` 改为 **`tokenize` 词法剥离**（只丢 COMMENT token、字符串原样保留），并加守卫断言「字符串内的 `#` 不被截断、注释整行被删」。
- **🟡 J3（P3，latent，已修）I5 改分母后 `hours_present_ratio` 无上 clamp**：网格为 `expect` 超集时（如 `hist=29 / days=1`）`present=29 > expect_hours=24` ⇒ 旧式会得 `ratio=1.2083`，同时 `missing_hours = max(0, 24−29) = 0` ⇒ 印出「**覆盖率 120.8% 而缺失 0h**」的自相矛盾 —— 正是 I5 想消除的那类不一致的**镜像版**。prod 当前不可达（`generate_series(expect_hours)` ⇒ `grid ≡ expect`），但 I5 的动机场景（网格定义变更）本身就会命中 ⇒ 属「修 A 引入的潜在 B」。⇒ `round(min(1.0, present / expect_hours), 4)` 夹取 + 单测钉住（`_FakeCur([1]*29)` ⇒ ratio 夹到 1.0 且 `missing_hours=0`）。
- **🟡 J4（P3，文档残留，**G2 同类第二次**，已修）上游条目仍以「现值」口吻命名已退役键**：E2（L422）/ G1（L455）/ G4（L457）三处写 `ci_decisive` / `segment_straddle`，而二者已被 I1 改名 ⇒ 与 G2（加删除线 + 证伪指针）确立的项目惯例不一致。⇒ 三处**就地加指针**「字段名已被 **I1** 改为 `*_raw`（判据读 `decisive`）」。
- **📌 O1~O4（观察项，无需动作，留档）**：① `raw_note` 无机器消费者（只对「人读 JSON」生效，**不拦下游代码**）；② `decisive` 现为 `exit_code∈{0,2}` 的**纯函数**、与 `exit_code` 信息冗余 —— 「CI 是否有判别力」的真实信息**只在 `ci_decisive_raw`**，字段名 `decisive` **已不承载该语义**（选 B 保留原始诊断的代价）；③ 并发 WIP 仍可能在 index（提交前须 `git status` 复核）；④ `--days 1` 分母打印「覆盖小时中位 25/24」跨整点边界，无实害。
- **📌 补测**：`check(` 69 → **71**（+2：J2 词法剥离守卫 + J3 超集网格夹取断言）⇒ 单测 **137/137**。
- **📌 本轮验证**：`py_compile` 2/2；单测 **137/137**；prod 四模式 rc 全 3。
- **📌 阈值仍一律不动**：prod 本轮实跑 `--days 1` 分母覆盖合格但「低于半格币占比 2.6% > 2.0%」拒判、`--days 7` 分母 0.620 < 0.9 且分子 32/168 = 19.1% + 122h 空洞 ⇒ 双不合格，仍无标定依据（数字随表滚动，一律以本次实跑为准）。**数据面根因依旧是最大杠杆**。

### 盘面异动告警邮件「深层补刀」O2~O6 处置（审计_盘面异动告警邮件_3币1币_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_盘面异动告警邮件_3币1币_2026-09-22.md`。审计结论「数据 100% 与 prod 库吻合、渲染无缺陷」；本轮处置其 §八「深层补刀」的 4 项标注语义/信息架构问题 + §四的 O2。**全部落在 `scan_daemon.py` 渲染层 + 一处告警落库，零 DDL、零迁移、无阈值变更**。

- **O2（展示）中性催化不计方向**：主题「催化剂 4多/0空/2中」易被扫读为强多 ⇒ 主题与卡片括注补「**净多 = 多−空**」（`bull != bear` 时才输出，避免「净多0」）。实测本批 BTW+PHA+EPIC 主题：`4多/0空/2中，净多4`。
- **O3（⚠️ 疑似误导，已改）失效位触夹带上下限仍冒充「2×ATR(14)」**：近 30 天 S1 共 66 条有 `stop_loss_pct` 的 14 条中 **8 条 = 8.00（57%）**，被夹到下限却仍标「2×ATR(14)」⇒ 风控读者误判波动幅度。现按常量判定：`sp <= STOP_PCT_MIN` → 标「**已触下限 8%（真实 2×ATR 更窄）**」；`sp >= STOP_PCT_MAX` → 「已触上限 20%（真实 2×ATR 更宽）」；带内才保留原文案。图例同步加「已触下限/上限」说明。（**未**加显式 ATR 数值列——需在生产者落原值，另议。）
- **O4（⚠️ 建议改，已取②）高置信池内质量离散**：EPIC（+2.90% / 0 催化 / 费率 n/a / CVD 反向）与 BTW（+9.32% / 5 催化 / CVD↑）同列 HIGH。现对「**资产已关联 且 催化剂确为 0 且 费率未覆盖**」的卡片加淡提示「纯技术面信号（无催化剂、费率未覆盖），缺基本面确认」。**未做①（展示置信分数）**——`confidence` 只有 high/medium/low 文本、无数值分，加分数需新增算分逻辑，属另立项。
- **O5（🔶 已统一口径）「共振」名实不符**：邮件「共振」= 事件+催化剂+KOL 三段聚合（`_get_resonance` 实时查），与 `biz.catalyst_resonance.resonance_score`（超额收益方向匹配）是**两套语义**。图例显式写明「本邮件共振 = 三段聚合，非 `catalyst_resonance` 表的超额收益方向匹配评分」。**未**把 `resonance_score` 纳入卡片（需评估权重与 d3「已定价」语义重复问题，见待办）。
- **O6（🔶 已修，沿用 PROC-1 精神）共振快照未落库、发信后不可回放**：主池 `scan_signal.detail` 此前恒 NULL，共振在渲染时实时算、催化剂 7 天窗口漂移后无法字节级回放。现 `task_scan_alert` 发信成功后把**三段明细（含全量结构化催化剂 title/dir/strength/date）+ captured_at** 落 `detail`（`COALESCE(detail,'{}'::jsonb) || 快照`，jsonb 直接容纳、不覆盖 squeeze 池既有 metrics）。`_get_resonance` 新增只写不渲染的 `catalyst_all`。
- **N1（沿用，本轮顺带落地）催化剂新鲜度**：`_get_resonance` 增 `catalyst_latest` / `catalyst_stale`，卡片括注渲染「最新 YYYY-MM-DD」「含 N 条 >3 天」（常量 `CATALYST_STALE_DAYS=3`）。**未缩短 7 天窗口**——窗口语义与 catalyst_signal 对齐，缩短会改共振口径，只做披露。
- **O1（数据覆盖，未修，仍开放）**：近 24h S1 信号 47% 缺 `funding_rate`，根因是 `biz.asset_derivatives` 对 BTW/PHA/SEI/EPIC 全空（641 行内无这些 symbol），属下游采集白名单/覆盖问题，**需另立工单**排查 `phase_derivatives_batch` 的覆盖范围；邮件 `n/a` 渲染本身正确。
- **自测**：新增 [test_scan_alert_audit_deepdive.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_scan_alert_audit_deepdive.py)（**24/24 通过**，纯离线：O2 净方向 + O3 触限/带内 + O4 四例（含「未关联≠无催化」）+ O5 图例 + N1 三例 + O6 源码 AST + `**` 护栏）；既有 `test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_squeeze_battle.py` 91/91 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效。

### O1 费率覆盖缺口修复（工单 `待修复工单_O1_费率覆盖缺口_2026-09-22.md`，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\待修复工单_O1_费率覆盖缺口_2026-09-22.md`（`de75b8c` 复验随附）。**采方案 A（推荐）**，无 DDL、无阈值变更、不动信号侧。

- **根因（工单已坐实）**：衍生品摄取 universe = `core.asset` 里「`market_cap_rank` 非空 + 按排名取前 `--limit`（默认 100）」；而信号 universe = 全部 Binance USDT 永续（含 meme/小市值 rank 222~6777）。两者结构性错位 ⇒ 近 24h S1 信号 14/33（42%）无 `funding_rate`，邮件只能渲染 `n/a`。`_load_funding_map` 只读 `biz.asset_derivatives`、信号创建时不实时拉 Binance，故缺口完全传导到告警。
- **修复（`phase_derivatives_batch.py`）**：新增 `--signal-days`（默认 **7**，0=关闭）+ `_signal_symbol_candidates()`（与 `scan_daemon._symbol_candidates` 同口径：原样→去 USDT→去 `1000/1000000` 前缀）+ `get_signal_gap_assets()`——取近 N 天 `pool='main' OR scenario='BRK'`（funding 的**消费方**，squeeze 池不用）出现过、且**符号级**尚无 `funding_rate` 的资产，经 `core.asset` 反查 asset_id。
  - **符号级覆盖 + 按符号去重**（本轮实测修正）：`_load_funding_map` 按**裸符号**注册候选 ⇒ 只要 `asset_derivatives` 任一行（任一 asset_id）有费率即已覆盖。且 `core.asset.canonical_symbol` 不唯一（9.9% 重复、单符号最多 18 行）——按行取会把 DOGE/BTC/SOL 算成十几个「缺口」（实测虚高 **50→150**）并重复入库 ⇒ 用 `DISTINCT ON (upper(canonical_symbol))` + `market_cap_rank NULLS LAST, asset_id` 只取最优那条（与 `_get_asset_id` 选择一致）。修正后缺口真实量 **73**。
  - `main()` 拓扑：**缺口资产置顶 + 不受 `--limit` 截断**，抽为纯函数 `merge_pending(ranked, gap, limit)`（`cap = max(limit, len(gap))`，`limit<=0` 不截断，按 asset_id 去重）——否则缺口（rank>100）永远排不到，bug 复现；其余按市值补齐。稳态增量有限（采到即离开缺口集）。
  - `ingest_run` 的 scope/params 同步带上 `signal_days`。
- **连带修复（本质是 O1 的「最后一公里」）`_aggregate_funding`**：原聚合把资金费率累加**嵌在 `if oi_val` 内** ⇒ 交易所未返回 OI 价值（小市值/meme 常见）时**即便费率可取也被整体丢弃**、`avg_funding=None`。实测补采后 MAGMA/UNI/XEC/MIRA/VTHO 仍 NULL（有 1~4 家交易所报费率、但无 OI 价值）⇒ 该类符号**永远补不上 funding**，O1 名义完成但实际无效。已抽为纯函数并改为：有 OI 价值按 OI 加权（**口径不变**），无 OI 价值退化为**等权简单平均**（有费率即可），严格提升覆盖率。
- **调度实际 `--limit` 已确认（`scheduler.py:111`）**：`phase_derivatives_batch.py --limit 200 --delay 0.2`，每 6 小时一次（`30 */6 * * *`）。故缺口仅挤掉当轮少量 top-N 刷新（`cap=max(200,缺口数)`），一次性、缺口清空后恢复。**无需改调度**（`--signal-days` 默认 7 即生效）。
- **✅ prod 实测收口（2026-09-22，本机出口对 Binance/OKX 等已恢复可达）**：`--limit 73` 跑批 **73/73 成功**（1266s，67 家有合约）→ 缺口 **73 → 7**；剩余 7 正是上述聚合 bug 的产物，修 `_aggregate_funding` 后 `--limit 7` 再跑 **7/7 成功**（140s）→ **缺口 0**。库里 BTC/DOGE/SOL/EPIC/MINA/UNI/XEC 等均已见非 NULL `funding_rate`。
- **未覆盖**：无 `core.asset` 行的 symbol（如 BROCCOLI714）属主数据治理缺口，本函数无法覆盖（`COUNT` 自然缺失）；工单 §六.4 的 `canonical_symbol` 重复/rank 冲突（JOE/EPIC/STAR/AGT）建议并入 W5 治理工单，本轮不扩 scope（**但其对 O1 缺口虚高的影响已由「符号级去重」消除**）。
- **方案 B（信号侧实时拉取）不采纳**：本机出口曾被 Binance 判 418，且不解决 OI/CVD 缺口。
- **自测**：新增 [test_derivatives_signal_gap.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_derivatives_signal_gap.py)（**35/35 通过**：归一化 6 例 + 源码 AST + `merge_pending` 8 例 + `_aggregate_funding` 7 例（含无 OI 退化）+ `--signal-days 0` 恒空 + 只读 prod 缺口不变量）。prod 只读实测收口后缺口 **0**。
- **验收命令**（三段式，部署/跑批后）：`python bin/phase_derivatives_batch.py --limit 200`（与调度一致），日志应显示「信号 universe 缺口 N」；随后库里原缺口 symbol 出现 `funding_rate` 非 NULL 行，新发主池信号 funding NULL 率下降。
- **复验收口（工单 §六·补，`复验_费率覆盖缺口_O1_c9478af_2026-09-22.md`，2026-09-22）**：复验以三段式确认方案 A 已落地、`c9478af` 部署后 prod 缺口收敛（其口径按**逐行**计、含重复行，故报 153→1；本轮 `5979d36` 已改**符号级**、口径下缺口 = **0**）。对 #4「`canonical_symbol` 重复放大采集负担」**只读复核后判定：O1 侧已中和、无需再改码**——
  - `core.asset` 实测 **21,828 行 / 18,854 distinct symbol / 2,024 个重复符号 / 2,974 行冗余**（复验报 1,911，仍在增长）；
  - **排名路径未受污染**：`get_pending_assets(limit=200, force=True)` 实测 **200 行 = 200 distinct symbol**（重复行无有效排名、进不了 top-N）⇒ 调度按 asset_id 写不会重复写；
  - **缺口路径已去重**：`get_signal_gap_assets` 的 `DISTINCT ON (upper(canonical_symbol))` 每符号只取最优一行 ⇒ 采集负担不随重复行放大；
  - 存量污染仅限 `asset_derivatives` **78 个符号有多行**（历史逐行版本产物），对**符号级**消费方（scan/funding_map）无影响，只影响按 `asset_id` 查的面板——属 W5 治理面。
  - ⇒ **不做破坏性去重**（AGENTS 铁律：破坏性数据操作需授权；且 W5 治理已有独立工作流 `fe459e1` 在处理），不扩 O1 scope。
  - ⚠️ **部署必须带上 `5979d36`**（非仅 `c9478af`）：`c9478af` 的缺口查询是**逐行**版，容器在跑它时会每 6h 继续为重复 asset_id 写行、持续放大污染；`5979d36` 才含符号级去重 + `_aggregate_funding` 无 OI 退化。

### 盘面异动告警邮件 09-22 14:0x 两封审计处置（ENAUSDT 含共振 / OPNUSDT+龙虾USDT 纯盘面，2026-09-22）

来源：用户提交的两封收件 eml（05:56 / 06:04 UTC）。方法：三段式（源码核验 → 库复算 → 端到端离线渲染复验），并**首次用 `detail.resonance_snapshot` 快照做字节级回放**（O6 产物的直接收益）。审计结论：**两封邮件与 HEAD 代码 + 库数据逐行一致**（仅「生成时刻」不同）⇒ 线上即 `ec1c1c2`，无「推了没部署」。处置 1 真 bug + 3 项口径/显示，**全部落在 `scan_daemon.py` 渲染层，零 DDL、零迁移、无阈值变更**。

- **P2-N7（⚠️ 真 bug，已修 `af63632`）失效位幅度符号硬编码为负**：`f"（-{sp:.2f}%"` —— 做多「跌破」在入场下方，负号正确；做空「升破」在入场**上方**，却渲染 `-16.35%` ⇒ **方向词与符号自相矛盾**，风控读者会误判失效位在下方。显形条件 = 已告警 + 做空 + 有失效位，只读库实证**全库仅 1 条**（`id=1292 龙虾USDT`，BRK 做空；BRK 做空全库亦仅此 1 条）⇒ 此前实物邮件从未可见。改法：符号随方向（`'-' if up else '+'`）。
- **P2-N8（⚠️ 数据噪声，已修）催化剂占位符标题被计为方向票**：上游把「标题缺失」落成**字面量** `'null'`（`biz.asset_catalyst` 实测 **177 条**；近 7 天影响 **10 个资产**，最多 `asset_id=2` 有 28 条）。它是空壳却带 `bullish` 参与计数（实物 ENAUSDT 快照「10多」里 1 条 `title='null'`），且一旦落进明细前 4 条会直接渲染「null（bullish/weak，2026-09-20）」。新增 `_is_placeholder_title()`（`null/none/nan/undefined/n/a/na/-/--` 或纯空白）在**去重前**丢弃。实测 ENAUSDT **12（10多/0空/2中）→ 11（9多/0空/2中）**。**不改判 neutral**——那会把噪声计进总数。
- **P2-N9（⚠️ 口径，已修）噪声级 OI 微增仍断言「杠杆驱动」**：`oi_dir` 由 `oi_chg > 0` **二值化**（L790）⇒ ENAUSDT OI 仅 **+0.04%**（渲染成 `OI up +0.0%`）仍进「杠杆驱动（OI 增而现货主动卖）」分支，机制结论无物性支撑。新增常量 `CVD_MECH_OI_MIN_PCT = 0.5`：`|oi_chg| < 0.5%` 时改述「现货主动卖且无现货承接，但 OI 未见同步扩张（不足 ±0.5%），机制待判」；阈值**对称**作用于「空头回补」分支。`oi_dir` 本身仍用于 S1~S4 场景分类，不受影响。
- **P2-N10（展示，已修）BRK 卡片渲染 `OI - -`**：`oi_dir`/`oi_chg_pct` 皆 NULL（BRK 生产者刻意不落 OI）时渲染 `OI - -`，与图例「n/a = 该维度无从查询」口径不一致、且像占位符残留。改为两者皆空时渲染 `OI n/a`（同费率段写法）。
- **观察（非缺陷，未改）**：① 上轮 N1 已落地（卡片显示「最新 2026-09-20，含 3 条 >3 天」）；② BRK 卡片无「历史同场景」行属**设计**（`_scenario_priors` 样本口径限主池）；③ 跨池互斥生效（`id=1285 SKRUSDT` 被置 `alert_suppressed_reason=跨池互斥…`，未进邮件）；④ 中文 symbol（`龙虾USDT`/`哈基米USDT`/`牛来USDT`/`我踏马来了USDT`）由 Binance `fapi/v1/exchangeInfo` 拉取且 K 线连续，是**真实永续代码、非脏数据**。
- **自测**：[test_scan_alert_audit_deepdive.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_scan_alert_audit_deepdive.py) 扩至 **49/49 通过**（新增 P2-N7 5 条 + P2-N8 12 条 + P2-N9 5 条 + P2-N10 3 条）；`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16 无回归；`py_compile` 通过。
- **离线端到端复验**：HEAD 代码 + 库内快照重渲染两封邮件，差异**仅**「生成时刻」+ 本轮 3 处修正（`-16.35%`→`+16.35%`、`OI - -`→`OI n/a`、CVD 机制文案）⇒ 修复精确、无副作用。（N8 不在回放中显形：快照是修复前冻结产物，已用**现行 `_get_resonance` 直查库**单独验证。）
- **待部署**：需重启容器（`scan_daemon`）后生效；当前容器跑 `ec1c1c2`，未含 `af63632` 与本提交。

### 盘面异动告警邮件 09-22 06:46 审计处置（FLOCKUSDT + AVAUSDT，2026-09-22）

来源：用户收件 eml（`🚨_盘面异动告警：2_币高置信信号（含共振_6_条，催化剂_3多_2空_1中，净多1）.eml`，`Date: Tue, 22 Sep 2026 06:46:59 +0000`），用户反馈**「看不懂」**。方法：三段式（源码核验 → 库复算 → 端到端离线渲染复验）。库侧逐字段核验全部命中：`id=1304 FLOCKUSDT`（main/S1/15m/up/+2.75%/vr=2.05/OI up +3.90%/CVD up/费率 0.00005/high）、`id=1303 AVAUSDT`（main/S1/1h/up/+4.39%/vr=2.53/**OI up +0.19%**/CVD up/费率 -0.0008045/high）。**本轮全部落在 `scan_daemon.py` 渲染/查询层，零 DDL、零迁移、无阈值变更**（`oi_dir` 二值化逻辑、场景分类、regime 门槛均未动）。

- **🔴 P2-N11（⚠️ 真 bug，已修）归一标题精确键判等漏合并两类转载** ⇒ 催化剂条数被转载**翻倍**，与图例「已合并多源转载」**相反**。实物：AVAUSDT 主题「催化剂 4 条」实为 **2 条新闻 × 2 来源**（实为 2多/1空/1中；主题原写「3多/2空/1中」，净多 1 不变）。两类漏合并：
  - **①源库截断版**：同一条新闻被源库落成「完整版」与「`...` 收尾的截断版」，归一后**短键是长键的前缀**（实物 46 vs 52 字）⇒ 精确键判等漏判。新增 `_is_repost_of_truncated()`：两键互为前缀 **且** 短的一侧原文以 `...`/`…`/`..` 收尾（截断可证）才合并，并设 `MIN_TRUNC_DEDUP_LEN=24` 防短标题互并。
  - **②正文内嵌源名**：`_norm_title()` 原只剥**标题首段**源前缀，够不着「火星财经消息，…，**据 HTX 行情数据**，…」这类**正文内**源名 ⇒ 差在 `HTX` 二字。现补：`_SRC_NAMES_LOW` 增补行情数据类源名（`htx`/`okx`/`火币` 等），并只删**紧跟在「据」之后**的源名（引用来源的固定句式）——**不全局删**，否则会吃掉「币安将上线…」这类正文源名、把无关新闻并掉。
  - ⚠️ **主动收窄过一次**：曾考虑「短原文是长原文纯前缀」也合并，推演发现英文新闻模板（`According to the announcement from Binance, the following tokens will be listed on …` 前 69 字全同）会把**不同公告**并掉 —— 正是 **P1-N1 踩过的坑**（误合并 208 组 / 吞掉 249 条）。⇒ 只认**可证的截断标记**，并把这段权衡写进常量注释与测试护栏。
  - **影响面量化（只读，近 7 天 2829 行）**：去重后 **2679 → 2397（多合并 282 条）**，受影响 **80/340 资产**；Top：BTC 672→599、ETH 407→352、ZEC 152→136、HYPE 114→98、USDC 77→67、UNI 53→45、SOL 81→74、TRUMP 16→12、DRAM 8→4、DLT 3→1、**AVA 4→2（目标用例）**。**抽检结论**：逐组打印 BTC/ETH/DRAM/DLT/AVA 的新增合并对，**全部为同一条新闻被 ChainCatcher / BlockBeats / 火星财经 / PANews 各记一条**的真转载（例：「ChainCatcher 消息，据 Lookonchain 监测，比特币上涨期间…」↔「火星财经消息，据 Lookonchain 监测…」）⇒ **无误合并**。
- **🔴 P2-N12（⚠️ 真 bug，已修）`_load_alert_candidates` 漏选 `confidence` 列 ⇒ 每张卡片渲染空的红/绿药丸**：渲染层用 `sig.get('confidence').upper()` 出徽章，但查询未选该列 ⇒ `None` ⇒ 实物 eml 里每张卡片都有一个 `<span style='background:#fee2e2…'></span>`**无文本**，读者无从判断那是什么。已补选该列；渲染层另做兜底（`confidence` 缺失时**不渲染空壳**），因该函数被多个调用方共用，避免同类空壳再次静默上线。
- **🟠 P2-N11c（显示，已修）OI 近乎持平时仍渲染 `OI up +0.2%`**：`oi_dir` 由 `oi_chg > 0` 二值化 ⇒ AVAUSDT OI 仅 **+0.19%** 也判 `up`，读者会以为 OI 明显扩张，**进而无法解释强度条为何只有 0.5 分**（强度 = 量比 × |OI 增速|，OI 持平则分数必然贴地）。常量 `CVD_MECH_OI_MIN_PCT` 更名 **`OI_FLAT_PCT = 0.5`**（同一阈值现同时驱动两处：① OI 段显示 ② CVD 机制断言），`|oi_chg| < OI_FLAT_PCT` 时渲染「**OI 持平 ±X%**」（正负皆然），`oi_dir` 本身仍用于场景分类与 regime，不受影响。
- **🟠 图例 4 项补全（展示，已修）**：① 强度条只写「相悖扣系数」未写**加成**（手算 8.0 对不上 9.7）⇒ 补「共振方向一致 ×1.15 / CVD 同向 ×1.05」并注明**「不含涨幅」**、OI 为乘性因子；②「市场环境」整行（`btc_1h`/`fgi`/`cap_trend`/「空头环境受限」）**无图例** ⇒ 新增一段，写明三指标与门槛、以及「环境受限 = 该方向当轮降级 high→medium，而告警只取 high ⇒ **该方向本轮不发信**（非否决该方向本身）」；③「历史同场景」两币数值完全相同（中位 +0.62% / 胜率 57% / n=58）⇒ 注明口径是**按场景跨币种聚合**（同为 S1 即共用同一组数字，与具体币无关），并提示样本含顺风期选择偏置；④ 补「OI 持平 ±X%」与费率正负语义（正 = 多头付空头 = 多头拥挤；负 = 空头付多头 = 对做多顺风）。
- **regime 复算（佐证「市场环境」行）**：FGI=72 **等于** `REGIME_FGI_GREED=72`（判据 `>` ⇒ 不触发）；`cap_trend = (2917342942198.4 − 2776581649862.35)/2776581649862.35 = +5.07%` > 0.5 ⇒ `short_fav=False` ⇒「空头环境受限（市值+5.07%）」；`btc_1h=+0.11%` 在 ±0.5% 内不触发。
- **强度复算**：① FLOCKUSDT `2.05 × 3.90 = 7.995` ×1.15（共振对齐）×1.05（CVD 同向）= **9.65 → 9.7（5 格）**；② AVAUSDT `2.53 × 0.19 = 0.4807` ×1.05（多空共振条数相等 ⇒ 不加共振系数）= **0.50（1 格）**。
- **离线端到端复验（HEAD 代码 + 真实库）**：主题 `6 条 / 3多2空1中` → **`4 条 / 2多1空1中`**（净多 1 不变）；FLOCKUSDT 强度 9.7 / 催化剂 2（1多/0空/1中）**未变、无误伤**；AVAUSDT `OI up +0.2%` → **`OI 持平 +0.2%`**、催化剂 4（2多/2空/0中）→ **2（1多/1空/0中）**、强度仍 0.5；图例 8 个新片段全部命中。
- **自测**：[test_scan_alert_audit_deepdive.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_scan_alert_audit_deepdive.py) 扩至 **75/75 通过**（新增 P2-N11a 内嵌源名 3 条 + P2-N11b 截断转载 6 条（含英文模板误合并回归护栏）+ P2-N11c OI 持平 4 条 + P2-N11d 图例 8 条 + P2-N12 5 条）；`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效；当前容器跑 `ec1c1c2`，未含 `af63632` / `827f6a4` 与本提交。

### 盘面异动告警邮件「纯盘面无共振」审计处置（审计_盘面异动告警邮件_纯盘面无共振_2026-09-22，2026-09-22）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_盘面异动告警邮件_纯盘面无共振_2026-09-22.md`（用户原话「审计一下最新的告警邮件，表示不知所云」）。审计结论：三信号字段与库 100% 吻合、共振=0 真实；「不知所云」主因是渲染层。本轮处置 **B1（P1）/ B3（P2）/ B4（P3）**，全部落在 `scan_daemon.py` 渲染层，**零 DDL、零迁移、无阈值变更**。

- **🔴 B1（P1，核心）头部「市场环境」泄漏 BRK 原始调试 token**：邮件 2 头部渲染成 `市场环境 brk_down | vol_x=29.9 | bar=2026-09-22T06`，而图例承诺「市场环境 = 全局 regime（btc_1h/fgi/cap_trend）」，**自相矛盾**。根因：头部取 `items[0]["signal"]["context_tags"]`（按强度排序后第一条），当第一条是蓄势池 BRK 时其 `context_tags` 是 `["brk_down","vol_x=…","bar=…"]`，且 L0 regime（`regime_tags`）**整段丢失**。
  - **修复**：`_render_alert_email(items, regime_tags=None)` 新增入参；`task_scan_alert` 批次级调用一次 `_build_regime(conn)["tags"]` 并传入 ⇒ 头部与逐信号 `context_tags` **解耦**。未传时只从 items 抽 **L0 形态**标签兜底（`btc_1h=`/`fgi=`/`cap_trend=`/含「环境受限」），**任何情况下都不再泄漏 `brk_*`/`vol_x=`/`bar=`**。
  - **BRK 状态人文化后并列展示**（不替换 regime）：头部加一行「本批含蓄势池突破（BRK）N 条」；BRK 卡片把原 `bar=` 标签解析为「触发根 09/22 14:00」（`bar=` 是 UTC 根，+8 转北京时间；去重键本身不变）。图例补两条对应说明。
- **🟠 B3（P2，已修）强度 0.6 却标「高置信」**：强度条基量 = 量比 × |OI 增速|（OI 为乘性因子）⇒ OI 持平时分数必然贴地，与标题「高置信」同框致读者困惑。现 `|oi_chg| < OI_FLAT_PCT` 时卡片在强度条后明示「（OI 持平，强度条偏低）」。
- **🟠 B4（P3，已修）CVD 缺值用图例未定义的「未知」**：`CVD {cvd or '未知'}` → `'n/a'`，与图例「n/a = 该维度无从查询」口径统一（BRK 无 CVD 即渲染 `CVD n/a`）。
- **未改（观察项）**：SOPHUSDT 费率 `-0.9616%/8h`（年化 -1053%）数学正确、DB 两行一致，属**数据 sanity 待查**（非渲染 bug），未动数据源。
- **部署观察**：审计指出 07:01~07:19 UTC 两封邮件由不同 `scan_daemon` 版本生成（重部署时间差），B1 在 `5979d36` 与 origin/main **两版均存在**；本轮修复需再次重启 `scan_daemon` 才生效。
- **自测**：新增 [test_scan_alert_header_regime.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_scan_alert_header_regime.py)（**18/18 通过**：B1 头部含全局 regime 且无 token 泄漏 + 兜底不泄漏 + BRK 条数/触发根 + B3 + B4 + `**` 护栏 + AST）；既有 `test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_squeeze_battle.py` 128/128 无回归；`py_compile` 通过。

### N-pure3-1：`btc_1h` 改用已收盘 1h 条（审计_盘面异动告警邮件_纯盘面无共振3_2026-09-22，2026-09-22）

来源：`审计_盘面异动告警邮件_纯盘面无共振3_2026-09-22.md`（对象 = 08:20 UTC 那封「1 币·无共振」邮件）。审计确认**本封已可读、数据 100% 对齐、B3/B4 在场**，只留一项 N-pure3-1。

- **N-pure3-1（已修）`_build_regime` 的 `btc_1h` 用了未收盘的当前小时**：`SELECT ... ORDER BY open_time DESC LIMIT 5` 取 `closes[0]`（最新一根）——而库里最新 1h 常是**未收盘的当前小时**（`scan_klines` 每 5min UPSERT 覆盖），其 `close_px` 是 live 价 ⇒ ① 同一时刻不同分钟读到不同值、**不可复现**；② 与图例「btc_1h = BTC 最近两根 1h **收盘**涨跌」措辞不符（实测 live +0.16% vs 已收盘棒 +0.09%，差 0.07pp）。**与已记档的 BRK「未收盘条不可复现」（P1-4）同类**，当时只修了 BRK、regime 未动。
  - 修法：复用 `_last_closed_idx(rows, "1h", now)`，取最近一根**已收盘**条与其前一根；无已收盘条则**不产出** `btc_1h` 标签（宁缺勿错，不拿 live 冒充收盘）。图例措辞补「已收盘」二字（用纯文本，**不得**用 markdown `**`）。
- **验证盲区（如实记录）**：该封仅 1 个干净 S1（牛来USDT）、**无 BRK 主导**，而 B1 只在「BRK 按强度居首」时触发 ⇒ **该封不能排他证明 B1 已部署**（旧代码对干净 S1 批次本来就输出人话头部）。终验需一封含 BRK 主导的批次邮件，或直连容器 `git rev-parse HEAD` 确认 ≥ `5dfbcd0`（我无容器访问权，未验）。
- **自测**：`test_scan_alert_header_regime.py` 扩至 **21/21**（+3：已收盘两根 (100−80)/80=+25.00% 而非 live +5.00%；全未收盘 → 不产 `btc_1h` 标签）；既有 deepdive 75/75、remaining 16/16、L1 16/16、squeeze 135/135 无回归；`py_compile` 通过；零 DDL。

### G4 流动性字面 0 值漏判（工单 `待修复工单_G4_流动性0值漏判_2026-09-22.md`，2026-09-22）

来源：复验 `1fc7a69`（催化剂邮件修复）副产物。定级 P2、非阻塞。

- **缺陷**：`workbench/catalyst/fundamental.py::FundamentalChecker._score_liquidity` 的守卫只写 `if liq_usd is None: return 0`，**漏了字面 `0`**（上游未抓取成功但写入 0 而非 NULL）⇒ 落兜底 `return 20`（与「有数据但极差」混淆），且与 `_score_tvl`（`is None or tvl_usd <= 0`）口径不一致。安全性无影响（COPPER `liq=0` 综合分 39 仍 < pass_threshold 50，G4 依旧 fail），但属 P1 修复的遗漏分支。
- **修复**：一行收敛为 `if liq_usd is None or liq_usd <= 0: return 0`（+ docstring 注明 0 与 None 同判）。**不引入新分支、不动权重/阈值**，与 `_score_tvl` 完全同构。
- **Scope（未动，遵工单）**：`_score_unlock`（字符串型，`0` 不适用，保持 `if not pressure: return 50`）；`composite_score/tier` 内部不一致（P3 展示层，另立）；`$COPPER` 供应量 1e17 数据源与商品同名误连（classify/asset_filter，另立）；无 DDL。
- **自测**：新增 [test_fundamental_liquidity.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_fundamental_liquidity.py)（**13/13 通过**：`liq=0/0.0/-1/None` → 0；真实阶梯 50K→20 / 200K→45 / 2M→70 / 6M→90 零误伤；与 `_score_tvl` 同构；源码守卫含 `is None or <= 0`；`_score_unlock` 未改）；`py_compile` 通过。

### 含共振三封审计 OPT-1~6 处置（审计_盘面异动告警邮件_含共振3封_2026-09-23，2026-09-23）

来源：`审计_盘面异动告警邮件_含共振3封_2026-09-23.md`。审计**先确认数据 100% 对齐、无新 bug**，并给出**部署闭环的物证**（见下「部署已闭环」），另开 6 条优化建议。本轮落地 **OPT-1/2/3/5/6**，**OPT-4 未做**（需新增 append-only 费率历史表，属另立项）。全部落在 `scan_daemon.py` 渲染层，**零 DDL、无阈值变更**。

- **✅ 部署已闭环（审计意外收获，双重独立锚点）**：① 图例含「btc_1h = BTC 最近两根**已收盘** 1h 的收盘涨跌」（「已收盘」三字唯 `1587238` 新增）；② 行为锚——`btc_1h` 精确等于**已收盘**口径（23:17 那封 −0.07% = 22:00 vs 21:00 = −0.0728%；18:17 那封 +0.17% = 17:00 vs 16:00 = +0.173%）；③ B1 锚——含 3 条 BRK 的邮件头部「本批含蓄势池突破（BRK）3 条」+ 卡片「触发根 09/22 17:00」（该行唯 `5dfbcd0` 新增）。⇒ **`5dfbcd0`（B1）+ `1587238`（N-pure3-1）均已部署生效**，昨日遗留的「B1 终验欠一封 BRK 主导邮件」**已满足**。
- **🔴 OPT-1（P1，已修）标题「含共振 N 条」缺覆盖币数**：`n_res` 为全批累加 ⇒ 「5 币…含共振 11 条」实为 **5 币里只有 1 币有**，易误读为「整批都有共振」。已补覆盖维度 `（k/N 币）`（实测形态：`含共振 11 条（1/5 币），催化剂…`）。
- **🔴 OPT-2（P1，已修）陈旧催化剂推高「净多」**：邮件1 ARB `催化剂17（11多/1空/5中，净多10，含 10 条 >3 天）`——**59% 陈旧**仍按全量算净多。`_get_resonance` 新增 `catalyst_dir_fresh`（仅 `published_at ≥ fresh_cut`），渲染在「含 N 条 >X 天」后并列「**剔除陈旧后净X**」（陈旧旧闻不再推高 conviction）。
- **🟠 OPT-3（P2，已修）BRK 静默缺「历史同场景」**：`_scenario_priors` 样本池只取主池 ⇒ BRK `prior` 恒空、原 `if prior:` **静默省略**该行，读者分不清「无样本」还是「漏渲染」。现 BRK 无先验时显式标注「历史同场景：BRK 暂无历史先验（先验样本池仅含主池信号）」。
- **🟠 OPT-5（P2，已修）BRK 的 OI/CVD 恒 `n/a` 观感像缺失**：图例补「BRK 判定只用价+量，不落 OI/CVD ⇒ BRK 卡片 OI/CVD 恒为 n/a（设计，非缺失）」。
- **🟡 OPT-6（P3，已修）强度条不含催化剂强度**：邮件1 ARB「催化剂 17 条（净多10）却强度仅 1.5」易困惑 ⇒ 图例显式声明「也不含催化剂强度（催化剂仅以方向 ×1.15/×0.75 修正，与体量无关）」。
- **🟠 OPT-4（P2，未做）费率不可回溯**：`biz.asset_derivatives` 按 symbol upsert 覆盖 `fetched_at` ⇒ 邮件时点的费率无历史。需 append-only 费率历史表或邮件内落费率快照，**属新增表/落库 → 另立项**（O6 已把催化剂/事件/KOL 三段快照落 `detail`，费率可比照样补，但涉及 schema）。
- **未改（验证限制，如实记录）**：N-1 催化剂精确条数**不可用简单 SQL 复现**（渲染含「标题归一去重 + 可交易过滤」；ARB 简单统计 14 vs 邮件 17、ZEC 111 vs 18——**口径差异非数据错误**，方向构成趋势一致）；N-2 费率不可回溯（= OPT-4）；N-3 无法直连容器（以行为锚点推断 ≥`1587238`）。
- **自测**：`test_scan_alert_header_regime.py` 扩至 **30/30**（+9：OPT-1 覆盖币数 2 例 + OPT-2 剔除陈旧 2 例 + OPT-3 2 例 + OPT-5/6 图例 3 例）；既有 `test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_squeeze_battle.py` 135/135 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效。

### OPT 复验盲区 N-786-1/2/3（复验_告警邮件OPT-1~6_786bcdb_2026-09-23，2026-09-23）

来源：`复验_告警邮件OPT-1~6_786bcdb_2026-09-23.md`。复验用**真函数 + 真 prod DB** 超 stub 复现，确认 OPT-1/2/3/5/6 代码正确、测试 30/30 真实，但指出 **3 项「修复不完整」**——OPT-2 只修了**卡片**、没修**标题**，而标题恰是原 P1 问题的**第一落点**（列表页唯一可见处）。本轮补刀，全在 `scan_daemon.py` 渲染层，**零 DDL、无阈值变更**。

- **真实数据（复验只读复现，`_get_resonance` 真函数）**：ARB `全量 11多/1空/5中=17 / 新鲜 5多/1空/0中=6`（stale 11）；ZEC `18 / 18`（stale 0）；**USELESS `5多/4空/2中=11 / 0多/2空/0中=2`（stale 9）** ⇒ 全量净多 **+1 → 新鲜净多 −2**，**方向反转**（陈旧旧闻把实际偏空的币显示为略偏多）。⇒ OPT-2 修复确有必要，且**必须覆盖标题**。
- **🔴 N-786-1（P1，已修）标题「净多」仍用全量口径**：`_alert_title` 方向段原取 `catalyst_dir`（含陈旧）；`catalyst_dir_fresh` 只在卡片被读。实测 ARB+USELESS 组合标题报「净多11」而新鲜仅 2（虚高 5.5×）。⇒ 标题方向段改用 `catalyst_dir_fresh`，并显式标「**已剔除陈旧**」。
- **🟠 N-786-2（P2，已修）覆盖币数把「只有陈旧催化剂」的币计为有共振**：`coin_n` 原用全量 ⇒ 与 OPT-2「陈旧不算数」口径相反。⇒ `coin_n` 改用 `f_coin`（新鲜三项合计）。**密封边界**：整批共振全 0 且存在「有催化剂但全 >3 天」的币时，标题**改印**「无新鲜共振；催化剂全部 >3 天，不计方向」，**不得**印「纯盘面信号，无共振」（事实错误——有催化剂，只是陈旧）。
- **🟡 N-786-3（P3，已修）全陈旧时卡片印「剔除陈旧后多空持平」**：`fresh` 三键全 0 仍 truthy ⇒ 输出「多空持平」（读作「新鲜的多空均衡」，与「一条新的都没有」**含义相反**）。⇒ `fb==0 and fbear==0 and fneut==0` 时改「剔除陈旧后**无新鲜条目**」。
- **本轮自引入并已修**：图例文案里写了 markdown 强调符 `**标题**`（HTML 邮件禁忌，护栏抓出）——**教训固化**：改渲染文案后必跑 `**` 护栏。
- **自测**：`test_scan_alert_header_regime.py` 扩至 **37/37**（+7：N-786-1 标题新鲜口径 2 例 + N-786-2 全陈旧边界/混合批次 2 例 + N-786-3 2 例 + 护栏）；`test_scan_alert_audit_deepdive.py` 的 `_res()` 补 `catalyst_dir_fresh`（默认与全量同值，`stale=0` 常规 fixture 语义不变）⇒ **75/75**；既有 `test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_squeeze_battle.py` 137/137、`test_fundamental_liquidity.py` 13/13、`test_derivatives_signal_gap.py` 35/35 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效（一次重启即可同时闭环 B1 + N-pure3-1 + OPT-1/2/3/5/6 + N-786-1/2/3）。

### N-786-4/5 补刀（复验_告警邮件N-786-1~3_0111ed2_2026-09-23，2026-09-23）

来源：`复验_告警邮件N-786-1~3_0111ed2_2026-09-23.md`。复验以**真函数 + 真 prod DB** 确认 N-786-1/2/3 已修（ARB 标题净多 4 非全量 10；**USELESS 方向反转 净空2 非净多+1**），另指出**我这两轮自引入的 2 项新缺陷**（N-786-4/5）。本轮补刀，全在 `scan_daemon.py` 渲染层，**零 DDL、无阈值变更**。

- **🔴 N-786-4（P2，已修）标题「全部 >3 天」对「无催化剂」是事实错误**：`else` 分支**无条件**打「催化剂新鲜条目 0（全部 >3 天，不计方向）」，而 `n_res>0` 可由 **event（解锁/链上转账预告）或 KOL** 贡献（`coin_n = len(event) + f_coin + len(kol)`；`_get_resonance` 的 event 段在 `if not asset_id: return` **之前**执行 ⇒ event 与 catalyst 可得性**相互独立**）。实测命中 **DOTUSDT/WLDUSDT/BILLUSDT/CAPUSDT**（event=1 + 7 天催化剂=0）——标题断言「有催化剂但全陈旧」，卡片却显示「催化剂0」，**同封自相矛盾**（BILL/CAP 连 30 天都是 0）。
  - 修法：`else` 分支按 `has_zero_cat`（存在 `_catalyst_total(cd)==0` 的币）细分——真为 0 条 → 「催化剂新鲜条目 0（**无催化剂条目**）」；否则才「全部 >3 天」。
  - **可达性（诚实定级）**：4 币当前均**不进告警**（DOT/WLD/BILL 是 `ACC`（`_load_alert_candidates` 只收 main 或 BRK）、CAP 是 `expired`）⇒ **潜在缺陷、当前不可达**；但 DOT/WLD 正在蓄势，**升格 BRK（量比≥3+突破价）即触发**，且是主流币 ⇒ 定 P2。
- **🟠 N-786-5（P2/P3，已修）标题「含共振 N 条」静默改新鲜口径**：`n_res` 用了 `f_coin`（新鲜）⇒ 标题「含共振 6 条」与卡片「催化剂 17」两个数字打架，且图例未声明。⇒ 拆出 `n_res_all`（全量）用于「含共振 N 条」（与卡片同口径），方向段/覆盖币数/密封边界仍用新鲜 `n_res`；图例显式声明「含共振 N 条 = 全量口径」。
- **⚠️ 本轮再次自引入并已修**：图例写「**全量**」（markdown 强调符）——`**` 护栏第二次抓出。**教训固化已升级为「改任何渲染文案后必跑 `**` 护栏」**（两轮三犯）。
- **自测**：`test_scan_alert_header_regime.py` 扩至 **44/44**（+7：N-786-5 全量条数/图例 4 例 + N-786-4 无催化剂/全陈旧对照 3 例）；既有 `test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_squeeze_battle.py` 137/137、`test_fundamental_liquidity.py` 13/13、`test_derivatives_signal_gap.py` 35/35 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效（同批闭环）。

### N-416-1/2/3 补刀（复验_告警邮件N-786-4~5_416b879_2026-09-23，2026-09-23）

来源：`复验_告警邮件N-786-4~5_416b879_2026-09-23.md`。复验以三段式 + **真函数 + 真 prod DB + 三版本对比**确认 N-786-4/5 代码正确、7 套件零回归，另指出 **3 项**（其中 N-416-1/N-416-3 是我上一轮**自引入**）。本轮全部处置，全在 `scan_daemon.py`（+ 一处测试护栏），**零 DDL、无阈值变更**。

- **🟠 N-416-1（P3 潜在，已修，本轮自引入）混合批次把错误换了一侧**：`elif has_zero_cat` 无条件优先于 `has_stale`（而 `has_stale` 唯一读取点在密封边界内 ⇒ 非密封路径成**死变量**）⇒ 同一批同时含「零催化剂币」与「全陈旧币」时，对全陈旧币**误述「无催化剂条目」**——正是 N-786-4 的**镜像失真**（一个布尔优先级无法表达三种事实）。修法：非新鲜方向分支**三态化**——`has_stale and has_zero_cat` → 「催化剂新鲜条目 0（含仅陈旧条目，另有币无催化剂）」，单独 `has_stale` → 「全部 >3 天」，单独 `has_zero_cat` → 「无催化剂条目」。**可达性**：真 DB 当前**不可达**（需同窗同时满足 zero-cat 带 event/KOL + 全陈旧币 + 全批无新鲜；event 路径仅 UNI/MARSCOIN 命中且非 zero-cat、KOL 近 7 天候选命中 0）⇒ P3 潜在。
- **🟠 N-416-2（P3，已修）`**` 护栏不覆盖标题**：`_alert_title` 输出是 **Subject**、**不在 html 内**（`_render_alert_email` 不调用它），而护栏只断言 `"**" not in html` ⇒ 若把 `**` 写进标题（正是 N-786-x 反复改动的函数）不会报错。修法：测试补 `check("**" not in sd._alert_title(items), ...)` 一行。
- **🟡 N-416-3（P3，已修，本轮自引入）标题内两窗混搭**：N-786-5 把「含共振 N 条」改回全量窗，但覆盖币数仍是新鲜窗 ⇒ 渲染「含共振 6 条（1/2 币）」（6 条里有 5 条属于「不计入」的币）。修法：覆盖币数改用 `all_coins`（全量窗，与 `n_res_all` 同源）⇒ `（2/2 币）`；新鲜窗只出现在方向段 `（已剔除陈旧）`；图例同步。
- **自查纠错（复验记录，非我本轮）**：复验首版用自建 `canonical_symbol = bare` SQL 近似可达性，误判 UNIUSDT 为 zero-cat（`canonical_symbol` 非唯一）⇒ 改用被测函数自身的 `_get_asset_id()` 后为「有新鲜」。**再次印证铁律：复现渲染路径必须用被测函数自身的查找逻辑**。
- **未改（观察项，非本提交）**：① `event_watchlist` 段**无时间窗**（`WHERE symbol = ANY(...)`，无 `published_at`/`event_date` 过滤）；实测 89 行、无陈旧/未来行 ⇒ 现状无污染，仅记录设计点；② KOL 段近 7 天仅 3 行/2 资产 ⇒ 共振第三段贡献极低（与 `catalyst_fast_daemon` 零产出同类）。
- **自测**：`test_scan_alert_header_regime.py` 扩至 **49/49**（+5：N-416-1 混合三态化 2 例 + N-416-3 同窗覆盖 2 例 + N-416-2 标题 `**` 护栏）；既有 75/16/16/143/13/35 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效。

### N-923-1 补刀 + 部署水位确认（审计_告警邮件CAKE单币_部署确认_2026-09-23，2026-09-23）

来源：`审计_告警邮件CAKE单币_部署确认_2026-09-23.md`。**重大里程碑**：该审计以**图例锚点**（「为全量口径」句唯 `416b879` 独有 + blob 一致性）确认 **`416b879` 已部署生效 —— 首次实现「推送→部署」闭环**（历史高频坑「代码推了没部署」本轮首次闭环）。

- **🟠 N-923-1（P3，高频，线上正在发生，已修）`（已剔除陈旧）` 无条件渲染**：`_alert_title` 第一分支无条件附加该后缀，而**无陈旧可剔时**（`catalyst_stale=0`）它仍是虚假暗示（读者以为「确实剔除了陈旧」）。**可达性实测 46%**：近 24h 108 个 high 信号币中 **19/41（46%）有共振的币是全新鲜**（BCH/CAKE/UNI/ZEC/SEI…）⇒ 近半数共振邮件印此虚句。
  - 修法（**建议 C，一石二鸟**）：方向段限定词由**后缀** `（已剔除陈旧）` 改为**前缀** `催化剂新鲜 X多/Y空/Z中` —— ① 对全新鲜场景恒准确（消除虚假暗示）；② 当场标明标题是**新鲜口径**，缓解与卡片全量括注的「打架」观感（见 N-416-3）；③ 与图例「方向段用新鲜口径」措辞天然一致。
  - **连带**：单币批次的 `（1/1 币）` 与「含共振 1 条」重复 ⇒ `cover_txt` **仅 `len(items)>1` 时渲染**。
- **🔴 N-416-3（上轮提，本轮**升为可达**，已缓解）标题方向段（新鲜）vs 卡片括注（全量）并置打架**：真数据实证 ARB `5多` vs `11多`、**USELESS `净空2` vs `净多1`（方向相反）**。**可达 14/108 币**（ARB/PONS/USELESS 均活跃币）。判定：卡片侧信息完备（同封给全量与剔除后），标题侧只给新鲜 ⇒ 属**可用性缺陷**。建议 C 的「新鲜」前缀让读者立即看出标题口径，**缓解**（未做双数字并列——标题应精简）。
- **N-416-1（上轮提，仍不可达）**：zero-cat 币 = 0 ⇒ 维持 P3 挂账（本轮未改；git 上已含上轮三态化，但该审计基线早于本轮提交，见「基线说明」）。
- **N-416-2（上轮提）**：`**` 护栏不覆盖标题 —— 已于上一轮补 `check("**" not in sd._alert_title(...))`。
- **未改（观察项）**：① `BRK × cooldown`——CAKE `id=1577`（BRK）因同币 12h 内已告警被 `COOLDOWN_H=12` 静默跳过，`signal_ts` 仅 20min 窗 ⇒ **永不告警**；属设计取舍（防同币轰炸），「丢信号检测」已正确排除不误报，**是否接受「BRK 质量升格被冷却吞」需产品判断**（未动）；② `历史同场景` 数字随时间滚动（n 不变、样本集合变），图例未声明「随时间滚动」；③ 强度条单币批次也满格（已有「OI 持平」提示缓解）。
- **⚠️ 基线说明**：该审计的「上轮 3 项未处置」是**它的基线快照**——`N-416-1/2/3` 实际已由 `90ec437` 处置（其对象是 `416b879`，早于 `90ec437`）。本轮在此基础上补 N-923-1 + 单币覆盖省略。
- **自测**：`test_scan_alert_header_regime.py` 扩至 **51/51**（+2：N-923-1 全新鲜「催化剂新鲜」前缀/无陈旧不印；单币省略 `（1/1 币）`）；`test_scan_alert_audit_deepdive.py` 的 O2 断言同步为「催化剂新鲜 4多/0空/2中」⇒ 75/75；既有 16/16/143/13/35/48 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效。

### N-90EC-1/2/4/5 补刀（复验_告警邮件N-416系列_90ec437_2026-09-23，2026-09-23）

来源：`复验_告警邮件N-416系列_90ec437_2026-09-23.md`。复验确认 N-416-1/3 真到位（夹具 A1/A5/B1 + 真函数）、B4 独立核验 122 行精确对上，另指出 6 项（含 2 项线上可达）。**该审计基线是 `90ec437`（早于我的 `62b07ce` N-923-1）**，故其中部分已被后续提交覆盖。本轮处置 **N-90EC-1/2/4/5**，**N-90EC-3 已由 `948d9db` 解决**，**N-90EC-6 为记档更正**。

- **🔴 N-90EC-1（P2，线上高频，已修）密封边界未三态化 —— N-416-1 的第三侧**：非密封路径已三态化，但**更早的返回路径** `if not n_res:` 仍用单一 `has_stale` 布尔，判不出「本批还同时含零催化剂币」（`has_zero_cat` 在此分支已计算但从未读取）。**真实可达**：按 `alerted_at`（**发信单位**，非 `signal_ts` 20min 窗）核近 7 天 123 批，**5~6 批**走到密封边界且混合（最强 **`09-23 00:32:30` 的 4 币批里 3 币零催化剂**，标题却断言「催化剂**全部** >3 天」）⇒ 与非密封侧同型。修法：密封分支**三态化**（`has_stale and has_zero_cat` → 「含仅陈旧条目，另有币无催化剂」），与非密封侧同族措辞。
  - ⚠️ **物证口径更正（`复验_告警邮件N-90EC系列_2b4d18c`，2026-09-23）**：本节原记「09-22 13:00 的 8 币批，其中 7 币根本没有催化剂」**不是一封邮件**——按 `alerted_at` 核 `09-22 12:50–13:30` 实为 **3 封**（`13:02:56` 2 币 / `13:17:56` 6 币 / `13:22:18` 2 币）；那 8 币来自 `signal_ts` 的 20min 窗聚合。作为「旧逻辑缺陷推演」成立，作为「线上物证」不成立 ⇒ **一律改用 `alerted_at` 口径**。
- **🔴 N-90EC-2（P3，已修）`**` 护栏只覆盖 1/5 分支**：原护栏的夹具 `n_res>0` ⇒ 只走「新鲜方向段」一条返回路径（注入测试：R1/R2/R3a/R3b 4 分支全漏）。修法：测试改**表驱动**遍历返回路径。**⚠️ 该修法首版自述「6 条返回路径」而实测为 7 条**（漏「非密封-全陈旧」`elif has_stale:`，注入 rc=0），且**未随本轮新增分支同步** ⇒ 已在下一节补到 **9 条全覆盖**（判据：9 个夹具产出 **9 条互不相同**的文案，否则即存在未覆盖路径）。
- **🟡 N-90EC-4（P3，已修）图例未声明 3 个新文案（三落点又漏一）**：标题新增的「无催化剂条目」「含仅陈旧条目，另有币无催化剂」「（已剔除陈旧）」均不在图例 ⇒ 读者无法理解。本轮图例补齐（且 N-923-1 已把「已剔除陈旧」后缀改为「催化剂新鲜」前缀，不再存在）。**⚠️ 本轮只补了非密封侧**：密封侧 3 文案（含最高频 S1「纯盘面信号，无共振」41/101 批）仍无声明 ⇒ 已在下一节补齐。
- **🟡 N-90EC-5（P4，已修）`res_coins` 沦为死变量**：N-416-3 改用 `all_coins` 后 `res_coins` 仅剩定义 + `+=1`、零读取 ⇒ 已删除。
- **✅ N-90EC-3（P2，已由 `948d9db` 解决）只改 daemon 未同步日报脚本**：`send_scan_signal_brief.py` 的 `PROD_SCENARIO_DESC` + `_scenario_label` 已在 `948d9db` 落地（本机核验其表含「S2 空头扎实」）⇒ **部署水位取 ≥`948d9db`** 即两处口径一致。
- **📌 N-90EC-6（P3，记档更正）**：`AGENTS.md` 在 `90ec437` 内写「`test_squeeze_battle.py` 扩至 143/143」，但该提交未改该测试（143 属 `948d9db`）——**归因更正**：`143` 的正确归属是 `948d9db`（+6 影子守卫）。
- **📌 内容级夹带（结构性问题，非操作失误）**：复验指出 `90ec437` 的 `scan_daemon.py` 内含 ~150 行未自述改动（B4 场景口径 / `_scenario_priors` 象限分桶 / `SQUEEZE_ALERT_SHADOW`）——因并发进程与我改同一文件、`restore --staged` 只能按**文件**粒度回滚。**教训固化：同文件并发改动先 `git stash push -- <path>` 或分文件提交，并在 commit message 列全实际改动面**（B4 是 P0 却未提，考古易误判）。B4 本身经复验独立核验**正确**（122 = 17+34+71 精确对上）。
- **自查纠错（复验记录，非我本轮）**：复验首版护栏注入探针 cwd 拼接错误被误读为「5/5 全抓到」，修正后真实为 **1 抓到 / 4 漏过**；复验首版可达性探针在 `biz.scan_signal` 上 `SELECT asset_id`（该表无此列）崩溃。**教训：工具 bug ≠ 结论，先自查探针**。
- **未改（观察项）**：`SQUEEZE_ALERT_SHADOW` 行为级影响评估（停发轧空邮件的下游依赖，另立项）；`event_watchlist` 段无时间窗（实测无污染）。
- **自测**：`test_scan_alert_header_regime.py` 扩至 **59/59**（+8：N-90EC-1 密封三态 3 例 + N-90EC-2 表驱动 6 分支护栏）；`test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_squeeze_battle.py` 143/143、`test_fundamental_liquidity.py` 13/13、`test_derivatives_signal_gap.py` 35/35、`test_scan_scenario_label.py` 48/48 无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效；**部署水位取 ≥`948d9db`**（含日报同口径）。

### N-2B4D-1/2/3 + N-62B-1/2 收口（复验_告警邮件N-90EC系列_2b4d18c，2026-09-23）

来源：`复验_告警邮件N-90EC系列_2b4d18c_2026-09-23.md`（对 `2b4d18c` 的三段式复验）。复验确认本轮 **N-90EC-1/5 真到位**（真 DB 7 天 6 批密封混合全部改新文案、同封逐币自洽；`res_coins` 全仓 0 残留）、**N-90EC-2 覆盖率 6/7**、**N-90EC-4 仅补非密封侧**、8 套测试与自述一致、**文件/内容粒度零夹带**（`_alert_title` 段 diff 仅 9 行）。本轮**同时收口上轮遗留的 N-62B-1/2**。

- **🟠 N-2B4D-1（P4，本轮自引入，已修）密封侧与图例首词不一致**：`2b4d18c` 新增的密封文案写「**催化剂**仅陈旧条目，…」、图例写「**含**仅陈旧条目，…」⇒ 只与非密封侧（N4）逐字对齐，密封侧偏移（本项目第 4 次同型漏）。修法：**统一措辞核**为「含仅陈旧条目，另有币无催化剂」，代码两支 + 图例**三落点逐字一致**。
- **🟠 N-2B4D-2（P4，已修）护栏 6/7 —— 自述「6 条返回路径」实为 7 条**：漏「非密封-全陈旧」（`elif has_stale:`，注入 `**` 后 `rc=0`）。修法：夹具表补该 case，并**随本轮新增分支一起**扩到 **9 条**（备注：当时所述「9 夹具须产出 9 条**互不相同**文案」只是**一次性探针**、未落到测试文件；实测该判据覆盖 9/33 条可达文案 = 27%，证明力不足 —— 复验 **N-0F7-2** 已改为**可达路径穷举**，见下节）。
- **🟠 N-2B4D-3（P4，已修）图例只补了非密封侧**：真实渲染图例段（1703 字符）里「纯盘面信号，无共振」（**S1，41/101 批 = 最高频**）、「无新鲜共振」、「催化剂仅陈旧条目」三处均 MISS。修法：图例补声明密封侧两种前缀 + 并列形态语义，并同时修 **N-62B-2**（见下）。
- **🟠 N-62B-1（P3，已修）`_alert_title` 全程不读 `asset_linked`**：`ctot == 0` 时未关联币也置 `has_zero_cat=True` ⇒ ① 弱形式（**线上 4/123 批可达**，如 `DATAIPUSDT`/`LUNA2USDT`/`RAYSOLUSDT`/`BROCCOLI714USDT`）标题断言「纯盘面信号，无共振」而卡片写「催化剂n/a · KOL n/a」；② 未关联 ctot=0 还会在密封侧误述「另有币无催化剂」。修法：**`has_zero_cat` 收窄为「已关联且真为 0 条」**，新增 `has_unlinked` 第 4 分支，两条返回路径各补一支（非密封支为「催化剂 n/a（未关联资产，非 0）」，兼防**落空档**）；**S1 原文案保持不动**（已关联真 0 条时「无共振」仍是事实），把改动面压到最小。
  - **`_others_clause(kinds, *exclude)`**：并列子句生成器，排除主句已陈述的形态 —— 否则单币批次会渲染出「（无催化剂条目，另有币无催化剂）」这类**同义重复**（自查发现并修）。
- **🟠 N-62B-2（P3，已修）图例写死「（k/M 币）」**：单币批次自 `62b07ce` 起已省略该括注（**62/101 = 61%** 邮件与图例不符）⇒ 图例改为「单币批次省略「（k/M 币）」——「1/1 币」与条数重复」。
- **📌 物证口径统一为 `alerted_at`**：见上一节 ⚠️ 更正（「09-22 13:00 的 8 币批」实为 3 封）。本轮所有可达性数字均按 `alerted_at` 30s 聚簇测得。
- **真 DB 影响面（只读，近 7 天发信口径 `confidence='high' AND (pool='main' OR (pool='accumulation' AND scenario='BRK'))`、**不过滤 `status`** ⇒ **206 行 → 114 批**；旧版 `2b4d18c` 树 vs 本轮真函数逐批 diff）**：
  - ⚠️ **口径更正（复验 N-0F7-5/6/7）**：① 条数/批次分母**必须**用 main+BRK 的 **114 批**，早前所述「222 行 → 129 批」把 **squeeze 15 批**算了进来 —— 而 `_alert_title` **根本不被 squeeze 使用**（走 `_render_squeeze_alert`）⇒ 分母被放大；「121 批不变」的正确数字是 **106 批不变**。② 加 `status IN ('active','confirmed')` 过滤是**错的**（206→106 行、114→56 批，漏 50%）：已告警后 status 漂移为 `expired` 的行**当时确实在邮件里**。
  - 标题变化 **8/114 批**，**全部**落在两类、其余 **106 批逐字不变** —— ① 5 批 = 措辞统一（`催化剂仅陈旧条目` → `含仅陈旧条目`）；② 3 批 = 未关联披露（`纯盘面信号，无共振` → `纯盘面信号；未关联资产，催化剂 n/a…`）。
  - 密封混合批次 5 批**全部**用统一措辞；含未关联币的批次 **4 批**，其中**标题断言「无共振」的由 3 批 → 0 批**（余 1 批 `09-22 11:47:30` 走**新鲜方向段**，两版都不含「无共振」，故不在该集合内）；单币 **68/116 = 59%**；标题含「（1/1 币）」= **0 批**。
- **自测（运行环境：工作区，含未提交改动）**：`test_scan_alert_header_regime.py` 扩至 **72/72**（+13：N-2B4D-1 三落点 2 + N-62B-1 三态 3 + N-2B4D-3/N-62B-2 图例 5 + 夹具表 +3 路径）；`test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_squeeze_battle.py` 143/143、`test_fundamental_liquidity.py` 13/13、`test_derivatives_signal_gap.py` 35/35、`test_scan_scenario_label.py` 48/48 无回归；`py_compile` 通过。
  - ⚠️ **口径更正（复验 N-0F7-7）**：上述数字在**工作区**（已加载 `scripts/.env`）复现；在**真码子树**（`git archive 0f7edc9`，无 `.env`）跑则 `test_derivatives_signal_gap` = **33 通过 + 1 跳过**、`test_scan_scenario_label` = **46 通过 + 1 跳过**（均因缺 `CMC_API_KEY` 而跳过），且**无 48 通过**的套件。⇒ 此后测试数字**一律标注运行环境（commit sha + 是否含工作区改动）**。
- **待部署（复验 N-0F7-4 更正）**：需重启容器（`scan_daemon`）后生效；**水位 ≥ 本节提交**。部署**范围**须按实际披露 —— 截止复验时 `2b4d18c..HEAD` 共 **15 个提交**，含 `scheduler.py` 的 **5 条新调度**（`highlight_alert` 每小时、`scan_outcome_settle` 每小时、`scan_edge_report` 08:10、`scan_edge_email` 08:20、`etl_sector_narrative` 每日 02:35），redeploy 是**整容器级**会一并激活；另 `origin/main` 已越过 `0f7edc9`，**应直接部 HEAD**（否则回退爆仓 24h 分列写入）。三个迁移 `fix_066/067/068` **均已在 prod 执行**（`biz.scan_signal_outcome`/`scan_edge_daily`/`scan_edge_bucket`/`highlight_alert_log`/`liquidation_snapshot.long|short_liq_usd_24h` 已存在）。⚠️ 项目**无迁移台账表**，迁移完整性只能靠 schema 反推，建议补 `schema_migrations`。

### N-0F7-1/2/3 + 口径更正（复验_告警邮件N-0F7系列_0f7edc9，2026-09-23）

来源：`复验_告警邮件N-0F7系列_0f7edc9_2026-09-23.md`（对 `0f7edc9` 的三段式复验，含**完备路径穷举**）。复验结论：**上轮代码改动本身正确、零回归**（8 批变化精确吻合、`_others_clause` 逐支推演无遗漏无重复、三迁移已在 prod、`0f7edc9→HEAD` 对 `scan_daemon.py` 的改动仅 `task_scan_liquidation` 且标题函数体未回退），缺陷集中在**一条返回路径未覆盖** + **验收判据证明力** + **图例落点** + **三处口径披露**。

- **🔴 N-0F7-1（P3，已修）「有新鲜方向」支是唯一不读 `has_zero_cat`/`has_unlinked` 的返回路径**：同封邮件里卡片对未关联币渲 `催化剂n/a`、对已关联真 0 条币渲 `催化剂0`，而标题把三者与「1 中」合并成**单向度计数串** ⇒ **n/a 与真 0 在标题层混同**（正是 N-62B-1 要消灭的语义），且读者会把「0多/0空/1中」读成覆盖全批。**真 DB 铁证**：`09-22 11:47:30` 的 4 币批（1 新鲜 + 1 未关联 + 2 真 0）。修法：该支末尾补 `_others_clause(kinds)`（与其余五支同族、零重复代码）。
- **🟠 N-0F7-2（P4，已修）判据由「夹具数 = 文案数」改为「代码可达路径穷举」**：旧循环只对 9 条手工夹具断言 `**`，覆盖 9/33 条可达文案（27%）；未覆盖分支的护栏**从未被验证**（复验一次性探针实测归一化口径 9/16 = 56%）。修法：以**币形态 12 种**（A 新鲜方向 4 变体 / B 仅陈旧 / C 已关联真 0 / D 未关联 / E 未关联+陈旧，各含密封与非密封）× 非空子集 = **4095 批**穷举，由**全路径**驱动三组断言 —— ① `**` 护栏（全 33 条）；② 文案集合**快照 = 33 条**（变化即须复核返回文案空间）；③ **分支原子可达性**（11 个原子逐一无遗漏，证明每条返回分支真实可达）；并断言**手工夹具 ⊆ 穷举集合**（枚举 ⊇ 夹具）。
- **🟡 N-0F7-3（P4，已修）图例未声明顿号双形态字面串**：原有描述式句「「另有币无催化剂/未关联资产」」**无字面串可逐字比对**（同类「图例落点」第 5 次）。修法：图例改为逐字列出三种并列串「另有币无催化剂」「另有币未关联资产」「另有币无催化剂、未关联资产」（**顿号 = 两者并存**），并声明「催化剂新鲜 X多/…」段同样可并列「另有币…」（N-0F7-1 的图例落点）。
- **🟠 N-0F7-4（P3，记档）部署范围披露不完整**：见上一节「待部署」更正（15 提交 / `scheduler.py` 5 条新调度 / 应部 HEAD / 三迁移已在 prod / 建议补 `schema_migrations` 台账）。
- **🟠 N-0F7-5/6/7（P4，记档）三处口径更正**：见上一节「口径更正」（**114 / 106 批** 分母、**断言无共振 3 批 → 0 批**、测试数字**标注运行环境**）。
- **真 DB 影响面（只读，发信口径 `confidence='high'` + main/BRK、不过滤 `status`；旧版 `2b4d18c` 树 vs 本轮真函数）**：近 7 天 **209 行 → 116 批**，标题变化 **27/116 批**（逐字不变 89）—— ① 5 批措辞统一（上轮）；② 3 批未关联披露（上轮）；③ **19 批 = N-0F7-1 新增并列披露**（如 `09-22 11:47:30` 4 币批：`…催化剂新鲜 0多/0空/1中` → `…0多/0空/1中，另有币无催化剂、未关联资产`）。含未关联币批次 4、其中标题断言「无共振」**3 → 0**（与上轮一致）；密封混合 5 批不变。
- **穷举完备性反证**：本轮真 DB 全部 **16 条唯一文案 ⊆ 穷举集合 33 条**（缺口 0）；相对 `2b4d18c`（14 条）新增 20 条、消失 1 条（`催化剂仅陈旧条目` 措辞）。
- **自测（运行环境：工作区 HEAD 含本节未提交改动）**：`test_scan_alert_header_regime.py` 扩至 **101/101**（+29：N-0F7-2 穷举 16 断言 + N-0F7-3 图例 4 + 夹具 ∈ 穷举 9 + N-0F7-1 回归 3）；其余 14 套与上节数字一致、无回归；`py_compile` 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效；**水位 ≥ 本节提交**；范围与迁移约束见上一节「待部署」更正。

### 告警邮件展开共振消息明细（2026-09-24）

需求：「在告警邮件中加入对应的催化剂消息」。**零 DDL、零阈值变更、不改共振判定口径**（只把已在 `resonance` 里取到、却从未渲染的消息本体展开）。

- **改动前状态**：卡片只渲染 `len()`（「事件2 · 催化剂5（4多/0空/1中） · KOL 1」），`res['event']/['catalyst']/['kol']` 三个消息列表**全仓从未被渲染**（grep 确认只被 `len()` 读）⇒ 收件人看得到「有几条」却看不到「是什么」。旧脚本 `scan_alert_monitor.py` 自带一份 `render_alert_email` 是**唯一**渲染消息本体的实现（旁证：原设计意图就是要渲染）。
- **三项口径（用户确认）**：① 范围 = **催化剂 + 事件 + KOL 三段全部展开**；② 每条 = **方向徽章 + 中文摘要 + 日期**；③ 上限 = **每段最多 4 条 + 明示「共 M 条，仅列最新 4 条」**。
- **`_get_resonance` 三段元素 `str` → `dict`**（`scan_daemon.py`）：事件段补 `ORDER BY event_date DESC NULLS LAST, id DESC`（让「最新 N 条」成立；长度即计数，口径不变）；催化剂 SQL 增选 `ac.ai_summary`，**展示文案优先 `ai_summary`、空则回退原文标题** —— 实测近 7 天 `ai_summary` **3775/3775 填充且为中文**，而 `title_cn` **0/3775 填充**（不可作展示来源）；KOL 段用 `count(*) OVER () AS total` 取**截断前**总数写入 `kol_total`（窗口函数在 `LIMIT` 之前求值），未关联币在第 3 段前 return ⇒ `out` 初始 dict 必须预置 `"kol_total": 0`。
- **⚠️ KOL 方向取值陷阱（本轮自查发现）**：`biz.kol_signal.direction` 的 CHECK 约束是 **`long`/`short`/`neutral`**（`kol_module_init.sql`，**不是** `up`/`down`）—— 按 `up/down` 映射会把**全部 KOL 误判为中性**（徽章恒灰）。实测近 7 天 prediction 行 `direction ∈ {long, neutral}`。已按 `long/short` 映射，摘要文案 `KOL 看涨/看跌/观望（SYMBOL）`。
- **新增渲染助手 `_render_resonance_msgs(res)` + 配色表 `_DIR_CN`**：徽章沿用文件内既有中文惯例（**利多=红 `#ef4444`/`#fee2e2`、利空=绿 `#22c55e`/`#dcfce7`、中性=灰 `#6b7280`/`#f1f5f9`**，与卡片左框、`↑做多` 同源）；文本经 `html.escape`；单条超 `RESONANCE_MSG_CHARS`(90) 截断加 `…`；三段皆空时**不渲染空壳**；挂载点为卡片 `共振：…` 行**之后**（保持风控行邻近计数行）。
- **截断披露必须与主数字同源**：`total` 催化剂取 `_catalyst_total(catalyst_dir)`（去重后全量，≤20，与卡片「催化剂 N」同一函数）、KOL 取 `kol_total`、事件取 `len(event)` —— 若取明细条数则披露本身变成新的口径不一致。
- **旧快照兼容**：历史落库的 `biz.scan_signal.detail.resonance_snapshot` 里元素是字符串（旧版 `f"{title}（{dir}/{strength}，{date}）"`）⇒ 渲染层非 dict 时按纯文本渲染，不抛异常。
- **真 DB 覆盖实测（只读 prod）**：近 30 天已告警 **205 币** → 事件段命中 **18 币**（`1INCHUSDT`/`AAVEUSDT`/`ENAUSDT`/`LINKUSDT`/`ONDOUSDT`/`TAOUSDT` 等，经 `_symbol_candidates` 归一后与 `event_watchlist` 的**裸符号**匹配）、KOL 段命中 **1 币**（`BTCUSDT`，KOL prediction 近 7 天仅 5 条、近 30 天 11 条 ⇒ 该段实际极少出现）、催化剂段多数命中。实测渲染：`PENDLEUSDT` 催化剂 8 条（4多/4中）→ 明细 4 条 + 披露「共 8 条，仅列最新 4 条」；`CETUSUSDT` → 「OKX将于2026年9月23日在USDC交易区新增CETUS/USDC…」；`1INCH` 事件段 → 「近7天大额转账 5 笔 / 合计 $11.3M …」；全渲染无 `**`。
- **⚠️ 事件段现状（据实披露，勿误读为漏渲染）**：`biz.event_watchlist` 当前**只有 `onchain_transfer`** 一种类型（91 行 / 91 币 / 每币 1 条，截断永不触发），且 **`event_date` 恒为 NULL**（91/91）⇒ 事件明细行**不显示日期**（诚实省略）；代码里的 `unlock`（🔓 解锁，记**利空** = 新增流通即抛压）分支**当前不可达**，链上转账方向不明一律记**中性**（不臆断）。
- **自测（运行环境：工作区，含未提交改动）**：`test_scan_alert_header_regime.py` 由 101 → **134/134**（+33：三段渲染 9 + 上限/披露 5 + 徽章配色 4 + 未关联/空壳 3 + 转义/截断/旧快照 3 + 图例声明 9）；`test_scan_alert_audit_deepdive.py` **75/75**、`test_scan_scenario_label.py` **48/48**、`test_scan_alert_remaining.py` **16/16**、`test_scan_l1_closed_bar.py` **16/16**、`test_derivatives_signal_gap.py` **35/35**、`test_squeeze_*` 三套 exit 0 无回归；`py_compile` 通过。两套测试的 `_res()` 夹具已同步为 **dict 形态**（含 `dir`/`kind`/`text`/`date`/`conf`/`kol_total`）。
  - ⚠️ **`test_scan_edge_metrics.py` = 68 通过 / 1 失败**（「已过 24h 且方向已知的行不得仍为 pending」结算滞后、「边缘桶不得含上限开口桶」），**与本次改动无关**：该文件在工作区已被**另一工作流**修改（`build_scan_edge_report.py`/`send_scan_edge_report.py`/`phase_check_cvd_ready.py` 同时处于未提交状态），失败项属其 WIP 范围。
- **待部署**：需重启容器（`scan_daemon`）后生效；**水位 ≥ 本节提交**；零迁移、零调度变更。

### B4 场景编号口径错位（审计_盘面异动扫描系统设计方案_v0.8，2026-09-23，本次提交）

来源：对 `04_架构与代码方案/盘面异动扫描系统设计方案_2026-09-16.md` v0.8 的审计（A1/A2/A3 科学有效性 + B4 口径 bug + C6~C10）。本提交只处置 **B4（展示层语义反转）** 与 **轧空告警降影子**；A1/A2 见下节工单。**零 DDL、零阈值变更、不改 `_compute_l2` 输出**（避免改动 §9 执行层选中的信号集）。

- **🔴 B4（P0，已修）`scenario` 两套编码共用编号空间、语义相反**：库内混有两种来源的 `scenario`——
  - **生产口径**（`scan_daemon._compute_l2`，L818）：只用 `(p_dir, oi_dir)` 两维 ⇒ `S1=P↑OI↑ / S2=P↓OI↑ / S3=P↑OI↓ / S4=P↓OI↓`
  - **设计口径**（`phase_scan_main_pool.py` 的 `compute_l2`，**未被 daemon 调度**）：含 `cvd_dir` 三维 ⇒ `S1..S8`
  - ⇒ 生产 `S2`（价↓+OI↑，真实空头）与设计 `S2`（价↑+OI↑+CVD↓，诱多）**同编号、语义相反**。渲染层原先统一取设计口径文案表 ⇒ 生产 S2 被标成「**诱多**」，方向判读完全反了。
  - **判别难点（勿再用错判据）**：生产行**也落 `cvd_dir`**（供渲染层显示幅度）⇒ **不能**靠「有无 CVD 维」区分来源。判据是 `PROD_SCENARIO_BY_DIMS[(p_dir, oi_dir)]` 是否等于行内 `scenario`——相等 ⇒ 生产行，不等 ⇒ 按三维重算设计编号。
  - 修法：`scan_daemon.py` 新增 `PROD_SCENARIO_BY_DIMS` / `PROD_SCENARIO_DESC`（S1 多头进攻 / S2 空头扎实 / S3 多头减仓 / S4 空头兑现）、`DESIGN_SCENARIO_BY_DIMS` / `DESIGN_SCENARIO_DESC`、`POOL_SCENARIO_DESC`，以及 `_scenario_label(sig)`；渲染循环的 `<b>{sc}</b> {desc}` 改为 `_scenario_label(sig)` 结果；图例改为「编号按行自身维度重算」两套口径说明。`send_scan_signal_brief.py` 同口径改造（表逐字一致，探针断言防单侧漂移）。
  - **先验分桶同因修正**：`_scenario_priors` 原按 `scenario` 分组（SQL `sg.scenario = ANY(%s)`）⇒ 两套编码的同编号行会串进同一桶。改为按 `(p_dir, oi_dir)` 象限分组（`quadrants` 入参、SQL 去 `scenario` 过滤、`sg.oi_dir IS NOT NULL`），文案改「历史同象限（价/OI 方向）」。
  - 物证（只读 prod，近 30 天主池 377 行）：`122 行`原被标错文案；典型 `S2/down/up` 17 行原渲染「诱多」→ 现「空头扎实」；`S3/up/down` 34 行原「空头扎实」→ 现「多头减仓」；`S4/down/down` 71 行原「诱空」→ 现「空头兑现」。
- **🟠 轧空实时告警降影子（`SQUEEZE_ALERT_SHADOW = True`）**：轧空判定阈值标定在极小且单一 regime 样本上、样本内选参、无 holdout（见下节工单）⇒ 影子期内照常入队/跟踪/判定并落库（`status='confirmed'`，供回放取数），**只不发邮件**；`stats` 增 `shadow` 计数（不静默吞掉）。
  - ⚠️ **影子期刻意不写 `alerted_at`**：它同时是主池「跨池互斥」的判据（`_cross_pool_recent(..., ("squeeze",))`）——影子期既然不发信，就不该让主池因一封并不存在的邮件被静音。
- **自测**：新增 `workbench/test_scan_scenario_label.py` **48/48**（生产/设计两套映射逐例 + 同编号判别有效性 + BRK/ACC/SQZ/None 兜底 + 卡片与图例接线 + AST 断言「渲染调 `_scenario_label`」「`_scenario_priors` 分桶键为 `(p_dir, oi_dir)`」「SQL 不再按 scenario 过滤」+ 日报同口径 + 只读库自洽不变量）；`test_squeeze_battle.py` 扩至 **143/143**（+6 影子模式守卫）；既有 `test_scan_alert_header_regime.py` 44/44、`test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16 无回归；`py_compile` 2/2 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效。
- **文档同步**：设计方案 v0.8 已按「只保留当前逻辑」原则清理（删除版本史 changelog、所有「旧稿/原实现/已作废/✅已修」注记），未决项统一收敛为 §12.1（A 类科学有效性 / B 类口径标定 / C 类代理口径与覆盖）。

### 待办工单：样本外验证与方向结论收口（A1/A2/A3，需设计变更，勿盲目改）

来源：同上审计。**性质是「结论有效性」而非代码缺陷** ⇒ 不得靠改阈值「修」，须补样本与流程。

- **A1（🔴 科学有效性）全系统参数标定在极小 + 单一 regime 样本上**：`confirm_signals` 90 条/3 日、止损带 131 条/6 天、OI 相关 21 天；关键分组的 t 值 **均 < 2**（`+5.239%` t=1.23、`+15.58%` t=0.38）。⇒ 当前所有「已标定」结论都只是**样本内拟合**，不构成显著性证据。
  - 待办：① 建立**样本量与 t 值门槛**（建议 n ≥ 30 且 t ≥ 2 才允许称「标定结论」，否则一律标「观察值」）；② 在阈值/常量的注释与设计方案中强制标注样本区间与 t 值；③ 把 `calib_*.py` 的产物（`biz.*_calibration`）加**样本量列**，不足门槛的维度回退默认值（现有 `n≥30` 门槛只覆盖催化剂权重，需推广到扫描侧）。
- **A2（🔴 科学有效性）「空头默认降级、只做多」由单边上涨样本反推**：执行层 `phase_execute_scan_signal.py` 取 `p_dir='up' AND scenario IN ('S1','S2')`（等价 P↑OI↑）的唯一依据是「回测中它唯一稳定正期望，空头侧全负」——而样本期是**单边上涨 regime**。⇒ 这是**逻辑越界**：在上涨 regime 里空头全负是必然，不能推出「空头无 edge」。
  - 待办：① 补 **regime 分层回测**（至少按 `btc_1h` 方向 / FGI 档 / 市值趋势分档），逐档报 n 与方向对齐中位；② 执行层恢复空头侧前，须有**非上涨 regime 子样本**的独立证据；③ 在方案 §9 与代码注释中把「只做多」显式标为「**样本期结论，非结构性结论**」（当前已加注，见 §12.1-A2）。
- **A3（🟠 无 holdout）全流程无留出集**：参数在**全样本**上选优 ⇒ 数据窥探（data snooping）。待办：① 把已有数据切**时间留出**（如最后 20% 时段）并在留出集上复核全部阈值；② 之后新参数一律「训练集选参 → 留出集验证」；③ 留出集结果**入库留档**（新增校准结果表或复用 `biz.*_calibration` 加 `split` 列），避免再次出现「结论只存在于对话里」。
- **关联**：设计方案 §12.1 A 类条目；本工单与 `待办（需设计变更，勿盲目改）` 同性质——**改前须先补样本，勿用当前 3~6 天样本调参**。

### 拉升期结构分解与轧空衰竭判定落地（设计方案 §10.10，2026-09-23，本次提交）

来源：设计方案 §10.10 此前标「设计稿 · 未落地」。本次按 §10.10.7 三步清单实现，**零 DDL、零阈值联动、不改 §10.6 判定口径**。

- **新增纯函数模块 `scripts/src/crypto_research/analysis/squeeze_fuel.py`**：窗口 `W = [surge_start_ts, now]`（**与 §10.6 的 `[peak_ts, now]` 不同窗口，勿混用**）。
  - 判定 `classify_fuel`：五 verdict `sqz_fuel_exhausting / sqz_fuel_active / long_pump / short_rebuild / mixed`，**优先级即书写顺序**（先判 OI 升的两类）⇒ 假信号「多头主动开仓拉盘」结构上不可能落进 `sqz_fuel_*`。
  - 闸门 `fuel_gate`：复用 §10.5 口径（覆盖率 / 尾部 `oi_lag_sec ≤ 2×桶` / `window_gate` 连续缺桶）+ 本节新增两条（W 内 LSR 点数 ≥ `MIN_LSR_POINTS`、有效 5m 桶 ≥ `MIN_FUEL_BUCKETS`）。缺桶文案**不**复用 `squeeze.gap_reason()`（其「判定窗口」前缀是 `check_scan_freshness` 的 `LIKE '判定窗口%'` 耦合点，属判定口径统计）。
  - 两个**代理口径**：`proxy_shares`（`longShare = r/(1+r)`，占比由已存比值反推，**无需改表**）、`quadrant`（`OI × CVD` 四象限反推买平/开仓）。
  - 阈值常量**全为经验初值、未标定**（§10.10.4 / §10.10.8：`biz.long_short_ratio` 只对 tracking 币采集且历史极短，无样本可回测）。
- **⚠️ 三条易错点已用单测钉住**：
  1. **「衰减必须先有峰值」**（§10.10.5）：`liq_peak` 未越阈时 `liq_decay` 是无意义比值（分子分母都极小），一律不得判 `sqz_fuel_exhausting` —— 实际是「爆仓从未发生」。
  2. **`cvd == 0` / `oi_delta == 0` 必须显式判掉**：写成 `cvd > 0 ... else ...` 会把「无方向」静默归入**卖压**侧（实现时即被单测抓到并修正）。
  3. **缺失 ≠ 0**：结构量（`d_oi_pct`/`d_s_pct`/`d_l_pct`）缺失 ⇒ `mixed`；`liq` 缺失 ⇒ 既不判 `exhausting`、也不判 `active`（后者断言「爆仓仍在高位」）⇒ 同样 `mixed`；`cvd_divergence` 未知只挡 `exhausting`。
- **接线（`scan_daemon.task_scan_squeeze` 阶段 2）**：
  - `_fetch_long_short_ratio` 默认 `limit` 20 → **100**（≈8h）：W 上限 180min，且首个 LSR 点的**基准桶**需落到窗口左端之外（解基准桶取错）。
  - 每轮跟踪调用 `evaluate_fuel`，结果落 `metrics['fuel']`（**`metrics || %s` 合并**语义，不覆盖入场指标）；判定闸门拒判路径与 judged 路径**都落**（两条闸门各自独立，窗口与口径不同）。
  - **影子模式**：只落库、**不发信**（`task_scan_squeeze` 内仍只有 §10.6 判定那一处 `notifier.send`，AST 守卫）；拒判记 `verdict=None` + `gate_reason`（与 `mixed` 的「结构不明」区分）。
- **自测**：新增 `workbench/test_squeeze_fuel.py` **99/99**（阈值 / 代理口径 / 序列纯函数 / 五 verdict + 优先级 + 缺失降级 / 五道闸门 / 端到端 / AST 接线守卫）；`test_squeeze_battle.py` **143/143**、`test_scan_scenario_label.py` **48/48** 无回归；`py_compile` 3/3 通过。
- **待部署**：需重启容器（`scan_daemon`）后生效。
- **仍待办（§10.10.8，不在本次范围）**：① 标定脚本 `calib_squeeze_fuel_thr.py`（口径同 `calib_squeeze_liq_thr.py`）；② `SQZ_FUEL_*` 与 §10.8 判定邮件的跨池互斥接线；③ 影子观察期长度与开信门槛。

### 轧空邮件静默断流修复 + 输出面观测（诊断_轧空邮件断流_2026-09-23，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\诊断_轧空邮件断流_2026-09-23.md`（用户报「凌晨 4 点后再没收到轧空邮件」）。**零 DDL、零阈值变更、未改发信行为**。

- **根因（物证级）**：`SQUEEZE_ALERT_SHADOW = True`（由 `90ec437` 夹带引入、commit message 未提）随 daemon 重启于 `∈(10:11,12:22] CST` 上线，把「轧空发信」短路。**04:12:28 CST 最后一封之后 8 笔判定 0 发信**（`id=1606 PHAUSDT` 起，全部 `alerted_at=NULL` 且无跨池互斥标记 ⇒ 非静音、是走了发信分支却没发）。上游 liq/oi_cvd/klines 全部新鲜、main/蓄势池同 notifier 发信正常 ⇒ 排除数据断档与 SMTP 故障。
- **为什么没有一封告警（真问题）**：两层看门狗**都只观测输入面**（数据新鲜度 / 队列占用 / 24h 入队 / 拒判数），**从不观测输出面（邮件是否真的发出）**。影子模式下判定照常落库、`signal_ts` 持续推进 ⇒ `_collect_items` 永远 `[ok]`、`_collect_squeeze_health` 三项越线条件全与发信无关 ⇒ 静默断流可无限期持续。`_mark_alert_suppressed` docstring 声称的 `_stall_parts` 因果链对 **squeeze 行本就不适用**（丢信号检测候选集是 `main`/`BRK`，不含 squeeze 池）——已在 docstring 就地澄清。
- **决策**：用户授权「按你的判断处理」⇒ **维持影子**（开关自陈的「阈值标定在极小单一 regime 样本 + 无 holdout」理由仍成立，prod 样本长期 `rc=3` ⇒ 恢复发信无依据），但**补输出面观测 + 影子可观测标记**。
- **改动 1（`scan_daemon.py`）**：新增常量 `SHADOW_MARKER_TASK = "squeeze_shadow"`；影子分支在 `stats["shadow"]` 后写 `_write_heartbeat(SHADOW_MARKER_TASK, True)` —— 把「有意不发信」从**不可观测**变成**可观测量**（该 task 键不在 `STALL_HEARTBEAT_TASKS` / `HEARTBEAT_MAX_AGE_MIN` 内，不会被停摆检测误当任务）。
- **改动 2（`check_scan_freshness.py`）**：新增 `SQUEEZE_SILENCE_WINDOW_H = 6` + `SHADOW_MARKER_TASK`；纯函数 `_squeeze_silence_note(silenced, shadow_ts, now)`（`silenced<=0`→None；影子标记新鲜≤窗口→**主动静默**文案；否则→**疑似发信分支故障**文案）；`_collect_squeeze_health` 接入判据 `pool='squeeze' AND status='confirmed' AND alerted_at IS NULL AND alert_suppressed_at IS NULL AND signal_ts > NOW()-6h`。
- **部署后行为**：影子期每 6h 收到一封「轧空通道影子模式：…主动静默 N 笔（非故障）」健康提示（去重键 `squeeze_health`，与 `scan_stall` 互不抑制）——这正是防止下次「以为链路坏了」的机制；若**标记缺失/陈旧而仍有静默** ⇒ 报「疑似发信分支被短路或发送失败，请立即检查 scan_daemon」。
- **自测**：新增 `workbench/test_squeeze_alert_silence.py` **18/18**（纯函数 6 例 + 边界 2 例 + 跨文件常量同值 + AST 接线守卫 + `**` 文案护栏）；既有 `test_squeeze_battle.py` 143/143、`test_squeeze_fuel.py` 99/99、`test_scan_alert_header_regime.py` 72/72、`test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_scan_scenario_label.py` 48/48、`test_fundamental_liquidity.py` 13/13、`test_derivatives_signal_gap.py` 35/35 无回归；`py_compile` 2/2。
- **prod 只读实测（部署前）**：`silenced_6h = 7`、`shadow_marker = None` ⇒ 判「疑似故障」（符合预期：旧构建无标记）。部署后影子分支首次吞批即写标记，文案转「主动静默」。
- **待部署**：需重启容器（`scan_daemon` 写标记 + `scheduler` 跑看门狗）后生效。**未做**：恢复发信（维持影子，产品决策）；补发方案 B（需定义「补发有效性」边界，仍挂账）。

### 重大事件通道（BCH/UNI CME 期货漏报，2026-09-23，本次提交）

来源：用户报「CME 将推出 BCH 与 UNI 期货（2026-09-22 20:44 CST）是很大利好，但没收到催化剂邮件」。**零 DDL、未改既有 A 级 Alert 的 tier 门槛**。

- **物证链（先确认不是漏采）**：该新闻确实进了管道——5 条重复催化剂 `10571/10572/10573/10574/10576`，`published_at` 2026-09-22 12:33~12:43 UTC（= 北京 20:33~20:43，与截图 20:44 吻合）；`authority_score 85~88`、`event_weight 15/75/75/75/98`、`prelaunch_ret_24h 8.2556`、`prelaunch_penalty 0`、`tradable=True`。信号也生成了：BCH `signal 650174`（`composite_score=65 / tier=C / status=watch`）、UNI `signal 650175`（`61 / C / invalid`）。
- **根因 1（A 级 Alert 结构性发不出）**：`tier='A' AND status='open'` **全表 0 行** ⇒ 告警查询命中 0，静默跳过。最后一次成功发送是 2026-09-22 05:14 UTC（`log_id=4`）。
- **根因 2（价格完整性闸门误伤，已修 `81f281c`）**：`run_signal` 是快通道每轮**全量重算**入口，却把 `entry/stop/tp` 与 G4/G5 一律传 `None` ⇒ 价格完整性闸门对**每一行**触发 ⇒ A/B 全封顶 C、open 全降 watch；且 upsert 对 `fundamental_pass`/`technical_state` 未 `COALESCE` ⇒ 慢通道 G4/G5 每轮被抹成 NULL，闭环使 `open→watch` 的行退出 `run_slow_g3g5` 候选集（`status='open' AND technical_state IS NULL`）⇒ 价格与 G3-G5 再也不会被补全。全量复算 4195 条：**A+open 0→2、B+open 0→240**。
- **为什么不把「重要性」并进 tier 门槛**：实测 tier 门槛本身混杂可交易性——`bearish` 9 条 A / 101 条 B、`neutral` 41 条 A / 412 条 B 混入 A 级；`confirmed` 72h 超额 **-2.16%(n=47)** vs `weak` **+1.58%(n=231)**；COPPER 三档全 0 仍进 A。⇒ **拆分两条通道**：`tier` 继续管「可交易性」，新增独立通道管「重要性」。
- **判据回测（否定性结论，先做后写）**：① `authority_score>=80` 命中 **8090/8468（96%）**，零区分度，草案被否；② 标题去重几乎无效（8093→7959）——BCH 5 条来自不同媒体不同措辞，`content_hash`/`source_article_id` 互异；③ `prelaunch_ret_24h` **只在 2026-09-18 起有数据**（更早全 NULL）⇒ 「近 30 天回测」实际只有 6 天窗口；④ 纯 catalyst 级判据 70 行/23 事件 ≈5.75/天，噪声重（`asset_id=2` BTC 兜底行情类、`title=null`、「TL;DR」、「CoinW研究院」等）；⑤ **信号侧判据胜出**：`tier IN ('A','B') AND status='open' AND prelaunch_ret_24h>=5 AND prelaunch_penalty=0` → 6 天 9 行/8 事件 ≈**1.33/天**，**含 BCH**，9 条中 8 条高质量（BCH CME 期货×2、Cardano x402、Ondo/Alpaca、XRP+8% Ripple、Bitmine ETH、Sui、PONS Bybit 上线），仅 1 条噪声（ATOM 24h 涨幅播报，`ai_event_type='market_update'`, evw=15）。
- **定稿判据**：`tier IN ('A','B') AND status='open' AND catalyst_kind IN ('structural','event') AND prelaunch_ret_24h >= 5 AND prelaunch_penalty = 0 AND COALESCE(ai_event_type,'') <> 'market_update'`（加 `<>'market_update'` 排除行情播报类 ⇒ 7 事件 ≈1.17/天）。
- **去重键 = 按资产**（非标题 / 非 `content_hash`，因跨语言跨媒体归并失效）：`DISTINCT ON (s.asset_id)` + 同资产 24h 内已有 `notification_type='major_event' AND status='sent'` 则跳过；窗口锚 `ac.published_at > NOW() - 24h`；单轮 `LIMIT 3`。
- **改动 1（`workbench/catalyst/notifier.py`，+约 245 行）**：常量组 `NTYPE_MAJOR_EVENT='major_event'` / `MAJOR_EVENT_MIN_PRELAUNCH_RET=5.0` / `MAJOR_EVENT_COOLDOWN_HOURS=24` / `MAJOR_EVENT_KINDS=('structural','event')` / `MAJOR_EVENT_MAX_PER_RUN=3`；`_recent_major_events()`（候选 SQL，复用 `ASSET_NAME_FILTER_SQL`）；`_major_event_subject()`；`_build_major_event_html()`（橙色警示头「📢 重大事件通报」+ 明写「本条为「重要性」通道通报，不含交易档位，**非交易建议**」；信息表 = 重要性/催化方向/市场确认/共振状态/当前价/信号分层/原文链接 + AI 摘要 + 原文段；**不含**止损止盈入场）；`send_major_event_alerts()`（`ensure_notification_table` → 取候选 → 逐条 `_try_acquire_send_lock` → `_send_email` → `_mark_sent`，异常不阻断返回 `sent=0`）。复用 `biz.catalyst_notification_log` 现有表，**无需迁移**。
- **改动 2（三处挂载点）**：`scripts/bin/catalyst_fast_daemon.py` `run_fast_once`（+ `main()` 轮次日志加「重大事件:N」）；`scripts/bin/phase_catalyst_pipeline.py` 的 `--fast` 分支与慢通道分支（每 4h 兜底）。**不依赖 `new_sig_ids`**——重大事件判据与「本轮是否转为 open」无关，否则漏掉状态未变的老信号。
- **自测**：新增 `workbench/test_major_event_alert.py` **30/30**（通道独立 / 判据独立于 tier / 事件级去重 / 负 alpha 排除 / 市场确认门槛 / 渲染护栏（含「非交易建议」且不含止损止盈）/ 失败不阻断 / 单轮上限）；`py_compile` 3/3 通过。
- **实发验证**：第 1 次 `send_major_event_alerts` → `{'sent':1,...,'signals':[650167]}`（`notification_log` 新增 `log_id=72 | signal_id=650167 | major_event | tier=B | status=sent | subject=📢 [重大事件] BCH - ...`）；第 2 次 → `sent=0`（24h 冷却挡住，幂等性通过）。
- **待部署**：需 Zeabur **显式 redeploy**（否则 `81f281c` 的价格闸门修复与本轮新通道都不生效）。
- **独立问题（未修）**：`catalyst_fast_daemon` 疑似长期零产出——近 24h 仅 5 个大批次写入（正常应约 96 批），与 AGENTS.md 早前「快通道零产出」记载一致 ⇒ 分钟级时延暂不可得，**当前新通道由慢通道每 4h 兜底**。恢复快通道需先解决其 FATAL（见「已知待办」）。

### A 级快讯 AI 否决闸门（审计_催化剂A级邮件_XRP_BCH_2026-09-24，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\_audit\审计_催化剂A级邮件_XRP_BCH_2026-09-24.md`。**零 DDL、未改 tier 计算、未改 AI 评审规则**。

- **现象（审计原例）**：XRP 信号 `composite_score=86 / tier=A / status=open`，邮件发「🚀 A级催化剂信号」并附「📈 做多 · 入场 $1.58 / 目标 $2.01 / 止损 $1.41 / 盈亏比 2.5」；而**同一封邮件**的 AI 深度评审写着「资产匹配 low · **不建议参与** · 0% 仓位」。自身前后矛盾，扫一眼档位的人会得到与警告相反的动作。
- **根因 1（P0-D2）**：`_score_to_tier` 只看综合分（[signal.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/catalyst/signal.py#L397-L405)），方向闸门也只认 `bearish/bullish`，**均不消费 `asset_match_confidence` 与 AI 交易结论** ⇒ 资产错配、AI 判 low 的信号照样拿 A 级推送位。
- **根因 2（P0-D1）**：交易档位区块（`_build_fast_alert_html`）原判据是 `if tp is not None or sl is not None or entry is not None`，**完全不消费 AI 结论** ⇒ 即便 AI 说不建议参与，档位照渲。
- **为什么不能落在 `_score_to_tier`（审计选项 A 的原位）**：`ai_deep_review` 由 **G7（AI 增强）**产出，`tier` 由 **G6（`build()`）**计算——同一轮内算 tier 时 AI 结论**尚不存在**（首轮必然 NULL，评审在 `send_fast_alerts_for_new_signals` 内部才生成）。因此否决只能落在**通知层**，那是两者都在手上的唯一位置。改 `signal.py` 需读回上一轮评审，会让 tier 依赖跨轮状态且首轮仍漏，故不取。
- **判据（新增纯函数 `_ai_review_blocks_alert`，两处调用共用，避免口径漂移）**：
  - `asset_match_confidence == 'low'`（大小写/空白容错）
  - `verdict` 含「不建议」（与渲染层配色判据同一约定）
  - 评审缺失 / 非 dict / 字段为空 ⇒ **一律不否决**。本闸门**只做减法**，不因 AI 异常扩大拦截面。
- **改动 1（P0-D2，`notifier.py` 发送侧）**：在 `send_fast_alerts_for_new_signals` 的发送循环内、**`_try_acquire_send_lock` 之后**加否决分支 ⇒ 命中则 `_mark_sent(status='suppressed', error_msg='AI 否决：…')` + `logger.info` + `continue`。**顺序有意如此**：放在加锁之后，已有 `sent` 记录的信号会先走 `skipped` 分支，不会被改写成 `suppressed` 而污染历史留痕。返回值新增 `suppressed` 计数。
- **改动 2（P0-D1，`notifier.py` 渲染侧）**：`_build_fast_alert_html` 的档位区块改为「先判否决」——命中且确有档位时渲染「📊 交易计划：已抑制」+ 原因，**不出现方向/入场价/目标价/止损价/盈亏比任何一项**；无档位可抑制时不凭空造块。与发送侧共用同一判据。
- **改动 3（可观测性）**：`catalyst_fast_daemon` 轮次日志加「抑制:N」并纳入 `stats`；`phase_catalyst_pipeline` 的 `--fast` 分支加「🚫 快提醒: AI 否决抑制 N 条」。**理由**：审计的投诉点正是「静默跳过」——若只丢不记，等于换个姿势重犯。
- **自测**：新增 `workbench/test_fast_alert_ai_veto.py` **46/46**（判据纯函数 17 例含「建议参与/强烈建议参与」不被子串误伤 + 渲染护栏 11 例 + 反向不误伤 6 例 + 顺序护栏 3 例 + 可观测 6 例 + 返回契约 3 例）；`py_compile` 4/4。
- **prod 只读实测**：审计原例 `signal=1085989 XRP score=86 status=open`，`ai_verdict='不建议参与'`、`ai_asset_match='low'` ⇒ **`_ai_review_blocks_alert` 判 VETOED=true**（闸门确实拦得住这封邮件）。近 3 天 A 级信号共 **10 条，仅 1 条**会被抑制 ⇒ 无误伤。`catalyst_notification_log` 现有 status 只有 `sent/failed` ⇒ `suppressed` 是新值，不与既有语义冲突（`_is_sent` 只按 `sent_at` 判窗口、`_recent_major_events` 只认 `status='sent'`，均不受影响）。
- **顺带观测**：`major_event` 留痕已有 3 条（`log_id=72` 本地探针 BCH；`74/76` 于 2026-09-23 16:06:01 UTC 由线上进程实发 AAVE/LIT，同一微秒时间戳 ⇒ 同批次 2 封）⇒ **上一轮 `c000fec` 的代码确已在线上跑**，佐证本次修复同样**只需 redeploy 即可生效**。
- **待部署**：需 Zeabur **显式 redeploy**（同 `81f281c` / `c000fec`）。
- **本轮范围外（审计 P1/P2/P3 共 11 项）——已在下一节修复 9 项**：量比缺失值默认 0 标「极度缩量」（与 `ai_enhance` 的「未知」口径不一）、`description_short` 内嵌 stale 价格、`total_liquidity_usd`（$2.02M）被当可交易量做「稀薄」叙事（疑脏数据，需 prod 复核）、共振分 90 与「弱共振」同屏、双置信度并列、警告文案硬编码「ticker同名但不同项目」、规则档位与 AI 风控不校准、源脆弱（结构性催化应以官方公告为锚）、赛道贴标、标题 `XRP / XRP` 冗余。

### 邮件时区统一为东八区（2026-09-24，用户指令「所有邮件中信息的时区都改成东八区」，本次提交）

**范围**：只改**展示层**。DB 会话时区、数据存储、判定口径、`bar=` 去重键**一律不动**（`bar=` 仍是 UTC 根，改它会破坏 P2-7 同根去重与历史数据）。控制台 `print` 日志**不在范围内**（如 `check_scan_freshness.py` 的 `[看门狗] … UTC 检查：` 保留 UTC）。

- **唯一定义**：新增 [time_utils.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/scripts/src/crypto_research/utils/time_utils.py) —— `BJ_TZ = timezone(timedelta(hours=8))`、`to_bj(dt)`（aware 直转 / naive 按 UTC 解释 / ISO 串与 `None` 容错，失败返 `None`）、`fmt_bj(dt, fmt, fallback)`。**禁止**再在别处重复定义 `timezone(timedelta(hours=8))`（原已有 4+ 处各自定义）。
- **改造点（8 文件）**：
  - `scan_daemon.py`（告警 / 停摆 / 轧空三封邮件）：抬头 `生成于`、停摆项 `停在 …`、任务心跳 `最近一次成功停在 …`、轧空 `入队→峰值→判定` 时间轴（`_hhmm_utc` → `_hhmm_bj`）；BRK 卡片 `触发根` 由 UTC `bar=` 串 +8（`2026-09-22T06` → `09/22 14:00`）。
  - `check_scan_freshness.py`：`_fmt_utc` → `_fmt_bj`（含「（北京时间）」），覆盖检查项「最新时间」列、静默失败交叉判据、OI 桶缺口「起于 …」、恢复邮件「告警发出于 …」。
  - `send_highlight_alert.py` / `send_scan_signal_brief.py` / `phase_check_cvd_ready.py` / `scan_alert_monitor.py`：抬头 `生成于`（原 `datetime.now(timezone.utc).astimezone()` 在容器 TZ=UTC 下实为 UTC 且无标注）。
  - `send_scan_signal_brief.py` 表格「时间」列：原 `str(signal_ts)[5:16]`（UTC）→ `fmt_bj(...)`，表头改「时间(北京时间)」。
  - `send_daily_brief.py`：`_fmt_liq_as_of` 改用共享 `fmt_bj`（去掉本地 `ZoneInfo("Asia/Shanghai")`），展示点补「（北京时间）」。
- **本就合规、未动**：`send_scan_edge_report.py`（已用 `SH` 且标注「北京时间」）、`binance_bapi_healthcheck.py`、`workbench/catalyst/notifier.py::_fmt_ts`（aware → 北京 + 「（北京）」）、`workbench/kol/notifier.py`（「北京时间」）、`clients/notifier.py`（无时间展示）。
- **验收**：`py_compile` 9 文件全过；`test_scan_alert_header_regime.py` **135/135**（触发根断言由 `09/22 06:00` 改 `09/22 14:00`，并新增「抬头标注北京时间」护栏）；`test_scan_alert_audit_deepdive` 75/75、`test_scan_alert_remaining` 16/16、`test_scan_scenario_label` 48/48、`test_scan_l1_closed_bar` 16/16、`test_squeeze_alert_silence` 18/18、`test_squeeze_battle` 143/143、`test_highlight_alert` 68/68、`test_daily_brief_p1` 22/22、`test_liq_overview_brief` 49/49 全无回归。
- **真 DB 端到端**：告警邮件实测 `bar=2026-09-23T23` → `触发根 09/24 07:00`、抬头 `生成于 2026-09-24 08:59（北京时间）`；`send_scan_signal_brief --dry-run` 抬头与「时间」列均北京时间；`send_highlight_alert --dry-run` 抬头北京时间。
- **待部署**：`scan_daemon` 需重启容器（渲染层在常驻进程内），其余为 scheduler 一次性脚本，下次调度即生效。

### A 级快讯展示层审计剩余 11 项（审计_催化剂A级邮件_XRP_BCH_2026-09-24，本次提交）

来源同「A 级快讯 AI 否决闸门」节。承接 `4307fb1`，本轮修掉审计剩余 **9 项**；**零 DDL、未改 tier 计算、未改 AI 评审规则**（只扩 prompt 与输出白名单）。D11（信息源脆弱 / 实体链接弱）属上游架构、D10 的**分类数据本身**属 CMC 数据治理，两者**未修**（见末条）。

**根因订正（与审计猜测不同，均为 prod 只读探针物证）**

- **P1-D4 真因不是「默认值」而是「SQL 没查」**：发送路径的两条 SQL（首查、AI 增强后重查）**根本没 SELECT `volume_ratio_7d`** ⇒ `row.get("volume_ratio_7d")` 恒为 `None` ⇒ 原 `float(vr) if vr is not None else 0` 得 `0` ⇒ 命中 `vr_val <= 0.5` 标「极度缩量」。更隐蔽的是 `_build_anomaly_quick_card` 的早退守卫 `if vr is None and v24 is None` 被**有值的** `volume_24h_usd` 绕过，于是渲染出「0.00x 极度缩量」。另：两通道 7 日均量窗口本就漂移——`_fetch_signal_row` 取「最近 7 个交易日」，发送路径取「MAX(market_date) 之前的**全部历史**（实测 119 天）」。
- **P1-D5 真因是 `biz.asset_liquidity` 是按链分行的 DEX 池快照表**（实测 116 行 / 49 资产，每资产 1~21 行，chain ∈ ethereum(41)/solana(15)/polygon(10)/base(8)/bsc(7)…），原 SQL `LIMIT 1` **无 ORDER BY** ⇒ 取哪条不确定。XRP(1127) 只有 1 行：`chain='solana'`、`source='geckoterminal'`、`total_liquidity_usd=$1.91M`——是 Solana 上的 wXRP/USDC 池，与「24h 成交量 $7.80B」差约 **4000 倍**，不是资产不流动，而是链上池覆盖不足。
- **P2-D6 是系统性现象**：近 14 天 A 级 open 信号的 `resonance_state` **全部为 weak**，`resonance_score` 区间 67~98 ⇒ 高分 + 弱共振是常态，属**两个独立口径**（共振分 = `excess*0.5 + vol_z*0.3 + direction*0.2` ± peer/divergent；共振状态由 `_determine_state(excess, vol_z, direction_match)` 阈值判定）。

**改动 1（`workbench/catalyst/notifier.py`）——抽共享 SQL 片段，一处口径三处共用**

- 新增模块级 `_MARKET_LATERAL_SQL`（三个 `LEFT JOIN LATERAL`：最新日行情 `md_latest`、**严格 7 日**均量 `md_avg`、链上池流动性 `liq` 且带 `ORDER BY total_liquidity_usd DESC NULLS LAST, chain` 消除非确定性）与 `_MARKET_COLS_SQL`（`current_price/change_24h_pct/change_7d_pct/volume_24h_usd/avg_volume_7d/volume_ratio_7d/is_volume_spike/liquidity_score/liquidity_chain/liquidity_source`）。
- **首查、AI 增强后重查、`_fetch_signal_row` 三条 SQL 全部改为 `""" + _MARKET_COLS_SQL + """` / `""" + _MARKET_LATERAL_SQL + """` 拼接** ⇒ 根治 D4 的「恒为 None」与两通道窗口漂移。`_fetch_signal_row` 因后续还有衍生品列，续行**前置逗号**（`_MARKET_COLS_SQL` 末列无尾逗号）。
- **D4 渲染层**：`vr is None` 时**不再伪造 0**，改渲染「—」+「7 日均量缺失，无法计算」，不落进「极度缩量」分支。
- **D3**：新增 `_strip_stale_price_sentences()`（正则剔除 CMC 标准后缀句：`last known price` / `is up|down X over the last 24 hours` / `traded over the last 24 hours` / `active market(s)`），在 `_build_fast_alert_html` 渲染简介前、以及 `_signal_row_to_deep_review_input` 喂 LLM 前**各清一次** ⇒ 邮件内不再出现 stale 价 $1.087 与实时 $1.57 自相矛盾，且 AI 也不再被脏文本误导。
- **D5**：新增 `_liquidity_label(row)` → 「链上池流动性（solana）」；原「流动性（24h）」误导性标签移除。**根因在 prompt**（见改动 2）。
- **D12**：`header_name = symbol if not name or str(name) == str(symbol) else f"{symbol} / {name}"` ⇒ 消除「XRP / XRP」。
- **D7**：`置信度 Z%` → **`模型方向置信 Z%`**，与 AI 的「信心度：低」区分口径。
- **D6**：共振状态行补口径说明「（按涨跌幅/量能阈值判定，与上方共振分不同口径）」。
- **D8**：警告文案改为**优先取 `ai_deep.get("asset_match_reason")`**（渲染为「AI 判定原因：…」），缺失时回退**不臆断成因**的通用文案「系统检测到该代币与催化剂所述项目可能不一致，请谨慎核实后再做决策。」⇒ 不再对每种 low 一律写死「ticker同名但不同项目」。
- **D9**：交易计划块标题改「📊 交易计划（规则计算）」，并明写「档位由规则按价格结构派生，**未与 AI 风控建议校准**；两者不一致时请以下方「🤖 AI 深度评审」的进场/止损/止盈建议为准。」
- **D10**：上线时间 `—` → **`未收录`**；赛道行补「赛道为 CMC 分类口径，仅作参考」。

**改动 2（`workbench/catalyst/ai_enhance.py`）——D5/D8 的根因在 prompt 与白名单**

- 「## 五、风险与流动性」加口径提示：该值只覆盖**某一条链上的 DEX 池**、不是全局流动性，必须与「24h 成交量」交叉对照，**不得**仅凭此值断言「流动性稀薄 / 易插针 / 大额进出造成滑点」——两者相差数个量级时说明是**链上池覆盖不足**，而非资产本身不流动。
- `asset_match_confidence` 定义由「同名不同币」扩为**三种 low 情形**（① ticker 同名但不同项目；② 该代币只是新闻里的被动提及/顺带列举；③ 跨链同名资产被误绑），并**新增 `asset_match_reason`**（low 时必填、一句中文说明具体是哪种错配；非 low 留空）。
- 标准化输出白名单加 `"asset_match_reason": str(data.get("asset_match_reason") or "")[:200]`。

**改动 3（`workbench/test_fast_alert_audit_rest.py`，新增）——离线护栏**

- **61/61 全绿**，11 组断言：D3 剥 stale 句 10 例 / D4 量比卡 7 例 / **D4 三 SQL 同源 7 例（源码级：`AS volume_ratio_7d` 恰出现 1 次、`_MARKET_COLS_SQL +` 与 `_MARKET_LATERAL_SQL +` 各 3 次）** / D5 9 例 / D6 2 例 / D7 3 例 / D8 8 例 / D9 6 例 / D10 3 例 / D12 3 例 / 返回契约 3 例。
- 因 `_build_anomaly_quick_card` 是 `_build_fast_alert_html` 内的**嵌套函数**（非模块级），D4/D9 断言一律**经渲染 HTML 间接验证**；D9 必须用**非否决**的 AI 评审（`asset_match_confidence='high'`、`verdict='建议轻仓参与'`）构造夹具，否则交易计划块会被上一节的否决闸门整块替换为「已抑制」。

**验收**

- `py_compile` notifier.py + ai_enhance.py 通过；`test_fast_alert_audit_rest.py` **61/61**；workbench 全量 **22 个 `test_*.py` 全部 exit=0**（含 `test_fast_alert_ai_veto.py` 46/46、`test_major_event_alert.py` 30/30、`test_scan_edge_metrics.py` 71/71）。
- **prod 只读探针**：三条 SQL（首查 / AI 增强后重查 / `_fetch_signal_row`）在真库**均可执行、字段齐备（缺失=无）**；用真行渲染审计原例（`signal=1085989 XRP score=86`）实测 `ratio=1.75`（与 AI 的 1.71x 吻合）、`liq=$1,913,193.87 chain=solana src=geckoterminal`，渲染结果含「链上池流动性（solana）」且**不含「0.00x」**；10/11 项展示核验通过（D9 因该例被否决闸门抑制属**预期**）。

**未修（本轮范围外，非通知层可解）**

- **P2-D11 信息源脆弱 + 实体链接弱**：属**上游架构**（源权重、实体链接/去重），通知层只能消费既有 `source_code`，无法修。
- **P2-D10 赛道分类本身**（XRP 被 CMC 标 `primary_sector='l1'`，`categories` 含 `Smart Contract Platform`/`Layer 1 (L1)`/`FTX Holdings`/`a16z Portfolio`；`launch_date=None`）：属 **CMC 分类数据治理**，需维护 override 表，本轮只做「标注来源 + 未收录」的展示层兜底。

**待部署**：需 Zeabur **显式 redeploy**（渲染层与 prompt 均在代码内，同 `4307fb1` / `81f281c` / `c000fec`）。

### 加密大盘早报 2026-09-24 审计处置（P1-1/P1-2 + P2-A~I，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_加密大盘早报_2026-09-24.md`。F3 运行态四核对全绿（P0/P1 系列已部署生效）；本轮修 **2 项确定性红线 + 7 项可读性**。**零 DDL、零迁移、不改判定口径**。

- **🔴 P1-1（ETF 卡片算术不自洽，已修 `send_daily_brief.py`）**：卡片第三列原为「合计净流入」= **全部资产**加总（含 SOL/XRP/…），而只展示 BTC/ETH ⇒ 读者按可见两项相加得 486，与展示的 609 对不上（只读 prod 实测：BTC +582.4M / ETH −95.5M / 其他 +122.0M（SOL 93.1 + XRP 17.0 + LINK 4.6 + HYPE 3.6 + SUI 1.7 + AVAX 1.1 + DOGE 0.6 + HBAR 0.5 + LTC −0.2）= 合计 608.9M）。改法：第三列改标 **「全部 ETF 合计」** + 卡片脚注 **「分项：BTC +$582M + ETH -$96M + 其他 +$122M（SOL +93M · XRP +17M · LINK +5M）」**，使算术自洽可核验；`_fmt_flow` 负数由 `$-96M` 改为 `-$96M`（符号前置）。
  - **BTC 分项偏差（582 vs 外部 712.62）不修**：只读 prod 逐日核对 `biz.etf_flow_daily`（BTC 09-22 单日 +714.7M，与审计引用的「9-22 单日 $715M」**精确吻合**）⇒ 我方日频数据正确；审计的「外部 7 日累计 712.62M」与**单日值**几乎相同，属**外部口径疑似单日/窗口错标**，非本系统可校准项（窗口 `latest_date - 7d` 含 8 个日历日但仅 6 个交易日，改窗口只会更偏离外部值）。
- **🔴 P1-2（XPL「占流通」同封打架，已修 `macro_market.fetch_upcoming_unlocks`）**：卡片/顶部预警走「实时计算」（`unlock_amount / core.asset.circulating_supply` = 64.80%，因该资产 `circulating_supply` 滞后），而「宏观&代币事件」栏走源 `unlock_ratio_mcap`（63.20%，= CMC 权威）。二者**数学同源**（`unlock_value/mcap = unlock_amount/circulating`，价格约去）⇒ 回退链改为 **源流通占比 → 源市值占比 → 实时计算**，并标记 `source`（不加 `~`）。只读 prod 复算：XPL 63.2 / 2Z 46.4 / SOSO 50.6（55/58 行走源市值占比，3 行走实时计算）。
- **🟠 P2-A（方向与盘面张力）**：AI 定调含「多」且当日 BTC/ETH/总市值中 **≥2 项 ≤ −1%** 时，方向行下加「⚠️ 方向偏多属结构性判断；当日 BTC/ETH/总市值同步回调，勿与当日走势混读」。
- **🟠 P2-B（两胜率打架）**：`_load_alert_quality` 增取 `roll3_win_1h/roll3_be_1h/roll3_pf_1h`；渲染拆「**当日** T+1h 胜率 X（平衡线 Y）· **近3日滚动** R（平衡线 S，PF T）」并加口径说明「失配判定以滚动口径为准；单日波动大，不宜据此判断阈值优劣」。
- **🟠 P2-C（脉搏无时点）**：`_build_tldr` 增 `data_as_of`（取 overview 快照 `fetched_at` Unix 秒）；渲染层新增 `_fmt_data_as_of()` 转北京时间，脉搏标题右侧显示「· 数据截至 MM-DD HH:MM（北京时间）」。
- **🟠 P2-D（截断无省略号）**：新增 `_clip()`，精选信号 `reason`（120）与驱动因子（30）超长时补「…」并去尾空白。
- **🟠 P2-E（高危口径不统一）**：综合高危标「今日高危信号（综合风险）」，Meme 专项标「Meme 风险（Meme 专项）」。
- **🟡 P2-I（措辞小悖）**：「即将解锁（未来14天）」→「（未来14天，含今日）」。
- **未修**：P2-F（`CRCLon` 命名，属数据源符号）、P2-G/P2-H（SOSO/2Z 占比、AI 赛道 +122.2% 待核，无外部源）。
- **自测**：新增 `workbench/test_daily_brief_20260924.py` **29/29**（ETF 合计自洽 + 源口径源码守卫 + 两胜率分列 + 时点 + 截断 + 维度标注 + `_clip`/`_fmt_data_as_of` + 边界不误触发）；`test_daily_brief_p1.py` 22/22、`test_liq_overview_brief.py` 49/49、`test_brief_data_model.py` 20/20 无回归；`py_compile` 2/2。
- **真快照端到端**：`load_snapshot(2026-09-24)` + `render_brief_html` 实测输出「全部 ETF 合计 +$609M」「分项：BTC +$582M + ETH -$96M + 其他 +$122M（SOL +93M · XRP +17M · LINK +5M）」「当日 T+1h 胜率 59.1% / 近3日滚动 44.5%」「数据截至 09-24 08:30（北京时间）」。
- **待部署**：早报由**容器内 scheduler 的子进程**执行（`scheduler.py` 用 `[sys.executable, "-u", script_path]` 跑脚本，脚本文件在镜像内）⇒ 代码改动**需容器 redeploy** 才生效。**更正**：此前本节误写「下次调度即生效、无需常驻容器重启」——`复验_早报0924审计修复_b4dac09_2026-09-24.md` 的 F3 活口指出该说法不成立（与项目「push ≠ 线上生效」的一贯口径一致）。
- **复验收口（`复验_早报0924审计修复_b4dac09_2026-09-24.md`，本次提交）**：独立三段式（拉 origin/main 真码 + `py_compile` + 抽码离线实跑）确认 —— `b4dac09` 在 origin/main、P1-1/P1-2/P2-A~I 与说明逐字吻合、并发提交未触碰本轮函数区、新增 29/29 与既有 22/16/20/49 四套护栏零回归。**唯一活口 = F3 部署生效**（已按上条更正）。另核 `subprocess.run` 执行模型确认「需 redeploy」。
  - **P2-F 非缺陷（已核实）**：`CRCLon` 是**真实代币符号** —— `core.asset` asset_id 8598 = `Circle Internet Group Tokenized Stock (Ondo)`（Ondo 代币化股票，`primary_sector='rwa'`，市值 $107.9M / rank 244）。审计「疑似渲染错误或缩写」的前提不成立，**不改**。
  - **P2-G 已随 P1-2 消解（已核实）**：`biz.asset_unlock_event` 的 SOSO/2Z 亦走源 `unlock_ratio_mcap`（50.6 / 46.4），与 XPL 同源口径；审计列的「50.73% / 46.09%」是修复前「实时计算」口径的旧值，**无需再核外部**。
  - **P2-H 非缓存 bug（已核实）**：`biz.sector_flow_daily`（sector_12）AI & Big Data 的 `mcap_change_7d_pct` **每日重算**（实测 09-18 8.07% → 09-19 9.36% → 09-21 25.04% → 09-22 54.02% → **09-23 122.22%**，逐日不同）。09-24 早报读到的是 **09-23 的 metric_date**（当日 ETL 尚未落库，MAX(metric_date)=09-23）⇒ 两封早报同值属 **ETL 时点**，且卡片已用「功能分类{metric_date}」披露数据日期。**不改**。

### 轧空标定判据「跨度需求 + 聚合方式」（工单 SQUEEZE-SPAN-001，2026-09-24，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\工单_轧空标定跨度需求与holdout_2026-09-24.md`。**只读标定工具改造，零 DDL、阈值一律不动**（`LONG_LIQ_RATIO_THR` / `SQZ_SHORT_LIQ_RATIO_MIN` 未触碰）。

- **工单核心结论（实证）**：判据卡点是**时间跨度**不是样本量——方差分解 **时间解释 98.7% 方差、抽样仅 1.3%**；**聚合悖论**（段层面 9/15 段 PASS、中位 17.58%，合并上界 31.47% = FAIL）；**单段杠杆**（一个 4h 段即可把合并结论 FAIL→PASS）；**极端段不是少数币顶的**（HHI 0.0068、剔除 top20 仅 −2.27pp ⇒ 按币剔除治不了）。⇒ 「维持影子模式」是当前唯一诚实处置，但须补明确退出条件。
- **改动（`calib_squeeze_liq_thr.py`，§6 A/B/C/D/E 全落）**：
  - **A（P1）判据改段层面稳健统计**：`segment_judge(segs, line)` 以各 4h 段上界的**中位数**为判据输入（旧为合并上界），`decisive` = 段 IQR 不跨线（P75<线 或 P25>线）。合并上界/CI/分段 min-max 跨线**降级为 `*_raw` 诊断**。实测同一数据：判据输入 **19.91%**（PASS 侧）vs 合并 **47.27%**（FAIL 侧）——聚合悖论现形。
  - **B（P1）极端段显式列出**：`EXTREME_SEG_PCT=40`，段上界 ≥ 该值者进 `judge.extreme_segments` + 文本渲染（不并入判据，但不得静默丢弃）。
  - **C（P1）跨度充分性前置门**：`span_sufficiency(span_hours, extreme_count)`——表跨度 < `MIN_SPAN_DAYS=30` 或极端段 < `MIN_EXTREME_SEGMENTS=3` ⇒ 并入 `sample_ok` ⇒ rc=3，**不打印**判据输入比率（避免被误读成「判据不通过」）。
  - **D（P2）**：模块 docstring 写明跨度量级是「**量级估计、非承诺**」（20~60 天来自稀释/频率两条独立路径，泊松 CI 极宽）。
  - **E（P2）**：段上界输出**前移到前置门之前**（任何路径都能看到，它是聚合悖论的唯一诊断入口）。
- **自测**：新增 `workbench/test_squeeze_span_judge.py` **25/25**（A 注入「5×10%+1×70%」→中位 10%、「全部 22%」→22%、IQR 跨线边界；B 极端段清单；C 四象限；D/E 源码守卫；**C 端到端桩接 DB**：分母/分子合格、跨度不足 ⇒ rc=3 且**未打印**判据输入比率、段上界仍打印）；既有 `test_squeeze_battle.py` 143/143（含全部 calib 源码守卫）、`test_squeeze_fuel.py` 99/99、`test_squeeze_alert_silence.py` 18/18 无回归；workbench 全量 `test_*.py` 零失败；`py_compile` 通过。
- **prod 只读实测（`--days 1`）**：跨度 72.1h / 6 段（中位 19.89% / max 73.50%）/ 极端段 1 个 @ 09-23 14:15 ⇒ 跨度门拦下 rc=3，段上界照常打印、无判定比率。
- **待拍板（未做，遵工单 §9）**：① holdout 现在不执行（跨度不足，记入待办，条件触发 span≥30 天）；② §4.2 四维 regime 分层未实测（仅给方案）；③ 「极端段是否混杂采集面变化」列为下一轮 P1；④ 影子模式维持。
- **复验收口（`复验_轧空标定SQUEEZE-SPAN-001_867f529_2026-09-24.md`，本次提交）**：复验确认 A/B/C/D/E 主体如实落地、单测 25/25、无回归、阈值未动（`calib` 无调度引用 ⇒ 无「推了没部署」问题），另开 F1~F6：
  - **🔴 F1（P1，已修）`rc=0 PASS` 可与「无判别力」并存**：`judge_pass` 误删 `decisive` 合取项 + `exit_code` 的 `pass_` 早返回排在 `decisive` 之前 ⇒ 「段 IQR 跨判据线」时仍判 rc=0，且 `judge.decisive` 假报 True。**当下不可达（跨度门先拦），但跨度门一放行即自动激活**（同族第 6 次复发，方向相反：主判据漏了合取项）。修法：`judge_pass` 恢复 `decisive`；`exit_code` 把 `if not decisive: return 4` 提到 `pass_` 之前；渲染层改用**统计事实**字段 `segment_iqr_straddle_raw`（不再借用码表派生的 `decisive`）。
  - **🟠 F2（P2，已修，取甲）段清单印 `中位=` 摘要行 = 判据输入泄漏**：A 让「中位数」同时是「段清单摘要」与「判据输入」⇒ 摘要行在 `🛑 拒绝出结论` 之前把判据输入印出来，与 §6-C「不打印判据输入」冲突。取**甲**：段清单只印逐段值、不印 min/中位/max 摘要（摘要改由【统计判别力】节在样本可用时给出）；单测断言由字面串改为**数值断言**（rc=3 路径不得出现段中位数值）。
  - **🟠 F3（P2，口径更正，未改码）A 只改聚合方式、不改可分性**：段中位在 1 小时内即可跨 20% 线（19.91%↔22.88%），`--days 1` 仅 6 段、IQR 由线性插值自 6 点得出 ⇒ 「段中位 PASS 侧」**不是稳定读数**；判据仍不可判，影子模式理由进一步强化。已写进 docstring。
  - **🟡 F4（P3，已修）rc=3 归因不唯一**：新增 `primary_gate()`（优先级 分母>分子>跨度），拒绝路径标注唯一充分因（实测默认参数「归因以分母门为准」、1e7「归因以跨度门为准」）。
  - **🟡 F5（P3，已修）字段语义漂移未改名**：`upper_bound_n`→`upper_bound_n_segments`、`ci_distance_pp`→`merged_ci_distance_pp`（旧名保留一轮为别名，无消费方）。
  - **⚪ F6（P3，已修）死变量** `new_rates` 删除。
  - **自测**：`test_squeeze_span_judge.py` 25→**42/42**（+F1 `exit_code` 契约 5 例 + **F1 端到端注入**（样本可用+IQR 跨线 ⇒ rc=4，旧码会误判 0）+ F4 `primary_gate` + F2 数值断言 + F5/F6 源码守卫）；workbench 全量 `test_*.py` 零失败；prod 只读实测 rc=3（默认「归因以分母门为准」、1e7「归因以跨度门为准」），段清单不再印摘要、无判定比率。
- **复验收口·二轮（`复验_轧空标定F1-F6处置_91d3162_2026-09-24.md`，本次提交）**：复验以**独立注入**（不复用我方单测）确认 F1 端到端封死（旧码 rc=0 / 新码 rc=4，同一输入）、F2/F3/F5/F6 如实落地、无需部署（`calib` 无调度引用），另开 G1~G4：
  - **🔴 G1（P2，新引入，prod 可达，已修）归因行用「理由条数」当「失败门数」**：`len(fails) > 1` 在「只有跨度门不过」时也成立（跨度门自带 2 条理由：跨度 + 极端段）⇒ 印出「其余门亦不过」这一与事实相反的句子（prod `--vol-win-min 1e7` 必然命中：分母 1.74% / 半格 0.58% 均达标）。修法：改用 `_failed_gates = sum(1 for ok in (denom_ok, molecule_ok, span_ok) if not ok)`，文案「另有 N 道门亦不过」。实测：默认（分母+跨度）印「另有 1 道门」、1e7（仅跨度）**不印归因行**。
  - **🟠 G2（P2，已修）测试7 选错样本，无回归保护力**：原用 FAIL 侧 bounds（段中位 45）⇒ 旧码 `judge_pass=(45<20)=False` ⇒ rc=4，新旧同值 ⇒ 假阳性通过。改用 **PASS 侧** `[5,5,5,5,19,45,45,45]`（段中位 12%<20、P25=5/P75=45 跨线、极端段 3）⇒ 旧码 rc=0 / 新码 rc=4，才构成有效断言。
  - **🟡 G3（P3，已修）测试7 非 hermetic**：只桩了 `psycopg.connect`、未桩 `get_settings`（测试6 的 finally 已还原真函数）⇒ 无 env 机器上崩 `Missing required environment variable: CMC_API_KEY`。补桩后**无 env 下 45/45**。
  - **🟡 G4（P3，选甲，已知残留）C 与 §6-E 的固有冲突**：去摘要行后逐段值仍可**手算**中位 ⇒ C「不打印判据输入」只字面成立。取**甲**（保 §6-E 诊断入口，接受可手算）；乙（rc=3 连逐段值也不印）会废掉聚合悖论的唯一诊断入口。
  - **自测**：`test_squeeze_span_judge.py` 42→**45/45**（+G1 源码守卫 + G1 单门不印/多门印归因 + G2 换 PASS 侧样本 + G3 打桩 get_settings）；workbench 全量 `test_*.py` 零失败；prod 只读实测 G1 文案正确（默认「另有 1 道门」、1e7 不印）。
- **复验收口·三轮（`复验_轧空标定G1-G4处置_f0eac95_2026-09-24.md`，本次提交）**：复验以**三版本注入**（缺陷源 `867f529` / 已修 `91d3162` / 现码 `f0eac95`）确认 G1~G4 全部如实落地、无新增 P1/P2、阈值未动，判「可收口」；另开 H1~H3（均 P3）：
  - **🟠 H1（P3，本轮自引入，已修）测试7 断言耦合渲染格式双空格**：`"→  PASS" not in _out2` 若渲染改成单空格即**静默恒真**、失去保护力。改用正则 `re.search(r"→\s*PASS", _out2)`，并加**非空转**断言（正则命中 PASS、不命中 INCONCLUSIVE）。
  - **🟠 H2（P3，已修）源码守卫对空白敏感**：`"len(fails) > 1" not in _src` 遇到 `len(fails)>1`（无空格）会**放行**。改用正则 `re.search(r"len\(fails\)\s*>\s*1", _src)`（实测两种写法均命中）。
  - **🟡 H3（P3，非缺陷，未改）单门不过时不印归因行**：G1 后仅单门失败不印归因（旧码此处碰巧印对首句）；与 F4 原意「三处**并发**失败时标出唯一充分因」不冲突（单门时拒绝行已列明理由，归因无歧义）⇒ 保持现状。
  - **📌 方法论教训（复验方自记，值得固化）**：验「回归测试是否有效」时，对照基准必须是**引入缺陷的那个 sha**（此处 `867f529`），不能选「紧邻前任」（`91d3162` 已修好 F1，当然也 rc=4）——否则会误判「新样本无保护力」。
  - **自测**：`test_squeeze_span_judge.py` 45→**46/46**；真树全量套件零失败。**计数更正（复验 I5）**：真实 `test_*.py` 数为 **26**（`848d7e1` 真树 `git ls-tree`），此前「20/21」是 **stale 子树的文件集过期**（非内容过期）—— 双方均须以 `git ls-tree` 为准（本机当前 28）。
  - **⚠️ 归因纪律更正（复验 item 4）**：`test_scan_edge_metrics.py` 的那次失败**不能**归为「并发 WIP」—— 该测试 DB 层**全为 SELECT**，依赖 `collect_scan_outcome`/`build_scan_edge_report`（blob = origin/main，**不在工作树 WIP 的导入图**），三场景（干净树/工作树/离线）实测均 **71/0/0**。更可能是 **prod 库跨表一致性不变量瞬时不过**（结算/聚合写入窗口）⇒ **遇该测试失败先看具体 ✗ 条目再归因，勿预设 WIP**。
  - **仍未核（复验 §9/§10 唯一实质开放项，下一轮）**：「极端段是否混杂 09-22 回填 / 09-23 采集面变化致 `vol_win` 少算」。
- **复验收口·四轮（`复验_轧空标定H1-H2处置_848d7e1_2026-09-24.md`，本次提交）**：复验确认 H1/H2 方向正确、无新增 P1/P2、可收口（calib blob 与上轮逐字节相同）；另开 I1~I5（全 P3）：
  - **🟠 I2（P3，已修）同类脆弱负向守卫仍有 1 处 + 正则未覆盖括号内空白**：F2 守卫原为单引号字面 `"sg['median_pct']" not in _src`（写 `sg["median_pct"]` 双引号即**放行**）⇒ 改正则 `sg\[[\"']median_pct[\"']\]` **并**保留 `中位\s*=\s*\{sg`（改名 `sg['p50']` 仍被后者捕获）；H2 正则加宽为 `len\(\s*fails\s*\)\s*>\s*1`（覆盖 `len( fails ) > 1`）。
  - **🟡 I4（P3，已修）H1/H2 自检不对称**：补 H2 **非空转**自检（无空格 / 带内空白两种写法均命中），与 H1 对称。
  - **🟡 I3（P3，已修）H1 自检演示不完整**：自检样本由双空格改**单空格**（H1 的真正失效模式），演示更有针对性。
  - **🔵 I1（P3，口径更正，未改码）H1/H2 属卫生性/纵深防御、非新增检测力**：端到端**突变体实证**——抓 F1/G1 回退的是**行为断言**（测试6 端到端跑 `main()` 看输出），源码守卫**不承重** ⇒ **后续新增守卫应优先补端到端行为断言，源码守卫只作辅助**。
  - **自测**：`test_squeeze_span_judge.py` 46→**47/47**；真树全量套件零失败（本机 28 个）。
- **复验收口·五轮（`复验_轧空标定I2-I4处置_34af4b1_2026-09-24.md`，本次提交）**：复验以**突变体对照**确认 I2（增量恰 2 处：双引号变体 / 括号内空白变体）、I3、I4 如实落地、无新增 P1/P2；另开 I6~I8（全 P3）：
  - **🟠 I6（P3，已修，取「补行为断言」）F2 守卫残留边界**：源码守卫（变量名含 `median_pct` 或标签 `中位={sg`）两者**同时**失效即可绕过（标签改英文 + 变量改名）。按 I1 结论**不再加固字面**，改为在测试6 补**端到端行为断言**（rc=3 路径输出不含 `中位=X%`）。
  - **🟡 I7（P3，已修）自检固有盲区**：守卫与自检原各自硬编码同一正则 ⇒ 「守卫改坏、自检未同步」不可发现。改为**共用模块级 `re.compile` 常量**（`_RE_LEN_FAILS`/`_RE_CONCL_PASS`/`_RE_SUMMARY_MEDIAN`），常量坏则自检立刻红。
  - **🔵 I8（P3，已修）AGENTS 套件计数口径**：补记「口径③ = workbench 根目录」；`34af4b1` 自身 = **27**（此前「26（848d7e1）/28（本机）」未含该 sha 自身）。
  - **⚠️ 并发 WIP 预判（本轮观察，非本提交内容）**：并发进程正在 `calib_squeeze_liq_thr.py` 上加「口径 B」（新增 `SQL_B_4H`/`cross_exchange_lower_bound`/`rolling_scale_groups`，并把 `primary_gate`/`_failed_gates` 扩为**第 4 道门** `b_gate`）。本轮已把两处**存在性守卫**改宽匹配（允许新增门）；但**测试8 的 e2e 桩未覆盖新 SQL** ⇒ 其 WIP 落地时需同步更新测试8 桩（否则 `KeyError: None`）。
  - **自测**：`test_squeeze_span_judge.py` 47→**48/48**；因工作树被并发 WIP 污染，本轮对**已提交 calib**（blob `fa3cb22e`）做**隔离运行**验证（把已提交 calib + 测试放入临时目录、`PYTHONPATH` 提供真实 src）。
- **🔴 唯一实质开放项已核（复验 §7 三步法，只读；结论：混杂可排除）**：「极端段是否混杂 09-22 回填 / 09-23 采集面变化致 `vol_win` 少算」。
  - **关键结构事实**：`vol_win = SUM(quote_vol)` 是**币级常量**（整窗口按 symbol 聚合）⇒ 少算只会让「进入该段的那些币」虚高，**不会**造成「某段整体虚高」⇒ 验证对象 = **各段入样币集的 `asset_klines` 覆盖率结构**（而非段的时序）。
  - **实测（`--days 1` / 5e6，2026-09-24，只读）**：整体分母覆盖中位 **0.997** / P10 0.997；**极端段(2)** 覆盖中位 **0.997**、`<0.9` 占比均值 **3.35%**；**非极端段(4)** 覆盖中位 **0.997**、`<0.9` 占比均值 **3.66%** ⇒ **Δ覆盖中位 +0.00pp、Δ<0.9占比 −0.31pp**。
  - **判读**（复验 §7.2：|Δ覆盖中位|>10pp 混杂成立 / ≤5pp 可排除）：**混杂可排除** ⇒ 极端段是**真实市场 regime**（全市场多头集体爆仓），非采集面假象 ⇒ 工单 §0「聚合悖论」的极端段前提成立。
- **复验收口·六轮（`复验_轧空标定I6-I8处置_a56ec6f_2026-09-24.md`，本次提交）**：复验确认 I6/I7/I8 落地、无新增 P1/P2、开放项核销成立（且给出比自报更硬的**反事实因果检验**：剔除低覆盖币 / 分母归一后极端段上界仍 76.57% / 77.02%，波动 <0.5pp ⇒ 混杂**决定性**排除）；另开 I9/I10（全 P3）：
  - **🟠 I9（P3，已修，取乙）`_RE_SUMMARY_MEDIAN` 无非空转自检 + 锚渲染字面**：常量退化后 `not …search(_out)` **真空为真**（突变体 T-a/T-c 双证）；且锚的是渲染标签 `中位=`，将来出现合法的 `中位=98.6%` 行会**假红**。取**乙**：删除该常量，改为锚**数值** —— 真值由脚本自身 `segment_upper_bounds`/`segment_judge` 算出（不硬编码 15.00、不耦合排版），并加非空转自检（真值 == 15.00%）。真正承重的本就是数值断言（T-c2 证：停用源码守卫 + 退化常量仍报红）。
  - **🟡 I10（P3，记档不修）I7 的边界**：共享常量只覆盖「常量坏」，不覆盖「守卫**引用脱离**常量」（T-d：改内联字面后自检仍绿）。属「守卫/自检分离」的固有性质，**记档不修**（纵深防御的理论完备性，实务风险低）。
  - **📌 同文件并发碰撞（本轮实况，务必知悉）**：并发进程在 `calib_squeeze_liq_thr.py` + `test_squeeze_span_judge.py` 上加「口径 B」（新增 `SQL_B_4H`/`cross_exchange_lower_bound`/`rolling_scale_groups` + 第 4 道门 `b_gate` + 对应测试），并**把本单未提交的 I9 改动一并提交进 `5a95b32`**（`feat(coinglass)` 提交）。⇒ 本单**只提交 AGENTS.md**；`test_squeeze_span_judge.py` 已由对方提交、现含对方的口径 B 测试（依赖 `calib.MIN_CROSS_PAIRS`）。**教训再次印证**：同文件并发必须 `git stash push -- <path>` 或尽快提交，否则改动会被对方提交「顺带」带走。
  - **自测**：`test_squeeze_span_judge.py` **48/48**（对 HEAD `5a95b32` 直接运行，工作树已干净）；I9 的数值断言与 I6 语义等价、且去排版耦合。
  - **套件计数口径（I8 收口）**：一律记「**口径③ = workbench 根目录 `test_*.py`** + 该 sha 自身数值」，且**只数已提交（`git ls-tree`）、勿把工作树未跟踪文件计入**；数值随提交推进（`848d7e1`=26 / `34af4b1`=27 / `a56ec6f`=29 / `5a95b32`=**30** / `0c0c567`=**31**），**勿复用旧值**。
  - **📌 复验口径更正（`复验_轧空标定I9-I10处置_a89ac49`，本次提交）**：上一轮把 `5a95b32` 记成 31 是**把工作树未跟踪的 `test_scan_alert_onchain_addr.py` 也数了进去**（该文件当时 `git status` 为 `??`，后由 `0c0c567` 正式提交）⇒ 已按 sha 更正为 30；**「只数已提交」已写入上条口径**。
  - **📌 验收纪律（复验 §4.3 教训，本次提交）**：`5a95b32` 自报「48/48 + 39/39 + 99/99 + 143/143」**全来自轧空家族**，而它同时改了 `scan_daemon.py`（+94/−6）⇒ **两个 scan_alert 套件（audit_deepdive 74/1、header_regime 132/3）被漏过**（根因：`legend` 串写进 markdown `**最大一笔**`，落进邮件 HTML）。已由 `0c0c567` 修（1 行：`**` → 「」），实测三套件全绿。⇒ **纪律：自报验收必须覆盖「改动所触及的全部套件」（按导入图/文件依赖），不能只跑改动主题的邻近套件。**

### 投研页 unlock_pct_30d 恒 0.0 修复（审计_投研页机会挖掘_8680_TAKE_2026-09-24.md，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_投研页机会挖掘_8680_TAKE_2026-09-24.md` 的 **P1（系统性代码 bug）**。资产 8680=OVERTAKE(TAKE)，线上 `/api/research/8680/notebook` 的 `unlock.unlock_pct_30d` 恒显 **0.0**，而 prod `biz.asset_unlock_pressure` 真实值 = **5.60**（9/25 即解锁 5.6%）⇒ 直接误导「无解锁抛压」。**本次只修 P1**；P2（fallback 信号计数无溯源）/P3（cg 单映射 `is_primary=False`）按审计 §五 Scope 不扩，另立。

- **根因（物证级）**：`_build_structured_metrics_inner`（[db_stats.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/db_stats.py#L2878-L2887) 原 L2880）用 `datetime.fromisoformat(str(e["date"]))` 解析**人类可读日期**（`"Sep 25, 2026"` / `"25 Dec 2026"` / `"Sep 12, 2026Next"`）⇒ 必然抛 `ValueError` 且被 `except` 静默吞掉 ⇒ `pct_30d` 恒 0.0。`next_unlock_date`/`next_unlock_pct` 直取 `e["date"]`/`e["pct"]`、不经过日期解析，故只有 `unlock_pct_30d` 塌成 0。同文件 [L7812 `_parse_unlock_event_date`](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/db_stats.py#L7812) 已是正确的多格式解析器（被 `compute_unlock_pressure` 正确调用，故 pressure 表才是真值 5.60）——L2880 属「重复实现且更弱」的反模式。
- **修复（1 处 + 注释）**：L2880 改用 `_parse_unlock_event_date(e.get("date"))`；因该函数返回 `date`，阈值同步改 `(datetime.now(timezone.utc) + timedelta(days=30)).date()`（否则 `date <= datetime` 抛 TypeError）；`if ed and ed <= thirty_days`。
- **影响面（审计只读量化）**：notebook `unlock_pct_30d` 恒显 0.0 的资产 **450 个**（有 upcoming 人类日期事件），其中 pressure 表实际 >0 的 **183 个**（两者矛盾可独立验证）。
- **验收三通道**：① 真码 diff = 本次改 1 处；② **prod 只读端到端**（本次实测）：读 `biz.research_notebook`(8680) 的 `snapshot_json` 后调 `_build_structured_metrics_from_snapshot` ⇒ `{"upcoming_events_count":4, "next_unlock_date":"Sep 25, 2026", "next_unlock_pct":5.6, "unlock_pct_30d":5.6}`（**原 bug 恒 0.0**，与审计 §二.② 真值吻合）；③ 部署后 notebook 重算应显示 5.60。
- **自测**：新增 [test_unlock_pct_30d.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_unlock_pct_30d.py) **16/16**（纯离线：多格式解析 7 例 + 30 天内累加/边界 4 例 + 人类日期原例 1 例 + 真无 30 天事件保 0.0 1 例 + AST 源码护栏 3 例）；`py_compile` 通过。
- **待部署**：`db_stats.py` 在 web 应用进程内，需 redeploy 后 notebook 才生效（本地已修 + prod 只读已证修复有效）。
- **未做（本工单不扩）**：P2 fallback thesis 去掉/渲染真实信号计数（产品决策）；P3 cg 单映射置 primary（并入 W5-plus）；onchain 33 天陈旧（上游采集调度，另立）。

**复验闭环 + 残留项处置（复验_投研页P1解锁抛压修复_048ccc9_2026-09-24.md，本次提交）**：复验三路全过（真码 diff `048ccc9` → raw main 已是修复版 → 线上 8680 `0.0→5.6`，另抽 6 资产 8781/8888/7760/7331/8863/9655 全 >0），P1 判定闭环。逐项复核残留项后结论：**三项均无「应改而未改」的代码 bug，本轮仅归档、零 DDL、零 DML**。

- **🔵 P2 = 审计误判（false positive），非缺陷、无需改码**：审计据「notebook JSON 无 signals 明细 + `biz.scan_signal` 对 TAKE=0 行」判为「有断言、无证据」。实测 `detect_asset_signals(8680)` 真跑返回 **恰好 3 critical + 1 warning** —— `price_surge +61.47%`、`volume_surge 284.6x`、`oi_surge +72.56%`、`unlock_soon 5.6%`，与 fallback 文案「3 个高危信号；1 个警告信号」逐字吻合。且研究页有**独立「⚡ 异动信号」卡片**：[research.html](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/templates/research.html) L2367 容器 + L2682 `loadSignals()`（随 notebook 加载**无条件**调用）→ `GET /api/research/<id>/signals`（[app.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/app.py#L2473) L2473）→ `detect_asset_signals`，与 thesis 计数**同源同页可溯源**。根因是审计把 **`biz.scan_signal`（大盘扫描池，按 K线+OI 出信号）** 与 **`detect_asset_signals`（投研页实时 diff 检测器，吃 CMC/衍生品数据）** 混为一谈。
- **🟠 P3 = 真实但低危的治理项，维持 W5-plus、本轮不动 prod**（只读量化）：`core.asset_source_map` 的 cg 映射分桶 —— 单映射无 primary **8042** / 多映射无 primary **1617** / 有 primary 8102（总 17761）；其中「单映射 + `match_status='confirmed'` + 无 primary」**4541**。`resolve_cg_coin_id`（[cg_resolve.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/scripts/src/crypto_research/db/cg_resolve.py)）已做确定性择优、单映射天然唯一 ⇒ **解析路径无污染**；但 `db_stats.py` 若干覆盖度查询直接用 `asm.source_code='cg' AND asm.is_primary=TRUE`（如 L248）⇒ 这些资产的 cg 维度显示缺失，**属真实下游影响**。**不做批量 UPDATE** 的理由：W5 治理正由并发进程推进（`fe459e1` fix_062 / `0551daf` fix_063，明确「全库 456 个灰色地带…需人工确认，**勿盲目翻转**」），8042/4541 的量级远超其「无歧义子集」，盲目置 primary 会把错标映射一并「洗白」，且与进行中的治理冲突。
- **⚪ onchain 33 天陈旧**：上游采集调度问题，另立，未动。
- **验收**：`py_compile` 无（本轮零改码）；结论均以 prod 只读探针物证支撑，探针脚本为临时产物、已清理。

### 催化剂 A 级邮件延迟链路诊断处置（XRP/BCH，catalyst 12267，2026-09-24，本次提交）

来源：`诊断_催化剂A级邮件延迟链路_XRP_BCH_2026-09-24.md`。诊断结论为**假说**（「2.5h = 回归恢复积压冲刷」）并留「待确认」。只读 prod 复核后：**假说被推翻/细化**，另发现一个可修的**跨通道重复通知**缺陷。**本轮零 DDL、不改 tier/方向判定口径**。

- **延迟真因（物证级，非积压冲刷）**：A 级档位由 **d6 方向闸门**（`signal.py:230`，只有显式 bullish 保留 A/B）决定，方向取 `COALESCE(catalyst_impact.impact_direction, ai_sentiment)`；而 `catalyst_impact` **只由 `build_catalyst_impact.py` 生成，该步仅在 `catalyst_run_all`（每 12h）内**（`catalyst_run_all.py:31`）。fast daemon 的 `run_fast_once` 只跑 classify/regime/grade/resonance/signal，**不生成 impact**。catalyst 12267 实测链：新闻 14:17:50 → `ai_processed_at` 16:00:36 → `catalyst_impact` 16:05:15（bullish）→ A 邮件 16:30/16:50。⇒ **XRP 在创建时（14:20:45）并非 tier A**（方向缺失被 d6 封顶 C），诊断「tier='A' 于 22:20 就生成」的前提不成立；A 级邮件延迟的上界 = `catalyst_run_all` 的 12h 节拍。
  - **按诊断自身判据（「信号诞生→发送 > 30min 且无回归背景才算缺陷」）**：A 信号诞生于 16:05（impact 落库），发送于 16:30/16:50，间隔 25~45min ⇒ 属边界；**不改方向生成路径**（把方向改成规则即时产出属设计变更，会改变所有新催化剂的方向来源，且 d4 校准样本仅 40 条，见 AGENTS「待办（需设计变更，勿盲目改）」）——本轮只记录。
- **🔴 跨通道重复通知（本轮修复）**：快讯与慢通道 digest **都发 A 级 Alert，但各用独立去重**（快讯按 `(signal_id, fast_alert)`；digest 按类别 sentinel）⇒ 同一信号两封邮件。实测 XRP `1085989`：`log_id=4` slow_digest（16:30:01，主题「🎯 催化剂 Alert·加密货币·A级 2 条」，并回写 `notified_at`）+ `log_id=79` fast_alert（16:50:55，回写 `pre_alert_sent_at`）。
  - **修法（`workbench/catalyst/notifier.py`）**：`notified_at` 仅由 digest 发送成功后写、`pre_alert_sent_at` 仅由快讯发送成功后写 ⇒ 二者互为「另一通道已覆盖」的判据，**双向合围**：① 快讯侧新增 `_slow_digest_sent_recently(conn, ids)`（查 `notified_at > NOW()-24h`），在**取锁之前**跳过；② digest 侧 `_recent_new_a_signals` 的 WHERE 增 `pre_alert_sent_at` 近窗口排除。同一信号 24h 内只发一封。查询失败按「未发送」处理（宁可多发一封也不静默漏发）。
  - **为什么诊断没发现**：诊断把 `notified_at` 读作「信号被标记通知」，未意识到它是**另一封邮件**（digest）的发送留痕。
- **D11（错链）现状（未改，已在通知层缓解）**：catalyst 12267 正文只提 BCH/UNI/BTC，**XRP 完全未出现**，却被 `link_source='trading_pairs'`（币安广场帖的 `tradingPairsV2` 标签，`kol/scraper.py:378`）链上并产出 A 信号。XRP 的 `ai_deep_review` 实测 `verdict='不建议参与'` + `asset_match_confidence='low'` ⇒ **AI 否决闸门（`4307fb1`）已能拦下这封**（该闸门 2026-09-24 08:33 才提交，晚于本封邮件 00:50）。采集层实体消歧 / 源权威性评分仍属**架构改动、另立工单**（诊断 §五已标「待拍板」）。
- **自测**：新增 `workbench/test_catalyst_channel_dedup.py` **22/22**（`_slow_digest_sent_recently` 行为 8 例含异常兜底/空入参不查库 + `_recent_new_a_signals` SQL 与参数顺序 5 例 + 快讯侧结构位次 5 例 + 既有不变量不回归 4 例）；`test_fast_alert_ai_veto.py` 46/46、`test_major_event_alert.py` 30/30、`test_fast_alert_audit_rest.py` 61/61 无回归；`py_compile` 通过。
- **待部署**：`notifier.py` 在 fast daemon 与慢通道脚本内，需重启相应进程（容器 `catalyst_fast_daemon` + scheduler 慢通道）后生效。

### 投研页「数据完整度」徽章 OPT-UI-001（工单_投研页数据完整度徽章_OPT-UI-001_2026-09-24.md，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\待修复工单_投研页数据完整度徽章_OPT-UI-001_2026-09-24.md`。**纯前端、零 DDL、零后端改动、零外部请求**（用户约束：Coinglass 仅 Hobbyist、其余免费额度 ⇒ 展示层不得新增配额消耗）。

- **工单前提修正（如实记录）**：工单称「页面没有把 `missing_json` 渲染出来」**不完全成立** —— `research.html::renderSidebar()` 早已把 `d.missing` 渲染为侧栏「投研资料完整性」列表（每项带绿/红点 + 已收集(N份)/缺失）。真正的缺口是**缺少顶层汇总徽章**与**缺失项措辞不够醒**（原文「缺失」易被读成「值为零」）。故本轮只补增量。
- **改动（`templates/research.html`，+39 行）**：① 头部 `r-actions` 增 `<span id="r-completeness">` 容器 + `.r-completeness` 样式；② 新增 `renderCompleteness(d)`（纯计算，无 fetch/XHR）：读 `d.missing`，`present` 计数得 `数据完整度 X/Y`，缺失 >0 时颜色随完整率（≥60% 琥珀 / <60% 红），`title` 列「暂无数据（N 项）：…」并可点击滚动定位侧栏；③ `render(d)` 接线调用；④ 侧栏缺失项文案由「缺失」改为「**⚠ 暂无数据**」，与徽章口径统一。
- **口径**：`missing` 即后端 `_compute_missing_materials` 产物（含 `social_heat`/`team_vc`/`roadmap`/`audit_report`/`tge_ido_info` 等 13 类），已按赛道过滤；`present:false` 显式区分「无数据」与「值为零」。**未改任何数据计算逻辑**，仅消费既有字段。
- **自测**：新增 [test_research_completeness_badge.py](file:///e:/瞎搞乱搞/web3/加密货币研究报告/05_代码与脚本/workbench/test_research_completeness_badge.py) **14/14**（容器/样式、`render(d)` 接线、口径 `m.present`、**零配额护栏（函数体内无 `fetch(`/`XMLHttpRequest`）**、侧栏文案、后端 `missing` 返回体）；另用 node 对整段 `<script>`（Jinja 占位符替换后）做 `--check` **JS 语法通过**。
- **待部署**：模板在 web 应用进程内，需 redeploy 后生效。
- **未做（遵工单 §三待拍板）**：D1 未采用弹层方案（取头部徽章，最省代码）；D2「关键维度（derivatives/klines）更强提示」未做（这些维度不在 `missing`（资料清单）而在 `structured_metrics`，属另一数据源，另议）。

### 高亮信号邮件审计处置 M1/M3/M4/M6（审计_高亮信号邮件_2026-09-24，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_高亮信号邮件_2026-09-24.md`。审计先确认标题自证 / 字段完整性 / D2·D4·A2·B2 防线**全部通过**，再列 M1~M6 + 产品洞察。本轮修 **M1/M3/M4/M6**（**零 DDL、零迁移、不改 tier 计算与 AI 评审规则**）；**M2（HIGH 泛滥 + 90% turnover）与 M5 另论**（见末条）。

- **🔴 M1（P1）事件强度跨类型不可比（已修 `macro_market._event_strength_score`）**：`flow_pct`（链 TVL）用 ×4、`mcap_pct`（叙事市值）用 ×3 ⇒ 同一百分比数值在链信号上系统性偏高，且**排序倒置**（同快照 Base 链 +11.9%→90 排在 ZeroKnowledge +11.9%→85 之前；`conv = 0.6×六轴 + 0.4×es` 把 es 失真传染给统一排序）。修为**单一曲线**（常量 `_PCT_ES_SLOPE=3.0` / `_PCT_ES_CAP=40`，两 kind 共用），同值必同分；实测 +11.9% 两口径均 **85**、+11.6%<+11.9%（倒置消除）。
- **🟠 M3（P2）catalyst 类缺 event_strength（已修）**：`c38e755` 的 A1 只覆盖 whale/kol/narrative/chain/etf，**漏了催化剂**——而本封邮件 5 币里 3 币是纯催化剂，其确定性只由 conv 单数字承载。`_event_strength_score` 新增 `"score"` kind（0-100 直接夹取），催化剂**两条路径**（决策/回退）均补 `event_strength=_event_strength_score("score", cscore, t)`。**不改 conviction**——催化情绪已按权重计入六轴，再融合会重复计权。
- **🟠 M4（P2）AI 未背书仍以 HIGH 呈现（已修 `send_highlight_alert.render_card`）**：`_ai_downgraded=True` 的卡片前端早已不挂 🔥HIGH（`index.html` L13585），但**邮件渲染层不消费该标记** ⇒ 「AI 建议不参与 + 系统 HIGH」同封自相矛盾（实测 BURN：`dg=True` 却 tier=HIGH conv=70）。现 `render_card` 对 `_ai_downgraded` 的 HIGH 降级为 **MED** 展示（用 MED 配色），与前端口径一致；MED/LOW 不误伤。
- **🟠 M6（P2）单笔巨鲸独占 HIGH（已修）**：`kol_onchain` 单源封顶 `es≤45`，而 whale 不封顶。抽纯函数 `_whale_event_strength(usd_total, n_tx, t)`：**单笔（n_tx<2）封顶 45**（常量 `_WHALE_SINGLE_TX_ES_CAP`），多笔聚合仍走金额对数主轴。实测近 24h 鲸鱼榜仅 BURN/USDtb/XAUt 为 n_tx=1（正是目标），聚合项（WLD n_tx=3 等）不受影响。
- **⚪ M5（时区）非缺陷（已核实）**：审计基于 09:05 那封邮件（UTC），而「邮件时区统一为东八区」已在本日更早提交落地——`send_highlight_alert.py` 现用 `fmt_bj(...)+"（北京时间）"`，HEAD 已修复，无需改码（仅待部署）。
- **⏸ M2（HIGH 泛滥 / turnover 90%）未做**：属**产品/统计层**（引入稳定性维度或按分位数定 tier），需主人定调，且需更长样本，**不搭在本轮代码修复里**。产品洞察（44% 卡片为板块/链聚合、无具体标的）同属功能增强，另议。
- **自测**：新增 `workbench/test_highlight_audit_20260924.py` **33/33**（M1 同值同分/单调/封顶/负值/None/排序倒置回归；M3 score 主轴 + 两路径源码守卫；M6 单笔封顶/聚合不封顶/None 保守；M4 HIGH→MED 且不误伤）；既有 `test_highlight_alert.py` **68/68**、`test_macro_market_board_tier2.py` 28/28、`test_macro_market_p1_upstream.py` 24/24、`test_macro_market_p0.py` 16/16 无回归；`py_compile` 通过。
- **待部署**：`macro_market.py` 在 overview 构建（build_daily_brief）内、`send_highlight_alert.py` 由 scheduler 子进程执行 ⇒ 均需容器 **redeploy** 后生效。

### 高亮信号类型标签 + GitHub 事件强度主轴 M7-1/M7-2（工单 OPT-HL-TYPING-002，2026-09-24，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\待修复工单_高亮信号类型标签与github事件强度_2026-09-24.md`（基线 `origin/main=a56ec6f`）。**零 DDL、零迁移**；Q1~Q4 均采工单建议（Q1 A 0.6/0.4、Q2 配额 1、Q3 去 `catalyst_events`、Q4 short/5）。

- **🟠 M7-2（主收益）GitHub 卡同日必然同分（已修）**：`github_activity` 的 conviction 全由**大盘级六轴**（funding/netflow/stable/roi 皆常量）算出、**无 dev 轴** ⇒ 同批次除 `mvrv_pct` 外输入相同 ⇒ conv 与 `ratio`（Dev 倍数）完全脱钩（实测 ASTER/WMETAX 同为 67、`es=None`）。修法：`_event_strength_score` 新增 **`"ratio_x"`** 主轴（`r=max(ratio,1/ratio)`，1.0x→50、1.5x→60、2.0x→70、3.0x+→90，常量 `_RATIO_ES_SLOPE=20.0`，与 M1 的 `_PCT_ES_CAP` 共用封顶保持跨类型可比），GitHub 卡按 A1 口径融合 `conviction = 0.6×conv + 0.4×es` 并落 `event_strength`。
- **🟠 M7-1（语义归位）多空博弈合并卡错用 `catalyst` 标签（已修）**：`signal_type: "catalyst"` → **`"conflict_game"`**，`related_dims` 去掉 `catalyst_events`（它是合成博弈卡、非催化剂事件，双重误导 + 占用 catalyst 配额）。同步补 4 处（缺一即引入新缺陷）：① V2 配额表 `"conflict_game": 1`（不挤 catalyst）；② horizon map `short/5`（否则落默认 medium/14 过长）；③ 邮件 `SIGNAL_TYPE_LABEL` 补 `"conflict_game": "多空博弈"`（否则徽章露原始 token；前端 `index.html:13491` 已有，零改动）。**对高亮邮件零影响**——博弈卡 `direction="watch"` 不进 `select_highlight_signals`（§1.3），仅机会清单/池层受益。**帮 M3 洗清嫌疑**：上轮复验「catalyst 仍 `es=None`」的表象是此标签误用，非 M3 漏项。
- **自测**：`workbench/test_highlight_audit_20260924.py` 由 33 → **50/50**（新增 T1~T10：ratio_x 六例含 0.5x 对称与 None/0/负/非法、GitHub 融合算术 64、同日 2.5x≠1.5x 消除同分、conflict_game 标签/配额/horizon/邮件标签表、T10 独立配额不挤 catalyst）；既有 `test_highlight_alert.py` 68/68、`test_macro_market_board_tier2.py` **36/36**、`test_macro_market_p1_upstream.py` 24/24、`test_macro_market_p0.py` 16/16、`test_brief_data_model.py` 20/20、`test_daily_brief_p1.py` 22/22、`test_daily_brief_20260924.py` 29/29、`test_ai_signal_quality.py` ALL PASS 无回归；`py_compile` 通过。
- **待部署**：需容器 **redeploy** 后生效。验收要点（§3②）：`signal_type=='conflict_game'` 条目出现（无样本则标注）、`github_activity` 的 `event_strength` 非 None、同日两条 GitHub 卡 conv 不再相同。

**复验处置 N1/M8（`复验_高亮信号类型标签与github事件强度_ce59f0a_2026-09-24.md`，2026-09-24，本次提交）**：复验确认 M7-1/M7-2 代码闭环（4 文件无夹带、A~F 逐项一致、50/50 独立复跑、真实样本离线推演 distinct 1→3），并指出第②路线上仍跑旧码（**待 redeploy**，非代码问题）；另开 2 项 P3，本轮按建议处置：

- **🟠 N1（P3，已修）`ratio_x` 双向对称 ⇒ 极端 decline 与极端 burst 同分（语义倒挂）**：`r=max(ratio,1/ratio)` 使 0.1x（开发停滞=风险/watch）与 10x（爆发=机会/long）es 同为 90、conv 并列（base 67 → 双双 76），方向相反却同分，且 ≥3.0x 后分辨率归零。**采方案 A**：新增 `_GITHUB_DECLINE_ES_CAP=65` + 纯函数 `_github_event_strength(ratio, gdir, t)`，**decline 侧 es 封顶 65**（风险信号不拿事件强度满分），burst 侧不变。实测 base 67：极端 decline → 66 < 极端 burst → 76（倒挂消除）；同倍数 decline(65) < burst(70)。（方案 B「r 硬上限 6」经推演**无效**——es 在 r≥3 已封顶 90，非本轮采用。）
- **⚪ M8（P3，已修）decline 卡 `key_metric` 显示 `Dev 0.0x` 语义不明**：方向与倍数关系反转（burst>1 为爆发、decline<1 为停滞），裸倍数无法区分。改为方向词前置：burst 保持 `Dev 3.0x`，decline 渲染 `Dev 停滞 ↓0.0x`。
- **自测**：`test_highlight_audit_20260924.py` 50 → **57/57**（N1 封顶常量/极端 decline 封顶/burst 不误伤/同倍数大小关系/语义倒挂消除/源码守卫 + M8 文案守卫）；既有 `test_highlight_alert` 68/68、`board_tier2` 36/36、`p1_upstream` 24/24、`p0` 16/16、`brief_data_model` 20/20 无回归；`py_compile` 通过。
- **P0 阻塞项（非代码）**：**Zeabur redeploy** 后第②路三项（conflict_game 出现 / es 非 None / 徽章翻新）才可终验。

**复验处置 N1残留/N2/N3（`复验_高亮信号N1M8处置_423fb16_2026-09-24.md`，2026-09-24，本次提交）**：复验确认 N1/M8 代码闭环、**M7-1 借本次 redeploy 终验生效**（线上 `conflict_game` 2 条 + `related_dims` 已去 `catalyst_events`），另挖出 **N2（P1 新缺陷）** 与 N1 残留、N3，本轮按建议全部处置：

- **🔴 N2（P1）催化剂展示分失真 + `es` 突破上限（已修）**：
  - **N2-a**：legacy 归一化 `raw*10` 在 `raw≥10` 即撞顶 100（实测 ETH/SOL/USDC 的 raw 37~120 全渲染「催化剂 100分」，真实 `composite_score` 仅 75~86）⇒ 改**有界饱和映射** `50 + 50·raw/(raw+30)`（raw=0→50、单调、**永不到 100**；raw=51→81.5 与 composite_score 81 量级对齐）。实测 legacy 由 `{100,100,100,100,100}` → `{BTC90.0, USDC82.5, HYPE82.0, ETH81.5, SOL77.8}`。
  - **N2-b**：`"score"` 主轴原 `[0,100]` 直通 ⇒ catalyst 天然可拿 es=100、其他主轴封顶 90，**M1 刚统一的跨类型可比性复发** ⇒ 统一夹 **`[40,90]`**。
  - **N2-c**：`_recent_catalyst_targets` / `_recent_catalyst_decision_targets` 尾部 `except Exception: return []` **静默吞异常**（决策 feed 失败会静默降级 legacy 而无人知晓）⇒ 改 `logger.warning(...)` 留痕（新增模块级 `logger`）。
- **🟠 N1 残留（P3，已修）温和区倒挂**：封顶 65 只在 `r>1.75` 生效 ⇒ `decline 0.5x(66) > burst 1.5x(64)` 仍倒挂 ⇒ `_GITHUB_DECLINE_ES_CAP` **65→60**（=burst 1.5x 水平），温和区与极端区倒挂**同时消除**。
- **⚪ N3（观察，已修）`funding`/`fng_extreme` 补 es**：融资落地卡 `event_strength = _event_strength_score("usd", amount_m*1e6)`（金额未披露 → None 不渲染）；恐贪极值两分支 `event_strength = _event_strength_score("score", abs(_fg_val-50)*2)`（偏离中性程度，20/80 对称 → 60）。`conflict_game`（合成卡无独立事件量）保持 `es=None` 合理。
- **自测**：`test_highlight_audit_20260924.py` 57 → **67/67**（N1 残留温和区消除 + N2-a 旧式移除/有界映射/单调不撞顶/ETH 81.5 + N2-b 夹取 40-90 + N2-c 两处 logger 守卫 + N3 两处 es 守卫/对称映射）；既有 `test_highlight_alert` 68/68、`board_tier2` 36/36、`p1_upstream` 24/24、`p0` 16/16、`brief_data_model` 20/20、`daily_brief_p1` 22/22、`ai_signal_quality` ALL PASS 无回归；`py_compile` 通过；prod 只读复跑 `_recent_catalyst_*` 无异常、legacy 不再撞顶。
- **待部署**：需容器 **redeploy** 后生效。⚠️ 注意：本轮复验已确认线上跑旧码（N2 三项均待部署），redeploy 后第②路可终验（含 N2-a 展示分不再 100、`github_activity` es 非 None、`conflict_game` 出现）。

### CoinGlass 套餐数据接入 P0-A / P0-C / P1（方案 `Coinglass套餐数据接入方案_2026-09-23.md`，2026-09-24，本次提交）

来源：`04_架构与代码方案/Coinglass套餐数据接入方案_2026-09-23.md`（P0-D / P0-B 此前已完成）。本轮做 **P0-A（已采未用列进消费）/ P0-C（混合口径偏高幅度下界，仅标定侧）/ P1（4h 爆仓历史回填）**；**不动任何判定阈值、不进 `scheduler.py`、不做 P2 扩维**。

- **🔴 P0-A 三消费点落地**：① `squeeze_fuel.evaluate_fuel()` 新增 `liq_bg_24h_usd / liq_bg_24h_long_usd / liq_bg_ts / liq_bg_scope` 四键落 `metrics.fuel`（**只读展示，不进任何判定分支**——`classify_fuel()` 签名与 docstring 均不含该参数，单测双保险）；② 缺口存在性标注：`_latest_liq_snapshot()` 超龄时判定行为**不变**（仍走 `missing ⇒ mixed`），额外落背景值供排查与邮件脚注；③ 标定脚本 `calib_squeeze_liq_thr.py` 新增 `liq_usd_4h` / `liq_usd_24h` **只读分组维度**（三分位切桶）。展示侧同步：`scan_daemon` 的爆仓轮询 SQL 补取 `long/short_liq_usd_24h`（此前只取 1h 两列 ⇒ `liq_background` 恒为 None），新增 `_liq_col()`（None-safe 取列，行缺失/列 NULL ⇒ None）与 `_fmt_usd_abs()`（`$1.23M` 量级、**不带正负号**、None→「—」）两个纯展示辅助，在「最近 1h 多空爆仓/成交额」后**并列**追加「24h 累计 多 X / 空 Y」；脚注同轮披露「窗口不同、**不可换算、不可相除**」+「**严禁跨桶差分**」+「严禁 `24h÷24` 当 1h」。⚠️ **口径列名故意不叫 `liq_scope`**（顶层 `metrics.liq_scope='coinglass_rolling_1h'` 已被判定侧占用，同名即一词多义）⇒ 起名 `liq_bg_scope='coinglass_rolling_24h'`。`FUEL_METRIC_VER` **1→2**（结构变更必须递增版本位；v1 历史行没有这 4 键 ⇒ NULL 而非 0，勿当「无爆仓」读）。
- **🔴 P0-C 口径 A / B 严格分表（只改标定侧）**：`calib_squeeze_liq_thr.py` 并列输出两口径且**禁止合并求分位**（不变量 4）——**口径 A** = 现役混合口径（coin-list 全所滚动 1h ÷ Binance 滚动 24h `vol_win`，5m）；**口径 B** = 新增单所同窗（`liquidation/history` `exchange=Binance` 的 **4h 分段增量** ÷ `asset_klines` 1h `quote_vol` 在同一 4h 墙钟区间求和），仅给「混合口径偏高幅度」的**下界**。护栏：`assert_single_scope()`（同一次聚合内 `interval`/`exchange_scope` 必须唯一）、`MIN_CROSS_PAIRS=30`（跨所配对数不足 ⇒ `ok=false` 且本行**不构成结论**）、`SQL_CROSS_EXCHANGE` 走 `AS all_long` 判定（`symbol` 须为**币种基码**，传合约码静默 0 行）。**四门闸门**优先级 `分母门 > 分子门 > 跨度门 > 口径B门`（`primary_gate` + `_failed_gates` 归因），退出码 0/2/3/4 配 `CONCLUSION_BY_CODE` / `RELIABLE_BY_CODE` / `DECISIVE_BY_CODE` 真源映射。⚠️ **目标降级（评审结论）**：原「把口径拉齐」**不成立**——A 的分子（全所）⊇ B 的分子（Binance 单所）⇒ `A分子/B分子 ≥ 1` 严格成立，但**分母端**（24h vs 同 4h 窗口）**无法对齐** ⇒「跨所放大」与「分母窗口错配」不可分离，`A/B` **只能读作下界**，**不得**据此直推「阈值该平移多少」。
- **🔴 P0-C 输出纪律：前置门之前只印结构性信息**：口径定义 / 样本条数 / 配对数 / B 门 `reasons` / `--no-b-gate` 标记**可印**；**比率与分位属结论性读数，一律在门后印**。实测 `--days 1` 文本模式在前置门路径**零比率输出**（只印「表跨度 77.5h < 30 天 / 极端段仅 1 次 < 3 次 / 跨所配对数 6 < 30」+ `归因以跨度门为准`）。
- **🔴 P1 `fix_069` 两表 + 回填脚本**：`biz.liquidation_history`（PK `(symbol, interval, exchange_scope, ts)`，`interval`/`exchange_scope` **进 PK** ⇒ 结构上杜绝 4h/1d 混桶与 Binance/全所混算）+ `biz.liquidation_backfill_cursor`（PK `(symbol, interval, exchange_scope)`，列 `done_through`/`updated_at`）。表注释**首行**写明用途限制（仅供标定/回测，**禁止**接入 `scan_squeeze` / `squeeze_fuel` 实时判定）与「分段增量 ≠ 滚动窗口、不可换算、不补 0」。`phase_backfill_liq_history.py`：`DEFAULT_MIN_GAP=2.5`（= 24 req/min，**不得**复用 `CG_MIN_GAP=0.3`）、`MAX_LIMIT=4500`（客户端上限，超出 ⇒ `code=400`；@4h 服务端最多回 180 天 = 1080 点 ⇒ 单请求覆盖全窗口）、指数退避 `1→2→4→8s` × 3 次后跳过该币（**不整轮失败**）、`RESUME_TOLERANCE_INTERVALS=1`、游标**仅在整币全区间成功落库后**推进（失败/截断不留游标，避免把半截当已完成）、`base_code()` 仅用于 `--scope all` 请求参数（落库仍写合约码）。
- **⚠️ 链上源选 CM 而非 CoinGlass（方案 §3.3/§4.6 复审结论）**：`/api/exchange/balance/*`（交易所净流）**已判定不接**——`macro_market.py` 现走 **CM Community 原生**并明确记为「比 CoinGlass 可靠」的替代死链路方案，接回来是**主动降级**。早先「疑似 legacy v2 路径待复核」的推测**已撤销**，勿再作为待确认项探测。
- **验收取证（已实跑）**：`py_compile` 全绿；单测 `test_squeeze_span_judge.py` **48/48**、`test_liq_history_scope.py` **39/39**、`test_squeeze_fuel.py` **99/99**、`test_squeeze_battle.py` **143/143**。`calib --days 1 --json` ⇒ **EXIT=3（SAMPLE_UNUSABLE，预期）**：`scope_split` A 侧 n=54586/P50=4.213e-05、B 侧 n=12/P50=1.141e-03；`cross_exchange_lower_bound` `ok=false`（n_pairs=6<30，中位 3.687、逐点「全所 ≥ Binance」占比 100%）；`rolling_groups` 三桶越阈率单调 1.21%/20.05%/30.12%。⚠️ **`calib --days 30` 实测跑不动**（5m × 500+ 币结果集过大，DB 侧 `wait_event=Client/ClientWrite` 卡 673s）⇒ 验收用 `--days 1` 取证，长窗口应等 `liquidation_history` 回填完再跑。`phase_backfill_liq_history.py --probe` 9 用例全符合预期（`1h ⇒ code=403 + upgrade_required=STANDARD`、缺 `exchange_list` ⇒ `400`、传合约码 ⇒ `code=0` 但 0 行；**意外可用项 0 个**）；`--dry-run --scope binance --days 180` ⇒ 宇宙 527、527 次 × 2.5s ≈ 22.0 min；`--resume --days 30 --symbols BTCUSDT,ETHUSDT` ⇒ `跳过 2 / 待处理 0 / rows_written 0`（**无重复行**，证明 PK+游标生效）；`apply_migration.py migrations/fix_069_liquidation_history.sql` **连跑两次均成功**（幂等）；DB 实测两口径各 180 行/币，窗口 08-25 08:00 → 09-24 04:00 UTC。
- **待部署**：P0-A 改动在 `scan_daemon.py` + `squeeze_fuel.py` ⇒ 需容器 **redeploy / 重启 `scan_daemon`** 后新列与新版本位才写入；P1 回填脚本与迁移**离线运行，无需重启**（迁移已手动应用，回填按需手动/后续排期执行）。

### 告警邮件链上转账「最大笔取数 + 地址显示」（审计_告警邮件LSK-CELR_链上转账地址_2026-09-24，本次提交）

- **N-A56-1（P2，已修）**：`phase_build_event_watchlist.build_transfers` 原用三个独立 `MAX()` 取 `chain/from_label/to_label`，与 `MAX(value_usd)` **无关**，且对类别列取 MAX 是**字典序**（`'unknown' > 'exchange'`）⇒ 实测 87 行里 12 行（14%）「最大单笔」元组与真实最大笔不符（含**链说错**：USDT0 polygon→optimism、BURN solana→eth）。改为 `LATERAL … ORDER BY t2.value_usd DESC LIMIT 1` 取**真实最大笔**，使 chain/标签/地址/tx 与 `max_usd` 同源。
- **N-A56-2（P2，已修）**：标签取数组列 `from_label_names`/`to_label_names`（非空时）→ `from_labels`/`to_labels` → 标量 `from_label`/`to_label` → `?`（新增 `_pick_label`，与 `scan_daemon._resolve_addr_label` 同口径）。实测 WLD/BEAM/ENA/U/PEPE 等由 `unknown→unknown` 变 `Binance→Binance`。
- **N-A56-3（P3，已修）**：detail 增「其中 N/M 笔流向交易所（潜在抛压）」（`COUNT(*) FILTER (WHERE is_to_exchange)`）。
- **地址显示（采纳审计 A/B/C/D）**：`source_ref.max_tx` 存最大一笔的链/完整收发地址/`tx_hash`；`_get_resonance` 事件段 SELECT 增 `source_ref` 并置 `addr` 键（向后兼容，`_render_resonance_msgs` 按 `dict.get` 取值）；`_render_resonance_msgs` 对带 `addr` 的条目**另起一行**以 monospace 渲染**完整**地址（`word-break:break-all`），且**豁免** `RESONANCE_MSG_CHARS`(90) 截断（否则 42 字地址被拦腰砍断、复制出来残缺 —— 恰好废掉展示用途）；图例同步声明（三落点逐字对齐）。
- **N-A56-4/5（未改，产品/观察）**：① 3 天新鲜度窗口贴边 2h25m（同一批信号晚发 2.4h 结论即翻转）属**产品口径**；② 「市场大幅波动 LUNA/AVA/LSK/STG」多币行情快讯被当 LSK 专属利空属 `catalyst_impact.asset_id` 关联精度（NER），非本层代码缺陷。
- **自测**：新增 `workbench/test_scan_alert_onchain_addr.py` **32/32**（`_pick_label` 7 例 + 生产者 SQL 源码守卫 7 例 + 地址行渲染 11 例含超长豁免/无 addr 不渲染/旧快照兼容 + `_get_resonance` 守卫 4 例 + 图例 3 例）；`test_scan_alert_header_regime.py` 135/135、`test_scan_alert_audit_deepdive.py` 75/75、`test_scan_alert_remaining.py` 16/16、`test_scan_l1_closed_bar.py` 16/16、`test_scan_scenario_label.py` 48/48 无回归；`py_compile` 2/2。
  - **⚠️ 踩坑（第 N 次）**：图例文案一度写 `**最大一笔**`（markdown 强调符）→ `test_scan_alert_audit_deepdive.py` 的 `**` 护栏当场抓到，改「最大一笔」。**固化：改任何渲染文案后必跑 `**` 护栏。**
  - **并发 WIP 处置**：`scan_daemon.py` 另有并发会话的未提交改动（P0-A 24h 爆仓背景，区域不同）⇒ 用 `git stash push -- <path>` 暂存 → 提交本单改动 → `git stash pop` 还原，未纳入本提交。
- **待部署**：`scan_daemon`（常驻，需重启）+ `phase_build_event_watchlist`（scheduler 子进程，需容器 redeploy）后生效。

**复验收口（`复验_链上转账N-A56_0c0c567_2026-09-24.md`，本次提交）**：独立三段式（真码子树 + 真 prod DB 只读 + 新旧两版 producer 同 conn 对比）确认 N-A56-1/2/3 与地址显示**代码全部到位**、测试 32/32 精确复现、`chain` 说错 2 行与自述一致。本轮处置其 4 项新发现 + 2 处口径/部署更正，**零 DDL、不改判定口径**。

- **🟠 N-567-1（P3，已修）标签解析器口径分裂**：producer `_pick_label` 的 docstring 误写「与 `scan_daemon._resolve_addr_label` 同口径」——该函数**不在 scan_daemon**（在 `send_daily_brief.py:123`），且因签名少 `addr` 参数，**第 4 步永远不同**（本处 `?`、日报 `addr[:8]+"..."`）。**采复验建议②（改动面最小）**：docstring 改为「与**日报** `send_daily_brief._resolve_addr_label` 的**步骤 1–3** 同口径；**第 4 步有意不同**（事件 detail 空间小、`?` 更诚实，且完整地址另有展示位）」；测试该行断言补注释固化「有意不同」，避免未来统一口径时撞测试却不自知。
- **🟠 N-567-2（P3，已修）SQL 仅有源码字符串守卫**：与技能 #114「源码文本守卫不承重」同型（改 `ORDER BY … DESC` 为 `ASC` 测试仍全绿）⇒ `test_scan_alert_onchain_addr.py` 补 **1 条真 DB 行为断言**：取 `build_transfers` 前 5 行，逐行与「直接 `ORDER BY value_usd DESC LIMIT 1`」的 `chain` 比对（无 DB 则跳过）。测试 **32 → 36/36**（带 DB 实测 5/5 命中）。
- **🟠 N-567-3（P3，已修）`tx_hash` 被 daemon 丢弃**：`source_ref.max_tx` 存了 8 字段，`ev["addr"]` 只透传 3 个 ⇒ `tx_hash` 采集了却无法渲染（浏览器链接做不到）。现 `ev["addr"]` 增 `tx`；渲染层在地址行下**再列一行** `tx 0x…`（完整、monospace、`break-all`、豁免 90 字截断），无值不渲染；图例同步声明。**未做可点击链接**（复验 §八#3 提醒邮件客户端兼容性；纯文本最稳且可复制，故采纯文本）。
- **🟡 N-567-4（P4，记档未改）**：`n_to_exchange` 仅在 `>0` 时披露 ⇒ 若上游未来改三态（NULL=未判定），`0` 与「未知」文案不可分。实测当前 `is_to_exchange IS NULL` = **0/8887** ⇒ 无风险，属前瞻记档。
- **📌 口径归一（已采纳）**：复验指出「元组任一分量被真实纠正」应为 **23**（`from` 13 + `to` 16 − 双侧重叠 6），此前自述「13 行」只覆盖 `from` 侧。**统一按 23 口径**；并注意 **54/77（70%）仅从 `unknown` 变 `?`**（无实质信息增量），「WLD/BEAM/ENA/U/PEPE 变 Binance」**不可外推**为整体效果。
- **🔴 部署判定更正（复验反转自述，**重要**）**：`0c0c567` **已自动部署**——容器**级重建**（`__daemon__` 与 `[看护] scheduler_watchdog` 两独立进程**同秒**启动）发生在 push 后 **5m33s~7m01s**（四次采样：15:01:23→15:07:11 / 16:09:36→16:16:11 / 16:33:16→16:40:17 / 16:40:50→16:46:23）。⇒ **「push → 约 6 分钟 → 线上生效」是稳定规律**，此前「需 Zeabur 显式 redeploy」的表述**不准确**（代码随容器自动重建上线）。唯一剩余动作 = **等 producer 调度**（`scan_event_watchlist = 17 */6 * * *`，18:17 CST）或手工补跑；验收锚点：`SELECT count(*) FILTER (WHERE source_ref ? 'max_tx') FROM biz.event_watchlist WHERE event_type='onchain_transfer'` 应 > 0（复验时为 0/93）。
- **🟠 队列积压（复验附带，P3）—— 2026-09-26 复核：原「runner 并发槽位不足」定性不成立**：排队 45~75min / 09-24 05:32 有 9 个任务连续启动均为**现象**，真因见下文「队列积压核查」节（① `monitor` 类被候选窗口 `LIMIT 5` **结构性饿死**；② DB 写不可达窗口）。容量实测占用率仅 **42.6%**、排队 p50/p90 = **0 分钟**。
- **未验/未做（复验边界）**：`5a95b32` 的 coinglass 套餐、`fix_069` 表正确性、N-A56-4/5 均不在本轮。

### 调度静默处置：data_sync_daily 停滞 30.8h（告警_调度停滞_data_sync_daily，2026-09-26，本次提交 `f11f1a3`）

**表象**：关键 cron `data_sync_daily` 30.8h 无成功（最近 done `1790289223.297721` = 09-24 22:33:43 UTC），阈值 30h，看护**未自动补跑**并提示「上一轮判定 stuck」。

**根因链（只读 prod DB 实证，三段）**：

- **① 补跑被永久挡住 —— 看护 `_last_run_error` 无时间窗**：原查询取「任意历史最近一条带 error 的行」，命中 **2026-09-08 14:16 的 `stuck:` 行（18 天前）**；此后 17 次成功**不改变判据** ⇒ 每个告警周期都被这条陈旧错误拦下。**修复**：`_last_run_error(key, within_seconds)` 加时间窗，窗口 = 该 key 的停滞阈值（`_check_key` 传 `threshold`），窗口外陈旧错误一律忽略。
- **② 零日志僵尸长期占位 —— 收割器 NULL 盲区**：条件2 `(SELECT MAX(created_at) FROM sys.task_log …) < NOW() - interval` 对**零日志任务恒为 NULL ⇒ 永不成立**，只能干等 12h 硬超时。实测 3 个 0 日志 running 任务（`cc842e30ede3` highlight_alert / `3adcf7f525db` scan_outcome_settle / `5e2e680bb24a` scan_freshness_watchdog，均 09-26 05:07~05:21 提交、10:23~10:45 取走后零日志）经 `_has_active_task` **同名占位**把对应整点任务的自调度一起堵死。**修复**：`COALESCE(…, 'epoch'::timestamptz)` 兜底 ⇒ 零日志任务运行满 10min 即可收割。只读比对：**旧式命中 0、新式命中上述 3 个且不误伤**其他 running 任务。
- **③ 静默放大器 —— `autorestart=unexpected` + `exitcodes=0`**：`scheduler` / `scheduler_watchdog` 以 0 退出时 supervisord **不拉起**，与 APScheduler 默认 MemoryJobStore（重启丢弃错过 cron、不补跑）叠加 ⇒ 一旦退出即整日调度静默。**修复**：改 `autorestart=true`。

**基础设施侧取证（只读；DB 侧可及范围内已定案）**：

- **Postgres 服务端从未重启**：`pg_postmaster_start_time()` = 2026-09-17 11:23:48 CST（取证时 uptime **223.75h**）⇒ 排除「DB 进程崩溃/重启」。
- **静默跨两个节点的两个容器同时发生；但「同停同起」定性已被后续复核证伪**：主容器探针 `biz.kol_post`（kol_daemon 每 30s）与**美区节点独立容器**探针 `biz.asset_klines.fetched_at`（scan_daemon 5min 任务）**同窗口静默**——US 侧末次写入 **09-25 14:13:41**、首次恢复 **09-26 13:25:43**（逐小时计数：09-25 14 之后整段为 0，直到 09-26 13 才 2162）；主容器探针同分钟恢复（13:23~13:24）。**但复核发现静默窗口内主容器仍在成功写 `sys.task`**：09-25 14:00:00 / 14:05:00 有 `[调度]` 提交行落库、09-25 20:09:49~20:46:29 有连续 `started_at`/`ended_at` 更新、09-26 10:21:22 有三条 `timeout: 运行超过 12h` 批量收割、10:35/10:52 又有新任务被取走 ⇒ **不是「容器被平台整体停掉」**。同容器的 `kol_daemon` 该窗口却零写入 ⇒ 更符合「**DB 间歇性可达 + 各常驻进程各自被这场故障打死/挂住**」（kol_daemon、scan_daemon 崩进 FATAL；scheduler/TaskManager 存活但任务积压 260min+）。
- **窗口内并非全库零写入**：`[看护]` 心跳在 09-25 16:19 / 09-26 05:41 / 12:36 有**孤立成功写入** ⇒ 与「DB 间歇可达、恢复首轮写一条心跳」一致，而非恒定写不可达。
- **未定项（DB 侧不可判）**：DB 服务端存活 ≠ 连接可达；「容器被停」与「连接被拒」在「写库即心跳」视角下签名一致。**最终定性需 Zeabur 控制台**（服务事件/重启记录、资源曲线、部署历史）——本机原无 `zeabur` CLI，取不到。
- **Zeabur 控制台侧收口（2026-09-26 补；结论：CLI 这条路走不通，不再重试）**：本机已装 CLI（`/opt/homebrew/bin/zeabur` 0.22.2，官方 Release `zeabur_0.22.2_darwin_arm64`，sha256 与官方 checksums 逐字符一致）并已登录（`tingting950802@gmail.com`；个人 workspace 下无 team 可切，项目 `n8n` 内含该服务）。取证结论：
  - **不可达三项**：① 服务指标 API 硬限窗口 `< 12h2m`（超出即 `INVALID_ARGUMENT: time range must be less than 12 hours and 2 minutes`），且实测 `MEMORY`/`CPU` 均返 `no metric history found`；② `deployment log --type runtime` 只回 ~2 分钟（102 行）；③ `deployment list` **封顶 5 条且无分页**（实测 5 条全为当日 push）。
  - **更根本的原因**：每次 push 都会**重建容器**，实时抹掉上一轮容器的运行时证据（重建后 `supervisorctl` 各进程 uptime 仅 `0:01:52`）⇒ 09-25 停滞窗在该服务上**已不可回溯**。
  - **顺带确证**：服务 `crypto-profile-collection`（ID `6a702918fefeb46a88349f8c`，git trigger `main`）状态 `RUNNING`；supervisord 7 进程（catalyst_fast_daemon / chain_transfer_monitor / gunicorn / kol_daemon / scan_daemon / scheduler / scheduler_watchdog）全 `RUNNING`，**无独立 task_manager 进程** ⇒ 与「runner 驻在 gunicorn 内」一致；容器内 `grep` 确认 `task_manager.py` 的 `NOSTART_ERROR`（L53）与 `scheduler_watchdog.py` 的 `infra_died`（L219）**已上线**。
  - **结论**：控制台侧佐证**不可得**（CLI 无历史，且证据被自身 push 抹除）⇒ 基础设施侧定性**维持**上文「DB 间歇可达 + 各常驻进程各自被打死/挂住」，**不再为此追查**。

**线上闭环（本次未做任何 prod 写操作 —— 重启后新代码自动完成，手写反而重复）**：

- **部署**：`f11f1a3` push 后容器**自动重建**（18:47 起恢复写入），与上文已固化的规律「push → 约 6 分钟 → 线上生效」一致。
- **3 个零日志僵尸已收割**：三者在 **18:47** 全部转 `failed`（`stuck: 90分钟无新日志，疑似卡死`），且**日志数均为 0** ⇒ 旧条件命中数为 0、只有 COALESCE 兜底能做到，**这是修复②生效的直接证据**。
- **data_sync_daily 已自动补跑成功**：任务 `0989a6d4485f` 由**看护**于 **18:47:18** 提交（名带 `[调度] ` 前缀 ⇒ 出自 `submit_scheduled_task`，非 Web 手点），**18:56:58 done**（918 行日志 / 10.2min）⇒ 告警条件解除；**同时证明修复①生效**（旧逻辑会被 09-08 那条旧行挡住）。
- **⚠️ 补跑增补口径**：从工作台 `/api/tasks/start` 手点同名任务，任务名为「每日数据同步/矫正总调度」**不带 `[调度]` 前缀** ⇒ 既不被看护 `_last_done_ts` / `_recent_submission` 认账，又会与看护补跑**双跑**。**补跑一律走看护/调度提交，勿在 Web 手点同名任务。**
- **被堵死的整点任务已疏通**：三个曾长期无法自调度的整点任务全部复跑成功 —— `highlight_alert`、`scan_outcome_settle`（均 `5 * * * *`）**19:05** 提交并 `done`；`scan_freshness_watchdog`（`20 * * * *`）**19:20** 提交（`0fd34fbc2244`）并 `done`（零 error）。`catalyst_run_all` 亦于 18:47 由看护补跑（同批 stale），日志持续增长。

**遗留**：① 基础设施侧定性已由「平台级事件」修正为「DB 间歇可达 + 各常驻进程各自被打死/挂住」；Zeabur 控制台侧佐证**已实测不可得**（见上文「Zeabur 控制台侧收口」，CLI 无历史 + 证据被自身 push 重建抹除）⇒ **本条就此收口，不再追查**；② `sys.task` 排队积压（前节已挂账）本轮未动；③ 冗余 `stash@{0}` 已 drop（内容仅为本轮已上线的 `工作台_OBM_CM执行指南.md` 删除）。

### 调度停滞 follow-up：cmc_quote_snapshot 停滞 31.5h（告警_调度停滞_cmc_quote_snapshot，2026-09-26，本次提交）

**表象**：`cmc_quote_snapshot` 31.5h 无成功（最近 done `1790308833.453979` = 09-25 12:00:33 CST），阈值 30h；看护**未自动补跑**，提示「上一轮判定 stuck: 90分钟无新日志」。

**先证伪一个假设**：初判为「排队延迟型误判」——任务 `f409bab6af0e` 排队 260min 后被 runner 取走（09-25 20:35:15，`started_at` 被覆盖为取走时刻），仅有的 2 条日志停在提交时（16:15/16:16），故判据 `MAX(created_at) < NOW()-90min` 立刻成立、任务「真正开始 11min 后」被误杀。**该假设被只读回放证伪**：`_run_task` 成功进入 `try` 后**第一件事**就是写 `[TASK] 开始执行，cwd=…`（对照组 8 个正常 done 任务的 `line_no=3` 即此行、时刻 = `started_at`）。近 3 天 17 条 `stuck:` 中 **9 条根本没有 post-start 日志**（含 `f409bab6af0e`，只剩提交时那 2 条）⇒ 线程**从未跑到那一步**，是真僵尸而非误判。（另 8 条有 post-start 日志 = 真卡死。）

**真根因**：`_run_task` 里 `task = _load_task(task_id)` 与 `cmd = list(task["cmd"])` **原本在 `try` 之外**；DB 瞬时不可达（正是 09-25 14:13→09-26 13:25 那段间歇期）时抛错 ⇒ **daemon 线程静默死亡**，任务停留 `running` 且零新日志 ⇒ ~10min 后被收割器判 `stuck:` ⇒ 看护 `blocked_by`（语义为「任务自身跑不完，补跑只会再空转」）命中的却是这条**基础设施侧死亡**记录 ⇒ 拒绝补跑 ⇒ 31.6h 停滞。

**修复（`workbench/task_manager.py`）**：

- **① 收割条件2 与 `started_at` 对齐**：日志子查询加 `AND l.created_at >= t.started_at`，只统计**任务真正开始之后**写的日志。对「提交时有日志、运行后零日志」的僵尸**同样立即命中**（甚至早于 12h 硬超时），对真运行任务无影响。只读回放：近 3 天 17 条 `stuck:` 判据命中数不变（17→17），无新误伤。
- **② 消除静默死亡**：把 `_load_task` / `cmd = list(task["cmd"])` **移入 `try`** ⇒ 失败被记日志并立即置 `failed`，错误串不以 `stuck:`/`timeout:` 开头 ⇒ 看护不再误拦补跑（这是 `blocked_by` 与「基础设施侧死亡」解耦的关键）。except 内的上报再套一层兜底（DB 仍不可达时至少落 stderr），避免「上报失败 → 二次静默死亡」。
- **离线自测**（不触库，monkeypatch `_load_task`/`_append_log`/`_update_task`）：`_load_task` 抛错 → 置 `failed` 且错误串不触发 `blocked_by`；`cmd=None` → 置 `failed`；正常路径 → 仍 `done`。

**口径更正**：`f11f1a3` 的 COALESCE 修复本身**正确且必要**（它 18:47 收割的 3 条 0 日志任务是真僵尸）；本条补修的是让这类静默死亡**不再发生**，并让「已发生」的那类错误串不再被看护当成「任务自身跑不完」。**残留**：收割器仍统一写 `stuck:` ——若 `_run_task` 的失败上报也因 DB 不可达而失败，任务仍会被标 `stuck:` 并短期拦住补跑（本次靠 30h 时间窗 + 下一次 cron 自愈：`cmc_quote_snapshot` 20:00 CST 那次因 `f409bab6af0e` 已 failed 而不受 `_has_active_task` 阻挡，会正常执行并解除告警）。**该残留已由下一节的错误标签分流收口。**

### 收割错误标签分流：`nostart:` vs `stuck:`（告警信号准确性，2026-09-26，本次提交）

**动机**：上一节的残留 —— 收割器对条件2 的命中一律写 `stuck: N分钟无新日志，疑似卡死`，看护 `blocked_by` 命中该前缀后固定输出「补跑大概率重蹈覆辙，请先排查根因（如 LLM 欠费 / 上游限频）」。但近 3 天 17 条 `stuck:` 中 **9 条实为基础设施侧静默死亡**（DB 写不可达窗口内线程从未启动）⇒ 告警把运维引向**错误的排查方向**（本会话的起点正是这封误导邮件）。

**动手前的复核（只读）**：原计划「让看护不再因该标签拦补跑」**改不动任何决策** —— 看护两个闸同窗口同谓词：`_last_run_error` 与 `_recent_submission` 均为 `name LIKE '[调度] {key}%' AND started_at > NOW() - 阈值`，而 `submit_scheduled_task` 在**提交时**即写 `started_at` ⇒ **`blocked_by` 是 `recent` 的子集**（`stuck:` 命中 ⇒ `recent` 必为真）。故本轮**只修信号准确性，不动补跑决策**。

**改动**：

- `workbench/task_manager.py`（`_reap_zombie_tasks` 条件2）：`error` 改为 `CASE WHEN NOT EXISTS (SELECT 1 FROM sys.task_log l WHERE l.task_id = t.task_id AND l.created_at >= t.started_at) THEN NOSTART_ERROR 常量 ELSE 'stuck: ' || … END`。零日志与仅提交时 2 条日志者 ⇒ `nostart:`；有运行期日志者 ⇒ `stuck:`。`RETURNING` 增列 `t.error LIKE 'nostart:%%'`（psycopg 裸 `%` 须写 `%%`），收割日志拆出 `未启动=` 计数。
- `workbench/scheduler_watchdog.py`（`_check_key`）：新增 `infra_died`（`nostart:` 前缀）分支，输出「此为**基础设施侧**（DB 写不可达等），非任务自身跑不完 —— 下一轮 cron 会自然重试；勿按任务侧根因排查」；`blocked_by` 仍只认 `timeout:`/`stuck:`，**补跑闸 `not check_only and not blocked_by and not recent` 一字未改**。

**验证（只读 + 离线，全部通过）**：

- **SQL 保真**：用假 cursor 捕获代码**真实发出**的条件2 SQL，对该 SQL 原文 `EXPLAIN`（只读库、不执行）：占位符 9 = 参数 9，`%%` 转义正常，计划显示 `Index Scan … status='running'` + 条件2 过滤链。
- **语义回放**：近 3 天 17 条 `stuck:` 在新 CASE 下 → **9 条改判 `nostart:`**（`n_log` ∈ {0, 2}，均无 post-start 日志）/ **8 条仍为 `stuck:`**（`n_log` ≥ 24）。不变式核对「有第 3 条日志却无 post-start 日志」= **0**（与上一节 9/8 分类完全吻合）。
- **看护文案离线**（monkeypatch `_last_done_ts`/`_last_run_error`/`_recent_submission`/`_send_alert_email`/`submit_scheduled_task`，13 项断言全过）：`nostart:`+存活 ⇒ 不补跑且文案含「基础设施侧」、**不再**出现「LLM 欠费」；`stuck:`+存活 ⇒ 仍指引排查任务侧；空错误+存活 ⇒ 原「问题在任务自身」文案不变；无提交 ⇒ 照常自动补跑（原行为）。
- `py_compile` 两文件通过。

**边界说明**：正常路径下 `last_err` 非空必然蕴含 `recent=True`，故补跑决策与改前**逐例等价**（不新增自动补跑）。

### 队列积压核查：runner 并发槽位（P3 挂账项，2026-09-26，本次提交）

**结论：容量不是瓶颈，「runner 并发槽位不足」定性不成立；另定位到一个真实代码缺陷（`monitor` 结构性饿死）。**

**容量实测（近 3 天只读）**：

- 排队延迟 `created→started`：`core` n=345，p50=**0** / p90=**0** / avg=8.8 / max=318.8 min；`monitor` n=48，p50=0 / p90=0 / max=323.3；`chain` n=10，p50=0 / p90=0 / max=278.9 ⇒ **90% 的任务 0 分钟即被取走**，长尾全部落在故障窗口。
- 槽位占用率：总占用 **122.7 槽位小时 / 可用 288（=4×72h）= 42.6%** ⇒ 平均远未打满。
- 参数：全局 `TASK_MAX_CONCURRENT=4`（`workbench/app.py` L44，**runner 线程驻在 gunicorn web 进程内**）；`CATEGORY_MAX = {chain:1, core:4, monitor:1}`（`task_manager.py` L361）。

**两种停摆（逐小时分进程写入计数，互不相同）**：

| 窗口 | 调度器插入 | 看护插入 | runner 取走 | 判读 |
|---|---|---|---|---|
| 09-25 17:00–19:00 | 0 | 0 | 0 | **全进程同时静默** ⇒ DB 写不可达 |
| 09-26 06:00–09:00 | 0 | 0 | 0 | **全进程同时静默** ⇒ DB 写不可达 |
| 09-24 12:00–13:32 | 有 | **有** | **0** | 仅 runner 不取 ⇒ **进程内因**（见下） |

**发现①（代码缺陷）：`monitor` 类被候选窗口 `LIMIT 5` 结构性饿死**。`_runner_loop` 取候选为

```sql
SELECT task_id, category FROM sys.task WHERE status='pending'
ORDER BY (CASE WHEN name ILIKE '%monitor%' THEN 1 ELSE 0 END), started_at ASC
LIMIT 5 FOR UPDATE SKIP LOCKED
```

随后在 Python 侧按 `running_by_cat < CATEGORY_MAX` 逐个筛、命中即 break。当**非 monitor 的 pending ≥5 且其 category 已满**时，候选窗口被同类任务占满，`monitor` 任务**根本不进入候选** ⇒ 即使 `monitor` 槽位空闲也永远不被取走。实况：09-24 12:20 起 pending = 5 个 core（cmc_quote_snapshot / highlight_alert / scan_outcome_settle / etl_asset_market_daily / scan_event_watchlist）+ 1 个 monitor（scan_freshness_watchdog），而 core 已于 12:00 满 4/4 ⇒ `scan_freshness_watchdog` 干等 **73 min**（12:20→13:32）；09-26 同类饿死 **323 min**（05:21→10:45）。**这正是原记录「排队 45~75min」的真因**（并非槽位不足）。

**发现②（非缺陷，但放大积压）：核心槽位被长任务长期占满**。09-24 12:00–13:33 core 满 4/4（`tokenomics_extract_batch` 243min + `spa_browser_crawl_auto` 273min + `b2_ai_noise_clean_by_asset_auto` 257min 三个 09:00–10:00 启动的长任务 + `catalyst_run_all`）；09-25 20:09–09-26 10:21 则被 3 个 core 长任务占 3/4 达 13–14h（`highlight_alert` 852min / `catalyst_run_all` 836min / `catalyst_slow_pipeline` 821min），三者最终**全部由 12h 硬超时收割**。

**修复（已实施，本次提交）**：候选查询新增 `AND (category = ANY(<free_cats>) OR category IS NULL OR category <> ALL(<known_cats>))` —— 只取**有空闲槽位**的 category；未知/NULL category 仍按默认上限 2 放行（与下方 Python 侧 `CATEGORY_MAX.get(cat, 2)` 语义一致，该判断继续作为安全网）。

- **只读现场回放（T = 09-24 13:00 CST，`running_by_cat = core=4`，`free_cats = chain,monitor`，pending = 9）**：**旧**查询候选 5 个全是 `core`，逐个过 Python 筛**全部 `accepted=False`** ⇒ `task_id=None`、monitor 永久饿死；**新**查询候选 `scan_freshness_watchdog`（monitor）⇒ 可被取走。**饿死已消除。**
- **谓词边界单测**（`free=[chain,monitor]`, `known=[chain,core,monitor]`）：`core`→排除（已满）、`chain`/`monitor`→纳入、`NULL`→纳入、未知 `weird`→纳入，全部符合预期；psycopg 的 list→array 绑定（`= ANY(%s)` / `<> ALL(%s)`）实测可用。
- **不建议**提高 `TASK_MAX_CONCURRENT` —— 利用率仅 42.6%，加槽位既不解饿死、也不解 DB 写不可达。

### 重大事件邮件「传导逻辑」可读性优化（audit_重大事件邮件_传导逻辑可读性优化_2026-09-26，2026-09-26，本次提交）

来源：`audit_重大事件邮件_传导逻辑可读性优化_2026-09-26.md`。**仅输出层**：不动 `catalyst_grade` / `classifier` / `resonance` / `catalyst_second_order` 生成逻辑，**零 DDL、零 DB 写入**、不调 LLM。

- **问题**：邮件只讲「发生了什么 + 一堆评分」，不回答读者三问 —— 作用在代币哪一层？直接利好还是仅沾生态光？公告前已涨多少算不算追高？三封样本（ONDO/SKY/SUI）同属 RWA 叙事却被平铺成同一套评分，读者无法感知传导直接度差异。
- **改动全在 `workbench/catalyst/notifier.py`**（`_build_major_event_html` 重排 + 4 个纯函数 + SQL 补一个 LATERAL）：
  - **① 影响传导模块**：规则模板给「传导路径」（`ai_event_type`→`rule_event_type`→`catalyst_kind` 逐级取 `_TRANSMISSION_PATH_CN`）+ 复用 `ai_summary` 作「事件要点」+ 「传导直接度」标签。
  - **② 传导直接度规则映射**（`_transmission_directness`）：标题/摘要点名该币 `symbol`/`canonical_name` 且含「自身受益动作」关键词（购入/买入/纳入/销毁/合作/推出/上线/采用/集成/托管…）→ 「直接利好标的（高）」，否则「生态间接受益（中）」。实测三样本：ONDO/SKY=direct、SUI=indirect，与审计逐条吻合。
  - **③ 预期已消化模块**：`prelaunch_ret_24h` 重解为「公告前已涨 X% ⇒ 部分预期已被提前消化，非零成本」，并按 `resonance_state`（`_RESONANCE_NOTE`）补「系统判定…」；**删掉内部术语「未被计入降权」**。
  - **④ 传导节奏模块**：按 `catalyst_kind` 给即时/短期/中期三阶段（`_TRANSMISSION_TIMELINE`）。
  - **⑤ 板块联动模块**：`_recent_major_events` 新增 `LEFT JOIN LATERAL biz.catalyst_second_order`（取 `array_agg` 同 catalyst 下非本资产的 `canonical_symbol`），**仅消费已有二阶数据**；无数据则不渲染（不臆造传导标的）。
- **护栏**：`workbench/test_major_event_alert.py` 由 30/30 扩到 **45/45**（新增第 9 节：三模块存在性、直接度规则双向样例、二阶有/无数据两态、内部术语已清除、复验三项、价格尾零三项）；`py_compile` 通过；「非交易建议 / 不含交易档位」原护栏零回归。
- **复验收口（`audit_重大事件邮件_传导逻辑改动复验_4fb0c59_2026-09-26.md`，本次提交）**：独立复验确认 `4fb0c59` 落地（三段式：源码/注入 39-0/SQL 红线未踩），并开出 2×P2 + 1×P3，本轮按建议收紧直接度规则（仍仅输出层，零 DDL）：
  - **P2-1 子串误命中** → 新增 `_mentions_token()`：ASCII 词用词边界匹配（`(?<![a-z0-9])…(?![a-z0-9])`），`ETH ⊄ ETHEREUM`、`GPT ⊄ CGPT`；CJK 名称仍子串匹配。
  - **P2-2 动作词偏宽** → 动作词表移除最歧义的「支持」；新增承载角色判据（token 紧邻「链/网络/主网/公链/生态/链上」→ 生态间接），封死「某协议支持 SUI 链」误判 direct。
  - **P3 大小写敏感** → 匹配前统一 `lower()`，`SUI`↔`Sui` 均命中。
  - 全量回归：`test_major_event_alert` 45/45、`test_fast_alert_audit_rest` 61/61、`test_fast_alert_ai_veto` 46/46、`test_catalyst_channel_dedup` 22/22 全绿。
  - **旧项闭环**：`_fmt_price` 新增 `_trim_trailing_zeros()`，定点小数去掉无意义尾零（`0.42830000 → 0.4283`、`620.5000 → 620.5`）；极小价仍走科学计数（不回归 2026-09-22 P0 显示修复）。属全邮件共享函数，已跑上述四个通道的离线护栏确认零回归。
- **待部署**：push 后约 6 分钟 Zeabur 自动重建生效。

### 盘面异动告警邮件 3 封审计处置（审计_盘面异动告警邮件_3封_2026-09-26，2026-09-26，本次提交）

来源：`audit_盘面异动告警邮件_3封_2026-09-26.md`（样本 PROMUSDT / DASHUSDT / ENJUSDT）。**范围经用户拍板 = P0 四项 + P1-1**；P1-2（8% 下限倒挂）/ P1-3（confidence 不吸收反向证据）/ P2-* 本轮**不动**。

- **P0-1 相悖判定口径不一致**（`_render_alert_email`）：卡片方向段与标题已用**新鲜**口径（`catalyst_dir_fresh`），但「⚠️ 共振方向以利空为主，与做多结论相悖」仍读**全量** `catalyst_dir` ⇒「不计方向」与「相悖警告」同框自相矛盾。改读新鲜口径，且新鲜三项全为 0 时改印中性说明「ℹ️ 共振方向无新鲜条目，未参与结论」。
- **P0-2 强度分吃陈旧旧闻**（`_alert_strength`）：共振加成 ×1.15 / 相悖惩罚 ×0.75 同样改读 `catalyst_dir_fresh`（陈旧旧闻不加不扣）。DASH 实况：全陈旧利空不再把强度从 61.1 打到 45.9。
- **P0-3 新鲜度对「预定动作」系统性误杀**：`biz.asset_catalyst` 无生效日列，而「下架/移除/上线/解锁/升级/减半…」类新闻**发布日必然早于生效日** ⇒ 3 天阈值把 09-25 已生效的币安下架（ENJ）判成「陈旧、不计方向」。新增 `_SCHEDULED_ACTION_RE` / `_is_scheduled_action()`：命中预定动作语义者不参与陈旧剔除（7 天查询窗不变，不会造成无限新鲜）。
- **P0-4 美股 ticker 串台**（`workbench/catalyst/linker.py`）：`DoorDash（NASDAQ: DASH）` 与纽约市和解的新闻被连到加密 Dash。新增 `_EQUITY_TICKER_COLLISIONS`（DASH/APT/SUI/SOL/TRX/LINK/STX/AR/OP，兜底清单非全量）并入同名消歧门禁：撞名 symbol 需正文含**加密语境**（`has_crypto_context`）才认。门禁在**查库前**拦下（探针断言 `conn.calls == 0`）。
- **P1-1 跨语种转载未去重**：ENJ 的**同一**币安公告实为 **4** 条（火星财经·中 / PANews·中 / ChainCatcher·中 / 英文原文），`_norm_title` 归一后全不相同 ⇒「净空 4」虚高四倍。改为按 `(交易所|动作, 币种清单)` **实体**二次合并（`_catalyst_entity` + `_same_catalyst_batch`）。踩过两个坑，均已固化进注释与探针：
  - **① 必须用 `body_text` 而非 title/ai_summary**：源站把标题截到 83 字，清单断在「…AIXBT/USDC、DOLO/US…」（英文版整段清单都没进标题）⇒ 用标题算实体会得到 4 个互不相等的清单，一条也合并不了。
  - **② 清单判等不能用集合相等、也不能用 `\b`**：正文**同样是截断的**（四版分别含 7/6/7/7 个交易对，短者正是长者的前缀）⇒ 改「同源截断 ⇒ 短者是长者的前缀」（长度 1 要求完全相等，避免「同首项不同批次」误并）；而 `\b` 走 Unicode 词字符判定，`…TNSR/USDC及 TURTLE/USDC` 处判不出边界会漏掉 TNSR、清单错位一位，故结尾改用 `(?![A-Za-z0-9])`。
- **只读 prod 复验（本轮修复后）**：ENJ `catalyst_raw=4` → `catalyst_dir={'bearish':1}`、`catalyst_dir_fresh={'bearish':1}`、`catalyst_stale=0`、`catalyst_all` 1 条（达成审计验收「净空 1」）；DASH `catalyst_stale=1`、`catalyst_dir_fresh` 全 0 ⇒ 假利空不再触发相悖警告与 ×0.75 惩罚。
- **新增离线探针**：`workbench/test_scan_alert_audit_20260926.py`（43/0 全绿，含 P0-1~P0-4 + P1-1 + `**` 护栏）。回归零失败：`test_scan_alert_audit_deepdive`、`test_scan_alert_header_regime`、`test_scan_scenario_label`、`test_scan_l1_closed_bar`、`test_highlight_audit_20260924`、`test_scan_alert_remaining`。
- **遗留（需用户授权，本轮未做）**：DASH 那条 DoorDash 存量**脏关联**仍在 `biz.asset_catalyst`（清理属 DELETE 数据操作）；`scripts/bin/backfill_catalyst_links.py` 内联的 linker 副本未同步 P0-4 门禁。
  → 两项遗留已在下一节完成。

### 美股 ticker 撞名「存量脏关联」清理 + 门禁扩容（审计_盘面异动告警邮件_3封_2026-09-26 遗留项，2026-09-26，本次提交）

来源：上节 P0-4 的两项遗留，经用户授权执行（拍板「DASH 清理深度 = **核心闭环**」「同类 = **扩清单 + 清存量**」）。

- **`backfill_catalyst_links.py` 内联 linker 副本补齐门禁**（commit `155917a`）：该文件自带的 `map_pairs_to_asset_ids` 副本缺 `context_text` 形参，而调用处传 `context_text=ctx` ⇒ 一跑即 `TypeError`，门禁**形同不存在**、重跑会把 P0-4 刚修掉的脏关联写回。已补齐两套集合 / `_CRYPTO_CONTEXT_RE` / `has_crypto_context()` 与签名，并在循环内加门禁。探针新增「防漂移」段（AST 校验两处集合内容一致、正则逐字一致、形参在场）。
- **门禁正则四条硬约束**（均只读复算 prod 后踩出，已固化进 `linker.py` 注释）：
  - ① 词表边界一律 `(?<![A-Za-z])` / `(?![A-Za-z])` 而**不是** `\b`：中文也是 Unicode 词字符 ⇒ `Solana于9月18日将目标出块时间…`（cid 6173）里 `\bsolana\b` 判不出词尾，**真 Solana 新闻被误杀**。取「非 ASCII 字母」而非「非字母数字」是为了保住复数（`tokens`/`wallets`/`stablecoins`）。
  - ② cashtag 支必须要求「至少一个 ASCII 字母」：否则 `$131.5 million` / `$163.3 million` / `$95.72 billion`（cid 10473 DoorDash / 13616 BlackBerry / 14272 Costco 正文）会被当成 `$131`/`$163`/`$95` 型 cashtag ⇒ **美股财报稿直接过闸**，P0-4 白修。
  - ③ 词表须补撞名 symbol 的**加密侧项目名**（`tron`/`chainlink`/`optimism`/`aptos`/`arweave`/`starknet`/`stacks`/`bouncebit`/`bedrock`/`myshell`/`blynex`/`distribute.ai`）+ 加密专属信源 + `SYM/USDT` 行情口径；**不得**纳入 equity 侧高频词（etf/shares/revenue/earnings/settlement/stock/analyst…）。
  - ④ 该正则**同时**服务商品 gate（`_COMMODITY_AMBIGUOUS_SYMBOLS`），故**不得**纳入宏观/商品稿高频词：实测加 `bitcoin` + `上涨|下跌|涨幅|行情|突破|新高|市值` 后，30 天内商品 gate 有 **9 条由「拦下」翻转为「放行」且全是噪音**（`金价下跌推动中国黄金进口` cid 10154/10160、`BTC/XAU 比率` 7433/7401/7405、`Bitcoin and gold are hedges` 4255、`美联储加息/油价` 7335）。宁可留残差。
  - 复算：修 ① 前 30 天 358 条撞名 catalyst 被误杀 **32** 条（SOL 12 / TRX 5 / APT 4 / SUI 4 / OP 3 / LINK 2 / STX 2）；加固后 383 条中丢弃 38 条（≈23 条为正确的美股噪音，**已知残差 ≈9 条/30 天**为纯行情快讯，见下）。
- **`_EQUITY_TICKER_COLLISIONS` 扩容 9 → 16**（每条都有实物 cid）：新增 `BB`(BlackBerry·13616) / `BX`(Blackstone·4164/13650) / `COST`(Costco·14272/5343) / `DIS`(Disney·13989) / `SHELL`(Shell plc·12572) / `BR`(Broadridge；另有**国家代码** BR·13722) / `UBER`(Uber·1642；亦与「Uber Technologies, Inc. • Robinhood Token」同名)。`linker.py` 与 `backfill_catalyst_links.py` 内联副本同步。
- **prod 存量清理（核心闭环，单事务，已提交）**：`asset_catalyst.asset_id → NULL` **21** 行 / `DELETE catalyst_asset_link` **20** 行 / `DELETE catalyst_impact` **25** 行 / `DELETE catalyst_resonance` **26** 行，事后残留全 0。保留 `catalyst_signal`（历史信号，已 invalid）与 `catalyst_second_order`（引用的是**其它**资产，属另一笔账）。涉及 cid：`633,847,1296,1642,2334,4164,4337,4906,5343,5453,6880,10473,12096,12326,12485,12572,13616,13650,13722,13989,14272`（另 `3831` 只删 COST 侧行 —— 其 `asset_catalyst.asset_id=9944`(CL, **Crude Oil Derivatives**) 属**另一类撞名**，不在本轮范围）。
- **探针扩容 43 → 65 断言**：新增 P0-4「反例集」（10 条必须拦下 + 7 条必须放行，ctx 逐字取自 prod）与「防漂移」段。回归零失败：`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_header_regime`(135/0)、`test_scan_scenario_label`(48/0)、`test_scan_l1_closed_bar`(16/0)、`test_highlight_audit_20260924`(67/0)、`test_scan_alert_remaining`(16/0)。
- **已知残差（未修，属设计取舍）**：`SOL 升破 110 USDT`(cid 6742)、`SOL 上涨突破 110 美元`(6763/6781)、`SUI 短时触及 1 USDT`(8895)、`Analyst Ali said SUI rebounded…`(14853) 等**纯行情快讯**因不含加密专属词被门禁丢弃（≈9 条/30 天）。修它必然要放进「行情」类通用词，而那样商品 gate 会翻车（见 ④），故按「宁可多列」保留。
- **兜底扫描（`asset_catalyst` 侧，30 天，覆盖无 impact 行的 catalyst）**：另见 `cid 4804`（SK Hynix/Intel 盘前）、`cid 13755`（Delivery Hero/优步）亦为美股/外卖噪音，但其 `asset_id` 已为 NULL 且**无** impact/link/resonance 行 ⇒ **无可清理**；新门禁已在入库侧拦下。
- **未做**：P1-2 / P1-3 / P2-*（按用户拍板范围不动）；`_EQUITY_TICKER_COLLISIONS` 仍为**兜底清单、非全量**（完整清单需用 `core.asset.canonical_symbol` 与美股 ticker 全量比对产出，需连库，另立项）。

### 高亮信号「回测 → 权重」反馈闭环 刀2（审计_高亮信号_确定性审计与优化_2026-09-26 §六·P0-B，2026-09-26，本次提交）

来源：`audit_高亮信号_确定性审计与优化_2026-09-26.md` §六 刀2（该审计唯一被显式缓办的 P0-B）。工单口径 = **先解 OBI-OPT-BACKTEST-001 三阻塞，再打通「回测 → 权重」**。**上级（刀1/3/4/5）已于 `292e82e` 落地，本轮补其缺失的反馈闭环**。

**用户三项决策（AskUserQuestion 拍板，已固化进代码注释）**：① 降权落点 = **分数×0.6 + 档位封顶 MED**（衰减分数但兜底到 MED 门槛，卡片以 medium 保留，不误删整类）；② 不可回测类 = **豁免：不降权、保留 HIGH**（聚合/硬数据极值无样本 ≠ 表现差）；③ 回测调度 = **暂不调度，先手动跑通**（本轮只打通链路 + 手动产出一份校准值）。

- **D3 快照读取拖垮回测（根因）**：原实现逐快照 `SELECT payload`（25 份 × ~500KB ≈ 12.5MB）在远程库上单次回测十几分钟且经常跑不完 —— 这才是「回测无法稳定产出」的直接原因，而非样本不足。改为 SQL `jsonb_array_elements(s.payload->'opportunity_list'->'opportunities')` 只抽 `signal_type/target/direction/conviction_tier/conviction_strength` 小字段；配 `_BTC_CACHE`（按 entry/exit 日缓存基准）与 `_EXTERNAL_CACHE`（按 symbol+date 缓存外呼、上限 `MAX_EXTERNAL_LOOKUPS=200`）。**实测耗时 >10min（跑不完）→ 2min30s**。
- **D3 口径退化（假 0% 命中率）**：`SIGNAL_HORIZONS["token_unlock"]` 原为 `[0,1,3]`，h=0 使 entry==exit ⇒ pnl 恒 0 ⇒ hit_rate 假 0%。改 `[1,3,7]`，并补 `etf_flow/conflict_game/price_surge/price_crash/price_volume_surge/volume_surge` 显式档 + `DEFAULT_HORIZONS=[1,7]`；新增断言「所有类型持有期均无 0」防同型复发。
- **D2 缺失样本静默丢**：`NOT_BACKTESTABLE` 只登记 2 个类型，实际还有 4 个聚合类型 + **56 条 `signal_type` 为 None** 在静默丢弃。现 skip 按 `no_signal_type / not_backtestable / dup / no_price` **四类计数并进入返回值与库表**（`skipped_dup` / `skipped_no_price` / `skipped_not_backtestable` 三列留痕）；`NOT_BACKTESTABLE` 扩到 11 项，`NOT_CALIBRABLE={mvrv_deep_under}`，`EXEMPT_SIGNAL_TYPES = 两者并集`（已断言两集合无交集，防 gate 归属歧义）。
- **D1 日价缺口**：新增 `_fetch_price_map()` 一次批量取价，口径 `SELECT DISTINCT ON (UPPER(a.canonical_symbol), m.market_date) ... ORDER BY …, a.asset_id`（同 symbol 多 asset 取最小 asset_id，与原实现同口径）+ 排除 `%Wrapped%`/`%Bridged%`（腐败价源）；`_is_symbol_target()` 拒聚合类 target（`Base 链`/`2 币 MVRV 极度高估`/`恐贪指数极度贪婪`/`AI & Big Data` 均判 False）。实测 30 天 245 条机会中 `no_price=43`（仍存缺口，但已留痕可追溯，不再静默丢）。
- **门控纯函数 `_gate_for(signal_type, n, hit_rate) → (gate, weight_factor, no_high)`** 六分支：豁免×2 → `(exempt_*, 1.00, False)`；`n < MIN_SAMPLES(30)` → `preliminary`；`n ≥ 30 且 hit_rate < HIT_RATE_MIN(0.5)` → `calibrated_low`；否则 `calibrated_ok`。**注意 preliminary 也衰减**（样本不足同样不可信），命中率高的 `github_activity`（60.7%，n=28）照样 ×0.6 不进 HIGH。
- **落表 `biz.signal_type_calibration`**（迁移 `fix_070_signal_type_calibration.sql`，已应用到 prod）：幂等键 `UNIQUE (signal_type, horizon_days, window_end)`，`ON CONFLICT DO UPDATE`；索引 `(signal_type, window_end DESC)`。CLI 新增 `--write`，返回体加 `window` / `skipped` / `calibration_rows_written`。**DDL 执行后立即 `conn.commit()`**（沿用工作记忆教训：`ALTER/CREATE` 持 `AccessExclusiveLock`，不及时 commit 会阻塞 `biz` 表读达十几分钟）。
- **消费端 `macro_market.py`**：新增 `_load_signal_type_calibration()`（`SELECT DISTINCT ON (signal_type) ... ORDER BY signal_type, window_end DESC, horizon_days ASC`）+ `_ensure_calibration_loaded()`（**惰性 + TTL `_CALIB_TTL_SEC=1800`**，不在 import 期打 DB，失败记时避免 DB 故障时反复重试）+ `_signal_type_calibration()`（无条目 / `factor≥1.0 且 not no_high` → 返回 None，与不校准严格等价）。落点在 `_push_opportunity`：`score = max(int(round(score × weight_factor)), med_min)`（**保卡下限**：衰减后不判 LOW、不删卡），档位 `no_high` 时封顶 MED，并写 `calibration` 溯源字段 + `calibration_note` + `tier_demote_reason`。**降档溯源判据从「仅当原判 HIGH」放宽为「衰减前分数 ≥ high_min 即记」**——否则 `86×0.6=52` 被保卡下限抬回 55、tier 直接判成 MED，降档原因会漏记（本轮实测踩到）。
- **未分类 target 的豁免语义**：豁免类型即使 0 样本也**留痕落表**（`sample_count=0`，`horizon_days` 兜底 1），使「豁免」是显式记录而非「查不到 = 不校准」，避免将来误认为数据缺失。
- **prod 实测（只读 + 一次写表）**：`--days 30 --write` → `snapshots=25 window=['2026-08-31','2026-09-25'] total=245`、`skipped={'no_signal_type':56,'not_backtestable':101,'dup':21,'no_price':43}`、**写入 19 行**。衰减 6 类：`whale_flow`(n=67, 47.8%)/`etf_flow`(44, 40.9%)/`token_unlock`(51, 45.1%) → `calibrated_low`；`github_activity`(28)/`conflict_game`(5)/`kol_onchain`(5) → `preliminary`。恒等 13 类：`catalyst`(45, 77.8%, `calibrated_ok`) + 12 个豁免。**macro 消费端连库加载 19 条目 → 6 类 decay=YES、13 类恒等** 与落表逐条对齐。
- **HIGH 数下降（DB 侧投影，最新快照 `2026-09-25`）**：按 `no_high` 投影 **23 → 14（降 9）**，逐条来源 `token_unlock 5 / whale_flow 2 / etf_flow 1 / conflict_game 1`；`catalyst 5` 与豁免类（`chain_inflow 3` / `narrative 3` / `funding`/`fng_extreme`/`mvrv_deep_over` 各 1）保留 HIGH。**前端计数须待 Zeabur 重建后复验**（本刀改了 macro 运行时评分，非展示层；`push ≠ 线上生效` 见 8-27/8-28 教训）。
- **新增离线探针 `workbench/test_signal_type_calibration_20260926.py`（73/0）+ 回归**：`test_highlight_determinacy_20260926.py` 钉死为「无校准」基准（`_CALIB_LOADED_AT=inf`，保持纯离线）后 **72/0**。覆盖 D1 取价 SQL 口径 / D2 豁免集合与 target 形状 / D3 h≠0 / `_gate_for` 六分支 / 落表键与列 / 迁移 DDL 保真 / macro 无校准不变 / 衰减+保卡+封顶+降档溯源 / 豁免不衰减 / 消费端源码守卫。
- **未做**：① `fix_070` 之外的存量脏数据未清（`no_price=43` 属日价源缺口，非本刀范围）；② 回测**未接调度**（按用户决策「先手动跑通」，故 `biz.signal_type_calibration` 目前为一次性快照，需人工重跑 `backtest_opportunities.py --days 30 --write` 刷新；**该 ② 已在同轮复验处置中闭环，见下节 §五#5**）；③ 前端 HIGH 计数复验待部署后补。

### 刀2 反馈闭环复验处置（核验_刀2_回测闭环_47bcb4d_2026-09-26 §五，2026-09-26，本次提交）

来源：`核验_刀2_回测闭环_47bcb4d_2026-09-26.md`。复验结论 = 源码核验 ✅、注入测试 ✅（探针 73/73 + 回归 72/72 独立实跑）、**线上 runtime 机制生效但验收数字不成立**。本轮处置其 §五 遗留清单 #1~#6。

- **§五#2「核实覆盖面」（先跑 SQL 定性，结论已得）**：prod 只读 `SELECT signal_type, sample_count, hit_rate, weight_factor, gate, no_high, window_end FROM biz.signal_type_calibration` 返回 **19 行** → 复验点名的 4 类**全部在表**，但语义分两种：`catalyst` = `calibrated_ok`（样本 45 / 命中率 77.8% / factor 1.00 / no_high False）**有回测背书**；`chain_inflow`（3 条 HIGH）/ `fng_extreme`（1 条）/ `narrative`（1 条）= `exempt_not_backtestable`（样本 0 / hit_rate None）**从未被回测**。即复验猜测的 `missing_calibration` 情形实际不存在，真实情形是「豁免未测」——**当时 8 条 HIGH 席位中 5 条坐在从未回测的类型上、3 条有真实背书**，「HIGH=高确定性」在类型层面只对了一半（这与刀2 的设计取舍一致：聚合/非可交易 target 无样本 ≠ 表现差，故豁免保留 HIGH，但必须可被外部看见）。
- **§五#1「calibrated_ok / missing 不可分辨」（P1，已修）**：根因 = `_signal_type_calibration` 对「表中无此类型」与「calibrated_ok（factor≥1 且不封顶）」**都返回 None** ⇒ API 上完全同形，HIGH 卡无法自证有无回测背书。修法 = **不改既有契约**，在 `macro_market._push_opportunity` 内另记一层 `opp["calibration_status"]`（新增函数 `_calibration_status()`，四态）：`calibrated_ok`（已回测且背书通过）/ `decayed`（已回测但结论差、已降权）/ `exempt_not_backtestable`·`exempt_not_calibrable`（表中有行但从未被回测）/ `missing`（表中无此类型）。字段 = `{state, gate, calibrated, note[, sample_count, hit_rate, window_end]}`；原 `opp["calibration"]` 仅衰减分支保留（**不污染卡片**，既有断言 `"calibration" not in hi/ex` 全部保持）。前端 `templates/index.html` 的 `renderSignalItem` 按 `state` 渲染角标：`✔ 已校准背书`（绿）/ `↓ 已降权`（黄）/ `未校准`（灰，含 tooltip 说明「无回测背书」）。
- **§五#3「验收数字口径统一」（P1，方法论，已固化）**：复验用今日实时池按 `high_min=70` + `before` 反算得 **不衰减 HIGH=13、实际 HIGH=8 → 降 5**（etf_flow 3 + conflict_game 2），与自报的「23→14 降 9」不符——**两者非同一输入集**（自报取历史快照固定投影，复验取今日实时池）。**约定：今后凡「降档数 / HIGH 条数」的前后对比，必须锚定「同一份快照 before/after」或「同一份实时池按 before 反算」，禁止跨输入集互相印证**；另注意 `token_unlock` 峰值 67、`whale_flow` 峰值 64 **本就够不到 70 线**，不得计入降档贡献（自报把二者计入「降 5+降 2」不成立）。
- **§五#4「保卡下限坍缩」（P2，已修）**：保卡下限把所有被衰减的卡压成同一个 `med_min`（`58→55` 与 `78→55` 落点相同），MED 内部相对序退化为随机。修法 = 衰减分支新增 `opp["raw_before_decay"] = before`（衰减前原分），并在 `select_highlight_signals._sort_key` **末位**加入该值作同分 tie-break（`raw = raw_before_decay if not None else score`）。**主序仍是 `decayed_score`/`conviction_score`**，故「低分卡不被高分卡反超」；无校准字段的卡退化为 `raw = score`，**排序结果与改动前逐条一致**（离线实测三条断言：同分按 raw 降序 / 无 raw 保持输入序 / 分数仍为主序）。
- **§五#5「回测未接调度」（P2，已修）**：新增 `scripts/bin/run_signal_type_calibration_backtest.py`（薄包装，按 `recompute_unlock_pressure.py` 同款 workbench 目录探测，`subprocess` 调 `backtest_opportunities.py --days N --write`）+ `scheduler.py` 新增周级任务 `signal_type_calibration_weekly`（`0 5 * * 0` = 每周日 05:00 北京，避开 `data_sync_daily` 06:30 早高峰；category `core`）。落表键 `(signal_type, horizon_days, window_end)` 幂等，重跑只新增窗口、不改历史；消费端按 `window_end DESC` 自动取最新窗口。**未加入 `scheduler_watchdog.KEY_JOBS`**（该白名单只收关键日频任务，周频阈值不适用）。
- **§五#6「D1/D2/D3 三阻塞正确性」（独立工单，未做）**：复验仅验「闭环效果」，未逐条复查 `backtest_opportunities.py` 621 行改动的取价/skip 计数正确性。**保留为独立工单**（需独立复核 `DISTINCT ON` 取价口径与 skip 四类计数在 prod 上的逐条对账），本轮不做。
- **探针**：`test_signal_type_calibration_20260926.py` 由 **73 → 89 断言**（新增四态留痕、`raw_before_decay` 排序还原、前端角标渲染与源码守卫）。**回归零失败**：`test_highlight_determinacy_20260926`(72/0)、`test_macro_market_board_tier2`(36/36)、`test_macro_market_p1_upstream`(24/24)、`test_highlight_audit_20260924`(67/0)、`test_highlight_alert`(68/0)；`node --check`（`index.html` 抽取 script + `{{...}}` 占位替换）与 `py_compile` 通过。

### 盘面告警邮件 d442bd6 复验处置（核验_盘面告警邮件修复_d442bd6_2026-09-26 §三 NEW-1/NEW-2，2026-09-26，本次提交）

来源：`核验_盘面告警邮件修复_d442bd6_2026-09-26.md`。该报告结论 = 上一轮 P0-1~P0-4 + P1-1 **五项全部通过、可关闭**（runtime 侧待下一封告警邮件补佐证），但新发现 2 个问题：**NEW-1（P1，由 P0-3 引入）豁免词过宽**、**NEW-2（P2，既有分支遗漏）新鲜仅中性时印「多空持平」**。报告 §四「backfill 内联副本未提交」/ §六「DASH 存量 DELETE」两项**已过期**（分别已在 `155917a`/`a9e7901` 完成），本轮不再动。

- **NEW-1 根因**：P0-3 的 `_SCHEDULED_ACTION_RE` 只是「动作**词**」表（`upgrade`/`list`/`上线`/`解锁`…），而这些词在加密新闻里极高频 ⇒ 仅凭词形判豁免，等于把「The network upgrade improves throughput」「Top 100 holders list published」「链上活跃度升级」「该协议上线三个月」这类**陈旧评论/汇总**也判成「发布即预告未来动作」，在 7 天查询窗内**恒新鲜** ⇒ 重新驱动 ⚠️ 相悖警告与 ×0.75 —— P0-1/P0-2 刚消灭的「旧闻驱动判定」在豁免类目上成规模回归（复验报告 11 条探针中 7 条被误豁免）。
- **NEW-1 修法（`_is_scheduled_action`：动作词 **∧** 具体性证据）**：证据 = ① **实体**（`_catalyst_entity` 非 None，即「交易所+动作+交易对清单」的真实上下架公告）∨ ② **将来语义**（新增 `_SCHEDULED_FUTURE_RE`：`将于/将要/即将/届时/倒计时/时间确定/定于/拟于/计划于`、`将(于|在)?+动作动词`、`scheduled to/set to/upcoming/next week`、`will (be) delist|list|remov|upgrad|unlock|…`、`to be delisted|listed|…`）。
- **刻意不采纳报告建议的两点，均有 prod 实测支撑**：
  - **不用「裸日期」当证据**：中文稿发稿戳（「PANews 9月22日消息」）必带 `X月X日` ⇒ 用日期当证据等于没收紧（同一 263 条：裸日期版留 **108** 条）。
  - **输入仍用 `title + ai_summary`，不用 `body_text`**：与 P1-1 取数**不同源是有意的** —— P1-1 要**完整正文**才能拿币种清单做转载合并，而本判据只问「标题是否在预告将来的动作」，正文越长越引入无关日期/动词（同一 263 条：摘要版留 **57** 条、正文版留 **86** 条，多出的 29 条集中在「今日要闻提示」「要闻预告」这类资讯汇总）。
- **prod 只读量化（复验报告的硬要求；口径 = 近 7 天查询窗内、已 >3 天即 `published_at < now-3d` 的现行豁免 263 条）**：收紧后 **263 → 57（清掉 78%）**。保留的 57 条为真·预定动作（解锁预告「将于…解锁 / will unlock / is scheduled to unlock」、交易所上下架公告（实体）、「将于/将某时上线或关闭」、「倒计时」类升级）。**报告担心的「英文截断版（无交易对、无日期）」回退未出现** —— 该版（cid 10091）正文含「the exchange **will remove** and cease trading」⇒ 命中将来语义；ENJ 的 4 版转载（3 中文 + 1 英文）全部仍豁免，P0-3 的原始验收（09-25 已生效的币安下架计入方向）不变。
- **NEW-2 根因**：卡片方向段原判据 `fb == 0 and fbear == 0 and fneut == 0`，只在**三项全零**时才印「（剔除陈旧后无新鲜条目）」，否则走 `else` 印「多空持平」。新鲜条目**只有中性**（复验报告合成场景 `{0多,0空,2中}`）时方向同样不存在，却印出「多空持平」（读作「新鲜的多空均衡」，与 N-786-3 立的规矩直接冲突），且因 `f_total=2≠0` 连 ℹ️「未参与结论」也不印 ⇒ 读者看到「净空3 + 多空持平」，既无警告也无说明。
- **NEW-2 修法**：判据只看方向 → `if fb == 0 and fbear == 0`；文案按 `fneut` 分两支：`fneut` 非零时印「（剔除陈旧后**无新鲜方向**，仅 N 条中性）」（保住「全陈旧 ⇒ 无新鲜条目」的旧文案不失真），否则仍印「（剔除陈旧后无新鲜条目）」。
- **探针 65 → 83 断言**（`workbench/test_scan_alert_audit_20260926.py`）：新增「复验 NEW-1」段（7 条非预定动作必须不豁免 + 6 条真·预定动作必须豁免（实体/将来语义两通道各取样）+ 实体通道英文公告）与「复验 NEW-2」段（合成 `{0多,0空,2中}` 不得印「多空持平」、须印「无新鲜方向」且如实披露「仅 2 条中性」；反向对照 `{0多,1空,1中}` 仍印「净空1」）。**回归零失败**：`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_header_regime`(135/0)、`test_scan_scenario_label`(48/0)、`test_scan_l1_closed_bar`(16/0)、`test_highlight_audit_20260924`(67/0)、`test_scan_alert_remaining`(16/0)。
- **已知残差（已于本轮收口，见下一节）**：英文解锁预告「… and MBG **will see** token unlocks」（无具体日期、无 `will unlock`）不在将来语义表内 ⇒ 该条判陈旧。**本轮只读量化推翻其归因**（标题被源库截断，`will see` 根本没入库），真缺口是中文「将迎来」；落地后 prod 净增豁免 5 行，资产 7760/8774/8801/3994。
- **未做**：runtime 侧佐证（需下一封盘面告警邮件落地后核对相悖警告/强度分是否与新鲜口径一致）；报告 §五 提到的 `cmc_quote_snapshot` 停滞告警属调度侧另一笔账。

### 一键投研页 P0 修复复验处置（核验_一键投研页P0修复_2cc1e55_2026-09-26，2026-09-26，本次提交）

来源：`核验_一键投研页P0修复_2cc1e55_2026-09-26.md`。该报告结论 = 4 项 P0 里 3 通过、**P0-1 半数失败**（只修了日表写路径，漏了实时快照表），另新暴露 2 项。本轮处置其遗留 #1/#2/#3/#4/#6，#5 与 #7 按报告界定另立项。

- **#1 实时快照表 FDV 退化（最要紧，报告根因定位正确）**：`repair_degenerate_fdv` 只写 `biz.asset_market_daily`，而 `db_stats` 的 `structured_metrics.market` 读的是 `src_cmc.cmc_asset_quote_snapshot`，**两张表两条写路径**。**采纳「读时兜底」而非写时改快照表**：`src_cmc.*` 是 CMC 原样镜像，写侧改写会毁掉审计痕迹（与日表 FDV 修复的既有原则一致）。新增 `_corroborated_max_supply(asset_id)`，守卫与写侧**逐字同口径**（`hist.cmc_hist_max >= tok.max_supply * 0.98`）；命中退化（`fdv≈market_cap` 或 NULL）且印证 max_supply 明显大于流通量时，用 `price × max_supply` 重建并留痕 `fdv_basis=price_x_corroborated_max_supply`。**prod 实测（11114）**：CMC 快照 `max_supply == circulating_supply == 683,710,367`（CMC 自我下调），故报告建议的「用快照自身 max_supply」**不可行**，必须走 tokenomist + 历史印证。
- **#2 drift 无条件不变式**：原判据只看「与 thesis 快照的相对偏差」，两个都错的值（旧 431,441,804 / 实时 430,970,552）偏差仅 0.1% ⇒ 永不告警。新增**只依赖实时值**的判据：`fdv≈market_cap` ∧ `circulating < 0.98×佐证max_supply` ⇒ `kind=fdv_degenerate` 无条件 stale。**门槛刻意保留「流通<最大」这一必要条件**——报告的 08-17~08-21（`circ==max==1e9`）是 fdv==mcap 的数学真值，不得误报（已写入断言）。前端补 `fdv_degenerate` 分支渲染（否则会印「偏离 undefined%」）。
- **#3 circ=0 / market_cap NULL（报告只说 2 天，实际 45 天窗口 12.7 万行）**：根因 = CMC 对低排名币把 `circulating_supply` 与 `market_cap` **一起**编码成 0，解析层只把 `market_cap` 的 0 归一为 NULL、`circulating` 仍是字面 0 ⇒ 日表出现「circ=0 且 mcap=NULL」。修法两层：① 写入侧 `INSERT` 加兜底（`circ ← total_supply`、`mcap ← price × 兜底供应量`）；② 新增 `repair_zero_circulating()` 并挂到主 ETL 与 `--repair-only`。**prod 已执行 `--repair-only --days 45`：流通量兜底修 106,173 行、FDV 修 1,308 行**。PONS 40 天窗口复验：`market_cap NULL=0 / circ=0 / fdv NULL=0`。**残差 20,506 行**（其中 20,346 行 CMC 连 `total_supply` 都没有）无法推导，按项目铁律保持缺失；实测「前一日 circ 回填」仅能再救 120 行，不值得引入 LATERAL。
- **#4 early-window 骤降定性**：只读复核结论 = **CMC 口径修正，非真实销毁**（① 08-18~08-23 CMC 直接返回 0/0 属缺数据；② 修正后 714,672,621 与 tokenomist `circulating_supply=712,104,762` 仅差 0.36% ⇒ 早期 1e9 是误报）。已落到 P0-3 裁决注释，并显式写明「08-17~08-21 的 fdv==mcap 属真值，不得按退化回改」。
- **#6 部署可用性**：复验时的 502 已自愈，本轮实测 `GET /` = **200**（属部署中断）。
- **#5 age_hours（533.2 > 168）**：本轮已单独处置，见下一节（根因不是「需重抓一次」，而是采集链路上**已抓过的行根本没有刷新路径**）。**#7（P0-1/P0-2 证据分级+引用索引、P1/P2 共 11 项）按报告界定未纳入**。
- **新增离线探针 `workbench/test_fdv_degeneracy_20260926.py`（14/0，纯离线不连库不连网）**：覆盖 #2 不变式五种边界（含「流通==最大」真值不误报、无印证 max_supply 不误报）+ #1 源码级守卫口径 + #3 ETL 兜底存在性 + 前端分支。**回归零失败**：`test_highlight_determinacy_20260926`(72/0)、`test_signal_type_calibration_20260926`(73/0)、`test_scan_alert_audit_20260926`(83/0)。
- **线上验收（已复验通过）**：`GET /api/research/11114/notebook?refresh=1` → `structured_metrics.market.fdv_usd = 630,340,818.06`（= 0.63034 × 1e9，ratio=1.462，原为 `430,970,552.34` ratio=1.0），`fdv_basis = price_x_corroborated_max_supply`、`fdv_corroborated_max_supply = 1e9`；`thesis.drift = {checked:[price,market_cap,fdv], reasons:[], stale:false}`（不再误判，且 fdv 已纳入 checked）。
- **踩坑留档（务必先只读复算再动手）**：报告给的两个「建议修法」实测都不可行 —— ①「用快照自身 max_supply > circulating 兜底」：CMC 已把 `max_supply` 下调到 == `circulating`（均 683,710,367），条件恒 False；②「重跑 ETL `--days 45` 修 circ=0」：会新增 7,668 行（08-19~08-21 只有 `cmc_historical` 无 `cmc` 行），可能改动报告明令不可动的 08-17~08-21 展示值 ⇒ 改用**外科式** `repair_zero_circulating()` 只动 circ=0 脏行。

### 解锁数据陈旧刷新路径（复验_一键投研页P0修复_2cc1e55 §#5，2026-09-26，本次提交）

来源：同报告的 #5（`data_freshness.unlock.age_hours=533.2`，要求 <168）。报告界定为「tokenomist 采集链路，需独立重抓任务」；本轮只读复算发现根因更深一层。

- **根因（报告未定位到）**：`phase_chain_token_unlocks_batch.get_pending_assets` 用 `_PENDING_EXCLUDE` 的 `NOT EXISTS ... u.crawl_status='ok'` **永久排除已成功的行** ⇒ 已抓过的资产**再无任何刷新路径**，`updated_at` 一旦落库就永久冻结。实测 PONS(11114) 停在 `2026-09-04`（533h），且**新候选已归零**（每日 07:00 任务实为空转），故 `data_freshness.unlock` 恒 stale —— 这不是「重抓一次」能解决的，必须补刷新通道。
- **修法（方案 A）**：候选查询并入「`crawl_status='ok'` 且 `updated_at < NOW() - N 天`」的刷新组，`UNION ALL` 到候选池、排在**新候选之后**（新覆盖优先），仍受 `--limit` 约束；新增 `--refresh-days`（默认 `DEFAULT_REFRESH_DAYS=5`，`0` = 关闭即回到旧行为，可用 `UNLOCK_REFRESH_DAYS` 覆盖）。`get_total_pending` 改为返回 `(新候选数, 陈旧刷新候选数)` 并在启动行打印两者。
- **排序**:刷新组按 `updated_at ASC`（**最旧优先**）。若沿用市值降序，长尾资产会被结构性饿死（与 09-26 早先修好的 `task_manager` category 饿死问题同型）；新候选组仍按市值降序，行为不变。
- **配套护栏（必需，否则刷新会毁数据）**：`phase_chain_token_unlocks.save_to_db` 原本无条件覆盖 `crawl_status` + `overview_json`/`unlock_events_json`。刷新一个 `ok` 行时若站点反爬/改版导致本次解析降级（`not_found`/`parse_empty`/`fail_timeout`），会把已抓到的时间表**抹成空**。故给 `ON CONFLICT ... DO UPDATE` 加 `WHERE crawl_status IS DISTINCT FROM 'ok' OR EXCLUDED.crawl_status = 'ok'` —— 只在「原行非 ok」或「本次仍 ok」时接受写入，与 `_mark_not_found`「不覆盖已成功的数据」同策略。
- **稳态时效口径（本次先写错过一次，探针抓出）**：到龄(D)才进队列 ⇒ 稳态刷新周期 `T = max(D, N/B)`（N=可通过门槛的 ok 行数、B=日预算），**不是** `N/B`。实测 N=269、B=100（调度 `--limit 100`）⇒ `N/B≈2.7 天`；取 D=5 天 ⇒ `T=120h < 168h` ✓（若取 D=7 则 `T=168h`，**恰好卡在边界上不达标**，故默认 5 而非 7）。
- **prod 实测（只读）**：`refresh_days=5` → 新候选 **0** / 陈旧刷新 **269**（全 ok 行 607，经 active/非稳定币/非 meme/市值≥20M 过滤后 269）；`refresh_days=0` → `0/0`（旧行为可回退）。排序实测最旧为 `2026-08-11`，`11114` 在池中。
- **验收（已通过，含端到端跑通刷新链路）**：对 11114 实跑 `phase_chain_token_unlocks.py --asset-id 11114 --save` → `crawl_status=ok`、`updated_at` 刷新为 `2026-09-26 14:56`；线上 `GET .../notebook?refresh=1` → `data_freshness.unlock = {age_hours: 0.0, stale: false}`（原 533.7/true）。注意 notebook 有 1h 快照缓存（`_SNAPSHOT_TTL_SECONDS=3600`），复验须带 **`?refresh=1`**（不是 `?force_refresh=1`）。
- **新增离线探针 `workbench/test_unlock_refresh_20260926.py`（23/0，纯离线）**：以假 cursor 捕获 SQL/参数装配，覆盖 ①刷新组并入 ②`refresh_days=0` 关闭（**注意**：不能用 `crawl_status='ok'` 字符串判关闭，主查询的 `_PENDING_EXCLUDE` 里本就有它）③门槛一致 ④排序（新候选优先 + 刷新组最旧优先）⑤`save_to_db` 降级护栏 ⑥`T=max(D,N/B) < 168h`。
- **未做 / 边界**：① 未跑全量刷新（269 条由每日 07:00 调度按预算自然消化，无需人工）；② 长尾资产（`market_cap < 20M`）仍不刷新（沿用 `MIN_UNLOCK_MCAP` 既有取舍）；③ 未查 `tokenomist` 侧 `released_pct=100 / total=1.00B` 与 CMC `circulating=683.7M` 的口径分歧（属源站数据问题，另行立项）。

### 催化剂门禁「通用词漏放行」收口（核验_催化剂门禁扩容与存量清理_a9e7901 §二C / §六 P1，2026-09-26，本次提交）

来源：`核验_催化剂门禁扩容与存量清理_a9e7901_2026-09-26.md` §二C（4 条漏放行）+ §六 P1。同报告 §三 的 NEW-1/NEW-2 已由 `e917e61` 独立收口（见上一节），本轮只做通用词架空那一笔。

- **根因（报告定性准确）**：`_CRYPTO_CONTEXT_RE` **一张词表同时服务商品 gate（`_COMMODITY_AMBIGUOUS_SYMBOLS`）与美股 gate（`_EQUITY_TICKER_COLLISIONS`）**，而表里混进了「既不指向加密、也不指向商品」的**通用词** ⇒ 补加密侧漏词与防商品/美股噪音互为翻转（零和）。报告实测 4 条：`矿工`（`澳洲煤矿矿工罢工` ⇒ COAL 翻转、`Gold miner output` ⇒ GOLD 翻转）、`上线`（`迪士尼+ 上线新剧集` ⇒ DIS 失效）、`stacks`（撞英文高频短语 `stacks of cash`）。
- **修法（按报告建议的最低成本顺序）**：① 删 `miner|矿工`（撞名清单内无矿业相关 crypto 项目，对判别贡献 ≈0，却实打实翻转 COAL/GOLD）；② `stacks` → `stacks(?!\s+of\b)`；③ 删 `上线`，并**补 `sui` 项目名**兜住其唯一代价。三处改动在 `linker.py` 与 `backfill_catalyst_links.py` 内联副本**逐字同步**（探针已锁字符串相等）。
- **③ 为何必须补 `sui`**：只读复算发现删 `上线` 会误杀 cid 6299「Foresight News 消息，Sui 生态借贷协议 Suilend 发推表示，其已上线 2.0 版本」——该条**只靠 `上线`** 命中，却是真加密新闻。补 `sui` 属既有约束 ③ 的同型补齐（`tron`/`chainlink`/`optimism`/`aptos`/`arweave`/`starknet` 已按撞名 symbol 的加密侧项目名登记，SUI 漏了）。
- **prod 只读量化（近 90 天 541 条撞名 symbol 催化剂；两阶段取数规避 `body_text` 的 `ClientWrite` 卡死）**：多变量对照 —— 仅删 `miner|矿工` 翻转 2 条；报告建议的** 拆表**（只认交易对·cashtag·交易所·加密项目名，通用词全出局）翻转 **78** 条；再去 `现货|合约` 翻转 54 条。**最终落地仅 1 条由放行翻转为拦下**：cid 11924「Copper futures hit a record $6.95 a pound…」（**商品噪音，本该拦**），且其 `catalyst_asset_link`/`catalyst_impact` 均为 **0 行**（COPPER 资产名含 `futures` 早被 `_NON_CRYPTO_NAME_SQL` 拦住）⇒ **无需 prod 存量清理**。
- **刻意不动（报告 §二C 建议 ③ 拆表属更大改动，另开工单）**：`现货` / `spot` 仍能把商品行情快讯放行（`现货黄金`/`spot silver`，近 90 天 ≈47 条），但删它会连带误杀「SOL 现货 ETF」这类**真加密**新闻（cid 3259 / 627）⇒ 同一张表下无解，须靠拆表（商品 gate 宽松 / 美股 gate 只认四类硬证据）才能两者兼得。已在 `linker.py` 约束 ⑤ 注释留档。
- **探针 83 → 90 断言**：新增「复验 a9e7901·通用词漏放行」段（4 条必须拦下 + 3 条反向必须放行：cid 6299 靠 `sui`、cid 3259 靠未删的 `spot`、`Stacks 网络完成硬分叉` 不得被 `stacks` 负向断言误伤）。**回归零失败**：`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_header_regime`(135/0)、`test_scan_scenario_label`(48/0)、`test_scan_l1_closed_bar`(16/0)、`test_highlight_audit_20260924`(67/0)、`test_scan_alert_remaining`(16/0)、`test_major_event_alert`(45/0)。
- **未做**：① 拆表（含报告 §六「提醒」的双副本单源收敛）；② §六 P2「67/0 临时探针转常驻」；③ 门禁收紧只影响**新入库**，存量撞名脏关联未重扫（本轮实测无需清，但历史行未复查）。

### 盘面告警「will see / 将迎来」残差收口（核验_NEW-1_NEW-2修复_e917e61 §八，2026-09-26，本次提交）

来源：`核验_NEW-1_NEW-2修复_e917e61_2026-09-26.md` §八 唯一的 🔴 项 —— `e917e61` 自留残差「英文 `will see token unlocks` 未纳入 `_SCHEDULED_FUTURE_RE`」（报告称 prod 2 行 / 资产 7760、8774）。**本轮只读量化推翻了该残差的归因**，据实测改口径落地。

- **归因修正（关键）**：残差原文写「英文句式不在表内 ⇒ 该条判陈旧」，实测**不成立** —— 源库把 cid 8072 的标题**截断在 83 字符**（`Token Unlocks data shows that … ID, and MBG wil...`），`will see` **根本没落进 `title + ai_summary`**；近 60 天全库仅 2 行含 `will see`，**均不在 7 天窗内**（cid 5619 新泽西通勤服务降班次、cid 3301 宏观解锁总览）。故「补英文句式就能修好 7760/8774」的前提是错的。
- **真缺口其实是两条**：① 中文 **`将迎来…`**（原表只有 `将于`，`将迎来大额解锁` 一条也不命中）—— 这才是 cid 8072（英文截断版）被去豁免的真正原因，它靠**其中文 `ai_summary`**「下周XPL、H、SOSO等代币将迎来大额解锁」参与判据；② 英文 `will see <动作词>`（原 `will` 分支要求动词**紧跟** `will`，`will see` 落空）。
- **修法（两条都补，但性质不同）**：新增 `将\s*迎来` 与 `\bwill\s+see\s+(?:\w+\s+){0,3}(?:delist|list|remov|upgrad|unlock|halt|suspend|migrat|launch)\w*`。英文那条**在 prod 净增豁免 = 0**，属**防御性**（防未截断的英文源）；刻意不写成裸 `\bwill\s+see\b` —— 裸写法会复活 NEW-1 刚消灭的「评论类旧闻」误豁免（`Analysts will see the impact of the halving next month` 含 `halving` 动作词即恒新鲜），故保留词距上限 `{0,3}` 构成「近距将来语义」双重门槛（已写成断言）。
- **prod 只读量化（7 天窗、>3 天陈旧候选 2321 条、现行豁免 60 条）**：三候选对照 —— A 仅英文 `will see`：**净增 0**；B 仅 `将迎来`：**净增 5**；C 裸 `\bwill\s+see\b`：净增 0。落地 **A+B**。5 行全为真·预告：asset 7760/8774（cid 8072 英文截断版，靠中文 `ai_summary`）、8774/8801（cid 8327「本周 H、SOSO 及 STBL 等将迎来代币一次性大额解锁」）、3994（「B.AI 热门模型权益将迎来新一轮升级」）；**无加密外误伤**。
- **探针 90 → 96 断言**：新增「复验 e917e61·八」段（3 条残差句式必须豁免：英文未截断版 / cid 8327 中文解锁 / asset 3994 中文升级；3 条必须不豁免：`will see` 词距反例 / prod cid 5619 实物 / 「将迎来抛压」无动作词）。**回归零失败**：`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_header_regime`(135/0)、`test_scan_scenario_label`(48/0)、`test_scan_l1_closed_bar`(16/0)、`test_highlight_audit_20260924`(67/0)、`test_scan_alert_remaining`(16/0)、`test_major_event_alert`(45/0)。
- **未做 / 边界**：① **标题截断本身无解**（源库行为；`e917e61` 已刻意排除 `body_text` 作判据输入，理由是正文版会系统性放宽 29 条汇总稿）⇒ 英文截断版只能靠中文 `ai_summary` 兜；② 只影响**新判定**，历史告警快照不回溯；③ runtime 侧佐证仍需下一封盘面告警邮件。

### 一键投研页 §4.2 #5~#15 处置（audit_一键投研页_高确定性结论_2026-09-26 §4.2，2026-09-26，本次提交）

来源：`audit_一键投研页_高确定性结论_2026-09-26.md` §4.2 建议清单 #5~#15（#1~#4 已由 `2cc1e55` 收口）。11 项全部落地；外部采集类缺口如实留档另开工单。

- **#6 引用索引错位（根因不是前端 off-by-one）**：生成侧 `_build_research_sources`（含 structured 条目、按 type 重排）与读取侧 `snapshot["sources"]`（仅文档，长度/顺序都不同）是**两份清单** ⇒ 同一个 index 指向不同来源。修法：`biz.research_thesis` 新增 `sources_json`（生成时刻清单快照），读路径透出 `citation_sources`，前端 `d.citation_sources || d.sources` 优先；旧行无 `sources_json` 回退旧口径不劣化。另落地「官网首页禁作数值类结论唯一引用」（`_is_homepage_source` + `_NUMERIC_POINT_RE`：首页对「资金费率 0.0206%」零证明力 → 剔除并退回「推断」）。
- **#5 证据分级**：`_compute_evidence_stats` 按论点（thesis / risks / 四维 points，与读取侧同口径）统计 cited/inferred，`inferred_ratio > 50%`（严格大于，恰好 50% 不触发）→ `conviction` 强制锁 `low`；生成侧与读取侧都执行。**踩坑**：生成侧 `_sanitize_citations` 的 `seen_idx` 原在 item 循环**外**（跨论点去重）→ 引用被误删 → inferred_ratio 虚高 → 误锁，已改为每 item 重置。
- **#14 确定性评分卡**：`coverage×0.35 + freshness×0.25 + consistency×0.25 + sample×0.15`，三档 ≥70 可下注 / 40–69 仅观察 / <40 不可用于决策；落 `analysis_json` + 前端评分卡（分数 + 档位 + 四维进度条 + notes）。**新增「零证据封顶」**：`coverage == 0` 时分数封顶 39.9 且档位强制 `unusable` —— 新鲜度/一致性/样本量再好看也替代不了证据本身。
- **#14 与审计验收带的偏差（如实记录，勿当回归失败）**：审计验收点为「PONS 实测落 25–35 并标不可用于决策」。用 prod 真实输入实测（asset 11114：21 论点 **0 条可核验引用**、三源时效全新鲜 0.6/1.9/3.6h、funding +0.0095% / OI +0.04% / CVD −31.47%、资料 5/12）→ **裸分 46.3**，落「仅观察」。差异根因：审计的 25–35 是在**未掌握时效数据**下的估计（隐含 freshness≈0.5、coverage≈0.21）；按审计给定权重，freshness=1.0 时无法落进该带。故以「零证据封顶」保证**档位验收点成立**（实测 39.9 → 不可用于决策），探针锁定该口径。
- **#7 催化剂去重**：`_catalyst_event_key` 原「归一化取前 24 字符」不够——真实重复的主因是**同一稿件被不同媒体加前缀转载**（Bonk Guy 4 条分别以 ChainCatcher / 火星财经 / PANews 开头）。新增 `_CATALYST_TITLE_NOISE_RE` 循环剥「媒体名 / 日期 / `消息` / `reported on <月> <日>`」前缀。**prod 只读实测**：13 条重灾标题 13 → 8 组（Bonk Guy 4→3、巨鲸清仓 5→3、Bonk Guy 持仓 3→2、PONS 反弹 2→1）。叠加 `horizon_days=0` 禁入（30 条中 18 条无窗口）+ 弱相关剔除 → **可消费项 30 → 25（去重）→ 7，满足审计「<15」**。
- **#7 残留（另开工单）**：中英**跨语言**同事件（13842 英文 / 13845 中文）无法靠标题归一化收敛，需语义聚类（embedding / LLM）。另 `_catalyst_relevance` 的 `link_source != 'auto' → +1` 会让 `trading_pairs` 关联绕过弱相关判据（PONS 30 条全部 `trading_pairs`），审计点名的弱相关条目实际是被 `horizon_days=0` 拦下的。
- **#7 horizon 门控的取舍**：`horizon_days` 只存在于 `biz.catalyst_impact`（`biz.asset_catalyst` 无此列），用 `LEFT JOIN LATERAL` 取；生成侧只排除**显式 =0**，`NULL`（未推导出影响）仍允许进上下文，避免把无 impact 行的催化剂全量误杀（展示侧 `_load_catalyst_impacts` 判据 `(0, None)` 保持不变）。
- **#8 压力评分重构**：由「只看解锁」扩为 `unlock_score − liquidity_discount + concentration + drawdown + cvd + oi + unlock_value`。**关键契约**：`liquidity_discount` 只抵扣 `unlock_score`，不得抹掉已实现抛压（回撤/CVD/OI）⇒ 保住旧三参契约 `(0, 933.71, 0) == (25.0, "low")`、`(0, 50, 0) == (12.5, "low")`（turnover=0 时 discount=0）。PONS-like（dd −30.3 / cvd −0.119 / oi −2.27，无解锁）> 0 且非 low。
- **#9 板块与竞品重分类**：新增 `_COMPETITOR_MCAP_BAND = 3.0`，竞品按**市值 ±3× + `asset_type` 优先**匹配，同量级候选 <3 个回退 `sector_only`；两处 return 补 `matched_by`。thesis 生成侧新增**消费 `/competitors`** 的结构化块（`metrics_structured["competitors"]`）+ prompt 规则 13（禁止拿不同量级蓝筹给 meme 做估值对标）。**缺口**：`core.asset` 无 chain 列（链信息散在 tokenomics / contracts JSON），本轮只落地「市值量级 + 资产属性」，**链维度未落地**。
- **#10 数据缺口**：**已接线** —— ① LP 锁仓 / 合约弃权 / 买卖税（`biz.asset_tokenomics`）② GitHub 仓库活跃度（`biz.github_repo_activity`，按文档 URL 匹配）③ DeFiLlama 协议 TVL（`src_dl.protocol_list` ⋈ `core.asset_source_map`，**列名是 `source_asset_key` 不是 `source_key`**；`protocol_list` 无 `change_30d`，只取 `change_1d/7d`）。**本就已在指标里** —— 链上 Top10 集中度（`biz.onchain_holder_snapshot`）、社交热度（`biz.asset_social_heat`）。**明确不可行（需外部采集，另开工单）** —— 协议**收入/fees**（无采集管道；prompt 明令只能写「收入数据未采集」，禁编造）、**审计报告结构化**（无 OCR/解析管道）。
- **#11 证伪条件**：`_build_falsifiers` 产出四类 —— 价格跌破 −15% / OI 跌破 −30% / Top10 集中度 `max(80, 当前×1.2)` / 解锁前 3 天失效；无数据不产出假条件。前端 `t-falsify` 区块。
- **#12 空态卡片升级**：missing item 新增 `impact_dimension` / `impact_desc` / `determinism_gain`，前端展示「⛔ 缺失 → 影响维度 → 补全后确定性分 X → Y」。
- **#13 信号引擎输出门控**：`detect_asset_signals` 尾部返回 `signal_gate`（`checks` / `available_checks` / `total_checks` / `summary`），区分「检测了但没触发」与「数据不足无法检测」；`signal_count=0` 时文案不得暗示中性，并列出不可检测项。**字段名是 `available` / `note`**（前端初版误用 `detectable` / `missing_reason`，已对齐）。
- **#15 版本化留痕**：新增 append-only `biz.research_thesis_version`（含 `determinism_score` / `determinism_tier` / `inferred_ratio` / `payload_json`），`_load_thesis_versions` + `_diff_thesis_versions` 产出最近两版 diff（stance / conviction / 档位 / 分数 / 关键数值），前端 `t-verdiff` 展示。
- **建表/迁移时序**：`sources_json` / `analysis_json` 用 `ADD COLUMN IF NOT EXISTS`、版本表用 `CREATE TABLE IF NOT EXISTS`，且 `_ensure_research_tables` **读路径也会调用** ⇒ 部署后首次读写即自动补列建表，无需手工迁移（规避了「读路径先于写路径取不到新列」的 500 风险）。
- **探针**：新增 `test_research_determinacy_20260926.py`（纯离线，**109 断言全绿**），覆盖 #5/#6/#7/#8/#10/#11/#12/#13/#14/#15；#7 夹具直接用 **prod 真实标题**（非构造）。**回归零失败**：`test_highlight_determinacy_20260926`(72/0)、`test_signal_type_calibration_20260926`(73/0)、`test_scan_alert_audit_20260926`(96/0)、`test_fdv_degeneracy_20260926`(14/0)、`test_unlock_refresh_20260926`(23/0)。
- **未做 / 边界**：① #10 收入/fees 与审计报告结构化需外部采集（另开工单）；② #9 链维度匹配缺失；③ #7 跨语言语义聚类；④ 生成侧改动只对**下次重算**生效（存量 thesis 的 `analysis_json` 为空，读取侧会实时补算，故前端不空）。

### 共享函数 `_fmt_price` 去尾零改动核验（`351d2ae`，2026-09-27，本次提交）

来源：WorkBuddy 侧 `.workbuddy/memory/2026-09-26.md` 记「`351d2ae` 改动共享函数 `_fmt_price`（去尾零），**是否接受需用户拍板**（波及面超出传导逻辑 Scope）」。本轮用户拍板 = **核验 + 钉边界 + 给局部版加去尾零**（不含「收拢两份实现」）。

- **核验对象**：`_trim_trailing_zeros()` 新增于 `workbench/catalyst/notifier.py`，被**模块级** `_fmt_price`（`f≥1000`/`f≥1`/`f≥1e-4` 三档定点分支）调用；`f<1e-4` 科学计数分支**刻意不调**（见下陷阱）。
- **关键发现：该文件存在两个同名 `_fmt_price`**，分属不同邮件通道且**行为不同**：
  - **局部版**（`notifier.py:821`，`_build_fast_alert_html` = **盘面快告警**通道）：改前**不去尾零**、阈值档位不同（`≥1` 用 `.2f`）、**带 `$` 前缀**。改前渲染实测：`620.50 → "$620.50"`、`100.0 → "$100.00"`、`1234.0 → "$1,234.00"`（尾零噪音曾在此通道存在，本节末尾已修）。
  - **模块级版**（`notifier.py:2434`，`_build_major_event_html` / `_build_a_alert_card` / `_build_token_snapshot`）：去尾零、`≥1` 用 `.4f`、无 `$` 前缀。同值渲染：`"620.5"` / `"100"` / `"1,234"`。
  - ⇒ 两版**阈值 + 精度 + 前缀均不同**，「收拢两份实现」**不是简单合并**，属行为改变（会改盘面快告警邮件价格显示精度），超出本轮授权范围，**未做**，留作待批项。
- **边界事实（已跑矩阵，全部为真）**：`.` 阻断 `rstrip("0")` ⇒ **整数部分零不被误吃**（`10.0000→10`、`100.0000→100`、`1000.0000→1000`、`1,234,500.0→1,234,500`）；`0.00000000→0`、`0.00010000→0.0001`、`1.0108` 不变；`None`/非数值 → `—`；`1→"1"`、`1000→"1,000"`、`999.9999→"999.9999"`（去尾零不降精度）、`0→"0.0000e+00"`。
- **指数尾零陷阱（源级守卫，当前不可达）**：`_trim_trailing_zeros("1.0000e-10")` 会返回 **`"1.0000e-1"`**（把指数尾零当小数尾零吃掉）。当前无任何行同时含 `:.4e}` 与 `_trim_trailing_zeros` ⇒ 科学计数分支未被 trim。若将来有人给该分支加 trim，meme 极小价（`1e-10` 类）会**静默退化成 `1.0000e-1`**。
- **探针**：`test_major_event_alert.py` 45 → **55** 断言（新增第 10 节：去尾零纯度 4 项 + 模块级矩阵 5 项 + 科学计数陷阱守卫 1 项）；`test_fast_alert_audit_rest.py` 61 → **66** 断言（新增第 12 节：局部版去尾零 3 项 + 非尾零价不受影响 + 局部版科学档未被 trim）。**回归零失败**：`test_fast_alert_ai_veto`(46/0)、`test_catalyst_channel_dedup`(22/0)。
- **后续（同一提交，用户拍板）**：选「仅给局部版加去尾零」⇒ 局部 `_fmt_price`（`notifier.py:821`）四个**定点档**套 `_trim_trailing_zeros`，**保留 `$` 前缀与既有精度档**（`≥1000` `.2f` 带千分位、`≥1` `.2f`、`≥0.01` `.4f`、`≥1e-4` `.6f`）；**科学计数档仍不 trim**（同上陷阱）。效果：`$620.50→$620.5`、`$100.00→$100`、`$1,234.00→$1,234`；非尾零价不受影响（`1.5788→$1.58`）。定前已 grep 确认**无任何既有断言依赖带尾零的美元串**。
- **未做 / 边界**：① 两份实现**仍未收拢**（阈值/精度/`$` 前缀差异原样保留）—— 「统一为单一函数」会改盘面快告警显示口径（去 `$`、精度变 `.4f`/`.8f`），未获授权；② 未触碰该函数之外的其它渲染行为。

### 复验遗留缺陷合并 FIX-DETERMINACY-002（工单_复验遗留缺陷合并_FIX-DETERMINACY-002_2026-09-27，2026-09-27，本次提交）

来源：`工单_复验遗留缺陷合并_FIX-DETERMINACY-002_2026-09-27.md`（A/C 两组落地；B 组写库/重抓待用户授权；D 组明令不在本单）。**关键前提：工单 A1②/A2/A4 声称「遗留未修」，但 `git log` 显示 `5ef3f02`（2026-09-26 22:28）已实现同三项，本轮不重做**，只补工单明确要求而缺失的两处（A1① 写时修、A1② 读时 `fdv_repaired` 标记），并在下方逐条留档「已落地 / 本轮补 / 同义命名」。

- **A1① 快照写时修（本轮新增，双管缺一不可）**：`ingest_cmc_quote_snapshot.py` 在落库前对退化 FDV 兜底 —— 退化判据 `fdv 为空/≤0` 或 `fdv ≤ market_cap × 1.001`，且 `max_supply > circulating × 1.02`；候选按 `cmc_id` 批量查 `src_cmc.cmc_asset_quote_snapshot` 历史 `MAX(max_supply)`，**沿用与读侧逐字同口径的印证判据 `hist_max >= max_supply × 0.98`**（挡代币化股票 MRVLon/CMGon 的 LLM 抽取失真股本），命中则 `fdv = price × max_supply`，仅在实际抬高（`> 旧值 × 1.02`）时改写，并打印 `[fdv-repair] 本轮修正 N 条`。**为何不能只做读时**：`src_cmc.*` 是 CMC 原样镜像，读时兜底只护 `db_stats`，其它直读快照的消费方仍见退化值；写时修同时给日表 ETL 供数。
- **A1② 读时 `fdv_repaired` 标记（本轮补）**：`db_stats.py` 读时兜底命中后（既有的 `result["market"]["fdv_basis"] = "price_x_corroborated_max_supply"`）追加 `result["market"]["fdv_repaired"] = True`，让下游可判别「本值是重建出来的」而非 CMC 原值。**A1② 主逻辑（`_corroborated_max_supply` 读时兜底）已由 `5ef3f02` 落地**（见上文「一键投研页 P0 修复复验处置」§#1）。
- **A2 已由 `5ef3f02` 落地，`kind` 命名同义不改**：`_detect_thesis_drift` 的**无条件不变式**（`fdv≈market_cap` ∧ `circulating < 0.98×佐证 max_supply ⇒ stale`）已在 `5ef3f02` 实现，kind 落为 **`fdv_degenerate`**。工单字面写 `degenerate_fdv`，**两者同义**；保留 `fdv_degenerate` 不重命名，因已有探针断言（`test_fdv_degeneracy_20260926`）与前端 `research.html` 分支按该名消费，改名会破坏已上线渲染。
- **A3 展示侧抛压改调 `compute_unlock_pressure()`（本轮改）**：`db_stats.py` 抛压展示侧原为「只看解锁」的简化版，改建为**优先调七分量算法 `compute_unlock_pressure(asset_id)`**（解锁×筹码集中×回撤×CVD/OI×解锁价值；与生成侧同源，含 6h 缓存 `biz.asset_unlock_pressure`），回填 `pressure_score` / `risk_level` / `top10_concentration_pct` / `detail`；**`except` 保旧简化版作 fallback**（仅在七分量算法不可用时兜底），命中 fallback 时标 `pressure_basis = "fallback_unlock_top10_only"`。**产品目的**：展示侧与生成侧结论同源，消除「同一资产两处抛压不一致」。
- **A4 已由 `5ef3f02` 落地**：日表 `circ=0` 回退（INSERT 侧 `COALESCE(NULLIF(q.circulating_supply,0), NULLIF(q.total_supply,0))` + `repair_zero_circulating()`）已在 `5ef3f02` 实现，prod 已执行 `--repair-only --days 45`（见上文「一键投研页 P0 修复复验处置」§#3）。本轮不重做。
- **C1 豁免类默认不回测即不进 HIGH（本轮改，双落点）**：① **写表侧**（`backtest_opportunities._gate_for`）exempt 分支由「豁免 = 保留 HIGH」改为 **`no_high=True`**（返回 `signal_type not in EXEMPT_ALLOW_HIGH`），周级重算后生效；新增 `_load_exempt_allow_high()` 读 `market_rules.yaml` 的 `opportunity_rules.exempt_allow_high`（失败回退空集）。② **消费侧即时封顶**（`macro_market._push_opportunity`）：新增 `_exempt_no_high(st)`（用 `_calibration_status` 判 gate 以 `exempt_` 开头，白名单内除外），tier 计算后若 `HIGH` 且命中则**当场降 `MED`** 并写 `tier_demote_reason`（含 `exempt_unbacktested` 文案），**不等周级刷新** ⇒ 当天可见。**重要**：`OPPORTUNITY_THRESHOLDS_DEFAULT` 必须同时登记 `"exempt_allow_high": []`，否则 yaml 覆盖因 key 不在默认表而失效（同 `fng_*` 教训）；`market_rules.yaml` 白名单**当前为空**（列入才允许进 HIGH，须注明理由）。**口径变更影响**：HIGH 数会下降（工单明示 13 → 5 属评分口径变更，非回归）。
- **C2 高亮区顶部成色指标（本轮改，前端纯计算）**：`templates/index.html` 的 `renderSignalHighlight` 在 `total` 计算后按 `o.calibration_status.state === 'calibrated_ok'` 统计 `_calibOk` / `_unbacked`，高亮区顶部渲染「**HIGH N：已校准背书 X / 未回测 Y**」（class `signal-high-credibility`，CSS 紧随 `.signal-calib-decay`）。后端 `_calibration_status` 与前端同源，**无需改 API**。
- **C3 断言（本轮补）**：`test_research_determinacy_20260926.py` 新增断言钉死 `horizon_days=0` 不进结论 —— 生成侧 `_fallback` 硬过滤存在 + 显式 `=0` 标 `no_horizon` 后才挑可消费项。
- **C4 零证据封顶具名常量（本轮改）**：`db_stats.py` 新增 `_ZERO_EVIDENCE_SCORE_CAP = 39.9`（紧随 `_DETERMINISM_TIERS`），`_compute_determinism` 内原散落字面量 `min(score, 39.9)` 改为消费该常量。断言：`== 39.9` 且 `< 40.0`（严格低于「仅观察」档下沿）、源码消费常量（非字面量）、**权重扰动后零证据仍 `unusable`**（不随权重浮动）。
- **C5 调度时区（核实，无需改）**：`scheduler.py` `TZ = os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")` + `BlockingScheduler(timezone=ZoneInfo(TZ))`，全仓无 `SCHEDULER_TIMEZONE` 覆盖（仅 README/.env.example 默认值）⇒ `signal_type_calibration_weekly` 的 cron `0 5 * * 0` **即北京周日 05:00**，正确。
- **B 组（用户授权后已执行完成）**：授权后于 2026-09-27 连 prod 执行（`assets` 11114 PONS）。
  - **B2 tokenomist 重抓 ✅**：`.venv/bin/python phase_chain_token_unlocks.py --asset-id 11114 --save`（**需 `.venv`，anaconda 无 playwright**）→ tokenomics.com 未收录、回落 tokenomist.ai 抓到 2 条解锁事件并写库，`crawl_status=ok`、`updated_at` 刷新 ⇒ `biz.asset_token_unlocks` 的 `age_hours ≈ 0.035h < 168` ✓。**注意**：工单读数「updated_at 仍 09-04 / age 532.2h」为**旧快照**，实测执行前已是 09-26 14:56（上一轮刷新），重抓后归零。
  - **B1 重算 PONS thesis ✅（含一轮「缓存刷新后二次重算」）**：`db_stats.generate_research_thesis(11114)` → `ok`。① **竞品（#9）已接入生成**（日志「竞品数据：DeFi 赛道 2 个对标（匹配方式 sector_only）」，论点含对标/估值）；② **versions 0 → 2**、`version_diff` 可产出（changes 覆盖置信度/价格/市值/FDV）；③ **TVL（#10）未出现** —— 根因是**数据覆盖缺口而非代码**：`core.asset_source_map` 中 11114 仅 `cg`/`cmc` 映射、**无 DeFiLlama 映射**，且 `src_dl.protocol_list` 里 `Pons V1/V2`（Launchpad）的 `tvl` **本身为 NULL**，故 `_fund["defillama_tvl"]` 不写入（与 prompt 规则 14「无则写未采集」一致）。
  - **关键发现：`biz.asset_unlock_pressure` 6h 缓存里存的是旧算法行**。首读 `compute_unlock_pressure(11114)` 命中缓存（`cached:true`）返回 **`pressure_score=0.0` 且 `detail` 只有旧三参**（`unlock_score`/`concentration_score`/`liquidity_discount`，缺 `drawdown/cvd/oi` 分量）—— 即新算法落地后**旧缓存行仍会返回旧结构**，须 `force=True` 重算或等 TTL 过期。`force=True` 重算 ⇒ **`pressure_score 0.0 → 28.24`**（`drawdown_score=28.24` 独撑，`cvd_ratio_24h=+0.076`、`oi_change_24h=+6.8%` 均为正 ⇒ cvd/oi 计 0）。已对 11114 force 刷新并二次重算 thesis，使其消费新值。
  - **A3 验收偏差（如实记录，属数据漂移非代码缺陷）**：工单 A3 验收为「`pressure_score > 0` 且 `risk_level != low`」。实测 **score 28.24 > 0 ✓**，但 **`risk_level` 仍 `low` ✗** —— `_compute_pressure_score` 阈值为 `≥60 high / ≥30 medium / 其余 low`，28.24 距 medium 差 1.76；且工单验收基于审计时的 PONS 快照（CVD −31.47%）而**实时 CVD 已转正**（+7.6%），cvd/oi 分量归零、仅回撤计分。**A3 的真实价值已兑现**：展示侧由旧简化版「忽略回撤的 0.0/low」改为与生成侧同源七分量（0.0 → 28.24），两处口径一致。
- **D 组明令不在本单（未做）**：`mvrv_deep_over 91` 归高危板 P0-R1；`backtest_opportunities.py` 取价/skip 复核不做；`linker` 双副本归催化剂单。
- **探针（全部零失败）**：`test_fdv_degeneracy_20260926`(14/0)、`test_signal_type_calibration_20260926`(**89 → 99**，新增 C1/C2 段 10 断言)、`test_research_determinacy_20260926`(**109 → 115**，新增 C3/C4 段 6 断言)、`test_highlight_determinacy_20260926`(72/0)、`test_macro_market_board_tier2`(36/36)、`test_macro_market_p1_upstream`(24/24)、`test_highlight_audit_20260924`(67/0)、`test_highlight_alert`(68/0)；`py_compile`（6 个改动 py）与 `node --check`（`index.html` 抽取 script + `{{...}}` 占位替换）、`yaml.safe_load(market_rules.yaml)` 全通过。
- **未做 / 边界**：① 工单 A1②/A2/A4 未重做（已由 `5ef3f02` 落地）；② A2 `kind` 保留 `fdv_degenerate`（工单 `degenerate_fdv` 同义）；③ B1/B2 已授权执行完成（见上）；④ 线上 runtime 复验（重拉 notebook / market-history / overview）须待 Zeabur 重建后执行（`push ≠ 线上生效`）；⑤ A3 验收的 `risk_level != low` 因实时数据漂移未达（score 28.24，详见上）。

### 🔴 P0 证据覆盖率虚高（复验 `核验_FIX-DETERMINACY-002_AC组_95d6bfc_2026-09-27.md` §五，2026-09-27，本次提交）

**现象（复验暴露，比修复前更危险）**：B1 重算后 `/api/research/11114/notebook` 的 `thesis.analysis` 从诚实的 `39.9 / unusable`（27 论点 0 引用）跳到 **`91.3 / actionable`** —— `breakdown.coverage = 1.0`、`evidence = {total_points:22, cited_points:22, inferred_ratio:0.0}`。但 **28 条引用 URL 全为空**（形状 `{"index":1,"title":"代币经济学数据","url":""}`），指向的只是内部数据类别名；其中「链上持仓数据」在 missing 清单里是 **MISS（count=0）**；而 9 条真实来源（官网×3 / docs / twitter×2 / blockscout / defillama×2）**零引用**。数据完备度未变（仍 7/12 缺），分数翻倍 ⇒ 用形式合规冒充实质证据。

**根因**：`db_stats._compute_evidence_stats` 原实现 `cited = sum(1 for p in points if (p["item"].get("citations") or []))` —— **只判 `citations` 数组非空、完全不校验 `url`**。于是 `cited 22/22 → coverage 1.0 → 不触发 39.9 封顶 → 0.35+0.25+0.25+0.0625 ≈ 91.3 → actionable`。

**修法（§五 四条，逐条落地）**：
1. **有效引用口径收紧（本轮改，`db_stats.py`）**：新增 `_citation_urls(item)`（只收 `url` 非空的引用）与 `_has_valid_citation(item)`；`_compute_evidence_stats` 改为「**≥1 条引用带非空 URL 才计 `cited_points`**」，新增 **`weak_cited_points`**（有 citations 但 url 全空）单列并透出；`inferred_points = total − cited_points`（弱引用同按推断计）。
2. **内部数据类别标记（不删条目，仅标记）**：`_build_research_sources._add_structured` 新增 `"internal_dataset": True`（结构化源 `url=None`）；读/生成两侧 `_sanitize*_citations` 回填引用时写 `internal_dataset = bool(s.get("internal_dataset")) or not str(s.get("url") or "").strip()` —— **后半段兼容旧存量行**（`sources_json` 无该标记，仅凭「无 URL」判定）。**为何不删条目**：`citation_sources` 是**按位置索引**解引用的，删条目会让所有 `citation.index` 错位。
3. **`coverage` / `conviction_locked` / 39.9 封顶改用有效引用率**：`_compute_determinism` 的 `coverage = cited_points / total_points` 与 `inferred_ratio` 现均基于有效引用（无需改公式，口径随 ① 自动收紧）；`coverage == 0` 仍触发 `_ZERO_EVIDENCE_SCORE_CAP = 39.9`；新增 notes 文案「N 个论点仅引用内部数据类别（无 URL，不可核验），不计入有效引用」。
4. **prompt 规则 8 补条（生成侧）**：明确要求 citations 必须指向**带 URL 的可核验来源**；**严禁**引用「代币经济学数据 / 链上持仓数据 / 社交热度数据 / 代币解锁数据 / 合约地址」等内部数据类别；`missing` 清单中的缺失数据集禁止出现在 citations；并写明「引用数组非空 ≠ 有据，系统按引用 URL 非空判有效引用」。
5. **`is_inferred` 口径同步（读/生成两侧）**：原 `new_item["is_inferred"] = not cites` 改为 `= not _has_valid_citation(new_item)` —— 仅引用内部数据类别的论点不再被标成「有据」（否则前端徽标与评分卡自相矛盾）。

**prod 只读验收（2026-09-27，`get_or_create_research_notebook(11114, force_refresh=False)`，未写库）**：`score = 39.9`、`tier = unusable / 不可用于决策`、`breakdown.coverage = 0.0`（freshness 1.0 / consistency 1.0 / sample 0.4167 未变）、`evidence = {total_points:22, cited_points:0, weak_cited_points:22, inferred_points:22, inferred_ratio:1.0, conviction_locked:true}`、`conviction = low`；notes 含「全部论点为推断、无一条可核验引用 → 封顶 39.9」与「22 个论点仅引用内部数据类别…」；实测 citations 28 条、**url 非空 0 条**。**⚠️ 注意**：analysis 是**读取时实时重算**（`db_stats` 读路径 `_compute_determinism`），旧存量行的 `analysis_json` 无需重算即已修正；无需 LLM 重新生成。`internal_dataset` 回填对旧存量行的生效由探针断言（`_td5` 用无标记的旧行形状）覆盖。

**探针（`test_research_determinacy_20260926.py`，115 → 123 断言，0 失败）**：
- **fixture 口径同步**：原 #5/#14 fixture 用**裸整数** citations（`[1]`）期望 `cited_points==1` —— 新语义下裸数字无 url 会变 0，已全部改为带 url 的 `{"index","url"}` 形状（`_t` / `_t2` / `_t3` / `_hi_thesis`）。
- **新增 #P0 断言（8 条）**：`_t_weak`（引用了「代币经济学数据 / 链上持仓数据」但 url 全空）→ `cited_points==0` ∧ `weak_cited_points==2` ∧ `conviction_locked==True`；`_compute_determinism(_t_weak, {}, [])` → `coverage==0` ∧ `score ≤ 39.9` ∧ `tier=="unusable"`（**复现 PONS 虚假 91.3 的根因**）∧ notes 含「内部数据类别」；`_sanitize_thesis_citations` 对**旧行形状**（`type=structured, url=None`，无 `internal_dataset` 键）→ 引用条目回填 `internal_dataset is True` 且 `is_inferred is True`。
- **源码结构断言更新**：`'new_item["is_inferred"] = not cites'` → `'new_item["is_inferred"] = not _has_valid_citation(new_item)'`；新增 `'"internal_dataset": bool(s.get("internal_dataset"))'`。

**遗留清单本轮结论（复验报告 §六）**：
- **#2（P1）TVL 未进结论 ❌ 判为数据缺口，非代码**：`core.asset_source_map` 中 11114 仅 `cg`/`cmc` 映射、**无 DeFiLlama 映射**；且 `src_dl.protocol_list` 的 `Pons V1/V2`（Launchpad）`tvl` **本身 NULL** ⇒ `structured_metrics.fundamentals` 无 `defillama_tvl`，与 prompt 规则 14「无则写未采集」一致。**动作：不修代码**（要有 TVL 须先补 `asset_source_map` 的 DL 映射与协议匹配）。
- **#3（P1）竞品已接入生成 ✅**：B1 重算日志「DeFi 赛道 2 个对标（sector_only）」、valuation 论点含对标/估值；**未逐句核正文是否出现同量级横向对比表述**（复验方标注的未验项），本单不改码。
- **#4（P2）A3 门槛边界**：`28.24 < 30`（medium）差 1.76，且 28.24 几乎全由 ATH 回撤单分量贡献 ⇒ 回撤再深约 2pp 即跨档。**本单未加断言**（复验建议项，属独立小工单），仅在 AGENTS 留档：`_compute_pressure_score` 阈值 `≥60 high / ≥30 medium / 其余 low`。
- **#5（P2）B2 已完成 ✅**：`unlock.age_hours ≈ 0.035h < 168`（见上文 B 组）。
- **#1（🔴 P0）证据覆盖率虚高 ✅ 本轮处置**（见上）。

**未做 / 边界**：① 未改前端（`research.html` 已按 `is_inferred` 渲染「(推断)」徽标，口径修正后自动正确；`internal_dataset` 仅作数据标记，未新增 UI 文案）；② 未对 PONS 做 LLM 重新生成（读路径实时重算已足够验证，且避免无谓消耗）；③ #2 TVL / #4 门槛边界按上表判为不改码。

**线上 runtime 复验（2026-09-27，Zeabur 重建后，只读）**：
- **✅ P0 修复线上生效**：`GET /api/research/11114/notebook` → `analysis.score = 39.9`、`tier = unusable / 不可用于决策`、`breakdown.coverage = 0.0`、（freshness 1.0 / consistency 0.6 / sample 0.4167）、`evidence = {total_points:22, cited_points:0, **weak_cited_points:22**, inferred_points:22, inferred_ratio:1.0, conviction_locked:true}`、`conviction = low`；notes 含「全部论点为推断、无一条可核验引用 → 封顶 39.9」+「22 个论点仅引用内部数据类别…」。**`weak_cited_points` 新键出现即为新代码上线的判定标记**（旧版返回 91.3/actionable 且无该键）。实测 citations **28 条全部 `internal_dataset: true`、`url` 非空 0 条** ⇒ 旧存量行的 `internal_dataset` 回填在线上确认。
- **✅ A1 FDV**：`GET /api/research/11114/market-history` 32 行含 fdv 字段，**`fdv` 空/0 = 0 行**；末行 `2026-09-27 fdv = 627,555,897.82`（日表为聚合值，不带 `fdv_repaired` 标记）。
- **✅ C1 豁免封顶**：`GET /api/market/overview` → `opportunity_list.opportunities` 中 **8 条 `exempt_*` 全部 `conviction_tier = MED`**、**`exempt_*` 无一进 HIGH**，11 条带 `tier_demote_reason`（文案「exempt_unbacktested：该类型从未被回测，默认封顶 MED…」）。**本次时点 HIGH = 0**（复验时为 3 条全 `calibrated_ok`）—— 属快照时点/分数衰减的业务漂移，与本轮改动无关（本轮未触碰 `macro_market.py` / `backtest_opportunities.py`）；C1 意图「HIGH 席位必有回测背书」在 HIGH=0 时为空真，不构成回归。
- **⚠️ 已知抖动**：`/api/market/overview` 是重计算端点，实测多次 **502 / 读超时**（同一次复验内重试 3 次才成功），属该端点既有负载特性，非本次改动引入；复验时按重试即可。**字段名提示**：机会条目的档位字段是 **`conviction_tier`**（`tier` 不存在），成色在 `calibration_status.state`，与 `index.html` 前端一致。

### 🟡 P2 来源表 `internal_dataset` 未回填（复验 `核验_P0证据覆盖率_96fe711_2026-09-27.md` §四，2026-09-27，本次提交）

**现象**：P0 修复后，**论点级 citations** 的 `internal_dataset` 已正确（28/28 为 true），但**顶层来源表 `citation_sources`**（13 条）该字段**全为 `None`**，含前 4 条 url 为空的「代币经济学数据 / 链上持仓数据 / 代币解锁数据 / 合约地址」。评分卡读论点级、结论不受影响，但前端若按来源表判来源性质会拿到未标记状态。

**根因**：`internal_dataset` 回填只写在 `_sanitize*_citations`（**逐论点**回填引用的循环里），**来源表本身从未被 sanitize**；且旧存量行的 `sources_json` 系修复前代码写入，本就没有该键。

**修法**：新增 `db_stats._mark_internal_sources(sources)`（就地、幂等、`None` 安全），判据与论点级 citations **逐字一致**：`internal_dataset = bool(已有标记) or 无 URL`；在读取路径 `get_or_create_research_notebook` 中紧接 `citation_sources` 取值后调用 `_mark_internal_sources(citation_sources)`。**为何用「或 无 URL」而非只读已有标记**：旧存量行没有该键，只读会永远 `None`。**新生成行**无需此步（`_add_structured` 已写 `internal_dataset: True`）。

**探针（`test_research_determinacy_20260926.py`，123 → 127 断言，0 失败）**：来源表用例 `_src_tbl`（`structured/url=None` + `official_website/url=非空`）→ 标记为 `True` / `False`；重复调用幂等；`_mark_internal_sources(None) == []`；源码结构断言读取路径确实调用。

**线上 runtime 复验（2026-09-27，Zeabur 重建后，只读）✅**：`GET /api/research/11114/notebook` → `citation_sources` **13 条：`internal_dataset=True` 4 条**（代币经济学数据 / 链上持仓数据 / 代币解锁数据 / 合约地址，均 `url=None`）、**`=False` 9 条**（docs / 官网 / twitter / blockscout / defillama 等真实 URL）、**`=None` 0 条**；「无 URL 却未标 internal」的残余条目 **为空**。回归核对无劣化：`39.9 / unusable`、`coverage=0.0`、`weak_cited_points=22`、`cited_points=0`。（重建窗口内 `notebook` 端点亦出现 502，与 overview 同属既有抖动。）

**本轮其余遗留处置**：
- **板块错配（P2，DefiLlama 标 `Launchpad`、系统标 `sector=defi`）**：复验方独立用 DefiLlama 公开 API 证实 `pons-v1/pons-v2` 的 `tvl` 与 `currentChainTvls` **均为空**（⇒ 遗留 #2 **关闭为「数据源无该指标」**，不再当代码缺陷）。但系统仍把 PONS 归 `defi`、拿 Aave/1inch/Aerodrome 做估值对标、TVL 指标悬空 —— **属分类数据问题，建议独立开工单「PONS sector 重分类 defi → launchpad」**，本单不改码（改分类会影响赛道聚合与竞品选择，需单独评估）。
- **A3 门槛边界（P2，28.24 vs 30 差 1.76）**：按上一轮约定**仅留档、不加断言**，接受。

### 9 币盘面告警邮件 NEW-A / P1-2 / NEW-B 处置（审计_9币盘面告警邮件_2026-09-27，2026-09-27，本次提交）

来源：`audit_9币盘面告警邮件_2026-09-27.md`（修复后第一封真实告警邮件；首轮 4×P0 + P1-1 + 两轮复验 NEW-1/NEW-2 已 prod 实证闭环）。本轮按用户「按你的判断处理」取 **NEW-A + P1-2 + NEW-B** 三项（NEW-C/NEW-D/NEW-E 暂缓）。

- **核实更正（两处与报告不符）**：
  - **NEW-A 的活跃生产者是 daemon，不是报告点名的 `phase_scan_accumulation_pool.py`**。全仓 grep：该脚本**无** scheduler / 测试 / import 引用（`scheduler.py:219` 注释「scan_accumulation_pool(30min) → scan_daemon」），属**设计口径副本**；真正的 BRK 生产者是 `scan_daemon.task_scan_accumulation`（原 `"high"` 硬编码落在此处 L1206），另 `_load_alert_candidates`（L1287）无条件收 BRK。
  - **遗留「C 组 4 条漏放行（矿工/miner/上线/stacks）」报告已过期** —— `linker.py` 现已是「删 `矿工|miner|上线` + `stacks(?!\s+of\b)` + 补 `sui`」（`ba96264` 已处理），**无需再做**。
- **NEW-A（P1，BRK 绕过 L0 regime）**：原 `confidence="high"` 硬编码 ⇒ 图例「受限方向信号被降级 high→medium、告警只取 high ⇒ 该方向本轮不发信」对 BRK 是**假声明**（本封 9 币里 6 个 BRK，页头却写「空头环境受限」）。改为 `task_scan_accumulation` 起始处 `regime = _build_regime(conn)`，BRK 按 `brk['dir']` 是否顺 regime 决定 high/medium（与主池同口径）。**设计副本** `phase_scan_accumulation_pool.py` 同步改为 `from scan_daemon import _build_regime`（共享判据防漂移）。
- **P1-2 复发（BRK 未落 `price_chg_pct`）**：BRK 原落 `price_chg_pct=None` ⇒ 卡片涨幅渲染 `-`，与「-8.00% 失效位」并列时**风险回报不可评估**（本封 6 个 BRK 皆如此）。改为取触发根（已收盘条 `closed[-1]`）相对前一根的涨跌幅（与主池「触发根涨跌幅」同口径）。失效位 `-8.00%` 本身是既有 `STOP_PCT_MIN=8.0` 夹带设计（已带「已触下限 8%（真实 2×ATR 更窄）」披露 + 图例说明），非本轮缺陷。
- **NEW-B（费率快照陈旧被当期值渲染，比报告更广）**：报告只说「LSK 单行异常」，prod 只读实测根因是 `_load_funding_map` **无任何年龄护栏** —— LSK 行 `fetched_at=09-14`（**陈旧 12 天**）值 −0.4781%；同批 GALA 9 天 / LA 4 天 / MOODENG 3 天 / 1000FLOKI 缺失。新增常量 `FUNDING_STALE_H = 24`，SQL 加 `fetched_at > NOW() - make_interval(hours => 24)`；超窗不供值（渲染回退「n/a（未覆盖）」）。**prod 实测**：库内陈旧符号 **326** 个、funding_map 仅保留 **189** 键，陈旧符号零泄漏。
- **探针**：`test_scan_alert_remaining.py` **16 → 26** 断言，新增「二之二 NEW-A/P1-2（源码 AST：`_build_regime` 调用、BRK 元组第 7 参非 `None`、第 13 参非字面量 `"high"`）」6 条 +「NEW-B 假 cursor 功能」3 条 +「NEW-B 生产库陈旧符号零泄漏」1 条。**回归零失败**：`test_scan_alert_audit_20260926`(96/0)、`test_scan_alert_header_regime`(135/0)、`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_onchain_addr`(36/0)、`test_scan_l1_closed_bar`(16/0)、`test_scan_scenario_label`(48/0)、`test_squeeze_alert_silence`(18/18)；`py_compile` 两改动文件通过。
- **未做 / 边界**：① NEW-C（年化 `×3×365` 假设 8h 结算，实测 7/9 为 ≈4h）需扩采集存 settlement-interval 后再按品种换算，未做；② NEW-D（`_catalyst_entity` 仅认 13 家 CEX + 要求三齐全，ZRO 那条连动作词都没有）需新增「非交易所类实体键」，未做；③ NEW-E（FLOKI 方向打标）P3 且该条已判陈旧、无当前影响，未做；④ 存量信号行不受影响（生产者改动只对**下轮**生效）；⑤ 线上 runtime 复验须待 Zeabur 重建后执行（`push ≠ 线上生效`）。

### 高危信号 P0-R2 共振门槛恒 1 + 单源封顶（审计_高危信号_确定性审计与优化_2026-09-26 §三，2026-09-27，本次提交）

来源：`audit_高危信号_确定性审计与优化_2026-09-26.md` §三·P0-R2（用户指定只修此项；R1/R3/R4/R5/R6/R7 未动）。

- **根因（比报告描述更完整）**：报告只说 `risk_min_resonance = 1 if ai_v2_enabled_for_init else 1`（恒 1）是漏改；实测**仅改这一个字面量不解决任何问题** —— 生产 `ai_v2_enabled` 默认 `"1"` ⇒ v2 恒开 ⇒ 高亮侧门槛也 =1。真正的漏洞是**高亮侧的聚合类 HIGH 闸门（P1-C）整块没复制到风险侧**：`mvrv_deep_over` / `fng_extreme` / `leverage_extreme` 的 `target` 全是**聚合类文案**（「N 币 MVRV 极度高估」「恐贪指数极度贪婪」「衍生品杠杆极值」），`_is_symbol_target()` 判否 ⇒ **天然绕过**币种级 `min_resonance` 筛选，门槛写 1 还是 2 都拦不住它们。
- **处置（两处，对称于高亮侧）**：
  1. `macro_market.py@6513` 调用处 `risk_min_resonance = hl_min_resonance`（引同一变量，防再次漂移；**自造**的第二条硬编码已删）。
  2. `select_risk_signals` 新增「单源封顶」闸门（`@5179-5202`）：`conviction_tier == "HIGH"` 且 `resonance_count < max(min_resonance, 2)` → 降 `MED` + 同步 `confidence="medium"` + 写 `tier_demote_reason`（**降档不删卡**，配额/展示位不变 = 审计所称「观察池」）。
- **刻意偏离（须留档）**：① 白名单取 `{fng_extreme, leverage_extreme}`（硬数据极值，单源即事实本身，与高亮侧 P1-C 同理由），**刻意不含 `mvrv_deep_over`** —— 审计验收明确要求「单源 mvrv_deep_over 不显 HIGH」，且其强度公式正是 P0-R1 点名的自创旁路；② 闸门**覆盖币种级 target**（高亮侧 P1-C 只覆盖聚合类），因审计对风险侧是扁平要求「HIGH 必须 ≥2 源」，且风险侧「确定性」语义不应比高亮侧更松。
- **探针**：新建 `test_risk_signal_p0r2_20260927.py`（**14 断言 / 0 失败**，纯离线）：A 源码对称（引 `hl_min_resonance`、恒 1 写法消失）、B 结构（白名单/门槛式/留痕/白名单不含 mvrv）、C 单源 MVRV 聚合 HIGH→MED 且不删卡、D 双源同标的→仍 HIGH（审计验收双向）、E 白名单单源→仍 HIGH、F 币种级单源→MED。**回归零失败**：`test_macro_market_board_tier2`(36/36)、`test_macro_market_p1_upstream`(24/24)、`test_highlight_determinacy_20260926`(72/0)、`test_signal_type_calibration_20260926`(99/0)、`test_highlight_audit_20260924`(67/0)、`test_highlight_alert`(68/0)、`test_research_determinacy_20260926`(123/0)、`test_scan_alert_header_regime`(135/0)；`py_compile` 通过。
- **未做 / 边界**：① 只改「档位/门槛」，未动 `_risk_sort_key` 的 `is_unlock` 首键（P1-R4）、未给 `price_crash` 打 `is_backward`（P1-R5）、未改 V2 短侧 `risk_score = 100 - ai_score`（P1-R6）、未建 `risk_signal_outcome` 回访闭环（P0-R3）、未统一 `risk_certainty` 模型（P0-R1）；② `ai_enrich_signals_v2(direction="short")` 在其后执行，只会按 AI 未背书**再降**档、不会把 MED 拉回 HIGH（P1-B 三处对称已验）；③ 存量快照行不受影响（读时筛选）；④ 线上 runtime 复验须待 Zeabur 重建后执行（`push ≠ 线上生效`）
。

### 首页大盘分析板块审计处置（audit_大盘分析板块_2026-09-27，2026-09-27，本次提交）

来源：`audit_大盘分析板块_2026-09-27.md`（审计基线 HEAD `78010a6`；落地时 HEAD 已到 `c023045`，两条提交均未触及大盘侧代码）。按用户选定范围修 **P2-A / P2-B / P2-C / P2-E / P2-F**；**P2-D 暂缓**、**P3-G / P3-H 核实为误报不改**。

- **核实更正（4 处与报告不符，前三处影响修法）**：
  - **P2-F 根因描述错误**：后端 `fetch_event_calendar` **确有** `gecko` 键（`macro_market.py:8679` 显式 `[]`，`e023058` 2026-08-31 加入，**早于**审计基线），审计「无 gecko 键 ⇒ 遗漏」不成立（结果恒空是对的，因为 CoinGecko `/events` 已废弃）。真正的浪费在 **`token_events` 算了不渲染**；且审计点名的 `unlock` 键是**人工维护解锁峰**（`macro_market.py:8600-8602`，当前恒为空数组），**真实解锁日程全在 `token_events`**（主源 `biz.asset_unlock_event`，未来 33 天、`LIMIT 50`）。修法据此调整为「渲染 `token_events`（+`unlock`）而非 `unlock` 单键」。
  - **P2-C 影响面比报告大**：`components["market_cap"]["extreme"]` **已被前端消费**（`index.html` 结构子分卡 `structureExtreme` 决定 `extreme-high` 高亮），非报告所称「当前未展示、风险潜伏」；改名必须同步该消费点，否则结构子分卡高亮静默失效。
  - **P3-G 误报（不改）**：两个 `min_available_weight` 是**同名不同段** —— `opportunity_rules:0.5`（供 conviction）/ `scoring_tuning:0.4`（供情绪·结构子分「覆盖度不足」判定）；代码侧 `macro_market.py:85`(0.4) 与 `:2491`(0.5) 与 yaml 一一对应，**无矛盾**，报告把两段当同一真源比对。
  - **P3-H 误报（不改）**：`select_highlight_signals` 合并同标的时，tier 覆盖**位于 `if o["conviction_score"] > merged["conviction_score"]` 守卫内**（`macro_market.py:4962-4964`），低分卡**不可能**拉低主卡 tier；报告「无条件覆盖」系对缩进的误读。（残留意涵：tier 跟随「最高分那张卡」的档位，与「豁免类默认封顶 MED」设计自洽。）
- **处置（2 个代码文件面，零 DDL、零迁移、不改判定口径与评分权重）**：
  1. **P2-A 死分支**：删 `index.html` 大盘周期热力图渲染块（60 余行）。根因：`renderOverview` 读 `data.cycle_dashboard`，而全仓（含 `db_stats.py`）**无任何生产者**；其期望 schema（五维 `valuation/sentiment/capital/derivatives/onchain` + `overall_heat` + `consistency_pct`）**不存在** —— `db_stats.py:4910-5115` 的周期服务产出的是 `phase_label/cycle_heat_score`（三维），与前端期望**不同构**，接线等于新建功能。BTC 周期定位卡（`btc_cycle`）已独立展示周期定位，信息不丢。
  2. **P2-B 死定义 + 过期文案**：删 `DIM_META`（6 维 taxonomy，实际仅用于 loading 占位，与 `renderOverview` 真实渲染的十余张卡**不同构** ⇒ 维护者按 key 找渲染逻辑必踩空），占位改为 `'<div class="dim-card loading">加载中…</div>'.repeat(6)`；`module-desc` 由「情绪子分 + 结构子分 + 事件日历」更新为真实八子板块。
  3. **P2-C 字段语义**：`compute_structure_subscore` 的 `components["market_cap"]` 中 `percentile`/`extreme` → `btc_dominance_percentile`/`btc_dominance_extreme`（**值不变**，仍是 BTC 占比分位/极值；改的是名实相符），同步更新前端唯一消费点 `structureExtreme` 并留注释说明「本卡高亮由 BTC 占比极端触发，非总市值极端」。
  4. **P2-E UI 误导**：链榜数据源**只有** `flow_7d`/`flow_7d_pct`（`chainRow`），**无法**随 1d/30d 切换（补 1d/30d 需扩 `fetch_chain_flow`，属新功能），故**不动后端**，仅澄清 UI：链榜 section 标签加「（7d，不随时间窗切换）」，卡尾结论文案由「点击切换时间窗」改为「时间窗切换仅作用于叙事榜（链榜固定 7d）」。
  5. **P2-F 键不匹配**：事件日历卡改为渲染 `hardcoded` + `unlock` + `token_events`，**移除恒空 `gecko` 分支**；token 事件按日期序截断 **15 条**并显式标注「另有 N 条未展示」（后端最多 50 条，不截断会撑爆卡片），卡头「仅展示」补总条数、卡尾标注数据源。
- **未做 / 边界（须留档）**：① **P2-D 暂缓** —— `mcap_step=40B` 使总市值 ≈3.8T 时 `mcap_score≈95`、牛市 >4T 恒 100，`market_cap` 占结构子分权重 0.25 且长期近满 ⇒ 结构子分被体量托高；但改相对/对数量程会**直接变更结构子分的展示数值**（评分模型变更），超出本轮 bug 修复范畴，待单独评估。② **P2-E 二级现象未改**：7d 叙事榜排序用 `composite_score`（市值腿 + TVL 腿混合）、1d/30d 用纯涨跌幅 ⇒ 三窗排序口径不一致；但 `composite_score` 是 FEAT-SECTOR-006 既定设计（行内已有 `市值+TVL`/`仅市值` mode 徽章说明），改排序键会**变更默认榜序**，非本轮范畴，仅留档。③ P3-G / P3-H 误报不改。④ 线上 runtime 复验须待 Zeabur 重建后执行（`push ≠ 线上生效`），待核 R1~R4（各表新鲜度 / 部署版本 / 实际渲染值 / `catalyst_events` 是否为空）。
- **校验**：`py_compile`（`macro_market.py`）通过；`index.html` 内联 `<script>` 抽取后 `node --check` 通过；相关探针 `test_macro_market_p0`(**16/16**)、`test_macro_market_board_tier2`(**36/36**) 零失败（全仓无测试引用被改字段 / 事件日历卡）。源码侧改动校验**未连 DB、未落库**（线上 runtime 复验见下节，为只读）。

### 大盘板块线上 runtime 复验（R1~R4，2026-09-27，只读）

按用户「按你的判断处理」执行审计第 3 节待办。入口 `https://crypto-profile-collection.zeabur.app`，prod DB 只读（`05_代码与脚本/scripts/.env` 的 `DATABASE_URL`；该文件已由 `.gitignore:17` 覆盖，未外泄）。**全程未做任何写操作。**

- **R2 部署版本 ✅**：`GET /healthz` → `{"ok":true,"status":"alive"}`；首页 HTML 实测含 `btc_dominance_extreme`(×2)、`tokenEvts`(×6)、`不随时间窗切换`(×1)，而 `DIM_META` / `cycle_dashboard` / `eventCal.gecko` 均 **0** ⇒ `c11ef8f` **已在线上生效**（本次修复上线确认，`push` 已生效）。
- **R3 实际渲染值 ✅**（`GET /api/market/overview`）：`emotion_subscore=57.5/ok/avail_w=1.0`、`structure_subscore=69.9/ok/avail_w=1.0`；`components.market_cap` 键已为 `btc_dominance_percentile`/`btc_dominance_extreme`（实测值 `None`/`NONE`）；`btc_cycle.phase=中性/heat=48.0`；`5板块` narr_ranked=10、chain_ranked=5、`category_flow.status=ok`；`event_calendar` hardcoded=6 / unlock=**0** / token_events=**50** ⇒ **P2-F 修复后真实解锁日程首次可见**（原 50 条算了不渲染，现已进卡，示例 `2026-09-28 SAFE 解锁 1.35%`）。
- **R4 `catalyst_events` ⚠️ 确认为空**：`opportunity_list.catalyst_events = {total:0, error:0, window_days:14}` ⇒ 大盘级 catalyst 管线无产出（上游数据/管线问题，**非前端缺陷**）；同域 `onchain_anomalies` 有 6 条（窗口 24h、2 KOL），`highlight_signals=10`、`risk_signals=4`。
- **R1 各表新鲜度**（DB `NOW()=2026-09-27 06:34Z`）：

  | 表 | 最新 | 滞后 | 判定 |
  |---|---|---|---|
  | `biz.cm_asset_onchain_daily`（温度计源） | `metric_date=2026-09-25` | **2 天** | ⚠️ 滞后 |
  | `src_dl.chain_tvl_snapshot`（链榜源） | `snapshot_date=2026-09-23` | **4 天** | ⚠️ 明显滞后（审计记「滞后 3 天」，实际已 4 天） |
  | `biz.kol_signal`（链上异动源） | `created_at=2026-09-27 06:20Z` | ~14 分钟 | ✅ 新鲜 |
  | `biz.asset_unlock_event`（解锁源） | `updated_at=2026-09-26 22:33Z` | ~8 小时 | ✅ 可接受（未来日程充足，`unlock_date` 远至 2030） |
  | `biz.etf_flow_daily`（机构源） | `flow_date=2026-09-25` | **2 天** | ⚠️ 滞后（ETF 惯例 T+1） |

  - **附带发现（供 P2-D 评估参考）**：`market_cap.btc_dominance_percentile = None` ⇒ BTC 占比**历史序列** `status != ok`（序列缺失），使 `market_cap` 的「极端」判定**恒不触发** ⇒ 结构子分卡 `extreme-high` 高亮**永不出现**；与 `mcap_step=40B` 量程饱和叠加，意味着结构子分当前只有「量程腿」在起作用。
  - **判定：上述 3 处滞后属数据/调度侧（需写操作），不在本轮只读复验范围，未处理**，建议另开数据管道工单。

### 新增缺陷 · 温度计裸调用 500（2026-09-27，本轮 runtime 复验发现并修复）

- **现象**：`GET /api/market/onchain-thermometer`（**不带** `limit`）稳定 **500** `{"error":"'<' not supported between instances of 'str' and 'int'"}`；带 `?limit=10` → **200**。审计第 3 节曾把该端点标 ✅ —— 因当时按带参路径核验，未覆盖缺参路径。
- **根因**：`app.py` 原为 `request.args.get("limit", "10", type=int)`。Flask/Werkzeug 的 `MultiDict.get(k, default, type=...)` **仅在键存在时**对值施加 `type`；**键缺失时直接返回 `default` 本身、不做转换** ⇒ 裸调用得到字符串 `"10"` ⇒ `min(30, "10")` 抛 TypeError。已用 `MultiDict()` 本地复现同一异常、验证 `int` 默认可修复（`max(3,min(30,10))=10`），与线上 200/500 差异完全吻合。
- **影响面**：**前端不受影响**（`index.html:14483` 显式调 `?limit=10`）；受影响的是省略参数的 API 直连 / 探针 / 监控。属**潜伏缺陷而非 UI 故障**，故静态审计与浏览器核验均未捕获 —— 这正是 runtime 复验的价值。
- **修复**：`app.py` 默认值 `"10"` → `10`，并加注释固化这条 Flask 语义坑。全仓扫描 `args.get(..., type=int|float)` **仅此一处**该模式（`app.py` 共 23 处 `type=`，其余默认值/用法正常）。
- **同类端点数**：同批裸调用扫描 `/api/market/{hot,long-tail,gainers,volume,sector-heatmap,overview,backtest}`、`/api/cm/{mvrv,activity,valuation}` **全部 200**，**仅温度计 500**。
- **校验**：`py_compile(app.py)` 通过 + `MultiDict` 机制复现/修复验证通过。线上生效确认须待 Zeabur 重建后重跑裸调用（`push ≠ 线上生效`）。
- **线上生效复验 ✅（2026-09-27 14:47 CST，push 后约 8 分钟）**：重建窗口内 `/healthz` 与裸调用均短暂 **502（Bad Gateway）**（容器滚动重启，非缺陷）；重建完成后 `GET /api/market/onchain-thermometer`（裸）→ **200**，`?limit=10` → 200 ⇒ 修复确认上线。**教训：push 后 6~8 分钟内 `500/502` 不代表修复失败，须等重建完成再判定。**

### 大盘板块全板块 runtime 复验续（R4 更正 + 2 个真实缺陷，2026-09-27，本提交）

上节 R4 把「`catalyst_events` 空 / `onchain_anomalies` 有 6 条」记为数据侧问题。本轮复验发现**该判定为误**，实际是**两个代码缺陷**（且会被缓存掩盖、间歇翻转）。

- **R4 更正（先撂）**：再取 `/api/market/overview`（`HTTP=200`，耗时 ~109s 冷算）得 `opportunity_list.catalyst_events = {total: 50, window_days: 14}`（**50 条，正常**），而 `onchain_anomalies = {total: 0, n_kols: 0, error: "invalid literal for int() with base 10: 'signal_id'"}`。即 **R4 的「有 6 条」与「空」两个结论在两次采集中互换**。只读直连 prod 复跑 `get_market_catalysts(window_days=14, limit=50)` 的聚合 SQL 得 `rows=4400`（近 14 天 `biz.asset_catalyst` 11956 条、其中 4400 条有 `catalyst_impact` 关联，`max_published_at` ≈ 采集前 4 分钟）⇒ **催化剂管线本身健康，R4「上游无产出」结论作废**。
  - **决定性补证（同根因）**：审计报告 §7.3 记的 R4 原始值 `catalyst_events = {..., "error": "0", "total": 0}` —— `str(e) == "0"` 正是 **`KeyError: 0`**。`get_market_catalysts` 在 `cat_ids = [r[0] for r in rows]` 处对 **dict 行**取下标 `r[0]` 即抛 `KeyError: 0`。**与缺陷 ① 是同一个「池被 `dict_row` 毒化」根因**：拿到毒化连接的是 `get_market_catalysts` → 报 `KeyError: 0`；拿到的是 `get_onchain_anomalies` → 报 `int('signal_id')`。两者互为「谁踩中谁挂」，这正是 R4 与本次采集结论互换的机制。**结论：R4 的催化剂「0 条 / 上游无产出」不是数据问题，是代码缺陷（已修）。**

- **缺陷 ①（P1）连接池 `row_factory` 毒化 → 链上异动整块失效**
  - **根因**：`macro_market.py` 的 `fetch_binance_etf_flows()` 在**池内借出的连接**上写 `conn.row_factory = psycopg.rows.dict_row`（旧 `:1352`）。`get_connection` 走全局连接池（`crypto_research/db/conn.py`，`max_size=5`），用完只 `commit()` + 归还、**不重置 `row_factory`** ⇒ 该连接被「毒化」，之后任何复用它的代码都拿到 **dict 行**。`get_onchain_anomalies()` 按 **16 元素元组解包**，拿到 dict 时解出的是 **key** ⇒ `int(sid)` 即 `int('signal_id')`，异常文案与首个 SELECT 列名逐字吻合 ⇒ 被自身 `except` 吞掉 → 该板块 `total=0`。
  - **为什么间歇且翻转**：是否踩中取决于池里拿到哪条连接；`overview` 缓存（`CACHE_TTL=180s` 新鲜 / `STALE_TTL=2h` 陈旧）会把某次毒化结果固化数分钟~2 小时。**这是「值会自己变」的典型症状，静态审计必漏。**
  - **修复**：`fetch_binance_etf_flows()` 改为 `conn.cursor(row_factory=psycopg.rows.dict_row)`（cursor 级，不污染连接）。
  - **护栏（防同类复发）**：`crypto_research/db/conn.py` 的 `get_connection` 在每次**借出**时 `conn.row_factory = tuple_row` 归位，使「忘记重置」不能再污染他人。全仓扫描 `conn.row_factory =` 赋值**仅此 1 处**（已清除）。
  - **验证（只读）**：`A1` 赋值已清除；`A2` 人为毒化后下一次借出实测 `row_factory = tuple_row` ✅；`A3` 先跑 `fetch_binance_etf_flows()`（旧版即毒化点）再跑 `get_onchain_anomalies()` → `total=6, n_kols=2, error=None` ✅（与 R4 首采的 6 条一致）。

- **缺陷 ②（P1）`dl_pipeline` 因 `KeyError: 'new'` 中止 → 链榜 TVL 滞后 4 天**
  - **证据（`sys.task` + `sys.task_log`，只读）**：`dl_pipeline` 最近两次 **failed / `exit code 1`**，且**均在 4 秒内死掉**（09-24 20:00Z、09-26 20:00Z）；上一次成功 09-23 20:00Z —— 与 `src_dl.chain_tvl_snapshot` 的 `MAX(snapshot_date)=2026-09-23` **完全对齐**（该表近 12 天分布：09-23 起往前每天 100 行齐整，之后**断在 09-23**）。
  - **根因**：`scripts/bin/bootstrap_dl_assets_batch.py`（流水线第 ② 步）`kind_counts = _count_kinds(matched)` 的初始桶只有 `{"cmc","gecko","symbol"}`，而写入侧第 245 行要 `kind_counts["new"] += 1`（新建资产桶）⇒ 只要 `unmatched` 非空即 `KeyError: 'new'`；`run_dl_pipeline.py` 遇非零退出码立即**中止整条流水线**，后面的链 TVL 步骤（③）**永远跑不到**。历史能过是因为当时 `unmatched` 恰为空（`--limit 1000` 批次里没有新协议）。
  - **修复**：`_count_kinds()` 预置 `"new": 0`（并补注释说明该桶语义与事故）。
  - **验证**：`py_compile` 通过；`_count_kinds([])` / `[{"match_kind":"cmc"}]` / `[{"match_kind":None}]` 三态均含 `new` 桶、`d["new"] += 1` 不再 KeyError ✅。
  - **教训（设计侧，未改）**：流水线「某一步失败 → 整条中止」会让**无关板块**（链榜）长时间静默停更，且 `scheduler_watchdog` 不感知「表级滞后」。建议后续为 ③ 之后的关键落库步骤加**独立性/新鲜度看门狗**（本轮未做，避免范围外改动）。

- **R1 三张滞后表定性（只读，结论收口）**
  | 表 | 最新 | 定性 |
  |---|---|---|
  | `src_dl.chain_tvl_snapshot` | `2026-09-23` | ❌ **真缺陷**（上游 `dl_pipeline` 中止，已修，见缺陷 ②） |
  | `biz.etf_flow_daily` | `2026-09-25` | ✅ **正常**：今日 `2026-09-27` 为**周日**，美股/ETF 休市，09-25(周五) 即最近交易日；`ingest_cryptoetf_flow` 最近两次均 `done` |
  | `biz.cm_asset_onchain_daily` | `2026-09-25` | ⚠️ 待观察：`cm_incremental` 每日 `done`（今日 06:30 CST 已跑），但日期分布**缺 09-24**、且无 09-26；每日行数恒为 16（非残缺写入）⇒ 疑 CMC 上游可用性延迟，**非本轮代码缺陷** |
  - 附：`catalyst_events` 已回归 50 条 ⇒ 大盘级催化剂**无需**数据管道工单。

- **写操作执行（用户授权后，2026-09-27 15:00~15:13 CST）**：重跑 dl_pipeline 回填链 TVL。
  - **安全裁剪**：**未**走官方 `run_dl_pipeline.py` 整链，改为逐脚本执行 **①②③④⑥⑦**，**主动跳过 ⑤（`run_refresh_sectors.py`）与 ⑧（`etl_sector_flow_daily.py`）** —— 这两步及其依赖（`crypto_research/mapping/sector.py`、`scripts/sql/biz/{create_asset_sector,refresh_sectors_multi_source}.sql`）**当时正被并行会话修改且未提交**，跑整链会把半成品赛道逻辑写进 prod。⑨（`dedup_assets.py --apply`）一并跳过以收窄写面。
  - **结果**：① 8386 协议 → ② **`{"status":"success","total":27,"matched":12,"new_assets":5,"mapped":17,"by_kind":{"cmc":2,"gecko":1,"symbol":9,"new":5}}`**（`by_kind` 出现 `"new": 5` ⇒ **旧代码在此必 `KeyError`，修复在 prod 生效的直接证据**；第 2 轮 `total=10/mapped=0` 收敛）→ ③④ 退出码 0 → ⑥ `status: ok` → ⑦ `status: success`。
  - **数据复验（只读）**：`src_dl.chain_tvl_snapshot` `MAX(snapshot_date)=` **`2026-09-27`**（当日 100 条），`biz.protocol_metric_daily` `MAX(metric_date)=2026-09-27` ⇒ **链榜数据源恢复新鲜**（原卡 09-23）。
  - **未回填**：`chain_tvl_snapshot` 的 **09-24 / 09-25 / 09-26 三天仍缺**（⑦ 只写「当日」快照，不补历史）。大盘板块只消费「最新」快照，无影响；若需连续序列须另写历史回填。

- **线上生效复验 ✅（2026-09-27 15:16 CST，push 后约 18 分钟）**：`GET /api/market/overview` → `HTTP=200`：`onchain_anomalies = {total: 6, n_kols: 2}`（**error 键消失** ⇒ 缺陷 ① 上线确认），`catalyst_events = {total: 50, window_days: 14}`；`5板块` chain_ranked=5 / narr_ranked=10；`emotion_subscore=57.5`、`structure_subscore=69.9`（与 R3 一致，未因改动漂移）。

- **本轮校验**：`py_compile`（`macro_market.py` / `conn.py` / `bootstrap_dl_assets_batch.py`）通过；`test_macro_market_p0` **16/16**、`test_macro_market_board_tier2` **36/36**；修复验证探针全绿（详见缺陷 ①② 的「验证」行）；prod 写操作已获用户显式授权，且做了写面裁剪。

- **附带收口（只读）**：`btc_dominance_percentile = None` **非缺陷、是已知缺口** —— `fetch_btc_dominance_history()`（`macro_market.py:649-653`）是**显式桩函数**，恒定返回 `{"status":"error","error":"CMC trial API 无 dominance 历史端点","series":[]}`，docstring 已注明「需 CoinMetrics CapBTC.DOM 或其他历史源才能算百分位」。故 P2-D 评估时不应把「极值高亮永不触发」当作 bug，而是**数据源未接**。

### 投研页三档确定性框架落地（上游 `方案_三档确定性框架_2026-09-27.md`，2026-09-27，本次提交 `df331d0`）

用户三轮指令：评估上游方案可行性 → 把可落地方案写入 `04_架构与代码方案/` → 「你可以直接按优化方案干」。落地方案文档：`04_架构与代码方案/投研页三档确定性可落地方案_2026-09-27.md`。

**核心设计：分档 = 分证据来源，不是给同一分数改权重。**
- **短期 S（T+0~14d）= 结构化量化数据「可复现性」口径**，输入 5 项（CVD 24h / OI 变化 / 资金费率 / 抛压评分 / 近端解锁状态，present 即证明已采集），分 = 完整度×0.5 + 新鲜度×0.3 + 方向一致×0.2，**不受 39.9 零证据封顶约束**（仅描述资金面状态，不含方向概率 → 命名为「数据完整度」而非「确定性」）。
- **中期 M（2~12 周）= 文本 URL 引用可核验口径**，`kind ∈ {catalyst, risk, thesis}`，权重 `coverage 0.50 / freshness 0.20 / sample 0.30`，保留封顶。
- **长期 L（季度~年）= 先过存在性硬门槛**（审计 / 公开代码库 / 治理三项全 1），过了才走 `coverage 0.55 / freshness 0.15 / sample 0.30`（同样保留封顶）。任一为 0 → `not_evaluable`，**结构上不被 S/M 档拉升**。
- **M/L 刻意不含 `consistency`**：`_consistency_dim()` 只读 24h 衍生品，对 2–12 周/季度-年无语义（上游「复用四维只调权重」语义不成立）。该维度**仅在 S 档使用**。

**上游方案四处不可落地（已在文档 §1 附代码证据留档）**：
1. **致命自相矛盾**：上游验收 #3（PONS 短期档 ≥60）与 #7（coverage=0 时封顶 39.9）互斥 —— `coverage` 不分档、三档共用同一个 0 ⇒ 全压 39.9 ⇒ 短期闸门永远打不开。**出路 = 口径分离**（S 档改走量化「可复现性」口径），而非调权重。
2. 「复用四维、只改权重」在语义上不成立（四维无期限结构，见上）。
3. 「上线前回测钉死阈值」客观做不到：`backtest_opportunities.py` 的 `SIGNAL_HORIZONS` 最长 30 天、快照全史约 25 天，且回测对象是 `biz.market_overview_snapshot` 的机会清单**而非** `biz.research_thesis` ⇒ **无法校准中/长档阈值**（故闸门一律标注 `uncalibrated`，用结构条件替代上游的绝对分 60/50）。
4. 上游 P0「板块重分类验收」无法通过：`sector.py` 的 `SECTORS` 枚举**无 `launchpad`**，`DL_CATEGORY_SECTOR_MAP["launchpad"] = ("defi", 0.65)` 是**刻意归一化**。
   - 附带更正：上游把「missing 二分」当新增能力是**误判** —— `_compute_missing_materials_inner` 早已输出 `impact_dimension` / `impact_desc` / `determinism_gain`。

**改动（2 个代码文件 + 1 新探针 + 1 新文档，零 DDL、零迁移、不改既有 `_compute_determinism` 行为）**：
- `db_stats.py`（7 处编辑，+477 行）：① L4256 后插入三档常量块 —— `_TIER_CALIBRATION(_NOTE)` / `_HORIZON_BY_KIND` / `_HORIZON_LABELS` / `_SHORT_TERM_INPUTS(5 项)` / `_SHORT_TERM_WEIGHTS` / `_SHORT_TERM_FRESH_MAX_H=24,_SOFT_H=168` / `_TIER_WEIGHTS_3`（m/l，均**不含** consistency）/ `_LONG_TERM_EXISTENCE_KEYS` / `_NEAR_UNLOCK_DAYS,_PCT` / `_M_HORIZON_MIN=14,_MAX=90` / `_MATERIAL_HORIZON`（21 类资料 key → (期限, 性质)）/ `_MISSING_NATURE_LABELS`。② `_iter_thesis_points` 新增 `_horizon_for_kind(kind)`，每点带 `horizon`（未知 kind 返回 `None`，**不静默落默认档**）。③ `_compute_evidence_stats` 签名加 `points: list | None = None`（默认内部重算 ⇒ **向后兼容，默认行为不变**；既有 127 断言据此不改）。④ `_compute_determinism` 后插入约 340 行三档函数块：`_tier_of` / `_sm_get` / `_days_until` / `_tier_point_sets`（未标注归中档并单独计数）/ `_short_term_evidence` / `_near_unlock_pressure` / `_short_term_gate` / `_verifiable_window`（解锁落 [14,90] 天或已有催化剂）/ `_mid_term_gate` / `_long_term_existence` / `_compose_3tier` / `_compute_determinism_3tier`。⑤⑥ 读取侧（`get_or_create_research_notebook` 正常路径 + 降级分支）与 ⑦ 生成侧（`generate_research_thesis`）三处接线 `analysis["determinism_3tier"]`。⑧ `_compute_missing_materials_inner` 条目补 `horizon` / `horizon_label` / `missing_nature(_label)` / `determinism_gain_by_tier`（**保留原 `determinism_gain`**）。
- `templates/research.html`（+203 行）：新增 `.t-3t*` 样式 + 三档卡渲染块（每档展示 分数 / 档位 / 证据口径 / 论点条数 / 闸门开闭与未开原因 / notes）+ 综合结论卡（`composite.verdict` + `lines` + 「不得用高确定性档掩盖低确定性档」规则）+ 「闸门阈值未校准」徽标（title 挂 `calibration_note`）；缺失项块改为按档展示「补全后：短期档 X → Y；中期档…」（`determinism_gain_by_tier` 与 `determinism_3tier[tier].score` 对算，**旧 `determinism_gain` 保留为回退**）+ 透出 `horizon_label` 与 `missing_nature_label`。
- 新探针 `test_research_3tier_20260927.py`（**82 断言 / 0 失败**，纯离线不连库不连网），覆盖方案 §6 编号 1/2/3/4/5/6/7/9：结构完整性、期限覆盖率 100%、**口径分离**（同 fixture 下 S 档 ≥70 越封顶而 M/L 仍封顶 unusable）、**存在性硬门槛**（三项全 0 → `not_evaluable`，且 S 档从 0 输入变满分时 L 档判定与 existence 结果**逐字不变**）、**文本档防骗不回退**（28 条空 URL 引用 → M/L 封顶 + `weak_cited_points>0`，S 档分数不受影响）、未校准标注、前端钩子源码级断言、**源码级口径隔离**（`_short_term_evidence` / `_short_term_gate` / `_near_unlock_pressure` 三个函数源码中**不得出现** `_compute_evidence_stats` 与 `_iter_thesis_points`；`_compute_determinism_3tier` 中 `_ZERO_EVIDENCE_SCORE_CAP` 出现 ≥2 次 ⇒ 封顶只施加于 m/l 两处）。

**验证**：`test_research_3tier_20260927.py` **82/0**；既有防骗回归 `test_research_determinacy_20260926.py` **127/0**（未回退）；`py_compile db_stats.py` 通过；`research.html` 内联 `<script>`（`{{ }}` 占位替换后）`node --check` 通过。

**未做 / 边界（须留档）**：
- **P3 板块重分类（PONS `defi` → `launchpad`）未做**：改分类会影响赛道聚合与竞品选择，且 `SECTORS` 无 `launchpad` 枚举（需同步 taxonomy + DL 映射 + 前端标签），须独立开工单评估。
- **P4 前向跟踪表 `biz.thesis_forward_track` 未建**：按方案 §8 拍板「本期只建表 + 写入、回填任务随日级调度」，本轮**连建表也未做**（未获数据写入授权）。
- **闸门阈值全部 `uncalibrated`**：受限于回测框架上限（见上第 3 点），任何「闸门已开 = 可下注」的读法都不成立，前端已显式标注。
- **存量 `biz.research_thesis` 行不含 `determinism_3tier`**：读取路径会**实时计算**并注入，故旧行无需回填；但 `analysis_json` 落库字段仅对**新生成**结论生效。
- **线上 runtime 复验已完成（2026-09-27，Zeabur 重建后，只读）✅**：`GET https://crypto-profile-collection.zeabur.app/api/research/11114/notebook`（响应外层 `{ok, data}`，结论在 `data.thesis.analysis`）→ `analysis.determinism_3tier` 三档齐备：**S `92.0 / actionable / structured_quant / gate_open=True`**、**M `32.5 / unusable / text_citation / gate_open=False`**、**L `None / not_evaluable / text_citation / gate_open=False`**；`unmapped_points=0`、`calibration=uncalibrated`、`composite.verdict="仅适合短线/事件驱动；长期不可评估，禁止长期持有叙事"`。`data.missing` 的 `audit_report` 条目已带 `horizon=l / horizon_label=长期 / missing_nature=existence_gate / missing_nature_label=长期档存在性门槛 / determinism_gain_by_tier={l:5,m:0,s:0}`。**口径分离线上实证**：同一结论 `analysis.score=39.9 / unusable`（整体文本口径仍封顶、防骗未回退），而 S 档 92.0 越封顶且闸门开 —— 与方案 §8 拍板 ①（接受 S 档口径分离）一致；M 档 32.5 **低于** 39.9 而非等于，印证封顶是**上限（ceiling）**而非等值（有 3 个原始断言写成 `==` 曾误判为失败，已改为 `<=` + 追加 `tier == "unusable"`）。

### P4 投研结论前向跟踪表落地（`biz.thesis_forward_track`，2026-09-27，本次提交）

用户拍板范围：上一轮「剩余独立轨」多选**只勾了 P4 前向跟踪表**，**未勾 P3 板块重分类**（故本轮不新增 `launchpad` 赛道枚举、不动竞品分池键）。对应方案 §1.3 / §5：`biz.research_thesis` 此前**没有任何前向收益链路**，中/长档闸门阈值客观无数据可校准；本表补上「结论 → 前向收益」链路，作为后续校准基建。

**改动（2 代码文件 + 1 新迁移 + 1 新脚本 + 1 新探针，零删除、不改既有评分/闸门口径）**：
- 新迁移 `scripts/migrations/fix_072_thesis_forward_track.sql`：`CREATE TABLE IF NOT EXISTS biz.thesis_forward_track`（`track_id / thesis_id / asset_id / as_of / tier_s_score / tier_m_score / tier_l_evaluable / gate_s_open / gate_m_open / price_at / ret_t7_pct / ret_t30_pct / ret_t90_pct / filled_at`，`UNIQUE (asset_id, as_of)`）+ `idx_thesis_forward_due ON (as_of) WHERE filled_at IS NULL`（partial index，表增长后扫描仍轻量）+ 4 条 `COMMENT`。**编号取 072 而非 071**：工作树中并行会话已有未跟踪的 `fix_071_launchpad_sector.sql`（P3 轨），避免同号歧义。
- `db_stats.py`：① `_ensure_research_tables` 追加同一份幂等 DDL（容器内自愈），并在函数末尾**立即 `conn.commit()`**（项目教训：DDL 取 `AccessExclusiveLock` 不及时 commit 会阻塞表读 15 分钟以上）。② `get_latest_research_thesis` 后新增 P4 块（约 140 行）：`_THESIS_FORWARD_HORIZONS`（7/30/90 → 列名，单一来源）、`_thesis_forward_payload()`（从 `determinism_3tier` 抽字段，L 档 `tier != "not_evaluable"` → `tier_l_evaluable`；缺档位一律 `None` **不臆造**）、`_forward_return_pct()`（`(到期价/基准价-1)×100`，任一侧缺失或非正 → `None`，**不写假数**）、`_fetch_as_of_price()`、`backfill_thesis_forward_track()`。③ `generate_research_thesis` 版本留痕之后接线 upsert：`ON CONFLICT (asset_id, as_of) DO UPDATE` 只更新档位分数与闸门，**不触碰 `ret_*`**（同资产同日重复生成不回退已填收益），`price_at` 用 `COALESCE(EXCLUDED.price_at, 表内已有值)` 保护，`as_of` 取 `(CURRENT_DATE AT TIME ZONE 'Asia/Shanghai')::date`（与项目北京时间口径一致）。
- 新脚本 `scripts/bin/backfill_thesis_forward_track.py`：薄壳，调 `db_stats.backfill_thesis_forward_track()` 并打印四计数（复用 `recompute_unlock_pressure.py` 的 workbench/prod 双路径解析写法）。
- `scheduler.py`：注册日级任务 `("thesis_forward_backfill", "50 7 * * *", ...)`，07:50 触发 —— 位于 `asset_market_daily` 06:15 ETL 之后、避开早高峰，落在 `data_sync_daily`(06:30) 窗口之外。
- 新探针 `test_thesis_forward_track_20260927.py`（**63 断言 / 0 失败**，纯离线不连库不连网）：DDL 双份齐备（迁移 + `_ensure_research_tables`）+ `conn.commit()` 位于 DDL 块之后、唯一键/partial index 存在、12 列齐备；`_thesis_forward_payload` 抽取（含与 `_compute_determinism_3tier` 真实输出**同源**断言、L 档三态）；`_forward_return_pct` 正负收益与 8 组非法价（`None`/`0`/负数/空串/非数）一律 `None`；写入侧「upsert 分支不含 `ret_*`」+ `COALESCE` 保护 + 北京日期 + 基准价取「as_of 及之前最近收盘价」；回填侧三档到期判定、未到期/已填跳过、`price_at` 空提前 `continue` 且计数、三期全非空才置 `filled_at`；脚本存在 + 调度 key/cron/脚本名一致。

**回填语义（幂等）**：只填「已到期（`as_of + N <= 北京今日`）且 该期次为空 且 `price_at` 非空」的格；到期价取「到期日当日或之后首个可得收盘价」（`source_code IN ('cmc','cmc_historical')`，同日 cmc 优先，与 `_build_structured_metrics_from_snapshot` 同口径）；三期全非空 → 置 `filled_at` 离开到期扫描索引；`price_at` 为空的行三期整体跳过（无基准价算不出收益，不写假数），计入 `skipped_no_price` 可观测。

**验证**：`test_thesis_forward_track_20260927.py` **63/0**；既有回归 `test_research_3tier_20260927.py` **82/0**、`test_research_determinacy_20260926.py` **127/0**（未回退）；`py_compile`（`db_stats.py` / `scheduler.py` / 新脚本 / 新探针）通过。**迁移已 apply 到 prod（幂等）并复验**：`information_schema.columns` 返回 14 列（含 `track_id`）齐备，`pg_indexes` 返回 `thesis_forward_track_pkey / uq_thesis_forward / idx_thesis_forward_due`，当前 0 行。

**偏离方案原文的 1 处（须留档）**：方案 §5 原文要求 `price_at = as_of 当日收盘价，取不到留 NULL`。落地改为**取 `market_date <= as_of` 的最近收盘价**（基准价非空优先）。原因：结论多在盘中生成，而 `etl_asset_market_daily_from_cmc` 每 6 小时才写日行，严格取「当日」会在当日 ETL 未完成时留 `price_at = NULL`，叠加「`price_at` 空则跳过」⇒ 该行**永久无法回填**（死行）。改法代价是极小概率用前一日收盘价作基准（T+7 收益含一日偏移），已在迁移 `COMMENT ON COLUMN price_at` 与函数 docstring 中显式说明。

**未做 / 边界（须留档）**：
- **P3 板块重分类仍未做**（本轮未被选中）：`SECTORS` 无 `launchpad` 枚举，须独立工单；竞品分池键未动。
- **闸门阈值仍全部 `uncalibrated`**：本表只是**校准基建**，方案 §5 明确「S 档需 ≥3 个月、M 档 ≥6 个月、L 档 ≥1 年才有样本」；在样本积累前，任何「按 `gate_s_open` 分组比较 `ret_t30_pct`」的结论都**不得宣称统计显著**，前端「未校准」徽标保持不变。
- **前向跟踪暂未接入任何前端/告警消费**：写入与回填是纯基建，无读取路径（避免在样本不足时被误读为业绩展示）。
- **降级结论写 `thesis_id = 0` 的分支未实现**：`_build_fallback_thesis` 走的是读取侧实时拼装、**不落库**，故当前只有 `generate_research_thesis` 真实生成路径会写跟踪行（表结构与注释已预留 `thesis_id=0` 语义）。
- **线上 runtime 复验已完成（2026-09-27，Zeabur 重建后）✅**：`POST /api/research/11114/thesis`（异步任务，返回 `{ok,pending,task_id}`，须轮询 `/api/tasks/<id>/log` 而非等待响应体）→ 任务日志走到「研究结论已生成 / 执行完成」（`推断占比 73%（16/22 个论点无引用）> 50%，conviction 由 low 强制锁定为 low`）→ 直连 prod 复查：**`biz.thesis_forward_track` 落行 1 条** —— `track_id=1 / thesis_id=1247 / asset_id=11114 / as_of=2026-09-27（北京今日，时区口径正确）/ tier_s_score=92.0 / tier_m_score=38.1 / tier_l_evaluable=False / gate_s_open=True / gate_m_open=True / price_at=0.627555897822132 / ret_t7·30·90 全 NULL / filled_at NULL`。写入钩子端到端打通，档位分数与闸门均与 `analysis.determinism_3tier` 一致（S 92.0 与既有线上读数吻合），`ret_*`/`filled_at` 保持空是对的（as_of=今日，T+7 未到期）。**另**：本次 m 档 `38.1 / gate_open=True`，与上次线上读数 `32.5 / gate_open=False` 不同 —— 原因是本次生成有 6/22 条论点带**可核验 URL 引用**（此前 0 条 ⇒ coverage=0 ⇒ 封顶且闸门必关），故 `coverage>0` 使中期闸门结构性打开、分数为未封顶真值（38.1 < 39.9 且非封顶上限），与 `_mid_term_gate` 的「中期 coverage > 0」结构条件自洽，**非回退**。
- **回填任务对 prod 实跑复验 ✅**：本地执行 `scripts/bin/backfill_thesis_forward_track.py`（连 prod，`_ensure_research_tables` 幂等 + 只读扫描，无写入）→ `扫描 1 行，填入 0 格，完成 0 行，无基准价跳过 0 行`，即 `as_of + 7/30/90` 到期闸门、partial index 扫描、`price_at` 非空路径在真实 schema 上全部跑通，且当前正确为 **no-op**（唯一行的 as_of = 今日，尚未到期）。

### 盘面告警邮件 4 封审计处置 NEW-G / NEW-F（审计_盘面告警邮件4封_2026-09-27，2026-09-27，本次提交）

来源：`audit_盘面告警邮件4封_2026-09-27.md`（4 封 runtime 样本，生成于 `78010a6` 部署窗口之后）。按用户「按你的意思办」取 **A=NEW-G + B=NEW-F**（两者都便宜、且直接闭环本报告）；**NEW-C（费率年化 8h 硬假设）仍暂缓**（需先扩采集存 settlement-interval 才能按品种换算）。

- **NEW-G（费率刷新饿死，比报告「MOVR 采集缺失」更准）**：报告猜测 MOVR 是「采集缺失或 NEW-B 护栏拦截」。prod 只读实测：`biz.asset_derivatives` **有** MOVR 行，但 `fetched_at=2026-08-27 22:08`（**陈旧 30 天**）⇒ 被 NEW-B 的 24h 护栏挡成 `n/a（未覆盖）`。根因在 `get_signal_gap_assets`：原判定「已覆盖 = 该符号**有任意一行**」，**不看行龄** ⇒ 有旧行的符号**永不进入采集队列**，无论跑多久都停在护栏之外。改名 ≠ 修好，故补「陈旧刷新组」：新增常量 `REFRESH_STALE_H = 12`（**必须显著小于 `scan_daemon.FUNDING_STALE_H=24`**；batch 每 6h 一轮 ⇒ 12h 保证最坏 12+6=18h < 24h），`get_signal_gap_assets(conn, days, refresh_hours=REFRESH_STALE_H)` 内新增「每符号 `MAX(fetched_at)`」查询与 `latest` 字典（keyed 含别名），把「任一别名已覆盖、但**最新一行也超 `refresh_hours`**」的符号并入 `stale` 组，`targets = uncovered | stale`；新增 CLI `--refresh-hours`（0=关闭）。`merge_pending` 的 `cap=max(limit, len(gap))` 保证并入后**不被 `--limit` 截断**。**prod 实测**：`refresh_hours=0 → 0 个；`refresh_hours=12 → **166 个刷新项**（`0G/4/ACE/ACU/AGT/AIN…`，MOVR 在内），且 166/166 均「有行且最新一行超 12h」。**调度无需改**：`scheduler.py` 的 `derivatives_batch` 未传该参数，走默认 12。
- **NEW-F（图例不解释卡片内联 ℹ️）**：报告指出 4 封邮件图例全文 **ℹ️ 出现 0 次**，但卡片内联了两处 ℹ️（「纯技术面信号…缺基本面确认」/「共振方向无新鲜条目，未参与结论」）语义各异、读者无从区分「提示」与「风险警告」。核实后实为**三类**（比报告多一类：「CVD … 与做空结论相反」）。`_render_alert_email` 图例结尾补一段：`ℹ️ = 提示性说明（非风险警告，不改变信号），本封出现三类：①纯技术面… ②共振方向无新鲜条目… ③CVD…与做空结论相反…`。
- **探针**：`test_derivatives_signal_gap.py` 新增【3d】NEW-G 源码事实 5 条 + 把【4】prod 段由「资产级」改为**符号级**（新增 `_sym_stats`，与 `get_signal_gap_assets` 同口径含别名）→ **43 断言 / 0 失败 / 0 跳过**；`test_scan_alert_remaining.py` 新增【NEW-F】3 条 → **26 → 29 断言 / 0 失败**（含生产库：库内陈旧符号 326 个 / funding_map 仅 189 键）。
- **回归零失败**：`test_scan_alert_audit_20260926`(96/0)、`test_scan_alert_header_regime`(135/0)、`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_onchain_addr`(36/0)、`test_major_event_alert`(55/0)；`py_compile`（`phase_derivatives_batch.py` / `scan_daemon.py` / 两探针）通过。
- **未做 / 边界（须留档）**：① **NEW-C 仍暂缓**（本报告用公开 API 实测把证据强化到「本批 5 币里 4 个是 4h 结算 ⇒ 年化低估 50%」，但修它须先扩采集存 `settlement_interval`，本轮未动，图例仍写「×3×365（8h 结算）」）；② NEW-D（LayerZero 双源转载未合并）P3 未做；③ **NEW-A 的降级分支（反向 BRK→medium）仍缺线上样本** —— 本批 4 封全为主池、全做多，零 BRK 卡片；P1-2 同理仅待 BRK 批次佐证；④ 刷新组并入只对**下轮采集**生效，存量陈旧行需等 batch 跑过一轮才被覆盖；⑤ 线上 runtime 复验须待 Zeabur 重建后执行（`push ≠ 线上生效`）。

### PONS 赛道重分类落地（工单 SECTOR-RECLASS-001，2026-09-27，本次提交）

来源：`工单_PONS赛道重分类_SECTOR-RECLASS-001_2026-09-27.md`。走**路线 ①（治本：改映射规则让分类器自己产出 `launchpad`）** —— 因 `refresh_asset_sectors.py` 是「每日全量重建 + `primary_sector` 只从自动分类结果回写」，人工覆盖会被次日刷新抹掉（路线 ② 需新加 override 层，改量更大）。用户选定本工作流后按工单 §八 顺序执行。

- **A 组改码（7 文件）**：`mapping/sector.py`（`SECTORS`/`SECTOR_LABELS` 增 `launchpad`；CMC tag / CG `launchpad` / CG `surge launchpad` / DL `launchpad` 由 `defi` 改向；CMC `category_hint` 补 `launchpad 0.8`；`nft launchpad` **保留 gamefi**）；生产实际生效的 `sql/biz/refresh_sectors_multi_source.sql`（`tag_hits`/`cat_hits`/`cg_hits`/`dl_hits` 共 5 处同步）；展示侧 `templates/index.html`（两份硬编码标签表 + `.sector-launchpad` 配色 `#84cc16`）与 `bin/etl_sector_flow_daily.py`（独立 `SECTOR_LABELS`）。
- **⚠️ 拦截一处会直接炸库的缺陷（比工单原文更严）**：工单给的 CHECK 白名单为 `l1,l2,defi,launchpad,meme,gamefi,rwa,ai,cex_token,derivatives,depin,infra,other`——**漏了 `stablecoin`**。而 prod 现行约束含 `stablecoin`、表内现存 **1046 行 `sector='stablecoin'`** ⇒ 照原文 `ADD CONSTRAINT` 会因存量行校验失败而**整条迁移报错**（DROP 已执行、ADD 失败 → 事务回滚，数据不脏但迁移永远跑不通）。已把 `fix_071` 与基线 `create_asset_sector.sql` 的白名单补回 `stablecoin`，两份均与 `SECTORS` **逐值一致（14 值）**。
- **B6 回滚点**：执行前只读落盘至 `/private/tmp/cpc_sector_rollback_20260927/`（`core_asset_primary_sector.csv` 21833 行 + `biz_asset_sector.csv` 28501 行 + `check_constraint.txt` 旧约束定义）。
- **B3 DDL（prod，已执行）**：`apply_migration.py fix_071_launchpad_sector.sql` → 成功；复查 `pg_get_constraintdef` 现为 14 值，**含 `launchpad` 且保留 `stablecoin`**。DDL 后由 `apply_migration.py` 立即 commit。
- **B4 全量刷新（prod，已执行）**：`bin/run_refresh_sectors.py`（**生产 SQL 路径**，与每日流水线同源）→ `launchpad` **88 个 primary 资产**（`defi` 3810 → 3689）。`biz.asset_sector` 中 `sector='launchpad'` 标签行 **289** 条。PONS(11114)：`core.asset.primary_sector='launchpad'`，`biz.asset_sector = [('launchpad','cmc',0.80,true), ('launchpad','cg',0.65,false)]`，**无 defi 残留**（与 B1/B2 dry-run 的 `merged=[('launchpad',0.8)]` 预测一致）。
- **验收 #2 的 DB 侧（只读实测）**：`get_sector_competitors(11114)` → `sector=launchpad / sector_label=Launchpad 打新平台 / matched_by=sector_only`，竞品换成 Launchpad 同类（`10SET/ADAPAD/ATD/BABI/BSCPAD/BTCBAM/DAISY/DAOP`），**不再出现 Aave/1inch/Aerodrome**。
- **B5 重算 PONS thesis（prod，已执行）**：调 `db_stats.generate_research_thesis(11114)` → 日志「竞品数据：**Launchpad 打新平台** 赛道 8 个对标（匹配方式 sector_only）」「研究结论已生成」，`ok=True`；`biz.research_thesis`(11114) 最新行 `thesis_id=1247` 已刷新，`biz.research_thesis_version` 计 4 行。
- **探针**：`test_sector_taxonomy_20260927.py` **31 → 34 / 0**（新增「两份 SQL 白名单 == `SECTORS` 逐值一致」×2 +「保留 `stablecoin`」×1，防同类漏项复发）。
- **回归零失败**：`test_research_determinacy_20260926`(127/0)、`test_research_3tier_20260927`(82/0)、`test_thesis_forward_track_20260927`(63/0)；`py_compile`（`sector.py` / `etl_sector_flow_daily.py` / 探针）通过。
- **线上 runtime 复验（Zeabur 重建后 15:18，只读）✅**：`GET /api/research/11114/notebook` → `sector=launchpad / sector_label=Launchpad 打新平台`；`GET /api/research/11114/competitors` → `sector=launchpad / sector_label=Launchpad 打新平台 / matched_by=sector_only`，竞品 = `PONS/10SET/ADAPAD/ATD/BABI/BSCPAD/BTCBAM/DAISY/DAOP`，**DeFi 蓝筹（AAVE/1INCH/AERO/UNI/MKR）零残留**（验收 #2 达成）。重建窗口实证：push 后约 7 分钟内 `sector_label` 仍回退裸 `launchpad`（旧码 `SECTOR_LABELS.get(k,k)`），重建后才变中文标签。
- **sticky 验证（验收 #5）✅**：`run_refresh_sectors.py` **再跑一次** → `launchpad` 仍 88、PONS `primary_sector` 仍 `launchpad`（规则天然 sticky，无需 override 层）。
- **未做 / 边界（须留档）**：① **不给 PONS 补 DeFiLlama 映射** —— DL 对 `pons-v1/v2` 的 `tvl`/`currentChainTvls` 本身为空，映射了也拿不到 TVL（遗留 #2 关闭为「数据源无该指标」）；② **`SECTOR_COLLECT_PRIORITY`/`SECTOR_TOPIC_PRIORITY`/`SECTOR_SCORE_WEIGHTS` 未配置 `launchpad`**：采集/主题优先级回退 `other`、评分权重回退默认，launchpad 资产投研资料清单会退化为 other 的通用主题集（不含 `tge_ido`/`exchange_listing` 等发射台强相关主题），属后续独立小改动；③ `etl_sector_flow_daily.py` 中历史命名 `sector_12` 不改（消费方按行动态读，新增 launchpad 行自动生效）；④ 规则改动对全库 launchpad 类资产生效（88 个 primary），不做逐资产手工调整；⑤ 验收 #3「资金流按 launchpad 分组」需等 `etl_sector_flow_daily` 日级任务写入 `biz.sector_flow_daily` 的 launchpad 行（`sector_key` 无 CHECK，可正常写入）。

### 大盘分析遗留三项落地（工单 OBI-OPT-MARKET-OVERVIEW-002，2026-09-27，本次提交）

来源：`工单_大盘分析遗留三项_P2-D_板块排序_催化日历_2026-09-27.md`。用户授权后执行 **D + E2**；**R4 已闭环，工单 §4 整段前提作废**。

- **R4 复核（未改任何 `get_market_catalysts` 相关码）**：线上实测 `opportunity_list.catalyst_events` = `{total: 50, list: 50 条, window_days: 14}`，**`error` 键已不存在**。工单 §4.1 记的 `error:"0"` 不是「无异常哨兵」，而是 `str(KeyError(0))` —— `cat_ids = [r[0] for r in rows]` 在 **dict 行**上取下标即抛 `KeyError: 0`，根因是连接池被 `dict_row` 毒化（`c6b009e` 修）。故 §4 的 Phase 2A/2B 均不执行；另 §4.3 的 D1/D2 SQL 列名 `pub_at` 应为 `published_at`，§6「未改码未落库」声明已过时。
- **D（体量分项线性量程饱和 → 对数刻度）**：`market_rules.yaml` 的 `mcap_step: 40B` 换成 `mcap_log_low: 800000000000` / `mcap_log_high: 12000000000000`；`macro_market.compute_structure_subscore` 的 `market_cap` 分项改 `ratio = (log10(mcap) − log10(lo)) / (log10(hi) − log10(lo))`，`hi ≤ lo` 或无市值时退回中性 50（不除零）。**⚠️ 工单 diff 有遗漏，照抄会静默失效**：`_load_market_rules` 只覆盖「target 已存在的键」（`if k in target`），新键必须同时登记进 `SCORING_TUNING_DEFAULT`，否则被整段丢弃并触发 `KeyError`；已在默认表登记并加注释说明。
- **D 实测（非工单预估）**：线上 `total_market_cap = 2.9011 T`（工单按 3.8 T 估算 ⇒ 前提失真）。`market_cap.score` **72.53 → 47.57**（−24.96），结构子分 **69.91 → 63.67**（−6.24；工单预估约 −9，差异来自 mcap 腿实际仅降 25 而非 37）。区分度已恢复：线性 4 T 即恒 100，对数 4 T = 59.4 / 6 T = 74.4 / 12 T = 100。
- **D 验收窗口偏离（须留档）**：工单 §2.4 写 `market_cap.score ∈ [50,65]`，实测 **47.57 落在窗口外下方**（因工单按 3.8 T 估算）。**保留工单「已定死」的量程锚点（0.8 T/12 T），把验收口径按实测改为 ≈47.6**，而非为凑窗口下调 `mcap_log_low`（那是 target-fitting）。若产品认为 47.6 偏低，改 `mcap_log_low` 一个数即可（0.5 T → 57.8）。
- **E2（板块叙事榜三窗口径不一 → 统一合成分）**：后端 `build_narrative_flow_ranking` 新增 `_composite()` 与字段 `composite_score_1d` / `composite_score_30d`（公式：`mc_w × 该窗市值腿 + tvl_w × TVL腿`，无 TVL 腿则仅市值腿；该窗市值腿缺失返回 `None`，由前端回落 7d，避免空刻度排榜首）；前端 `getNarrChangeByWindow` 的 1d/30d 分支由 `mcap_change_*_pct` 改为取上述合成分。7d 分支不变（市值腿仍是 `momentum_score` 三窗混合分）。
- **E2 口径取证（工单未定死，须留档）**：工单说「用该窗 `mcap_change` + `tvl_change` 加权」，但 **TVL 腿只有 7d** —— `fetch_category_tvl_flow` 聚合 DeFiLlama `/protocols`，源仅给 `change_7d`，无 1d/30d 口径。故 1d/30d 的合成值 = 该窗市值涨跌幅 + **沿用 7d 的 TVL 腿**（即「换市值腿、TVL 腿固定 7d」），而非纯涨跌幅。若要纯该窗口径须先扩 TVL 采集。
- **E2 实测（翻转样例）**：构造 A（blended，1d 涨幅 2% + TVL 腿 20% → 合成 11.0）vs B（mcap_only，1d 涨幅 8% → 合成 8.0）：**旧纯涨幅榜 B 第一，新合成分榜 A 第一，排序已翻转**；公式逐值核对通过。
- **校验**：`py_compile macro_market.py` 通过；`templates/index.html` 内联 `<script>`（1 块）`node --check` 通过；`test_macro_market_p0` **16/16**、`test_macro_market_board_tier2` **36/36**（两探针均未断言 `mcap_step` / 结构子分数值，零回退）。
- **未做 / 边界（须留档）**：① **后端仍按 7d 合成分先截断 `rank_limit=10`，前端再按窗重排** ⇒ 切 1d/30d 是在「7d TOP10 候选集」内重排，非全量重排（工单未要求改，原设计即如此）；若后续要「1d 全量榜」需把截断下移到前端或放宽 `rank_limit`。② 叙事榜行仍以 `toFixed(1)%` 渲染合成分（7d 既有行为，本次未扩大改动面）——合成分不是百分比，1d/30d 也带 `%` 属既有展示缺陷，未在本单修。③ 前端 1d/30d 的 `composite_score_*` 为 `null` 时静默回落 7d 值（数据缺失时该窗与 7d 同序，属预期降级）。④ **线上 runtime 复验须待 Zeabur 重建后执行**（`push ≠ 线上生效`），本单未做。

### A3 抛压分档：阈值外置 yaml + 滞回带（`biz.asset_unlock_pressure.risk_level`，2026-09-27，本次提交）

来源：FIX-DETERMINACY-002 遗留项「A3 验收半达成」——`pressure_score` 已从 0 修到 >0（28.24），但 `risk_level` 仍是 `low`（阈值卡 ≥30）。用户拍板方案：**缓冲带**（阈值外置 + 单一口径 + 滞回），而非口径重构（剥离 ATH 回撤）或维持现状。

**取证（改前，全部为只读实测）**：
- **阈值硬编码破了自家约定**：`_compute_pressure_score` 内联 `>=60→high / >=30→medium`，而 `market_rules.yaml` 头部明写「P2-4 所有评分/背离阈值全外置于此文件」——抛压分档是当时**唯一没外置**的评分阈值。
- **同一字段两套阈值（真 bug 级）**：展示侧 fallback 用 `high>=70 / medium>=40`，主算法用 `60/30`，且二者**分数尺度都不同**（fallback 是解锁档位 20/50/80 与集中度档位 30/60/90 取平均）。
- **PONS（11114）分量拆解**：`26.96 = drawdown 26.02(96.5%) + oi 0.94`；`unlock_score=0`（30d 解锁 0%）、`concentration_score=0`（**top10 = NULL，无 holder 快照 → 缺失当零**）、`liquidity_discount=13.61`（被 `max(0,·)` 全吃）、`cvd_score=0`（CVD +6.85%）、`unlock_value_score=0`。即该资产分数几乎全是**已实现回撤**的投影，不是前瞻抛压。
- **临界抖动是系统性的**：medium 下界 30 分 ↔ ATH 回撤 **-33.3%**，PONS 当时 -28.91% ⇒ 再跌约 3.4% 即跨档，且分数里只剩一个连续变量在动；同日两次计算已实测漂移 **28.24 → 26.96**。全表 332 行分布 `low 246 / medium 70 / high 16`，p50=15.83，**落在 `[30,40)` 的有 35 行（10.5%）**。
- **传导面**：写库（6h 缓存）→ notebook 展示 → LLM prompt「抛压风险等级必须严格使用 `pressure.risk_level`」→ **结论文本随抖**；另一路 `phase_catalyst_backfill.py` 把它当 `unlock_pressure` 喂进催化剂技术面评分。

**改动（2 文件改 + 1 新探针，不动分数口径）**：
- `market_rules.yaml` 新增 `unlock_pressure:` 段（`high_threshold: 60` / `medium_threshold: 30` / `hysteresis_band: 5`），值 = 外置前的硬编码值；段内注释写清滞回原因与实证数字。改 yaml 需重启生效（同本文件其它段）。
- `db_stats.py` 新增 `_PRESSURE_BAND_DEFAULTS` / `_load_unlock_pressure_rules()` / `_PRESSURE_RULES` / `_band_risk_level()`（照 `meme_risk._load_meme_risk` 的「yaml 缺失即回退内建默认」范式）。**回退值刻意设为 60/30 + band 0**，即 yaml 不可用时分档与改造前**逐分一致**，不因配置缺失而改变档位。
- `_compute_pressure_score` 新增 `previous_risk` 形参，尾部 `if/elif` 三分档换成 `_band_risk_level(score, previous_risk)`；展示侧 fallback 的 `70/40` 一并改为 `_band_risk_level(score)`（无上一档 → 裸阈值），**消除同字段双阈值**。
- `compute_unlock_pressure`：把「读缓存行」从 `if not force:` 里提出来**无条件执行**（未过期且非 force 仍早退），使该行的 `risk_level` 成为滞回基准 `previous_risk` —— 过期行也照用，因为它就是当前对外展示的档位，抑制抖动必须对齐它。
- 新探针 `test_pressure_band_20260927.py`（**53 断言 / 0 失败**，纯离线）：yaml 三键 + 回退默认值、裸阈值与改造前逐分一致、滞回**上/下/跨两档/带内维持**全部边界（34.99/35.0、25.01/25.0、64.99/65.0、55.01/55.0、直跳 64.99/65.0）、非法 score 与非法 previous 安全退化、源码级断言「函数体内不再硬编码 60/30」「fallback 块内无 70/40」「previous_risk 透传且取值早于算分」。

**滞回语义**：以库中上一档为基准，向上跨档需 `score >= 目标档下界 + band`，向下跨档需 `score <= 当前档下界 - band`，带内维持原档；无上一档/非法/`band<=0` 退化为裸阈值。

**验证**：`test_pressure_band_20260927.py` **53/0**；既有回归 `test_daily_brief_p1.py` **22/22**、`test_research_determinacy_20260926.py` **127/0**、`test_research_3tier_20260927.py` **82/0**、`test_thesis_forward_track_20260927.py` **63/0**（含「25.0/low、12.5/low」两条旧断言，口径不变）；`py_compile` 通过；`yaml.safe_load` 校验通过。**prod 只读模拟（不写库）**：332 行逐行 `_band_risk_level(score, 库中档位)` vs 库中原档 —— **即刻改档 0 行**（档位分布与改造前完全一致），**50 行（15.1%）落在滞回带 `[25,35)` 或 `[55,65)` 内**（这部分正是被保护起来的抖动区）。即本改动对现有数据零瞬时影响，只在后续重算时抑制振荡。

**未做 / 边界（须留档）**：① **未做口径重构**：`drawdown_score` 仍计入抛压分（PONS 案例里它占 96.5%），即「抛压分度量的是已实现回撤 + 前瞻解锁」的混合口径未改；剥离它需全表重算 + 前端/LLM 口径同步 + 阈值重校准，本轮未授权。② **`top10 = NULL → concentration_score = 0`（缺失当零）**与本条同批留档，**已于下条单独立项修复**（分数口径不动，仅标记缺失，见「A3 相邻缺陷：抛压分量缺失当零标记」）。③ **滞回只在写入路径生效**：读取侧 `biz.asset_unlock_pressure` 直接返库值，若库中档位由改造前写入且处于带内，不会因本次改动被动更新（这正是「即刻改档 0 行」的原因）。④ **`hysteresis_band` 的 5 分是经验值、未校准**：与三档确定性闸门同属 `uncalibrated`，无「该资产真实抛压事件」的前向样本可对齐；数值集中在 yaml，改一个数即可（无需改码）。⑤ 展示侧 fallback 传 `previous_risk=None`（拿不到上一档），故该紧急路径**无滞回**，代价是极端情况下档位可能与主路径差一档（已在代码注释标明）。⑥ **线上 runtime 复验须待 Zeabur 重建后执行**（`push ≠ 线上生效`），本单未做。

### 盘面告警审计 NEW-C：费率年化按品种实际结算间隔（2026-09-27，本次提交）

来源：`audit_9币盘面告警邮件_2026-09-27.md` §三 NEW-C（P2）。用户授权后落地。

- **现状 / 根因**：`scan_daemon.py` 卡片费率年化硬编码 `×3×365`（假设 8h 结算），图例亦写死「×3×365（8h 结算）」。实测 Binance 存在 **4h 结算**品种（LSK/ZRO/PAXG/ASTER/GRAM/LA/MOODENG）⇒ 年化被**低估一半**（应 ×6×365），该封 9 币中 **7 个受影响**。
- **改动（迁移 + 2 采集侧 + 1 展示侧 + 探针）**：
  - 迁移 `fix_073_funding_interval.sql`：`biz.asset_derivatives` 增列 `funding_interval_h NUMERIC(4,1)`（幂等 `ADD COLUMN IF NOT EXISTS`；**DDL 后已立即 commit**，防 `AccessExclusiveLock`）。基线 `sql/biz/derivatives_table.sql`、`phase_derivatives_batch.ensure_table`、`db_stats.get_asset_derivatives` 建表兜底同步补齐。
  - **生产者** `phase_derivatives_batch`：新增 `_derive_funding_interval_h(history)` —— 由结算历史（`get_funding_rate_history`）相邻 `funding_time` 差取**中位数**（抗缺失/重复/乱序），在同一份历史里顺带算出；`_aggregate_funding` 新增 `interval_h`：**取 OI 价值最大交易所**（= 费率主导来源）的间隔，无 OI 时回退任一可得者，全无 → None；写入 INSERT / ON CONFLICT。
  - **消费者** `scan_daemon`：新增 `_funding_annualize_mult(interval_h)` = `(24/间隔)×365`（None/≤0/非法 → 回退 8h，**不劣化**）；`_load_funding_interval_map(conn)` 与 `_load_funding_map` 同源、同 `FUNDING_STALE_H` 新鲜度口径（保证「费率」与「间隔」取自同一行快照）；`task_scan_alert` 把间隔写入渲染项 `it["funding_interval_h"]`，`_render_alert_email` 据此换算；图例改为「当期 ×(24/结算间隔)×365（Binance U 本位多为 8h、部分品种 4h；间隔缺失按 8h）」。
  - **`db_stats.get_asset_derivatives` 是并行的第二条采集/写库路径**（同写 `biz.asset_derivatives`），已按同口径补 `_derive_funding_interval_h` + 间隔选取 + 落库 —— 否则该路径写入的行间隔为空，会让年化在这些资产上被回退成 8h（修复失效）。
- **验证**：新探针 `test_funding_interval_20260927.py` **35/0**（间隔推导 8h/4h/1h/空/单条/噪声/倒序；跨交易所选取 OI 主导+退化+全无；年化倍率与回退；卡片渲染 4h 年化≈2×8h；源码护栏不再写死 8h；列存在于基线/迁移/两处建表）。回归全绿：`test_derivatives_signal_gap` 43/0、`test_scan_alert_remaining` 29/0、`test_scan_alert_header_regime` 135/0、`test_scan_alert_audit_deepdive` 75/0、`test_scan_alert_audit_20260926` 96/0、`test_scan_scenario_label` 48/0；`py_compile` 通过。迁移已 apply 到 prod 并核验列存在（`numeric(4,1)`）。
- **未做 / 边界（须留档）**：① **`funding_rate_7d_avg` 的 7 天窗口仍按 8h 假设**（`rates[:21]`）—— 与年化同根因，但本次范围仅年化；对 4h 品种该窗口实际只覆盖 ~3.5 天，后续可按 `round(7*24/间隔)` 取窗（消费方为 `research.html` / `db_stats` 展示，非告警邮件）。② **间隔取「OI 主导交易所」而非各所加权**：跨所聚合费率是 OI 加权均值，而间隔是离散量、无法加权，故以主导来源代表；若某币 Binance 为 4h、其它所为 8h，展示间隔取 Binance。③ 历史不足（相邻间隔为空）→ 列 NULL → 消费侧回退 8h（不猜测、不劣化）。④ **线上 runtime 复验（2026-09-27 已闭环）**：`derivatives_batch` 是 `30 */6 * * *`（北京 00:30/06:30/12:30/18:30）的 6h 调度任务，push 时刻（07:38Z）正落在两轮之间 ⇒ 审计测得的「0/792 全 NULL」实为「部署后该任务尚未触发」，**非「采集侧未部署」**（部署后 `scheduler_watchdog` 07:46Z、`highlight_alert` 08:05Z 均正常执行；生产者链路逐行核对 + 本地对真实交易所实测推导均正常）。手动向 `sys.task` 入队一轮同 cmd 任务（`/usr/local/bin/python -u /app/scripts/bin/phase_derivatives_batch.py --limit 200 --delay 0.2`，08:17→08:31Z，`success 200/200`）后：**覆盖率 0 → 191/794**（间隔分布 4.0h×153 / 8.0h×34 / 1.0h×3 / 2.0h×1；192 个有数据中仅 1 例「有费率但间隔 NULL」）。消费侧只读复核：`_load_funding_interval_map` 返回 **191** 项，`_funding_annualize_mult(4.0)=2190`（=6×365，恰为 8h 的 2 倍）、`(8.0)=1095`、`(None)=1095`。**留意**：审计的 7 目标币仅 4 个（LSK/GRAM/LA/MOODENG）本轮在范围内；ASTER/ZRO/PAXG 因未进入本轮 200 资产（`fetched_at` 仍为 04:30Z）而为 NULL —— 非推导失败（实时重算仍为 ASTER 4.0 / ZRO 4.0 / PAXG 8.0），将于其下次刷新（≤12h 后由 NEW-G 陈旧刷新组纳入）补齐。另：审计对个别币的「≈4h」估算不准（LSK 实为 1h、PAXG 实为 8h）。

### A3 相邻缺陷：抛压分量「缺失当零」标记（2026-09-27，本次提交）

来源：上条 A3 留档项 ②「`top10 = NULL → concentration_score = 0`（缺失当零）未修，与「空 URL 引用不封顶」同类防骗漏洞，建议单独立项」——本轮自立项处置。

**根因**：`_compute_pressure_score` 对 None 输入一律**按 0 计分**（concentration 取 `(top10 or 0)/100*25`，`_downside_score(None) → 0`），而 0 分正是该分量的**最低值** ⇒ 「未采集」被读成「无风险」：top10 快照缺失时集中度分量记 0，与「集中度真为 0（完全分散）」**不可区分**。
**实证**：PONS(11114) `top10=NULL → concentration_score=0`，当时总分 `26.96/low`；若集中度为 80%（=20 分）总分即 `46.96` → **跨 30 分档**，即缺失足以让档位被系统性低估（A3 那条 `26.96 = drawdown 26.02 + oi 0.94` 的拆解里已埋此因）。

**改动（1 文件改 + 探针扩节，分数口径不动）**：
- `db_stats.py` 新增 `_PRESSURE_COMPONENT_TOTAL = 6` 与纯函数 `_pressure_missing_inputs(...)`：把「按 0 计分」的分量（concentration / turnover / drawdown / cvd / oi）按 None 登记为缺失 key。
- `compute_unlock_pressure`：`detail` 与**顶层返回值**（含缓存早退分支）新增 `missing_inputs` / `components_available` / `components_total` / `is_partial` 四字段；**`_compute_pressure_score` 的返回与判档逐分不变**（未擅自抬分）。
- notebook 展示侧（`_build_structured_metrics_inner`）把四字段透出到 `pressure` 块；`generate_research_thesis` 的 `metrics_structured.pressure` 同步注入供 LLM 消费。
- **system_prompt 规则 5** 增子条：`is_partial=true` 时禁止把低分断言为「无抛压 / 无集中度风险」，必须写明「仅基于 components_available/components_total 个分量，缺失项无法排除风险」；仅 `is_partial=false` 才可写「当前未见明显抛压」——与既有规则 4（`unlock.data_available=false` 禁止断言「无解锁抛压」）**同构**。
- 探针 `test_pressure_band_20260927.py` 扩 `[8]` 节（**53 → 68 断言**）：缺失 key 判定、「值为 0 不被误标」、分数口径不变（None → `0.0/low`；80 → 20 分）、写路径 / 缓存分支 / 展示侧 / 结构化指标 / prompt 共五处接线。

**验证**：`test_pressure_band_20260927.py` **68/0**；既有回归 `test_research_determinacy_20260926.py` **127/0**、`test_research_3tier_20260927.py` **82/0**、`test_thesis_forward_track_20260927.py` **63/0**；`py_compile` 通过。

**未做 / 边界（须留档）**：① **未改分数口径**：缺失仍按 0 计分，本轮只做标记——让缺失参与判档（缺失时不判 low、或按最坏值填充）属口径变更，需产品拍板。② **前端未加徽标**：`index.html`（抛压风险行）、`research.html`（抛压评分）仍只显示分数/档位，未渲染「分量缺失 N/6」；本轮刻意不动模板（`index.html` 常有并行会话未提交改动，避串台），标记已在 API 与结论文本可用。③ **旧缓存行（6h TTL 内）视为齐备**：`detail_json` 无 `missing_inputs` 键 ⇒ `is_partial=false`，TTL 过期重算后自然补齐，不做回填。④ **`unlock_pct_30d = 0` 不标缺失**：解锁是否「真无事件」由既有 `unlock.data_available` 单独判定（规则 4），本标记不重复。⑤ **线上 runtime 复验须待 Zeabur 重建后执行**（`push ≠ 线上生效`），本单未做。

### 链上快照系统性滞后修复 OBI-OPT-SNAPSHOT-FRESHNESS P1-A~D（2026-09-27，本次提交 8881a80）

来源：`工单_OBI-OPT-SNAPSHOT-FRESHNESS_2026-09-27.md`（上游 OBI-OPT-BACKTEST-001 拆解项 X3）。用户授权「按你的判断处理」后落地。

- **根因三连**：链上快照（`biz.onchain_holder_snapshot`）分链**隔日**运行（`1,3,5`/`2,4,6`）+ 单轮 `--limit` 截断 + RPC 失败无兜底 ⇒ 每条链天然滞后 1–2 天、长尾（rank 靠后合约）永远轮不到、单次失败即整日空缺，实测滞后达 3 天。
- **改动（4 文件 + 1 探针）**：
  - **P1-A** `scheduler.py`：四条 `chain_holder_snapshot_*` cron 由隔日改**每日**，错峰 **14:00 / 14:20 / 14:40 / 15:00**（各差 20 分钟，比工单草案的 14:00/14:30/15:00 更细，避 RPC 并发挤兑）。
  - **P1-B** 新增 `scripts/bin/check_onchain_snapshot_freshness.py`（仿 `check_scan_freshness.py`）：按链 `SELECT chain, MAX(snapshot_date) ... GROUP BY chain`，滞后 **>2 天**发告警邮件；复用 `biz.scan_stall_alert` 表 + **独立去重键 `onchain_snapshot_stall`**（`ON CONFLICT (task) DO UPDATE`），6h 静默、恢复后清空时间戳并解除；**整表为空按滞后告警**（不静默通过）；**恰 2 天不告警**（`>2` 才告警）；退出码恒 0（停摆用邮件表达，非退出码）。scheduler 注册 `onchain_snapshot_freshness`（`20 * * * *`，**monitor** 类）。
  - **P1-C** `phase_chain_holder_batch.get_pending_assets` 改**「最久未采优先」限界旋转**：新增 `LEFT JOIN LATERAL (SELECT MAX(s.snapshot_date) AS last_dt FROM biz.onchain_holder_snapshot s WHERE s.asset_id=c.asset_id AND s.chain=c.chain) ls ON TRUE`，排序由「仅 `market_cap_rank ASC`」改为 `ls.last_dt ASC NULLS FIRST, a.market_cap_rank ASC NULLS LAST, c.asset_id ASC`。未采过的资产（`last_dt` NULL）恒排最前，已采资产按陈旧度跨日轮转 ⇒ 长尾数日内自然覆盖。
  - **P1-D** `phase_chain_holder_snapshot_auto.py` 头部加 **DEPRECATED** 段（说明自动化调度角色已废弃、保留原因、`--limit 0` 长任务占槽位风险）。
- **两处定调偏离（须留档）**：
  - ① **P1-C 未采用工单原方案「取消硬 `--limit` / 分页续跑遍历完」**。依据 `scheduler.py` 明文史实「chain_transfer_monitor_auto…不再占用 chain 并发槽位，避免每天 5 小时级任务饿死其他 chain 任务」——无界「跑到完」会重现 chain 槽位饿死事故。故保 `--limit`（1200/1200/800/300）+ 旋转排序。
  - ② **P1-D 未删文件**。工单判其「死代码」有误：`phase_chain_holder_snapshot_auto.py` 虽不被 scheduler/supervisord 调度，但仍是 **`app.py` 工作台可见手动触发任务 `chain_holder_snapshot_auto` 的入口**，直删会使该 UI 任务触发即失败。故仅标 DEPRECATED，并注明「若不再需要手动入口，须先删 `app.py` 条目再删本文件」。
- **验证**：新探针 `workbench/test_snapshot_freshness_20260927.py` **27/0**（A 组调度四条齐备/每日/错峰/`--limit` 保留；B 组看门狗注册/monitor；C 组判定边界：新鲜/3 天告警/恰 2 天不告警/表空告警/阈值与去重键/复用 upsert/恢复清空；D 组旋转排序/`LIMIT` 保留/北京时间排除；E 组 DEPRECATED/未删/app.py 未悬空/不被调度）。既有回归全绿：`test_scan_alert_header_regime` 135/0、`test_macro_market_board_tier2` 36/0、`test_scan_alert_remaining` 29/0、`test_derivatives_signal_gap` 43/0、`test_funding_interval_20260927` 35/0、`test_risk_signal_p0r2_20260927` 14/0、`test_sector_taxonomy_20260927` 34/0、`test_research_3tier_20260927` 82/0、`test_thesis_forward_track_20260927` 63/0；`py_compile` 5/5 OK；`python3 scheduler.py --list` 已确认四条每日 cron 与新看门狗注册生效。
- **三段式验收（2026-09-27 已执行，只读）**：
  - **② runtime ✅**：prod `sys.task` 出现新任务 `[调度] onchain_snapshot_freshness - 链上快照·外部看门狗…`，**2026-09-27 09:20:00 UTC 运行 `done`（4s，无 error）** ⇒ `8881a80` 已部署生效；同一时刻 `biz.scan_stall_alert(task='onchain_snapshot_stall')` 写入 `last_email_ts=09:20:00 UTC` ⇒ **告警邮件确已发出**（非静默）；本地对 prod 跑 `--dry-run` 复验判定 `arbitrum 1天[ok]/base 1天[ok]/bsc 1天[ok]/ethereum 0天[ok]/solana 4天[STALE]`，二次运行正确输出「距上次邮件仅 0.5h（<6h），去重跳过」⇒ 阈值与 6h 去重均按预期。
  - **③ DB SELECT ✅**：链上滞后 `ethereum 0天`、`bsc/arbitrum/base 1天`、**`solana 4天（09-23）`**；近 2 日覆盖 `bsc 1130 / arbitrum 724 / base 679 / ethereum 306 / solana 0`；行情快照近 5 日每日行数 `8154/16304/8155/24468/24456` ⇒ **每日均 ≥ 7000，`check_cmc_snapshot_gap.py --threshold 7000` 阈值合理、无需下调**（关联项闭环）。
- **新发现（超出 P1-A~D 范围，建议单独立项）**：① **solana 采集实质失效**：自 2026-08-30 起断更（10 天内仅 09-23 落 1 行，历史 2652 资产）。根因**非**调度/`--limit`。**已查证 Zeabur 侧 `HELIUS_API_KEY`（只读）并证伪「缺 key」假设**：`zeabur variable list --id 6a702918fefeb46a88349f8c` 显示 prod **已配置** key（len=36）且与本机可用 key **逐字符相同**；prod 任务日志 299 行中「未配置 HELIUS_API_KEY」出现 **0 次**。**真因 = mint 参数/数据 + 静默吞错**：本地同 key 同码直测 `SolanaClient.get_token_holders` —— `USDT`/`XRP` 返回 20 条 ✅，而 `USDC(EPjFWdd5…超大 supply)` 报 `-32600 Too many accounts requested (10000000 pubkeys)`、`SOL(So111…原生 mint)` 报 `-32602 not a Token mint` ⇒ **返回空且不抛异常**（`_json_rpc` 失败仅 print 后 `return None` ⇒ `accounts=[]`）→ `_scrape_holders_helius` 静默 `return None` → 回退 Solscan（已被 CF 封）→ 整体失败，prod 逐条复现。**处置（a/b/c 三项已于本日「推进 solana 采集修复」中全部落地，见下节）**：a) 排除 solana 原生 mint；b) `_scrape_holders_helius` 失败必须打印真因（区分 401/429/参数/网络），当前静默是本次误判的直接原因；c) 超大 supply mint 无 fallback、Solscan 源已废 ⇒ 需换源或降级。② **周期性「stuck: 240分钟无新日志」被杀**（即工单根因 R3 的真实表现，P1-A~D 未覆盖）：`bsc` 09-22/09-24、`eth` 09-23/09-25、`solana` 09-25 均被杀 ⇒ 即使 P1-A 改每日，**单轮失败仍整日空缺**；现由新看门狗兜底告警（滞后>2 天发信），但「失败即重试/断点续跑」机制仍缺，建议单独立项。③ 现网调度文案暂仍为旧描述（`周二四六 / 周一三五`）系今日 14:00 那轮在重建前已触发所致，非未部署；新每日 cron 首次生效在次日 14:00。
- **未做 / 边界（须留档）**：① **连续 24h 观察未做**（须明日起，`chain_holder_snapshot_*` 新每日 cron 首次触发在 14:00 北京）。② 旋转覆盖长尾需**数日收敛**（非即时全量），验收看趋势不看单日。

### Solana 链上持仓采集修复（2026-09-27，承接上节「新发现①」）

来源：上节 OBI-OPT-SNAPSHOT-FRESHNESS「新发现①」solana 断更，用户授权「推进 solana 采集修复」后落地。

**根因（三段式核验后定调，非「缺 key」）**：
- **已证伪**：prod Zeabur 的 `HELIUS_API_KEY` 已正确配置（`zeabur variable list --id 6a702918fefeb46a88349f8c`，len=36，与本机逐字符相同）；prod 任务日志 299 行中「未配置 HELIUS_API_KEY」出现 0 次。
- **真因三连**：① **mint 参数/数据**——`SOL(So1111…1111)` 是 solana **原生 mint**（非 SPL token），Helius 恒报 `-32602 not a Token mint`；`USDC(EPjFWdd5…)`/`USDT(Es9vMFrz…)` 持币账户数达千万级，`getTokenLargestAccounts` 恒报 `-32600 Too many accounts requested`（Helius 免费档护栏，公共 RPC 该 method 亦被硬限流 429，**无免费替代源**）。② **静默吞错**——`_scrape_holders_helius` 在 `top_holders_json` 为空时直接 `return None`，真因只散落在 `_json_rpc` 的 print 里且随 `run_single` 的临时文件被删 ⇒ 是本次误判为「缺 key」的直接原因。③ **死回退**——其后回退的 Solscan(Playwright) 已被 Cloudflare 全面拦截，prod 日志逐币皆为「Solscan 未获取到数据」，却**每币硬耗 2~3 分钟**。单轮 300 币即 8~15h，**正是链上任务被判「stuck: 90/240 分钟无新日志」或「12h 超时」而被杀的根因**（09-23 那轮 `dur=30723s`、success=1/fail=299 即此模式）。

**改动（4 文件；另见本节日末「续」的 solana 地址结构护栏）**：
- `solana_client.py`：新增 `classify_rpc_error()`（`-32602→invalid_mint`、`-32600+too many accounts→too_many_accounts`、其余 `rpc_error`）；`SolanaClient` 新增 `self.last_error`（`{"kind","method","code","detail"}`），`_json_rpc` 在 RPC error / 429 耗尽 / 网络异常时落盘、成功时清空（原 429 静默无痕）。
- `phase_chain_holder_scrape.py`：`_scrape_holders_helius` 改返回 `(结果, 失败类别)`，空结果时**显式打印真因**；新增 `PERMANENT_MINT_ERRORS = {invalid_mint, too_many_accounts}`；**摘除 Solscan(Playwright) 回退**，solana 失败即 `return None`（快速失败，当日由看门狗告警，远优于悬挂数小时）。`_scrape_holders_solscan` 实现**保留但注明「当前未被调用」**（待接入 Solscan Pro API 或可用源后复用）。
- `phase_chain_holder_batch.py`：`EXCLUDE_CONTRACTS` 增补 solana 原生 mint `So11111111111111111111111111111111111111111`（否则每天白占一个待采集名额且永远落 fail 统计）；solana 单币超时由 300s（为 Playwright 留的）**下调至 120s**。
- `workbench/test_solana_holder_20260927.py`（新）：A~F 六组判据，**27/0**（F 组为下文地址结构护栏）。

**验证**：`py_compile` 4/4；新探针 27/0；既有回归 `test_snapshot_freshness_20260927` 27/0、`test_derivatives_signal_gap` / `test_scan_alert_remaining` / `test_funding_interval_20260927` / `test_macro_market_p0` / `test_macro_market_board_tier2` / `test_sector_taxonomy_20260927` 均通过。**runtime（本地连 prod，只读）**：`--asset-id 1814 --chain solana` 秒级返回并打印 `Helius 未取到持仓（invalid_mint）: Invalid param: not a Token mint` + 「判定永久不可采，跳过」（**不再等 2~3 分钟 Playwright**）；`--asset-id 1127` 仍走 `数据来源: Helius RPC (solana)`、解析 20 条持仓 ⇒ 正常路径无回退。

**未做 / 边界**：① **USDC/USDT 等超大持币 mint 仍无数据**——Helius 护栏 + 公共 RPC 硬限流，免费档确无替代源，属**已知且显式**的失败（fast-fail 并计入 fail），如需覆盖须接入付费源（Birdeye/Solscan Pro）。② solana「失败即重试/断点续跑」仍缺（属「新发现②周期性 stuck」范畴，未在本单）：本次仅保证**快速失败 + 不悬挂**，单轮失败当日不再自动补跑，由次日 cron + 看门狗兜底。

**续：Helius 额度证伪 + 合约地址污染（用户问「额度是不是不够」后追加，同日）**
- **额度不是瓶颈（有据）**：09-27 那轮 275 成功 / 25 失败，25 条失败**逐条**归类为 —— `-32602 invalid_mint / WrongSize` 19 条、`-32600 too many_accounts` 2 条（USDC）、RPC 正常但结果空 4 条（PURR/MA/SPY/TSLA，本地复现确认）；**429/限流/额度 = 0 条**。另用现有 key 突发 60 次请求全 200。⇒ **不建议再申请 key**（多 key 救不回这 25 条中的任何一条；仅当**新代码**打出 `rate_limited` 时才有必要，且需配套多 key 轮换改造，当前只认单个 `HELIUS_API_KEY`）。
- **真根因 = `core.asset_contract` 的 solana 地址被系统性污染**（23 个从未成功的 solana 合约中）：① **15 个被降格为全小写** —— base58 **大小写敏感**，降格后或解码字节数不对（`-32602 WrongSize`，如 FOXSY/AIXCB/SAFA/BELG）、或指向不存在的 mint（`not a Token mint`，如 GEOD）；其中 11 个还含 base58 禁用字符 `l`（如 PYM/MOEW/LUNA/DEUS/SFA）；② 1 个混入 **Solscan URL 片段**（`token/EdAhkb5…`，L=50）；③ 1 个把 **EVM 地址**贴到 solana 链（`0xadb2437e…`）；④ 1 个是原生 mint（已剔除）；⑤ 4 个 vanity 前缀地址（SPY/PURR/MA/TSLA，Helius 判 not-a-mint/空）。这批行 `last_dt` 恒 NULL ⇒ 永远排待采最前、永远失败、每日白占名额。
- **本次落地（结构性护栏，零误伤）**：`phase_chain_holder_batch.py` 新增 `SOLANA_ADDR_RE = ^[1-9A-HJ-NP-Za-km-z]{32,44}$` 与 `_sol_guard()`，`get_pending_assets` / `get_total_pending` 对 solana 追加该谓词，拦下 ①中 11 个（含 `l`/0x/URL 片段）等**结构上就非法**的行（实测 prod：solana 队列违规=0，eth/bsc 不受影响）。**未**采用「全小写即污染」的启发式 —— 实测有 1 例全小写地址（TAO `taoc6xyv…`）曾成功，启发式有误伤风险。
- **覆盖度实况（纠偏）**：solana 合约 3166 条中 **3143 条已有成功快照（99.3%）**，「断更」实为**时新性缺口**而非覆盖缺口；仅 23 条从未成功（即上述污染 + 边缘个例）。
- **未做（建议单独立项「数据侧」）**：**地址本体的修复**——须定位并修掉把 solana 地址降格/贴 URL 的写入方（疑似 contract 回填/CMC·DexScreener 侧），再从可信源重同步以**恢复**那 15 个资产（本次仅做队列侧结构过滤，**未改 `core.asset_contract` 任何数据**）；以及 5 个「全小写但形态合法」者（GEOD/FOXSY/AIXCB/SAFA/BELG）仍会每日失败一次（形态上无法与真地址区分，只能靠数据修复）。

**续（同日）：写入方定位 + 护栏落地（用户「先帮我定位并修复那 15 个被污染的数据写入方」）**

- **定位（三段式证据）**：① 16 行污染**全部** `created_at = 2026-07-31`、`source_code ∈ {cmc ×15, dl ×1}`；`cg` 侧 1445 行**零污染**、`cmc_backfill` 35 行零污染；**2026-07-31 之后无任何新增小写行** ⇒ 系建表当日一次性导入产物，非在跑的写入方持续产生。② **写入方早已修复**：`77bf42e`「合约导入保留 Solana 地址原始大小写」修的就是 `POPULATE_FROM_CMC`（现为 `CASE WHEN platform IN ('solana','solana (spl)') THEN token_address ELSE LOWER(...)`）；直接证据 —— BOOP 资产 09-16 又写入**正确大小写**行 `2HrZ5R18H48b8ptL3n8N5zX65zDB4o2cMCHcP3QfSfye`，与 07-31 的小写旧行 `2hrz5r18…` **并存**。③ **机制根因**：建表 DDL 用 `UNIQUE (chain, contract_address)`（**大小写敏感**），与 07-29 初始化 SQL 的原设计 `UNIQUE (chain_id, LOWER(contract_address))` **背离** ⇒ 正确大小写值与旧小写值**不冲突、不覆盖**，正确值只能「并存新增」，旧行**永久存活**（BOOP/TAO 实测并存即此）。
- **本轮落地（护栏 3 处，纯代码，零数据变更）**：① `phase_chain_contract_backfill.py` 新增 `SOLANA_ADDR_RE = ^[1-9A-HJ-NP-Za-km-z]{32,44}$`、`SOLANA_NATIVE_MINTS`（Wrapped SOL / System Program）与 `is_valid_solana_addr()`，`parse_contracts` 对 solana 剔除并打印（该脚本**无调度调用**，属手动入口；实证它曾写入原生 mint `So1111…1111`）。② `phase_a_build_core.POPULATE_FROM_CMC`（**每日 03:00 流水线 ④，正是这 15 行的源头**）WHERE 增 solana 结构条件。③ `POPULATE_FROM_DL` 同款（DefiLlama 的 `address` 可能是 EVM hex 或 URL 片段，实证 UNCX `0xadb2437e…` / FAB `token/EdAhkb5…`）。三处均**只对 solana 生效**，非 solana 链行为不变。
- **验证**：`EXPLAIN (COSTS OFF)` 只读验 SQL —— `POPULATE_FROM_CMC` OK（16 行计划）；新探针 `workbench/test_contract_addr_guard_20260927.py` **28/0**（A 结构校验 10 例含「合法全小写不误伤」/ B `parse_contracts` 端到端剔脏 / C·D 两段 SQL 护栏 + 保留链感知赋值 / E 相邻写入方未被波及）；回归 `test_solana_holder_20260927` 27/0、`test_snapshot_freshness_20260927` 27/0、`test_scan_alert_onchain_addr` 退出 0；`py_compile` 2/2 OK。
- **新发现（既有缺陷，须留档）**：`POPULATE_FROM_DL` 存在**列歧义** —— `SELECT asset_id`（首列）在 `dl` CTE 与 `core.asset a` 之间歧义，**HEAD 版本同样报 `AmbiguousColumn`**（非本轮引入）。叠加 `run_cmc_pipeline.py` 只跑 `--step populate_cmc`、**无任何调度/流水线调用 `populate_dl`** ⇒ 该 SQL 长期是**死代码路径**（生产 `source_code='dl'` 的行来自更早版本）。修复只需 1 词（`dl.asset_id`），已 EXPLAIN 验证修复版 OK（17 行计划）；但修复后候选 **2965 行（solana 167）**、属**行为变更**（会让一条长期失败/未执行的回填突然写库），故**本轮未修**，留待显式授权。
- **未动唯一约束（须留档）**：改「大小写不敏感」需先清掉并存的等价变体行，并同步 4~5 处 `ON CONFLICT (chain, contract_address)` 写法（否则无法推断唯一索引、写入方全部报错），改动面大 ⇒ **本轮明确不做**。5 个「全小写但形态合法」者（GEOD/FOXSY/AIXCB/SAFA/BELG）**结构上无法与真地址区分**，护栏拦不住，只能靠数据修复（下条已修）。

**续（同日）：存量数据修复（用户「按你的判断处理」⇒ 授权做数据修复、不做唯一约束）**

- **范围**：完整污染集合实为 **18 行**（不是 15）—— `cmc` 15 行全小写 + `cmc_backfill` 1 行原生 mint + `dl` 2 行错配。`cg` 1445 行与其余 `cmc_backfill` 干净。
- **判定口径**：以 CMC `/v2/cryptocurrency/info` 为真值源逐行对照 ⇒ 真值存在且不同 → `UPDATE`（大小写还原）；真值已存在于同 asset 合法行 → `DELETE`（重复）；真值即自身 → `DELETE`（源即脏，如原生 mint）；无真值 → `DELETE`（EVM/URL 错配）。**全局唯一约束预检**：15 个 UPDATE 目标值与**其它任何行**（不限同 asset）零冲突；外键 0 个、依赖视图仅 `asset_contract_map`（纯投影）。
- **执行结果：15 UPDATE + 3 DELETE**（一个事务内，逐行打印 `rowcount` 并断言 ==1，失败即整体回滚）。
  - `UPDATE`（大小写还原）：cid 14 TAO / 120 GEOD / 1057 LUNA / 1092 DEUS / 1347 FOXSY / 1380 PUPS / 1442 MOEW / 2329 PYM / 2659 AIXCB / 3575 BELG / 3576 SFA / 3618 SAFA / 4555 CHOMP / 5748 NINJA —— 真值均取自 CMC，且**不是简单 `UPPER()`**（base58 大小写是内容）。
  - `DELETE`：cid 2072 BOOP（真值 `2HrZ5R18…` 已并存于同 asset）、cid 9376 UNCX（`0xadb2437e…` 是**以太坊地址被贴上 solana 链**，实为另一条 ethereum 行的复制；CMC 无 UNCX solana 记录）、cid 157084 SOL（原生 mint `So1111…1111` 被当 SPL 合约；删后 SOL 与 BTC/ZEC/XMR 一致为 0 合约行，更诚实）。
- **⚠️ 一处判定修正（原计划误判为 DELETE）**：cid 9655 FAB 原值 `token/EdAhkbj5nF9sRM7XN7ewuW8C9XEUMs8P7cnoQ57SYE96`。回溯上游 `src_dl.protocol_list.address = 'solana:token/EdAhkbj5nF9sRM7XN7ewuW8C9XEUMs8P7cnoQ57SYE96'`（**DefiLlama API 原样返回该格式**，非本项目解析 bug —— `api.llama.fi/protocol/fabric` 实测同值）⇒ 剥离 `token/` 前缀得 44 字符 base58，经 **Solana RPC `getAccountInfo` 证实为真实 SPL mint**（owner=`TokenkegQfe…`、type=mint、decimals=9、mintAuthority=`fabdwk1nQ1mPFD7cVNTbxzZf32NG2pLbZ4fRPzPpiE9`，前缀恰为 `fab`）⇒ 改为 `UPDATE`，保住 FAB 唯一映射。
- **派生表同步**：`biz.coin_basic.primary_contract_address`（内容取自 `core.asset_contract`，`phase_a_coin_basic.py` 口径：`ORDER BY is_primary DESC, contract_id LIMIT 1`）有 **17 行**同值残留 ⇒ 在同一事务内按**同一口径**对 18 个受影响 asset 重算（SOL 归 NULL、UNCX 归 ethereum 行、BOOP 归 `boopkpWqe…`）。
- **明确不改（已核）**：① `src_dl.protocol_list.address` = 上游 API 原样落库，重拉即覆盖；② `biz.onchain_holder_snapshot` 13 行 / `onchain_transfer_log` 1 行命中的是 `0xadb2…` 但 **`chain='ethereum'/'eth'`**，属 UNCX 在以太坊的**合法**历史数据（与 solana 脏行共享同一字符串而已），**不动**。
- **修复后复验（prod 只读）**：solana 链非法形态行 **13 → 0**；3 个待删 `contract_id` 残留 0；15 个 UPDATE 逐行比对真值全 OK；RPC 抽验 3 个还原地址（TAO/PYM/FAB）**全部 `type=mint` / `decimals=9`** ⇒ 大小写还原是**真实账户**，非仅字面整洁。
- **污染的功能性危害（RPC 对照，决定性证据）**：`getTokenSupply` 对**小写旧值** `taoc6xyv2v8tdlcev4uagugv4vdqswjrgft2kcbrrby` / `7yf97k6jrbkb7bxjyxzmwqlqyxvirltcssgb75qlqan8` 一律 `Invalid param: Invalid`，对**还原后** `taoC6xyv2v8tDLcev4uaGUgV4vdQsWJrGft2kcBRrBY` / `7yF97k6jrBkb7BXJYXzmwQLQyxVirLtCSSGb75qLqAN8` 返回真实供应量 ⇒ 小写行**根本取不到链上数据**（本地 sweep 亦见 `TAO 小写 holders=0 supply=None` 而 `9cYXqd… holders=20`）。已删的原生 mint `So1111…1111` 则报 `Invalid param: not a Token mint`（它是 SPL 程序的**原生 mint**，非普通代币 mint —— 顺带说明为何它不该作为 `is_primary` 合约行留存）。
- **写入方根因收口（用户追问「根因处理完了吗」后补做）**：对 `core.asset_contract` **全部 5 个写入方**逐一清点，确认已无任何路径会把 solana 地址降格：
  | 写入方 | 触发 | solana 大小写 | 结构护栏 |
  |---|---|---|---|
  | `POPULATE_FROM_CMC` | 每日 03:00 流水线 ④ | 保留原样（`CASE`） | ✅ 本轮加 |
  | `populate_contracts_from_dexscreener` | 每日 03:00 流水线 ⑤ | **不适用** —— `CHAIN_MAP` 只含 EVM 链，`if chain_id not in CHAIN_MAP: continue` 先过滤，**结构上不可能写 solana 行** | — |
  | `POPULATE_FROM_DL` | 仅手动 `--step populate_dl`（**无调度**） | 保留原样（`CASE`） | ✅ 本轮加 |
  | `step3b_populate_cg` | 手动 | 保留原样（`CASE_SENSITIVE_CHAINS`） | 无（`DO NOTHING`，CG platforms 形态可靠，历史零污染） |
  | `phase_chain_contract_backfill` | 手动（无调度） | 保留原样（CMC 原值） | ✅ 本轮加（Python） |
- **`POPULATE_FROM_DL` 两处既有缺陷已修**（原「未授权留档」项，本轮一并处理）：① **列歧义** —— 外层 `SELECT asset_id` 在 `dl` CTE 与 `core.asset a` 之间歧义 ⇒ 改 `dl.asset_id`（此前该 SQL 在任何情况下都报 `AmbiguousColumn`，是死代码）；② **`token/` 前缀未剥离** —— DefiLlama 自带的 `'solana:token/<addr>'` 格式会让 FAB 这类**合法**地址被新护栏整体拦下 ⇒ 在 CTE 落库前加 `regexp_replace(..., '^token/', '')`。修后 `EXPLAIN` 通过（17 行计划）、候选 **2966 行 / solana 168**（较修复前的 2965/167 恰 +1 = FAB 归一化后通过）、候选中 `token/` 残留 0、solana 候选形态违规 0。⚠️ 修好使该路径**变为可运行**，但其唯一入口仍只有手动 `--step populate_dl|all`，**无任何调度器/流水线调用**（已 grep 确认），故日常行为不变。
- **唯一剩余的结构性根因（已评估、有意接受）**：唯一索引仍是 `UNIQUE (chain, contract_address)`（**大小写敏感**），意味着**理论上**若未来再出现任一把 solana 地址降格的写入路径，脏小写行仍会与正确值**并存**而非冲突覆盖。之所以不改：改「大小写不敏感」须先清并存的等价变体行，并同步 4 处 `ON CONFLICT (chain, contract_address)` 写法（否则无法推断唯一索引、写入方全部报错），且对 `DO UPDATE` 路径存在「用脏值覆盖好值」的反向风险 —— 属**有意接受的风险**，而非遗漏。当前防线是「写入侧全量 case-safe + 结构护栏」。
- **验证（本轮）**：探针 `test_contract_addr_guard_20260927.py` 扩至 **37/0**（新增 D 组 4 例：列歧义已消 / `token/` 剥离 / 原样被拦 + 剥离后通过；新增 F 组 5 例：DexScreener 不含 solana、CG 保留原样且 `DO NOTHING` 且跳过原生币）；回归 `test_solana_holder_20260927` 27/0、`test_snapshot_freshness_20260927` 27/0、`test_scan_alert_onchain_addr` exit 0；`py_compile` 2/2；`POPULATE_FROM_CMC`（16 行）与 `POPULATE_FROM_DL`（17 行）`EXPLAIN` 均通过。
- **回滚点**：`/Users/tinley/Workbuddy/crypto-profile-collection/回滚点_solana合约地址修复_2026-09-27.json`（含 18 行原始快照 `rows`、逐行判定 `plan`、`deleted_rows`/`updated_rows`、`coin_basic_before` 备份）。

**续（同日）：脏值兜底邮件告警（用户「如果不能实在闭合，能发邮件提醒一下脏值问题吗」⇒ 加可观测，而非改唯一约束）**

既然唯一约束的大小写敏感**有意不改**（见上条「唯一剩余的结构性根因」），用户要求在无法结构闭合的前提下**补一条邮件兜底**。做法照项目同型先例 `check_onchain_snapshot_freshness.py`（独立看门狗 + 每小时 + 6h 去重 + 恢复解除邮件 + category `monitor`），**不**硬塞进语义不符的盘面扫描看门狗邮件。

- **新增 `scripts/bin/check_contract_addr_hygiene.py`**（`ALERT_TASK_KEY='contract_addr_dirty'`，`REALERT_INTERVAL_H=6`，`--dry-run`，始终 `return 0`）——独立于**任何**写入方，每小时只看最终事实，三类信号任一 > 0 即告警：
  - **A 硬信号**：`chain='solana'` 且 `contract_address !~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$'` 或 `= ANY(原生 mint 白名单)`（Wrapped SOL / System Program）⇒ 明确不可用的脏值（本轮修复的那批）。
  - **B 软信号**：同一 `asset_id` 内存在「仅大小写不同」的 solana 并存变体对（`b.contract_address = lower(a.contract_address)` 且 `a` 非全小写）⇒ **专为「唯一约束大小写敏感」这一残留风险设的抓手**，正是 BOOP 那种并存形态。
  - **C 派生不一致**：`biz.coin_basic.primary_contract_address` 与该 asset 在 `core.asset_contract` 的主合约口径（`ORDER BY is_primary DESC, contract_id LIMIT 1`）不一致 ⇒ 脏值已污染到早报/看板消费的派生表。
  - 去重复用 `biz.scan_stall_alert`（与 `scan_stall` / `onchain_snapshot_stall` 同表不同键，互不抑制）；持续期间每 6h 重发，全部归零后发「✅ 已恢复」并把 `last_email_ts` 置 NULL。收件人 `settings.admin_email or settings.smtp_to`，发件名「合约地址卫生看门狗」。
- **调度注册**：`workbench/scheduler.py` 的 `SCHEDULE` 新增 `("contract_addr_hygiene", "20 * * * *", "check_contract_addr_hygiene.py", [], "…", "monitor")`（每小时第 20 分，与另两个看门狗同点）。
- **验证**：新脚本 `--dry-run` 实测 **A=0 / B=0 / C=0**（正与修复后状态一致，不触发越线分支）；探针 `test_contract_addr_guard_20260927.py` 新增 **G 组 15 例**（形态正则与原生 mint 口径同源、三类信号 SQL 语义、去重键独立且复用同表、退出码约定、渲染三分支、调度注册）⇒ **52/0**；回归 `test_solana_holder_20260927` 27/0、`test_snapshot_freshness_20260927` 27/0；`py_compile` 2/2。
- **定位说明**：这是**兜底可观测**，不替代结构护栏——结构护栏在写入侧（5 个写入方已 case-safe），本邮件只在「栏杆万一漏了」时让脏值**当天可见**，而不是像本次那样静默残留近两个月。

### 早报 / 催化剂邮件「停发」兜底与可观测（2026-09-27，本次提交）

来源：`诊断_早报与催化剂邮件停发_2026-09-27.md`（诊断假说「两套同时停 = 共同上游 DB 连接池毒化」，未连 prod）。用户授权「修复没发的问题」后，先做 **prod 只读取证**（`sys.task`/`task_log`/`catalyst_notification_log`/`catalyst_signal`/`pg_stat_activity`），结论**推翻原假说**：

- **早报**：`daily_brief_email` 09-27 01:00Z 任务 `done`，`task_log` 明写 `[OK] 早报邮件已发送: 已发送`；**仅 09-26 缺跑**（09-25 23:00Z ~ 09-26 02:23Z 无任何 `[调度]` 提交），即 09-26 白天 scheduler 整日静默（与 `f11f1a3` 09-26 18:40「autorestart 改 true」所修事故同窗）。
- **催化剂**：`catalyst_notification_log` 显示 `major_event` 09-24 ONDO / 09-25 SUI / **09-26 SKY（10:50Z）** 均 `sent`；**09-27 无候选属正常** —— 近 24h 完整 gate（`tier A/B + open + kind∈{structural,event} + prelaunch_ret_24h≥5 + prelaunch_penalty=0 + ai_event_type≠market_update + 发布 <24h`）**候选 = 0**（48h 仅 2 条，均已 >24h 被正确排除）。daemon 存活正常（`catalyst_grade` 逐小时写入至 08:28Z，`catalyst_signal.updated_at` 距查询 7 分钟）。
- **连接池**：`pg_stat_activity` 无 `idle in transaction` 长事务（仅 14 条 idle，最长 7h54m，非毒化）。
- **结论**：非「一个共同上游」，而是两件已自愈/正常的事；真正的结构性缺口是**缺少兜底与可观测**。

**改动（2 文件）**：
- `workbench/scheduler_watchdog.py`：`KEY_JOBS` 增 `("daily_brief_email", "每日大盘早报邮件（09:00）", 30)`。此前白名单**不含早报**，scheduler 静默失活或发送失败时既无告警也无补跑——即 09-26 丢整天的直接原因（「最后一道防线」对早报缺失）。阈值 30h > 24h 周期，与其余日频任务一致；补跑走既有 `submit_scheduled_task`（`SCHEDULE` 内 `daily_brief_email` 存在，已核）。
- `scripts/bin/catalyst_fast_daemon.py`：常驻循环新增**连续异常计数**（`consecutive_failures`）与 `FAIL_ALERT_ROUNDS`（默认 4 轮 ≈ 1h，`CATALYST_FAIL_ALERT_ROUNDS` 可覆盖）。此前每轮异常被 `try` 吞掉、**无任何对外信号**（daemon 照常心跳、邮件恒 0 无人知）。现连续 N 轮异常即发一封告警邮件（复用 `catalyst.notifier._send_email`，**不依赖 DB**，避免与故障同源），同一段连续故障内只告警一次，恢复后计数归零可再告警。

**验证**：`py_compile` 2/2 OK；自检断言通过（`daily_brief_email` 在 `KEY_JOBS` 且 `SCHEDULE` 可命中；`FAIL_ALERT_ROUNDS=4`；`_alert_consecutive_failures` 在 SMTP 未配时**不抛**仅打印失败；模拟 6 轮全失败 → 告警恰在第 4 轮触发）。既有回归全绿：`test_daily_brief_20260924` 29/0、`test_daily_brief_p1` 22/0、`test_catalyst_channel_dedup` 22/0、`test_major_event_alert` 55/0。

**未做 / 边界（须留档）**：① **未把早报「补发」做成一发即校验的强幂等**：看护是「按 task 状态超阈值未 done」触发的通用兜底，若某日 09:00 发送成功但 task 行写入失败，理论上可能补发第二封（概率极低，未加日级去重锁）。② **未给 `catalyst_fast_daemon` 加进程级心跳**：本轮只做「连续异常」自告警；**进程被 kill 不告警**（依赖 supervisord `autorestart=true` + 容器 FATAL 可见），如需「daemon 死了也报警」须另加 `sys.task` 心跳并纳入看护。③ **未收紧 `send_daily_brief.py` 的 SMTP 未配静默分支**（`notifier.configured == False → return 0`，任务显示 `done` 但未发信）：本次 prod 实测该分支未触发（发送成功），故未改；但它是「没发却显示成功」的同类陷阱，建议后续单独立项。④ **未加 `pool_pre_ping`**：`pg_stat_activity` 无中毒证据、且连接池毒化根因已由 `c6b009e` 处置，故不动连接池（避免无谓行为变更）。⑤ **未回填 09-26 缺失的早报**（补发只对「当前超阈值」生效，不回放历史）。⑥ **runtime 复验须待 Zeabur 约 6 分钟重建**（`push ≠ 线上生效`）。

### 催化剂 A 级 Alert 通道静默可观测（2026-09-28，本次提交）

来源：用户报「**最近几天都没收到催化剂邮件**」。经 prod 只读取证（`catalyst_notification_log` / `catalyst_signal` / `asset_catalyst`），结论：**管道与 SMTP 均存活**，停的只是「A 级 Alert」这一侧的候选供给。

**诊断物证**：
- `fast_alert` 最后发送 **09-23 16:50 UTC**、`slow_digest` 最后 **09-23 16:30 UTC**；同期 `major_event` 09-24 ONDO / 09-25 SUI / 09-26 SKY / 09-27 NEAR 仍**每天发** ⇒ 摄入、分级、发信链路正常，问题只在 `tier='A' AND status='open'` 候选为 0。
- 自 09-24 起无任何新建 `tier='A' AND status='open'` 信号（最后 3 条在 09-23）。
- 上游 `asset_catalyst` 日入库量 **9/21~9/24 约 1300~1600 条 → 9/25~9/28 约 300~475 条**（≈3~4 倍塌陷）；期间少数 `composite_score>=80` 的样本**全部 `resonance_state='confirmed'`**，按设计降级为 `status='watch'`，被 `status='open'` 排除。
- 代码根因：`notifier.py` 旧 docstring 承诺「无 A 级信号发空窗 note」，实现实为**静默跳过**——git 追溯确认 `058c246`（2026-09-16）显式删除了空窗邮件、只留 docstring 未同步。

**改动（3 文件，均仅本人改动）**：
- `workbench/catalyst/notifier.py`：订正过时 docstring；新增**运维侧告警通道** `send_channel_silence_alert()`（`NTYPE_CHANNEL_SILENCE='channel_silence'`、哨兵 `SENTINEL_CHANNEL_SILENCE_SIGNAL_ID=-3`、`CHANNEL_SILENCE_DAYS=3` 可被 env `CATALYST_SILENCE_DAYS` 覆盖）。**维持 `058c246` 决议**：空窗期**不**向用户发「今日无信号」邮件；只当 `MAX(created_at) FROM catalyst_signal WHERE tier='A' AND status='open'` 距今 ≥ 阈值时，向运维发一封告警。判据无状态、不落表、无 DDL（直接读地面事实，历史可复算）；发送频率由既有 `_try_acquire_send_lock` 的 24h 去重约束（空转期间至多每天一封）。
- `scripts/bin/phase_catalyst_pipeline.py`：慢通道 `send_major_event_alerts` 之后挂载 `send_channel_silence_alert()`（`--no-alert` 时跳过）。
- `workbench/test_channel_silence_alert.py`：新增离线护栏测试 **29/0**（通道独立、负号哨兵、快照 SQL 字段、阈值判定、从未产生候选、24h 去重、渲染护栏、失败不阻断）。

**验证**：`py_compile` 2/2 OK；`test_channel_silence_alert.py` 29/0 全绿。

**未做 / 边界（须留档）**：① **上游掉量根因（「抓得少 vs 翻旧帖」）未查实**：本轮只定位到 `asset_catalyst` 日入库量 3~4 倍塌陷 + 高分样本全被判「已定价」两重因素，但**采集端为何掉量**（源站节奏下降 / 翻旧帖稀释 / 采集器限流）尚未取证，属独立工单。② **口径只用单一全资产 MAX(created_at)，不拆 crypto/stock**：两封 digest 候选同源，且 `slow_digest_stock` 实测从未有过 A 级候选，故不拆；若日后美股通道独立出量需拆双哨兵。③ **runtime 复验须待 Zeabur redeploy**（`push ≠ 线上生效`）。

### 催化剂周报（2026-09-29，本次提交）

来源：用户「**我想每周收到催化剂的周报**」，随后**纠正需求**为「**周报总结——总结这周发生了哪些重要的事情，分别有什么影响**」（初版实现的「统计仪表盘 + A 级交易信号清单」方向不对，已重构为**叙事型周报**）。

**需求三确认**：① 时间 = 周一 09:00（北京）；② 收件人 = 复用现有催化剂邮箱（SMTP_TO）；③ 二次确认：重要事件口径 =「A 级 + 高共振」、影响呈现 =「AI 叙事解读」、允许每周一次的 LLM 调用。

**改动（4 文件，均仅本人改动）**：
- `workbench/catalyst/notifier.py`：新增 `send_catalyst_weekly_report()` 及配套函数。
  - 常量：`NTYPE_WEEKLY_REPORT='weekly_report'`、哨兵 `SENTINEL_WEEKLY_REPORT_SIGNAL_ID=-4`（延续 -1/-2/-3 负号哨兵约定）、`WEEKLY_EVENT_LIMIT=30`（送入 LLM 的事件上限）。
  - 窗口：`_weekly_window()`（北京时区，上周一 00:00 ~ 本周一 00:00）。
  - 素材：`_weekly_key_events()` 口径 = `tier='A' OR resonance_state='confirmed'`，排除 `divergent`，按 `composite_score DESC` 取 Top 30。**关键：不按 `status='open'` 过滤**——按 d3 分层，confirmed 的信号在库中为 `status='watch'`（价格已消化），但「已被定价」正是周报要回看的影响事实（与 A 级 Alert 通道的 `status='open'` 口径刻意不同）。另 `_weekly_overview_stats()` 出多维统计作附录（事件类型/情感沿用 `COALESCE` 统一口径）。
  - 叙事：`_weekly_events_brief()` 压缩输入 → `_weekly_llm_narrative()`（LLMClient，temperature 0.3 / max_tokens 4096 / `use_cache=False`）输出 `{overview, themes[], events[]}`；**失败回退** `_weekly_fallback_narrative()`（标题 + 已有 `ai_summary` 拼接，无主题分组，正文明确标注「AI 叙事不可用」），**回退不阻断发信**。
  - 渲染：`_build_weekly_report_html()` 四段式 = 本周总述 / 主线主题（含标的 chips）/ 大事记与影响（逐条 impact + 利好·利空·中性徽章）/ 本周概览（附录）；**已移除交易档位**（用户未选），并加「非投资建议」免责。Prompt 硬约束含「不得编造价格/数字」。
  - 去重：`_weekly_report_already_sent()` 按**自然周**（本周窗口内 `status='sent'`），非 Alert 通道的 24h 窗口；发送走既有 `_try_acquire_send_lock` 原子占锁。新增 `dry_run` 参数（只查不发、不占去重位、返回 body）。
- `scripts/bin/send_catalyst_weekly.py`：薄脚本（复用 `_setup_paths()` 路径探测 + `get_conn` + `send_catalyst_weekly_report`，`--dry-run` 打印渲染结果）。
- `workbench/scheduler.py`：`SCHEDULE` 新增 `("catalyst_weekly_report", "0 9 * * 1", "send_catalyst_weekly.py", [], "催化剂周报（每周一 09:00）", "core")`。
- `workbench/test_catalyst_weekly_report.py`：离线护栏测试 **57/0**（通道独立、负号哨兵 -4、自然周窗口恰 7 天且北京周一 00:00、概览/重要事件 SQL 口径（含「不按 status 过滤」断言）、叙事链路（LLM/回退/dry-run）、自然周去重、渲染三段结构 + 交易字样负向断言 + 运维告警负向断言、失败不阻断）。

**验证**：`py_compile` 4/4 OK；`test_catalyst_weekly_report.py` 57/0 全绿；**实跑 dry-run 连 prod**（窗口 2026-09-21~09-28：候选 30 条、LLM 产出 14 条事件 + 主题分组、exit=0）——端到端链路（SQL → LLM → 渲染）已验证。

**未做 / 边界（须留档）**：① **runtime 复验须待 Zeabur redeploy**（`push ≠ 线上生效`，新调度项 `catalyst_weekly_report` 需容器重建后才注册，首个执行点为周一 09:00）。② **确认为 `submit_scheduled_task` 白名单/看护未覆盖**：新任务不在 `scheduler_watchdog.KEY_JOBS`，若某周静默失败不会触发告警+补跑（首期先观察，如需可后补）。③ **周报无历史归档表**：叙事仅在邮件正文，未落库（如需「往期周报」页面须另加工单）。④ **LLM 叙事为一次成稿无事实校验**：prompt 已禁止编造，但未做「输出 symbol ⊆ 输入 symbol」的机器校验（回退路径不受影响）。

#### 周报选材口径重建（2026-09-29，同日追加）

来源：用户收到首封周报（标题「重要事件 13 条」）后质疑「**确定这些是重要的催化剂？你联网查一下，是否覆盖全了**」。经与用户二次确认：① 选材口径 =「**重建事件重要性口径**」（放弃 `tier` 作为主口径）；② 采集缺口 =「**一并排查修复**」（非仅记工单）。

**取证（窗口 2026-09-21~09-28 北京，prod 只读）—— 四条根因**：
- **根因 1（口径错配）**：`tier` 唯一来源是 `composite_score`（A≥80/B≥60/C≥40），其权重表在 `grade.py`/`catalyst_rules.yaml` 里是**可交易性**（listing=95）；而周报要的是**市场显著性**。结果 13 条 A 级中 **11 条是 listing**（分数 80~88），同期 **BTC 现货 ETF 创纪录流入 C/57、Bitget 被盗 3.516 亿 C/66、美联储加息 C/69、Fetch.ai 被黑 B/69、CLARITY 法案受阻 C/58–68、Binance 投资 Circle B/75 全部落选**。
- **根因 2（SQL bug）**：旧 SQL `WHERE (s.tier = 'A' OR s.resonance_state = 'confirmed') ... ORDER BY composite_score DESC LIMIT 30` —— A 级 80~88 占满 Top30，confirmed 均分仅 58.4 几无机会，**`OR` 被 `ORDER BY + LIMIT` 吃掉、形同虚设**。
- **根因 3（类型噪音）**：窗口内 `market_update` 纯行情播报 6788 条（**38%**），稀释候选池。
- **根因 4（采集/消费缺口，最关键）**：窗口内 `biz.asset_catalyst` 7049 条中 **4698 条 `asset_id IS NULL`（67%）**，**从不进入 `biz.catalyst_signal`**，故《CLARITY 法案》《Genius 法案》、稳定币监管、美联储加息等宏观/监管要闻**天然不可能入选**。另：窗口内全部事件**仅来自 6 个 `kol_*_binance_square_*` 源**（2743/1475/1132/744/493/462），即**仅币安广场 KOL 内容**。

**修法（消费侧重写，不动采集）**：
1. **素材底盘更换**：`_weekly_key_events()` 从 `biz.catalyst_signal` 改为 **`biz.asset_catalyst` 原始事件**（含无 asset_id 的宏观/监管类），`catalyst_signal` 降级为 `LEFT JOIN LATERAL`（仅补 tier/composite_score/confidence/resonance_state/status）。
2. **两套权重刻意分离**：新增 `_WEEKLY_TYPE_WEIGHT` = **市场显著性**（security 95 / macro 88 / etf 85 / regulation 80 / delisting 72 / tech_upgrade 68 / **listing 62** / funding 55 / burn 52 / partnership 45 / airdrop 45 / staking 40 / governance 40 / market_update 18 / other 32），与 `grade.py` 的 `event_type_weights`（**可交易性**）**互不可替代**；注释已写明。
3. **关键词兜底归类**（库里常见误落 `other` / `market_update`）：security（hack|exploit|stolen|breach|drain|被盗|被黑|遭攻击|漏洞|rug pull）→ etf → macro（美联储|rate hike/cut|加息|降息|基点|通胀|非农）→ 否则回落 `raw_event_type`。**仅匹配标题**（`head` 由 `COALESCE(title_cn,'') || ' ' || COALESCE(title,'')` 构成，**不含 `ai_summary`**）—— 首版误用整段摘要，「Taiko DAO 安全委员会提案」被判 security-95、「今日要闻提示：」「一、热点新闻精选」「TL;DR」「标普500指数…」被判 macro/etf，故收敛到仅标题。
4. **`market_update` 整类剔除**（`WHERE category <> 'market_update'`）。
5. **无标的按「市场级」计权**：`raw_asset_id IS NULL → asset_w=0.75`，且 security/macro/etf/regulation 四类取 `GREATEST(asset_w, 0.5)`，避免宏观要闻因无 asset_id 被埋没；渲染侧补可读占位标的（`_WEEKLY_SYMBOL_FALLBACK`：宏观/监管/安全/ETF）。
6. **候选池放大 + Python 侧收敛**：SQL 按 `importance DESC` 取 `max(WEEKLY_EVENT_LIMIT*10, 400)` = 400 条，Python 侧再做 ① **同一事件去重**（`_weekly_story_key`：`_WEEKLY_SOURCE_PREFIX` 循环剥「来源+日期+转述词」前缀 → 去非字母数字 → 取 40 字符；空键回退 `cid:{catalyst_id}`）并**聚合多标的**；② **广度加成**（覆盖标的数每多 1 个 +3，上限 +15）；③ **单类型配额** `WEEKLY_MAX_PER_TYPE=8`（首版 listing 占 11/13 即因此）；④ 排序后截断。
7. **占位标题与聚合帖剔除**（两类噪音源）：标题为字面量 `'null'/'none'/'nan'/''/'tl;dr'`（LLM 漏译写入）→ `NOT IN` 直接剔除；「今日要闻|要闻预告|要闻提示|一、|二、|热点新闻|行情|盘前|盘后|每日|快讯汇总|市场综述」前缀 → `!~` 剔除（此类帖标题不含单一事件，只会污染关键词归类）。
8. **`_weekly_events_brief` 增 `symbols` 字段**（供 LLM 做主题聚合）；`_weekly_fallback_narrative` 措辞订正（「A 级或高共振」→「按事件类型与标的重要性筛选」；「按信号强度降序」→「按事件重要性降序」）。

**验证**：`py_compile` EXIT=0；`test_catalyst_weekly_report.py` **78/0 全绿**（section 5 全量重写为 13 条新口径断言 + 新增 section 5b「去重/广度加成/占位标的/单类型配额/`_weekly_story_key`」假连接护栏）；**prod dry-run EXIT=0**：窗口 2026-09-21~09-28，候选池 30 条（类型分布 security 8 / etf 8 / macro 8 / regulation 6），LLM 产出 **4 主题 + 14 事件**，**覆盖全部外部大事**——美联储加息 25bp、BTC 现货 ETF 流入 23.1 亿美元、XRP/SOL/ETH 现货 ETF 净流入、贝莱德 ETH ETF 20 日买入 10.1 亿、Bitget 3.516 亿漏洞、XRP 2000 万硬件钱包被盗、Payy Network 被攻、CLARITY 受阻、巴尔鹰派、Hyperliquid 监管、CFTC 调查 Kalshi、DOGE ETF。剥前缀去重收益实测（全周 7049 行）：no-strip/40 → 唯一键 6617（1.07x）；**strip/40 → 5974（1.18x）**；strip/20 → 4978（1.42x），故取 strip/40。

**未做 / 边界（须留档）**：
- ① **源覆盖缺口（独立工单，本轮未修）**：ETH `Glamsterdam`/`Fusaka` 升级在库中 **0 条**（`Fusaka` ILIKE 命中 0；`Glamsterdam` 4 条均 Taiko 无关巧合）—— 属**采源问题**（当前仅 6 个币安广场 KOL 源），**不新增采源无法修复**。（注：原先怀疑的「Solana ETF 创纪录流入」为**误报**——英文 `Solana ETF` 搜得 0，但中文「SOL 现货 ETF 单日总净流入」在库存在且已入选。）
- ② **跨来源改写无法靠标题归一化归并**（同语言不同措辞、中英双语；如 Payy Network 4 个变体、XRP ETF 3 个变体），**交由 LLM 在叙事阶段合并**（prompt 已限定 events 8~15 条）。已写入 `_weekly_story_key` docstring 留档。
- ③ **runtime 复验须待 Zeabur redeploy**（`push ≠ 线上生效`）。首封周报（旧口径 13 条）已在 2026-09-29 09:00 发出并占自然周去重位，**本周不会再发新口径版本**；新口径将于下一自然周首次生效（需先 redeploy）。

#### 重大事件通道口径重建（2026-09-29，同日追加）

来源：用户收到两封 NEAR「📢 [重大事件]」邮件（Rhea Finance 跨链 DeFi／Bitwise NEAR 现货 ETF 递表）后提出「**合理怀疑重大事件邮件通道也存在同样的问题**」。经与用户二次确认三项决议：① 修复范围 =「**全面重建口径**」（`asset_catalyst` 为底盘 + 市场显著性权重 + 关键词兜底归类 + 统一 `COALESCE(ai, rule, 'other')`）；② 门槛 =「**按利好/利空双向判定**」（利好型用异动绝对值；利空型不以涨幅为准、由事件类型权重直达）；③ 无 `asset_id` 的宏观/监管事件 =「**不进逐条告警，只进周报**」→ 告警通道保留 `asset_id` 硬要求。

**取证（窗口 2026-09-15~09-29，prod 只读）**：`major_event` 通道历史**仅 8 封**（09-23~09-28），类型分布 partnership 4 / funding 3 / listing 1，**security/regulation/etf/macro 各 0 封**。五条根因：
- **根因 ①（tier 语义错位）**：`_recent_major_events` 用 `s.tier IN ('A','B') AND s.status='open'` 做「重要性闸门」，而 `tier` 由价格档位/RR/方向闸门决定（`event_type_weights` 里 listing=95），注释却自称「重要性闸门」——**可交易性被当成了重要性**。
- **根因 ②（素材依赖 asset_id + 排序口径错）**：`JOIN ac ON ac.catalyst_id=s.catalyst_id AND ac.asset_id=s.asset_id`，窗口 12972 条中 **8886 条（68.5%）无 asset_id** 永不入选（regulation 无标的 88%=1608/1818、macro 86%=244/285）；且 inner 以 `composite_score` 截断（A 级高分占满名额）。
- **根因 ③（market_update 过滤可绕过）**：只精确匹配 `ai_event_type <> 'market_update'`，误分类即放行——实测 Rhea「价格突破 0.19 美元，24H 涨幅 131%」被判 **partnership** 入池；Bitwise「NEAR 现货 ETF 提交最终招股说明书」被判 **funding** → 渲染成「融资到账 → 基本面改善」，**解释错误**。
- **根因 ④（prelaunch 门槛单向性，最狠）**：`prelaunch_ret_24h >= 5 AND prelaunch_penalty = 0` 要求事件**发生前已涨** ≥5% → **结构性灭杀利空型重大事件**。有 asset_id 事件的通过数：security 71→**4**、macro 13→**0**、delisting 22→5、regulation 210→27、etf 40→6。近 14 天全量候选仅 7 条（partnership 4/listing 2/funding 1，全 tier=B）。
- **根因 ⑤（权重表缺 security）**：`catalyst_rules.yaml` 的 `event_type_weights` **无 `security` 键**（只有 listing95/delisting90/burn80/regulation75/airdrop70/funding65/partnership60/tech_upgrade55/market_update25/other15）→ hack 类 `event_weight` 兜底 15（邮件实测显示「事件权重 15」）；且 `structural_event_types={listing,delisting,burn,regulation,tech_upgrade}` **不含 security** → 旧 `catalyst_kind = ANY('{structural,event}')` 把低分 security 事件（kind=sentiment/noise）一并排除。

**修法（只动消费侧 `notifier.py`，不动采集/分级）**：
1. **常量**：保留 `NTYPE_MAJOR_EVENT`／`MAJOR_EVENT_COOLDOWN_HOURS=24`／`MAJOR_EVENT_MAX_PER_RUN=3`；**删除** `MAJOR_EVENT_MIN_PRELAUNCH_RET`／`MAJOR_EVENT_KINDS`；**新增** `MAJOR_EVENT_MIN_MOVE=5.0`、`MAJOR_EVENT_MIN_IMPORTANCE=70.0`、`_MAJOR_BEARISH_TYPES=('security','delisting')`。
2. **权重复用**：类型权重直接复用周报 `_WEEKLY_TYPE_WEIGHT`（security95/macro88/etf85/regulation80/delisting72/…）——在函数内把该表**运行时**拼成 SQL `CASE`（`weight_case`），保证两处口径不漂移。
3. **SQL 重建**（`WITH base → categorized → scored → gated`）：`head` 仍仅由标题构成（`COALESCE(title_cn,'')||' '||COALESCE(title,'')`）；`category` 关键词兜底 security → etf → macro，**并新增行情播报识别**（`head ~ '涨幅' AND head ~ '%'`、`head ~ '价格突破|暴涨'` → `market_update`）再回落 `raw_event_type`；`importance = 类型权重 × 资产权重`（`market_cap_rank` 分档 1.00/0.80/0.62/0.45/0.30，NULL→0.5）`+ confirmed 加成 6`。
4. **WHERE 重写**：`importance >= MAJOR_EVENT_MIN_IMPORTANCE` AND `(is_bearish OR (ABS(prelaunch_ret_24h) >= 5 AND prelaunch_penalty=0))` AND 发布时间窗口 AND `ASSET_NAME_FILTER_SQL` AND 24h 同资产冷却 NOT EXISTS AND `category <> 'market_update'` AND `catalyst_kind <> 'noise'` AND 标题非占位/非聚合帖；**删除** `s.tier IN ('A','B')`／`s.status='open'`／`catalyst_kind = ANY(...)`／旧的 `<> 'market_update'`。`is_bearish = category IN ('security','delisting') OR ai_sentiment='bearish'`。
5. **排序**：`DISTINCT ON (asset_id) ... ORDER BY asset_id, importance DESC, composite_score DESC NULLS LAST`，外层 `ORDER BY importance DESC, composite_score DESC NULLS LAST LIMIT %s`（不再按合成分截断）。
6. **渲染**：SELECT 增 `category AS event_type_norm`／`importance`／`is_bearish`；`_transmission_path` 优先取 `event_type_norm`；`_TRANSMISSION_PATH_CN` 新增 `security`／`etf` 键；「事件类别」行改用 `event_type_norm`；`_build_major_event_html` 的「预期已消化」与「市场确认」两处按 `is_bearish` 分支——**利空型不再写「公告前 24h 已涨」**（旧文案会把利空写反），改「事件类型直达（利空型不以异动确认）」。

**验证**：`py_compile` EXIT=0；`test_major_event_alert.py` **67/0 全绿**（section 2/4/5 全量重写为新口径断言 + section 6 新增利空型渲染护栏 4 条）；`test_catalyst_weekly_report.py` **78/0 无回归**；**prod 只读探针**（窗口 336h=14 天，`_recent_major_events` 实跑）→ 候选 **14 条**：**security 8 / etf 3 / regulation 3**（旧口径此三类合计 0），利空型 9 条，含 **Bitget 3.516 亿被盗（BNB）**、**XRP 硬件钱包 2000 万被盗**、**Payy Network 跨链合约被攻**、**CME 上线 BCH 期货（监管待批）**、**Grayscale Zcash ETF**、**SEC 代币化股票豁免（UNI）**；两封 NEAR 邮件对应事件在窗口内**候选 0 条**（Rhea 归 `market_update` 剔除；NEAR ETF 重要性 <70）。

**未做 / 边界（须留档）**：
- ① **runtime 复验须待 Zeabur redeploy**（`push ≠ 线上生效`，容器重建后新口径才在线上告警通道生效）。
- ② **仍在 `asset_id` 硬要求内**：无标的的宏观/监管事件按用户决议只进周报、不进逐条告警（告警需 `signal_id` 作为发送锁/去重锚点）。
- ③ **`market_update` 识别靠标题正则**：`涨幅…%`／`价格突破/暴涨` 属启发式，标题同时含「涨幅」与真实事件的少数混合帖会被整类剔除（误差方向偏保守）。
- ④ **阈值 70 为主观分界**：NEAR「ETF 递表」类（etf 85 × asset_w 0.62~0.80 = 52.7~68）落在阈值下缘，属「市场显著性不足」而非漏采；如需上调/下调只改 `MAJOR_EVENT_MIN_IMPORTANCE` 一处。
- ⑤ **SQL 注释内的字面量 `%` 必须写成 `%%`**：psycopg 的占位符解析器**不识别 SQL 注释**，注释里出现裸 `%` 会抛 `UnicodeDecodeError`（本轮实测踩坑，已在注释中规避）。

### 代币基本面统一 SSOT（工单 SSOT-001，2026-09-27，本次提交）

**现象**：同一份 `biz.asset_tokenomics` 有 **4 个组装点**且口径已漂移——① `db_stats.get_asset_tokenomics()` 字段最全（22 列 + `biz.asset_token_unlocks` 的 revenue/valuation/overview）但**无逐字段来源/时点**；② 投研结论 prompt 的 inline `_fund` **只吃 `lp_locked / contract_renounced / buy_tax_pct / sell_tax_pct`**；③ 解锁测算 prompt **另起一套 raw SQL 吃 10 列**并**复制了一份 CMC supply 校验**；④ 页面各渲染一个子集。**核心病根**：库里已有的 `allocation / burn_info / emission_schedule / inflation_info / governance_info / utility_info` **从未进入投研结论主线**（只进解锁支线）——2043 行中 allocation 776、emission 676、utility 1204、governance 328、burn 203、inflation 155 全部对投资决策 prompt 不可见。

**修法（唯一事实源 + 三端接线，不改采集）**：
1. **新增 `db_stats.get_asset_fundamentals(asset_id)`**：内部复用 `get_asset_tokenomics()`（**不改其签名与返回结构**，保 4 个既有消费点），逐字段返回信封 `{value, source, as_of, confidence, missing_reason}`，另出 `coverage{present,total,missing}`、行级 `confidence`、`source_urls`、`assembled_at`。
2. **missing_reason 四态**（本轮判据）：`None` 有值且新鲜；`not_collected` 行/字段缺失；`fetch_failed` 有重试留痕（`extract_status ∈ {failed,error}` 或 `extract_attempts>0 且 next_retry_at 非空`）但仍取不到值；`stale` **有值但** `as_of` 超 `_FUND_STALE_DAYS=180`（**value 仍为原值**，消费端规则：`value is not None` 即渲染，再叠加陈旧告警）。**缺失优先于陈旧**：行陈旧时缺失字段仍记 `not_collected`。
3. **来源标注**：supply 三件套与 CMC 权威快照值一致 → `cmc_quote_snapshot`（`as_of` 用快照 `quote_time`），否则 `biz.asset_tokenomics`；github/defillama_tvl 各自独立来源。
4. **三端接线**：① 投研主线 `_fund` 改为 `fundamentals_raw_values(get_asset_fundamentals(asset_id))`，prompt 规则 14 同步扩项（新增 allocation / emission_schedule / inflation_info / burn_info / governance_info / utility_info，并要求 supply 维度消费 allocation）。② 解锁 prompt 删除其独立 raw SQL 与重复 CMC 校验，改由 SSOT 组装 `tkn`（键名照旧，下游 payload 不变）；旧实现的缺失占位 `'无'` 改为具体原因（未采集 / 采集失败 / 数据陈旧），`_or_missing()` 承载。③ 页面：两个 tokenomics API **`data` 结构不变**，仅新增同级 `meta`（老前端零影响）。
5. **页面重构**：`research.html` `renderTokenomics(d, meta)` 改为**有值才渲染 + 卡片级缺失留痕**，**删除 `slice(0,120)` 截断**（长文本由 `.tm-row` 换行承接），新增「治理与用途」分区与 `renderFundMeta()`（覆盖率 / 缺失清单中文名 / 来源 / 数据时点 / 置信度 / 陈旧告警）；`index.html` `renderTokenomics(t, meta)` 补 `inflation_info` 一行与同款覆盖率页脚。
6. **新增公共小函数**（从 `generate_research_thesis` 抽取，供 SSOT 与主线共用）：`_fetch_github_activity()`、`_fetch_dl_tvl()`、`_fetch_tokenomics_retry_meta()`、`_fetch_cmc_supply_baseline()`、`_parse_ts()`、`_is_stale()`、`_fund_field()`、`fundamentals_raw_values()`；字段清单常量 `_FUND_TOKENOMICS_FIELDS`（15 项）+ `FUND_SOURCE_LABELS`。

**验证**：新探针 `workbench/test_asset_fundamentals_ssot_20260927.py` **68/0**（纯离线，桩掉 5 个外部依赖：契约/四态/来源/Coverage 自洽/raw 摊平/三端源码护栏/规则 14 扩项/两页渲染护栏/字段清单三端一致/API 兼容/只读护栏）。既有回归全绿：`test_research_3tier_20260927` 82/0、`test_research_determinacy_20260926` 127/0、`test_thesis_forward_track_20260927` 63/0、`test_macro_market_p0` 16/16、`test_macro_market_board_tier2` 36/36、`test_fundamental_liquidity` 13/0、`test_unlock_refresh_20260926` 23/0；`py_compile` 2/2 OK；两模板内联 JS 抽取（Jinja 占位替换为字面量后）`node --check` 双 OK。本轮**无 DDL、无数据迁移**（`create_asset_tokenomics.sql` 未动）。

**未做 / 边界（须留档）**：
- ① **不改任何采集脚本、不提升覆盖率**：`buy_tax_pct 63 / lp_locked 46 / contract_renounced 41` 仍为 **2–3%**，`biz.asset_tokenomics` 总覆盖 2043/21833（9.4%）；覆盖率另开独立工单。
- ② **存量 495 条 `research_thesis` 未重算**（用户决议）：仅新生成的结论带新增基本面字段；旧结论页的 `structured_metrics.fundamentals` 仍是旧 4 项（读路径不重算 fundamentals）。
- ③ **`stale` 阈值是行级统一值（180 天）**，未做字段级细分（如 tax/LP 与 allocation 的更新节奏不同）。
- ④ **CMC supply 查询仍有并行副本**：`_fetch_cmc_supply_baseline()` 与 `_build_structured_metrics_inner`（L~8400）/另一处（L~3010）各自查一次 —— 本次只**消除了解锁 prompt 那份**（净减一处），另两处属其它函数内部语义，未动以免回归。
- ⑤ **`_fetch_github_activity` / `_fetch_dl_tvl` 吞掉异常且不再 `_emit`**：原 inline 实现失败时会打日志，抽取后静默返回空（页面会显示「未采集」，无法区分「无映射」与「查询报错」）。
- ⑥ **契约与原工单差异**：返回顶层**未含 `symbol`**（`get_asset_tokenomics` 无该字段，避免为此再加一次查询；消费端本来就有 symbol）；字段清单**新增 `tax_info` / `lp_lock_info`**（原工单未列，但两页本就在渲染，纳入后口径才一致）。
- ⑦ **runtime 复验已闭环（2026-09-27 17:53 线上生效，`4e984ad`）**：
  - ✅ **页面侧**：`/api/assets/11114/tokenomics` 与 `/api/research/11114/tokenomics` 均返回同级 `meta`，`meta.coverage` **逐字段一致**（`present 4 / total 15`，missing 11 项），`data` 键数仍 24、**无 `meta` 渗入 `data`**（老前端零影响）；`meta.fields` 15 项信封齐备（supply 三件套 + allocation 记 `missing_reason=None`，其余 `not_collected`）。
  - ✅ **prompt 侧**：触发一次结论生成（`POST /api/research/11114/thesis`，task `d0f438c3606b`）→ 任务日志出现 `基本面补充：allocation, circulating_supply, max_supply, total_supply`。**改造前 11114 的 4 个旧字段（买/卖税、LP 锁定、弃权）全为 NULL ⇒ 基本面块为空、该行根本不会出现**，故此行即「库里已有字段首次进入投研 prompt」的直接证据。
  - ✅ **本地只读端到端**（prod DB，无写）：`get_asset_fundamentals(11114)` 与线上 `meta` 完全一致（4/15）；另取字段最丰富的 **DGB(1137) = 10/15**，`allocation / burn_info / emission_schedule / inflation_info / governance_info / utility_info` **六键齐全**，DCR(1231) 8/15，满足工单 7.1「六键有值时非空」。
  - ⚠️ **工单 7.3-① 措辞不可达（须留档）**：`/api/research/<id>/notebook` 的 `structured_metrics` **读路径不重算 fundamentals** —— 它由 `_build_structured_metrics_from_snapshot → _build_structured_metrics_inner`（L3159/L3186）**实时重建**并**覆盖** `thesis["structured_metrics"]`（L3930），而该函数只产 `market / tokenomics / unlock / onchain / social / derivatives / pressure / data_freshness` **八段、从无 `fundamentals` 段**；`fundamentals` 也**不落 `biz.research_thesis`**（表内仅有 `thesis_json / key_metrics_json / risks_json / catalysts_json / sources_json / analysis_json`），它只作为 **LLM 输入的 `metrics_structured["fundamentals"]`** 存在。故 7.3-① 的原意（证明新字段进了结论生成）由上面的**任务日志行**达成，**未为此改 notebook 读路径**（避免超出「不改采集、页面 data 结构不变」的范围）。
  - ✅ **research 页目检 PASS**（浏览器实取 DOM）：`#r-tokenomics` 内文本含「已采集 4/15 项 · 缺失：买入税、卖出税、流动性锁定、合约权限放弃、释放计划、通胀机制、销毁机制、治理、代币用途、GitHub 活跃度、协议 TVL · 数据时点 2026-09-27 · 提取置信度 1.00」，长文本完整换行、**无 120 字截断**，且存在「治理与用途」分区 —— 与线上 `meta` 的 4/15 逐项吻合。
  - ✅ **index 页已上线（curl 实证，非目检）**：取主页 HTML（661797 B）确认含 `renderTokenomics(d.data, d.meta)`(×2)、`TKM_FUND_LABELS`(×2)、`id="rc-tokenomics"`、`id="tkm-indicator"`、`通胀机制`(×2)、覆盖率页脚模板 `已采集 ${cov.present}/${cov.total} 项`（注：index 的字段来源块是既有 `.tkm-source`，文案为半角冒号 `来源: `，非全角）。
  - ⚠️ **index 页浏览器目检未完成（非阻断，环境问题）**：18:14 起本机**沙箱网络层故障**（`example.com` 亦 TLS 失败、zeabur 被解析到 `28.0.0.31`），沙箱内 curl 全 `000`、浏览器子代理取回 502/timeout；`dangerouslyDisableSandbox` 绕过后同一 URL 立即 200，**证明站点一直正常、502 系沙箱代理所致**。故 index 目检改由「部署 HTML 实证 + 探针 9 分区源码护栏 + `node --check` + 与 research 页同构渲染逻辑已目检」交叉覆盖，**未再重试浏览器**（避免无谓重试）。

### 大盘早报「投资指导意义」重构 P0（2026-09-27，本次提交）

来源：`方案_大盘早报_投资指导意义重构_2026-09-27.md`（用户上传，451 行）＋我方修订版方案（用户「按修订方案执行」批准）。样本：`📊_加密大盘早报_2026-09-27.eml`（实测 `href count: 0`、`查看详情`×1、`置信度`×3、`85%`×2）。原方案 P0 含 5 项，本轮**收敛为「不撒谎」最小集**：可算分、观望闸门阈值、条件触发清单均推后到 P1。

**P0-a 去硬编码置信度**：`send_daily_brief.py` 删 `conviction_pct = {"high":85,"medium":65,"low":40}`（与证据量/数据完整度/历史胜率均无关的纯装饰），头部右侧改「**证据覆盖 N/M 项** · 信心X」；N = `data_quality` 中 `status=="ok"` 的维度数，M = 总维度数（无 `data_quality` 时显示 `—`）。

**P0-b 数据门控 missing ≠ 0**（M1 与 M5 合并为**一次** prompt 改动）：
- `macro_market.generate_morning_brief_ai_summary` 新增 `data_quality` 块：7 个维度逐条 `{section, status(ok/partial/empty/error), items}`；**空段（`None`/`{}`）一律记 `empty`，绝不记 `ok`**（否则「证据覆盖」会虚高，与 P0-a 的展示直接矛盾）。
- 组装 None-aware：`交易所净流入` 由 `.get('net_exchange_usd', 0)` 改为 `_wm_ok` 驱动——无有效样本时渲染「数据不可用（无有效样本）」，有样本时渲染「N M USD（样本 K 笔）」；`总笔数`/`总金额` 同口径；空段落渲染 `- （暂无数据：交易所净流量不可用）` 而非空字符串（旧行为让 LLM 把「空」读成「零」）。
- system prompt：旧「必须给出具体方向 / 不能只说关注」三条被替换为 5 条硬约束（默认输出无操作 / 缺失只能表述为「数据不可用」且严禁表述为零·无·没有·抛压有限 / 依赖 empty 维度的结论须标「依据不足」/ 禁止凑数 / 基于数据）；输出 schema 增 `no_trade_reason`。
- 返回 dict 增 `no_trade_reason`、`data_quality`。`generate_morning_brief` 是**直接赋值** `brief["M0_ai_summary"]`，新键自然透传；`brief_data_model.normalize_brief` 只标准化 M2/M6 特定模块、不触碰 `M0_ai_summary`（已核）。

**P0-c 渲染层兜底**（「允许空数组」≠「LLM 会可靠输出空数组」）：`render_brief_html` 渲染前判断「|BTC|、|ETH| ≤ 1% 且 `M3_highlights` 空 且 `M4_risks` 空」，若 AI 仍给方向 → 强制置空 + 打印降级日志 + 写入兜底原因「当日横盘、无新鲜信号，无明确可执行机会」。**只降不升**的单向闸门。

**P0-d 假入口**：删高危信号条内 `<span style="…cursor:pointer">查看详情 ↓</span>`（全邮件 `href=0`，点了没反应）。本轮选「删 affordance」而非「补真链接」（无落点页面）。

**P0-e 砍脏卡**：① 催化剂热点卡（B/C 级）**整块代码删除**（而非 `cat_hotspots = []` 置空留死分支）——实测上屏为脏数据（正文出现爬虫署名「作者：谷昱，ChainCatcher」、英文截断半句），且固定文案自指「高置信度 A 级见邮件 Alert」把读者指向另一封邮件（闭环断裂）；重放条件留档在源码注释：AI 正文质量修复 且 仅 A 级 且 带原文链接。② Meme 卡由纯数字（`高危0 · 中危102 · 低风险3 · 排雷0`）改「排雷/高危」名单（各 ≤5 个符号），无名单则不出卡。③ KOL 卡加就绪门（需 `created_at` 且有 `event_usd_value`/`event_amount`），并把金额与事件时间渲染上屏——字段名以 `fetch_kol_onchain_signals` 的 SELECT 为准（`created_at`/`event_direction`/`event_usd_value`/`event_amount`，不是 `published_at`/`amount_usd`）。

**验证**：新探针 `workbench/test_daily_brief_p0_20260927.py` **46/0**（纯离线：渲染层喂合成 brief；prompt 层用假 `LLMClient` 捕获 system/user prompt，**不连 DB、不发信**）。冻结断言含：空数据场景 user_prompt **必含**「数据不可用（无有效样本）」「（暂无数据：交易所净流量不可用）」、**不得含**「交易所净流入：0.0M USD」；`data_quality` 7 维全空时**无一被记 ok**；横盘+无信号时 AI 的「做多」不得进入邮件、有信号或非横盘时**不误伤**；空建议渲染「⚪ 今日无操作」+ `no_trade_reason`；渲染幂等（两次渲染字节一致）。`test_daily_brief_20260924` 的旧断言 `Meme 风险（Meme 专项）` 依赖纯计数卡，已同步改版为「无名单不出卡 + 有名单出卡」→ **31/31**。其余回归全绿：`test_daily_brief_p1` 22/0、`test_catalyst_channel_dedup` 22/0、`test_major_event_alert` 55/0；`py_compile` 2/2 OK。

**未做 / 边界（须留档）**：
- ① **P1 全部未做**：条件触发清单（每条 5 要素 `trigger`/`invalidate`/`target`/`horizon`/`ref_price`+时间戳）、单一口径裁决（同标的多结论、同赛道两个涨幅数字、2Z 既领涨又高危）、「可算分」与观望闸门阈值。
- ② **AI 结论未落库（P2 前置依赖，实测确认）**：`biz.market_overview_snapshot.payload` 顶层键为 `summary/btc_cycle/meme_risk/resonance/dimensions/fetched_at/event_calendar/chimney_signals/opportunity_list/divergence_signals/institutional_mvrv/smart_money_divergence/onchain_anomaly_signals`，**不含 `M0_ai_summary`/`DIFF`/`M9_degraded`**。故 `data_quality` 与 `no_trade_reason` 目前只活在「生成→渲染」这一次内存链路里，**不落库、不可回测、次日不可比**；变更日志（观点连续性 M2）依赖此，故未做。
- ③ **兜底闸门阈值是保守硬编码**：只用「|BTC|、|ETH| ≤ 1%」，未做波动率归一（ATR/σ），且只看 BTC/ETH，小市值币横盘不触发。
- ④ **`data_quality` 的 `partial` 分支在真实数据上是否出现未验证**（现有 7 段生产者可能只产出 ok/empty/error）。
- ⑤ **SMTP 未配时的「静默成功」已收紧为本轮 P0-f**（原先与上一节同项的待立项目）：`send_daily_brief.main()` 的 `if not notifier.configured:` 分支由 `return 0` 改为 `return 1`，措辞由「[WARN] 跳过邮件发送」改为「[ERROR] 早报邮件未发送（按失败处理，避免静默成功）」。依据 `task_manager.py:782` 的 `error=None if returncode == 0 else f"exit code {returncode}"` —— 旧行为下任务记 `done` 而邮件没发（与 09-26 丢整天的陷阱同类）；现记 `failed: exit code 1`，`daily_brief_email` 已在 `KEY_JOBS`，故会告警 + 补跑。不发信不可能造成重复投递，无告警风暴。**边界**：本轮仍**未**给该分支加「只告警一次」的去重（若连续多日 SMTP 未配，看护会每日告警 + 每日补跑失败）。
- ⑥ **runtime 复验须待 Zeabur 约 6 分钟重建**（`push ≠ 线上生效`）：需验次日 09:00 邮件头部为「证据覆盖 N/M 项」而非「置信度 85%」、「查看详情」已消失、横盘日不出现方向建议。

### 大盘早报「投资指导意义」重构 P1（2026-09-27，本次提交）

承接上节 P0。P1 收敛为 **P1-a 交易方向可执行化 + P1-b 观望闸门**（原方案 §3.3 M2/M5）；**M4 单一口径裁决、变更日志（M3）、AI 结论落库仍推后**（M3 依赖落库，见 P0 边界②）。

**P1-a 交易方向可执行化**：一条建议要进「💡 具体交易方向」区，必须齐备 **6 个可判定字段** `trigger`/`invalidate`/`target`/`horizon`/`ref_price`/`ref_as_of`，缺任一 → 该条**降级**进「👀 观察（不构成建议·缺可判定条件）」区（**降级而非删除**，信息不丢弃）。
- `macro_market.generate_morning_brief_ai_summary`：JSON schema 的 `trade_suggestions` 增 `trigger`/`invalidate`/`target`/`ref_price`/`ref_as_of`（`trigger`/`invalidate` 明确要求「价格或指标 + 具体阈值」，禁「关注/择机/逢低/留意」等不可判定表述）；system prompt 要求第 2 条改为「六要素必须齐备，缺任一者不得写进 `trade_suggestions`，改放 `watchlist`」；返回 dict 透传并截断（`trigger`/`invalidate` 160、`target` 120、`ref_price` 24、`ref_as_of` 40 字符）。
- `scripts/bin/send_daily_brief.py`：模块级新增 `_REQUIRED_TRADE_FIELDS` / `_TRADE_FIELD_CN` / `_trade_missing_fields()`；渲染前把 `_ai_trade_suggestions` 分流为 `_trade_ready`（进交易区，渲染 进场/失效/目标/参照 4 行结构化字段 → 合计 ≥5 行）与 `_trade_excluded`（**打印计数 + 标的 + 缺项到 stdout**，并渲染「👀 观察」小区）。
- **耦合（必须同步修）**：`workbench/test_daily_brief_p0_20260927.py` 的 `_brief()` fixture 原 `trade_suggestions` 只有 `asset/direction/horizon/reason`，加拒收后会被全部踢出、导致 P0-c 两条「不误伤」断言（`"做多" in html`）失败 → 已给 fixture 补齐 6 字段。

**P1-b 观望闸门**：复用 P0 的 `data_quality`，新增模块级常量 `_DQ_MIN_OK_FOR_TRADE = 2`；渲染前若「有 `data_quality` 块 且 `status=="ok"` 维度数 < 阈值」而 AI 仍给方向 → 强制置空 + 打印日志 + 写入原因「数据覆盖不足（N/M 项可用），证据不足以支撑方向」。**只降不升**的单向闸门，与 P0-c 同处（`render_brief_html` 的 override 区）。无 `data_quality` 块（旧 payload）时**不触发**，避免误伤。

**偏离原方案（留档）**：原方案 §3.3 M2 表述为「5 要素」（参照价 + 时间戳合一）；实现拆为 **6 个独立字段**（`ref_price` 与 `ref_as_of` 分开校验/渲染），因渲染需时间戳单独上屏，合一无法保证「必须带时间戳」。

**验证**：`workbench/test_daily_brief_p0_20260927.py` **49 → 69/0**（新增 20 条：P1-a 齐备条渲染 ≥5 行结构化字段、缺字段条不进交易区、观察区列出标的与缺失项、拒收计数写入渲染日志（`contextlib.redirect_stdout` 捕获）、齐备/缺字段混合并存；P1-b 闸门生效、覆盖达标不误伤、无 `data_quality` 不触发）。回归全绿：`test_daily_brief_20260924` 31/31、`test_daily_brief_p1` 22/22、`test_catalyst_channel_dedup` 22/0、`test_major_event_alert` 55/0；`py_compile` 3/3 OK。

**未做 / 边界（须留档）**：
- ① **M4 单一口径裁决未做**：同一标的多结论（SOL 67 vs 76）、同赛道两个涨幅数字（AI 18.3 vs 17.1）、2Z 既领涨又高危，均未裁决。
- ② **变更日志（M3）未做**：依赖 AI 结论落库（P0 边界②），当前 `data_quality`/`trade_suggestions`/`no_trade_reason` 只在「生成→渲染」一次内存链路里。
- ③ **闸门阈值未校准**：`_DQ_MIN_OK_FOR_TRADE = 2` 为写死值，未做回测校准（与 P0 兜底的 `|BTC|/|ETH| ≤ 1%` 同类保守硬编码）。
- ④ **`_trade_excluded` 的 `direction` 仍出现在观察区**：属「信息保留」而非建议（观察区显式标注「不构成建议」）；若后续要连方向一并折叠，需另议。
- ⑤ **交易区仍只渲染前 4 条**（`_trade_ready[:4]`，与旧 `[:4]` 一致）；观察区同样截 `[:5]`。生成侧上限 5 条不变。
- ⑥ **runtime 复验须待 Zeabur 约 6 分钟重建**：需验次日 09:00 邮件「具体交易方向」每条含 进场/失效/目标/参照（带时间戳），且缺字段条目出现在「👀 观察」而非被静默丢弃。

### 早报重构 P2-a：M4 单一口径裁决（2026-09-27）

承接上节 P1。本轮落地原方案 §3.3 **M4 单一口径裁决**四条要求；**变更日志（M3）、AI 结论落库仍推后**（M3 依赖落库，见 P0 边界②）。

**M4-1 同一 target 只保留一条结论（折叠关联）**：`scripts/bin/send_daily_brief.py` 新增模块级 `_norm_target_key()` / `_target_keys()` / `_build_target_registry()`。渲染前扫全邮件各板块（交易方向 / AI 精选高亮 / 今日高危信号 / 赛道轮动），得到 `owners`（归一化键 → 板块名）。「🎯 精选机会」卡对每张卡用 `target`+`symbol`+`name` 三别名归一化，凡命中 `owners` 的标的**不再上屏**，改在卡底部以「关联折叠（…此处不重复列示）：X → 见「Y」」一行说明去向（信息不丢、不并排列示）。**领涨币不参与折叠归属**（领涨非独立结论），只用于 M4-4 裁决。

**M4-2 排序口径分离**：模块级 `_tier_score_key()`（`(_TIER_RANK[tier], score)`），`all_opps` 由原「纯分数直排」改为**先按等级 HIGH/MED/LOW 分组、组内再按分数**；机会卡标题由「按综合评分排序」改为「按证据等级分组·组内按分数排序」，披露口径。

**M4-3 同赛道唯一事实源**：`workbench/macro_market.py` 新增 `_norm_target_key()` / `_sector_ssot_map()` / `_unify_sector_metric()`，在 `generate_morning_brief` 组装 brief 前调用。以 `biz.sector_flow_daily`（`M2_sector_flow.sectors`，日频 ETL）为唯一事实源，把叙事机会（`signal_type="narrative"`）内嵌的「市值 +X%」用 `_SECTOR_MCAP_RE` 改写成赛道口径，并回填 `mcap_change_7d_pct` + `mcap_ssot=True`；SSOT 落 `brief["M4_sector_ssot"]`（`{metric_date, map}`）供渲染层复用。根因：两张卡两条取数路径（日频 ETL vs CMC categories 现算）→ 同一赛道同一天两个涨幅数字（实测 AI & Big Data +18.3% vs +17.1%）。

**M4-4 冲突必须裁决**：`_build_target_registry` 同时产出 `arbitrations`（同一标的**既领涨/入高亮、又入高危**）→ 渲染「⚖️ 单一口径裁决（同一标的只取一条结论）」区块（置于 AI 精选高亮卡之后，最多 4 条）。裁决语例：「2Z 领涨Infrastructure属资金驱动，同时存在高危信号风险，**判定：不参与**」。无冲突时不输出该区块。

**验证**：`workbench/test_daily_brief_p0_20260927.py` **69 → 83/0**（新增 14 条：M4-2 等级分组优先于分数 + 标题披露口径；M4-1 SOL 折叠为「关联」且第二份分数不上屏；M4-1/M4-3 同赛道仅一个涨幅数字；M4-4 裁决语 + 无冲突不输出；M4-3 数据层 SSOT 归一化索引 / 改写 / 回填 / 幂等）。回归全绿：`test_daily_brief_20260924` 31/31、`test_daily_brief_p1` 22/22、`test_catalyst_channel_dedup` 22/0、`test_major_event_alert` 55/0、`test_macro_market_p1_upstream` 24/24；`py_compile` OK。

**未做 / 边界（须留档）**：
- ① **折叠仅在「精选机会」卡生效**：交易方向 / 高亮 / 高危 / 赛道轮动四个板块本身不去重（它们是结论的**产出位**）；同一标的若同时进「高亮(long)」与「高危(short)」两个产出位，两卡仍并排，但会由 M4-4 裁决区块给出解释。
- ② **M4-2 只改了「精选机会」列表**：AI 精选高亮卡仍按生成侧顺序取前 3，未做等级分组重排（其条目本就同源同口径）。
- ③ **M4-3 只统一「叙事机会」**：仅匹配 `target`/`symbol` 归一化后与赛道标签/键相同的条目；CMC 分类名与 `sector_12` 标签不一致时（如「Artificial Intelligence」vs「AI & Big Data」）不触发，数字仍各自显示。
- ④ **裁决语模板为写死文案**，风险类型未逐条枚举（只说「高危信号风险」），因风险 `signal_types` 未透传到渲染层。
- ⑤ **变更日志（M3）未做**：仍依赖 AI 结论落库（P0 边界②）。
- ⑥ **runtime 复验须待 Zeabur 约 6 分钟重建**：需验次日 09:00 邮件中同一标的只有一个方向结论、同一赛道只有一个涨幅数字、冲突标的带「判定：不参与」。

### 加密大盘早报 2026-09-28 审计处置（指导意义 + 详细度，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_加密大盘早报_2026-09-28.md`。审计判定「可读性已不是问题，短板是**把数据转成决策**这一层」。本轮落地 **P0-1 / P0-2 / P1-1 / P1-3 / P1-4 / P1-5 / P1-6 / P1-7 / 准确性P1 / P2-1 / P2-2 / P2-3**；**P1-2（板块目标=移动止盈无规则）由既有 P1-a 六要素闸门部分覆盖，未再加模板**；BTC $85K 取整口径属数据源，未改。**零 DDL、零迁移、未改任何阈值/评分权重、未改 tier 判定口径**。

- **P0-1 顶部风险缝合层（`send_daily_brief._top_risk_guard_html`）**：`BTC 周期` 含「顶」或 `恐贪 ≥ 70` 时，在 AI 定调块的交易方向之后强制追加「⚠️ 高风险环境约束」注脚 —— 战术性/轻仓（**建议单笔不超过常态仓位的 50%**，常量 `_TOP_RISK_MIN_POSITION`）/必带止损/可部分止盈，并给「失效翻转」视角（BTC 跌破 50 日线或恐贪回落中性 → 方向全失效，转观望/减仓）。无触发条件时返回空串（不渲染空壳）。
- **P0-2 内部数据矛盾（口径一致性）**：① **交易所净流量**：`generate_morning_brief_ai_summary` 的全局口径替代源文案加「**【替代源估算 · 非「交易所净流量」模块口径】**」，并在 `data_quality` 该维度打 `note`「本封以全市场链上转账全局口径估算 X（替代源，非该模块口径）」；② **即将解锁**：`M6_upcoming_unlocks` 不可用但催化剂 `token_events` 有 unlock 事件时，`data_quality` 该维度打 `note`「解锁事件由催化剂管道（宏观&代币事件）提供，与「即将解锁」模块口径不同」；③ `send_daily_brief._data_status_table_html` 新增渲染 `note`；④ system prompt 新增规则 10（empty 维度不得当可用、引用替代源必须保留标注）。
- **P1-1 今日操作清单（TL;DR）**：新增 `_build_tldr_html`，置于 AI Morning Call 正下方（邮件前 1/3）。优先取六要素齐备的交易方向（asset + 方向 + 进场 + 失效），不足 3 条用机会清单 `trigger_logic` 补足；**遵守 M4-1 折叠**（`_tgt_owners` 已在其他板块给出结论的标的不重复），否则会把「精选机会」被折叠掉的重复结论又渲一遍（首版即踩此坑，`test_daily_brief_p0_20260927` M4-1/M4-3 断言当场抓到，已改为先算 `_build_target_registry` 再建摘要）。
- **P1-3 高危信号逐条解读**：新增 `_risk_one_liner`，把原「只列名字」改为逐条「风险点 + 应对」；稳定币/锚定资产（`_STABLE_RISK_SYMBOLS` 含 USDC/USDS/CBBTC/XAUt 等）必解释「脱锚/储备/监管，应对=缩短敞口」。
- **P1-4 证据覆盖缺项披露**：AI 定调头部「证据覆盖 N/M」旁新增「缺 X、Y」（`_coverage_missing_names`），读者无需翻到数据状态表。
- **P1-5 赛道领涨币涨幅**：赛道卡领涨币由「只有符号」改为「符号 + 7d 涨幅」（数据本就在 `fetch_sector_flow_with_leaders` 的 SELECT 中，此前未渲染）。
- **P1-6 数据时效逐卡标注**：ETF 卡补「，滞后 N 天」；赛道卡补「截至 MM-DD，滞后 N 天」；巨鲸动向补「（截至 snapshot_date）」。
- **P1-7 DePIN 自相矛盾**：高亮卡若 `_ai_downgraded` 或 reason 含「不构成高亮」→ 渲染灰色徽章「观察级（非高亮）」。
- **准确性 P1（恐贪标签）**：`macro_market._fng_extreme_label(value, is_greed)`（模块级纯函数）—— 70–74 标「恐贪指数贪婪」、≥75 才「极度贪婪」；≤25「极度恐惧」、其余「恐惧」。根治「同封脉搏 Greed vs 高危榜极度贪婪」的自相矛盾。
- **P2-1 大额转账去重顺序 bug**：`_dedup_whale_transfers` 由「只认链尾相接（有向贪心）」改为**无向并查集**（共享任一端点 + 金额相近即同链，每簇留最大额）。修复同一多跳链两跳 `block_timestamp` 相同、下游先处理时反向跳合并不上 ⇒ TAO 5Q544→BQ72→J6nzA 被计两笔、总额虚高的问题。
- **P2-2 巨鲸摘要选择性**：prompt 增规则 11（必须同时提增持/减持数量，禁「向优质集中」定性）；user_prompt 巨鲸段补「增仓 N 个 / 减仓 M 个」计数。
- **P2-3 术语注解**：聪明钱标题改「🐋 聪明钱（链上监控地址净买卖）」+ 一行口径；告警质量区置顶「白话结论」（按 severity normal/watch/high 三分支）。
- **P1-2 伪值防漏（顺带）**：`_trade_missing_fields` 新增 `_is_placeholder_value`，把 `N/A`/`无`/`—`/`未知` 等不可判定字面量也算缺失（原 XRP「参照 N/A」会混进交易方向区）。
- **自测**：新增 `workbench/test_daily_brief_20260928.py` **36/36**（伪值判定/六要素、覆盖缺项、顶部注脚三分支、TL;DR 折叠排除、稳定币解读、观察级徽章、渲染集成含赛道涨幅/时效/缺项/`**` 护栏、多跳去重双向/往返/无共享端点、恐贪标签四档、状态表 note）。**回归**：workbench 全量 **50 个 `test_*.py` 全部 exit=0**（含 `test_daily_brief_p0_20260927` 279/0、`test_daily_brief_p1` 22/22、`test_macro_market_*`、`test_highlight_*`、`test_risk_signal_p0r2`、`test_signal_type_calibration`）。`py_compile` 2 文件通过。
  - **顺带修一处时间脆弱测试**：`test_daily_brief_20260924` 的 ETF fixture 写死 `latest_date="2026-09-22"`，当系统日期 >3 天后 W-10 会把整卡降级为「数据不可用」使 4 条 ETF 断言失效（本次运行实测 27/31）⇒ 改为 `date.today().isoformat()`（31/31）。**该失败与本轮改动无关，属测试对系统时钟敏感**。
- **未做 / 边界（须留档）**：① **P1-2（移动止盈无规则）未加模板** —— 由既有 P1-a 六要素闸门（trigger/invalidate/target/horizon/ref_price/ref_as_of）覆盖，`移动止盈` 是 prompt 显式允许的 target 写法；如产品要求页内展示回撤规则需另立项。② **BTC $85K 取整偏高 / 日变不匹配** 属取数源口径（审计判 P2 待核），本轮未改。③ **P0-1 的轻仓 50% 为审计建议默认值**（未回测校准），写在常量 `_TOP_RISK_MIN_POSITION`，可一行调整。④ **P0-2 交易所净流量替代源仍会出现在 AI 结论**（只是加了标注），审计的「是否保留」属产品决策，未做删除。⑤ **大额转账去重仅按 symbol + 共享端点 + 金额 ±2%**：同币同地址对不同金额的多笔不会合并（保守）。
- **待部署**：`send_daily_brief.py` / `macro_market.py` 改的是早报渲染与 AI 提示层，需容器 **redeploy** 后次日 09:00 邮件生效。

### 高亮信号 N4 排序三层收口 N5-1/2/3（复验 `复验_f225ac3_N4排序护栏_2026-09-28.md`，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_f225ac3_N4排序护栏_2026-09-28.md`。复验判 `f225ac3` 本身干净（75/75、无回归），但揭示 **N4 被锁在错误的层** —— 网页高亮榜的最终顺序由 `ai_enrich_signals_v2._sort_key` 决定（排在 `select_highlight_signals` 之后并整体覆盖其顺序），而该键**不含 es** ⇒ 事件强度与卡片顺序在网页端原样失配（实测 PENDLE(es79)→ZEC(es80)→JUP(es81) 升序）。本轮修 **N5-1（P2）+ N5-2/N5-3（P3）**，**零 DDL、零迁移、不改 tier/评分口径**。

- **N5-1（P2，核心）AI 终排末级补 es**：`ai_signal_analyzer.py` 四处排序键全部末级挂 `es = _safe_float(s.get("event_strength")) or 0.0` —— `ai_enrich_signals_v2` 的 long（`(ai_approved, mixed, ai_score, es)`）与 short（`(ai_approved, mixed, base_score, es)`）两支，以及 V1 回退 `ai_enrich_highlight_signals` / `ai_enrich_risk_signals`（`(-downgraded, mixed, …, es)`）。此前只改了池层 `_sort_key` 与邮件 `card_sort_key`，漏了真正决定网页顺序的 AI 层；`es` 只作**末级 tie-break**，不改主序（AI 认可 / 混合分 / 分数优先级不变）。
- **N5-3（P3）池层最终排序显式补 raw→es**：`macro_market.select_highlight_signals` 的 `after_resonance.sort` 由「仅 `conviction_score`」改为 `_final_sort_key = (conviction_score, raw, es)`，与上方候选池 `_sort_key` 的 tie-break 顺序（score→raw→es）一致，**不再依赖 `list.sort` 稳定性的隐式传播**；主卡换源时 `event_strength` 一并跟随（与 `decayed_score` 同处置），避免展示的 es 与主卡分数来自不同子信号。
  - ⚠️ 首版只写 `(conviction_score, es)`，被新增的「raw 优先于 es」断言当场抓到（raw tie-break 被 es 覆盖）⇒ 已改为 `(score, raw, es)`。**该断言有判别力，故能拦住这类回归。**
- **N5-2（P3）护栏去自证**：`test_highlight_audit_20260924.py` 的「分数优先于 es」用例原先给高分卡**同时**高 es ⇒ 正确/错误实现给出同一顺序（无判别力，只靠源码文本守卫兜住，即复验 M-G/M-B 的成因）。改为**高分低 es vs 低分高 es**（错误实现会把低分卡越级，必红）；并新增「raw 优先于 es」判别用例（`raw_before_decay=90,es=10` vs `raw=None,es=90`）；另加 N5-1 四条 AI 层源码守卫 + 一条行为断言（用非符号 target 走 skipped 分支、monkeypatch `load_ai_signal_rules`/`analyze_asset_v2` ⇒ **不连库不连网**，同分同 AI 仅 es 不同 ⇒ 只有末级 es 能分胜负）。
- **验证**：`test_highlight_audit_20260924.py` **75 → 80/0**；回归 `test_highlight_alert`(68/0)、`test_highlight_determinacy_20260926`(72/0)、`test_macro_market_p0`(16/0)、`test_macro_market_board_tier2`(36/0)、`test_macro_market_p1_upstream`(24/0)、`test_risk_signal_p0r2_20260927`(14/0)、`test_signal_type_calibration_20260926`(99/0)、`test_major_event_alert`(55/0) 全绿；workbench 全量 **51 套件**仅 `test_scan_alert_header_regime.py`(74/9) 红，且该套件**只 import 并发会话正在改的 `scan_daemon.py` / `build_scan_edge_report.py`（未提交 WIP），不 import 本轮任何文件** ⇒ 与本次改动无关。`py_compile` 3/3 通过。
- **待部署**：`macro_market.py` / `ai_signal_analyzer.py` 在 web 应用进程内，需 Zeabur redeploy 后网页高亮榜才生效；**邮件层 N4 的行为证据最早要到 2026-09-29 08:34 CST 之后那封**（高亮邮件读的快照每日 08:34 才写一次，今日那份早于上线）。
- **未做 / 边界**：① **N5-4（两个早报红套件治理）按复验建议单独出单**，本轮未做；② 复验的验收 #1（线上顺序用 `(ai_approved, mixed, ai_score, es)` 逐项重算相等）需 redeploy 后执行；③ 上一轮遗留 M2（HIGH 泛滥 / conv 扎堆）/ N2-b / decision 路径间歇失败根因**本轮未触碰**。

### N5 承重加固 N6-1/2/3（复验 `复验_79d3d2e_N5排序三层收口_2026-09-28.md`，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_79d3d2e_N5排序三层收口_2026-09-28.md`。复验确认 `79d3d2e` 代码正确、范围干净、测试 80/0 精确复现、**N5-1 已进容器**（容器重建 11:59:11 CST，推送后 8m05s），但 15 组突变体中 **4 组不承重**（M-F/M-L/M-I）。本轮**只补护栏（纯测试）**，不动生产代码。**零 DDL、零迁移、无生产行为变更**。

- **N6-1（P3）终排 es 判别例**：终排 `_final_sort_key` 的 es 前面还压着 tier/is_new/共振维数（**候选池** key），它们能把「低 es 卡」先顶上去，只有终排的 es 能翻回。构造「共振维数与 es 方向相反」的两张同分卡（`板块M` 共振 2 维/es10 vs `板块N` 共振 1 维/es90）⇒ 正确实现 `[N, M]`，去掉终排 es 则 `[M, N]`。**实测 M-F（终排去 es）、M-L（终排读错字段）双双由 rc=0 转 rc=1。**
- **N6-2（P3）AI 终排 es 层级判别例**：此前 `es` 的位置只被源码文本守卫兜住（M-B/M-M/M-J 等）。补三条**离线行为断言**（stub `load_ai_signal_rules` / `analyze_asset_v2` / `_analyze_merged_signal`，非符号 target 走 skipped 分支 ⇒ **不连库不连网**），构造「mixed 并列、ai_score（或 base_score）与 es 方向相反」的并列组：
  - v2 long：`(ai80,es10,base40)/(ai60,es90,base60)` ⇒ mixed 并列 60，正确 `[O,P]`；es 提前则 `[P,O]`。
  - v2 short：`(ai90,es10,base60)/(ai60,es90,base30)` ⇒ mixed 并列 35，正确 `[U,V]`。
  - V1 高亮：`(ai90,es10,base60)/(ai60,es90,base80)` ⇒ mixed 并列 72，正确 `[Q,R]`；V1 高危：`(ai90,es10,base60)/(ai60,es90,base40)` ⇒ mixed 并列 40，正确 `[S,T]`。
- **N6-3（P3）主卡换源 es 跟随**：同一 target 两条子信号，`p1(分60/共振2维/es30)` 先被候选池（共振维数）顶到前面成为主卡，`p2(分80/共振1维/es95)` 因分更高触发换源 ⇒ 合并卡 `event_strength` 须为 95。**实测 M-I（换源 es 不跟随）由 rc=0 转 rc=1。**
- **验证**：`test_highlight_audit_20260924.py` **80 → 86/0**；**9 组突变体全部 rc=1**（M-F/M-L/M-I 以及 M-B/M-M/M-D/M-E/M-H/M-J，含原「仅源码守卫」者）；恢复校验 86/0。既有回归（`test_highlight_alert` 68/0、`test_highlight_determinacy_20260926` 72/0、`test_macro_market_*`、`test_risk_signal_p0r2` 14/0、`test_signal_type_calibration` 99/0、`test_major_event_alert` 55/0）全绿；`py_compile` 通过。
- **未做 / 边界（须留档）**：① **N6-4（线上行为态取证）为观测项，不改码** —— 需一份含「判别性并列组」（前 N-1 分量完全相同且上游序与 es 序相反）的线上快照才能补 N5-1 的行为证据，当前样本不具判别力（复验 §4.2）；② 复验 §二末段登记的 **`decayed_score`（候选池主序）vs `conviction_score`（终排主序）口径分裂**属既有设计，**本轮未动**（改主序口径是行为变更，需产品拍板）；③ `select_risk_signals` 池层未补 es（其终排走 AI short 分支，行为已由 N5-1 覆盖）。

### N7-1 删除型承重加固（复验 `复验_814ad8d_N6承重加固_2026-09-28.md`，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_814ad8d_N6承重加固_2026-09-28.md`。复验确认 N6-1/2/3 三条加固**全部真承重**（12 组突变体全 rc=1，上轮不承重的 M-F/M-L/M-I 全数转红）、86/0 精确复现、`814ad8d` 已部署（容器重建 13:33:53/54 CST，滞后 5m18s），并独立核对了 N6-2 四条断言的 mixed 并列公式与源码同源、离线不连库不连网。唯一新发现 **N7-1（P4）**：删除型突变在 v2-short / V1-高亮 / V1-高危三条支路上**只有源码文本守卫**（M10/M11/M12），无行为断言响应；本轮**只补护栏（纯测试）**，不动生产代码。**零 DDL、零迁移、无生产行为变更**。

- **N7-1（P4，已修）三条删除型行为断言**：`test_highlight_audit_20260924.py` 新增 ——
  - v2 高危支：`(ai70,base60,es10)` vs `(ai70,base60,es90)` ⇒ mixed 并列 45、base_score 并列 60，**仅 es 不同**；正确 `[Y,W]`，删掉 es 分量即回落输入的稳定序 `[W,Y]`。
  - V1 高亮：`(ai70,base60,es10)` vs `(ai70,base60,es90)` ⇒ mixed 并列 64、ai_score 并列，仅 es 不同。
  - V1 高危：`(ai70,base60,es10)` vs `(ai70,base60,es90)` ⇒ mixed 并列 48、base_score 并列，仅 es 不同。
  - v2 long 的删除型已由既有 N5-1 `_ai_tie` 行为断言覆盖（M9）。
- **验证（本轮实测）**：`test_highlight_audit_20260924.py` **86 → 89/0**；**删除型承重的独立见证** —— 把三处 `_sort_key` 的 `es = _safe_float(...) or 0.0` 全部改为 `es = 0.0`（**return 元组不动 ⇒ 源码文本守卫保持全绿**），跑出 **85/4（rc=1）**，4 条失败正是 `_ai_tie`（long）+ 三条 N7-1 删除型断言 ⇒ **证明它们是行为承重，而非只靠字符串守卫**；恢复后 89/0。回归（`test_highlight_alert` 68/0、`test_highlight_determinacy_20260926` 72/0、`test_macro_market_*`、`test_risk_signal_p0r2` 14/0、`test_signal_type_calibration` 99/0、`test_major_event_alert` 55/0）全绿；`py_compile` 通过。
- **未做 / 边界（须留档）**：① **N6-4（线上行为态取证）仍为观测项**（需「前 N-1 分量全等且上游序与 es 序相反」的快照）；② 复验 §六 登记的 `decayed_score` vs `conviction_score` 主序口径分裂、`select_risk_signals` 池层未补 es、两个历史红套件（`test_daily_brief_p0_20260927` 出生即红 263/16；`test_daily_brief_20260924`）均**不在本轮范围**。

### 告警邮件「开仓依据」复验 N-8702-A~H 处置（复验_告警邮件开仓依据_8702607_2026-09-28，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_告警邮件开仓依据_8702607_2026-09-28.md`。复验确认 `8702607`「全项到位、测试计数逐项复现、真库重渲染全部兑现」，另开 **8 条新缺陷（N-8702-A~H）+ 3 项自述纠正**。本轮按报告 §十 待拍板建议**全部落地**（A~H + 自述②③），**零删除、未改 tier/评分口径、未改任何阈值**。改动面：`scan_daemon.py` / `build_scan_edge_report.py` / 新迁移 `fix_079` / 探针。

- **N-8702-A（P2，已修）桶 `n` 是 1h 口径、而 `sl_rate/ret_p75/mfe_p75` 在 24h 子集**：邮件把 28 行的分位印成「同档 n=57」。新增 `biz.scan_edge_bucket.n_24h`（迁移 `fix_079`，幂等 `ADD COLUMN IF NOT EXISTS`）；`_mk_bucket` 产出 `n_24h=a24["n"]`；`save()` 写入。渲染侧 `_build_reason` 新增 `_n24(r)`（24h 真分母，缺值回退 1h `n`），`_row` 门槛对 **24h 单独生效**（防「1h n=10 过关、24h 只看 4 行」）、`_stat(r)` 打印 `_n24`。**已对 prod 执行 fix_079 + 重跑 `build_scan_edge_report --date 2026-09-27` 回填**（实测 `funding_sign/>0` n=57/n_24h=29、`vol_ratio/>6` n=10/n_24h=4 ⇒ 该档现判「样本不足」）。
- **N-8702-B（P2，已修）同封「窗口」双口径**：槽⑤ 原用当日 `win_1h/be_1h`、批级摘要用 `roll3_*` ⇒「批级窗口为负」与「批级环境为正」并存。槽⑤ 改用 `roll3_*`（缺则回退当日并标口径「近 3 日滚动 / YYYY-MM-DD 当日」）。
- **N-8702-C（P2，已修）判读只设顺风下限、无逆风上限**：判据由 `goods>=2 and rr>=RR` 改为 **`goods>=2 and bads<=1 and rr>=RR`**；槽③④ 统一补 `_stat`（24h 分母/均值/止损率）。**物证级效果**：复验原例 INXUSDT 由【可开】（bads=3）改判【观望】；且该档 `vol_ratio/<2.5` 现显式披露「止损率 57.1%」。
- **N-8702-D（P3，已修）混合方向批丢失「环境受限」**：`_batch_direction_line` 混合分支原直接 return；现逐方向补「多头受限：…/空头受限：…」，双方均顺风时显式「环境 ✓ 双方均未受限」。
- **N-8702-E（P3，已修）依据段静默降级 + 图例撒谎（= 自述②）**：`_load_reason_context` 异常/无日报时写 `REASON_HEARTBEAT_TASK='scan_alert_reason'` 心跳（`last_error` 记原因；不在 `STALL_HEARTBEAT_TASKS`）；图例拆为「核心段（恒在）+ 本批方向锚点（`dir_line` 有则加）+ 依据段锚点（`has_reason` 有则加）」，**降级时不再宣称有依据段**、2 参旧调用方亦不再凭空多 463 字符。
- **N-8702-F（P3，已修）暴增警示的「当日」= 日报日**：文案改「⚠️ 告警量暴增（日报日 YYYY-MM-DD）：…」；倍数改走 `_reason_mod().ALERT_SURGE_X` 真源（自述③，`REASON_ALERT_SURGE_X` 降为模块不可导入时的回退）。
- **N-8702-G（P4，已修）写侧/读侧降级不对称**：`save()` 新增 `_existing_bucket_cols()` 预检，缺 `sl_rate/ret_p75/mfe_p75/n_24h` 任一 ⇒ 降级为「不带该列写」并显式告警（原为 `UndefinedColumn` 直接崩）。
- **N-8702-H（P4，已修）「本批共性逆风」实为主池**：文案改「主池共性逆风」「主池 N 币量比落最高档」。
- **自述①已闭环**：`fix_078` 确已执行（复验实测），本轮追加 `fix_079`（已 apply prod）。
- **探针**：`test_scan_alert_header_regime.py` **183 → 198/0**（新增 N-8702-A/C/D/E/F 用例 + `_mk_bucket` 的 `n_24h` + `save()` 缺列降级假连接用例 + 源码级心跳/真源守卫）。回归全绿：`test_scan_edge_metrics`(71/0)、`test_scan_alert_onchain_addr`(36/0)、`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_remaining`(29/0)、`test_scan_scenario_label`(48/0)、`test_scan_alert_audit_20260926`(96/0)、`test_funding_interval_20260927`(35/0)、`test_scan_l1_closed_bar`(16/0)、`test_derivatives_signal_gap`(43/0)、`test_squeeze_battle`(143/143)、`test_squeeze_fuel`(99/99)、`test_squeeze_alert_silence`(18/18)、`test_liq_history_scope`(39/0)；`py_compile` 2/2。
- **prod 只读端到端复验**：`_load_reason_context` + `_build_reason` 真库实跑，INXUSDT 姿态渲染出「同档 n=23 / n=29」「量比 <2.5 档 · n=7 · 止损率 57.1%」「窗口（批级 近 3 日滚动）」「3 顺 2 逆 ⇒ 观望」；心跳 `scan_alert_reason` 写入 `last_ok_at`、`last_error=NULL`。
- **待部署**：`scan_daemon.py` 需重启容器后生效（`build_scan_edge_report.py` 为调度脚本，下次调度即用新码）。
- **未做 / 边界**：① **N-8702-A 的 B 方案（跨日滚动窗口，避开当日结算滞后）不在本单**，按待拍板列入下个工单；② 2 参旧调用方「行为逐字不变」的旧自述**已被本轮修正**（图例不再无条件输出新锚点），对应断言已覆盖；③ 复验的「真实邮件观感」需等下一批告警邮件（部署后）。

### 变化榜 R3 决策落地 + D3 信号联动 + D4 质量收口（2026-09-28，本次提交）

来源：`决策_R3产出策略_2026-09-28.md` + `工单_变化榜D3信号联动+D4质量收口_2026-09-28.md`。**零 DDL、零迁移**；R3 三项维持现状（不改判定策略），D3 按工单建议档实施，D4 三条经 live 数据核实不成立。

- **R3（产出策略）拍板 = 维持现状，但退役过期提醒**：R3-1 `volume_surge_24h` **不加回** Tier2 白名单；R3-2 不新增总开关（复用 `diff_streak_threshold: 0` 入口开关）；R3-3 阈值维持 3。**代码动作**：`scheduler.py` 删除一次性「国庆后待办·评估 volume_surge 加回」提醒 job（原 `0 9 6 10 *`，会在 2026-10-06 发一封「请拍板」邮件，现决策已下，留着即误导），并删除 `scripts/bin/remind_tier2_revisit.py`；原地留注释登记决策出处。
- **D3（信号联动字段加权，`macro_market.derive_board_opportunities`）**：`conviction_score = base(52+(sd-3)*6) + mcap_tier 加成 + vol_mcap_ratio 分位加成（仅 pvs）+ composite_score clamp 加成（仅 sector_rotation）`，**总 cap 80 不变、不改是否派生**。
  - 常量：`_MCAP_TIER_BONUS={top10:6,top100:4,top500:2,top1000:0}`、`_VOL_MCAP_HIGH_BONUS=6 / _VOL_MCAP_MID_BONUS=3`（批次分位 top25%/前50%）、`_VOL_MCAP_MIN_BATCH=4`（小批次不分位）、`_SECTOR_BONUS_CAP=8`（`clamp(round((cs-50)/5),0,8)`）。
  - 口径取舍（工单 D3-1~D3-3 的建议档）：D3-1 启用 mcap_tier 加成；**D3-2 `primary_sector` 不加权**（缺赛道热度基准，易主观）；D3-3 启用 vol 分位 + sector clamp。
  - 加成分量落 `board_score_bonus={mcap_tier,vol_mcap,sector}` 供溯源；缺 tier / 缺 detail / 未知值一律 0（保守，不改基准分）。
  - 数据来源已核实（非臆测）：`db_stats.get_daily_diff_summary` 的 item 顶层带 `mcap_tier`，`detail` 带 `vol_mcap_ratio`（pvs）/ `composite_score`（sector_rotation，`daily_diff_generator` L684-705 落库）。
- **D4（数据质量三条）经 live 09-27 数据核实，均为单日假象 / 已被信号侧规避，不改码**：
  - D4-1 `price_change_24h` rank 基准：09-23 曾现 down 从 721 起；live 09-27 实测 up rank 1-37、down rank 1-23（**均从 1 起**）⇒ 跨方向不可比问题不复现。
  - D4-2 `volume_surge_24h` down 语义：live down 值域 [-98.79,-57.81]（缩量），语义冲突仍在；但 `_BOARD_DIRECTIONAL` 已排除该榜双向（信号侧不消费），展示层是否改标「缩量」属可选微调，按工单**先不管**。
  - D4-3 `market_cap_mover` down 缺 1~4：live 09-27 down n=5、rank 1-5（**无缺口**）⇒ 观察不复现，非预期缺陷。
- **自测**：`test_macro_market_board_tier2.py` **36 → 51/0**（新增 G 组 15 条：G1 mcap_tier ±6、G2 vol 分位 +6/+3/0、G3 sector clamp 0/4/8、G4 总 cap 80、G5 缺字段保守 base、G6 未知 tier 0、G7 常量值域 + 小批次不分位、G8 缺值不抛异常/派生数不变）。**回归**：workbench 全量 **52 个 `test_*.py` 全部 exit=0**；`py_compile` 通过；`scheduler.py --list` 确认提醒 job 已移除。
- **runtime 只读复验（R1 待办）**：`GET /api/daily-diff`（2026-09-27）⇒ ① `streak_start_ambiguous` 字段在响应中出现 **235 次**（接口已透出）；② `volume_surge_24h/up` 40 条中 `streak_start_ambiguous=True` 的 4 条（KII/SXT/ME/ACT，🔥20 天）——**不再是「11 条同值 🔥16 天」**，且前端连板榜 `if (item.streak_start_ambiguous) return` 将其排除。③ `/api/market/overview` 本轮多次超时未取到（重端点 + 沙箱网络），`diff_streak_up` 信号数未在本次核到。
- **待部署**：`macro_market.py`（D3）/ `scheduler.py`（R3 提醒退役）需容器 **redeploy** 后生效。

### 告警邮件开仓依据 N-0928 系列处置（审计_告警邮件_6413c1f上线首封_2026-09-28，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\审计_告警邮件_6413c1f上线首封_2026-09-28.md`。该审计**铁证**了 `6413c1f` 已上线（真库批喂真码重渲染 9787 字节、去时间戳逐字节相等），N-8702-A~H 线上全部生效；另开 **N-0928-1~7**。本轮按 §七 待拍板建议全部落地（1~6），**零 DDL、未改阈值、未改 tier/评分口径**（仅一处新增 `SUMMARY_DEDUP_*` 去重参数）。改动面：`scan_daemon.py` / 探针（+ `AGENTS.md`）。

- **N-0928-1（P2）5 槽中 4 槽跨币共享 + ③④ 同维双计**：判读 `goods/bads` 改按**币级独立维度**计数（`_add` 带 `grp`/`coin`；①②③④ 为币级、③④ 同属量比档合并为 1 维、⑤ 批级）；某维度同时顺/逆 ⇒ 记逆（保守）。槽③ 文案补「（档位经验值·跨币共享）」。
- **N-0928-2（P2）RR 是 mfe 乐观上界**：判「可开」新增必要条件 `ret_p75 > 0`；槽④ 文案标「为乐观上界」；图例同步。**⚠️ 归因更正（复验 N-11A4-D）**：MARSCOIN 的档位变化**不是**本门造成的 —— 旧码本就是 `watch`、新码是 `avoid`，变化来自 **N-0928-1 的维度合并**（goods 1→0 ⇒ 触发 `goods==0` 兜底）；其 RR 一直 0.29 从未满足 `RR≥1.5`，`ret_p75` 门对它**零作用**。`ret_p75>0` 门的真实效果是：量比档 P75 为负/为 0 时即使 RR≥1.5 也不判可开（探针覆盖）。
- **N-0928-3（P3）催化剂跨语言转载未合并**：新增摘要骨架去重 `_norm_summary()`（小写 + 仅字母/数字/汉字）+ `_is_similar_summary(key, day, seen)`（`difflib` 相似度 ≥ `SUMMARY_DEDUP_RATIO=0.9` **且** 同 UTC 日，长度差限制，短摘要 <20 不进层），在 `_get_resonance` 标题去重之后二次合并。**prod 只读实测**：MARSCOINUSDT `catalyst_dir` `{bearish:2,bullish:1}` → `{bearish:1,bullish:1}`（净空 1 → 净 0），两条近乎逐字相同的中/英利空合并为 1。
- **N-0928-4（P3）槽⑤ 括注时间口径混搭**：1h 用 `roll3_*`、`24h 均值` 是单日 ⇒ 文案改「24h 均值（日报日 {report_date}）」。
- **N-0928-5（P3）判读混加批级共享项**：批级窗口（⑤）退出 `goods/bads`，`_render_reason` 单列「—— 以下为批级（全批共享，不计入上列顺/逆风计数）——」；rr<1 分支文案由「【观望·不建议新开】」改「【观望】」与摘要用词统一。
- **N-0928-6（P4）图例多报「⚠️ 告警量暴增」**：`_render_batch_summary(items, batch, out=None)` 回填 `out["surge_shown"]`，图例 `legend_surge` 仅在 `surge_shown` 时挂。
- **N-0928-7（P4）图例「共振方向 ×1.15/×0.75」未标新鲜**：改「新鲜共振方向…」。
- **探针**：`test_scan_alert_header_regime.py` **198 → 210/0**（新增 N-0928-1/2/4/5 槽位与计数、N-0928-3 摘要去重 4 例含跨日/低相似/过短、N-0928-6 图例暴增守卫、N-0928-7 图例新鲜）。回归全绿：`test_scan_edge_metrics`(71/0)、`test_scan_alert_onchain_addr`(36/0)、`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_remaining`(29/0)、`test_scan_scenario_label`(48/0)、`test_scan_alert_audit_20260926`(96/0)、`test_funding_interval_20260927`(35/0)、`test_scan_l1_closed_bar`(16/0)、`test_derivatives_signal_gap`(43/0)、`test_squeeze_battle`(143/143)、`test_squeeze_fuel`(99/99)、`test_squeeze_alert_silence`(18/18)、`test_liq_history_scope`(39/0)；`py_compile` 通过。
- **prod 只读端到端复验**：MARSCOINUSDT 真库 `_get_resonance` + `_build_reason` 渲染——`catalyst_dir {bearish:1,bullish:1}`、判读「3 维逆风、无顺风项 ⇒ 【不建议新开】」（量比维只计 1）、批级窗口单列、`ret_p75=-2.44%` 使 RR 0.29 不判可开。
- **待部署**：`scan_daemon.py` 需重启容器后生效。
- **未做 / 边界**：① 报告 §七#7「长窗统计选日规则（`T-1 且 24h 覆盖 ≥90%`）」按建议**单开工单**，本轮不做；② N-0928-1 的「重」方案（④ 目标位改本币 2×ATR）按待拍板取「最省」方案，未做；③ 摘要去重阈值 0.9 为经验值（未回测），有 `SUMMARY_DEDUP_MIN_LEN`/`RATIO` 两个常量可调。

### 告警邮件 N-0928 系列复验 N-11A4 处置（复验_告警邮件N-0928系列_11a49b1_2026-09-28，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_告警邮件N-0928系列_11a49b1_2026-09-28.md`。复验确认 `11a49b1` 代码质量高、口径净化到位、8 项中 6 项实打实，但指出 **2 条承重逃逸（M4/M8）+ 1 条自述归因错误 + 若干披露/守卫遗漏**，且指出本轮对「敢开仓」产品目标是负向的（可开 5→5 不变、不建议 0→10）。本轮按 §六 待拍板 P1~P5 全部落地（+ N-11A4-H 标注）。**零 DDL、未改阈值/tier 口径**。改动面：`scan_daemon.py` / 探针 / `AGENTS.md`。

- **N-11A4-A（P2，已修）N-0928-3 摘要去重**接线**无断言承重**（把 `_is_similar_summary(...)` 换成 `if False` 时 210/0 仍全绿）⇒ 新增**行为级接线断言**：假 `conn` 喂两条「同币同日、标题中/英不同、`ai_summary` 近乎逐字相同」的催化剂行，断言 `catalyst_raw==2` 且 `catalyst_dir["bearish"]==1`。**已用猴子补丁证承重**：正常 bearish=1 / 去重关闭 bearish=2。
- **N-11A4-B（P3，已修）批级单列分隔行无断言**（删除 `—— 以下为批级（全批共享，不计入上列顺/逆风计数）——` 仍 210/0）⇒ 补字面断言。
- **N-11A4-C（P2，已修）判读兜底分支不披露未达条件**（直击「不敢开仓」病灶）：原 `else` 只印「N 顺 M 逆 ⇒ 【观望】」，出现「全屏无 ✗ 却拒绝」（量比档样本不足 / P75=0）⇒ 现显式列未达项「（可开未达：RR …；该档 24h P75 …；顺风维数 …；逆风维数 …）」。三种形态（P75<0 / P75=0 / 样本不足）均有断言。
- **N-11A4-D（P3，已修）自述归因错误**：见上方 N-0928-2 条的 ⚠️ 更正。
- **N-11A4-E（P3，已修）批级摘要未披露新门**：head 补「且该档 24h P75 > 0」，与图例同口径。
- **N-11A4-F（P3，已修）图例「📊 本批可开性」锚点未守卫**（N-0928-6 遗漏的另一半）：`_render_batch_summary` 回填 `out["summary_head_shown"]`（`main` 非空时为 True），图例 `legend_summary` 据此挂 —— 整批全 BRK 时正文无该行、图例不再多报。
- **N-11A4-H（P4，已修）④ 槽未标跨币共享**：槽④ 补「档位经验值·跨币共享」（与 ③ 一致，RR 同样取自跨币共享的量比桶）。
- **探针**：`test_scan_alert_header_regime.py` **210 → 219/0**（N-11A4-A 接线行为 1 + C 三形态 4 + B 分隔行 1 + F 全 BRK 3）。回归全绿：`test_scan_edge_metrics`(71/0)、`test_scan_alert_onchain_addr`(36/0)、`test_scan_alert_audit_deepdive`(75/0)、`test_scan_alert_remaining`(29/0)、`test_scan_scenario_label`(48/0)、`test_scan_alert_audit_20260926`(96/0)、`test_funding_interval_20260927`(35/0)、`test_scan_l1_closed_bar`(16/0)、`test_derivatives_signal_gap`(43/0)、`test_squeeze_battle`(143/143)、`test_squeeze_fuel`(99/99)、`test_squeeze_alert_silence`(18/18)、`test_liq_history_scope`(39/0)；`py_compile` 通过。
- **prod 只读端到端复验**：MARSCOIN 真库真函数渲染，7 个字面锚点除「可开未达」（MARSCOIN 走 `goods==0` 的 avoid 分支，正确不出现）外全部命中。
- **去重副作用登记（复验 §四）**：N-0928-3 去重直接改 `catalyst_dir` ⇒ ① 连带 `catalyst_stale` 同步下降；② `_alert_strength` 的**新鲜方向修正 ×1.15/×0.75** 取值跟随变化 ⇒ **强度条分数与排序会变**（近 7 天 344 资产中 9.6% 的方向构成变化，TOP：ZEC bearish 4→2、NEAR 3→1）；③ 催化剂明细少一条。
- **待部署**：`scan_daemon.py` 需重启容器后生效（复验实测进程已按上轮推送重启；本轮的产物锚点须下一封邮件确认）。
- **未做 / 边界**：① **N-11A4-G（可开信号数 0 增长）属产品决策**（当前门槛在真实数据上几乎判不了「可开」），按待拍板 P6 留主人定调，本轮未动；② N-11A4-I（当日新增新闻 `ai_summary` 填充仅 65.8% ⇒ 去重对最新一批覆盖不足）属上游 AI 处理时效，另议；③ N-11A4-J（快照非可靠版本指纹）已记档；④ 上轮报告遗留 7 项（长窗选日规则工单等）仍未处置。

### 告警邮件 N-11A4 复验 + 部署首封审计后续处置（复验_告警邮件N-11A4系列_7b4f3e2 / 审计_告警邮件_7b4f3e2部署首封_2026-09-28，2026-09-28，本次提交）

来源：`复验_告警邮件N-11A4系列_7b4f3e2_2026-09-28.md` + `审计_告警邮件_7b4f3e2部署首封_2026-09-28.md`。前者确认 `7b4f3e2` 代码全对、上轮两条逃逸（M4/M8）已真承重、纯增量零副作用，但指出 **3 组突变体逃逸（C3/C4/C5）** 与 **N-11A4-L/M** 两条口径问题；后者以**逐字节重渲染铁证**确认 `7b4f3e2` 已上线（6043==6043），并开 **N-EML-1/2/3**。本轮全部处置（**零 DDL、未改阈值/tier 口径**）。改动面：`scan_daemon.py` / 2 探针 / `AGENTS.md`。

- **N-11A4-K（P3，已修）兜底披露 3 个主力子项无断言**：真库 23 条新增披露绝大多数是「`RR x < 1.5` / `顺风维数 N < 2` / `逆风维数 N > 1`」，而测试只锁了 P75 两项 ⇒ 新增 2 个合成用例（1 顺 2 逆 rr=1.38 / 单顺风其余 info），断言显式出现这三个字面串（逐个删除对应 `unmet.append` 必红）。
- **N-11A4-L（P4，已修）avoid 分支「0 维逆风」自相矛盾**：四槽全 info（无价方向 + 费率未覆盖 + 量比档样本不足）时原印「0 维逆风、无顺风项 ⇒ 【不建议新开】」⇒ 改 `goods==0 and bads==0` 单列支「无可用维度（数据缺失）⇒ 【观望】」。**⚠️ 频率口径更正（复验 N-A6-A）**：原自述「真库近 7 天 0 命中，理论边界」**字面不实** —— 全库近 7 天 `alerted_at` 非空 517 条中有 **7 条**命中该支（均 `confidence=low` 的 `squeeze/SQZ_CHURN`）。但这 7 条走独立渲染器 `_render_squeeze_alert`（且 `SQUEEZE_ALERT_SHADOW` 不发信），而「开仓依据」**只在主池告警路径渲染**（`_build_reason` 唯一调用点在 `task_scan_alert`，候选硬过滤 `confidence='high'`）⇒ **在「会渲染开仓依据的 high 置信主池告警」中确为 0 命中**。代码行为正确（更诚实），仅措辞须带限定词（代码注释已同步更正）。
- **N-11A4-M（P4，已修）`rr<1` 单列支不披露 P75/维数**：删除该支并入兜底，统一由 `unmet` 列表披露 ⇒ 真库 LYNUSDT 由「1 顺 2 逆，但 RR 0.29 < 1 ⇒ 【观望】」升级为「1 顺 2 逆 ⇒ 【观望】（可开未达：RR 0.29 < 1.5；该档 24h 收益 P75 -2.44% ≤ 0；顺风维数 1 < 2；逆风维数 2 > 1）」。
- **N-EML-1（P3，已修）同桶两个「P75」标签歧义**：③ 的 `ret_p75` 标签补「收益」限定 ⇒ ③ 印「该档 24h **收益** P75」、head/图例同步「该档 24h 收益 P75 > 0」，与 ④ 的「最大有利偏移 P75」区分（复验实测近 7 天 36.8% 命中）。
- **N-EML-2（P3，已修）图例缺批级行锚点**：`_render_batch_summary` 回填 `out["batch_rows_shown"]`，新增 `legend_batch`（「当前批级环境」/「边缘桶」/「近 3 日滚动」/「盈亏平衡」），`legend_summary` 补「主池共性逆风」。
- **N-EML-3（P3，已修）图例「历史同场景」vs 卡片「历史同象限」术语断层**：图例统一为「历史同象限（价方向 × OI 方向）」「按象限跨币种聚合」。
- **探针**：`test_scan_alert_header_regime.py` **219 → 231/0**（K 两例 + L + M + EML-2/3）；`test_scan_alert_audit_deepdive.py` 同步「按象限跨币种聚合」（74→75/0 不回归）。全量 14 套相关回归 rc=0。
- **prod 只读端到端复验**：LYNUSDT 姿态渲染——③「该档 24h 收益 P75 -2.44%」、判读四项未达全披露、图例 8 项新锚点全命中且无「历史同场景」。
- **部署已坐实（复验/审计）**：`7b4f3e2` 经真库重渲染**逐字节相等**（6043==6043，父版本重渲染仅差 3 项本轮改动 ⇒ 双向闭环）；本提交需再次重启容器生效。
- **N-11A4-G（产品决策，已定调「维持门槛」）**：`pass` 仍 5/57 = 8.8%（ARXUSDT/AZTECUSDT/METUSDT/XAIUSDT/GRASSUSDT），本封 0/1。**决定：不放宽门槛**——`RR≥1.5` / `P75>0` 均为经验值、**未做样本外校准**，而当前仅 5 天/单一 regime 样本，按它调门槛即数据窥探（违反待办 A1/A3「勿用当前 3~6 天样本调参」）。「可开」稀少是该门槛的**诚实体现**，非缺陷。落地仅做**显式披露**：图例 `legend_reason` 补「⚠️ 该判读门槛（RR / P75 / 维数）均为经验值、未做样本外校准，「可开」稀少属门槛偏严的诚实体现，勿据此加杠杆」。**放宽的三个候选（允许 P75≥0 / 降 RR / 增设「接近可开」档）一律不做**，须待样本外校准（A3 holdout）后再议。
- **未做 / 边界**：① N-11A4-I（当日新闻 `ai_summary` 填充不足 ⇒ 去重对最新批覆盖不全）/ J（快照非可靠版本指纹）记档；② 上轮遗留 7 项（长窗选日规则工单等）未处置。

### 告警邮件 N-A6 系列收口（复验_告警邮件N-11A4-KLM_EML系列_be312e8_a6a4e1a_2026-09-28，2026-09-28，本次提交）

来源：`复验_告警邮件N-11A4-KLM_EML系列_be312e8_a6a4e1a_2026-09-28.md`。复验确认 K/L/M + EML-1/2/3 + G **7 项全部正确落地**、C3/C4/C5 三条历史逃逸**真堵死**（15 组突变体 13 杀 / 2 逃）、真库 517 条 AB 数值零影响（仅 7 条 `avoid→watch`）、`7b4f3e2` 重渲染 LYNUSDT **6031==6031 逐字节**（上一封邮件产物确认）。两处新问题已处置：

- **N-A6-A（P3，已修）自述频率字面不实**：原「真库近 7 天 0 命中」⇒ 加限定词「**在会渲染开仓依据的 high 置信主池告警中 0 命中；全库近 7 天有 7 例（均 low 置信 squeeze）**」（代码注释 + 上方 N-11A4-L 条已同步更正）。
- **N-A6-B（P4，已修）`batch_rows_shown` 守卫无负向断言**（突变体 `if True:` / 赋值提前 均逃逸）⇒ 新增负向用例：构造「ctx.daily 缺 `roll3_*`/`win_1h` + 全 BRK」使批级行缺席，断言 `_render_batch_summary` 返回 `""` 且 `out["batch_rows_shown"] is False`、图例**不含**「当前批级环境/边缘桶/主池共性逆风」但**仍含**「🎯 开仓依据」。
- **口径记档（复验 §2.4 附注）**：上轮 AB 自述样本「近 7 天 59 条」与全库 500/517 条口径不一致，其子集规则未记录 ⇒ 后续 AB 一律以「全量 + 明示过滤条件」为口径（本轮用全库 517 条）。**记档：59 条为未记录子集，勿再引用。**
- **探针**：`test_scan_alert_header_regime.py` **232 → 235/0**。全量 14 套相关回归 rc=0；`py_compile` 通过。
- **未做 / 边界**：N-11A4-G 维持门槛（复验认同）；部署逐字闭环待下一封邮件（4 字面指纹：`该档 24h 收益 P75` / `「历史同象限」` / `该判读门槛…未做样本外校准` / `「当前批级环境」= 近 3 日滚动（缺则当日）的 1h 胜率 vs 盈亏平衡`）。

### L0 告警邮件融合变化榜（工单_L0告警邮件融合变化榜_2026-09-29，2026-09-29，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\工单_L0告警邮件融合变化榜_2026-09-29.md`。决策：变化榜已接网页端/信号层高亮/早报，唯独告警邮件零引用；本轮做 **L0（纯文案上下文）** —— 渲染时标注同源变化榜上榜信息，**不改判读、不抬 conviction、不绕门槛**（守 N-11A4-G 定调）。**零 DDL、未改阈值/tier 口径**。

- **改动 1（`scan_daemon.py` 顶部）**：新增 workbench 路径候选探测（`<root>/workbench` → `/app` → `<root>`，防容器扁平拷贝坑），并**裸** `import db_stats`（`try/except` 降级，导入失败不阻断守护进程）。
- **改动 2/3（`task_scan_alert`）**：复用 `_get_asset_id` 避免重复查；预查 `db_stats.get_daily_diff_summary()`（自开连接池、不接 conn），经纯函数 `_diff_board_map()` 建 `asset_id → [上榜记录]` 反查 map，给每个 `it` 挂 `asset_id`/`diff_boards`/`diff_date`。
- **改动 4（`_render_alert_email`）**：新增纯函数 `_render_diff_boards(it)`，在卡片方向行之后独立一行渲染 `📌 同源变化榜：🔥 连续N天登涨幅榜 TopR（最近可用变化榜 YYYY-MM-DD）`。Q1 连板 ≥3 用 🔥（与 U-A 口径统一）；Q2 独立一行不混入 reason 判读段；Q3 显式披露变化榜数据日期（prod 可能滞后）；Q4 **BRK 蓄势池不标**（变化榜是「已发生异动」，语义不同）。图例补 `legend_diff` 锚点，守卫跟随 `has_diff`（仅本封确有非 BRK 卡片渲染标注时挂）。
- **判读纪律（源码级断言）**：`_build_reason` / `_load_reason_context` **不引用** `diff_boards`；diff 只进渲染层，不改 goods/bads/RR/conviction。
- **探针**：新增 `workbench/test_scan_alert_diff_boards.py` **20/0**（A 命中/连板/非连板/rank 缺失/无日期；B 不在榜+BRK；C 旧 2 参调用逐字无痕迹+图例守卫；D 判读纪律源码级+tone 无关；E `_diff_board_map` 纯函数）。全量 15 套相关回归 rc=0；`py_compile` 通过。
- **prod 只读复验（通道2）**：`get_daily_diff_summary()` → `diff_date=2026-09-27`、162 资产上榜；QNT(1555) `price_change_24h/up rank1 streak4`、SAGA(4888) `price_change_24h/down rank5 streak4`、随机 id 无命中 —— 与工单预期一致。
- **待部署**：`scan_daemon.py` 需重启容器后生效；验收：含 QNT/SAGA 的告警邮件卡片出现「📌 同源变化榜：…」，且判读 tone 不受影响。
- **未做 / 边界**：L1（披露性加权，标「双重确认」）/ L2（综合评分联立）**不在 scope**；L2 须先做 A3 holdout 校准（G 定调硬前提），否则数据窥探 + 门槛失守。

### 连板增强接早报 U-A（工单_连板增强接早报_2026-09-28，2026-09-28，本次提交）

来源：`工单_连板增强接早报_2026-09-28.md`。**候选范围**：U-A（信号层接 `streak_days`，必做）+ U-B（复活 BRIEF-OPT-002 展示层，可选/待拍板）。**本轮只做 U-A**（Q1 取工单建议「不做 U-B，仅 U-A 足够」），Q2 阈值 = 3（与 D3 一致），Q3 连跌只标注不加成。**零 DDL、零迁移、不改判定口径/上限**。

- **关键发现（工单已核实，本轮沿用）**：`_build_daily_diff_brief` 产出的 `M5_daily_diff` 是**死代码**——`brief` 里 put 了但全仓零 get，早报 HTML 无「每日变化榜」板块；因此连板接早报的真实高杠杆点是**信号层**（`FEAT-SIGNAL-SRC-001` 的 `price_surge`/`price_crash`/`price_volume_surge` 机会，已渲染进机会清单），而非展示层。U-B 未做，`M5_daily_diff` 仍为死代码（**待拍板**是否复活）。
- **U-A 落地（`macro_market.py`）**：新增纯函数 `_diff_streak_hits` / `_diff_streak_note` / `_diff_streak_bonus` + 单点 `augment_diff_streak(items, base_strength, cap, label, apply_bonus, t)`；三段接线：
  - `price_surge`：命中连板 → `trigger_logic` 追「（其中 QNT连续4天 持续强势）」+ 强度 `min(90, base+bonus)`。
  - `price_crash`：命中连跌 → 只标注「持续走弱」，**不加成**（`apply_bonus=False`；连跌是弱势确认，不应抬 confidence —— Q3）。
  - `price_volume_surge`：命中 → 标「持续共振」+ `min(88, base+bonus)`。
  - 阈值 / 加成常量：`_DIFF_STREAK_MIN_DAYS=3`、`_DIFF_STREAK_BONUS_CAP=8`、`_DIFF_STREAK_BONUS_PER_HIT=2`；与 `740e31c` 口径屏障一致，`streak_start_ambiguous=True` 一律剔除（口径年龄≠强度）。可用 `t["diff_streak_min_days"]` 覆盖阈值。
  - **不改** `n_confirm`/`direction`/`signal_type`/`key_metric`/上限值（90/92/88）——只动文案与分数。
- **通道2 prod 只读复算（live 09-27 `daily_diff_summary`）**：`price_change_24h/up` 强势(≥15%)17 条 → U-A 命中 **1 条 = QNT（连续4天）**；`price_change_24h/down` 强势(≥12%)8 条 → 命中 **1 条 = SAGA（连续4天，走弱）**；`price_volume_surge/up` 30 条 → 命中 0（无 ≥3 天）。即当日早报 `price_surge`/`price_crash` 文案会分别标注 QNT / SAGA 的连板。
- **探针**：`test_macro_market_board_tier2.py` **51 → 67/0**（新增 H 组 16 条：H1 命中集剔 ambiguous/未达阈值、H2 note 文案、H3 加成封顶、H4 命中→+2、H5 连跌不加成、H6 cap 90、H7 未达阈值/ambiguous 不变、H8 源码守卫「1 定义 + 3 调用 / 各段 cap 90/92/88 / apply_bonus=False / `{_note}` 已拼接」）。**回归**：workbench 全量 **52 个 `test_*.py` 全部 exit=0**；`py_compile` 通过。
- **与 D3 的关系**：D3 是**独立派生** `diff_streak_up` 机会（变化榜连板独立成信号）；U-A 是在**既有暴涨/暴跌/量价齐升机会**里标注连板并小幅加成。两路互不冲突（信号层双保险）。
- **未做 / 边界（须留档）**：① **U-B（`M5_daily_diff` 渲染）未做** —— `_build_daily_diff_brief` 每次仍计算但不消费，属已知死代码，待产品拍板是否新增「📊 每日变化榜」模块；② 阈值 3 为工单/与 D3 口径一致的拍板值，非回测校准；③ 加成 cap 8 / per_hit 2 为经验值（工单建议档）；④ live 只读复算基于沙箱抓取的 09-27 `daily_diff_summary`，非直连 prod DB。
- **待部署**：`macro_market.py` 需容器 **redeploy** 后次日早报机会文案生效。

### U-B：复活早报「每日变化榜」模块（工单_U-B复活早报变化榜模块_M5_daily_diff_2026-09-28，2026-09-28，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\工单_U-B复活早报变化榜模块_M5_daily_diff_2026-09-28.md`。**零 DDL、零迁移**；Q1~Q4 取工单建议（模块 1.5 / 7 分类 Top5 / 保留 ⭐⚠️ / 连板暂不做）。改动面：`send_daily_brief.py`（渲染层）+ `macro_market.py`（生产者，**工单原以为「数据已就绪、不改」——实测前提不成立**）+ 新探针。

- **🔴 先纠正工单前提（物证级）**：工单称「`_build_daily_diff_brief` 已就绪、数据已进 brief」，实测**两处缺陷叠加** ⇒ M5 从来就是 `{}`：
  1. **形态不匹配**：`db_stats.get_daily_diff_summary` 返回的是 `{ok,diff_date,available_sectors,available_tiers,categories:{cat:{up,down}}}`，旧 `_build_daily_diff_brief` 按「扁平 `{label:[items]}`」遍历该 dict ⇒ 命中 `available_sectors`（str 列表）并在 `it.get(...)` 处抛 `AttributeError`（本地复现坐实）。旧 `CATEGORY_LABELS` 的键 `price_change_24h_down` 也不存在（真实是 `price_change_24h` 的 `up/down` 两侧）。
  2. **导入+调用双错**（线 5518 preload 与 8862 兜底同款）：`from crypto_research.db import db_stats` —— 该模块**不存在**（`db_stats.py` 在 workbench 根 / 容器 `/app`），必然 `ImportError` 被 `except` 吞掉；且 `get_daily_diff_summary` **自开连接池、不接收 conn**，旧码却传了 `conn`（会被当 `diff_date`）。
- **修复 1（生产者 `macro_market._build_daily_diff_brief`）**：按**嵌套 categories** 拍平为展示顺序 7 榜（涨/跌幅榜来自同一 `price_change_24h` 的 up/down；成交量异动取放量、缺则缩量；**即将解锁取 `unlock_7d` 的 `down` 侧**——生成器口径落 direction='down'，实测 up=0/down=20），每条透传 `category`/`direction` 并打 ⭐/⚠️；保留扁平输入兼容（注入/旧形态）。空/异常一律返回 `{}`。
- **修复 2（导入与调用，两处）**：`from crypto_research.db import db_stats` → `import db_stats`；`get_daily_diff_summary(conn)` → `get_daily_diff_summary()`。**连带效果（须留档）**：5518 preload 修复后 `overview["daily_diff_summary"]` 首次真正落地 ⇒ **D3 `derive_board_opportunities`(FEAT-SIGNAL-SRC-003) 同步复活**（该特性此前因同一 ImportError 一直空转）。prod 只读实测 09-27 阈值 3 下派生 **n=3（1 long / 2 short）**，量级很小、属预期。
- **渲染层（`send_daily_brief.py`）**：新增 `import html`；`_fmt_diff_value`（量价齐升/赛道轮动=X.X 分、**即将解锁=美元金额**（`_fmt_mcap`，避免渲染成 `621274868.38%`）、其余=百分比）；`_render_daily_diff_html(brief)`（空/非 dict/无条目 → `""`；7 榜固定序 + 未知键追加；⭐/⚠️ 标记 + 数值；纯展示不查库）；`render_brief_html` 在**模块 1.5**（大盘脉搏之后、赛道轮动之前）`html_parts.append(_render_daily_diff_html(brief))`。
- **三通道验收**：
  - **源码/探针**：新增 `workbench/test_send_daily_brief_m5.py` **29/0**（嵌套拍平/不抛异常/7 榜序/Top5/⭐⚠️/category·direction 透传/空兜底/扁平兼容/数值口径含解锁金额/渲染空串与正常/接线位次/端到端 `render_brief_html` 含「每日变化榜」且在赛道轮动之前）。
  - **prod 只读（通道2/3）**：`get_daily_diff_summary()` 实测 7 类（含 `tvl_surge_24h`，未纳入 7 榜；`unlock_7d` up=0/down=20）；`_build_daily_diff_brief` 产出 **7 榜全非空**；真库渲染预览：`价格涨幅榜 +50.81%… / 价格跌幅榜 -22.33%… / 成交量异动 +829.20%… / 量价齐升 99.6 分… / 赛道轮动 128.8 分… / 即将解锁 $621.3M… / 市值变化榜 +80.07%…`。
  - **回归**：workbench 全量 **53 个 `test_*.py` 全部 exit=0**；`py_compile` 3/3。
- **未做 / 边界（须留档）**：① **`tvl_surge_24h` 未纳入**（工单只列 7 类、网页端 `DIFF_CATEGORY_LABELS` 也无此键）——数据存在但早报未展示，如需可后续加；② **连板标注（Q4）未做**（U-A 已在信号层落地）；③ `_fmt_diff_value` 对 `volume_surge_24h/down`（缩量）沿用网页端不显 `+`（值为正、语义为缩量），未另造口径；④ 存量快照无 `M5_daily_diff` 内容，需 **redeploy + 次日 build** 才有真数据（`push ≠ 线上生效`）；⑤ D3 复活属本次连带修复，若产品认为需单独评估请回退 5518 处改动（仅影响 D3，不影响 M5——M5 走 8862 兜底）。
- **待部署**：`send_daily_brief.py` / `macro_market.py` 需容器 **redeploy**，次日 08:30 快照 + 09:00 早报生效。

### 高亮邮件「只进不出」+ M2 方案 A（审计_高亮信号邮件_只进不出诊断 / 处置建议_M2_HIGH档位收死与早报空窗，2026-09-28，本次提交）

**范围**：邮件「只进不出」诊断（用户拍板「和高亮池同步显示，不用退送邮件」；R3 每日全集「你看着办」）+ M2 报告**只做方案 A**（展示层，用户拍板）。**零 DDL、零迁移、不改评分/档位/白名单**。

- **高亮邮件改为与池同步（`send_highlight_alert.py`）**：
  - `classify_card` 触发逻辑不变（仍只 new/upgrade 才发信，不发退场/降级邮件）；但**正文改为渲染当前高亮池全集**——本轮获批发送的 new/upgrade 标徽章、其余标「📌 在池」（新增 `ALERT_HOLD`/`ALERT_LABEL`）。`main` 阶段1 仍只对 candidates 取锁去重；阶段2 用 `pool_items`（granted kind 或 HOLD）渲染，抬头加「在池 N 条」。`--dry-run` 同样给池全集。⇒ 读者每次都能看到「现在池里有哪些」，退出/降级卡因不在池中自然消失，无需另发退场邮件（等于把 R3「每日全集」并入每次告警，不新增邮件通道、不刷屏）。
  - **R4 聚合类不计币**：新增 `AGGREGATE_SIGNAL_TYPES`（narrative / chain_inflow / sector_* / stablecoin_* / mvrv_* / fng_extreme / leverage_extreme / btc_left_accum / cm_adoption_divergence）；`symbol_count` 对聚合类 target 不再单凭正则判币（DePIN/Oracles 不再计；真实币仍走 `involved_symbols`）。prod 只读实测：09-28 高亮池覆盖币数 **7 → 6**（差值为 DePIN）、09-29 **15 → 14**（差值为 Oracles）。
- **M2 方案 A（展示层，零算法风险）**：
  - **A2/A3（`macro_market.py`）**：新增纯函数 `_brief_top_opportunities(opps, top_n=3, label)` = 「HIGH ∪ 池内 conv 前 3」；~~兜底项就地打 `display_demoted`+`display_note`~~ ⚠️ **更正（`1d66940` N3）**：兜底项已改为 **`copy.deepcopy` 后再打标**并通过 `_split_brief_opportunities` 返回 `(机会, 观察)` 两不重叠清单（就地打标会污染共享的高亮卡对象）。早报 `M8_opportunities`/`M4_risks` 用它（高危侧对称）。**prod 只读实测**：09-28（HIGH=0）→ 兜底 3 条（AI & Big Data / DePIN / 2 币 MVRV 极度高估），机会段不再空窗；09-29（HIGH=3）→ 3 HIGH + 3 兜底。
  - **A1（三处消费 `tier_demote_reason`）**：高亮邮件 `render_card` 加「⬇️ 降档说明」；早报 `send_daily_brief` 的 AI 精选高亮卡与精选机会卡各加降档说明行（优先 `display_note` 再 `tier_demote_reason`）；前端 `index.html` `renderSignalItem` 加 `.signal-demote` 行（消费 `o.display_note || o.tier_demote_reason`）。
- **验证**：`test_highlight_alert.py` **68 → 77/0**（新增 H7：池同步/在池徽章/R4 聚合类/降档说明/main 池全集接线）；新增 `test_m2_high_fallback_20260928.py` **21/0**（A2 helper 全分支 + brief 接线源码守卫 + A1 早报渲染带降档说明 + 前端消费）。workbench 全量 **56 个 `test_*.py` 全部 exit=0**；`py_compile` 3/3；`node --check`（index.html script）通过。
- **未做 / 边界（须留档）**：① **M2 方案 B（放行硬数据极值类进 HIGH）未做**（用户选「只做 A」）；② **C（为 11 类豁免设计可回测口径）/ D（分位数 tier）/ E（turnover 加分）均未做**；③ ~~`_brief_top_opportunities` 对 `opps` 是就地加键，无其它消费者读取该二键，安全~~ ⚠️ **该判断已被 `1d66940` 证伪**：`highlight_signals` 与 `opportunities` 共享 dict，就地打标会连带污染高亮卡；现已改 deepcopy（见上条更正）；④ 存量早报快照仍是旧口径，需 **redeploy + 次日 build** 才生效（`push ≠ 线上生效`）；⑤ 「每日无变化也发一封池摘要」未单独建通道（并入告警正文，见上）。
- **待部署**：`send_highlight_alert.py`（scheduler 子进程）/ `send_daily_brief.py` / `macro_market.py` / `index.html` 需容器 **redeploy** 后生效。

### 早报 U-A 可见性修复 + 审计 2026-09-29 处置（审计_加密大盘早报_2026-09-29，2026-09-29，本次提交）

来源：`审计_加密大盘早报_2026-09-29.md`（对象 = 09-29 09:00 早报，U-A/U-B 部署验收）。**关键更正：审计的 U-A「未观测到 ⇒ 倾向漏部署」判断错误**——实测 U-A 已部署且正常，问题在**渲染层不可见**。

- **U-A 诊断（物证级，live 只读）**：`git merge-base --is-ancestor 9859fb8 775e9d7` = **真**（U-A `9859fb8` 是 U-B `775e9d7` 的直接祖先，14:25 < 15:15）；既然 U-B 已部署，U-A 必在同一镜像内。`GET /api/market/overview` 实测 `opportunity_list` 的 `17 币 24h 暴涨` `trigger_logic` = `QNT, SOON, AUDIO, SHFL, INX 24h 涨幅超 15%，最高 50.8%，平均 23.6%（其中 QNT连续4天 持续强势）`、`8 币 24h 暴跌` = `…（其中 SAGA连续4天 持续走弱）` ⇒ **U-A 已生效**。
  - **真因（渲染层遮蔽）**：① `send_daily_brief` 的 AI 精选高亮卡渲染 `reason_summary or trigger_logic`，而 AI 已给 `17 币 24h 暴涨` 写了 reason_summary（「…RSI96.7 透支…」）⇒ 连板注脚被覆盖；② 同一聚合机会被 **M4-1 折叠**出「精选机会」⇒ `trigger_logic` 在邮件里**无处渲染**。两者叠加 ⇒ 连板信息全封不可见。
- **修法（`send_daily_brief.py`，零 DDL）**：新增 `_streak_hint(logic)`（正则 `（其中…持续(强势|走弱|共振)）` 提取连板注脚）；高亮卡在 `reason_summary` 覆盖时**补显**连板注脚（去重、不改 reason 口径）。⇒ `QNT连续4天 持续强势` 进入邮件。
- **同批 P2/P1-7（审计 §五）**：① **P2-3**「今日无操作 vs 操作清单」矛盾 → `_build_tldr_html` 无新开方向时标题改「🎯 观察 / 持仓参考（非新开方向）」并注明「今日无新开方向（见 AI 定调「今日无操作」）」；② **P1-7 残留** → 高亮区含「观察级（非高亮）」条目时标题加「（含观察级）」。
- **未改（留档）**：① **恐贪 73 vs 74**：风险信号 `key_metric` 取 overview `3情绪`（73），大盘脉搏取 SSOT（74），两源不一致；改需把信号侧 fng 源切到 SSOT（可能移动阈值），属独立小工单。② **恐贪「不可用」vs「74」**：Morning Call 的「不可用」是 LLM 文本，非渲染层可确定性修复，需 prompt/数据门控，留档。
- **自测**：`test_daily_brief_20260928.py` **36 → 44/0**（新增 H-1~H-8：`_streak_hint` 提取/空串、reason_summary 覆盖时补显且原 reason 不丢、无观察级不加注、含观察级加注、TL;DR 无方向改标题/有方向保持）。回归：workbench 全量 57 个 test 中 **55 通过**；**2 个失败（`test_highlight_determinacy_20260926` / `test_signal_type_calibration_20260926`）与本次无关**——已用 `git stash` 在无本次改动的 HEAD 上复现同样失败，属并发进程在制品（raw 95→MED 等 tier 断言）。`py_compile` 通过。
- **待部署**：`send_daily_brief.py` 需容器 **redeploy** 后次日 09:00 邮件生效（U-A 连板注脚才会在 AI 高亮卡出现）。

### `cae0431` 复验四问处置 Q1~Q4（复验_cae0431_高亮邮件池同步与M2方案A_2026-09-29，2026-09-29，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_cae0431_高亮邮件池同步与M2方案A_2026-09-29.md`。复验确认 `cae0431` 源码齐备、离线测试独立复现、5 套相关回归未破、**已部署**（容器 09-29 08:56:27 CST 同秒重启，滞后 8m43s）、**前端 A1 有铁证**（线上首页含 `signal-demote`×2、`降档说明`×1），指出 **M5 突变逃逸**（正文退回只渲染增量仍全绿）与 N1/N2/N3 三项。用户拍板：**N2 同口径封顶、N3 deepcopy**；Q1/Q2 按复验建议做。**零 DDL、不改评分/阈值口径**。

- **Q1（P2，已修）核心改动承重**：邮件正文组装抽为纯函数 `send_highlight_alert.build_pool_items(highlights, granted_kinds, max_cards)`（本轮获准项保留 new/upgrade、其余标 `ALERT_HOLD`；`card_sort_key` 排序、可选截断）；`main` 阶段2 与 `--dry-run` 均调它。`test_highlight_alert` 补**行为断言**（池 10 卡 + 1 granted ⇒ 全集 10 / 1 new / 9 在池 / 新增排首 / max_cards 截断）+ 阶段2 调用守卫（`build_pool_items(highlights, _granted_kinds`）。**独立见证**：`pool_items = granted`（退回只渲染增量）→ rc=1、`build_pool_items` 丢 granted → rc=1；恢复 82/0。
- **Q2/N1（P2，已修）兜底文案不说谎**：`_brief_top_opportunities` 的 `display_note` 前缀按「当日是否有 HIGH」二选一——有 HIGH 用「池内分数靠前（非 HIGH）」，无 HIGH 才用调用方传入的「非高确定性档（当日 HIGH 不足）」。避免 HIGH 充足时仍印「HIGH 不足」。
- **N2（P2，已修，原则取舍）`missing_calibration` 与 `exempt_*` 同口径封顶 MED**：`_exempt_no_high` 判据由「gate 以 `exempt_` 开头」扩为「`exempt_*` **或 `missing_calibration`**」；降档原因区分文案（`missing_calibration：该类型未进入回测校准表…`）。根因：不在校准表的类型（如 `price_surge/crash/pvs`）此前绕过「未回测不进 HIGH」直进 HIGH。**prod 只读实测**：三类 gate 均为 `missing_calibration` ⇒ 下轮重算起归零（09-29 那 3 条 HIGH 将降 MED）；`catalyst`(calibrated_ok) 不封顶。**连带**：更新 `test_signal_type_calibration_20260926`（无校准断言由「仍 HIGH」改为「封顶 MED + missing_calibration 原因」）与 `test_highlight_determinacy_20260926`（把其 `_push` 用的 `catalyst` 钉为 calibrated_ok，保持「分数→档位」测试语义）。
- **N3（P2，已修）兜底打标不污染高亮卡**：`_brief_top_opportunities` 对兜底项改 **`copy.deepcopy` 后再打** `display_demoted`/`display_note`，返回 `(items, fallback_src_ids)`；调用方用 `fallback_src_ids` 从 `M8_watchlist` 剔除原始对象（避免副本与原对象重复）。**prod 只读实测**：原对象 `display_demoted` 保持 None、副本被正确打标。
- **验证**：`test_highlight_alert` **77 → 82/0**、`test_m2_high_fallback` **21 → 30/0**、`test_signal_type_calibration_20260926` 与 `test_highlight_determinacy_20260926` 改契约后绿；workbench 全量 **57 个 `test_*.py` 全部 exit=0**；`py_compile` 4/4。
- **未做 / 边界（须留档）**：① **M2 方案 B/C/D/E 仍不做**（B 放行硬数据极值需另行拍板）；② **行为态验收仍需转发邮件**（邮件正文不落库；早报 A2 兜底场景待 HIGH=0 的日子，可用 09-28 快照离线回填验证）；③ N2 属**原则取舍**，会使 HIGH 更稀缺（用户已知并接受）。
- **待部署**：`macro_market.py` / `send_highlight_alert.py` 需容器 **redeploy** 后生效。

### `1d66940` 复验 Q1~Q4 处置（复验_1d66940_Q1-Q4处置_2026-09-29，2026-09-29，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\复验_1d66940_Q1-Q4处置_2026-09-29.md`。复验确认 Q1~Q4 源码全部到位、离线测试独立复现（82/0、30/0）、5 套回归全绿、`py_compile` 6/6、**已部署**（容器 09:42:59 CST 同秒重启，滞后 6m11s）、真数据回填确认 N1/N3 生效；另开 **MU2/MU6 突变逃逸** + **N9/N10**。用户拍板：**N10 接受 + 图例标注**。**零 DDL、不改评分/阈值**。

- **Q1（P2，已修）排序断言判别性（MU2）**：原 `build_pool_items` 行为断言里 granted 恰钉在**首张**卡 ⇒ 同分下排序与否同序，断言恒绿。改为 granted 钉**末尾卡（T9）**并断言 `_items[0][0]["target"] == "T9"`；去排序突变 → rc=1。（M5「退回只渲染增量」上轮已堵死。）
- **Q2（P2，已修）watchlist 去重无行为断言（MU6 真风险）**：`M8_watchlist` 剔除兜底项原先只有**源码文本守卫**（去掉 `| set(fallback_src_ids)` 仍全绿）⇒ 早报里同一条信号会在「机会段（带降档说明）」与「观察段（不带）」各出现一次。新增纯函数 `macro_market._split_brief_opportunities(opps, top_n, label)` 返回 `(机会, 观察)` 两**不重叠**清单并在 `generate_morning_brief` 使用；补行为断言（兜底原始对象不在 watchlist、HIGH 原对象不在 watchlist、`len(机会)+len(观察)=全池`）。去 `| set(src_ids)` 突变 → rc=1。
- **Q3/N10（战略，用户拍板「接受 + 图例标注」）**：N2 后能进 HIGH 的只剩有回测背书的类型（prod 实测仅 `catalyst`，且 09-29 catalyst 最高 62 < 70）⇒ HIGH 档**实质无供给**。接受现状，并把「无 HIGH」显式写进早报：`send_daily_brief` 机会段在 `M8_opportunities` 无 HIGH 时加注「⚠️ 当日无满足回测背书的 HIGH 档信号（HIGH 现主要由有回测背书的类型供给）；下方为池内分数靠前项，属『非高确定性』，勿当高置信对待」，有 HIGH 时不显示（动态、无陈旧断言）。**未做** price_* 可回测口径与方案 B 白名单。
- **Q4（P1，已修）N9 校准加载失败静默清零 HIGH**：N2 之后「表为空」= 所有类型 `missing_calibration` ⇒ 全量降 MED，而注释仍写「空 dict = 安全降级」。新增哨兵 `_CALIB_LOAD_FAILED`（`_load_signal_type_calibration` 失败置 True、成功置 False），`_exempt_no_high` 见哨兵**保守返回 False（不封顶）**；同步改掉 2681 行陈旧注释。突变去哨兵 → rc=1。**未做**：加载失败的邮件告警（仅 `logger.warning`，新增告警通道需去重设计，留档）。
- **Q4b（文档）**：更正 `AGENTS.md` `cae0431` 节「兜底项就地打标…无其它消费者…安全」的陈旧/自相矛盾表述（就地打标会污染高亮卡，已改 deepcopy + `_split_brief_opportunities`）。
- **验证**：`test_highlight_alert` **82/0**、`test_m2_high_fallback` **30 → 41/0**；突变 MU2/MU6/MU7(N9) 全部 rc=1；workbench 全量 **57 个 `test_*.py` 全部 exit=0**；`py_compile` 4/4。
- **未做 / 边界（须留档）**：① N9 的邮件告警未做（仅日志）；② N10 的 price_* 回测口径/方案 B 未做（用户选「接受+标注」）；③ 行为态验收需转发邮件（邮件正文不落库）；早报 N2 效果待 **09-30 08:35** 快照（应见 0 HIGH + 3 兜底、`display_note` 含 `missing_calibration`）。
- **待部署**：`macro_market.py` / `send_daily_brief.py` 需容器 **redeploy** 后生效。

### 早报小白可读性处置 R-1/3/5/6/7/8/11/12（审计_早报可读性_小白视角_2026-09-29，2026-09-29，本次提交）

来源：`审计_早报可读性_小白视角_2026-09-29.md`（对象 = 09-29 早报，纯新手视角）。**全部在 `send_daily_brief.py`/`macro_market.py` 渲染层**，零 DDL、零信号/数据层改动。R-2/R-10 已由 `76d60c9` 修复（待部署）。

- **R-5（🔴 P0，疑似 bug → 实为口径混同，已修）**：审计见「5 币量价齐升」卡置信度显 **LOW** 而降档说明写「档位降为 MED」⇒ 好像同卡两档。**真因**：卡片徽章渲染的是 `ai_analysis_v2.confidence`（**模型信心**），而降档说明讲的是 `conviction_tier`（**信号档位**）——两套口径并置被读成矛盾。修法：① `macro_market` 三处降档文案 `档位降为 MED` → **`信号档位封顶 MED`**（L5194/5217/5413）；② `send_daily_brief` 徽章由裸 `{confidence}` 改 **`AI信心 {confidence}`** 并加 `title` 说明「与信号档位是两套口径」。
- **R-1（🔴 P0，语气打架）**：头条「RWA 单周暴涨 34%」兴奋向、正文却「今日无操作/不宜追高」。修法：AI 定调 chips 行新增**「行动」chip**（有方向 → `行动：N 条方向`；无 → `行动：今日无操作`），让第一眼就见行动结论，不被标题带偏。
- **R-3（🟠 P1，恐贪三说 + 内部词漏出）**：`macro_market._fear_greed_ssot_verdict` 的 note 原写「已按 **SSOT** 74 取值」→ 删内部词，改「**已采用最新官方值 74**」。（Morning Call 的「不可用」属 LLM 文本、非渲染层可确定性修复，留档。）
- **R-6（🟠 P1，六维名不副实）**：高亮卡标题「六维评分 · 自动搜索补全」→ **「关键三维评分（技术/基本面/情绪）· 自动搜索补全」**（实际只渲 3 维）。
- **R-11（🟡 P2）**：「AI存疑」→ **「AI观点分歧」**（原字面像「AI 有疑问」，实为「AI 不认同方向」）。
- **R-8（🟡 P2，数字缺刻度）**：恐贪标签加「（0-100，>50 偏贪婪）」；高亮卡「综合评分」→「综合评分（满分100）」。
- **R-7/R-12（🟠 P1，术语 + 信息过密）**：新增顶部**「🧭 今日 3 句话」**卡（① 大盘 ② 能不能动 ③ 最该盯）+ 一行**名词速查**（RWA/MVRV/共振/HIGH-MED-LOW/命中率白话）；`_build_top_summary_html` 无 AI 也能降级产出。
- **自测**：`test_daily_brief_20260928.py` **44 → 54/0**（新增 R-1/3/5/6/8/11/12 断言）。**回归**：workbench 全量 **57 个 `test_*.py` 全部 exit=0**；`py_compile` 2/2。
  - **踩坑（两处严格护栏）**：① 高亮徽章 `title` 初版写「模型（AI）**置信度**」→ 撞 `test_daily_brief_p0_20260927` 的 `"置信度" not in html` 护栏 ⇒ 改「模型（AI）信心」；② 名词速查初版含「**>85%** 偏贵」→ 撞同测试 `"85%" not in html`（反硬编码置信度）护栏 ⇒ 删数字改「越高越易抛压」。**该测试是对 09-27 P0-a 的守卫，非误伤，措辞须绕开其禁词**。
- **未改（留档）**：R-4 恐贪 73 vs 74（信号侧源 vs SSOT，改需切源、可能动阈值）；R-9 告警质量区块折叠 + 删调试词（`边缘桶 price_chg=<3`/`规则 C/D` 出自 `build_scan_edge_report` 的 conclusion，非本渲染层）；R-13 降档说明逐条展开（已由 R-5 消歧）；R-14 关联折叠内联（结构微调）。
- **待部署**：`send_daily_brief.py` / `macro_market.py` 需容器 **redeploy** 后次日 09:00 邮件生效。

### data_sync_daily「老是告警」根因修复（2026-09-29，本次提交）

来源：看护邮件「关键 cron `data_sync_daily` 已 30.7 小时无成功执行（最近 done `1790549530.869194`）／未自动补跑（scheduler 存活），最近错误 `exit code 1`」。**物证级只读排查（prod `sys.task`/`sys.task_log`）**：

- **最近一次 run = task `361d0b6a6c59`（09-28 22:30 UTC = 09-29 06:30 CST，耗时 69s，`failed / exit code 1`）**；日志 42~43 行：`全部完成：成功 1 / 失败 1 / 共 14`、`失败任务：资产同名去重`。子任务 2 `dedup_assets.py --apply` 输出 `[FAIL] E/ACC … canceling statement due to lock timeout` / `[FAIL] HOODIE … lock timeout`、`exit=2`。
- **根因链**：① 删 `core.asset` 触发 ~42 张子表 FK 级联，06:30 与 `derivatives_batch`（`30 */6 * * *`，**同 06:30**）并发写子表 ⇒ 撞 `lock_timeout`；② 当时部署的还是**旧编排**（critical 失败即 `break`）⇒ 只跑了 2/14 子任务、整体退出 1。`4f5cf8d`（09-29 08:37 提交）已加「每组独立事务 + 宽锁 120s + 5/15/30s 重试 + 子任务隔离」，**但晚于该 run**，且**它仍把「资产同名去重」标 `critical=True`**、`dedup_assets.main()` 只要有一组失败仍 `return 2` ⇒ 即便部署，残留锁超时仍会 `exit 1` → 看护反复报「任务自身失败」。
- **修复（`scripts/bin/dedup_assets.py`，外溢最小）**：`main()` 区分 **锁竞争耗尽**（`failed_lock`）与 **真实错误**（`failed_other`）——① 锁耗尽 → 打印 `[WARN] … 撞锁重试已耗尽，本轮跳过（幂等，次日重试）`，**退出码 0**（同名去重是幂等的机会性清理，锁是调度竞争非数据故障）；② 只有非锁竞争的真实错误（约束冲突/数据异常）才 `return 2`（保持失败可见）。**不改编排 `critical` 标记**（真实错误仍会 `exit 1` → 告警，正确）；不改调度（06:30 与 derivatives_batch 同窗属既有安排，另议）。
- **自测**：`test_dedup_assets_resilience.py` **+4 断言**（`failed_lock`/`failed_other` 分流、`return 2 if failed_other else 0`、`[WARN] 本轮跳过` 文案、`is_lock_error` 同源判据）。**回归**：workbench 全量 **59 个 `test_*.py` 全部 exit=0**；`py_compile` 通过。
- **根因补充（物证升级）**：prod `core.asset` 上有 **42 条 FK**（子表含 `biz.asset_derivatives` 等），删 `core.asset` 需对这些子表级联/检查；`derivatives_batch`（写 `biz.asset_derivatives`）原与 `data_sync_daily` **同在 06:30 起跑**、持续 ~12min 持锁 ⇒ 去重（日同步子任务 #2）等锁超时。历史 35 天里 `data_sync_daily` 仅 09-28 这一次因锁超时失败（其余 ~3-5min done），当前**完全同名重复组 = 0**（去重是幂等机会性清理、无累积）。
- **二次修复（错峰，本次追加）**：`scheduler.py` 的 `derivatives_batch` 由 `30 */6 * * *` → **`5 */6 * * *`**（06:05 起跑、约 06:17 完成，与 06:30 的去重错开）。universe 只依赖 `core.asset.market_cap_rank`、不依赖同窗 ETL，错峰无副作用；`data_sync_daily` 保持 `30 6` 不动（避免下游时序连锁）。护栏：`test_dedup_assets_resilience.py` +3 断言（`derivatives_batch` 在 5 */6、不在 30 */6、`data_sync_daily` 仍在 30 6）。
- **根因定性（两层）**：① **告警根因**（严重度误判）已治——瞬时锁超时是良性、幂等的清理失败，不该升级为「关键任务失败→整条日同步 failed→告警」，现按「重试+跳过」处理（exit 0）；② **锁竞争**（删父行 vs 并发写子行）是结构性的，**靠错峰降低频率、不追求 100% 消除**（硬消除需停并发写/长事务，代价更大）。
- **未做 / 边界（须留档）**：① 锁耗尽跳过 = 该批重复资产延后至次日清理（幂等、无数据丢失）；② 其他常驻写 `core.asset` FK 子表的任务（如 `catalyst_fast_daemon` 写 `biz.asset_catalyst`，FK=NO ACTION）仍可能偶发短事务竞争，由 120s 宽锁 + 重试覆盖；③ **需容器 redeploy** 后下次 06:05/06:30（北京）生效。

### 变化榜数据卡 09-27 诊断处置（诊断_变化榜数据卡09-27_根因_data_sync_daily_2026-09-29，2026-09-29，本次提交）

来源：`E:\瞎搞乱搞\workbuddy\crypto-profile-collection\诊断_变化榜数据卡09-27_根因_data_sync_daily_2026-09-29.md`。用户授权：**P0 立即补跑 09-28**、**P2 加独立兜底调度**。**零 DDL**。

- **🔴 更正诊断的根因归属（物证级，prod `sys.task_log` 只读）**：诊断报告推测「L32 赛道刷新 / L54 supply 对齐」导致 break，实测**都不是**——失败 run `361d0b6a6c59`（09-28 22:30 UTC = 09-29 06:30 CST，69s）的日志显示：子任务 2 **`资产同名去重`（`dedup_assets.py --apply`）** 因 `lock timeout`（E/ACC、HOODIE）`exit=2` → 旧编排 `[FAIL] 关键任务 [资产同名去重] 失败，终止后续任务` → 只跑 2/14、`每日 diff 变化榜`（排第 8）从未执行 ⇒ 09-28 零行。与 AGENTS 上一节「dedup 锁竞争」**同源**；报告的 L58 位置/前序任务列表与实码不符。
- **✅ P2-A 已在 HEAD（无需改码）**：诊断建议的「子任务隔离」早已提交——`df4444f`（`run_data_sync_daily.py` 去掉 `break`，14 个子任务全跑，`critical` 只影响整体退出码）、`4fb35ff`（`dedup_assets` 锁耗尽 `exit 0` 跳过）、`4f5cf8d`。三者均为 `HEAD` 祖先（`merge-base --is-ancestor` 已验证），09-28 那次失败是**旧构建**产物。
- **✅ P0 补跑（用户授权，prod 写）**：执行 `daily_diff_generator.py --date 2026-09-28`（幂等 `ON CONFLICT DO NOTHING`）→ 本次仅补 **9 行**（`market_cap_mover 1` + `sector_rotation 8`），说明 09-28 其余 ~230 行**此前已被回填**（并发进程/他方）；复查 `biz.daily_diff_summary` 09-28 现 **239 行**（09-27 = 235），恢复完成。09-29 因 `asset_market_daily` 09-29 未就绪（=0）暂不可补，待 ETL 就绪或次日跑批。
- **✅ P2-B 独立兜底调度（本次提交）**：`scheduler.py` 新增 `("daily_diff_fallback", "5 8 * * *", "daily_diff_generator.py", [], …, "core")` —— 每日 **08:05（北京）**（ETL 06:15 → 日同步 06:30 → 早报快照 08:30 之间）幂等重生成变化榜，与 `data_sync_daily` 解耦；即便日同步关键任务失败也不会让变化榜缺日。`scheduler.py --list` 实测已注册。
- **验证**：workbench 全量 **60 个 `test_*.py` 全部 exit=0**；`py_compile scheduler.py` 通过；`scheduler.py --list` 含 `daily_diff_fallback  5 8 * * *`。
- **未做 / 边界（须留档）**：① 未改 `data_sync_daily` 的 `critical` 标记（真实错误仍应 `exit 1` 告警，已在上一节理由）；② 未加「加载失败邮件告警」；③ `daily_diff_generator` 默认取 `max(market_date)`，故兜底只会生成「最新有行情的一天」——若 09-29 ETL 迟迟不就绪，兜底不会凭空补历史缺口（历史缺口仍需显式 `--date`）。
- **待部署**：`scheduler.py` 需容器 **redeploy** 后 `daily_diff_fallback` 生效。

### data_sync_daily 子任务「挂死」隔离（用户「改任一子任务失败不影响其他任务的执行」，2026-09-29，本次提交）

- **核实现状**：**失败隔离早已存在**——`run_data_sync_daily.py`（2026-09-28 `df4444f`）已去掉 `break`，14 个子任务无论成败都依次执行；`critical` 只影响整体退出码、不影响执行。容器实跑日志亦印证（`共 14 个子任务（相互隔离：任一失败不中止其余）`）。
- **仍缺的一环 = 挂死（hang）**：`subprocess.run(cmd)` **无超时**，一个网络无响应的子任务会**永久阻塞**、后续子任务全部不跑（本机/沙盒实测 `sync_core_supply_from_cmc.py` 可挂 >30min；与本次重跑时容器被重建打断是两回事）。这才是「一个子任务影响其他子任务」的剩余形态。
- **修复（`scripts/bin/run_data_sync_daily.py`）**：`run_task` 改为 `subprocess.run(..., timeout=SUBTASK_TIMEOUT_SEC)` 并捕获 `subprocess.TimeoutExpired` → 打印 `[TIMEOUT] … 超时 Ns，已终止该子任务（不影响其余子任务继续执行）`、返回 `TIMEOUT_RC=124`、**主循环继续**。常量 `SUBTASK_TIMEOUT_SEC = int(os.getenv("DATA_SYNC_SUBTASK_TIMEOUT_SEC", "1800"))`（默认 30min，远大于整条调度正常 3~5min；外层 task_manager 12h 硬超时兜底）；启动行同步披露超时上限。
- **语义**：超时=一种失败（计入 `failed_names`；若属 `critical` 则整体退出码仍非 0，保持可见），但**绝不再阻塞其余子任务**。
- **自测**：`test_dedup_assets_resilience.py` 新增【测试10】：monkeypatch `subprocess.run` 抛 `TimeoutExpired` → `run_task` 返回 **124**、打印 `[TIMEOUT]` 且含「不影响其余子任务继续执行」；源码守卫 `timeout=SUBTASK_TIMEOUT_SEC` + `subprocess.TimeoutExpired` + 常量 env 可覆盖。**47/47**；workbench 全量 **60 个 `test_*.py` 全部 exit=0**；`py_compile` 通过。
- **未动**：`critical` 标记与退出码语义（真实失败仍需 `exit 1` 告警，符合用户只要求「不影响其他任务的执行」）；仅对挂死新增隔离。

### scheduler_watchdog 告警去重持久化（用户「怎么还在报错」，2026-09-29，本次提交）

来源：用户再收到 `data_sync_daily` 停滞告警（32.5h，与上一条 30.7h 仅隔 ~2h）。**只读核查定性**：

- **告警本身「正确」但不会因手工重跑而消除**：看护 `_last_done_ts` 只统计 `name LIKE '[调度] data_sync_daily%'` 的行；而手工/Web 触发的任务名是「每日数据同步/矫正总调度」（**不含** `data_sync_daily`），永远不匹配。实况：`[调度]` 最近 done = **09-27 22:30 UTC**、09-28 22:30 **failed**、下一次 = **09-29 22:30 UTC（= 09-30 06:30 CST）** ⇒ 只能等下一次调度成功才会清零（届时跑的是已修复代码，应 14/14）。
- **重复告警的真因 = 去重状态不持久**：`scheduler_watchdog._last_alerted` 是**进程内 dict**，**每次容器重启即清零**；实测看护心跳间隔 6~23min（≠ 配置 30min）⇒ 容器被并发进程频繁 redeploy ⇒ 同一停滞状态被反复告警（30.7h / 32.5h 两次即此）。
- **修复（`workbench/scheduler_watchdog.py`）**：去重状态持久化到 `biz.scan_stall_alert`（与其它看门狗同表，key 前缀 `sched_stall:`）——新增 `_last_alert_ts(key)` / `_mark_alerted(key)`；`_check_key` 用 `_last_alert_ts` 判静默期（替换 `_last_alerted.get`），且**仅在 `mail_ok` 时落时间**（发送失败不占位、下轮可重试）。⇒ 重启后仍记住上次告警时间，同一停滞期内每阈值只告警一次。
- **自测**：`test_scheduler_watchdog_dedup_20260929.py` **9/9**（源码守卫：前缀/复用表/不再用进程内 dict/仅成功落时间；行为：假连接断言 `_last_alert_ts` 查询与 `_mark_alerted` UPSERT 的 key 加前缀 `sched_stall:`、无记录→None）。workbench 全量 **61 个 `test_*.py` 全部 exit=0**；`py_compile` 通过。
- **落地动作**：因旧代码（未部署）仍用进程内 dict，部署新代码前可能再告警一次；**已向 `biz.scan_stall_alert` 预置 `sched_stall:data_sync_daily = NOW()`**，使新代码上线后立即进入静默期（等价于「刚告警过」），**不再重复轰炸**；`data_sync_daily` 于 09-30 06:30 CST 调度成功后 `stale=false`，告警自然解除。
- **未动**：看护阈值（30h）与 `_recent_submission` 逻辑；`biz.scan_stall_alert` 建表（已存在，复用）。
- **待部署**：`scheduler_watchdog.py` 需容器 **redeploy** 后生效。

### CVD 维度回测首测（P1 第四轮，拆 S1/S2、S7/S8）（2026-10-01，本次提交）

来源：`phase_check_cvd_ready.py` 自动提醒邮件「CVD 数据已就绪：可跑维度回测（拆 S1/S2）」——CVD 自 2026-09-16 起实时累积满 14 天。**零 DDL、零生产写**；回测脚本为手动工具、不入调度，**无需 redeploy**。

- **DB 核验（只读，prod）**：`biz.oi_cvd_snapshot WHERE cvd_5m_usd IS NOT NULL` = **579,280 行**，范围 09-16 05:40 → 10-01 03:05（UTC），来源以 `realtime` 为主（`backfill` 仅 6 行）。
- **代码（`scripts/bin/backtest_scan_scenarios.py`）**：接入 CVD 维——① 新增 `load_cvd_hourly`（`date_trunc('hour', ts)` + `SUM(cvd_5m_usd)`）；② `SCENARIO8` 常量按 §4.3 ②「设计口径」映射 `(P,OI,CVD) → S1..S8`；③ `scan_symbol` 记录元组**末尾追加 `cvd_dir`（index 8）**；④ 新增 `summarize_cvd` + main「CVD 维度拆分」表 + CVD 边际（S2−S1 / S8−S7）+ `backtest_cvd_scenarios.csv` 输出。**向后兼容**：P×OI 四象限表、`sweep`、funding 消融、指标影子消融**全不动**（`scan_symbol` 新参 `cvd_hours=None` 默认；`sweep_all` 不传）。帧头 docstring 同步去掉「CVD 暂不可回测」。
- **运行**：`python bin/backtest_scan_scenarios.py --min-n 15`（191 符号；K线 465,546 根 / OI 小时 151,198 点 / CVD 小时 36,409 点）。CVD 覆盖记录 1,221（含各窗口），缺失 NA 14,865。
- **结果（8 场景，仅 S1/S2/S7/S8 达标；S3~S6 在 CVD 覆盖期内样本 <门槛）**：S1 P↑OI↑CVD↑ n=146（1h −0.56% / 4h −0.35% / 24h −0.45%，日t −0.64）；S2 P↑OI↑CVD↓ n=93（+0.51% / −0.10% / −2.01%，−0.81）；S7 P↓OI↓CVD↓ n=93（+0.68% / +1.08% / −1.53%，−0.32）；S8 P↓OI↓CVD↑ n=33（−0.21% / −5.27% / −5.21%，−1.51）。CVD 边际：**S2−S1** 24h **−1.56%**、**S8−S7** 4h **−6.35%**。
- **结论（全 provisional）**：① 样本极薄、全桶 |日 t|<2.1，**无统计显著性**；② P↑OI↑ 侧方向符合设计（S2「诱多」24h 差于 S1），但两者 24h **均负**，只成立相对关系、不构成「CVD↑ 更优」绝对证据；③ P↓OI↓ 侧**与设计相反**——S8（设计=见底反弹）反而最差（4h −5.27%、t=−2.02），§4.3 ② 对 S8 的乐观解读未被支持；④ **区间口径警告**：CVD 覆盖窗内 P↑OI↑ 汇总 24h 仅 +3.65%、其子桶 S1/S2 均负 ⇒ 子样本落在与首轮 +5.24% 不同的（更差）regime，**CVD 拆分不可与首轮绝对值并列引用**。
- **决策（用户拍板）**：**暂不把 CVD 纳入 L2 置信度分级**（证据不足）。改动**只落回测框架 + 文档**，线上 `scan_daemon._compute_l2` 置信度公式**未动**。
- **口径边界（留档）**：回测取**触发根所在整小时** CVD 净额符号，线上 `_compute_l2` 取**近 2 个 5m 桶**（≈10min），两窗口不同（与 OI 的 §12.1-B18 同类）；若未来纳入分级须先对齐。
- **文档同步**：设计方案 §8 新增「P1 第四轮 · CVD 维拆分」（表+结论+口径边界）；§4.3 待验证、§8 局限 4、§8 复验机制、§3 阶段表 P1 行、§8.1、§12.1-A3、§14.6 的「三轮→四轮」同步。
- **阈值复校（`--sweep`，邮件标注「可选」）**：已跑通 **全量历史口径** 6×4=24 组（产出 `backtest_threshold_sweep.csv`）——**方向性结论无回归**：① 价格阈值仍是主杠杆（P↑OI↑ 24h：2.0 档 +2.67% → 4.5 档 +4.93%，PF 1.65→1.78）；② 量比阈值仍几乎无边际贡献，`VOL_RATIO_THR=2.0` 定位不变；③ 日 t 全档 0.3~1.5 仍不显著，`PRICE_THR_1H` 状态不变。⚠️ 绝对值**不可与第三轮 45 天窗表并列**（n 不同，如 4.5/1.5：554 vs 909），全量口径整体更低，与「近期 regime 更差」同向。与第三轮严格同口径的 `--lookback-days 45` 复跑**未完成**：远端 PG 流式返回大结果集时多次 `server closed the connection unexpectedly`（环境侧网络/代理中断，非脚本缺陷；时段内本机曾重启、`/tmp` 被清空），留待下次复校。
- **回归**：`py_compile` 通过；30 符号小样本干跑（`--symbols 30 --min-n 3`）与 191 符号全量均 exit 0，四个 CSV 正常产出。
- **产物**：`scripts/data/backtest_cvd_scenarios.csv`（+ 既有 `backtest_scan_results.csv` / `_indicator_ablation` / `_funding_ablation` / `_threshold_sweep`）。
- **遗留**：① CVD 覆盖窗仅 14 天且小时覆盖 ~57%，随窗口拉长须复跑；② ~~A3 holdout 仍未跑~~ → **已跑，见下方独立节**；③ 第三轮同口径（45 天窗）`--sweep` 复跑待补。

### A3 holdout 首跑：未通过（P1 标定样本外检验）（2026-10-01，本次提交）

来源：用户选择「跑 A3 样本外验证」——用回测框架已有的 `--holdout-days` 做时序切分，回答「§8 四轮标定可不可信」。**零 DDL、零生产写**；回测脚本为手动工具、不入调度，**无需 redeploy**。

- **口径**：`--min-n 15 --holdout-days 14`，universe 191 符号，cutoff = **2026-09-17 05:00**（test = 09-17→10-01，14 天；train = 其余）。加载量：K线 465,836 根 / OI 小时 151,624 点 / CVD 小时 36,835 点 / funding 95,453 点。
- **代码改动（本次唯一）**：`scripts/bin/backtest_scan_scenarios.py`
  ① 4 个 `load_*`（klines / oi / cvd / funding）改为**服务端游标 + `BATCH_ROWS=20000` 分批迭代**：原先大结果集一次性 `fetchall()`，远端连接抖动时服务端长期阻塞在 `ClientWrite`（客户端 0% CPU、UTIME 仅 ~0.5s），整段数据既拿不到也报不出；改后服务端可见 `FETCH FORWARD 20000 FROM "bt_klines"`。取数语义不变。
  ② 兄弟 CSV 加 `_sibling()` 护栏：显式给 `--out` 时用 `<out_stem>_*` 前缀，避免 `--out data/_a3_*.csv` 仍把 `data/backtest_{indicator,funding,cvd}_scenarios.csv` 这些 tracked 的**全窗口证据（split=all）覆盖成 holdout（split=train/test）**——**本次已实际发生（`with_name()` 会丢弃前缀），已 `git checkout` 回滚**，改名后不再复发。
- **主回测（P×OI 四象限，train → test 净均，括号日 t）**：
  | 场景 | 1h | 4h | 24h |
  |---|---|---|---|
  | **P↑OI↑** | +0.348%(0.62) → −0.469%(−1.87) | +2.142%(1.51) → −0.901%(−1.48) | **+5.773%(1.14) → −0.093%(−1.19)** |
  | P↓OI↑ | −1.186%(**−2.85**) → −0.182%(−1.16) | −1.560%(**−2.32**) → +0.729%(0.72) | −3.353%(**−2.12**) → −1.544%(0.55) |
  | P↓OI↓ | −0.783%(**−2.64**) → +0.458%(1.12) | −0.802%(−2.02) → −0.006%(0.09) | −1.717%(−1.10) → −2.437%(−1.04) |
  | P↑OI↓ | −0.108% → −0.126% | +0.479% → +0.095% | +2.468%(1.61) → +4.014%(−0.43) |
- **sweep（P↑OI↑ 24h，量比固定 1.5，train → test）**：2.0 **+3.590%(1.61) → +1.288%(0.12)**；2.5 +4.299% → +0.962%；3.0 +5.301% → +0.509%；3.5 +6.440% → +0.106%；4.5 +7.819% → **−0.308%**；6.0 +7.768% → **−0.955%**（量比 2.0 档同向）。
- **结论（FAIL）**：① **§8 旗舰结论 P↑OI↑ 24h +5.773% 样本外崩塌至 −0.093%**（Δ≈−5.87pp），是样本内选参产物，不得再作「已标定」证据，与 §8.1 线上 +0.896% 失配**同向且更差**；② train 侧唯一有显著性的桶（P↓OI↑，三窗口日 t ≤ −2.1）test 侧全退到 |t| < 1.2，**参数不稳健**（非反向可用）；③ test 侧 12 个组合 |t| ≤ 1.9，无一条显著；④ **指标影子消融同步未通过**——`bbw` train 单调（low→high：−0.339% → −0.094%）而 test **完全反转**（+0.244% → −0.653%），`rsi`/`percent_b` 亦不一致 ⇒ 按 §14.1 三指标**均不准入告警逻辑**；⑤ **sweep 的阈值单调性被证伪**：train 严格递增、test **严格递减**，选出的 4.5/1.5 恰是 test 最差档之一（**数据窥探直接反例**）；唯一两侧一致的结论是**量比无边际**（`VOL_RATIO_THR=2.0` 定位不变）。
- **效力边界（不得过度解读，三条均为数据面事实）**：(a) `biz.oi_cvd_snapshot` 的 OI 自 **2026-08-26** 起（36 天）⇒ P×OI 的 train 段实际仅 **08-26→09-17 = 22 天**，与 test **相邻**，不是跨 regime 切分；(b) 两段**同属上涨 regime**（BTC train +19.11% / test +10.43%；ETH +40.43% / +11.50%；test 全宇宙等权 +21.43%）⇒ 本次「未通过」**不能外推为跨 regime 失效**，证明的是**参数在相邻窗口内不稳**，但已足够否定「已标定」表述；(c) **funding 消融在 test 段无数据**——`biz.funding_rate_hist` 最大值 = 2026-09-17 05:00（恰为 cutoff）。
- **决策/影响**：A3 **仍未关闭**，但 §4.3 置信度分级 / §9 选信号口径继续标 `待验证假设`，不得引用 §8 样本内数字；关闭需**真正的跨 regime holdout**（当前唯一具备跨 regime 样本面的是爆仓维 `biz.liquidation_history` 180 天）。**线上代码/阈值/调度一律未动。**
- **顺带发现（新增 §12.1-B26）**：`scan_funding_backfill` 只挂工作台任务表、**未进 `scheduler.py` 定时**（该列表只有 `scan_oi_backfill` 等）⇒ funding 自 09-17 起停滞 14 天，影响 §8 funding 消融 / B12 费率拥挤度 / A4 补费率成本项。行动项：接入调度 + 加 freshness 告警 → **同日已修复，见下节**。
- **读数口径提醒（留档）**：`summarize`/sweep 行里「净均收益%」是**按笔**均值、「日t值」是**按日等权**，两者可反号（如 test P↑OI↑ 2.0/2.0 24h：按笔 +1.001%、日 t −0.35）⇒ 按 §8 自己的日级聚类口径读 t，勿只看按笔均值。
- **回归**：`py_compile` 通过；主回测与 sweep 两次全量跑均 exit 0；四个 CSV 正常产出。
- **产物**：文档 §8「阈值复校」新增 ⑥、§12.1-A3（主回测表 + sweep 表 + 边界 + 行动项）、§12.1-B26、§11 阶段表 P1 行；临时日志 `data/_a3_h14*.log` 与 `data/_a3_h14*.csv` 已清理（未入库）。

### funding 采集接回调度 + 修复 `--incremental` 潜伏缺陷（B26 修复）（2026-10-01，本次提交）

来源：上节 A3 复盘发现 `biz.funding_rate_hist` 停在 09-17（停滞 14 天）。用户确认「按你的意思办」→ 执行 A3 复盘列出的行动项①。**唯一涉及生产的改动 = 新增一个调度条目**（无 DDL、无线上阈值改动）。

- **根因（两层）**：① `phase_backfill_funding_history.py` 只挂工作台任务表 `scan_funding_backfill`（`default_args=["--full"]`），**未注册到 `scheduler.py`** ⇒ 只能人工点、无自动保鲜；② 更隐蔽的是 **`--incremental` 路径本身是坏的**——`main()` 在线程池内逐符号调 `get_last_funding_time(conn, sym)`，**6 个抓取线程共用同一条 psycopg 连接**，连接被打死。实测：`[funding] 完成 185 符号，13407 条，失败 6` 之后 `psycopg.OperationalError: the connection is closed`，**upsert 一条没写进去**（`MAX(funding_time)` 仍是 09-17）。⇒ 与 B26 的停滞互为因果：即使有人想接自动化，增量路径也跑不通。
- **改动**：
  1. `workbench/scheduler.py`：新增 `scan_funding_backfill`，cron **`40 2 * * *`**（北京 02:40），脚本 `phase_backfill_funding_history.py`，args `["--incremental"]`，category `core`。避开 01:00 周日 OI 回填、03:05 CVD 就绪检查、06:30~09:00 邮件/早报高峰；8h 结算点、日频增量正常滞后 ≤3h。
  2. `scripts/bin/phase_backfill_funding_history.py`：`get_last_funding_time(conn, sym)`（逐符号、线程内调用）→ **`get_last_funding_times(conn, symbols)`（批量、线程启动前一次取回）**，`_run()` 不再碰 DB，线程内只发 HTTP。
  3. `scripts/bin/check_scan_freshness.py`：新增常量 `FUNDING_MAX_AGE_H=24` + `_collect_funding_health(conn)`，挂进既有 `health_notes`（复用 `squeeze_health` 去重键与每小时那次检查）。**刻意不进 `items` 的「停摆」分支**——funding 是 8h 结算点、不驱动实时扫描，进停摆分支会让邮件误报「主池/蓄势池扫描已无法产出有效信号」。连带把健康分节标题/邮件主题/引导语从「轧空池/采样」泛化为「轧空池/采样/费率采集」「采集缺口」。
- **验证**：`py_compile` 三个文件通过；`check_scan_freshness.py --dry-run` **修复前**正确报出「funding 费率已停更：最新结算点 09-17 13:00（北京时间），距今 337h（阈值 24h）」；`--incremental` 补 **14,046 行**、`MAX(funding_time)` → **2026-10-01 06:00 UTC**、191 符号全部新鲜；**重跑幂等（0 条新增）**；`--dry-run` **修复后** funding 项不再出现（转正常），其余观测项与既有行为不变。
- **影响**：§8 funding 消融 / §4.3 B12 费率拥挤度标签 / A4「补费率成本项」的数据面恢复；**funding 消融的 train/test 对照从「无法做」变为「可做」**（下次 A3 复跑补上，本次未重跑）。调度生效需容器重建（push 后约 6 分钟）。
- **未动**：线上扫描逻辑/阈值/`scan_daemon`/DB 结构一律未改；`SQUEEZE_ALERT_SHADOW` 影子模式维持原状（看门狗仍在提示，非本轮范围）。
- **产物**：文档 §12.1-B26 改写为「发现 + 同日修复」（含③潜伏缺陷与验证数）、§12.1-A3 (c) 标注已修复；AGENTS.md 本节。
