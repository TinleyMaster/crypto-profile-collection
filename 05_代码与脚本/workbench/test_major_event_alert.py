"""重大事件通道（major_event）离线护栏测试。

覆盖重大事件通道（2026-09-23 建；2026-09-29 口径重建）的硬约束：
  1. 通道独立：notification_type 与 A 级 Alert 的三种类型互不重叠（独立去重）
  2. 重要性闸门独立于 tier：不得再用 tier/status 当重要性判据
  3. 事件级去重：一条新闻被多家媒体采成多条 catalyst 时只发一封
     （DISTINCT ON (s.asset_id) + 同资产 24h 已发则跳过）
  4. 负 alpha 类别排除：行情播报（含被 AI 误分类者）不得入池
  5. 双向市场确认门槛：利好型 |异动| ≥ 阈值且未降权；利空型由类型权重直达
  6. 渲染护栏：邮件不含任何交易档位字样、必须含「非交易建议」、
     必须含共振状态与催化方向（避免被读成追高指令）
  7. 失败不阻断主流程：查询异常返回 dict 而非抛异常；无候选不发信
  8. 单轮上限 ≤3（对应「日均 ≤3 条」目标）
  9. 传导逻辑模块（输出层优化）：含「影响传导/预期已消化/传导节奏」、直接度规则映射、
     二阶「板块联动」仅在数据存在时渲染；不再出现内部术语「未被计入降权」；
     直接度规则复验收口：ASCII 词边界（P2-1）、大小写不敏感（P3）、承载角色不误判（P2-2）
 10. 价格去尾零共享函数（复验 351d2ae）：整数部分零不被误吃、阈值边界、类型兜底、
     科学计数分支不得被 trim（指数尾零陷阱）

运行: python test_major_event_alert.py
"""
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from catalyst import notifier as N  # noqa: E402

passed = 0
failed = 0


def check(cond, name, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"    {detail}")


_SRC = open(N.__file__, encoding="utf-8").read()

# 抽出 _recent_major_events 的 SQL 文本（从 def 到下一个顶层 def）
_m = re.search(
    r"def _recent_major_events\(.*?\n(.*?)\ndef ",
    _SRC, re.S,
)
_QUERY_SRC = _m.group(1) if _m else ""


def _fake_row(**over):
    r = {
        "signal_id": 650174, "catalyst_id": 10571, "asset_id": 1348,
        "tier": "B", "composite_score": 65, "resonance_state": "weak",
        "resonance_score": 62, "kind": "structural",
        "canonical_name": "Bitcoin Cash", "symbol": "BCH",
        "catalyst_title": "CME plans to launch Bitcoin Cash (BCH) and Uniswap (UNI) futures",
        "title_cn": None, "catalyst_body": "CME said it will launch futures on BCH and UNI.",
        "ai_summary": "芝商所计划上线 BCH 与 UNI 期货。",
        "ai_event_type": "listing", "ai_sentiment": "bullish",
        "rule_event_type": "regulation", "source_code": "kol_catalyst_binance_square_7",
        "source_url": "https://example.com/a", "published_at": None,
        "authority_score": 88, "event_weight": 75, "scope_score": 67,
        "prelaunch_ret_24h": 8.2556, "prelaunch_penalty": 0,
        "catalyst_kind": "structural", "tradable": True,
        "impact_direction": "bullish", "impact_strength": "strong",
        "current_price": 620.5, "change_24h": 21.96, "change_7d": 30.1,
        "volume_24h": 1.2e9,
    }
    r.update(over)
    return r


print("== 1. 通道独立（去重互不干扰） ==")
check(N.NTYPE_MAJOR_EVENT == "major_event", "notification_type 常量为 major_event",
      N.NTYPE_MAJOR_EVENT)
check(len({N.NTYPE_MAJOR_EVENT, N.NTYPE_FAST_ALERT, N.NTYPE_SLOW_DIGEST,
           N.NTYPE_SLOW_DIGEST_STOCK}) == 4,
      "major_event 与 fast_alert/slow_digest/slow_digest_stock 互不重叠")

print("== 2. 重要性闸门独立于 tier ==")
check("tier IN ('A', 'B')" not in _QUERY_SRC and "tier IN ('A','B')" not in _QUERY_SRC,
      "不再用 tier 当重要性闸门（tier 只作展示，不做入选）")
check("s.status = 'open'" not in _QUERY_SRC,
      "不再要求 status='open'（可交易性闸门已与重要性解耦）")
check("entry_price" not in _QUERY_SRC and "take_profit" not in _QUERY_SRC,
      "判据不依赖 entry/stop/tp（不因给不出交易计划而漏报）")
check(N.MAJOR_EVENT_MIN_IMPORTANCE == 70.0,
      "重要性改为市场显著性阈值 importance>=70",
      str(N.MAJOR_EVENT_MIN_IMPORTANCE))
check("importance >= %s" in _QUERY_SRC, "SQL 以 importance 作为闸门")
check("_WEEKLY_TYPE_WEIGHT" in _SRC and "{weight_case}" in _QUERY_SRC,
      "类型权重复用周报 _WEEKLY_TYPE_WEIGHT（避免第二份口径漂移）")
check(N._WEEKLY_TYPE_WEIGHT.get("security") == 95
      and N._WEEKLY_TYPE_WEIGHT.get("etf") == 85,
      "security/etf 高权已在权重表内（旧 grade 表缺 security 键）")

print("== 3. 事件级去重 ==")
check("DISTINCT ON (asset_id)" in _QUERY_SRC, "每资产只留一条（多 catalyst 归并）")
check("ns.asset_id = gated.asset_id" in _QUERY_SRC,
      "24h 冷却按资产判定（代表信号变化也不会重发）")
check("nl.status = 'sent'" in _QUERY_SRC, "只有成功发送才计入冷却（失败可重试）")
check("nl.notification_type = %s" in _QUERY_SRC, "冷却按 notification_type 隔离")

print("== 4. 负 alpha 类别排除 ==")
check("category <> 'market_update'" in _QUERY_SRC, "行情播报归 market_update 后剔除")
check("head ~ '涨幅' AND head ~ '%%'" in _QUERY_SRC,
      "标题含「涨幅…%」判为行情播报（修复 AI 误分类漏网）")
check("价格突破|暴涨" in _QUERY_SRC, "标题含「价格突破/暴涨」判为行情播报")
check("COALESCE(cg.catalyst_kind, '') <> 'noise'" in _QUERY_SRC,
      "剔除噪音 kind（替代旧 kind 白名单）")
check("catalyst_kind = ANY" not in _QUERY_SRC,
      "旧 kind 白名单已删除（曾把 security 类挡在门外）")

print("== 5. 双向市场确认门槛 ==")
check("ABS(COALESCE(prelaunch_ret_24h, 0)) >= %s" in _QUERY_SRC,
      "利好型用双向绝对值（不限涨跌，修复只认涨幅的单向性）")
check("cg.prelaunch_ret_24h >= %s" not in _QUERY_SRC,
      "旧的单向 >= 门槛已删除（曾结构性灭杀利空型事件）")
check("COALESCE(prelaunch_penalty, 0) = 0" in _QUERY_SRC, "要求异动未被计入降权")
check("category IN ('security', 'delisting')" in _QUERY_SRC
      and "= 'bearish'" in _QUERY_SRC,
      "利空型 = security/delisting 或 ai_sentiment=bearish")
check("AND (is_bearish" in _QUERY_SRC, "利空型可绕过异动门槛（类型权重直达）")
check(N.MAJOR_EVENT_MIN_MOVE >= 5.0,
      "利好型异动阈值不低于 5%（实测 BCH 为 8.26%）", str(N.MAJOR_EVENT_MIN_MOVE))
check("published_at > NOW() - " in _QUERY_SRC, "只通报新鲜事件（避免停摆后补发陈旧事件）")

print("== 6. 渲染护栏 ==")
_html = N._build_major_event_html(_fake_row())
check("非交易建议" in _html, "含「非交易建议」声明")
check("重大事件通报" in _html, "含通道标识「重大事件通报」")
_banned = ("止损", "止盈", "入场", "做多", "做空", "entry_price", "stop_loss")
_hit = [k for k in _banned if k in _html]
check(not _hit, "不含任何交易档位/方向指令字样", f"命中: {_hit}")
check("共振状态" in _html, "展示共振状态（避免引导追高）")
check("催化方向" in _html and "利好" in _html, "展示催化方向标签")
check("市场确认" in _html and "+8.26%" in _html, "展示事件前异动幅度")
# 利空型（security/delisting）：不得写成「公告前 24h 已涨」，改由类型权重直达
_html_bear = N._build_major_event_html(_fake_row(
    is_bearish=True, event_type_norm="security",
    prelaunch_ret_24h=None, ai_sentiment="bearish",
    catalyst_title="某协议遭攻击，损失 1200 万美元", title_cn=None,
    ai_event_type="other", rule_event_type="other"))
check("利空型重大事件" in _html_bear, "利空型渲染「不以涨幅确认」文案")
check("公告前 24h 已涨" not in _html_bear,
      "利空型不得写「公告前 24h 已涨」（防把利空写反）")
check("事件类型直达" in _html_bear, "利空型「市场确认」行改为「事件类型直达」")
check("安全事件" in _html_bear, "传导路径优先取 event_type_norm=security")
_subj = N._major_event_subject(_fake_row())
check("重大事件" in _subj and "BCH" in _subj, "主题含通道标识与标的", _subj)
check(len(_subj) <= 90, "主题长度受控（≤90 字）", str(len(_subj)))

print("== 7. 失败不阻断 / 无候选不发信 ==")


class _BoomConn:
    def execute(self, *a, **k):
        raise RuntimeError("boom")


_r = N.send_major_event_alerts(_BoomConn())
check(isinstance(_r, dict) and _r.get("sent") == 0 and _r.get("failed") == 0,
      "查询异常时返回 dict 且 sent=0（不抛异常）", str(_r))


class _EmptyConn:
    def execute(self, *a, **k):
        class _C:
            def fetchall(self):
                return []

            def fetchone(self):
                return None
        return _C()


_r2 = N.send_major_event_alerts(_EmptyConn())
check(_r2 == {"sent": 0, "skipped": 0, "failed": 0, "signals": [], "reason": None},
      "无候选时静默返回（不发空窗邮件）", str(_r2))

print("== 8. 单轮上限 ==")
check(N.MAJOR_EVENT_MAX_PER_RUN <= 3, "单轮上限 ≤3", str(N.MAJOR_EVENT_MAX_PER_RUN))
check("LIMIT %s" in _QUERY_SRC, "SQL 带 LIMIT（上限由参数控制）")
check(N.MAJOR_EVENT_COOLDOWN_HOURS == 24, "事件级冷却 24h", str(N.MAJOR_EVENT_COOLDOWN_HOURS))

print("== 9. 传导逻辑模块（输出层优化护栏） ==")
check("影响传导" in _html and "传导直接度" in _html, "含「影响传导」与「传导直接度」模块")
check("预期已消化" in _html, "含「预期已消化」模块（prelaunch 重解）")
check("传导节奏" in _html and "即时" in _html and "中期" in _html,
      "含「传导节奏」三阶段（即时/短期/中期）")
check("未被计入降权" not in _html, "已去掉内部术语「未被计入降权」")
check("catalyst_second_order" in _QUERY_SRC, "SQL 消费二阶传导表（板块联动数据源）")
# 直接度规则：点名自身 + 自身受益动作 → 直接；仅作生态承载 → 间接
check(N._transmission_directness({
    "symbol": "SKY", "canonical_name": "Sky",
    "title_cn": "Galaxy 购入 SKY 并纳入财库", "ai_summary": ""})[0] == "direct",
    "自身受益动作点名该币 → 直接利好标的")
check(N._transmission_directness({
    "symbol": "SUI", "canonical_name": "Sui",
    "title_cn": "RWA 代币作 Bluefin Lend 抵押品", "ai_summary": ""})[0] == "indirect",
    "仅作生态承载链 → 生态间接受益")
# 复验 P2-1：ASCII 词边界（ETH ⊄ ETHEREUM、GPT ⊄ CGPT）
check(N._transmission_directness({
    "symbol": "GPT", "canonical_name": "GptToken",
    "title_cn": "CGPT 上线某交易所", "ai_summary": ""})[0] == "indirect",
    "ASCII 词边界：GPT 不误命中 CGPT（复验 P2-1）")
# 复验 P3：大小写不敏感
check(N._transmission_directness({
    "symbol": "SKY", "canonical_name": "Sky",
    "title_cn": "Galaxy 购入 sky 并纳入财库", "ai_summary": ""})[0] == "direct",
    "大小写不敏感命中并判直接（复验 P3）")
# 复验 P2-2：承载角色（X 链/生态）不误判为直接
check(N._transmission_directness({
    "symbol": "SUI", "canonical_name": "Sui",
    "title_cn": "某协议支持 SUI 链的 RWA 抵押", "ai_summary": ""})[0] == "indirect",
    "承载角色（X 链）不误判为直接（复验 P2-2）")
# 二阶联动：有数据才渲染，无数据不臆造
_h_so = N._build_major_event_html(
    _fake_row(second_order_symbols=["AAA", "BBB"], second_order_sector="RWA"))
check("板块联动" in _h_so and "AAA" in _h_so, "有二阶数据时渲染「板块联动」")
check("板块联动" not in _html, "无二阶数据时不渲染「板块联动」（不臆造传导标的）")
# 复验旧项：价格尾零（0.42830000 → 0.4283）
check(N._fmt_price(0.4283) == "0.4283", "价格尾零：0.42830000 → 0.4283")
check(N._fmt_price(620.5) == "620.5" and "620.5000" not in _html, "价格尾零：620.5000 → 620.5")
check(N._fmt_price(1e-5) == "1.0000e-05", "极小价仍走科学计数（不回归 2026-09-22 P0 显示修复）")

print("== 10. 价格去尾零共享函数（复验 351d2ae） ==")
# (a) _trim_trailing_zeros 纯度：`.` 阻断 rstrip，整数部分零不被误吃
check(N._trim_trailing_zeros("10.0000") == "10"
      and N._trim_trailing_zeros("100.0000") == "100"
      and N._trim_trailing_zeros("1000.0000") == "1000",
      "去尾零不吃整数部分零：10.0000→10 / 100.0000→100 / 1000.0000→1000")
check(N._trim_trailing_zeros("1,234,500.0") == "1,234,500"
      and N._trim_trailing_zeros("1,234,567.0") == "1,234,567",
      "千分位整数同样安全：1,234,500.0→1,234,500")
check(N._trim_trailing_zeros("0.00000000") == "0"
      and N._trim_trailing_zeros("0.00010000") == "0.0001",
      "全零小数→0；0.00010000→0.0001")
check(N._trim_trailing_zeros("1.0108") == "1.0108"
      and N._trim_trailing_zeros("100") == "100",
      "无尾零/无小数点串原样返回")
# (b) 模块级 _fmt_price：类型兜底 + 阈值边界
check(N._fmt_price(None) == "—" and N._fmt_price("abc") == "—",
      "None/非数值 → '—'（不抛异常）")
check(N._fmt_price(0) == "0.0000e+00" and N._fmt_price(-5) == "-5.0000e+00",
      "0/负数走科学计数分支")
check(N._fmt_price(1) == "1" and N._fmt_price(1000) == "1,000",
      "整数档去尾零：1→1 / 1000→1,000（阈值 1000 起用千分位）")
check(N._fmt_price(999.9999) == "999.9999" and N._fmt_price(1.4995) == "1.4995",
      "1~1000 档保留 4 位有效（去尾零不降精度）")
check(N._fmt_price(1234.567) == "1,234.6" and N._fmt_price("620.5") == "620.5",
      "≥1000 档一位小数；字符串数值同样格式化")
# (c) 关键陷阱守卫：科学计数分支刻意不调 trim
#     _trim_trailing_zeros("1.0000e-10") 会返回 "1.0000e-1"（指数尾零被当小数尾零吃掉），
#     若将来给该分支加 trim，meme 极小价会静默退化成 1.0000e-1。
check(N._fmt_price(1e-10) == "1.0000e-10",
      "科学计数分支未被 trim（指数尾零陷阱守卫）")

print("== 11. 审计 2026-09-29 修复护栏 ==")
# P1-3 极性：利空型不得再写「受益」
_d_harm = N._transmission_directness({
    "symbol": "XRP", "canonical_name": "XRP",
    "title_cn": "D'CENT 钱包 12.4M XRP 被盗，7000+ 钱包受影响", "ai_summary": "",
    "is_bearish": True})
check(_d_harm[0] == "harm" and "受损" in _d_harm[1],
      "利空型点名该币 → 「直接受损标的」（不再写受益）", str(_d_harm))
_d_harm_ind = N._transmission_directness({
    "symbol": "SUI", "canonical_name": "Sui",
    "title_cn": "某交易所被盗资金经 SUI 链转移", "ai_summary": "",
    "is_bearish": True})
check(_d_harm_ind[0] == "harm_indirect" and "承压" in _d_harm_ind[1],
      "利空型仅载体角色（X 链）→ 「生态间接承压」", str(_d_harm_ind))
_d_en = N._transmission_directness({
    "symbol": "SKY", "canonical_name": "Sky",
    "title_cn": None, "ai_summary": "Galaxy acquires SKY and adds it to treasury"})
check(_d_en[0] == "direct", "英文动作词（acquire/add to treasury）判直接利好", str(_d_en))
check("生态间接受益" not in N._build_major_event_html(_fake_row(
    is_bearish=True, event_type_norm="security", ai_sentiment="bearish",
    catalyst_title="某协议遭攻击，损失 1200 万美元", catalyst_body="hack", ai_summary="被盗")),
      "利空型邮件不再出现「生态间接受益」字样")

# P1-1 来源展示真实媒体
check(N._extract_publisher("ChainCatcher 消息，据 Lookonchain 监测…") == "ChainCatcher",
      "从标题抽媒体名：ChainCatcher")
check(N._extract_publisher(None, "Foresight News 报道，Arbitrum 基金会…") == "Foresight News",
      "从正文抽媒体名：Foresight News")
check(N._extract_publisher("某协议遭攻击，损失 1200 万美元") is None,
      "无媒体名时返回 None（不臆造）")
check(N._display_source(_fake_row()) == "kol_catalyst_binance_square_7",
      "无媒体名时回落 source_code（不丢溯源）")
_html_pub = N._build_major_event_html(_fake_row(
    catalyst_title="Foresight News 消息，Arbitrum 基金会推出安全计划", title_cn=None))
check("Foresight News" in _html_pub, "邮件「来源」展示真实媒体名")
check("kol_catalyst_binance_square_7" not in _html_pub,
      "渠道 id 不再出现在邮件正文（仅内部溯源）")

# P1-2 类别：误标 regulation 降级（不路由到监管口径）
check("raw_event_type = 'regulation'" in _QUERY_SRC and "THEN 'partnership'" in _QUERY_SRC
      and "THEN 'other'" in _QUERY_SRC,
      "SQL 对无监管线索的 regulation 做降级（partnership/other）")
check("基金会|foundation" in _QUERY_SRC and "program" in _QUERY_SRC,
      "降级判据含「基金会/foundation/program」（Arbitrum 类事件）")
check("cftc" in _QUERY_SRC and "l lawsuit" not in _QUERY_SRC
      and "|sec|" not in _QUERY_SRC,
      "监管线索含 cftc 等强线索，且刻意不含裸 sec/ban（避免误命中 security/arbitrum）")

# P2-2 板块枚举翻译
check(N._sector_label("l1") == "L1 公链" and N._sector_label("RWA") == "RWA",
      "板块枚举翻译（l1→L1 公链，大小写不敏感）",
      f"{N._sector_label('l1')} / {N._sector_label('RWA')}")
check(N._sector_label("weird") == "weird" and N._sector_label(None) == "",
      "未知枚举原样返回、空→空（不臆造）")
_html_sec = N._build_major_event_html(_fake_row(
    second_order_symbols=["AAA"], second_order_sector="l1"))
check("L1 公链" in _html_sec and "「l1」" not in _html_sec,
      "板块联动不再泄漏原始枚举 l1")

# P2-1 归因一致性披露（稳定币 + 大幅「事件前异动」= 错挂迹象）
check(N._prelaunch_attribution_warning({
    "symbol": "USDC", "prelaunch_ret_24h": -11.89}).startswith("⚠️"),
      "P2-1：稳定币展示币 + 大幅「事件前异动」→ 披露归因可能错位")
check(N._prelaunch_attribution_warning({
    "symbol": "USDC", "prelaunch_ret_24h": 0.02}) == "",
      "P2-1：稳定币异动正常（0.02%）不误报")
check(N._prelaunch_attribution_warning({
    "symbol": "ARB", "prelaunch_ret_24h": -11.89}) == "",
      "P2-1：非稳定币不触发该披露（避免噪声）")
check("归因/数据源错位" in N._build_major_event_html(_fake_row(
    symbol="USDC", canonical_name="USD Coin", prelaunch_ret_24h=-11.89,
    is_bearish=False, ai_sentiment="neutral")),
      "P2-1：USDC 式错挂行邮件含归因披露")

# P2-3 低时延兜底任务接线（脚本存在 + scheduler 注册每 30 分钟）
_bin = os.path.join(os.path.dirname(_here), "scripts", "bin")
check(os.path.exists(os.path.join(_bin, "send_major_events.py")),
      "P2-3：send_major_events.py 存在")
_sched = open(os.path.join(_here, "scheduler.py"), encoding="utf-8").read()
check('"catalyst_major_events"' in _sched and '"*/30 * * * *"' in _sched,
      "P2-3：scheduler 注册 catalyst_major_events（每 30 分钟）")
check('"send_major_events.py"' in _sched, "P2-3：调度指向 send_major_events.py")

print(f"\n{'=' * 50}\n通过 {passed} / 失败 {failed}\n{'=' * 50}")
sys.exit(1 if failed else 0)
