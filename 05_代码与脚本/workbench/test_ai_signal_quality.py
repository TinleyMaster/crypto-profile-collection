"""
AI 信号分析数据质量函数单测（2026-09-23 审计 P1 / 复验盲区 1）。

覆盖：
- _sanitize_json_control_chars：字符串内字面控制符转义、已转义序列保留、字符串外空白不动
- extract_json_from_llm_response：坏 JSON（字面换行/tab）能解析，正常 JSON 不回归
- _normalize_ai_decision：score 阈值强制 should_risk/should_highlight + override 标记

运行: python test_ai_signal_quality.py
"""
import sys
import os

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
sys.path.insert(0, os.path.join(os.path.dirname(_here), "scripts", "src"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crypto_research.clients.llm_client import (  # noqa: E402
    extract_json_from_llm_response,
    _sanitize_json_control_chars,
)
from ai_signal_analyzer import _normalize_ai_decision  # noqa: E402


def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"  ✓ {name}")


print("== _sanitize_json_control_chars ==")
# 1) 字符串内字面换行 -> 转义为 \\u000a，且整体可被 json.loads
s = '{"a": "line1\nline2"}'
out = _sanitize_json_control_chars(s)
_check("字面换行被转义", "\\u000a" in out)
import json
_check("转义后可解析且内容保留", json.loads(out)["a"] == "line1\nline2")
# 2) 已转义序列不被双重转义
s2 = '{"a": "line1\\nline2"}'
out2 = _sanitize_json_control_chars(s2)
_check("已转义\\n保留", json.loads(out2)["a"] == "line1\nline2")
# 3) 字符串外合法空白不动
s3 = '{\n  "a": 1\n}'
out3 = _sanitize_json_control_chars(s3)
_check("字符串外空白不动", out3 == s3)
# 4) 字面 tab 转义
s4 = '{"a": "x\ty"}'
_check("字面tab转义", "\\u0009" in _sanitize_json_control_chars(s4))

print("== extract_json_from_llm_response（坏 JSON 修复） ==")
bad = '{\n  "should_risk": true,\n  "overall_score": 38,\n  "score_card": {"valuation": {"score": 45, "comment": "稳定币\n无估值意义（字面换行）\t带tab"}}\n}'
r = extract_json_from_llm_response(bad)
_check("坏 JSON（字面换行/tab）可解析", r["overall_score"] == 38)
_check("comment 内容保留", "无估值意义" in r["score_card"]["valuation"]["comment"])
ok = '{"should_highlight": true, "overall_score": 71, "score_card": {"valuation": {"score": 55}}}'
_check("正常 JSON 不回归", extract_json_from_llm_response(ok)["overall_score"] == 71)

print("== _normalize_ai_decision（判定阈值后处理） ==")
cases = [
    ({"overall_score": 30, "should_risk": True}, {"should_risk": True, "_risk_forced_off": None}),
    ({"overall_score": 51, "should_risk": True}, {"should_risk": False, "_risk_forced_off": 51.0}),
    ({"overall_score": 40, "should_highlight": True}, {"should_highlight": False, "_highlight_forced_off": 40.0}),
    ({"overall_score": 60, "should_highlight": True}, {"should_highlight": False, "_highlight_forced_off": 60.0}),
    ({"overall_score": 70, "should_highlight": True}, {"should_highlight": True, "_highlight_forced_off": None}),
    ({"overall_score": None, "should_risk": True}, {"should_risk": True}),  # score 缺失不改
]
for i, (inp, expect) in enumerate(cases, 1):
    n = _normalize_ai_decision(dict(inp))
    for k, v in expect.items():
        _check(f"case{i} {k}", n.get(k) == v)

print("\nALL PASS")