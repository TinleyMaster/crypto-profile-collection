#!/usr/bin/env python3
"""P3-B · 归属评测集「半自动定标」器（只读 DB，写 gold 文件）。

背景
----
`attr_gold_sample.py` 产出的候选集里，`label_source in (review, auto_suspect)`
的行**桶标签不可信**：分桶用的是 `core.asset` 的 symbol 匹配，而 symbol
撞名极其普遍（实测 `SHY` → `#7237 Shytoshi Kusama`、`NEX` → `Nexus`），
导致大量「美债收益率 / 券商财报」类**非加密**新闻被错分进 `crypto_gap`。
⇒ 标签必须**按正文判读**，不能沿用桶规则。

定标策略（半自动 = 规则 + 人工判读，规则显式、默认显式）
------------------------------------------------------
1. `overrides["pos"]`   —— 人工判读为**真加密事件**的行，给其 ticker（严格解析）。
2. `overrides["review"]`—— 判读**存疑/无法表达**（资产不可解析、归属可争）→
   保持 `needs_review=True`，评测**跳过**（不污染指标）。
3. `overrides["neg"]`   —— 显式负样本（可选微调；默认已按桶归负）。
4. 其余未命中者 → **按桶默认归负**（`__non_crypto__`），并把**全部默认 cid
   打印出来**供复核，杜绝静默默认。

严格解析
--------
`pos` 的 ticker → `core.asset` 中 `canonical_symbol` 完全相等、
`market_cap_rank IS NOT NULL`、非合成资产（name 不匹配 SYNTH）中
**rank 最小者（取 1 个）**。避免把 `Bitcoin AI`/`Wrapped Solana` 之类
同名异物算进标签（撞名是本案的核心病灶，标签必须比预测更干净）。

用法
----
    python attr_gold_label.py                  # 定标并覆盖写 attr_gold.jsonl
    python attr_gold_label.py --dry            # 只打印，不写
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
_candidate = SCRIPT_DIR.parent.parent / "workbench"
WORKBENCH_DIR = _candidate if _candidate.exists() else SCRIPT_DIR.parent.parent
sys.path.insert(0, str(WORKBENCH_DIR))
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from catalyst.db import get_conn  # noqa: E402

NON_CRYPTO = "__non_crypto__"
SYNTH_NAME_RE = r"tokeniz|bstock|prestock|derivativ|robinhood token|dinari|backpack|xstock"

FIXTURES = SCRIPT_DIR.parent.parent / "tests" / "fixtures"
GOLD = FIXTURES / "attr_gold.jsonl"
OVERRIDES = FIXTURES / "attr_gold_overrides.json"
REVIEW_MD = FIXTURES / "attr_gold_review.md"

# 归负默认适用的桶（其余桶若出现未命中行，视为异常并告警）
NEG_DEFAULT_BUCKETS = {"crypto_gap", "unknown_mixed", "no_pairs", "tradfi_only"}


def _load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _resolve_strict(cur, sym: str) -> int | None:
    cur.execute(
        f"""select asset_id from core.asset
            where upper(canonical_symbol) = %s
              and market_cap_rank is not null
              and lower(canonical_name) !~ '{SYNTH_NAME_RE}'
            order by market_cap_rank asc, asset_id asc limit 1""",
        (sym.upper(),),
    )
    r = cur.fetchone()
    return r["asset_id"] if r else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(GOLD))
    ap.add_argument("--overrides", default=str(OVERRIDES))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    gold_p = Path(args.gold)
    rows = _load_jsonl(gold_p)
    ov = json.loads(Path(args.overrides).read_text(encoding="utf-8"))
    pos = {int(k): v for k, v in ov.get("pos", {}).items()}
    review = {int(k): v for k, v in ov.get("review", {}).items()}
    neg_explicit = set(int(k) for k in ov.get("neg", []))

    stats = Counter()
    defaulted: list[tuple[int, str, str]] = []
    unresolved: list[tuple[int, str]] = []
    review_rows: list[dict] = []

    with get_conn() as conn:
        cur = conn.cursor()
        for r in rows:
            cid = int(r["cid"])
            bucket = r.get("bucket")

            # 1) 显式正样本
            if cid in pos:
                aids = []
                for s in pos[cid]:
                    aid = _resolve_strict(cur, s)
                    if aid is None:
                        unresolved.append((cid, s))
                    elif aid not in aids:
                        aids.append(aid)
                if aids:
                    r["label"] = aids
                    r["label_symbols"] = list(pos[cid])
                    r["label_source"] = "ai_review"
                    r["confidence"] = "high"
                    r["needs_review"] = False
                    r["hint"] = "人工判读：真加密事件"
                    stats["pos"] += 1
                    continue
                # 解析不出资产 → 降级 review
                r["label"] = None
                r["label_source"] = "review"
                r["needs_review"] = True
                r["hint"] = f"判读为正但资产不可解析: {pos[cid]}"
                review_rows.append(r)
                stats["review_unresolved"] += 1
                continue

            # 2) 显式存疑 → 跳过评测
            if cid in review:
                r["label"] = None
                r["label_source"] = "review"
                r["confidence"] = "low"
                r["needs_review"] = True
                r["hint"] = f"人工判读存疑: {review[cid]}"
                review_rows.append(r)
                stats["review"] += 1
                continue

            # 3) 显式负样本（优先于 auto 重解析，用于纠正撞名假正样本）
            if cid in neg_explicit:
                r["label"] = [NON_CRYPTO]
                r["label_symbols"] = []
                r["label_source"] = "ai_review"
                r["confidence"] = "high"
                r["needs_review"] = False
                r["hint"] = "人工判读：非加密事件 / 无可归属加密资产"
                stats["neg_explicit"] += 1
                continue

            # 3.5) 幂等保护：已定标（ai_review）且已解析出标签的行不重判。
            #      否则二次运行时它会掉进 step 4/5，被「默认归负」覆盖成负样本
            #      —— 这正是 2026-10-19 复核实测到的破坏性行为（正样本 30 → 17）。
            if (
                r.get("label_source") == "ai_review"
                and isinstance(r.get("label"), list)
                and r["label"]
            ):
                r["needs_review"] = False
                stats["keep_labeled"] += 1
                continue

            # 4) 既有 auto 标签 → 按严格口径重解析（统一标签口径）
            if r.get("label_source") == "auto" and isinstance(r.get("label"), list):
                if r["label"] == [NON_CRYPTO]:
                    r["label_source"] = "ai_review"
                    r["needs_review"] = False
                    stats["keep_neg"] += 1
                    continue
                syms = r.get("label_symbols") or []
                aids = []
                for s in syms:
                    aid = _resolve_strict(cur, s)
                    if aid is not None and aid not in aids:
                        aids.append(aid)
                if aids:
                    r["label"] = aids
                    r["label_source"] = "ai_review"
                    r["needs_review"] = False
                    stats["pos_from_auto"] += 1
                    continue
                # 解析失败 → 转为存疑
                r["label"] = None
                r["label_source"] = "review"
                r["needs_review"] = True
                review_rows.append(r)
                stats["review_unresolved"] += 1
                continue

            # 5) 其余 → 按桶默认归负（显式记录）
            if bucket in NEG_DEFAULT_BUCKETS or cid in neg_explicit:
                r["label"] = [NON_CRYPTO]
                r["label_symbols"] = []
                r["label_source"] = "ai_review"
                r["confidence"] = "medium"
                r["needs_review"] = False
                r["hint"] = (r.get("hint") or "") + f"  [default_neg:{bucket}]"
                defaulted.append((cid, bucket, (r.get("title") or "")[:70]))
                stats["default_neg"] += 1
                continue

            # 6) 兜底：不认识的桶 → 不猜
            r["label"] = None
            r["label_source"] = "review"
            r["needs_review"] = True
            r["hint"] = (r.get("hint") or "") + "  [unknown_bucket]"
            review_rows.append(r)
            stats["review_unknown_bucket"] += 1

    # ---- 输出 ----
    print("=" * 72)
    print("半自动定标结果")
    print("=" * 72)
    for k, v in sorted(stats.items()):
        print(f"  {k:<22} {v}")
    n_scored = sum(1 for r in rows if r.get("label") is not None)
    n_neg = sum(1 for r in rows if r.get("label") == [NON_CRYPTO])
    n_pos = sum(1 for r in rows if isinstance(r.get("label"), list) and r["label"] != [NON_CRYPTO])
    print(f"  {'─' * 20}")
    print(f"  可评分 {n_scored}  正样本 {n_pos}  负样本 {n_neg}  跳过 {len(rows) - n_scored}")

    if unresolved:
        print("\n⚠️ 正样本 ticker 解析失败（已降级 review）:")
        for cid, s in unresolved:
            print(f"    cid={cid} sym={s}")

    print("\n---- 被默认归负的行（逐条复核用）----")
    for cid, b, t in defaulted:
        print(f"  {cid:<7} {b:<14} {t}")

    # review 清单写 MD
    lines = ["# attr_gold 待人工复核清单（P3-B 半自动定标残项）", ""]
    lines.append(f"> 共 {len(review_rows)} 条。这些行**不参与评测**（`label=null`），")
    lines.append("> 需人工确认后填 `label`（`[]`/`[asset_id...]`/`[\"__non_crypto__\"]`）并把 `needs_review` 置 false。")
    lines.append("")
    lines.append("| cid | bucket | pairs | 判读备注 | title |")
    lines.append("|---|---|---|---|---|")
    for r in sorted(review_rows, key=lambda x: x["cid"]):
        t = (r.get("title") or "").replace("|", "/")[:90]
        pairs = ",".join(r.get("related_pairs") or []) or "-"
        lines.append(f"| {r['cid']} | {r.get('bucket')} | {pairs} | {r.get('hint')} | {t} |")
    review_md_text = "\n".join(lines) + "\n"

    if args.dry:
        print("\n[dry] 未写文件")
        return 0

    with gold_p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    REVIEW_MD.write_text(review_md_text, encoding="utf-8")
    print(f"\n写出 {len(rows)} 行 → {gold_p.name}")
    print(f"复核清单 {len(review_rows)} 条 → {REVIEW_MD.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
