# 工单 SCAN-REGIME-GATE-001：regime 闸门因果性验证

> 日期：2026-09-29
> 提出背景：阈值 sweep holdout 双侧矩阵（commit `34a4297`）实证 **P↑OI↑ 24/24 组 test 侧全负、样本内选参反向**（train 最优 6.0/1.5 +8.5% = test 最差 −7.4%，t=−4.73）——固定 % 阈值标定死局，出路指向 regime 闸门。本工单验证规则 B 的 `regime_label` 是否具备**信号级**因果预测力。
> 前置文档：`盘面异动扫描系统设计方案.md` §12.1-A3/规则 B、§14（v1.2）

---

## 1. 结论先行（本工单要回答什么）

**问题**：`classify_regime` 打出的 trend/range/mixed 标签，与信号（P↑OI↑ / P↑OI↓ 等）在**该标签日入场**的实际收益之间，是否存在可被闸门利用的稳定关系？

**判据（预登记，防事后挑数）**：
- 若「非 trend regime 下 P↑OI↑ 的 PF 显著更差（train/test 两侧方向一致、test 侧 PF 差 ≥0.3）」成立 → regime 闸门有因果价值，进入下一工单（线上降权影子）；
- 若各 regime 下收益无显著差异（t<2 且方向不一致）→ 标签只配当日报描述语，**不得**用于任何降权/暂停动作，本工单关闭并记入 §12.1；
- 任何结论一律标 `provisional_单regime`：45 天窗内 regime 分布未知，若 trend 占比 >80% 则标签本身分辨力不足，验证结论降级为「不可判定」。

## 2. Scope

### IN（做）
1. 新增只读分析脚本 `scripts/bin/backtest_regime_gate.py`：
   - 从 `biz.asset_klines`（BTC，1h）逐日重算 `btc_amp_pct` / `btc_1h_gt05_ratio` / `btc_chg_pct` → 复刻 `classify_regime` 打标签（**逐字复刻判据**，含 2026-09-23 修正的 trend 第二分支）；
   - 复用 `backtest_scan_scenarios.py` 的 `scan_symbol` 信号机制，信号入场日关联当日 regime 标签；
   - 输出：regime × 场景 × 持有期的收益矩阵（train/test 双侧），CSV 落 `scripts/data/backtest_regime_gate.csv`；
   - 附标签一致性核对：与 `scan_edge_daily` 已有 ~7 天真实标签逐日比对，不一致即 FAIL。
2. 设计方案 §12.1 规则 B 条目追加「信号级验证工单」指针。

### OUT（不做，显式边界）
- ❌ 不改 `scan_daemon.py` / `build_scan_edge_report.py` / 任何线上调度与告警代码；
- ❌ 不改 `classify_regime` 判据本身（哪怕发现阈值可优化——那是验证通过后的下一工单）；
- ❌ 不做任何线上降权/暂停动作的落地；
- ❌ 不连写库：全流程只读 prod（SELECT），产物只落本地 CSV。

## 3. PR 级改动清单

| 文件 | 改动 | 性质 |
|---|---|---|
| `scripts/bin/backtest_regime_gate.py` | 新增，~250 行：标签重算 + 信号 join + 分组统计 + CSV 输出 | 新文件 |
| `04_架构与代码方案/盘面异动扫描系统设计方案.md` | §12.1-A3 追加工单指针（≤5 行） | 文档 |

## 4. 验收（三通道）

| 通道 | 验收项 | 方法 |
|---|---|---|
| ① 源码核验 | `classify_regime` 复刻与原实现逐分支一致（trend 两分支、range 双条件、mixed 兜底缺数据） | 代码 diff 对照 `build_scan_edge_report.py:154-175` |
| ② 线上 runtime | 标签一致性核对：重算标签 ∩ `scan_edge_daily.regime_label` 重叠日 **不一致 = 0**；脚本对 prod 只读（代码审查无 INSERT/UPDATE/DDL） | 直连 prod SELECT + 比对输出 |
| ③ 注入测试 | 合成 K 线构造三类日（振幅 5% 占比 40% / 振幅 2% 占比 10% / 介于其间），标签输出 trend/range/mixed 各就各位；缺数据日落 mixed | 沙盒合成数据断言 |

## 5. 铁律

- **未改码未落库**：本工单交付前不改任何线上代码；验证结论无论正负，均不触发 `scan_daemon` 行为变更；
- 产物结论一律 `provisional`，引用数字时必须带 train/test 双侧与 n 值；
- 临时文件（/tmp 日志、__pycache__）随手清理；提交只含 §3 清单文件，并发会话改动不碰。

## 6. 风险与预期管理

1. **样本窗内 regime 分布未知**（大概率 trend 占绝对多数）——若标签几乎恒为 trend，本工单结论是「不可判定」而非「无效」，需积累跨 regime 数据后重跑（脚本设计为可重入，参数化日期窗）；
2. 标签是**日级**的、信号是小时级的——日内 regime 切换粒度不足是已知局限，验证结论只能回答「日级闸门是否有戏」，不能回答「小时级闸门」；
3. 同日标签 + 同日收益存在**同期相关 ≠ 因果**的风险：预登记判据（§1）+ train/test 双侧一致性是本次能做到的最强防事后挑数手段，彻底因果性仍需线上前向样本（≥30 天，日历阻塞）。

## 7. 工时预估

纯只读分析 + 新单文件脚本：**0.5~1 天**（含三通道验收）。无线上部署环节。
