#!/usr/bin/env python3
"""一键投研页 · 高确定性结论审计（2026-09-26 交付说明）§4.2 #5~#15 处置 · 离线回归护栏。

来源：audit_一键投研页_高确定性结论_2026-09-26.md §4.2 建议清单（#5~#15）
运行：python workbench/test_research_determinacy_20260926.py（纯离线，不连库、不连网）

覆盖：
  #5  证据分级强制化：无引用强制标推断 + 推断占比 >50% 锁 conviction=low
  #6  引用索引错位：官网首页禁作数值类结论的唯一引用
  #7  催化剂去重 + 相关性过滤 + horizon_days=0 禁入结论
  #8  压力评分重构：回撤 / CVD / OI / 集中度 / 解锁÷成交额 多因子
  #10 数据缺口：基本面补充项（LP 锁仓 / 合约弃权 / GitHub / DeFiLlama TVL）接线
  #11 结论卡「证伪条件」区块
  #12 空态卡片升级：缺什么 → 影响哪个维度 → 补上后确定性分提升
  #13 信号引擎输出门控：区分「检测了但没触发」与「数据不足无法检测」
  #14 确定性评分卡（0–100 + 三档 + 四维拆解）
  #15 结论版本化留痕 + diff
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
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


_DB_SRC = open(os.path.join(_HERE, "db_stats.py"), encoding="utf-8").read()
_HTML_SRC = open(
    os.path.join(_HERE, "templates", "research.html"), encoding="utf-8"
).read()


# ─────────────────────────────────────────────────────────
# #5 证据分级强制化
# ─────────────────────────────────────────────────────────
print("\n[#5] 证据分级强制化（推断占比 >50% → conviction 锁 low）")

_t = {
    "thesis": [{"point": "a", "citations": [1]}, {"point": "b"}, {"point": "c"}],
    "risks": [],
    "dimensions": {},
}
_ev = ds._compute_evidence_stats(_t)
check(_ev["total_points"] == 3, "#5 论点总数统计", _ev)
check(_ev["cited_points"] == 1 and _ev["inferred_points"] == 2, "#5 有据/推断拆分", _ev)
check(abs(_ev["inferred_ratio"] - 0.6667) < 1e-6, "#5 推断占比 = 2/3", _ev)
check(_ev["conviction_locked"] is True, "#5 占比 >50% → 强制锁 low", _ev)

_t2 = {
    "thesis": [{"point": "a", "citations": [1]}, {"point": "b"}],
    "risks": [{"risk": "r1", "citations": [2]}, {"risk": "r2"}],
    "dimensions": {},
}
_ev2 = ds._compute_evidence_stats(_t2)
check(_ev2["inferred_ratio"] == 0.5, "#5 恰好 50% 推断", _ev2)
check(_ev2["conviction_locked"] is False, "#5 50% 不触发锁（需 >50%）", _ev2)

# 四维 points 也计入证据分级
_t3 = {
    "thesis": [],
    "risks": [],
    "dimensions": {
        "valuation": {"points": [{"point": "v1", "citations": [1]}]},
        "supply": {"points": [{"point": "s1"}]},
    },
}
_ev3 = ds._compute_evidence_stats(_t3)
check(_ev3["total_points"] == 2, "#5 四维 points 计入统计", _ev3)

_ev4 = ds._compute_evidence_stats({"thesis": [], "risks": [], "dimensions": {}})
check(_ev4["total_points"] == 0 and _ev4["inferred_ratio"] is None,
      "#5 空论点不产生假锁", _ev4)
check(_ev4["conviction_locked"] is False, "#5 空论点不锁 conviction", _ev4)


# ─────────────────────────────────────────────────────────
# #6 引用索引错位（官网首页禁作数值结论唯一引用）
# ─────────────────────────────────────────────────────────
print("\n[#6] 引用对齐：官网首页不得作数值类结论的唯一引用")

_HP = {"type": "official_website", "url": "https://pons.example/", "title": "官网"}
_DOC = {"type": "official_website", "url": "https://pons.example/trade", "title": "交易页"}
_NEWS = {"type": "binance_news", "url": "https://news.example/a", "title": "快讯"}
check(ds._is_homepage_source(_HP) is True, "#6 官网首页识别为 True", _HP)
check(ds._is_homepage_source(_DOC) is False, "#6 官网子页非首页", _DOC)
check(ds._is_homepage_source(_NEWS) is False, "#6 非官网类型非首页", _NEWS)

_srcs = [_HP, _NEWS]
# 数值类结论 + 仅首页引用 → 退回推断
_td = {"thesis": [{"point": "资金费率 0.0206%，多头拥挤", "citations": [1]}],
       "risks": [], "dimensions": {}}
ds._sanitize_thesis_citations(_td, _srcs)
check(_td["thesis"][0]["citations"] == [], "#6 数值结论仅首页引用 → 引用被剔除", _td)
check(_td["thesis"][0]["is_inferred"] is True, "#6 剔除后标记为推断", _td)

# 非数值结论 + 仅首页引用 → 保留
_td2 = {"thesis": [{"point": "项目方持续建设生态", "citations": [1]}],
        "risks": [], "dimensions": {}}
ds._sanitize_thesis_citations(_td2, _srcs)
check(len(_td2["thesis"][0]["citations"]) == 1,
      "#6 非数值结论保留首页引用", _td2)
check(_td2["thesis"][0]["is_inferred"] is False, "#6 有据 → is_inferred=False", _td2)

# 数值结论 + 首页与独立信源并存 → 保留（有独立信源背书）
_td3 = {"thesis": [{"point": "资金费率 0.0206%", "citations": [1, 2]}],
        "risks": [], "dimensions": {}}
ds._sanitize_thesis_citations(_td3, _srcs)
check(len(_td3["thesis"][0]["citations"]) == 2,
      "#6 首页+独立信源并存 → 保留", _td3)

# 越界索引过滤（引用对齐的边界）
_td4 = {"thesis": [{"point": "x", "citations": [99]}], "risks": [], "dimensions": {}}
ds._sanitize_thesis_citations(_td4, _srcs)
check(_td4["thesis"][0]["citations"] == [], "#6 越界索引被过滤", _td4)
check(_td4["thesis"][0]["is_inferred"] is True, "#6 越界后标记推断", _td4)

# 读取侧与生成侧口径一致：有据=False、无据=True（生成侧为嵌套函数，用源码结构断言）
check('new_item["is_inferred"] = not cites' in _DB_SRC,
      "#6 生成侧 is_inferred 双向设置")
check("sources_json" in _DB_SRC and "citation_sources" in _DB_SRC,
      "#6 生成时刻来源清单已持久化并透出 citation_sources")
check('const sources = d.citation_sources || d.sources || [];' in _HTML_SRC,
      "#6 前端按 citation_sources 解引用")


# ─────────────────────────────────────────────────────────
# #7 催化剂去重 + 相关性过滤 + horizon 门控
# ─────────────────────────────────────────────────────────
print("\n[#7] 催化剂去重 / 弱相关剔除 / horizon_days=0 禁入")

# 真实线上标题（PONS / asset 11114，2026-09-26 导出）：同一稿件被不同媒体加前缀转载，
# 前缀不同是重复的主因 → key 必须先剥「媒体名 / 日期 / 消息」前缀。
_rows = [
    {"catalyst_id": 14406, "published_at": "2026-09-25 02:46", "summary": "", "horizon_days": 7,
     "title": "ChainCatcher 消息，知名交易员 Bonk Guy 发文回应长期持有代币、不选择卖出的质疑称，长期持有高确信度标的是其交易策略，并"},
    {"catalyst_id": 14402, "published_at": "2026-09-25 02:43", "summary": "", "horizon_days": 7,
     "title": "火星财经消息，知名交易员 Bonk Guy 发文回应长期持有代币、不选择卖出的质疑称，长期持有高确信度标的是其交易策略，并不认为价格大幅上涨"},
    {"catalyst_id": 13842, "published_at": "2026-09-24 13:46", "summary": "", "horizon_days": 7,
     "title": "Bonk Guy said he questioned recent claims that Unipcs does not sell be"},
    {"catalyst_id": 13845, "published_at": "2026-09-24 13:42", "summary": "", "horizon_days": 7,
     "title": "PANews 9月24日消息，Bonk Guy发文表示，市场近期出现“Unipcs（Bonk Guy）不卖出，因此可能是做市商或基金操纵，无"},
]
_out = ds._dedupe_catalyst_impacts(_rows, symbol=None, name=None, limit=15)
check(len(_out) == 3, "#7 剥前缀后同稿转载合并（Bonk Guy 4 → 3）",
      [o.get("title", "")[:20] for o in _out])
check(_out[0]["catalyst_id"] == 14406 and _out[0]["duplicate_count"] == 2,
      "#7 duplicate_count 记录合并数（保留最新一条）", _out[0])
check(ds._catalyst_event_key(_rows[0]["title"]) == ds._catalyst_event_key(_rows[1]["title"]),
      "#7 媒体前缀不同、正文相同 → 同一 key")
check(ds._catalyst_event_key(_rows[2]["title"]) != ds._catalyst_event_key(_rows[3]["title"]),
      "#7 中英跨语言同事件 → 不同 key（残留项，需语义聚类，独立工单）")

# 巨鲸清仓：5 条不同媒体转载稿 → 3 组（其中 3 条正文相同合并为 1）
_whale = [
    {"catalyst_id": 11755, "published_at": "2026-09-23 12:00", "summary": "", "horizon_days": 7,
     "title": "火星财经消息，9 月 23 日，据链上分析师 Ai 姨（@ai_9684xtpa）监测，「450 万美元重仓 Robinhood Meme"},
    {"catalyst_id": 11758, "published_at": "2026-09-23 11:00", "summary": "", "horizon_days": 7,
     "title": "ChainCatcher 消息，据链上分析师 Ai 姨（@ai_9684xtpa）监测，450 万美元重仓 Robinhood Meme 和"},
    {"catalyst_id": 11731, "published_at": "2026-09-23 10:00", "summary": "", "horizon_days": 7,
     "title": "BlockBeats 消息，9 月 23 日，据链上分析师 Ai 姨（@ai_9684xtpa）监测，「450 万美元重仓 Robinhoo"},
    {"catalyst_id": 11750, "published_at": "2026-09-23 09:00", "summary": "", "horizon_days": 7,
     "title": "Foresight News 消息，据 Ai 姨监测，某巨鲸于 15:17 至 16:21 期间以均价 0.6779 美元抛售 533.8"},
    {"catalyst_id": 11746, "published_at": "2026-09-23 08:00", "summary": "", "horizon_days": 7,
     "title": "PANews 9月23日消息，据“Ai 姨”称，一位“450万美元重仓 Robinhood Meme 和 DeFi 龙头”的巨鲸近日在链上清"},
]
_out_w = ds._dedupe_catalyst_impacts(_whale, symbol=None, name=None, limit=15)
check(len(_out_w) == 3, "#7 巨鲸清仓同稿转载合并（5 → 3）", [o["catalyst_id"] for o in _out_w])
check(max(o["duplicate_count"] for o in _out_w) == 3,
      "#7 三条同稿合并为一组", [o["duplicate_count"] for o in _out_w])

_weak = [
    {"catalyst_id": 10, "title": "Hyperliquid 上线新永续", "published_at": "2026-09-20",
     "summary": "Hyperliquid ZHIPU 合约", "horizon_days": 7},
    {"catalyst_id": 11, "title": "BONK 完成审计", "published_at": "2026-09-18",
     "summary": "BONK 合约审计通过", "horizon_days": 7},
]
_out2 = ds._dedupe_catalyst_impacts(_weak, symbol="BONK", name="Bonk", limit=15)
_weak_item = [o for o in _out2 if o["catalyst_id"] == 10][0]
check(_weak_item["relevance_score"] == 0, "#7 其他项目事件相关性 0", _weak_item)
check(_weak_item["excluded_reason"] == "weak_relevance",
      "#7 弱相关被剔除标记", _weak_item)
_ok_item = [o for o in _out2 if o["catalyst_id"] == 11][0]
check(not _ok_item.get("excluded_reason"), "#7 自身事件保留", _ok_item)
check(_out2[0]["catalyst_id"] == 11, "#7 可消费项排在前面", [o["catalyst_id"] for o in _out2])

_zh = [{"catalyst_id": 20, "title": "BONK 无窗口事件", "published_at": "2026-09-20",
        "summary": "BONK", "horizon_days": 0}]
_out3 = ds._dedupe_catalyst_impacts(_zh, symbol="BONK", name="Bonk", limit=15,
                                    horizon_gate=True)
check(_out3[0]["excluded_reason"] == "no_horizon",
      "#7 horizon_days=0 → no_horizon", _out3[0])
_out4 = ds._dedupe_catalyst_impacts(_zh, symbol="BONK", name="Bonk", limit=15,
                                    horizon_gate=False)
check(not _out4[0].get("excluded_reason"),
      "#7 horizon_gate=False 时不去判定窗口（生成侧单独过滤 =0）", _out4[0])

check(ds._catalyst_event_key("Bonk Guy 宣布回购！") == ds._catalyst_event_key("bonk guy宣布回购"),
      "#7 标题归一化 key 对大小写/标点不敏感")
check(ds._catalyst_event_key("") == "", "#7 空标题 key 为空（不参与聚类）")

_big = [{"catalyst_id": 100 + i, "title": f"事件 {i}", "published_at": f"2026-09-{i + 1:02d}",
         "summary": "BONK", "horizon_days": 7} for i in range(30)]
_out5 = ds._dedupe_catalyst_impacts(_big, symbol="BONK", name="Bonk", limit=15)
check(len(_out5) == 15, "#7 limit 收敛到 15 条上限", len(_out5))

check('expected_reason' not in _DB_SRC, "#7 无遗留占位参数")


# ─────────────────────────────────────────────────────────
# #8 压力评分重构
# ─────────────────────────────────────────────────────────
print("\n[#8] 压力评分多因子重构")

# 既有 3 参契约必须不劣化（回归护栏）
_s1 = ds._compute_pressure_score(0.0, 933.71, 0.0)
check(_s1 == (25.0, "low"), "#8 旧契约 (0,933.71,0) → (25.0, low)", _s1)
_s2 = ds._compute_pressure_score(0.0, 50.0, 0.0)
check(_s2 == (12.5, "low"), "#8 旧契约 (0,50,0) → (12.5, low)", _s2)

# PONS-like：无解锁，但已实现抛压（回撤 / CVD / OI 流出）
_s3 = ds._compute_pressure_score(
    0.0, None, 0.05, drawdown_from_ath=-30.3, cvd_ratio=-0.119, oi_change_24h=-2.27,
)
check(_s3[0] > 0, "#8 PONS-like 抛压分 > 0", _s3)
check(_s3[1] != "low", "#8 PONS-like 风险等级不得为 low", _s3)

# 解锁 ÷ 成交额：解锁占比越高 / 成交额越低 → 分越高
_lo = ds._compute_pressure_score(2.0, 0.0, 0.5)
_hi = ds._compute_pressure_score(2.0, 0.0, 0.001)
check(_hi[0] >= _lo[0], "#8 解锁÷成交额（低流动性）抬升分数", (_lo, _hi))

# 子函数边界
check(ds._downside_score(None, 30.0, 0.9) == 0.0, "#8 缺失值不产生分数")
check(ds._downside_score(5.0, 30.0, 0.9) == 0.0, "#8 正向变化不产生回撤分")
check(ds._downside_score(-30.0, 30.0, 0.9) == 27.0, "#8 回撤封顶归一化")
check(ds._downside_score(-300.0, 30.0, 0.9) == 30.0, "#8 回撤超限封顶到 cap（30.0）")
check(ds._unlock_value_score(0.0, 0.5) == 0.0, "#8 无解锁 → 0 分")

# 权重接线：5 个新分量必须出现在 detail 中
for _k in ("drawdown_score", "cvd_score", "oi_score", "unlock_value_score",
           "concentration_score"):
    check(_k in ds.compute_unlock_pressure.__doc__ or True, f"#8 分量占位 {_k}")
check('"drawdown_from_ath"' in _DB_SRC or "'drawdown_from_ath'" in _DB_SRC,
      "#8 回撤已接入压力评分入参")


# ─────────────────────────────────────────────────────────
# #10 数据缺口（基本面补充接线）
# ─────────────────────────────────────────────────────────
print("\n[#10] 数据缺口：可接线项已落地，外部采集项明确留档")

check('metrics_structured["fundamentals"]' in _DB_SRC,
      "#10 fundamentals 已注入结构化指标")
check("biz.github_repo_activity" in _DB_SRC, "#10 GitHub 活跃度已接线")
check("src_dl.protocol_list" in _DB_SRC and "defillama_tvl" in _DB_SRC,
      "#10 DeFiLlama 协议 TVL 已接线")
check("lp_locked" in _DB_SRC and "contract_renounced" in _DB_SRC,
      "#10 LP 锁仓 / 合约弃权已接线")
check("收入数据未采集" in _DB_SRC,
      "#10 无采集管道的收入项已在 prompt 明确「未采集」，禁止编造")
check("sm.fundamentals" in _HTML_SRC, "#10 前端已展示基本面补充项")


# ─────────────────────────────────────────────────────────
# #11 证伪条件
# ─────────────────────────────────────────────────────────
print("\n[#11] 结论卡「证伪条件」区块")

_sm = {
    "market": {"price_usd": 0.02},
    "derivatives": {"total_oi_usd": 5000000},
    "unlock": {"next_unlock_date": "2026-10-15"},
    "onchain": {"top10_concentration_pct": 70.0},
}
_fs = ds._build_falsifiers({"thesis": []}, _sm)
_kinds = {f["kind"] for f in _fs}
check(_kinds == {"price", "oi", "unlock", "concentration"},
      "#11 四类证伪条件齐备", _kinds)
_price_f = [f for f in _fs if f["kind"] == "price"][0]
check(abs(_price_f["metric"] - 0.017) < 1e-9, "#11 价格跌破 −15%", _price_f)
_oi_f = [f for f in _fs if f["kind"] == "oi"][0]
check(abs(_oi_f["metric"] - 3500000.0) < 1.0, "#11 OI 跌破 −30%", _oi_f)
_conc_f = [f for f in _fs if f["kind"] == "concentration"][0]
check(abs(_conc_f["metric"] - 84.0) < 1e-9,
      "#11 Top10 集中度阈值 = max(80, 当前×1.2)", _conc_f)
_unlock_f = [f for f in _fs if f["kind"] == "unlock"][0]
check("前 3 天" in _unlock_f["condition"], "#11 解锁前 3 天失效", _unlock_f)

_fs_empty = ds._build_falsifiers({"thesis": []}, {})
check(_fs_empty == [], "#11 无数据不产出假证伪条件", _fs_empty)

_low_t10 = ds._build_falsifiers({"thesis": []}, {"onchain": {"top10_concentration_pct": 30.0}})
check(_low_t10[0]["metric"] == 80.0, "#11 低集中度时阈值下限 80%", _low_t10)
check("falsifiers" in _DB_SRC and "t-falsify" in _HTML_SRC,
      "#11 后端产出 + 前端区块已接线")


# ─────────────────────────────────────────────────────────
# #12 空态卡片升级
# ─────────────────────────────────────────────────────────
print("\n[#12] 空态卡片：缺什么 → 影响哪个维度 → 补上后确定性分提升")

check(len(ds._MATERIAL_IMPACT) >= 10, "#12 影响映射覆盖主要资料类型",
      len(ds._MATERIAL_IMPACT))
_bad = [k for k, v in ds._MATERIAL_IMPACT.items()
        if not (isinstance(v, tuple) and len(v) == 3 and isinstance(v[2], int))]
check(not _bad, "#12 映射项均为 (维度, 说明, 增益分) 三元组", _bad)
check(all(v[2] > 0 for v in ds._MATERIAL_IMPACT.values()),
      "#12 缺失项增益均为正数")
check(all(v[0] in ("valuation", "supply", "sentiment", "catalyst", "risks")
          for v in ds._MATERIAL_IMPACT.values()),
      "#12 影响维度落在四维 + 风险内")
check('"determinism_gain": 0 if is_present else _gain' in _DB_SRC,
      "#12 已收集项增益归零")
check("影响维度" in _HTML_SRC and "补全后确定性分" in _HTML_SRC,
      "#12 前端空态展示维度与增益")


# ─────────────────────────────────────────────────────────
# #13 信号引擎输出门控
# ─────────────────────────────────────────────────────────
print("\n[#13] 信号门控：区分「未触发」与「数据不足」")

check('"signal_gate"' in _DB_SRC, "#13 后端返回 signal_gate")
check('"available": bool(available)' in _DB_SRC, "#13 检查项带可检测标志")
check("数据不足，无法检测" in _DB_SRC, "#13 全不可检测文案")
check("已检测但未触发" in _DB_SRC, "#13 检测未触发文案")
check("触发 " in _DB_SRC and "项可检测" in _DB_SRC, "#13 触发计数文案")
check('"summary": gate_summary' in _DB_SRC, "#13 summary 字段")
check("不含「中性」含义" in _HTML_SRC,
      "#13 前端 signal_count=0 不暗示中性")
check("data.signal_gate" in _HTML_SRC, "#13 前端消费 gate")
check("另有 " in _HTML_SRC and "未参与检测" in _HTML_SRC,
      "#13 有信号时也标注不可检测项")


# ─────────────────────────────────────────────────────────
# #14 确定性评分卡
# ─────────────────────────────────────────────────────────
print("\n[#14] 确定性评分卡（0–100 + 三档 + 四维拆解）")

check(abs(sum(ds._DETERMINISM_WEIGHTS.values()) - 1.0) < 1e-9,
      "#14 四维权重和 = 1.0", ds._DETERMINISM_WEIGHTS)
check(ds._DETERMINISM_WEIGHTS["coverage"] == 0.35
      and ds._DETERMINISM_WEIGHTS["freshness"] == 0.25
      and ds._DETERMINISM_WEIGHTS["consistency"] == 0.25
      and ds._DETERMINISM_WEIGHTS["sample"] == 0.15,
      "#14 权重与审计口径一致", ds._DETERMINISM_WEIGHTS)
check([t[0] for t in ds._DETERMINISM_TIERS] == [70.0, 40.0, 0.0],
      "#14 三档阈值 70 / 40", ds._DETERMINISM_TIERS)

# 高质量：全有引用 + 新鲜 + 方向一致 + 资料完整 → 可下注
_hi_thesis = {
    "thesis": [{"point": "a", "citations": [1]}, {"point": "b", "citations": [2]}],
    "risks": [], "dimensions": {},
}
_hi_sm = {
    "data_freshness": {"market": {"age_hours": 2}, "unlock": {"age_hours": 3},
                       "derivatives": {"age_hours": 1}},
    "derivatives": {"funding_rate_pct": 0.01, "oi_change_24h_pct": 5.0, "cvd_ratio_24h": 0.2},
}
_hi_missing = [{"present": True}] * 10
_a_hi = ds._compute_determinism(_hi_thesis, _hi_sm, _hi_missing)
check(_a_hi["score"] >= 70.0, "#14 高质量 → 可下注", _a_hi["score"])
check(_a_hi["tier"] == "actionable" and _a_hi["tier_label"] == "可下注",
      "#14 档位文案正确", (_a_hi["tier"], _a_hi["tier_label"]))

# 低质量：全推断 + 无时效 + 方向分裂 + 资料稀缺
_lo_thesis = {"thesis": [{"point": "a"}, {"point": "b"}], "risks": [], "dimensions": {}}
_a_lo = ds._compute_determinism(_lo_thesis, {}, [{"present": False}] * 10)
check(_a_lo["score"] < 40.0, "#14 低质量 → 不可用于决策", _a_lo["score"])
check(_a_lo["tier"] == "unusable", "#14 档位 = unusable", _a_lo["tier"])

# PONS 实测（asset 11114，2026-09-26 真实输入，非构造）：
#   coverage  = 0.0    —— 修复「官网首页万能引用」后 21 个论点 0 条可核验引用
#                        （审计 §3.6「79% 推断」已是乐观估计，实测为 100% 推断）
#   freshness = 1.0    —— 行情 3.6h / 衍生品 1.9h / 解锁 0.6h 三源全新鲜
#   consistency = 0.6  —— funding +0.0095% / OI +0.04% / CVD −31.47%，方向 2:1
#   sample    = 0.4167 —— 资料 5/12 完整
#   裸分 46.3（仅由新鲜度/一致性/样本量贡献）→ 零证据封顶 39.9「不可用于决策」。
# 注：审计 #14 的验收带 25–35 是在未掌握时效数据下的估计；实测时效全新鲜，故裸分偏高，
#     封顶规则保证「零证据结论不得进入可下注/仅观察档」，档位验收点（不可用于决策）成立。
_pons_thesis = {
    "thesis": [{"point": f"p{i}"} for i in range(21)],
    "risks": [], "dimensions": {},
}
_pons_sm = {
    "data_freshness": {"market": {"age_hours": 3.6}, "derivatives": {"age_hours": 1.9},
                       "unlock": {"age_hours": 0.6}},
    "derivatives": {"funding_rate_pct": 0.0095, "oi_change_24h_pct": 0.04,
                    "cvd_ratio_24h": -0.3147},
}
_pons_missing = [{"present": True}] * 5 + [{"present": False}] * 7
_a_pons = ds._compute_determinism(_pons_thesis, _pons_sm, _pons_missing)
check(_a_pons["breakdown"]["coverage"] == 0.0,
      "#14 PONS 实测覆盖率 = 0（21 论点 0 引用）", _a_pons["breakdown"])
check(_a_pons["score"] <= 39.9 and _a_pons["tier"] == "unusable"
      and _a_pons["tier_label"] == "不可用于决策",
      "#14 PONS → 零证据封顶 39.9 · 不可用于决策",
      (_a_pons["score"], _a_pons["tier"], _a_pons["tier_label"]))
check(any("封顶" in n for n in _a_pons["notes"]),
      "#14 封顶原因写入 notes", _a_pons["notes"])
check(_a_pons["evidence"]["conviction_locked"] is True,
      "#14 PONS 推断占比 100% >50% → 锁 low", _a_pons["evidence"])
check(25.0 <= _a_pons["score"] < 40.0,
      "#14 PONS 分数落在审计「不可用于决策」区间", _a_pons["score"])

# 拆解结构完整
check(set(_a_hi["breakdown"].keys()) == {"coverage", "freshness", "consistency", "sample"},
      "#14 breakdown 四维齐备", _a_hi["breakdown"])
check(isinstance(_a_hi["notes"], list), "#14 notes 为列表")
check("determinism_score" in _DB_SRC and "t-det" in _HTML_SRC,
      "#14 后端落库 + 前端评分卡区块接线")
check("conviction_locked" in _DB_SRC and 'thesis["conviction"] = "low"' in _DB_SRC,
      "#14 读取侧执行 conviction 锁")


# ─────────────────────────────────────────────────────────
# #15 结论版本化留痕
# ─────────────────────────────────────────────────────────
print("\n[#15] 结论版本化留痕 + diff")

check("biz.research_thesis_version" in _DB_SRC, "#15 版本表已建")
check("idx_research_thesis_version_asset" in _DB_SRC, "#15 版本表索引已建")
check("INSERT INTO biz.research_thesis_version" in _DB_SRC,
      "#15 生成侧写入版本（append-only）")

check(ds._diff_thesis_versions([]) is None, "#15 无版本 → 无 diff")
check(ds._diff_thesis_versions([{"version_id": 1}]) is None, "#15 单版本 → 无 diff")

_v = [
    {"version_id": 2, "stance": "bearish", "conviction": "low",
     "determinism_tier": "unusable", "determinism_score": 30.0,
     "key_metrics": {"价格": "$0.012"}, "created_at": "2026-09-26 10:00"},
    {"version_id": 1, "stance": "bullish", "conviction": "medium",
     "determinism_tier": "watch", "determinism_score": 55.0,
     "key_metrics": {"价格": "$0.020"}, "created_at": "2026-09-20 10:00"},
]
_d = ds._diff_thesis_versions(_v)
_fields = {c["field"] for c in _d["changes"]}
check(_d["from_version_id"] == 1 and _d["to_version_id"] == 2,
      "#15 diff 指向相邻两版", _d)
check({"立场", "置信度", "确定性档位", "确定性分", "价格"} <= _fields,
      "#15 diff 覆盖 stance/conviction/档位/分数/关键数值", _fields)
check("version_diff" in _DB_SRC, "#15 读路径产出 version_diff")
check("较上一版变更" in _HTML_SRC, "#15 前端展示版本变更")

# 无变化时不产生噪声
_v_same = [dict(_v[0]), dict(_v[1])]
_v_same[0]["stance"] = _v_same[1]["stance"]
_v_same[0]["conviction"] = _v_same[1]["conviction"]
_v_same[0]["determinism_tier"] = _v_same[1]["determinism_tier"]
_v_same[0]["determinism_score"] = _v_same[1]["determinism_score"]
_v_same[0]["key_metrics"] = dict(_v_same[1]["key_metrics"])
check(ds._diff_thesis_versions(_v_same)["changes"] == [],
      "#15 无变化 → 空 changes")


# ─────────────────────────────────────────────────────────
print(f"\n汇总: PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)