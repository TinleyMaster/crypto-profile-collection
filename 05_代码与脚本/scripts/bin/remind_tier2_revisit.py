"""国庆后待办提醒：评估 volume_surge_24h 是否加回 Tier 2 连板白名单（R3-1）。

背景（2026-09-24 落地）
------------------------
`a7be401` 把变化榜连板派生为高亮/高危信号（Tier 2），但 `volume_surge_24h` 被排除在
白名单 `_BOARD_DIRECTIONAL` 之外——原因是 2026-09-08 该榜由 top-20 扩到 top-40
（commit 8a22847），造成 11 个标的「自 09-08 起连续 16 天在榜」的结构性伪连板。
`740e31c` 随后补了口径屏障（`db_stats.DIFF_STREAK_CALIBER_BARRIERS` +
`streak_start_ambiguous`），展示层不再主张这类连板的强度，但白名单仍维持不加回。

剩下的唯一问题：新口径数据沉淀到什么程度、加回是否会挤掉其他类别的连板名额。
本脚本在提醒日发一封邮件，附带当天实测的连板分布（含与其他榜单的对比），
不做「够/不够」的自动判定——那个判断需要人来拍。

用法
----
    python remind_tier2_revisit.py                  # 仅提醒日发送（默认 2026-10-06）
    python remind_tier2_revisit.py --dry-run        # 只打印，不发信
    python remind_tier2_revisit.py --force          # 忽略日期直接发（补发/测试）
    python remind_tier2_revisit.py --on 2026-10-09  # 临时改期

调度：`scheduler.py` 注册 `0 9 6 10 *`（10-06 09:00 Asia/Shanghai，一年一次）。
非目标日期脚本自身跳过，故不会在后续年份重复发信；一次性任务，用完可从 SCHEDULE 删除。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from crypto_research.clients.notifier import EmailNotifier  # noqa: E402

DEFAULT_REMIND_DATE = "2026-10-06"

# 榜单口径变更日：镜像 db_stats.DIFF_STREAK_CALIBER_BARRIERS（新增口径变更日需同步）。
# 连板起点恰为这些日期的，天数=口径年龄而非标的强度，跨标的零区分度。
CALIBER_BARRIERS: dict[str, str] = {
    "volume_surge_24h": "2026-09-08",
    "price_change_24h": "2026-09-08",
}

TARGET_CATEGORY = "volume_surge_24h"
STREAK_MIN = 3

# 换算：Tier 2 每方向最多派生 8 条（derive_board_opportunities.max_per_direction），
# 高亮侧再按配额 diff_streak_up=3 精选。
TIER2_PER_DIRECTION = 8
TIER2_HIGHLIGHT_QUOTA = 3

# 与 db_stats.STREAK_SQL 同口径的 gaps-and-islands（只读），带出 category / symbol。
STREAK_SQL = """
WITH ranked AS (
    SELECT
        asset_id, category, direction, diff_date,
        diff_date - (ROW_NUMBER() OVER (
            PARTITION BY asset_id, category, direction ORDER BY diff_date
        ) || ' days')::interval AS grp
    FROM biz.daily_diff_summary
    WHERE diff_date <= %s::DATE
),
islands AS (
    SELECT
        asset_id, category, direction,
        MAX(diff_date) AS last_date,
        COUNT(*)       AS streak_days,
        MIN(diff_date) AS first_date
    FROM ranked
    GROUP BY asset_id, category, direction, grp
)
SELECT
    i.category, i.direction, a.canonical_symbol AS sym,
    i.streak_days, i.first_date
FROM islands i
JOIN core.asset a ON a.asset_id = i.asset_id
WHERE i.last_date = %s::DATE AND i.streak_days >= %s
ORDER BY i.category, i.streak_days DESC, i.direction
"""


def fetch_streaks() -> dict:
    """只读：读取截至最新 diff_date 的各榜单连板（≥STREAK_MIN），按口径变更日分流。"""
    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(diff_date) FROM biz.daily_diff_summary")
            row = cur.fetchone()
            if not row or not row[0]:
                return {"as_of": None}
            as_of = str(row[0])
            cur.execute(STREAK_SQL, (as_of, as_of, STREAK_MIN))
            rows = cur.fetchall()

    by_cat: dict[str, dict] = {}
    target = {"genuine": [], "ambiguous": []}
    for category, direction, sym, sd, first_date in rows:
        first_date = str(first_date)
        ambiguous = CALIBER_BARRIERS.get(category) == first_date
        bucket = by_cat.setdefault(category, {"genuine": 0, "ambiguous": 0, "max_gen": 0})
        item = {"sym": sym, "dir": direction, "sd": sd, "first": first_date}
        if ambiguous:
            bucket["ambiguous"] += 1
            if category == TARGET_CATEGORY:
                target["ambiguous"].append(item)
        else:
            bucket["genuine"] += 1
            bucket["max_gen"] = max(bucket["max_gen"], sd)
            if category == TARGET_CATEGORY:
                target["genuine"].append(item)
    return {"as_of": as_of, "by_cat": by_cat, "target": target}


def _rows_html(items: list[dict]) -> str:
    if not items:
        return '<tr><td colspan="3" style="padding:8px;color:#9ca3af">（无）</td></tr>'
    return "".join(
        f'<tr style="background:#fff">'
        f'<td style="padding:7px 10px;border:1px solid #eee;font-weight:bold">{i["sym"]}</td>'
        f'<td style="padding:7px 10px;border:1px solid #eee">'
        f'{"📈 放量" if i["dir"] == "up" else "📉 缩量"}</td>'
        f'<td style="padding:7px 10px;border:1px solid #eee;text-align:right">'
        f'🔥{i["sd"]} 天 <span style="color:#9ca3af">(起 {i["first"]})</span></td>'
        f"</tr>"
        for i in items
    )


def build_email_html(stats: dict) -> tuple[str, str]:
    """返回 (subject, html)。"""
    as_of = stats.get("as_of") or "—"
    by_cat: dict = stats.get("by_cat") or {}
    genuine = stats["target"]["genuine"]
    ambiguous = stats["target"]["ambiguous"]
    n_gen = len(genuine)
    n_up = len([i for i in genuine if i["dir"] == "up"])
    max_gen = max([i["sd"] for i in genuine], default=0)

    # 横比：把各榜单「真实连板≥3 组数」并排——volume_surge 是否一枝独大，一眼可判
    cmp_rows = ""
    for cat, b in sorted(by_cat.items(), key=lambda kv: -kv[1]["genuine"]):
        is_target = cat == TARGET_CATEGORY
        cmp_rows += (
            f'<tr style="background:{"#eff6ff" if is_target else "#fff"}">'
            f'<td style="padding:7px 10px;border:1px solid #eee;'
            f'{"font-weight:bold" if is_target else ""}">{cat}</td>'
            f'<td style="padding:7px 10px;border:1px solid #eee;text-align:right">{b["genuine"]}</td>'
            f'<td style="padding:7px 10px;border:1px solid #eee;text-align:right">{b["max_gen"] or "—"}</td>'
            f'<td style="padding:7px 10px;border:1px solid #eee;text-align:right;color:#9ca3af">'
            f'{b["ambiguous"] or "—"}</td>'
            f"</tr>"
        )

    other_genuine = sum(b["genuine"] for c, b in by_cat.items() if c != TARGET_CATEGORY)
    crowd_note = (
        f"{TARGET_CATEGORY} 真实连板 {n_gen} 组，其余全部榜单合计 {other_genuine} 组——"
        f"加回后 Tier 2 每方向 {TIER2_PER_DIRECTION} 个派生名额、再按配额 "
        f"{TIER2_HIGHLIGHT_QUOTA} 精选，基本会被它占满。"
        if n_gen > other_genuine else
        f"{TARGET_CATEGORY} 真实连板 {n_gen} 组，其余全部榜单合计 {other_genuine} 组，"
        f"已不占绝对优势，挤占风险较小。"
    )

    subject = f"⏰ 国庆后待办｜评估 volume_surge 加回 Tier 2 白名单（{as_of} 实测：真实连板 {n_gen} 组 / 变更日起始 {len(ambiguous)} 组）"

    html = f"""
    <div style="font-family:sans-serif;max-width:680px;margin:auto;font-size:14px;color:#333">
      <h2 style="color:#2563eb;margin-bottom:6px">⏰ 国庆后待办 · R3-1</h2>
      <p style="margin:0 0 4px;font-size:15px">
        <b>评估 <code>{TARGET_CATEGORY}</code> 是否加回 Tier 2 连板白名单</b>
        （<code>macro_market.py</code> 的 <code>_BOARD_DIRECTIONAL</code>）
      </p>
      <p style="color:#6b7280;margin:0">
        当初排除它的理由（2026-09-08 扩档造成的结构性伪连板）已由口径屏障
        <code>740e31c</code> 收口，现在只差一个判断：<b>沉淀够了没、加回会不会挤掉别人</b>。
        没有自动判定，下面是 {as_of} 的实测事实。
      </p>

      <div style="margin-top:16px;padding:14px;background:#eff6ff;border-radius:6px">
        <div style="font-weight:bold;margin-bottom:6px">📊 关键事实（截至 {as_of}）</div>
        <ul style="margin:0;padding-left:20px;line-height:1.9">
          <li>{TARGET_CATEGORY} 连板 ≥{STREAK_MIN} 天：<b>真实 {n_gen} 组</b>
              （其中放量 {n_up} 组 / 缩量 {n_gen - n_up} 组，最长 <b>{max_gen} 天</b>）、
              口径变更日起始（不可比）{len(ambiguous)} 组</li>
          <li>{crowd_note}</li>
          <li>缩量侧语义：该榜 down = 「24h 成交量环比下降」，不等于价格下跌——
              是否算看空、要不要进高危信号，需单独拍板</li>
        </ul>
      </div>

      <table style="border-collapse:collapse;width:100%;margin-top:16px">
        <thead>
          <tr style="background:#f9fafb">
            <th style="padding:9px 10px;border:1px solid #eee;text-align:left">榜单</th>
            <th style="padding:9px 10px;border:1px solid #eee;text-align:right">真实连板组数</th>
            <th style="padding:9px 10px;border:1px solid #eee;text-align:right">最长(天)</th>
            <th style="padding:9px 10px;border:1px solid #eee;text-align:right">变更日起始</th>
          </tr>
        </thead>
        <tbody>{cmp_rows}</tbody>
      </table>
      <p style="margin:8px 0 0;font-size:12px;color:#9ca3af">
        口径：连板 ≥{STREAK_MIN} 天（与 Tier 2 派生门槛同口径）；
        「变更日起始」= 起点恰为榜单口径变更日，天数=口径年龄，已在展示层收口。
      </p>

      <table style="border-collapse:collapse;width:100%;margin-top:16px">
        <thead>
          <tr style="background:#f9fafb">
            <th style="padding:9px 10px;border:1px solid #eee;text-align:left">标的</th>
            <th style="padding:9px 10px;border:1px solid #eee;text-align:left">方向</th>
            <th style="padding:9px 10px;border:1px solid #eee;text-align:right">连板</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="3" style="padding:9px 10px;background:#f3f4f6;font-weight:bold">
            {TARGET_CATEGORY} · 真实连板（{n_gen} 组）
          </td></tr>
          {_rows_html(genuine)}
          <tr><td colspan="3" style="padding:9px 10px;background:#f3f4f6;font-weight:bold">
            {TARGET_CATEGORY} · 口径变更日起始（{len(ambiguous)} 组，不可比）
          </td></tr>
          {_rows_html(ambiguous)}
        </tbody>
      </table>

      <div style="margin-top:20px;padding:14px;background:#f0fdf4;border-radius:6px">
        <div style="font-weight:bold;margin-bottom:8px">🛠 若决定加回（两处代码）</div>
        <ol style="margin:0;padding-left:20px;line-height:1.9">
          <li><code>macro_market.py</code> → <code>_BOARD_DIRECTIONAL</code> 增补
              <code>("volume_surge_24h", "up"): "long"</code>
              <br><span style="color:#6b7280;font-size:12px">
              down 侧（缩量）映射别照抄，先按上面的语义问题拍板。</span></li>
          <li><code>test_macro_market_board_tier2.py</code> 的 <b>A14</b> 断言
              （"volume_surge 双向不派生"）会失效，须同步更新。</li>
        </ol>
        <p style="margin:10px 0 0;font-size:12px;color:#6b7280">
          回归：<code>python 05_代码与脚本/workbench/test_macro_market_board_tier2.py</code>
        </p>
      </div>

      <div style="margin-top:16px;padding:14px;background:#fffbeb;border-radius:6px">
        <div style="font-weight:bold;margin-bottom:6px">📌 其他现成开关</div>
        <ul style="margin:0;padding-left:20px;line-height:1.9">
          <li>维持现状：什么都不做（当时的建议）</li>
          <li>整体关闭 Tier 2：yaml <code>diff_streak_threshold: 0</code>
              （函数入口 <code>&lt;=0</code> 直接返回 <code>[]</code>）</li>
          <li>放宽产出：阈值降到 2 —— 连板组全量进池、噪声显著上升，<b>不建议</b></li>
        </ul>
      </div>

      <p style="margin-top:20px;font-size:12px;color:#999;line-height:1.7">
        相关提交：<code>a7be401</code>（Tier 2 派生）· <code>740e31c</code>（口径屏障）<br>
        相关文档：<code>规划_变化榜数据高效利用与高亮信号联动_2026-09-24.md</code><br>
        本邮件由 <code>remind_tier2_revisit.py</code> 在提醒日自动发送（一次性任务）
      </p>
    </div>
    """
    return subject, html


def main() -> int:
    parser = argparse.ArgumentParser(description="国庆后待办提醒（R3-1 白名单评估）")
    parser.add_argument("--on", default=DEFAULT_REMIND_DATE,
                        help=f"提醒日 YYYY-MM-DD（默认 {DEFAULT_REMIND_DATE}）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发邮件")
    parser.add_argument("--force", action="store_true", help="忽略日期直接发送（补发/测试）")
    parser.add_argument("--emit-html", metavar="PATH", help="把邮件 HTML 落盘以便目检")
    args = parser.parse_args()

    today = date.today().isoformat()
    if today != args.on and not args.force:
        print(f"[跳过] 今天 {today} 不是提醒日 {args.on}（--force 可强制发送）")
        return 0

    stats = fetch_streaks()
    if not stats.get("as_of"):
        print("[跳过] biz.daily_diff_summary 无数据，无法生成提醒内容")
        return 0

    subject, html = build_email_html(stats)
    n_gen = len(stats["target"]["genuine"])
    print(f"[提醒日 {args.on}] 数据截至 {stats['as_of']}｜"
          f"{TARGET_CATEGORY} 真实连板 {n_gen} 组 / 变更日起始 {len(stats['target']['ambiguous'])} 组")
    print(f"[主题] {subject}")

    if args.emit_html:
        Path(args.emit_html).write_text(html, encoding="utf-8")
        print(f"[emit-html] 已写入 {args.emit_html}")

    if args.dry_run:
        print("[dry-run] 不发送邮件。HTML 长度:", len(html))
        return 0

    settings = get_settings()
    notifier = EmailNotifier(settings)
    if not notifier.configured:
        print("SMTP 未配置（SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO），无法发送邮件。")
        return 1

    ok, msg = notifier.send(subject, html, from_name="变化榜待办")
    if ok:
        print(f"✅ 提醒邮件已发送: {msg}")
        return 0
    print(f"❌ 邮件发送失败: {msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())