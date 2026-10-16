#!/usr/bin/env python3
"""为研究型（research_manual）催化剂补结算管道 → biz.catalyst_signal + biz.catalyst_impact。

**为什么**：`biz.catalyst_outcome` 只覆盖新闻流信号（币安广场 KOL 等）；投研时人工录入的
研究型催化剂此前**没有 signal → 从不结算 → 无法验证「我们判断的利好是否真的带来超额」**。
本脚本把它们接入既有结算链路，此后跑 `collect_catalyst_outcome.py` 即自动结算多窗口收益，
并由 `asset_price_elasticity.py` 计入单币事件弹性——形成「判断 → 验证」闭环。

做法（幂等）
-----------
1. 找 `source_code='research_manual'` 且尚无 signal 的催化剂
2. 插 `catalyst_signal`：`kind='event'`，**`created_at = published_at`**
   （created_at 是结算基准时间，必须等于催化剂真实发布日期，否则窗口全错）
3. 插 `catalyst_impact`：方向由 `rule_event_type` 映射，**复用
   `collect_catalyst_outcome.EVENT_TYPE_DIRECTION` 保持单一口径**
4. 之后由 `collect_catalyst_outcome.py` 结算；幂等键 catalyst_id+asset_id，可重复跑

用法
----
    python bin/backfill_research_manual_signal.py --dry-run   # 预览
    python bin/backfill_research_manual_signal.py             # 写入
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
for p in (str(PROJECT_SRC), str(SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402
from collect_catalyst_outcome import EVENT_TYPE_DIRECTION  # noqa: E402  单一口径源

REASON = "研究型催化剂回填（基准时间=催化剂发布日期）；由 backfill_research_manual_signal.py 生成"

CANDIDATES_SQL = """
SELECT ac.catalyst_id, ac.asset_id, ac.published_at, ac.rule_event_type, ac.title,
       a.canonical_symbol
FROM biz.asset_catalyst ac
JOIN core.asset a ON a.asset_id = ac.asset_id
LEFT JOIN biz.catalyst_signal cs
       ON cs.catalyst_id = ac.catalyst_id AND cs.asset_id = ac.asset_id
WHERE ac.source_code = 'research_manual'
  AND ac.asset_id IS NOT NULL
  AND cs.signal_id IS NULL
ORDER BY ac.published_at
"""

INSERT_SIGNAL_SQL = """
INSERT INTO biz.catalyst_signal (catalyst_id, asset_id, kind, created_at, status, ai_reason, updated_at)
VALUES (%s, %s, 'event', %s, 'open', %s, NOW())
ON CONFLICT (catalyst_id, asset_id) DO UPDATE SET updated_at = NOW()
RETURNING signal_id
"""

INSERT_IMPACT_SQL = """
INSERT INTO biz.catalyst_impact (catalyst_id, asset_id, impact_direction, impact_strength, derived_from)
VALUES (%s, %s, %s, 'medium', 'research_manual_rule')
ON CONFLICT (catalyst_id, asset_id) DO NOTHING
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="研究型催化剂补结算管道")
    ap.add_argument("--dry-run", action="store_true", help="预览不写入")
    args = ap.parse_args()

    with get_connection(get_settings(require_database=True).database_url) as conn:
        cur = conn.cursor()
        cur.execute(CANDIDATES_SQL)
        rows = cur.fetchall()
        print(f"待补研究型催化剂: {len(rows)} 条")
        if not rows:
            print("无待补记录（已全部接入）")
            return 0

        n_sig = n_imp = n_no_dir = 0
        for cat_id, aid, pub, et, title, sym in rows:
            direction = EVENT_TYPE_DIRECTION.get(et)
            if not args.dry_run:
                cur.execute(INSERT_SIGNAL_SQL, (cat_id, aid, pub, REASON))
                cur.fetchone()
            n_sig += 1
            if direction:
                if not args.dry_run:
                    cur.execute(INSERT_IMPACT_SQL, (cat_id, aid, direction))
                n_imp += 1
            else:
                n_no_dir += 1
            print(f"  [{'dry' if args.dry_run else 'ok '}] cat={cat_id} {sym:<6} {str(pub)[:10]} "
                  f"{et:<16} → dir={direction or '未映射(不参与命中率)'}")

        print(f"\n{'[dry-run] ' if args.dry_run else ''}signal +{n_sig}，impact +{n_imp}，"
              f"方向未映射 {n_no_dir}（不影响超额收益结算，仅不参与命中率）")
        print("下一步：python bin/collect_catalyst_outcome.py --limit 500  （结算多窗口收益）")
        return 0


if __name__ == "__main__":
    sys.exit(main())
