# 工单 SCAN-BRK-SHORT-OFF-001：BRK 空头停推（影子抑制）

> 日期：2026-10-01
> 提出背景：§8.1.10 只读追查实证 **BRK 空头 130 笔已结算、24h 净 −3.353%、胜率 26.9%、按日聚类 t=−2.06**，
> 且剔最大批次（n=38，−2.205%）与同 bar 去重（n=28 bar，−2.535%）后仍为负 —— 本项目迄今**唯一 |t|>2 的负面结论**。
> 前置文档：`盘面异动扫描系统设计方案.md` §8.1.7 / §8.1.8 / §8.1.9 / §8.1.10

---

## 1. 结论先行（本工单要回答什么）

**问题**：既然 BRK 空头有统计上站得住的**负期望证据**，是否应停止向用户发信？

**预登记判据（本工单开工前已锁定，§8.1.10 五 已逐条满足）**：

| 条件 | 要求 | 实测 | 满足 |
|---|---|---|---|
| ① 统计显著 | 按日聚类 \|t\| ≥ 2 | t = **−2.06**（7 个独立日） | ✅ |
| ② 非单批次伪影 | 剔除最大 1h 批次后净仍 < 0 | n=38，**−2.205%** | ✅ |
| ③ 非重复计数伪影 | 同 bar 去重后净仍 < 0 | n=28 bar，**−2.535%** | ✅ |
| ④ 样本底线 | 独立日 ≥ 5 且已结算 ≥ 50 笔 | 7 日 / 130 笔 | ✅ |

**处置方式（推荐「影子抑制」而非删除信号）**：BRK 空头**继续落库**（`biz.scan_signal` 留痕、
JSON 留档、`signal_ts/p_dir/symbol` 齐全），**但不进告警邮件**，并以既有
`alert_suppressed_at` / `alert_suppressed_reason` 两列留痕（复用
`scan_daemon.py::_mark_alert_suppressed`，该机制已服务「跨池互斥」）。

**为什么不是删除、也不是改判据**：
- 不删除 ⇒ 前向样本继续积累，随时可复评（§5）；
- 不改判据 ⇒ 避免重蹈 A3 / `SCAN-REGIME-GATE-001` / `SCAN-DIR-GATE-001` **三次样本内选参翻车**
  （§8.1.10 行动项① 已明令禁止「BTC 跌 >0.5% 就不发空头」这类阈值修补）。

## 2. Scope

### IN（做）
1. `scan_daemon.py`：
   - 新增模块级常量 `SUPPRESS_BRK_SHORT = True`（与 `LIQ_FILTER_ENABLED` 同款式，单行回滚）；
   - 在 `task_scan_alert` 的候选过滤循环内新增**单点**判定：候选满足
     `pool='accumulation' AND scenario='BRK' AND p_dir='down'` 且 `SUPPRESS_BRK_SHORT` ⇒
     调 `_mark_alert_suppressed(conn, [c["id"]], "BRK空头停推(SCAN-BRK-SHORT-OFF-001)：§8.1.10 负期望证据")`
     并 `continue`，不进本封邮件；
   - 返回值新增 `suppressed_brk_short` 计数，便于线上核验（与既有 `suppressed_cross_pool` 并列）。
2. 文档：§8.1.10 行动项② 补「已开工单」指针；AGENTS.md 追加一节。

**已核实无需改动的两处看门狗**（开工前查证，避免顺手改坏）：
- `scan_daemon._stall_parts`（丢信号检测，候选集恰为 `pool='main' OR (pool='accumulation' AND scenario='BRK')`）
  已排除 `alert_suppressed_at IS NOT NULL` 的行 ⇒ 抑制行**不会**被误计入「丢信号」；
- `check_scan_freshness._collect_squeeze_health`（输出面静默检测）作用域是 `pool='squeeze'`
  ⇒ 与 BRK 无关，**不误报**。

### OUT（不做，显式边界）
- ❌ 不改 `detect_brk` 判据、不改 `BRK_VOL_RATIO`、不改 `_build_regime` 的 `long_fav/short_fav` 语义；
- ❌ **不引入任何新阈值**（BTC 涨跌幅门槛、量比门槛、OI/CVD 确认维等一律不做 —— 属 §8.1.10 行动项① 禁区）；
- ❌ 不删 `biz.scan_signal` 已有 BRK down 行、不改历史 `alerted_at`；
- ❌ 不改 `collect_scan_outcome.py`（结算口径保持「线上真发」纯净，见 §3 说明）；
- ❌ 不动主池 S1/S2 与 BRK **多头**（多头 n=140 亦为负 −0.719%，但样本/显著性未达 §1 判据，
  本轮**不动**，另列观察项）；
- ❌ 不做 goto/回补、不改调度周期。

## 3. PR 级改动清单

| 文件 | 改动 | 性质 |
|---|---|---|
| `scripts/bin/scan_daemon.py` | `+ SUPPRESS_BRK_SHORT` 常量；`task_scan_alert` 循环内 +1 判定分支（复用 `_mark_alert_suppressed`）；返回值 +1 计数 | 逻辑 |
| `04_架构与代码方案/盘面异动扫描系统设计方案.md` | §8.1.10 行动项② 补指针（≤3 行） | 文档 |
| `AGENTS.md` | 追加一节 | 文档 |

**为什么不改 `collect_scan_outcome.py`**：其 `CAND_SQL` 以 `alerted_at IS NOT NULL` 为候选条件，
被抑制行 `alerted_at` 恒为 NULL ⇒ 自然地**不进 `scan_signal_outcome`**，日报（`build_scan_edge_report`
从 `scan_signal_outcome` 取数）与 §8.1.7「线上真发的告警」样本面**保持纯净、不受污染**。
前向复评不需要 outcome 表 —— `biz.asset_klines` 常驻，复评时按 `signal_ts` 现算即可（见 §5）。

## 4. 验收（三通道）

| 通道 | 验收项 | 方法 |
|---|---|---|
| ① 源码核验 | 抑制判定**单点**（grep 全仓无第二处 BRK-down 发信/放行路径）；`SUPPRESS_BRK_SHORT=False` 时行为与改前逐字节一致 | 代码审查 + 该常量两态各跑一轮 dry-run |
| ② 线上 runtime（观察 ≥3 天） | ⓐ 邮件中 BRK down 卡片数 = **0**；ⓑ `alert_suppressed_reason LIKE 'BRK空头停推%'` 的行数 = 同期 `scan_signal` 里 BRK down/high 的新增行数（**抑制 ≠ 丢失**）；ⓒ 日报 `alerts_n` 与 §8.1.7 口径**不因本改动而失真**；ⓓ `check_scan_freshness` 无因本 reason 产生的误报 | 直连 prod 只读比对 |
| ③ 注入测试 | 合成三类候选：BRK `down`+high ⇒ **不发**且留痕；BRK `up`+high ⇒ **照发**；`main/S1` up+high ⇒ **照发** | 沙盒合成候选断言 |

## 5. 复评与解禁判据（预登记，防事后挑数）

**复评触发**：影子期累积 **≥ 30 个独立交易日**且 BRK 空头前向样本 **≥ 50 笔**（按
`scan_signal` 内 BRK down/high 行计，口径与本工单 §1 一致）。

**复评口径（只用 `biz.scan_signal` + `biz.asset_klines`，不依赖 outcome 表）**：
`signal_ts` 为该信号基准时刻，`signal_ts + 24h` 取该币该周期最后一根 K 线收盘价，
按 `p_dir='down'` 反号对齐，扣双边 taker 0.1%（与 `collect_scan_outcome.COST` 同口径）。

**解禁条件（三条**全部**满足才恢复发信）**：
1. 前向样本按日聚类 **t > 0**；
2. 前向样本 24h 净均 **> +0.20%**（覆盖双边手续费并留余量）；
3. 剔除最大单日批次后仍为正。

**反向升级条件**：若影子期发现 BRK 多头同样满足 §1 四条判据，另开同型工单（本工单**不**顺带处理多头）。

## 6. 铁律

- 本工单只做「不发信」，**不改判据、不加阈值、不改结算**；任何阈值型修补另行预登记 + holdout；
- 抑制必须留痕（`alert_suppressed_at/reason`），禁止静默丢弃；
- 回滚 = 单常量置 `False`，无 DB 变更、无数据补写（因为 `scan_signal` 与 `asset_klines` 一直在）；
- 临时文件随手清理；提交只含 §3 清单文件，并发会话改动不碰。

## 7. 风险与预期管理

1. **`alerted_at` 语义变化**：被抑制行的 `alerted_at` 恒为 NULL，语义从「已处理」收窄为
   「已发信」——这正是本工单想要的（保持样本面纯净），但任何**按 `alerted_at IS NULL` 找
   「漏发」**的既有巡检须复核（通道 ②ⓓ）；
2. **本轮不产生「赚」**：本工单只止损（停止亏），不产生正期望；多头侧的 −0.719%（n=140）
   仍是未收敛的观察项；
3. **行情依赖风险**：若影子期市场转为持续单边下跌，BRK 空头可能事后看是对的 ——
   但预登记判据（§5）**不接受事后行情解释**，只接受前向样本，且解禁门槛设为「净 > +0.20%」
   而非「不亏」，以覆盖成本；
4. **样本量**：停推后前向样本累积速度取决于市场事件频率（当前 13 天仅 7 个空头独立日），
   30 个独立日可能需要数月 —— 这是**已知且接受**的等待成本。

## 8. 工时预估

代码改动 ≤ 30 行（含注释）+ 三通道验收 + 文档：**0.5 天**。线上观察期 3 天（阻塞、不占工时）。

---

## 9. 附录：开工前基线（供验收比对，§8.1.10 实录）

| 项 | 值 |
|---|---|
| BRK 空头 已结算 / 总量 | 130 / 246 |
| 24h 净均 / 超额 / 胜率 | −3.353% / −3.513pp / 26.9% |
| 按日聚类（7 日） | −1.824%，t = −2.06 |
| 剔 09-23T14 批次 | n=38，−2.205%，胜率 47.4% |
| 同 bar 去重 | n=28 bar，−2.535%，胜率 42.9% |
| 触发时 BTC 1h < −0.5% 组 | n=94，−3.987%，胜率 18.1% |
| 同期 `main/S1 up` 对照 | n=347，+0.050% |
| 同期 BRK `up` 对照 | n=140，−0.719% |

---

## 10. 实施记录（2026-10-01，已落地，待线上观察）

**已改文件（单个）**：`scripts/bin/scan_daemon.py`

| 落点 | 内容 |
|---|---|
| 常量区（`CROSS_POOL_MUTE_MIN` 之后） | `SUPPRESS_BRK_SHORT = True` + `SUPPRESS_BRK_SHORT_REASON`（附依据长注释：§8.1.10 数字、三条缺陷、禁用阈值型修补、回滚方式） |
| `_is_brk_short(c)`（`_mark_alert_suppressed` 之前） | 纯谓词，不碰 DB/全局状态；抽函数只为满足通道③ 注入测试并使「单点」可 grep 核验 |
| `task_scan_alert` 候选过滤循环内 | `if _is_brk_short(c): _mark_alert_suppressed(...); suppressed_brk_short += 1; continue`（置于跨池互斥之前） |
| `task_scan_alert` 返回值 | 两处携带抑制计数的 return 各增 `suppressed_brk_short` |

**验收进度（§4 三通道）**：

| 通道 | 状态 | 结果 |
|---|---|---|
| ① 源码核验 | ✅ 已完成 | `py_compile` 通过；`grep` 确认全仓 `_load_alert_candidates` **仅 1 处调用**（`task_scan_alert`）、`UPDATE ... alerted_at` **仅 1 处**（同函数成功分支）⇒ 无第二处 BRK-down 发信/放行路径；`SUPPRESS_BRK_SHORT` 全仓 3 处（定义 1 + 谓词 1 + 调用 1） |
| ③ 注入测试 | ✅ 已完成 | 6 组断言全 PASS：态 1（默认 True）BRK `down` ⇒ 抑制、BRK `up` ⇒ 放行、`main/S1 up` ⇒ 放行、`main/S2 down` ⇒ 放行、ACC `flat` ⇒ 放行；态 2（置 `False` 回滚）**全部放行**；空 dict 不抛异常 |
| ② 线上 runtime | ⏳ 待观察 ≥3 天 | 需核对：ⓐ 邮件 BRK down 卡片数 = 0；ⓑ `alert_suppressed_reason LIKE 'BRK空头停推%'` 行数 = 同期 BRK down/high 新增行数（抑制 ≠ 丢失）；ⓒ 日报 `alerts_n` 口径不失真；ⓓ 无看门狗误报 |

**未做**：未连 prod 执行 `--run-once alert`（该命令会真实发信且需占用单实例锁，生产 daemon 在跑），
通道 ② 改为部署后按上表只读比对。

**回滚**：`SUPPRESS_BRK_SHORT = False` 单行即可，无 DB 变更、无需补数据。

**注**：Zeabur 容器约 6 分钟后自动重建上线，通道 ② 的观察窗口自该时点起算。
