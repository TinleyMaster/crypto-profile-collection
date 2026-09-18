"""合并已入库的重复催化剂（按新 content_hash 归一化判定）。

策略（保守）：
- 同一 hash 组内：保留最早 published_at 的一条为主记录
- 将其他重复条的 source_code 追加到主记录 source_codes 数组
- 删除重复条（先删除 catalyst_asset_link 关联，再删 catalyst）

⚠️ 默认只处理新媒体源（kol_news_media_binance_square_*）：
   这些是我们新增的、格式一致的全文转载媒体，title 判定可靠。
   历史 kol_catalyst_binance_square_7（金十/Wallstreetcn）多为
   「标题相同但正文不同」的快讯，title-only 判定会误删，仅报告不删除。

用法：
    python merge_duplicate_catalysts.py --dry-run         # 预览（全部源）
    python merge_duplicate_catalysts.py                   # 执行（仅新媒体源）
    python merge_duplicate_catalysts.py --all-sources     # 执行（全部源，谨慎）
"""
import sys
import argparse
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "workbench"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from catalyst.models import CatalystItem  # noqa: E402
from kol.db import get_conn  # noqa: E402

# 默认只处理新媒体源（全文转载，title 判定可靠）
NEWS_MEDIA_PREFIX = "kol_news_media_binance_square_"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="预览不删除")
    parser.add_argument("--all-sources", action="store_true",
                        help="处理全部来源（默认仅新媒体源，历史 kol 源仅报告）")
    args = parser.parse_args()

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT catalyst_id, title, body_text, source_code, source_codes, published_at
            FROM biz.asset_catalyst
            ORDER BY published_at ASC
        """).fetchall()

        groups = defaultdict(list)
        for r in rows:
            item = CatalystItem(
                source_code=r["source_code"], source_item_id=str(r["catalyst_id"]),
                title=r["title"] or "", body_text=r["body_text"] or "", published_at=0.0,
            )
            groups[item.content_hash].append(r)

        dup_groups = {h: v for h, v in groups.items() if len(v) > 1}

        # 区分：新媒体组 vs 历史 kol 组
        media_dups = {}
        kol_dups = {}
        for h, v in dup_groups.items():
            if all(r["source_code"].startswith(NEWS_MEDIA_PREFIX) for r in v):
                media_dups[h] = v
            else:
                kol_dups[h] = v

        print(f"全量催化剂: {len(rows)} 条，重复组 {len(dup_groups)}（共涉及 "
              f"{sum(len(v) for v in dup_groups.values())} 条）")
        print(f"  新媒体组（可安全合并）: {len(media_dups)} 组，涉及 "
              f"{sum(len(v) for v in media_dups.values())} 条")
        print(f"  历史kol组（仅报告不删）: {len(kol_dups)} 组，涉及 "
              f"{sum(len(v) for v in kol_dups.values())} 条")

        # 报告历史 kol 重复（不删）
        if kol_dups:
            print("\n── 历史 kol 源重复（仅报告，未删除）──")
            for h, v in list(kol_dups.items())[:8]:
                print(f"  [{h[:10]}] {len(v)} 条: {str(v[0]['title'])[:55]}")
                for r in v:
                    print(f"      id={r['catalyst_id']} {r['source_code']} {str(r['published_at'])[:16]}")
            if len(kol_dups) > 8:
                print(f"  ... 其余 {len(kol_dups)-8} 组省略")

        # 处理新媒体重复
        targets = media_dups if not args.all_sources else dup_groups
        if not targets:
            print("\n无新媒体重复，退出")
            return 0

        print(f"\n── 处理 {'全部' if args.all_sources else '新媒体'} 重复 ──")
        total_removed = 0
        for h, v in sorted(targets.items(), key=lambda kv: -len(kv[1])):
            v.sort(key=lambda r: r["published_at"])
            keep = v[0]
            dupes = v[1:]
            keep_sources = list(keep["source_codes"] or [keep["source_code"]])
            print(f"\n组[{h[:12]}] {len(v)} 条 → 保留 id={keep['catalyst_id']}，删除 {len(dupes)} 条")
            print(f"  主: {str(keep['title'])[:50]}")
            for d in dupes:
                src = d["source_code"]
                if src not in keep_sources:
                    keep_sources.append(src)
                print(f"  删: id={d['catalyst_id']} {src} {str(d['title'])[:40]}")

            if args.dry_run:
                continue

            conn.execute(
                "UPDATE biz.asset_catalyst SET source_codes = %s, updated_at = NOW() "
                "WHERE catalyst_id = %s",
                (keep_sources, keep["catalyst_id"]),
            )
            for d in dupes:
                conn.execute(
                    "DELETE FROM biz.catalyst_asset_link WHERE catalyst_id = %s",
                    (d["catalyst_id"],),
                )
                conn.execute(
                    "DELETE FROM biz.asset_catalyst WHERE catalyst_id = %s",
                    (d["catalyst_id"],),
                )
            total_removed += len(dupes)

        if args.dry_run:
            print(f"\n[dry-run] 预览完成，可删除 {total_removed} 条")
        else:
            print(f"\n完成：删除 {total_removed} 条重复催化剂，主记录已合并 source_codes")
        return 0


if __name__ == "__main__":
    sys.exit(main())
