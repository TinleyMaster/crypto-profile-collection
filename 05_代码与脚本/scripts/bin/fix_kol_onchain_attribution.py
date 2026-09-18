#!/usr/bin/env python3
"""修复存量 kol_signal onchain 信号错资产归因（2026-09-18 审计 P0-1）。

问题：runner 此前按帖子的主币（symbol）解析 asset_id，导致「ZEC 空军头子帖里的
ETH 提币」被归因到 ZEC（signal 489：symbol=ZEC、event_token=ETH、$85.1M），
卡片显示"聪明钱 $85.1M"挂在 ZEC 上、方向还反了。

修法：onchain 类信号以 event_token（事件标的币）优先定资产，帖子提及币仅作 fallback。
本脚本一次性回写存量错归因行，与 runner.py 的新逻辑保持一致。

用法：
    python fix_kol_onchain_attribution.py --dry-run   # 预览将修正的行
    python fix_kol_onchain_attribution.py             # 实际回写
"""
from __future__ import annotations

import argparse
import os
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


def _match_asset(conn, symbol: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id FROM core.asset "
            "WHERE UPPER(canonical_symbol) = UPPER(%s) ORDER BY asset_id LIMIT 1",
            (symbol.strip(),),
        )
        row = cur.fetchone()
        return row[0] if row else None


# 稳定币/计价币等噪声 token：归因到这类卡无意义，跳过（保持原 symbol）
_NOISE_TOKENS = {
    "USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "BUSD", "PYUSD",
    "GUSD", "USD", "RLUSD", "FRAX", "USDD", "USD1", "USD0", "EUSD",
    "sUSD", "LUSD", "MIM", "DAI", "EURC", "USDP",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="修复 kol_signal onchain 错资产归因")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    parser.add_argument("--limit", type=int, default=5000, help="最多处理行数")
    args = parser.parse_args()

    settings = get_settings(require_database=True)
    with get_connection(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, symbol, event_token, asset_id
                FROM biz.kol_signal
                WHERE signal_category = 'onchain'
                  AND event_token IS NOT NULL
                  AND event_token <> ''
                  AND UPPER(event_token) <> UPPER(COALESCE(symbol, ''))
                ORDER BY signal_id DESC
                LIMIT %s
            """, (args.limit,))
            rows = cur.fetchall()

        if not rows:
            print("无需修正：没有 onchain 信号存在 event_token 与 symbol 不一致")
            return 0

        print(f"发现 {len(rows)} 条 onchain 信号 event_token 与 symbol 不一致：")
        print(f"{'signal_id':>10}  {'symbol':>10}  {'event_token':>12}  {'asset_id':>8}  -> 动作")
        to_fix = []
        for sid, symbol, event_token, asset_id in rows:
            token_upper = str(event_token or "").upper().strip()
            if token_upper in _NOISE_TOKENS:
                print(f"{sid:>10}  {str(symbol or ''):>10}  {token_upper:>12}  {str(asset_id or ''):>8}  -> 跳过（稳定币/计价币噪声，保持原 symbol）")
                continue
            new_asset = _match_asset(conn, token_upper)
            if not new_asset:
                print(f"{sid:>10}  {str(symbol or ''):>10}  {token_upper:>12}  {str(asset_id or ''):>8}  -> 跳过（event_token 无法匹配资产）")
                continue
            action = f"symbol={token_upper}, asset_id={new_asset}"
            to_fix.append((sid, token_upper, new_asset))
            print(f"{sid:>10}  {str(symbol or ''):>10}  {token_upper:>12}  {str(asset_id or ''):>8}  -> {action}")

        if not to_fix:
            print("没有可回写资产的行（event_token 均无法匹配 core.asset）")
            return 0

        if args.dry_run:
            print(f"\n[dry-run] 将回写 {len(to_fix)} 行：symbol=event_token + asset_id 重定向")
            return 0

        with conn.cursor() as cur:
            for sid, token, new_asset in to_fix:
                cur.execute(
                    "UPDATE biz.kol_signal SET symbol = %s, asset_id = %s, updated_at = NOW() "
                    "WHERE signal_id = %s",
                    (token.upper(), new_asset, sid),
                )
        conn.commit()
        print(f"\n已回写 {len(to_fix)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())