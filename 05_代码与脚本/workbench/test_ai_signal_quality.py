"""
AI 信号分析数据质量函数单测（2026-09-23 审计 P1 / 复验盲区 1）。

覆盖：
- _sanitize_json_control_chars：字符串内字面控制符转义、已转义序列保留、字符串外空白不动
- _sanitize_unescaped_quotes：字符串内未转义直引号补转义、合法 JSON 恒为空操作
- extract_json_from_llm_response：坏 JSON（字面换行/tab/未转义直引号）能解析，正常 JSON 不回归
- _normalize_ai_decision：score 阈值强制 should_risk/should_highlight + override 标记
- _to_storable_json：落库前清洗（复验 P1），保证 sys.ai_trace.raw_response 列内可 JSON 校验

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
    _sanitize_unescaped_quotes,
)
from ai_signal_analyzer import _normalize_ai_decision, _to_storable_json  # noqa: E402


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

print("== _sanitize_unescaped_quotes（未转义直引号，复验 P1） ==")
# 1) 中文强调里的散落直引号 -> 补转义，整体可解析且文本无损
q1 = '{"comment": "属"强而透支"结构，需警惕。"}'
out_q1 = _sanitize_unescaped_quotes(q1)
_check("散落直引号被转义", json.loads(out_q1)["comment"] == '属"强而透支"结构，需警惕。')
# 2) 合法 JSON 恒为空操作（含空串、含已转义 \\"）
_check("合法 JSON 空操作（普通值）", _sanitize_unescaped_quotes('{"a": "x"}') == '{"a": "x"}')
_check("合法 JSON 空操作（空串值）", _sanitize_unescaped_quotes('{"a": ""}') == '{"a": ""}')
_check("合法 JSON 空操作（已转义 \\\"）", _sanitize_unescaped_quotes('{"a": "x\\"y"}') == '{"a": "x\\"y"}')
_check("合法 JSON 空操作（多键缩进）", _sanitize_unescaped_quotes('{\n  "a": 1,\n  "b": [2]\n}') == '{\n  "a": 1,\n  "b": [2]\n}')
# 3) 收尾引号后跟结构分隔符不被误判
q3 = '{"a": "x", "b": "y"}'
_check("收尾引号不误判", _sanitize_unescaped_quotes(q3) == q3)

print("== extract_json_from_llm_response（坏 JSON 修复） ==")
bad = '{\n  "should_risk": true,\n  "overall_score": 38,\n  "score_card": {"valuation": {"score": 45, "comment": "稳定币\n无估值意义（字面换行）\t带tab"}}\n}'
r = extract_json_from_llm_response(bad)
_check("坏 JSON（字面换行/tab）可解析", r["overall_score"] == 38)
_check("comment 内容保留", "无估值意义" in r["score_card"]["valuation"]["comment"])
ok = '{"should_highlight": true, "overall_score": 71, "score_card": {"valuation": {"score": 55}}}'
_check("正常 JSON 不回归", extract_json_from_llm_response(ok)["overall_score"] == 71)
bad_q = '{"should_risk": true, "overall_score": 44, "reason_summary": "属"强而透支"结构,需警惕"}'
rq = extract_json_from_llm_response(bad_q)
_check("坏 JSON（未转义直引号）可解析", rq["overall_score"] == 44)
_check("直引号内容无损保留", rq["reason_summary"] == '属"强而透支"结构,需警惕')

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

print("== _to_storable_json（落库前清洗，复验 P1） ==")
# 1) 字面控制符（Invalid control character）-> 转义后可解析，内容保真
raw_nl = '{\n  "overall_score": 38,\n  "reason_summary": "稳定币\n无估值意义\t带tab"\n}'
st1 = _to_storable_json(raw_nl)
_check("字面换行/tab 清洗后可 JSON 校验", json.loads(st1)["overall_score"] == 38)
_check("清洗后内容保真", json.loads(st1)["reason_summary"] == "稳定币\n无估值意义\t带tab")
# 2) 本身合法（带缩进）-> 原样返回，不重排格式
ok_pretty = '{\n  "should_highlight": true,\n  "overall_score": 71\n}'
_check("合法 JSON 原样透传（零改动）", _to_storable_json(ok_pretty) == ok_pretty)
# 3) markdown 代码块包裹 -> 解析后落规范 JSON
fenced = '```json\n{"overall_score": 55, "should_risk": false}\n```'
_check("代码块包裹可落库", json.loads(_to_storable_json(fenced))["overall_score"] == 55)
# 4) JSON 后多尾巴（Extra data）-> 解析后落规范 JSON
extra = '{"overall_score": 62}\n\n以上为分析结论，仅供参考。'
_check("JSON 后多尾巴可落库", json.loads(_to_storable_json(extra))["overall_score"] == 62)
# 5) 完全不可解析 -> 保留原文（不丢数据、不阻断写入）
garbage = "<<<<<<< 这不是 JSON >>>>>>>"
_check("不可解析时保原文", _to_storable_json(garbage) == garbage)
# 6) 空串不炸
_check("空串原样返回", _to_storable_json("") == "")

print("\nALL PASS")