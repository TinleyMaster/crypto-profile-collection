"""enrich 地址标签补充提醒 — 监控无标签地址数量，超过阈值发邮件。

定期检查 onchain_transfer_log 中没有标签的陌生地址数量，
超过阈值时发邮件提醒，让你记得跑 backfill_enrich_labels.py。

用法：
    # 默认阈值 500 个，超过就发邮件
    python enrich_reminder.py

    # 自定义阈值 2000 个
    python enrich_reminder.py --threshold 2000

    # 只检查特定链
    python enrich_reminder.py --chain eth,base

    # 不发邮件，只打印结果（测试用）
    python enrich_reminder.py --dry-run

可加到 cron / 任务计划，每天跑一次。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from crypto_research.config import get_settings
from crypto_research.db.conn import get_connection
from crypto_research.clients.notifier import EmailNotifier

# 支持的链（和 enrich 保持一致）
ENRICH_SUPPORTED_CHAINS = {"eth", "base", "polygon"}


def count_unlabeled(conn, chain: str) -> int:
    """统计转账记录中出现、但地址标签表中没有的地址数量。

    同时考虑 onchain_address_label 和 onchain_exchange_wallet 两张表。
    """
    case_sensitive = chain in {"solana", "tron", "ton", "sui", "aptos"}

    if case_sensitive:
        addr_col_from = "from_address"
        addr_col_to = "to_address"
    else:
        addr_col_from = "LOWER(from_address)"
        addr_col_to = "LOWER(to_address)"

    sql = f"""
        WITH all_addrs AS (
            SELECT DISTINCT {addr_col_from} AS addr
            FROM biz.onchain_transfer_log
            WHERE chain = %s AND from_address IS NOT NULL
            UNION
            SELECT DISTINCT {addr_col_to} AS addr
            FROM biz.onchain_transfer_log
            WHERE chain = %s AND to_address IS NOT NULL
        ),
        labeled AS (
            SELECT LOWER(address) AS addr
            FROM biz.onchain_address_label
            WHERE chain = %s
            UNION
            SELECT LOWER(address) AS addr
            FROM biz.onchain_exchange_wallet
            WHERE chain = %s
        )
        SELECT COUNT(*)
        FROM all_addrs ac
        LEFT JOIN labeled l ON LOWER(ac.addr) = LOWER(l.addr)
        WHERE l.addr IS NULL AND ac.addr IS NOT NULL AND ac.addr <> ''
    """
    with conn.cursor() as cur:
        cur.execute(sql, (chain, chain, chain, chain))
        return cur.fetchone()[0]


def count_recent_unlabeled(conn, chain: str, days: int = 7) -> int:
    """统计最近 N 天新增的无标签地址数量（更有参考价值）。"""
    case_sensitive = chain in {"solana", "tron", "ton", "sui", "aptos"}

    if case_sensitive:
        from_op = "t.from_address"
        to_op = "t.to_address"
    else:
        from_op = "LOWER(t.from_address)"
        to_op = "LOWER(t.to_address)"

    sql = f"""
        SELECT COUNT(DISTINCT addr)
        FROM (
            SELECT {from_op} AS addr FROM biz.onchain_transfer_log t
            WHERE t.chain = %s
              AND t.block_timestamp >= NOW() - INTERVAL '{days} days'
            UNION
            SELECT {to_op} AS addr FROM biz.onchain_transfer_log t
            WHERE t.chain = %s
              AND t.block_timestamp >= NOW() - INTERVAL '{days} days'
        ) all_addrs
        LEFT JOIN biz.onchain_address_label l
          ON all_addrs.addr = {'l.address' if case_sensitive else 'LOWER(l.address)'}
         AND l.chain = %s
        WHERE l.address IS NULL
          AND all_addrs.addr IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql, (chain, chain, chain))
        return cur.fetchone()[0]


def build_email_html(results: list[dict], threshold: int) -> str:
    """构建提醒邮件 HTML。"""
    rows_html = ""
    total = 0
    for r in results:
        total += r["total"]
        status = "🔴 超过阈值" if r["total"] >= threshold else "🟡 数量不少"
        rows_html += f"""
        <tr style="background:#fff">
          <td style="padding:10px;border:1px solid #eee;font-weight:bold">{r['chain']}</td>
          <td style="padding:10px;border:1px solid #eee;text-align:right">{r['total']:,}</td>
          <td style="padding:10px;border:1px solid #eee;text-align:right">{r['recent']:,}</td>
          <td style="padding:10px;border:1px solid #eee">{status}</td>
        </tr>"""

    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:auto;font-size:14px;color:#333">
      <h2 style="color:#dc2626;margin-bottom:8px">⏰ 地址标签补充提醒</h2>
      <p>检测到有 <b style="font-size:18px;color:#dc2626">{total:,}</b> 个转账地址还没有标签，
      建议跑一下 <code>backfill_enrich_labels.py</code> 补充。</p>

      <table style="border-collapse:collapse;width:100%;margin-top:16px">
        <thead>
          <tr style="background:#f9fafb">
            <th style="padding:10px;border:1px solid #eee;text-align:left">链</th>
            <th style="padding:10px;border:1px solid #eee;text-align:right">无标签地址总数</th>
            <th style="padding:10px;border:1px solid #eee;text-align:right">近 7 天新增</th>
            <th style="padding:10px;border:1px solid #eee;text-align:left">状态</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>

      <div style="margin-top:20px;padding:14px;background:#f0fdf4;border-radius:6px">
        <div style="font-weight:bold;margin-bottom:6px">💡 补充命令（本地执行）</div>
        <pre style="margin:0;padding:10px;background:#1f2937;color:#f9fafb;border-radius:4px;overflow-x:auto">python 05_代码与脚本/scripts/bin/backfill_enrich_labels.py --chain eth,base,polygon --concurrency 8</pre>
        <p style="margin:8px 0 0;font-size:12px;color:#6b7280">
          预计速度约 0.3~0.5 地址/秒，1 万个地址大约 6~9 小时。
          可以先跑 <code>--dry-run</code> 预览数量。
        </p>
      </div>

      <div style="margin-top:20px;padding:14px;background:#fffbeb;border-radius:6px">
        <div style="font-weight:bold;margin-bottom:6px">📌 小提示</div>
        <ul style="margin:0;padding-left:20px;color:#6b7280;line-height:1.8">
          <li>优先补充高频地址（脚本默认按频次倒序）</li>
          <li>并发数建议 5~10，太高容易触发 etherscan 反爬</li>
          <li>可随时 Ctrl+C 中断，下次接着跑（幂等）</li>
        </ul>
      </div>

      <p style="margin-top:24px;font-size:12px;color:#999">
        — 本邮件由 enrich_reminder.py 自动发送，阈值 {threshold:,} 个
      </p>
    </div>
    """


def main():
    parser = argparse.ArgumentParser(description="enrich 地址标签补充提醒")
    parser.add_argument("--chain", type=str, default="eth,base,polygon",
                        help=f"要检查的链，多个用逗号分隔（默认: eth,base,polygon）")
    parser.add_argument("--threshold", type=int, default=500,
                        help="无标签地址数量超过该值时发邮件（默认 500）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印结果，不发邮件")
    parser.add_argument("--force", action="store_true",
                        help="强制发送邮件（即使没超过阈值，用于测试）")
    parser.add_argument("--db-url", type=str, default=None,
                        help="数据库连接串（默认从 settings 读）")
    args = parser.parse_args()

    chains = [c.strip() for c in args.chain.split(",") if c.strip()]
    for c in chains:
        if c not in ENRICH_SUPPORTED_CHAINS:
            print(f"⚠️  不支持的链: {c}（支持: {', '.join(sorted(ENRICH_SUPPORTED_CHAINS))}）")
            sys.exit(1)

    # 连接数据库
    if args.db_url:
        conn_cm = get_connection(args.db_url)
    else:
        settings = get_settings(require_database=True)
        conn_cm = get_connection(settings.database_url)

    # 统计每条链
    results = []
    with conn_cm as conn:
        for chain in chains:
            total = count_unlabeled(conn, chain)
            recent = count_recent_unlabeled(conn, chain, days=7)
            results.append({"chain": chain, "total": total, "recent": recent})
            status = "⚠️ " if total >= args.threshold else "  "
            print(f"  {status} {chain:10s}  无标签总数: {total:>8,}  近7天新增: {recent:>8,}")

    # 判断是否需要发邮件
    any_over = any(r["total"] >= args.threshold for r in results)

    if args.dry_run:
        print(f"\n[dry-run] 阈值: {args.threshold:,}")
        print(f"[dry-run] 是否触发提醒: {'是' if any_over else '否'}")
        return

    if not any_over and not args.force:
        print(f"\n✅ 所有链无标签地址均低于阈值（{args.threshold:,}），不发邮件。")
        return

    # 发邮件
    settings = get_settings()
    notifier = EmailNotifier(settings)

    if not notifier.configured:
        print("\n⚠️  SMTP 未配置，无法发送邮件。请设置 SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO 环境变量。")
        sys.exit(1)

    subject = f"⏰ 地址标签补充提醒 — {sum(r['total'] for r in results):,} 个地址待补充"
    html = build_email_html(results, args.threshold)

    ok, msg = notifier.send(subject, html, from_name="地址标签监控")
    if ok:
        print(f"\n✅ 邮件已发送: {msg}")
    else:
        print(f"\n❌ 邮件发送失败: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
