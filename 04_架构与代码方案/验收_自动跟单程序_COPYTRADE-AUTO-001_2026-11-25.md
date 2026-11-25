# 验收报告 — COPYTRADE-AUTO-001 自动跟单程序实现

> 日期：2026-11-25 · 状态：**代码已实现、离线验收通过、实盘未启用**
> 铁律遵守：未改任何既有代码、未连 prod DB、未动任何凭证、未下任何真实单。
> 工作目录：`05_代码与脚本/copytrade/`（独立子包，与 workbench/scripts 零交叉）

---

## 一、结论先行

方案可实现，已落地为可运行代码。三个硬伤全部修复并有单测/回放物证；默认 paper 模式，无凭证也能安全跑全链路。

**未闭环项**：H1/H2 的真实样本验证（本机出口 IP 被币安 bapi 边缘风控 302，抓不到），需女王大人本地浏览器抓包后放入 `fixtures/` 回放。实盘 live 模式预留但未启用。

---

## 二、交付物清单

| 文件 | 作用 |
|---|---|
| `copytrade/config.py` | 配置 + 风控闸门；D1=A1主+A2对账 / D2=B2 / D3=C3；live 需显式开关+凭证 |
| `copytrade/source.py` | L1 三源互备 + 退避 + 302风控识别 + 双时钟健康 + ReplaySource |
| `copytrade/state_machine.py` | L2 三级判据状态机（P1显式字段/P2双向/P3单向），totalPnl 仅旁证 |
| `copytrade/decision.py` | L3 B2 映射 + 三道闸 + 降级杠杆 + 减仓同比例映射 |
| `copytrade/executor.py` | L4 paper 默认 + live 预留（ccxt，不启用） |
| `copytrade/store.py` | SQLite 先存后算可重放；双时钟 sync_state |
| `copytrade/daemon.py` | 主循环 --once/--loop/--replay/--health |
| `copytrade/sql/schema.sql` | Postgres 版 schema（自建库备用，不连 prod） |
| `copytrade/fixtures/sample_001.json` | 离线回放样本（含 totalPnl=0 部分平仓坑位） |
| `copytrade/tests/` | 23 个单测 |
| `copytrade/README.md` | 使用说明 + H1/H2 本地闭环指引 |

---

## 三、三个硬伤修复对照

| # | 原方案缺陷 | 修复 | 物证 |
|---|---|---|---|
| ① | 数据源单点、bapi 私有接口可变、15s 轮询易 ban | 三源互备封装单点替换；退避 2/4/6/8/16s；302/202/418 边缘风控识别；allow_redirects=False | `source.py:_fetch_with_retry` / `check_health` |
| ② | watchdog 18h 无新单判据失效（他本就 20h 不交易） | 双时钟：数据源看 `last_success_fetch_at`，交易员看 `last_trade_at` 仅提示；`--health` 仅数据源故障退出码=1 | `store.py:mark_fetch_success/fail` + `daemon.py:run_health` |
| ③ | H2 `totalPnl==0` 猜开平，部分平仓误判 | 三级判据；totalPnl 冲突降级；翻转 HOLD 挂起；FIFO 配对 | `tests/test_state_machine.py` 12 项全过 |

回放中额外发现并修复的 P0 级坑：
- 减仓原样照搬交易员 qty → **巨量平仓单** → 改为 B2 同比例映射
- clamp 基准误用交易员镜像仓位 → 改为我方 exec_order 累计持仓
- 我方持仓方向误用成交方向（SELL≠做空）→ 改为 action 后缀方向

---

## 四、验收结果（三通道）

| 通道 | 项目 | 结果 |
|---|---|---|
| 源码 | `python -m compileall -q .` | ✅ exit 0 |
| 源码 | 单测 `python -m pytest tests/ -q` | ✅ **23 passed**（状态机12 + 决策执行9 + daemon口径2） |
| 运行 | 离线回放 `CT_REPLAY_MODE=true python daemon.py --replay` | ✅ 4 成交 → 4 信号 → 3 执行 + 1 正确拒单；镜像净持仓归零、已实现盈亏 250（模拟） |
| 运行 | 部分平仓 totalPnl=0 判向 | ✅ 判为 REDUCE_LONG（非旧规则的开仓），回放物证 |
| 运行 | 减仓 clamp | ✅ 0.05 → 0.002381（我方持仓上限），无超量平仓 |
| 安全 | paper 默认、live 缺凭证拒启 | ✅ 单测 `test_paper_never_uses_live` + `test_live_requires_explicit_mode` |
| DB | 回放 SQLite 表 6 张 | ✅ 先存后算，行数与信号/订单一致 |
| 网络 | 真实 bapi 取数 | ❌ 本机 302→binance.com（边缘风控），非代码问题；转 fixtures 回放 |

---

## 五、待拍板 / 未闭环

| # | 事项 | 状态 |
|---|---|---|
| D4 | 分配资金规模 | 默认 1000 USDT（paper），实盘值待定 |
| D5 | 部署位置 | 未部署；程序可本地 venv 跑 |
| D6 | 先模拟2周 | ✅ 已按 paper 默认实现，建议实盘前先跑满 |
| H1 | position-history 是否含未平仓 | ❌ 需本地抓包样本 |
| H2 | 三级判据对真实112笔准确率 | ❌ 需本地抓包样本回放 |
| LIVE | live 实盘 | ❌ 预留未启用，需显式授权+testnet验证 |
| 面板 | Flask 状态面板 | ❌ 未做（当前 --health JSON 替代） |

**网络实测记录**：`api.binance.com/ping` 经代理 7890 → 200；`bapi.binance.com` 全部端点 → 302→`https://www.binance.com/en`（nginx，边缘风控），直连超时。故真实样本无法在本机获取。

---

## 六、Scope（先不管）

- 不动任何既有代码文件、不连 prod、不碰凭证、不下真实单
- 不做低延迟 / WebSocket / 多交易员 / 策略复刻
- 未验证该交易员策略的期望收益（收益率/夏普/回撤数据原方案就缺）

---

## 七、下一步（需女王大人拍板）

1. 本地浏览器抓 `order-history` / `position-history` 样本 → `fixtures/` → 跑回放闭环 H1/H2
2. 决定是否给分配资金规模（D4）与部署位置（D5）
3. paper 跑满观察期后，再独立授权 live + testnet 验证
