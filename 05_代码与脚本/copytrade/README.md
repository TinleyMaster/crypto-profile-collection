# COPYTRADE-AUTO-001 自动跟单程序（实现版）

> 工单来源：`C:/Users/SuperTing/Downloads/自动跟单程序设计方案.md`
> 状态：**代码已实现 + 离线回放通过；实盘未启用（paper 默认）**
> 铁律：默认 paper；live 需显式 `CT_MODE=live` + 自填凭证；程序不输出密钥、不连 prod DB。

---

## 一、三个硬伤的修复（相对原方案）

| # | 原方案缺陷 | 本实现如何修 |
|---|---|---|
| ① | 数据源单点（bapi 私有接口可变、15s 轮询易被 ban） | 三源互备（order/position/detail）封装在 `source.py` 单一替换点；退避 2/4/6/8/16s；识别 302→首页 / 202 / 418 边缘风控；`allow_redirects=False` 不盲跟 |
| ② | watchdog「18h 无新单」判据失效（他本来就 20h 不交易） | **双时钟**：数据源健康看 `last_success_fetch_at`（成功取到响应即刷新，与有无新单无关）；交易员活跃看 `last_trade_at`（仅提示，不判故障）。`--health` 退出码只在数据源故障时非 0 |
| ③ | H2 用 `totalPnl==0` 猜开平（部分平仓误判） | **三级判据**：P1 `reduceOnly/closePosition` 显式字段 → P2 双向 `positionSide` 标准语义 → P3 单向净持仓方向推断；`totalPnl` 仅作旁证、冲突降级；翻转挂起 HOLD 不执行 |

额外修复（回放中暴露）：
- 减仓必须按 B2 同比例映射，不能照搬交易员原始 qty（否则巨量平仓单）
- 减仓 clamp 到**我方实际持仓**（从 exec_order 累计），不是交易员镜像仓位
- 我方持仓方向从 `action` 后缀取，不是成交方向（SELL≠做空）

---

## 二、目录结构

```
copytrade/
├── config.py           配置 + 风控闸门（D1=A1主+A2对账 / D2=B2 / D3=C3）
├── source.py           L1 数据源（BinanceCopySource + ReplaySource）
├── state_machine.py    L2 三级判据状态机（纯函数可单测）
├── decision.py         L3 资金映射 + 闸门
├── executor.py         L4 paper/live 执行器（live 预留不启用）
├── store.py            本地 SQLite（先存后算可重放）
├── daemon.py           主循环（--once/--loop/--replay/--health）
├── sql/schema.sql      Postgres 版 schema（自建库才用，绝不连 prod）
├── fixtures/           离线样本（本地浏览器抓包后放这里验证 H1/H2）
├── tests/              23 个单测
└── .env.example        全占位符配置模板
```

---

## 三、运行

```bash
cd 05_代码与脚本/copytrade

# 0. 安装依赖（paper 模式只需 requests；pytest 仅测试用）
python -m pip install requests pytest

# 1. 离线回放（无需网络，验证状态机全流程）
python daemon.py --replay

# 2. 单测（23 项）
python -m pytest tests/ -q

# 3. 健康状态（watchdog 用；数据源故障退出码=1，交易员静默=0）
python daemon.py --health

# 4. 跑一轮真实取数（仍 paper，不下单）
python daemon.py --once

# 5. 常驻（supervisor）
python daemon.py --loop
```

配置：复制 `.env.example` 为 `.env`，按注释填。**默认全空即可安全跑 paper + replay。**

---

## 四、真实协议与 H1/H2 闭环（2026-11-26 纠错）

> ⚠️ 11-25 版本曾误判「bapi 整体 302、抓不到样本」——域名用错了。已纠正，详见
> `04_架构与代码方案/纠错_自动跟单程序_bapi协议与真实数据复验_2026-11-26.md`。

**正确协议**：域名 `www.binance.com`，参数 `portfolioId`（不是 uid）：

| 接口 | 方法 | 参数 |
|---|---|---|
| detail | GET | `?portfolioId=5075281354358777856` |
| order-history | POST JSON | `{"portfolioId":"...","pageSize":100[,"indexValue":<游标>]}` |
| position-history | POST JSON | 同上 |

真实样本已存档 `fixtures/real_20261126.json`（274 成交 / 100 仓位 / detail）。回放：
```bash
mkdir -p /tmp/ct_real && cp fixtures/real_20261126.json /tmp/ct_real/
CT_REPLAY_MODE=true CT_REPLAY_DIR=/tmp/ct_real python daemon.py --replay
```

- **H1 已闭环**：真实 position 100 条全 `All Closed`，**不含未平仓** → A2 只能做历史复盘，不能实时对账。
- **H2 已闭环（侧证）**：position 按 `(symbol,side)` 净方向与 274 条 order 回放镜像对比，**30/30 = 100% 同向**。严格逐 positionId 配对列为观察项。
- **真实字段坑**：价格是 `avgPrice`（不是 price）、AUM 是 `aumAmount`、`reduceOnly/closePosition` 真实数据里 0 条 → P2 双向判据是主力。

---

## 五、仍未做（Scope 边界）

- ❌ 未接 live 实盘（`LiveExecutor` 预留，需你显式授权 + 自填凭证 + testnet 验证）
- ❌ 未做 Flask 状态面板（原方案 L5 的 UI 部分，当前用 `--health` JSON + DB 查询替代）
- ❌ 未接 Postgres（`schema.sql` 已备，但默认 SQLite，不连 prod）
- ❌ 未解决官方席位满员（外部不可控）
- ❌ **未验证跟单策略期望收益**（这是投资判断，不是工程问题；方案里也缺收益率/夏普/回撤数据）

---

## 六、验收快照

- `python -m pytest tests/ -q` → **23 passed**
- `python -m compileall` → exit 0
- 模拟样本回放 → 4 成交 → 4 信号 → 3 执行（1 拒单），镜像净持仓归零、已实现盈亏 250 USDT
- **真实样本回放（274 条）** → 261 approved / 8 rejected（风控闸门）/ 5 HOLD（翻转保护）→ 232 笔执行；AUM 8,083,290.86；H1/H2 闭环见第四节
