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
- **结论**：判定门与执行层**无缺陷**，机制稳定 ⇒ **但仍不能开总开关**（`SIGNAL_TRADE_ENABLED=0` 保持）。下一步：继续 dry-run 攒 confirmed 收益样本，且**先解决「横截面相关 ⇒ 有效样本数 ≪ 条数」的统计口径**，否则攒够 10 个日历日也不等于 10 个独立样本。

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
- **待部署**：早报由 scheduler 一次性脚本发送，下次调度（每日 09:00 CST）即生效，无需常驻容器重启。
