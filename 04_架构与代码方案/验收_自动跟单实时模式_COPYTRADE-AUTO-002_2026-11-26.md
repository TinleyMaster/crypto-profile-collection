# 验收报告：自动跟单实时模式 COPYTRADE-AUTO-002

- 日期：2026-11-26
- 基线上一个 commit：`c168753`（协议纠错 + 274 条真实成交离线回放）
- 本次性质：**把 paper 回放验证系统推进为真实自动运行的跟单系统**（默认 paper，不下真单）
- 结论：✅ 实时模式端到端跑通；发现并修复 4 个离线回放掩盖的 P0；34 单测通过。live 实盘未开启（须用户授权+凭证+策略期望确认）。

---

## 一、结论先行

前两轮交付的是**离线回放验证**（静态样本喂状态机），不是真正能自动运行的跟单。本轮实测 `python daemon.py --once` 第一跑就全崩，暴露四个 P0，全部修复并真实网络复验通过。

| # | P0 | 现象 | 状态 |
|---|---|---|---|
| 1 | 环境死代理覆盖 | 环境 `HTTPS_PROXY=127.0.0.1:60459`（死），覆盖 session 代理 → 12 次全 ProxyError | ✅ `trust_env=False` + 代理候选池轮换 |
| 2 | `indexValue` 方向搞反 | 昨天当"增量续传游标"写进代码；实测带游标拿到的是**更早**订单 → 热运行会漏新单 | ✅ 改为"最新一页 + dedup"，游标只用于冷启动回填 |
| 3 | 冷启动缺完整性判据 | order 源失败仍标记 bootstrap_done → 永久空基线、系统空跑 | ✅ order 失败不标记、下轮重试 |
| 4 | 对账覆写毁状态机 | position-history 全是已平仓，拿 `closedVolume` 当净持仓覆写镜像 → 偏差 5 亿 | ✅ 对账改只读，无实时字段时跳过（H1 结论落地到代码） |

附带修复：进程重启后镜像仓位全丢（`_rebuild_state` 从 raw_order 重建）、kill switch 硬编码（文件哨兵 + CLI）、live 执行层四个真实下单坑（reduceOnly / BOTH / 杠杆 / 精度）、`iter_new_raw_orders` SQL 引用不存在的 `id` 列。

---

## 二、证据（真实网络，非自报）

### 2.1 冷启动（全新 data 目录）
```
源 order:   OK rows=274   （4 页历史回填，total=278，4 条重复去重）
源 position: OK rows=30
源 detail:   OK rows=1
健康: source_healthy=True / leader_idle=False (2.34h)
交易员 AUM = 8,144,315.39
[rebuild] 从 274 条历史成交重建 16 个镜像仓位
[bootstrap] 回填 274 条，本轮不下单
DB: raw_order=274 follow_signal=0 exec_order=0 mirror_position=16 bootstrap_done=1
```

### 2.2 增量（新进程模拟重启）
```
[rebuild] 从 274 条历史成交重建 16 个仓位
源 order: OK rows=100   （只拉最新一页，不是全量）
stats: fetched=0 signal=0 executed=0   ← 历史单全部被 dedup 挡住，零重复处理
对账: position-history 全为已平仓 → 跳过对账，镜像保持不变
```

### 2.3 熔断（运行时，无重启）
```
$ python daemon.py --kill   → 🛑 熔断已开启
下一轮: 🛑 检测到熔断哨兵，拒绝所有新单（approved=0 executed=0）
$ python daemon.py --unkill → ✅ 熔断已解除
```

### 2.4 全量验收
- `python -m pytest tests/ -q` → **34 passed**（原 23 + 新增实时模式 11）
- `python -m compileall -q .` → OK
- 测试锁住：对账不覆写、bootstrap 完整性、kill switch、状态重建、`trust_env=False`、live 下单参数

---

## 三、根因分析

1. **代理**：本机环境注入 `HTTPS_PROXY=60459`（已死），requests 的 session 级 proxies 被环境覆盖 → 必须 `trust_env=False`。7890 本身也会间歇 502，需退避而非快速轮换。
2. **游标方向**：币安 `lead-portfolio/order-history` 的 `indexValue` 是历史方向翻页游标——page1 最新=11-26 07:56，带游标 page2 最新=11-24 10:02。昨天把它当增量游标是方向性错误。
3. **冷启动语义**：离线回放一次性喂全部样本，不需要"首次 vs 后续"区分；实时运行必须有 bootstrap 阶段建立基线，且失败不能带病启动。
4. **对账数据性质**：H1 昨天已确认 position-history 全是已平仓历史，但代码里 `reconcile_from_position` 还在拿它覆写镜像——结论没落到代码，本轮补上。

---

## 四、改动清单（PR 级）

| 文件 | 改动 |
|---|---|
| `source.py` | `trust_env=False`；代理候选池+轮换；`_request_once` 拆分；`indexValue` 改为仅 bootstrap 历史翻页；热运行只拉最新页；限流专项退避 |
| `daemon.py` | 冷启动 bootstrap 阶段（只回填不执行）；order 失败不标记完成；`_rebuild_state` 重启恢复；`_check_kill_switch` 文件哨兵；`--kill/--unkill` CLI；`fetch_all(bootstrap=)` |
| `state_machine.py` | `reconcile_from_position` 改只读：无实时净持仓字段时跳过，绝不覆写镜像 |
| `store.py` | 新增 `meta` 表 + `get_meta/set_meta/mark_bootstrap_done`；修 `iter_new_raw_orders` 的 `id` 列 bug |
| `executor.py` | 新增 `build_order_params` 纯函数（reduceOnly/BOTH/杠杆）；LiveExecutor 加杠杆设置+数量精度 |
| `config.py` | 新增 `proxy_candidates/page_size/bootstrap_max_pages` 及 env 映射 |
| `sql/schema.sql` | Postgres 端同步 meta 表 |
| `.env.example` | 新增代理池/分页配置说明 |
| `deploy/supervisor_copytrade.conf` | 常驻运行配置（新增） |
| `tests/test_live_mode.py` | 11 项实时模式回归测试（新增） |
| `README.md` | 运行方式/Scope 更新 |

---

## 五、验收清单（三通道）

- [x] **源码**：逐文件核验改动落地，`compileall` 通过
- [x] **runtime（真实网络）**：冷启动 274 条回填 + 增量零重复 + 熔断生效
- [x] **DB**：raw_order=274 / mirror=16 / bootstrap_done=1 / exec_order=0（paper 未下单）
- [x] **注入测试**：34 单测全过，含对账覆写、bootstrap 完整性、kill switch、状态重建

---

## 六、Scope 边界（先不管）

- **live 实盘未开启**：`CT_MODE=paper` 默认。开实盘需女王大人三确认：①API 凭证自填 ②策略期望收益（该交易员收益率/夏普/回撤，原方案一个字没有）③D4 资金规模。AI 不代开、不碰凭证。
- **Postgres 后端**：`CT_DATABASE_URL` 仍 NotImplementedError（D5 待拍板），当前仅本地 SQLite，绝不连 prod。
- **Flask 观测面板**：未做，先用 DB 查询 + 日志。
- **Wall 时钟对账**：H1 已确认接口无实时仓位，A2 只能历史复盘；若需实时对账须换数据源（如 uapi 账户接口）。

---

## 七、运行方式

```bash
cd 05_代码与脚本/copytrade
pip install -r requirements.txt
cp .env.example .env          # 按需调整
python daemon.py --once       # 首轮自动冷启动回填，不下单
python daemon.py --loop       # 常驻（supervisor 见 deploy/）
python daemon.py --health     # watchdog（数据源故障退出码=1）
python daemon.py --kill       # 运行时熔断
python daemon.py --unkill     # 解除熔断
python -m pytest tests/ -q    # 34 项测试
```

---

## 八、待拍板

1. D4 资金规模（当前 paper 默认 1000 USDT）
2. D5 部署位置（当前本地 SQLite + supervisor 配置模板）
3. live 实盘三确认（凭证 / 策略期望 / 资金）
4. H1 实时对账替代数据源（若需要）
