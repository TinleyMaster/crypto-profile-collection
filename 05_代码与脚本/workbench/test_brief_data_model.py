"""
brief_data_model 单元测试

运行: python test_brief_data_model.py
"""
import sys
import os

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

from brief_data_model import (
    normalize_brief,
    check_brief_health,
    _normalize_transfer,
    _normalize_holder_item,
    _normalize_unlock_item,
    _normalize_stablecoin,
)

passed = 0
failed = 0


def assert_eq(actual, expected, name):
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        print(f"    期望: {expected}")
        print(f"    实际: {actual}")


# ════════════════════════════════════════════════════════════
# 1. 大额转账字段映射
# ════════════════════════════════════════════════════════════
print("\n【测试1】大额转账 value_usd 字段归一化")

# 旧字段 amount_usd → 标准字段 value_usd
t_old = {"amount_usd": 1000000, "from_address": "0x123"}
t_norm = _normalize_transfer(t_old)
assert_eq(t_norm["value_usd"], 1000000, "amount_usd → value_usd 映射")
assert_eq(t_norm["amount_usd"], 1000000, "原 amount_usd 字段保留（兼容）")

# 已是标准字段，不重复处理
t_new = {"value_usd": 5000000}
t_norm2 = _normalize_transfer(t_new)
assert_eq(t_norm2["value_usd"], 5000000, "已有 value_usd 保持不变")


# ════════════════════════════════════════════════════════════
# 2. 持仓集中度巨鲸变化字段映射
# ════════════════════════════════════════════════════════════
print("\n【测试2】持仓集中度 whale_balance_change_7d_pct 字段归一化")

h_old = {"symbol": "BTC", "whale_change_pct": 5.2}
h_norm = _normalize_holder_item(h_old)
assert_eq(h_norm["whale_balance_change_7d_pct"], 5.2,
          "whale_change_pct → whale_balance_change_7d_pct 映射")
assert_eq(h_norm["whale_change_pct"], 5.2, "原 whale_change_pct 字段保留")

h_new = {"symbol": "ETH", "whale_balance_change_7d_pct": -3.1}
h_norm2 = _normalize_holder_item(h_new)
assert_eq(h_norm2["whale_balance_change_7d_pct"], -3.1, "已有标准字段保持不变")


# ════════════════════════════════════════════════════════════
# 3. 解锁事件字段映射
# ════════════════════════════════════════════════════════════
print("\n【测试3】解锁事件字段归一化")

u_old = {"symbol": "APT", "amount": 1000000, "date": "2024-01-15",
         "value_usd": 8500000}
u_norm = _normalize_unlock_item(u_old)
assert_eq(u_norm["unlock_amount"], 1000000, "amount → unlock_amount 映射")
assert_eq(u_norm["unlock_date"], "2024-01-15", "date → unlock_date 映射")
assert_eq(u_norm["unlock_value_usd"], 8500000, "value_usd → unlock_value_usd 映射")
# 原字段保留
assert_eq(u_norm["amount"], 1000000, "原 amount 字段保留")
assert_eq(u_norm["date"], "2024-01-15", "原 date 字段保留")


# ════════════════════════════════════════════════════════════
# 4. 稳定币 7日变化金额自动计算
# ════════════════════════════════════════════════════════════
print("\n【测试4】稳定币 change_7d_usd 自动计算")

s = {"total_usd": 150000000000, "change_7d_pct": 2.5}
s_norm = _normalize_stablecoin(s)
expected = round(150000000000 * 2.5 / 100, 2)  # 3,750,000,000
assert_eq(s_norm["change_7d_usd"], expected,
          "change_7d_usd 自动计算 = total_usd * change_7d_pct / 100")

# 已有 change_7d_usd 不覆盖
s2 = {"total_usd": 100e9, "change_7d_pct": 1.0, "change_7d_usd": 999}
s_norm2 = _normalize_stablecoin(s2)
assert_eq(s_norm2["change_7d_usd"], 999, "已有 change_7d_usd 不覆盖")


# ════════════════════════════════════════════════════════════
# 5. 完整 normalize_brief 集成测试
# ════════════════════════════════════════════════════════════
print("\n【测试5】normalize_brief 集成（多模块）")

raw = {
    "status": "ok",
    "M0_tldr": {"btc_price": 60000, "total_market_cap": 2.5e12, "fear_greed": 65},
    "M2_whale_moves": {
        "status": "ok",
        "transfers": [
            {"amount_usd": 1000000, "symbol": "BTC"},  # 旧字段
            {"value_usd": 5000000, "symbol": "ETH"},   # 新字段
        ]
    },
    "M2_holder_concentration": {
        "most_concentrated": [
            {"symbol": "SOL", "whale_change_pct": 10.5},  # 旧字段
        ],
        "whale_buying": [],
        "whale_selling": [],
    },
    "M2_stablecoin": {"total_usd": 150e9, "change_7d_pct": 2.5},
    "M6_upcoming_unlocks": {
        "unlocks": [
            {"symbol": "APT", "amount": 1000000, "date": "2024-01-15",
             "value_usd": 8500000},
        ]
    },
    "narrative_flow": {
        "ranked": [
            {"name": "DeFi", "whale_change_pct": 3.2},  # 旧字段
        ]
    },
}

brief = normalize_brief(raw)

# 验证所有模块都被正确处理
assert_eq(brief["status"], "ok", "顶层字段保留")
assert_eq(brief["M2_whale_moves"]["transfers"][0]["value_usd"], 1000000,
          "大额转账 - 旧字段映射成功")
assert_eq(brief["M2_whale_moves"]["transfers"][1]["value_usd"], 5000000,
          "大额转账 - 新字段保持")
assert_eq(brief["M2_holder_concentration"]["most_concentrated"][0]
          ["whale_balance_change_7d_pct"], 10.5,
          "持仓集中度 - 旧字段映射成功")
assert_eq(brief["M2_stablecoin"]["change_7d_usd"],
          round(150e9 * 2.5 / 100, 2),
          "稳定币 - 7日变化金额计算")
assert_eq(brief["M6_upcoming_unlocks"]["unlocks"][0]["unlock_amount"],
          1000000, "解锁 - amount 映射")
assert_eq(brief["narrative_flow"]["ranked"][0]
          ["whale_balance_change_7d_pct"], 3.2,
          "叙事榜 - 旧字段映射成功")


# ════════════════════════════════════════════════════════════
# 6. 健康度检查
# ════════════════════════════════════════════════════════════
print("\n【测试6】数据健康度检查")

# 全正常的 brief
good_brief = normalize_brief(raw)  # 复用上面的 raw
health = check_brief_health(good_brief)
print(f"  健康度评分: {health['score']}/100")
print(f"  核心缺失: {health['critical']}")
print(f"  辅助缺失: {health['warning']}")
assert health["score"] >= 64, f"预期健康度>=64（4核心全ok+部分辅助），实际{health['score']}"
assert len(health["critical"]) == 0, "核心模块应全部正常"

# 全坏的 brief
bad_brief = {"status": "error"}
health_bad = check_brief_health(bad_brief)
print(f"  坏数据健康度: {health_bad['score']}/100")
print(f"  坏数据核心缺失数量: {len(health_bad['critical'])}")
assert health_bad["score"] < 30, f"预期健康度<30，实际{health_bad['score']}"
assert len(health_bad["critical"]) > 0, "应有核心字段缺失"


# ════════════════════════════════════════════════════════════
# 总结
# ════════════════════════════════════════════════════════════
print(f"\n{'='*50}")
print(f"结果: {passed} 通过, {failed} 失败")
print(f"{'='*50}")

sys.exit(0 if failed == 0 else 1)
