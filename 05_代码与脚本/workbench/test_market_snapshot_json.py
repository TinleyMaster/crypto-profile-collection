#!/usr/bin/env python3
"""build_market_snapshot raw_payload JSON 序列化回归护栏（2026-09-30）。

运行：python test_market_snapshot_json.py

背景：market_daily 任务的 `market_snapshot` 子步骤整跑 FAIL（exit 1）——
`upsert_snapshot` 直接 `json.dumps(overview)`，而 overview 含 DB 取出的
NUMERIC（psycopg → Decimal）与日期对象，触发
`TypeError: Object of type Decimal is not JSON serializable`。
修复：json.dumps 传 `default=_json_default`（Decimal→float / 时间→ISO / 兜底 str）。

⚠️ 纯离线：假 conn，不连库、不打网络。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "scripts", "bin")
for p in (_BIN, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import build_market_snapshot as B  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \u2713 {name}")
    else:
        failed += 1
        print(f"  \u2717 {name}")
        if detail:
            print(f"    {detail}")


class _Cur:
    def __init__(self):
        self.sql = None
        self.vals = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, vals=None):
        self.sql = sql
        self.vals = vals


class _Conn:
    def __init__(self):
        self.cur = _Cur()

    def cursor(self):
        return self.cur

    def commit(self):
        pass


print("== 1. _json_default 类型转换 ==")
check(B._json_default(Decimal("1.5")) == 1.5, "Decimal → float")
check(isinstance(B._json_default(Decimal("1.5")), float), "Decimal 转成 float（JSON 数值）")
check(B._json_default(Decimal("0")) == 0.0, "Decimal 0 → 0.0")
check(B._json_default(datetime(2026, 9, 29, 1, 2, 3)) == "2026-09-29T01:02:03",
      "datetime → ISO 字符串", str(B._json_default(datetime(2026, 9, 29, 1, 2, 3))))
check(B._json_default(date(2026, 9, 29)) == "2026-09-29", "date → ISO 字符串")
check(B._json_default({1, 2}) == "{1, 2}", "未知类型兜底 str（不抛）")

print("== 2. json.dumps(..., default=_json_default) 不再炸 ==")
payload = {
    "btc": {"price": Decimal("83631.20"), "dom": Decimal("54.31")},
    "date": date(2026, 9, 29),
    "ts": datetime(2026, 9, 29, 6, 30, 0, tzinfo=timezone.utc),
    "list": [Decimal("1"), Decimal("2.5")],
}
_s = json.dumps(payload, ensure_ascii=False, default=B._json_default)
_back = json.loads(_s)
check(_back["btc"]["price"] == 83631.20 and _back["btc"]["dom"] == 54.31,
      "Decimal 嵌套转成 JSON 数值", str(_back["btc"]))
check(_back["date"] == "2026-09-29", "date 可解析")
check(_back["list"] == [1.0, 2.5], "Decimal 列表转数值", str(_back["list"]))

# 反例：无 default 时确实会炸（证明测试有判别力）
try:
    json.dumps({"x": Decimal("1")})
    _raised = False
except TypeError:
    _raised = True
check(_raised, "对照：无 default 时 Decimal 抛 TypeError（非空转）")

print("== 3. upsert_snapshot 落库 raw_payload 为合法 JSON ==")
conn = _Conn()
B.upsert_snapshot(conn, "2026-09-29", {"btc_price": 1.0, "fear_greed_value": 73},
                  payload)
raw_val = conn.cur.vals[-1]
check(isinstance(raw_val, str), "raw_payload 位置是字符串（已 dumps）", type(raw_val).__name__)
_parsed = json.loads(raw_val)
check(_parsed["btc"]["price"] == 83631.20, "落库 JSON 可反序列化且数值正确", str(_parsed.get("btc")))
check(conn.cur.sql is not None and "raw_payload" in conn.cur.sql, "SQL 含 raw_payload 列")

print("== 4. 源码护栏：dumps 必须带 default ==")
_src = open(os.path.join(_BIN, "build_market_snapshot.py"), encoding="utf-8").read()
_dumps_line = [ln for ln in _src.splitlines() if "json.dumps(raw_payload" in ln]
check(bool(_dumps_line), "找到 raw_payload 的 dumps 调用")
check(all("default=_json_default" in ln for ln in _dumps_line),
      "raw_payload 的 dumps 全部带 default=_json_default", str(_dumps_line))
check("from decimal import Decimal" in _src, "模块已导入 Decimal")
check("def _json_default" in _src, "存在 _json_default 兜底函数")

print("== 5. extract_snapshot 对含 Decimal 的 overview 不炸 ==")
_ov = {
    "dimensions": {
        "2盘面": {"data": {"btc": {"price": Decimal("83631.2"), "change_24h_pct": Decimal("1.2")},
                           "eth": {"price": Decimal("2500"), "change_24h_pct": None}}},
        "1体量": {"data": {"total_market_cap": Decimal("2900000000000"),
                           "btc_dominance": Decimal("54.3"), "total_volume_24h": Decimal("1")}},
        "3情绪": {"data": {"fear_greed": {"value": Decimal("73")}}},
    },
    "btc_cycle": {"phase": "early_top", "phase_label": "顶部风险（早期）"},
}
_snap = B.extract_snapshot(_ov)
check(_snap["btc_price"] == 83631.2 and _snap["fear_greed_value"] == 73,
      "extract_snapshot 正确解析 Decimal 输入", str(_snap["btc_price"]))
check(_snap["total_market_cap_usd"] == 2900000000000.0, "大额 Decimal 市值解析")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
