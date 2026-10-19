#!/usr/bin/env python3
"""P3-B · 归属评测器（**只读**）。

对 gold 集算 precision / recall / F1（按 (catalyst_id, asset_id) 微平均），
并可 `--predict` 用**当前 linker** 跑一遍产出 baseline 预测。

gold 记录（`tests/fixtures/attr_gold.jsonl`）字段：
    cid, bucket, title, body_snippet, related_pairs,
    label: [asset_id...] | ["__non_crypto__"] | null(未标注)
    label_source: auto | review       needs_review: bool

约定
----
- `label = null` → 跳过（未标注）
- `label = ["__non_crypto__"]` → 期望**连不上**；任何预测都算 FP
- 其余 → 期望连到 label 内的 asset_id 集合

用法
----
    python attr_eval.py --predict                 # 用当前 linker 出 baseline 指标
    python attr_eval.py --pred my_predictions.jsonl
    python attr_eval.py --bucket crypto_gap --predict
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
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

NON_CRYPTO = "__non_crypto__"


def _load_jsonl(p: Path) -> list[dict]:
    rows = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _predict_with_linker(gold: list[dict]) -> dict[int, list[int]]:
    """用当前 linker（pipeline._resolve_asset_ids）跑一遍预测。"""
    from catalyst.db import get_conn
    from catalyst.pipeline import _resolve_asset_ids

    class _It:
        __slots__ = ("title", "body_text", "related_pairs")

        def __init__(self, t, b, p):
            self.title, self.body_text, self.related_pairs = t, b, p

    pred: dict[int, list[int]] = {}
    with get_conn() as conn:
        for g in gold:
            it = _It(g.get("title") or "", g.get("body_snippet") or "", list(g.get("related_pairs") or []))
            try:
                pred[g["cid"]] = list(_resolve_asset_ids(it, conn, "trading_pairs"))
            except Exception as e:  # 单条失败不阻断
                print(f"  ! cid={g['cid']} 预测失败: {e}", file=sys.stderr)
                pred[g["cid"]] = []
    return pred


def _evaluate(gold: list[dict], pred: dict[int, list[int]], bucket: str | None) -> dict:
    per = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "n": 0, "neg": 0, "neg_ok": 0})
    errs: list[dict] = []
    skipped = 0
    for g in gold:
        lab = g.get("label")
        src = g.get("label_source")
        # 未标注 / 疑似标签（auto_suspect，需人工确认）→ 跳过
        if lab is None or src == "auto_suspect":
            skipped += 1
            continue
        if bucket and g.get("bucket") != bucket:
            continue
        b = g.get("bucket", "?")
        lab = list(lab) if isinstance(lab, (list, tuple)) else [lab]
        p_set = set(pred.get(g["cid"], []) or [])
        if NON_CRYPTO in lab:
            per[b]["n"] += 1
            per[b]["neg"] += 1
            if not p_set:
                per[b]["neg_ok"] += 1
            else:
                per[b]["fp"] += len(p_set)
                errs.append({"cid": g["cid"], "bucket": b, "kind": "neg_fp",
                             "pred": sorted(p_set), "label": [], "title": (g.get("title") or "")[:90]})
            continue
        g_set = set(int(x) for x in lab)
        tp = len(g_set & p_set)
        fp = len(p_set - g_set)
        fn = len(g_set - p_set)
        if fp:
            errs.append({"cid": g["cid"], "bucket": b, "kind": "fp",
                         "pred": sorted(p_set - g_set), "label": sorted(g_set),
                         "title": (g.get("title") or "")[:90]})
        if fn:
            errs.append({"cid": g["cid"], "bucket": b, "kind": "fn",
                         "pred": sorted(p_set), "label": sorted(g_set - p_set),
                         "title": (g.get("title") or "")[:90]})
        per[b]["tp"] += tp
        per[b]["fp"] += fp
        per[b]["fn"] += fn
        per[b]["n"] += 1

    agg = {"tp": 0, "fp": 0, "fn": 0, "n": 0, "neg": 0, "neg_ok": 0}
    for v in per.values():
        for k in agg:
            agg[k] += v[k]
    tp, fp, fn = agg["tp"], agg["fp"], agg["fn"]
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {
        "overall": {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
                    "tp": tp, "fp": fp, "fn": fn, "n_rows": agg["n"],
                    "neg_rows": agg["neg"],
                    "neg_kept_empty": agg["neg_ok"],
                    "neg_kept_empty_pct": round(100.0 * agg["neg_ok"] / agg["neg"], 1) if agg["neg"] else None,
                    "skipped_unlabeled": skipped},
        "by_bucket": {
            k: {"n": v["n"], "tp": v["tp"], "fp": v["fp"], "fn": v["fn"],
                "neg": v["neg"], "neg_ok": v["neg_ok"]}
            for k, v in sorted(per.items())
        },
        "errors": errs,
    }


def main() -> int:
    default_gold = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "attr_gold.jsonl"
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(default_gold))
    ap.add_argument("--pred", default=None, help="预测文件(jsonl: cid→asset_ids)")
    ap.add_argument("--predict", action="store_true", help="用当前 linker 生成 baseline 预测")
    ap.add_argument("--bucket", default=None, help="只评某一桶")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-errors", action="store_true", help="逐条打印 FP/FN/负样本误连")
    args = ap.parse_args()

    gold = _load_jsonl(Path(args.gold))
    if args.pred:
        pred = {int(r["cid"]): list(r.get("asset_ids") or []) for r in _load_jsonl(Path(args.pred))}
    elif args.predict:
        pred = _predict_with_linker(gold)
    else:
        print("需要 --predict 或 --pred <file>", file=sys.stderr)
        return 2

    res = _evaluate(gold, pred, args.bucket)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    o = res["overall"]
    print("=" * 62)
    print(f"归属评测（gold={Path(args.gold).name}"
          + (f", bucket={args.bucket}" if args.bucket else "") + (", linker baseline" if args.predict else "") + "）")
    print("=" * 62)
    print(f"  precision {o['precision']:.3f}   recall {o['recall']:.3f}   F1 {o['f1']:.3f}")
    print(f"  TP {o['tp']}  FP {o['fp']}  FN {o['fn']}  已评行 {o['n_rows']}  跳过(未标注/存疑) {o['skipped_unlabeled']}")
    print(f"  负样本(应留空) {o['neg_rows']}  保持为空 {o['neg_kept_empty']} ({o['neg_kept_empty_pct']}%)")
    print("-" * 62)
    for k, v in res["by_bucket"].items():
        print(f"  {k:<14} n={v['n']:<4} TP={v['tp']:<4} FP={v['fp']:<4} FN={v['fn']:<4} "
              f"neg={v['neg']}/{v['neg_ok']}")
    print("=" * 62)
    if args.show_errors:
        for e in res.get("errors") or []:
            print(f"  [{e['kind']:<6}] cid={e['cid']:<6} {e['bucket']:<13} "
                  f"pred={e['pred']} label={e['label']}")
            print(f"           {e['title']}")
        print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
