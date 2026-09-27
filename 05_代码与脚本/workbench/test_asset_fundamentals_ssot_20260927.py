#!/usr/bin/env python3
"""工单 SSOT-001 · 代币基本面统一事实源（三端同源消费）。

背景：改造前代币基本面有 4 个组装点且口径漂移——
  ① get_asset_tokenomics() 字段最全但无逐字段来源/时点；
  ② 投研结论 prompt 的 inline _fund 只吃 lp_locked / contract_renounced /
     buy_tax_pct / sell_tax_pct ⇒ 库里已有的 分配/销毁/排放/通胀/治理/用途
     从未进入投研结论主线；
  ③ 解锁测算 prompt 另起一套 raw SQL 吃 10 个字段，并复制了一份 CMC supply 校验；
  ④ 页面各渲染一个子集，research.html 还对长文本做 slice(0,120) 截断。

运行：python workbench/test_asset_fundamentals_ssot_20260927.py（纯离线，不连库、不连网）

覆盖：
  1  SSOT 契约：字段信封 5 键、四态 missing_reason、coverage 与缺失清单自洽
  2  来源标注：supply 与 CMC 权威值一致 → cmc_quote_snapshot，否则 biz.asset_tokenomics
  3  缺失语义：行缺失 / 字段缺失 → not_collected；有重试留痕 → fetch_failed；超阈值 → stale
  4  三端同源：投研 _fund 与解锁 tkn 均取自 SSOT；解锁侧不再有独立 raw SQL 与重复 CMC 校验
  5  投研 prompt：规则 14 已扩项（allocation / emission_schedule / inflation_info /
     burn_info / governance_info / utility_info）
  6  页面：research.html 去掉 slice(0,120) 截断、消费 meta 信封；index.html 补 inflation_info
     与覆盖率页脚；两页字段清单与 db_stats._FUND_TOKENOMICS_FIELDS 一致
  7  API：两个 tokenomics 端点均返回 meta，且 data 结构不变（老前端兼容）
  8  边界留档：不改采集脚本；get_asset_tokenomics 签名/返回结构未变
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))          # 05_代码与脚本/workbench
_ROOT = os.path.dirname(_HERE)                              # 05_代码与脚本
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, os.path.join(_SCRIPTS, "src"))
sys.path.insert(0, _HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import db_stats as ds  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f"  → {detail}" if detail else ""))


def _read(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError as e:
        return f"__READ_ERROR__ {e}"


_DS_SRC = _read(os.path.join(_HERE, "db_stats.py"))
_APP_SRC = _read(os.path.join(_HERE, "app.py"))
_RESEARCH_HTML = _read(os.path.join(_HERE, "templates", "research.html"))
_INDEX_HTML = _read(os.path.join(_HERE, "templates", "index.html"))

_TS_OK = datetime.now(timezone.utc).isoformat()
_TS_OLD = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()


def _tok_base(**overrides):
    """构造一份 get_asset_tokenomics 的返回（仅含 SSOT 会读取的键）。"""
    tok = {
        "total_supply": 1_000_000_000,
        "circulating_supply": 250_000_000,
        "max_supply": None,
        "buy_tax_pct": None,
        "sell_tax_pct": None,
        "tax_info": None,
        "lp_locked": True,
        "contract_renounced": False,
        "lp_lock_info": None,
        "allocation": [{"category": "Team", "pct": 20}],
        "emission_schedule": "48 个月线性释放",
        "inflation_info": None,
        "burn_info": None,
        "governance_info": "持币可投票",
        "utility_info": "支付 Gas 与质押",
        "confidence": 0.86,
        "extraction_notes": None,
        "source_urls": ["https://example.com/tokenomics"],
        "updated_at": _TS_OK,
    }
    tok.update(overrides)
    return tok


def _patch(tok, retry=None, cmc=None, cmc_time=None, gh=None, tvl=None):
    """把 SSOT 的 5 个外部依赖替换为离线桩，避免连库。"""
    ds.get_asset_tokenomics = lambda aid: tok
    ds._fetch_tokenomics_retry_meta = lambda aid: (retry or {})
    ds._fetch_cmc_supply_baseline = lambda aid: (cmc or {}, cmc_time)
    ds._fetch_github_activity = lambda aid: (gh or [])
    ds._fetch_dl_tvl = lambda aid: tvl


CMC_SAME = {"total_supply": 1_000_000_000, "circulating_supply": 250_000_000, "max_supply": None}

print("── 1. SSOT 契约（字段信封 / 四态 / coverage 自洽）──")
_patch(_tok_base(), cmc=CMC_SAME, cmc_time="2026-09-27T00:00:00Z")
_f = ds.get_asset_fundamentals(11114)
check(set(_f.keys()) >= {"asset_id", "fields", "coverage", "tokenomics_row_exists",
                         "confidence", "source_urls", "assembled_at"},
      "顶层返回含 asset_id/fields/coverage/confidence/source_urls/assembled_at", sorted(_f.keys()))
check(_f["tokenomics_row_exists"] is True, "行存在 → tokenomics_row_exists=True")
check(_f["confidence"] == 0.86, "行级置信度透传（0.86）", _f["confidence"])
check(all(set(v.keys()) == {"value", "source", "as_of", "confidence", "missing_reason"}
          for v in _f["fields"].values()),
      "每个字段信封恰含 value/source/as_of/confidence/missing_reason")
_missing_calc = sorted(k for k, v in _f["fields"].items() if v["value"] is None)
check(sorted(_f["coverage"]["missing"]) == _missing_calc,
      "coverage.missing 与「value is None」完全一致", f'{_f["coverage"]["missing"]} vs {_missing_calc}')
check(_f["coverage"]["present"] + len(_f["coverage"]["missing"]) == _f["coverage"]["total"],
      "present + missing == total")
check(_f["coverage"]["total"] == len(ds._FUND_TOKENOMICS_FIELDS) + 2,
      "total = tokenomics 字段数 + github + defillama_tvl",
      f'{_f["coverage"]["total"]} vs {len(ds._FUND_TOKENOMICS_FIELDS) + 2}')
check(_f["fields"]["allocation"]["missing_reason"] is None,
      "有值字段 missing_reason 为 None", _f["fields"]["allocation"]["missing_reason"])
check(_f["fields"]["max_supply"]["missing_reason"] == ds.FUND_MISSING_NOT_COLLECTED,
      "字段缺失 → not_collected", _f["fields"]["max_supply"]["missing_reason"])
check(_f["fields"]["lp_locked"]["value"] is True and _f["fields"]["contract_renounced"]["value"] is False,
      "布尔字段原值透传（True/False 不被吞成 None）")

print("\n── 2. 来源标注（CMC 权威 vs 代币经济学库）──")
check(_f["fields"]["total_supply"]["source"] == "cmc_quote_snapshot",
      "总量与 CMC 一致 → 来源 cmc_quote_snapshot", _f["fields"]["total_supply"]["source"])
check(_f["fields"]["total_supply"]["as_of"] == "2026-09-27T00:00:00Z",
      "CMC 来源时点用快照 quote_time", _f["fields"]["total_supply"]["as_of"])
check(_f["fields"]["governance_info"]["source"] == "biz.asset_tokenomics",
      "非 supply 字段来源 biz.asset_tokenomics", _f["fields"]["governance_info"]["source"])
_patch(_tok_base(), cmc={"total_supply": 999, "circulating_supply": 250_000_000}, cmc_time=_TS_OK)
_f2 = ds.get_asset_fundamentals(11114)
check(_f2["fields"]["total_supply"]["source"] == "biz.asset_tokenomics",
      "与 CMC 不一致 → 回退 biz.asset_tokenomics", _f2["fields"]["total_supply"]["source"])

print("\n── 3. 缺失语义四态 ──")
_patch(_tok_base(), cmc=CMC_SAME,
       retry={"extract_attempts": 2, "next_retry_at": _TS_OK, "extract_status": "pending"})
_f3 = ds.get_asset_fundamentals(11114)
check(_f3["fields"]["max_supply"]["missing_reason"] == ds.FUND_MISSING_FETCH_FAILED,
      "有重试留痕且仍无值 → fetch_failed", _f3["fields"]["max_supply"]["missing_reason"])
check(_f3["fields"]["allocation"]["missing_reason"] is None,
      "fetch_failed 不污染有值字段", _f3["fields"]["allocation"]["missing_reason"])

_patch(_tok_base(updated_at=_TS_OLD), cmc=CMC_SAME)
_f4 = ds.get_asset_fundamentals(11114)
check(_f4["fields"]["allocation"]["missing_reason"] == ds.FUND_MISSING_STALE,
      "有值但超 180 天 → stale", _f4["fields"]["allocation"]["missing_reason"])
check(_f4["fields"]["allocation"]["value"] is not None,
      "stale 字段仍保留原值（页面渲染 + 陈旧告警）", _f4["fields"]["allocation"]["value"])
check(_f4["fields"]["max_supply"]["missing_reason"] == ds.FUND_MISSING_NOT_COLLECTED,
      "stale 行内缺失字段仍为 not_collected（缺失优先于陈旧）",
      _f4["fields"]["max_supply"]["missing_reason"])

_patch(None)   # 行不存在
_f5 = ds.get_asset_fundamentals(99999)
check(_f5["tokenomics_row_exists"] is False, "行不存在 → tokenomics_row_exists=False")
check(_f5["coverage"]["present"] == 0, "行不存在 → present=0", _f5["coverage"]["present"])
check(all(v["missing_reason"] == ds.FUND_MISSING_NOT_COLLECTED for v in _f5["fields"].values()),
      "行不存在 → 全字段 not_collected")

print("\n── 4. 时间解析与陈旧判定 ──")
check(ds._is_stale(_TS_OK, 180) is False, "当前时间 → 不陈旧")
check(ds._is_stale(_TS_OLD, 180) is True, "200 天前 → 陈旧")
check(ds._is_stale(None, 180) is False, "None → 不判陈旧（无时点不等于陈旧）")
check(ds._is_stale("not-a-date", 180) is False, "非法字符串 → 不判陈旧")
check(ds._parse_ts("2026-09-27T00:00:00Z") is not None, "Z 后缀可解析")

print("\n── 5. github / defillama 与 raw 摊平 ──")
_patch(_tok_base(), cmc=CMC_SAME,
       gh=[{"repo": "a/b", "stars": 10, "pushed_at": "2026-09-01T00:00:00Z"}],
       tvl={"protocol": "Pons", "tvl_usd": 1.5e6, "change_7d_pct": 3.0,
            "fetched_at": "2026-09-27T00:00:00Z"})
_f6 = ds.get_asset_fundamentals(11114)
check(_f6["fields"]["github"]["source"] == "github" and _f6["fields"]["github"]["value"],
      "github 有值时来源 github", _f6["fields"]["github"]["source"])
check(_f6["fields"]["defillama_tvl"]["source"] == "defillama",
      "defillama_tvl 有值时来源 defillama")
_raw = ds.fundamentals_raw_values(_f6)
check("allocation" in _raw and "governance_info" in _raw and "utility_info" in _raw,
      "raw 摊平后含 allocation / governance_info / utility_info（投研主线新接入项）")
check(all(v is not None for v in _raw.values()), "raw 不含 None 值（LLM 只见原值）")
check("missing_reason" not in _raw and "source" not in _raw,
      "raw 不泄漏元信息（source/missing_reason）")
check(ds.fundamentals_raw_values(None) == {} and ds.fundamentals_raw_values({}) == {},
      "fundamentals_raw_values 对 None/空 dict 安全")

print("\n── 6. 三端同源：源码护栏 ──")
check("def get_asset_fundamentals(asset_id: int) -> dict:" in _DS_SRC, "db_stats 定义 get_asset_fundamentals")
check("_fund = fundamentals_raw_values(get_asset_fundamentals(asset_id))" in _DS_SRC,
      "投研主线 _fund 改由 SSOT 装配")
check("_fund: dict = {}\n    if isinstance(tokenomics, dict):" not in _DS_SRC,
      "投研主线旧 inline _fund（只吃 4 字段）已移除")
check('_fund[_k] = tokenomics[_k]' not in _DS_SRC, "旧的 4 字段白名单赋值已移除")
check("_fv = fundamentals_raw_values(_fund)" in _DS_SRC, "解锁 prompt 改由 SSOT 取值")
check('"allocation_json": _fv.get("allocation")' in _DS_SRC, "解锁侧 allocation_json 映射自 SSOT 的 allocation")
_old_raw_sql = """SELECT total_supply, max_supply, circulating_supply,
                          allocation_json, burn_info, emission_schedule,
                          governance_info, utility_info, confidence, source_urls
                   FROM biz.asset_tokenomics WHERE asset_id = %s"""
check(_old_raw_sql not in _DS_SRC, "解锁侧旧 raw SQL（10 列）已删除")
check("auth_total" not in _DS_SRC, "解锁侧重复的 CMC supply 校验已删除（auth_total 别名不再出现）")
check("def _or_missing(key: str, value):" in _DS_SRC, "解锁 prompt 缺失项改渲染具体原因")
check("_FUND_MISSING_ZH" in _DS_SRC, "新增缺失原因中文映射")

print("\n── 7. 投研 prompt 规则 14 已扩项 ──")
_rule14 = _DS_SRC.split("14. 基本面规则", 1)[-1][:700] if "14. 基本面规则" in _DS_SRC else ""
for _k in ("allocation", "emission_schedule", "inflation_info", "burn_info",
           "governance_info", "utility_info"):
    check(_k in _rule14, f"规则 14 覆盖 {_k}")

print("\n── 8. 页面：research.html ──")
check("slice(0, 120)" not in _RESEARCH_HTML, "research.html 已无 slice(0,120) 截断")
check("renderTokenomics(data.data || {}, data.meta || null)" in _RESEARCH_HTML,
      "research.html 消费 meta 信封")
check("function renderFundMeta (meta)" in _RESEARCH_HTML, "新增卡片级缺失留痕 renderFundMeta")
check(".tm-info-rows" in _RESEARCH_HTML and ".tm-row " in _RESEARCH_HTML, "新增逐行渲染样式")
check("治理与用途" in _RESEARCH_HTML, "research.html 新增「治理与用途」分区")
check("['通胀机制', 'inflation_info']" in _RESEARCH_HTML, "通胀机制接入渲染")

print("\n── 9. 页面：index.html ──")
check("function renderTokenomics (t, meta)" in _INDEX_HTML, "index.html renderTokenomics 接受 meta")
check("if (t.inflation_info)" in _INDEX_HTML, "index.html 补通胀机制一行")
check("TKM_FUND_LABELS" in _INDEX_HTML, "index.html 新增字段中文名映射")
check("已采集 ${cov.present}/${cov.total} 项" in _INDEX_HTML, "index.html 新增覆盖率页脚")
check(_INDEX_HTML.count("renderTokenomics(res.data.data, res.data.meta)") == 1
      and "render: (d) => renderTokenomics(d.data, d.meta)" in _INDEX_HTML
      and "renderTokenomics(d.data, d.meta);" in _INDEX_HTML,
      "index.html 三处调用点均透传 meta")

print("\n── 10. 字段清单三端一致 ──")


def _js_labels(src, const_name):
    m = re.search(const_name + r"\s*=\s*\{(.*?)\};", src, flags=re.S)
    if not m:
        return set()
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1)))


_ds_fields = set(ds._FUND_TOKENOMICS_FIELDS) | {"github", "defillama_tvl"}
_res_labels = _js_labels(_RESEARCH_HTML, "const FUND_LABELS")
_idx_labels = _js_labels(_INDEX_HTML, "const TKM_FUND_LABELS")
check(_ds_fields <= _res_labels, "research.html FUND_LABELS 覆盖 SSOT 全部字段",
      sorted(_ds_fields - _res_labels))
check(_ds_fields <= _idx_labels, "index.html TKM_FUND_LABELS 覆盖 SSOT 全部字段",
      sorted(_ds_fields - _idx_labels))

print("\n── 11. API 兼容（data 结构不变，仅新增 meta）──")
check(_APP_SRC.count('return jsonify({"ok": True, "data": data, "meta": meta})') == 1,
      "/api/assets/<id>/tokenomics 返回 meta")
check('return jsonify({"ok": True, "data": data or {}, "meta": meta})' in _APP_SRC,
      "/api/research/<id>/tokenomics 返回 meta 且 data 兜底不变")
check(_APP_SRC.count('stats.get_asset_tokenomics(asset_id)') == 2,
      "两处端点仍直接读 get_asset_tokenomics（data 结构未变）")

print("\n── 12. 边界留档（本轮不做）──")
check("def get_asset_tokenomics(asset_id: int) -> dict | None:" in _DS_SRC,
      "get_asset_tokenomics 签名未变（4 个既有消费点不受影响）")
check('"allocation": row["allocation_json"],' in _DS_SRC,
      "get_asset_tokenomics 返回结构未变（allocation 键名照旧）")
_ssot_region = _DS_SRC.split("def get_asset_fundamentals(asset_id: int) -> dict:")[1] \
                       .split("def get_whitepaper_summary")[0]
check(not re.search(r"\b(INSERT INTO|UPDATE |DELETE FROM|ALTER TABLE|CREATE TABLE)\b", _ssot_region),
      "SSOT 区域为纯只读（无任何写库语句）")

print(f"\n{'=' * 60}\nPASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)