"""合并已入库的重复催化剂（按新 content_hash 归一化判定）。

策略（保守）：
- 同一 hash 组内：保留最早 published_at 的一条为主记录
- 将其他重复条的 source_code 追加到主记录 source_codes 数组
- 删除重复条（先删除 catalyst_asset_link 关联，再删 catalyst）

重复判定分两类：
1. 新媒体源（kol_news_media_binance_square_*）：
   全文转载媒体，title 判定可靠 → 默认处理
2. 历史 kol 源（金十/Wallstreetcn 等快讯）：
   title 相同但正文不同 =「快讯更新序列」（保留，仅报告）
   title + 正文均高度一致 = 真重复（--all-sources 时处理）

用法：
    python merge_duplicate_catalysts.py --dry-run         # 预览（新媒体）
    python merge_duplicate_catalysts.py                   # 执行（新媒体）
    python merge_duplicate_catalysts.py --all-sources     # 执行（含历史 kol 真重复）
"""
import sys
import argparse
from pathlib import Path
from collections import defaultdict

# catalyst / kol 包所在目录，兼容两种部署结构：
#   本地开发：<project>/workbench/catalyst/  容器部署：/app/catalyst/（Dockerfile 扁平拷贝）
# 容器内原写法 parent.parent.parent/"workbench" 指向不存在的 /app/workbench。
_SCRIPT_PATH = Path(__file__).resolve()
_WB_CANDIDATE = _SCRIPT_PATH.parent.parent.parent / "workbench"
WORKBENCH_DIR = (
    _WB_CANDIDATE
    if (_WB_CANDIDATE / "catalyst" / "__init__.py").exists()
    else _SCRIPT_PATH.parent.parent.parent
)
sys.path.insert(0, str(WORKBENCH_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from catalyst.models import CatalystItem, _normalize_text  # noqa: E402
from kol.db import get_conn  # noqa: E402
from difflib import SequenceMatcher  # noqa: E402

# 默认只处理新媒体源（全文转载，title 判定可靠）
NEWS_MEDIA_PREFIX = "kol_news_media_binance_square_"
# 历史 kol 源正文相似度阈值（标题相同且正文也高度一致才算真重复）
BODY_SIM_THRESHOLD = 0.85


def _body_similarity(a_body: str, b_body: str) -> float:
    """计算两条归一化后正文的相似度（0~1）。空正文返回 0。"""
    na = _normalize_text(a_body or "")
    nb = _normalize_text(b_body or "")
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


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
        kol_update_series = {}
        for h, v in dup_groups.items():
            if all(r["source_code"].startswith(NEWS_MEDIA_PREFIX) for r in v):
                media_dups[h] = v
            else:
                # 历史 kol 源：正文也高度一致才算真重复；否则是「快讯更新序列」
                v_sorted = sorted(v, key=lambda r: r["published_at"])
                keep = v_sorted[0]
                truedup = []
                series = []
                for d in v_sorted[1:]:
                    sim = _body_similarity(keep["body_text"], d["body_text"])
                    if sim >= BODY_SIM_THRESHOLD:
                        truedup.append(d)
                    else:
                        series.append(d)
                if truedup:
                    kol_dups[h] = [keep] + truedup
                if series:
                    kol_update_series[h] = [keep] + series

        print(f"全量催化剂: {len(rows)} 条，重复组 {len(dup_groups)}（共涉及 "
              f"{sum(len(v) for v in dup_groups.values())} 条）")
        print(f"  新媒体组（全文转载，可安全合并）: {len(media_dups)} 组，涉及 "
              f"{sum(len(v) for v in media_dups.values())} 条")
        print(f"  历史kol真重复（标题+正文均一致）: {len(kol_dups)} 组，涉及 "
              f"{sum(len(v) for v in kol_dups.values())} 条")
        print(f"  历史kol更新序列（标题同正文异，保留）: {len(kol_update_series)} 组，涉及 "
              f"{sum(len(v) for v in kol_update_series.values())} 条")

        # 报告历史 kol 更新序列（不删）
        if kol_update_series:
            print("\n── 历史 kol 更新序列（标题相同正文不同，保留不删）──")
            for h, v in list(kol_update_series.items())[:5]:
                print(f"  [{h[:10]}] {len(v)} 条: {str(v[0]['title'])[:50]}")
                for r in v:
                    print(f"      id={r['catalyst_id']} {r['source_code']} {str(r['published_at'])[:16]}")

        # 处理重复：默认新媒体；--all-sources 时含历史 kol 真重复
        targets = dict(media_dups)
        if args.all_sources:
            targets.update(kol_dups)
        if not targets:
            print("\n无待处理重复，退出")
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
