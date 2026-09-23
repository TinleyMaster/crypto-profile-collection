#!/usr/bin/env python3
"""回填 sys.ai_trace 的 symbol / asset_id（2026-09-23 审计 P0 数据修复）。

根因：_call_llm_analysis_v2 写 trace 时从 profile 顶层取 asset_id/symbol，
但两者存在 profile["basic"] 下 → 全表这两列 NULL（1299 行），"追溯"无法关联代币。
代码已修（35ef6f0），历史行从 user_prompt 里的「- 代币: SYMBOL (NAME)」正则提取 symbol，
再经 core.asset 解析 asset_id（优先有市值的真币）。

用法：
    python backfill_ai_trace_identity.py --dry-run   # 预览
    python backfill_ai_trace_identity.py             # 回写

存量坏 JSON 修复（2026-09-23 复验 P1 衍生）：
    写入口已修（落库前补转义），但历史行的 raw_response 仍是脏原文，
    导致 `raw_response::json` 类库内校验失败、PG JSON 函数不可用。
    --clean-raw 只做**无损**修复（字符串内控制符 / 未转义直引号补转义），
    要求整体可解析才回写；截断型坏 JSON 会被跳过（补齐会丢原文），
    这类行靠查看器 parsed_response 兜底，库里原文保持不动。
    ⚠️ 会覆盖 raw_response 原文，属 prod 数据写操作，需用户授权后执行；先 --dry-run 看数。
    python backfill_ai_trace_identity.py --clean-raw --dry-run
    python backfill_ai_trace_identity.py --clean-raw
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = SCRIPT_DIR.parent / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import psycopg  # noqa: E402

from crypto_research.config import get_settings  # noqa: E402
from crypto_research.db.conn import get_connection  # noqa: E402

_SYM_RE = re.compile(r"- 代币:\s*(\S+?)\s*\(")
_SYM_VALID = re.compile(r"^[A-Za-z0-9.]+$")
_BAD = {"canonical_symbol", "symbol", "TBD", "unknown", "UNKNOWN", "?", "null", "None"}


def _extract_symbol(user_prompt: str | None) -> str | None:
    if not user_prompt:
        return None
    m = _SYM_RE.search(user_prompt)
    if not m:
        return None
    sym = m.group(1).strip()
    if not sym or len(sym) > 20 or not _SYM_VALID.match(sym) or sym in _BAD:
        return None
    return sym


def _resolve_asset_ids(conn, symbols: list[str]) -> dict[str, int]:
    """批量解析 symbol → asset_id（单次查询，避免逐行往返慢查询）。"""
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (UPPER(canonical_symbol)) UPPER(canonical_symbol) AS sym, asset_id "
            "FROM core.asset "
            "WHERE UPPER(canonical_symbol) = ANY(%s) "
            "ORDER BY UPPER(canonical_symbol), (market_cap_rank IS NULL), market_cap_rank NULLS LAST, asset_id",
            (symbols,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _lossless_repair(raw: str) -> str | None:
    """无损修复 raw_response：只补转义（控制符 / 字符串内未转义直引号），整体必须可解析。

    刻意**不用** `extract_json_from_llm_response` 的截断补齐策略：实测 48 条坏 JSON 里
    有 12 条属「输出被截断」，补齐会丢 170~1050 字原文，审计记录不该被悄悄截短 ——
    这类行留给查看器 parsed_response 兜底展示，库里原文保持不动。
    修复不了（返回 None）的行同样原样保留。
    """
    from crypto_research.clients.llm_client import (
        _sanitize_json_control_chars,
        _sanitize_unescaped_quotes,
    )

    fixed = _sanitize_unescaped_quotes(_sanitize_json_control_chars(raw))
    if fixed == raw:
        return None
    try:
        json.loads(fixed)
    except Exception:
        return None
    return fixed


def _clean_raw(conn, dry_run: bool, limit: int) -> int:
    """把 raw_response 非法的行按无损规则修复为合法 JSON（只修能修的）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, raw_response FROM sys.ai_trace "
            "WHERE raw_response IS NOT NULL AND raw_response <> '' ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()

    if not rows:
        print("无待扫描行")
        return 0

    to_fix: list[tuple[int, str]] = []
    skipped = 0
    for rid, raw in rows:
        try:
            json.loads(raw)
            continue  # 已合法，跳过
        except Exception:
            pass
        fixed = _lossless_repair(raw)
        if fixed is None:
            skipped += 1  # 截断型等不可无损修复，保留原文
            continue
        to_fix.append((rid, fixed))

    print(f"扫描 {len(rows)} 行：可无损修复 {len(to_fix)} 行，"
          f"不可无损修复（保留原文，靠 parsed_response 兜底）{skipped} 行")

    if dry_run:
        print(f"[dry-run] 将回写 {len(to_fix)} 行")
        for rid, fixed in to_fix[:3]:
            print(f"  id={rid}: {fixed[:100]}...")
        return 0

    if not to_fix:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sys.ai_trace t SET raw_response = v.raw "
            "FROM (SELECT unnest(%s::bigint[]) AS id, "
            "             unnest(%s::text[]) AS raw) v "
            "WHERE t.id = v.id",
            ([r[0] for r in to_fix], [r[1] for r in to_fix]),
        )
    conn.commit()
    print(f"已回写 {len(to_fix)} 行（raw_response 现为合法 JSON，内容无损）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 sys.ai_trace symbol/asset_id")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--all", action="store_true",
                        help="处理全部 signal_v2 行并从 user_prompt 重导（含覆盖错误值/清占位符）")
    parser.add_argument("--clean-raw", action="store_true",
                        help="改为修复历史坏 JSON：按写入口同一清洗逻辑重写 raw_response")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        if args.clean_raw:
            return _clean_raw(conn, args.dry_run, args.limit)
        with conn.cursor() as cur:
            where = "tag = 'signal_v2'" if args.all else (
                "tag = 'signal_v2' AND (symbol IS NULL OR symbol = '' OR asset_id IS NULL)"
            )
            cur.execute(
                f"SELECT id, user_prompt FROM sys.ai_trace WHERE {where} ORDER BY ts DESC LIMIT %s",
                (args.limit,),
            )
            rows = cur.fetchall()

        if not rows:
            print("无待处理行")
            return 0

        to_fix: list[tuple[int, str | None, int | None]] = []  # (id, symbol, asset_id)
        no_sym = 0
        symbols: list[str] = []
        for rid, up in rows:
            sym = _extract_symbol(up)
            if not sym:
                no_sym += 1
                # --all 模式下占位符（- / ?）清为 NULL，避免残留脏值
                if args.all:
                    to_fix.append((rid, None, None))
                continue
            to_fix.append((rid, sym, None))
            symbols.append(sym.upper())

        # 批量解析 asset_id（一次查询）
        aid_map = _resolve_asset_ids(conn, list(set(symbols)))
        no_asset = 0
        for idx in range(len(to_fix)):
            rid, sym, _ = to_fix[idx]
            if sym is None:
                continue
            aid = aid_map.get(sym.upper())
            if aid is None:
                no_asset += 1
            to_fix[idx] = (rid, sym, aid)

        print(f"待处理 {len(rows)} 行：提取到 symbol {sum(1 for _, s, _ in to_fix if s)}"
              f"（无 symbol {no_sym}，symbol 解析不到 asset {no_asset}）")

        if args.dry_run:
            print(f"[dry-run] 将回写 {len(to_fix)} 行（symbol 命中 "
                  f"{sum(1 for _, s, _ in to_fix if s)}，asset_id 命中 "
                  f"{sum(1 for _, _, a in to_fix if a is not None)} 行）")
            for rid, sym, aid in to_fix[:8]:
                print(f"  id={rid}: symbol={sym!r} asset_id={aid}")
            return 0

        with conn.cursor() as cur:
            ids = [r[0] for r in to_fix]
            syms = [r[1] for r in to_fix]
            aids = [r[2] for r in to_fix]
            # 批量单次 UPDATE（unnest 数组），避免逐行往返慢查询
            cur.execute(
                "UPDATE sys.ai_trace t SET symbol = v.sym, asset_id = v.aid "
                "FROM (SELECT unnest(%s::bigint[]) AS id, "
                "             unnest(%s::text[]) AS sym, "
                "             unnest(%s::bigint[]) AS aid) v "
                "WHERE t.id = v.id",
                (ids, syms, aids),
            )
        conn.commit()
        print(f"已回写 {len(to_fix)} 行（symbol 命中 {sum(1 for _, s, _ in to_fix if s)}，"
              f"asset_id 命中 {sum(1 for _, _, a in to_fix if a is not None)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())