# 验收：自动跟单双账户实盘执行 COPYTRADE-MULTI-001（2026-11-28）

## 结论先行

双账户跟单**已实现并推 main**。两个子账户的凭证已落本地 `.env`（gitignore 覆盖，绝不入库/不回显）。执行层**复用系统已有的 `BinanceFuturesClient`**（HMAC 签名+时间校准+LOT_SIZE 精度+双开模式检测），不另造轮子。

**默认 paper**。我没有测连接（按主人指示我的 IP 达不到）、没有开实盘、没有替你做任何交易决策。启用实盘的动作（`CT_MODE=paper` → `live`）留给你。

## 背景与需求

- 两个子账户需要跟同一交易员（UID `5075281354358777856`）
- 参照系统中已实现的开仓代码 → 定位到 `scripts/src/crypto_research/clients/binance_futures.py::BinanceFuturesClient`
- 不测连接（IP 达不到）

## 根因：单账户架构无法直接支持双账户

原代码是**单账户 + 单决策**：
- `Config` 只有一组 `CT_API_KEY`/`CT_API_SECRET`
- `DecisionEngine` 用全局 `cfg.allocated_capital`
- `exec_order` 表无账户列，`client_order_id` 不含账户名 → 双账户同信号会撞主键
- `follow_signal.dedup_key` UNIQUE → 双账户同成交撞键
- 敞口/持仓查询不按账户过滤 → A 账户仓位被 B 账户占用

另有**隐藏坑**：`Config.load()` 只读 `os.environ`，从不读 `.env` 文件——用户把凭证写进 `.env` 也不生效。

## Changes（6 文件 + 1 测试）

| 文件 | 改动 |
|---|---|
| `config.py` | 加零依赖 `_load_dotenv()`（修 .env 不生效）；`AccountConfig` 数据结构；`CT_ACCOUNT_N_*` 解析；`active_accounts` 属性；validate 按账户校验 |
| `executor.py` | 去掉 ccxt，改复用 `BinanceFuturesClient`；`client_order_id` 含账户名；`build_account_executors` 多账户工厂；可注入 mock client（离线测真实下单逻辑） |
| `decision.py` | `DecisionEngine` 加 `allocated_capital` 覆盖，4 处资金闸门改用账户自身资金 |
| `daemon.py` | `__init__` 每账户独立决策引擎+执行器；核心循环按账户扇出 L3→L4；AUM 广播到所有账户；`_my_position`/`_exposure` 加账户隔离 |
| `store.py` | `exec_order`/`follow_signal` 加 `account` 列 + ALTER 兼容旧库；`insert_exec_order`/`insert_signal` 写账户；按账户查询方法 |
| `.env.example` / `README.md` | 多账户配置模板 + 运行说明 + 实盘启用前置条件 |

## 验收（三通道，非自报）

### ① 源码核验
- `BinanceFuturesClient` 从 copytrade 可 import ✅
- `Config._parse_accounts()` 实测解析出 2 账户（key 长度 64、capital 1000）✅
- 凭证全仓搜索：仅 `.env` 命中，且被 gitignore；待提交文件零明文 ✅

### ② 单测（42 passed，原 34 + 新增 8）
新增 8 项锁死多账户坑：
- 配置解析（遇空 key 停止）
- 双账户执行器/决策引擎独立构造
- `client_order_id` 跨账户唯一（撞车会致主键冲突）
- exec_order 账户隔离（A 查不到 B）
- signal dedup_key 账户前缀（撞 UNIQUE）
- live 复用 `BinanceFuturesClient`（非 ccxt）
- live 下单参数：开多 `positionSide=LONG`、平多 `reduceOnly`+反向 `SELL`
- 凭证不进日志（`safe_name` 只返账户名）

### ③ 真实数据回放（274 条真实成交，paper）
```
fetched=274, signal=274, ready=269, approved=464, executed=464, hold=5
  sub1: signals=269 approved=261 exec_orders=232
  sub2: signals=269 approved=261 exec_orders=232
重复 client_order_id=0 / 跨账户 cid 重叠=0 / 重复 signal dedup_key=0
```
两账户完全对称、零串账、零幂等冲突。`totalPnl=0` 的部分平仓判为 REDUCE（状态机修复在真实数据上再次成立）。

- 编译：`compileall` 全通过

## Scope 边界（先不管）

- **实盘执行未验证**：live 代码按币安规则与系统已有客户端写对、单测覆盖参数构造，但**没有真实 API 调用验证**（按指示不测连接 + 未获开实盘授权）。这部分算「代码修复完成」，不算「实盘验证通过」。
- 不连 prod DB：当前仅本地 SQLite。
- 不挂额外止损止盈：跟单镜像交易员仓位，交易员平仓才平（与策略开仓 `open_position` 语义不同）。

## 启用实盘前置（待主人拍板）

1. 确认两个子账户 API 权限**只开合约交易、不开提现**
2. 确认策略期望收益（原方案全程未给收益率/夏普/回撤）
3. 确认每账户资金规模（`CT_ACCOUNT_N_CAPITAL`）
4. 改 `CT_MODE=live`，建议先 `--once` 单轮观察日志

## commit

`393fe6c` — fix: 实现双账户自动跟单-复用BinanceFuturesClient/账户隔离/env加载修复(42单测+274条真实回放双账户对称验证)
