"""Binance News（kol_catalyst_binance_square_7）存量噪声软标记。

审计 2026-10-08 P0：该源为币安广场全量财经 RSS，41.2% 为非加密内容
（美元/原油/伊朗/美股/他国事务等），已生成 4,502 条 signal 污染下游。

本脚本用与源头过滤 `_is_crypto_relevant` 完全一致的匹配逻辑（英文词边界/
中文子串），将命中噪声黑名单的存量 catalyst 软标记为 noise：
  - biz.catalyst_grade.catalyst_kind  → 'noise'
  - biz.catalyst_signal.kind          → 'noise'
下游（重大事件/周报/回填/信号生成）已全部 `catalyst_kind != 'noise'` 过滤，
标记后自动不再使用；历史数据保留、可回滚（快照写盘）。

幂等：只更新当前非 noise 的行，重复执行结果不变。
用法：python scripts/bin/mark_binance_news_noise.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

WORKBENCH = Path(__file__).resolve().parents[2] / "workbench"
sys.path.insert(0, str(WORKBENCH))

from catalyst.db import get_conn  # noqa: E402

SOURCE = "kol_catalyst_binance_square_7"
SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "catalyst_binance_noise_snapshot_20261009.csv"


def load_noise_keywords() -> list[str]:
    for cand in (Path("/app/catalyst_rules.yaml"),
                 WORKBENCH / "catalyst" / "catalyst_rules.yaml"):
        if cand.exists():
            cfg = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
            return (cfg.get("source_crypto_filter") or {}).get("noise_keywords") or []
    raise RuntimeError("catalyst_rules.yaml not found")


def is_noise(text: str, noise_kws: list[str]) -> bool:
    """与 kol/catalyst_pipeline._is_crypto_relevant 一致：英文词边界 / 中文子串。"""
    low = (text or "").lower()
    for kw in noise_kws:
        if not kw:
            continue
        if kw.isascii() and kw.isalpha():
            if re.search(rf"\b{re.escape(kw.lower())}\b", low):
                return True
        else:
            if kw.lower() in low:
                return True
    return False


def main() -> None:
    noise_kws = load_noise_keywords()
    print(f"noise_keywords: {len(noise_kws)} 个")
    print(f"目标 source: {SOURCE}")

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ac.catalyst_id, ac.title, ac.title_cn, COALESCE(ac.body_text, '') AS body
            FROM biz.asset_catalyst ac
            WHERE ac.source_code = %s
            """,
            (SOURCE,),
        ).fetchall()
        total = len(rows)
        noise_ids = [
            r["catalyst_id"]
            for r in rows
            if is_noise(f"{r['title'] or ''} {r['title_cn'] or ''} {r['body'][:600]}", noise_kws)
        ]
        print(f"存量总条数: {total}, 命中噪声: {len(noise_ids)} ({len(noise_ids)/total*100:.1f}%)")

        if not noise_ids:
            print("无噪声命中，退出")
            return

        # 1) 快照（回滚依据）：标记前的 grade/signal kind（同一事务内，UPDATE 前读旧值）
        #    批量查询（ANY 一次拉全量），避免逐条往返
        grade_map = {
            r["catalyst_id"]: r["catalyst_kind"]
            for r in conn.execute(
                "SELECT catalyst_id, catalyst_kind FROM biz.catalyst_grade WHERE catalyst_id = ANY(%s)",
                (noise_ids,),
            ).fetchall()
        }
        signal_map: dict[int, str] = {}
        for r in conn.execute(
            "SELECT catalyst_id, kind FROM biz.catalyst_signal WHERE catalyst_id = ANY(%s)",
            (noise_ids,),
        ).fetchall():
            signal_map[r["catalyst_id"]] = (signal_map.get(r["catalyst_id"], "") or "") + "|" + r["kind"]
        signal_map = {k: v.lstrip("|") for k, v in signal_map.items()}

        snapshot_lines = ["catalyst_id,grade_kind,signal_kind"]
        for cid in noise_ids:
            snapshot_lines.append(
                f"{cid},{grade_map.get(cid, '')},{signal_map.get(cid, '')}"
            )
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text("\n".join(snapshot_lines), encoding="utf-8")
        print(f"快照已写入: {SNAPSHOT} ({len(snapshot_lines) - 1} 行)")

        # 2) 软标记 grade（幂等：只更新非 noise）
        cur = conn.execute(
            """
            UPDATE biz.catalyst_grade
               SET catalyst_kind = 'noise', updated_at = NOW()
             WHERE catalyst_id = ANY(%s)
               AND catalyst_kind <> 'noise'
            """,
            (noise_ids,),
        )
        grade_updated = cur.rowcount

        # 3) 软标记 signal（幂等）
        cur = conn.execute(
            """
            UPDATE biz.catalyst_signal
               SET kind = 'noise', updated_at = NOW()
             WHERE catalyst_id = ANY(%s)
               AND kind <> 'noise'
            """,
            (noise_ids,),
        )
        signal_updated = cur.rowcount

        print(f"grade 标记为 noise: {grade_updated} 行")
        print(f"signal 标记为 noise: {signal_updated} 行")

        # 4) 验证
        v1 = conn.execute(
            "SELECT count(*) AS n FROM biz.catalyst_grade cg JOIN biz.asset_catalyst ac "
            "ON ac.catalyst_id = cg.catalyst_id WHERE ac.source_code = %s AND cg.catalyst_kind = 'noise'",
            (SOURCE,),
        ).fetchone()["n"]
        v2 = conn.execute(
            "SELECT count(*) AS n FROM biz.catalyst_signal s JOIN biz.asset_catalyst ac "
            "ON ac.catalyst_id = s.catalyst_id WHERE ac.source_code = %s AND s.kind = 'noise'",
            (SOURCE,),
        ).fetchone()["n"]
        print(f"验证：该源 grade=noise {v1} 条, signal=noise {v2} 条")


if __name__ == "__main__":
    main()
